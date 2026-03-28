# 07 - 部署架构与 CI/CD 流水线

> **本文档定义**：AIOps 系统的生产部署架构、Kubernetes 编排、灰度发布、版本管理和回滚策略。
> **核心挑战**：AI 系统的发布不仅仅是"代码部署"，还涉及模型、Prompt、索引、评测集的多轨协同。

---

## 一、Kubernetes 部署架构

### 1.1 整体部署拓扑

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    K8s Namespace: aiops-production                       │
│                                                                          │
│  ┌─── Ingress (APISIX / Nginx) ──────────────────────────────────────┐ │
│  │ /api/v1/agent/*  → agent-service                                   │ │
│  │ /api/v1/tools/*  → mcp-gateway                                    │ │
│  │ /ws/agent/*      → agent-service (WebSocket)                      │ │
│  │ /admin/*         → admin-dashboard                                │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  ┌─── 应用层 Deployments ─────────────────────────────────────────────┐ │
│  │                                                                    │ │
│  │  agent-service (Python)          mcp-gateway (Go)                 │ │
│  │  ┌────┐ ┌────┐ ┌────┐          ┌────┐ ┌────┐                    │ │
│  │  │Pod1│ │Pod2│ │Pod3│          │Pod1│ │Pod2│                    │ │
│  │  └────┘ └────┘ └────┘          └────┘ └────┘                    │ │
│  │  HPA: min=2, max=10             HPA: min=2, max=6                │ │
│  │  CPU target: 60%                CPU target: 70%                   │ │
│  │                                                                    │ │
│  │  hdfs-mcp-server (Go)    kafka-mcp-server (Go)    es-mcp (Go)   │ │
│  │  ┌────┐ ┌────┐          ┌────┐ ┌────┐          ┌────┐ ┌────┐  │ │
│  │  │Pod1│ │Pod2│          │Pod1│ │Pod2│          │Pod1│ │Pod2│  │ │
│  │  └────┘ └────┘          └────┘ └────┘          └────┘ └────┘  │ │
│  │                                                                    │ │
│  │  rag-service (Python)           admin-dashboard (Node.js)         │ │
│  │  ┌────┐ ┌────┐                 ┌────┐                           │ │
│  │  │Pod1│ │Pod2│                 │Pod1│                           │ │
│  │  └────┘ └────┘                 └────┘                           │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  ┌─── GPU 节点池 (节点亲和性调度) ────────────────────────────────────┐ │
│  │  embedding-service (TEI)        llm-service (vLLM)               │ │
│  │  ┌──────────────────┐           ┌──────────────────┐             │ │
│  │  │ GPU Pod (A10/L40)│           │ GPU Pod (A100)   │             │ │
│  │  │ BGE-large-zh     │           │ Qwen2.5-72B      │             │ │
│  │  └──────────────────┘           └──────────────────┘             │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  ┌─── 有状态服务 StatefulSets ────────────────────────────────────────┐ │
│  │  PostgreSQL (Patroni)    Redis (Sentinel)     Neo4j (Causal)     │ │
│  │  ┌────┐ ┌────┐         ┌────┐ ┌────┐ ┌────┐  ┌────┐ ┌────┐   │ │
│  │  │主   │ │从   │         │M   │ │S   │ │Sen │  │Core│ │Core│   │ │
│  │  └────┘ └────┘         └────┘ └────┘ └────┘  └────┘ └────┘   │ │
│  │  PVC: 100Gi             PVC: 20Gi            PVC: 50Gi         │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  ┌─── 可观测 ────────────────────────────────────────────────────────┐ │
│  │  Prometheus + VictoriaMetrics │ Jaeger │ LangFuse │ Grafana     │ │
│  └────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────┘
```

### 1.2 核心 Deployment 配置

```yaml
# agent-service Deployment
apiVersion: apps/v1
kind: Deployment
metadata:
  name: agent-service
  namespace: aiops-production
  labels:
    app: agent-service
    version: v1.3.0
spec:
  replicas: 3
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0  # 零停机更新
  selector:
    matchLabels:
      app: agent-service
  template:
    metadata:
      labels:
        app: agent-service
        version: v1.3.0
      annotations:
        prometheus.io/scrape: "true"
        prometheus.io/port: "8080"
        prometheus.io/path: "/metrics"
    spec:
      containers:
        - name: agent
          image: registry.internal/aiops/agent-service:v1.3.0
          ports:
            - containerPort: 8080
              name: http
          env:
            - name: LLM_PROVIDER
              valueFrom:
                configMapKeyRef:
                  name: agent-config
                  key: llm_provider
            - name: OPENAI_API_KEY
              valueFrom:
                secretKeyRef:
                  name: llm-credentials
                  key: openai_api_key
            - name: PROMPT_VERSION
              value: "v2.3"
            - name: OTEL_EXPORTER_OTLP_ENDPOINT
              value: "http://jaeger-collector:4317"
          resources:
            requests:
              cpu: "500m"
              memory: "1Gi"
            limits:
              cpu: "2"
              memory: "4Gi"
          readinessProbe:
            httpGet:
              path: /healthz
              port: 8080
            initialDelaySeconds: 10
            periodSeconds: 5
          livenessProbe:
            httpGet:
              path: /healthz
              port: 8080
            initialDelaySeconds: 30
            periodSeconds: 10
            failureThreshold: 3
          startupProbe:
            httpGet:
              path: /healthz
              port: 8080
            failureThreshold: 30
            periodSeconds: 5

---
# HPA 自动伸缩
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: agent-service-hpa
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: agent-service
  minReplicas: 2
  maxReplicas: 10
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 60
    - type: Pods
      pods:
        metric:
          name: aiops_agent_active_sessions
        target:
          type: AverageValue
          averageValue: "20"  # 每 Pod 最多 20 个并发会话
  behavior:
    scaleUp:
      stabilizationWindowSeconds: 60
      policies:
        - type: Pods
          value: 2
          periodSeconds: 60
    scaleDown:
      stabilizationWindowSeconds: 300  # 缩容等待 5 分钟
```

### 1.3 MCP Server Sidecar 模式（可选）

```yaml
# 方案 B：MCP Server 作为 Agent Pod 的 Sidecar
# 优点：Agent 和 MCP Server 通过 localhost stdio 通信，零网络延迟
# 缺点：耦合度高，独立伸缩困难

apiVersion: apps/v1
kind: Deployment
metadata:
  name: agent-with-mcp-sidecar
spec:
  template:
    spec:
      containers:
        # 主容器：Agent
        - name: agent
          image: registry.internal/aiops/agent-service:v1.3.0
          # ...

        # Sidecar：MCP Server
        - name: mcp-server
          image: registry.internal/aiops/mcp-server:v1.3.0
          command: ["/mcp-server", "--transport=stdio"]
          resources:
            requests:
              cpu: "200m"
              memory: "256Mi"
```

---

## 二、七轨版本管理

### 2.1 版本组成

```
一次"正确的" AIOps 系统发布需要同时绑定七个维度的版本：

Release v1.3.0
├── 📦 Code:       git@main:abc123def         (Agent 代码 + MCP Server 代码)
├── 🧠 Model:      gpt-4o-2024-11-20          (主力 LLM 版本)
├── 📐 Embedding:  bge-large-zh-v1.5          (向量化模型)
├── 📚 Index:      index-2026-03-25-v3        (向量索引版本)
├── 💬 Prompt:     prompt-pack-v2.3           (Prompt 模板版本)
├── ✅ Eval Set:   eval-set-v2.1              (评测集版本)
└── ⚙️ Config:     config-v1.3.0              (系统配置版本)

修改任何一个 → 必须重新跑评测 → 评测通过 → 才能发布
```

### 2.2 版本清单文件

```yaml
# release-manifest.yaml
# 每次发布时生成，记录所有组件版本

release:
  version: "v1.3.0"
  date: "2026-03-25"
  author: "zhangsan"
  changelog: "增加 Kafka consumer lag 诊断场景"

components:
  agent_service:
    image: "registry.internal/aiops/agent-service:v1.3.0"
    git_commit: "abc123def"
    git_tag: "v1.3.0"

  mcp_server:
    hdfs: "registry.internal/aiops/mcp-hdfs:v1.2.1"
    kafka: "registry.internal/aiops/mcp-kafka:v1.3.0"  # 本次更新
    es: "registry.internal/aiops/mcp-es:v1.1.0"

  models:
    primary_llm: "gpt-4o-2024-11-20"
    fallback_llm: "claude-3.5-sonnet-20241022"
    local_llm: "qwen2.5-72b-instruct"
    embedding: "bge-large-zh-v1.5"
    reranker: "bge-reranker-large"

  knowledge:
    vector_index: "index-2026-03-25-v3"
    knowledge_graph: "kg-2026-03-24"
    document_count: 1523
    chunk_count: 8764

  prompts:
    version: "v2.3"
    git_path: "prompt-templates/v2.3/"

  evaluation:
    eval_set: "v2.1"
    total_cases: 215
    results:
      triage_accuracy: 0.93
      diagnosis_accuracy: 0.78
      faithfulness: 0.87
      security_pass_rate: 1.00
      regression_count: 0

  config:
    version: "v1.3.0"
    llm_temperature: 0.0
    max_collection_rounds: 5
    hitl_timeout_seconds: 1800
    token_budget_diagnosis: 15000
```

---

## 三、CI/CD 流水线

### 3.1 完整发布流水线

```
┌─────────────────────────────────────────────────────────────────────┐
│                    CI/CD 发布流水线                                   │
│                                                                      │
│  代码/Prompt/配置 Push                                               │
│         │                                                            │
│         ▼                                                            │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ Stage 1: 构建 (Build) — 2-5 分钟                             │   │
│  │                                                               │   │
│  │ • Go MCP Server 编译 (CGO_ENABLED=0 静态链接)                │   │
│  │ • Python Agent 打包 (Docker 多阶段构建)                      │   │
│  │ • Prompt 模板渲染验证                                        │   │
│  │ • 镜像推送到内部 Registry                                    │   │
│  └──────────────────────────────────────────────────────────────┘   │
│         │                                                            │
│         ▼                                                            │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ Stage 2: 测试 (Test) — 5-15 分钟                             │   │
│  │                                                               │   │
│  │ 2a. 单元测试                                                 │   │
│  │     • Go: go test ./... (MCP 协议、工具、中间件)             │   │
│  │     • Python: pytest tests/unit/ (状态、路由、格式)          │   │
│  │                                                               │   │
│  │ 2b. 集成测试                                                 │   │
│  │     • MCP Client ↔ Server 端到端通信                         │   │
│  │     • Agent 工作流完整执行 (Mock LLM)                        │   │
│  │                                                               │   │
│  │ 2c. 安全测试                                                 │   │
│  │     • Prompt 注入测试集 (20+ 攻击场景)                       │   │
│  │     • 权限越权测试                                           │   │
│  │     • 依赖漏洞扫描 (Trivy)                                   │   │
│  └──────────────────────────────────────────────────────────────┘   │
│         │                                                            │
│         ▼                                                            │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ Stage 3: AI 评测 (Evaluation) — 15-30 分钟                   │   │
│  │                                                               │   │
│  │ • 离线评测集运行 (215 个用例)                                │   │
│  │ • RAGAS 指标计算 (Faithfulness/Relevancy/Precision/Recall)   │   │
│  │ • 与基线版本对比                                             │   │
│  │ • 门禁检查：                                                 │   │
│  │   - diagnosis_accuracy >= 0.70  ✅                           │   │
│  │   - faithfulness >= 0.82        ✅                           │   │
│  │   - security_pass_rate == 1.00  ✅                           │   │
│  │   - regression_count <= 3       ✅                           │   │
│  │ • 生成评测报告 (eval-report.md)                              │   │
│  └──────────────────────────────────────────────────────────────┘   │
│         │ 全部通过                                                   │
│         ▼                                                            │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ Stage 4: 灰度发布 (Canary) — 30-60 分钟                     │   │
│  │                                                               │   │
│  │ Phase 1: 5% 流量 → 观察 15 分钟                              │   │
│  │   监控：错误率 < 5%、P99 延迟 < 20s、诊断置信度 > 0.6       │   │
│  │                                                               │   │
│  │ Phase 2: 20% 流量 → 观察 15 分钟                             │   │
│  │   监控：同上 + 用户反馈无异常                                │   │
│  │                                                               │   │
│  │ Phase 3: 50% 流量 → 观察 15 分钟                             │   │
│  │   监控：同上                                                 │   │
│  │                                                               │   │
│  │ ⚠️ 任何阶段指标异常 → 自动回滚到上一版本                    │   │
│  └──────────────────────────────────────────────────────────────┘   │
│         │ 全部正常                                                   │
│         ▼                                                            │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ Stage 5: 全量发布 (Rollout) — 5 分钟                         │   │
│  │                                                               │   │
│  │ • 100% 流量切换                                              │   │
│  │ • 生成 Release Manifest (release-manifest.yaml)              │   │
│  │ • 打 Git Tag                                                 │   │
│  │ • 更新变更日志                                               │   │
│  │ • 通知团队 (企微/钉钉)                                       │   │
│  └──────────────────────────────────────────────────────────────┘   │
│         │                                                            │
│         ▼                                                            │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ Stage 6: 发布后监控 (Post-Deploy) — 24 小时                  │   │
│  │                                                               │   │
│  │ • 24 小时密切监控核心指标                                    │   │
│  │ • 自动比较发布前后指标变化                                   │   │
│  │ • 异常时自动告警 + 准备回滚                                  │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

### 3.2 灰度发布实现（Istio / Argo Rollouts）

```yaml
# Argo Rollouts 灰度发布配置
apiVersion: argoproj.io/v1alpha1
kind: Rollout
metadata:
  name: agent-service
spec:
  replicas: 6
  strategy:
    canary:
      steps:
        # Phase 1: 5% 流量
        - setWeight: 5
        - pause: {duration: 15m}
        - analysis:
            templates:
              - templateName: agent-canary-analysis
            args:
              - name: service-name
                value: agent-service

        # Phase 2: 20% 流量
        - setWeight: 20
        - pause: {duration: 15m}
        - analysis:
            templates:
              - templateName: agent-canary-analysis

        # Phase 3: 50% 流量
        - setWeight: 50
        - pause: {duration: 15m}
        - analysis:
            templates:
              - templateName: agent-canary-analysis

        # Phase 4: 全量
        - setWeight: 100

      # 灰度分析模板
      canaryMetadata:
        labels:
          role: canary
      stableMetadata:
        labels:
          role: stable

---
# 灰度分析模板
apiVersion: argoproj.io/v1alpha1
kind: AnalysisTemplate
metadata:
  name: agent-canary-analysis
spec:
  metrics:
    # 错误率检查
    - name: error-rate
      interval: 2m
      successCondition: result[0] < 0.05
      failureLimit: 3
      provider:
        prometheus:
          address: http://prometheus:9090
          query: |
            sum(rate(aiops_agent_requests_total{status="error",role="canary"}[5m]))
            / sum(rate(aiops_agent_requests_total{role="canary"}[5m]))

    # P99 延迟检查
    - name: p99-latency
      interval: 2m
      successCondition: result[0] < 20
      failureLimit: 3
      provider:
        prometheus:
          address: http://prometheus:9090
          query: |
            histogram_quantile(0.99,
              sum(rate(aiops_agent_request_duration_seconds_bucket{role="canary"}[5m]))
              by (le)
            )

    # 诊断置信度检查
    - name: diagnosis-confidence
      interval: 5m
      successCondition: result[0] > 0.6
      failureLimit: 2
      provider:
        prometheus:
          address: http://prometheus:9090
          query: |
            histogram_quantile(0.5,
              sum(rate(aiops_diagnosis_confidence_bucket{role="canary"}[10m]))
              by (le)
            )
```

---

## 四、回滚策略

### 4.1 分层回滚能力

```
┌──────────────────────────────────────────────────────────────────────┐
│                    分层回滚策略                                       │
│                                                                       │
│  ┌─── 秒级回滚 ──────────────────────────────────────────────────┐  │
│  │                                                                │  │
│  │ Prompt 回滚                                                    │  │
│  │ • 方式：ConfigMap 更新 → PROMPT_VERSION 切换                   │  │
│  │ • 时间：< 30s                                                  │  │
│  │ • 影响：新请求立即使用旧版 Prompt                              │  │
│  │                                                                │  │
│  │ 配置回滚                                                      │  │
│  │ • 方式：Feature Flag 切换 / ConfigMap 更新                     │  │
│  │ • 时间：< 30s                                                  │  │
│  │ • 影响：即时生效                                               │  │
│  │                                                                │  │
│  │ 模型回滚                                                      │  │
│  │ • 方式：ModelRouter 配置切换                                   │  │
│  │ • 时间：< 30s                                                  │  │
│  │ • 影响：新请求使用旧版模型                                     │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  ┌─── 分钟级回滚 ────────────────────────────────────────────────┐  │
│  │                                                                │  │
│  │ 代码回滚                                                      │  │
│  │ • 方式：Argo Rollouts abort → 自动回滚到 stable 版本          │  │
│  │ • 时间：1-3 分钟 (K8s Rolling Update)                         │  │
│  │ • 影响：旧 Pod 启动完成后接管流量                              │  │
│  │                                                                │  │
│  │ MCP Server 回滚                                                │  │
│  │ • 方式：独立 Deployment 回滚                                   │  │
│  │ • 时间：1-2 分钟                                               │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  ┌─── 小时级回滚 ────────────────────────────────────────────────┐  │
│  │                                                                │  │
│  │ 向量索引回滚                                                  │  │
│  │ • 方式：切换索引别名指向旧版本                                 │  │
│  │ • 时间：5-30 分钟 (取决于索引大小)                             │  │
│  │ • 前提：保留上一版本索引                                       │  │
│  │                                                                │  │
│  │ 知识图谱回滚                                                  │  │
│  │ • 方式：Neo4j 数据库快照恢复                                   │  │
│  │ • 时间：15-60 分钟                                             │  │
│  └────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
```

### 4.2 自动回滚触发条件

```yaml
# 自动回滚规则
auto_rollback:
  enabled: true
  conditions:
    # 任一条件触发 → 自动回滚
    - name: "high_error_rate"
      metric: "aiops_agent_requests_total{status='error'}"
      threshold: "> 10% for 5m"

    - name: "high_latency"
      metric: "histogram_quantile(0.99, aiops_agent_request_duration_seconds_bucket)"
      threshold: "> 30s for 5m"

    - name: "llm_all_providers_down"
      metric: "aiops_llm_calls_total{status='error'}"
      threshold: "all providers error rate > 50% for 3m"

    - name: "low_diagnosis_quality"
      metric: "histogram_quantile(0.5, aiops_diagnosis_confidence_bucket)"
      threshold: "< 0.3 for 10m"

  notification:
    channels: ["wecom-ops-group", "pagerduty"]
    message: "⚠️ AIOps 系统自动回滚: {condition_name}, 版本 {new_version} → {old_version}"
```

---

## 五、多环境管理

### 5.1 环境规划

```
┌──────────────────────────────────────────────────────────────────────┐
│                    环境规划                                           │
├────────────┬──────────┬──────────┬──────────────┬───────────────────┤
│ 环境        │ 用途      │ LLM      │ 数据         │ 发布频率         │
├────────────┼──────────┼──────────┼──────────────┼───────────────────┤
│ dev        │ 开发调试  │ Mock LLM │ Mock 数据    │ 每次 Push        │
│ staging    │ 集成测试  │ 真实 LLM │ 测试集群数据 │ 每次 MR 合入     │
│ pre-prod   │ 预发验证  │ 真实 LLM │ 生产集群(只读)│ 发布前          │
│ production │ 生产环境  │ 真实 LLM │ 生产集群     │ 灰度发布         │
└────────────┴──────────┴──────────┴──────────────┴───────────────────┘
```

### 5.2 配置管理（Kustomize）

```
k8s/
├── base/                           # 基础配置
│   ├── agent-deployment.yaml
│   ├── mcp-deployment.yaml
│   ├── service.yaml
│   ├── hpa.yaml
│   └── kustomization.yaml
│
├── overlays/
│   ├── dev/                        # 开发环境覆盖
│   │   ├── kustomization.yaml
│   │   ├── patch-replicas.yaml     # replicas: 1
│   │   └── configmap.yaml          # LLM_PROVIDER=mock
│   │
│   ├── staging/                    # 预发环境覆盖
│   │   ├── kustomization.yaml
│   │   ├── patch-replicas.yaml     # replicas: 2
│   │   └── configmap.yaml          # LLM_PROVIDER=openai
│   │
│   └── production/                 # 生产环境覆盖
│       ├── kustomization.yaml
│       ├── patch-replicas.yaml     # replicas: 3, HPA enabled
│       ├── patch-resources.yaml    # 更高资源限制
│       └── configmap.yaml          # 生产配置
```

---

## 六、Docker 镜像构建

### 6.1 多阶段构建

```dockerfile
# ========================
# Stage 1: Go MCP Server 编译
# ========================
FROM golang:1.22-alpine AS go-builder
WORKDIR /build

COPY mcp-server/go.mod mcp-server/go.sum ./
RUN go mod download

COPY mcp-server/ .
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build -ldflags="-s -w -X main.Version=${VERSION}" \
    -o /mcp-server ./cmd/server/

# ========================
# Stage 2: Python Agent 打包
# ========================
FROM python:3.12-slim AS python-builder
WORKDIR /build

COPY agent/pyproject.toml agent/poetry.lock* ./
RUN pip install --no-cache-dir poetry && \
    poetry config virtualenvs.in-project true && \
    poetry install --no-dev --no-root

COPY agent/ .
RUN poetry install --no-dev

# ========================
# Stage 3: 最终运行时镜像
# ========================
FROM python:3.12-slim AS runtime
WORKDIR /app

# 安装运行时依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl jq && rm -rf /var/lib/apt/lists/*

# 从 go-builder 复制编译好的二进制
COPY --from=go-builder /mcp-server /usr/local/bin/mcp-server

# 从 python-builder 复制虚拟环境和代码
COPY --from=python-builder /build/.venv /app/.venv
COPY --from=python-builder /build /app/agent

# Prompt 模板
COPY prompt-templates/ /app/prompt-templates/

# 环境变量
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV MCP_SERVER_PATH="/usr/local/bin/mcp-server"

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8080/healthz || exit 1

# 非 root 运行
RUN addgroup --system app && adduser --system --ingroup app app
USER app

EXPOSE 8080
CMD ["python", "-m", "uvicorn", "agent.web.app:app", "--host", "0.0.0.0", "--port", "8080"]
```

---

## 七、Makefile 与开发工作流

```makefile
# Makefile - 开发者工作流

VERSION ?= $(shell git describe --tags --always --dirty)
REGISTRY ?= registry.internal/aiops

.PHONY: help build test eval deploy rollback

help: ## 显示帮助
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ─── 构建 ────────────────────────────────────────────
build-mcp: ## 编译 Go MCP Server
	cd mcp-server && CGO_ENABLED=0 go build -o ../bin/mcp-server ./cmd/server/

build-docker: ## 构建 Docker 镜像
	docker build -t $(REGISTRY)/agent-service:$(VERSION) .

push: build-docker ## 推送镜像
	docker push $(REGISTRY)/agent-service:$(VERSION)

# ─── 测试 ────────────────────────────────────────────
test-unit: ## 运行单元测试
	cd mcp-server && go test ./... -v
	cd agent && python -m pytest tests/unit/ -v

test-integration: ## 运行集成测试
	cd agent && python -m pytest tests/integration/ -v

test-security: ## 运行安全测试
	cd agent && python -m pytest tests/security/ -v

# ─── AI 评测 ──────────────────────────────────────────
eval: ## 运行离线评测
	python eval/run_evaluation.py --dataset eval/eval_set_v2.yaml

eval-compare: ## 与基线对比
	python eval/compare_baseline.py --current eval_results.json --baseline eval/baseline.json

eval-gate: eval eval-compare ## 评测门禁（CI 中使用）
	python eval/check_gates.py eval_comparison.json

# ─── 部署 ────────────────────────────────────────────
deploy-staging: ## 部署到预发环境
	kubectl apply -k k8s/overlays/staging/

deploy-canary: ## 灰度发布到生产
	kubectl argo rollouts set image agent-service agent=$(REGISTRY)/agent-service:$(VERSION)

deploy-promote: ## 确认灰度 → 全量
	kubectl argo rollouts promote agent-service

rollback: ## 回滚到上一版本
	kubectl argo rollouts abort agent-service

# ─── 本地开发 ──────────────────────────────────────────
dev: build-mcp ## 本地启动开发环境
	cd agent && python -m agent.main

dev-web: build-mcp ## 本地启动 Web UI
	cd agent && python -m uvicorn agent.web.app:app --reload --host 0.0.0.0 --port 8080

# ─── 运维 ──────────────────────────────────────────────
logs: ## 查看 Agent 日志
	kubectl logs -f -l app=agent-service --tail=100

status: ## 查看部署状态
	kubectl argo rollouts status agent-service

dashboard: ## 打开 Grafana Dashboard
	kubectl port-forward svc/grafana 3000:3000
```

---

## 八、生产上线 Checklist

```
┌─────────────────────────────────────────────────────────────────────┐
│                    生产上线检查清单 (Go-Live Checklist)               │
│                                                                      │
│  ── 基础设施 ──────────────────────────────────────────────────────  │
│  □ K8s 集群资源充足（CPU/内存/GPU/存储）                             │
│  □ 数据库高可用配置验证（PostgreSQL 主从/Redis Sentinel）            │
│  □ 向量数据库集群健康（Milvus/pgvector）                            │
│  □ 网络策略配置（MCP Server 只能访问目标大数据组件）                │
│  □ 密钥管理（LLM API Key 在 K8s Secret 中，非明文）                │
│                                                                      │
│  ── 应用配置 ──────────────────────────────────────────────────────  │
│  □ Release Manifest 生成并归档                                      │
│  □ 所有 Prompt 版本锁定（非 latest）                                │
│  □ LLM 多供应商容灾配置验证                                        │
│  □ HITL 审批通道联通（企微/钉钉 Bot）                               │
│  □ Token 预算配置合理                                               │
│                                                                      │
│  ── 可观测性 ──────────────────────────────────────────────────────  │
│  □ Prometheus 指标采集正常                                          │
│  □ Jaeger Tracing 端到端验证                                        │
│  □ LangFuse LLM 追踪验证                                           │
│  □ Grafana Dashboard 配置完成                                       │
│  □ 告警规则配置并验证                                               │
│  □ PagerDuty/企微值班群配置                                         │
│                                                                      │
│  ── 评测验证 ──────────────────────────────────────────────────────  │
│  □ 全量评测集通过（215 用例，门禁全绿）                             │
│  □ 安全测试 100% 通过                                               │
│  □ 回归数量 = 0                                                     │
│  □ 预发环境跑完 10+ 真实场景验证                                    │
│                                                                      │
│  ── 安全 ──────────────────────────────────────────────────────────  │
│  □ RBAC 角色与权限矩阵审查                                         │
│  □ 审计日志存储与查询验证                                           │
│  □ 敏感数据脱敏验证                                                 │
│  □ Prompt 注入防御测试                                              │
│                                                                      │
│  ── 容灾 ──────────────────────────────────────────────────────────  │
│  □ LLM 多供应商切换演练                                             │
│  □ 数据库故障转移演练                                               │
│  □ 全面降级模式验证（AI 全挂 → 规则引擎兜底）                      │
│  □ 回滚流程演练（代码/Prompt/索引 各回滚一次）                     │
│                                                                      │
│  ── 文档 ──────────────────────────────────────────────────────────  │
│  □ 运维 Runbook 编写完成                                            │
│  □ 故障处理流程文档                                                 │
│  □ 值班手册更新                                                     │
│  □ 用户使用手册                                                     │
│                                                                      │
│  签字确认：                                                          │
│  开发负责人: ________    运维负责人: ________    安全负责人: ________ │
│  日期: __________                                                    │
└─────────────────────────────────────────────────────────────────────┘
```
