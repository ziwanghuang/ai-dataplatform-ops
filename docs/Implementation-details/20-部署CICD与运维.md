# 20 - 部署、CI/CD 与运维

> **设计文档引用**：`07-部署架构与CICD流水线.md`  
> **职责边界**：K8s 编排、Argo Rollouts 金丝雀发布、7 轨版本管理、6 阶段 CI/CD、3 层回滚、多环境管理  
> **优先级**：P2

---

## 1. K8s 部署拓扑

### 1.1 总体架构图

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    Namespace: aiops-production                          │
│                                                                         │
│  ┌──────────────────── Ingress (APISIX) ──────────────────────┐        │
│  │  /api/v1/agent/*  → agent-service     (3 Pod, HPA 2-10)   │        │
│  │  /api/v1/tools/*  → mcp-gateway       (2 Pod, HPA 2-6)    │        │
│  │  /ws/*            → agent-service     (WebSocket sticky)   │        │
│  │  /api/v1/eval/*   → eval-service      (1 Pod, no HPA)     │        │
│  │  /metrics         → prometheus-proxy  (内网隔离)            │        │
│  └────────────────────────────────────────────────────────────┘        │
│                                                                         │
│  ┌─── 无状态服务层 ───────────────────────────────────────────┐        │
│  │  agent-service × 3    (Python, FastAPI + LangGraph)        │        │
│  │  mcp-gateway × 2      (Go, 中间件链 + 路由)                │        │
│  │  rag-service × 2      (Python, 检索 + 重排)                │        │
│  │  eval-service × 1     (Python, 离线评估调度)                │        │
│  └────────────────────────────────────────────────────────────┘        │
│                                                                         │
│  ┌─── MCP Server 组 ─────────────────────────────────────────┐        │
│  │  hdfs-mcp × 2   kafka-mcp × 2   es-mcp × 2               │        │
│  │  yarn-mcp × 2   zk-mcp × 1      ops-mcp × 1 (高权限)     │        │
│  └────────────────────────────────────────────────────────────┘        │
│                                                                         │
│  ┌─── GPU 节点池（nodeSelector: gpu=true）────────────────────┐        │
│  │  embedding-service × 1  (BGE-M3, NVIDIA A10, 24GB)        │        │
│  │  reranker-service × 1   (BGE-Reranker-Large, A10)         │        │
│  │  llm-service × 0~2     (Qwen2.5-72B, A100 80GB，可选)     │        │
│  └────────────────────────────────────────────────────────────┘        │
│                                                                         │
│  ┌─── 有状态服务层 ──────────────────────────────────────────┐        │
│  │  PostgreSQL 16     (Patroni 主从, 3 节点, PVC 100Gi)      │        │
│  │  Redis 7           (Sentinel 3+1+1, PVC 20Gi)             │        │
│  │  Milvus 2.4        (3 节点, NVMe SSD, PVC 200Gi)          │        │
│  │  Neo4j 5           (3 Core + 2 ReadReplica, PVC 50Gi)     │        │
│  │  Elasticsearch 8   (3 Master + 3 Data, PVC 500Gi)         │        │
│  └────────────────────────────────────────────────────────────┘        │
│                                                                         │
│  ┌─── 可观测性 ──────────────────────────────────────────────┐        │
│  │  Prometheus + VictoriaMetrics (长期存储)                    │        │
│  │  Jaeger (Collector + Query + Cassandra)                    │        │
│  │  LangFuse (Agent 行为追踪)                                 │        │
│  │  Grafana (Dashboard 4 块)                                  │        │
│  │  Alertmanager (告警路由)                                    │        │
│  └────────────────────────────────────────────────────────────┘        │
└─────────────────────────────────────────────────────────────────────────┘
```

### 1.2 核心 Deployment — agent-service

```yaml
# infra/k8s/base/agent-deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: agent-service
  namespace: aiops-production
  labels:
    app.kubernetes.io/name: agent-service
    app.kubernetes.io/part-of: aiops
    app.kubernetes.io/version: "v1.3.0"
spec:
  replicas: 3
  selector:
    matchLabels:
      app: agent-service
  template:
    metadata:
      labels:
        app: agent-service
      annotations:
        prometheus.io/scrape: "true"
        prometheus.io/port: "8080"
        prometheus.io/path: "/metrics"
    spec:
      serviceAccountName: agent-sa
      terminationGracePeriodSeconds: 60    # Agent 可能有长事务
      containers:
        - name: agent
          image: registry.internal/aiops/agent-service:v1.3.0
          ports:
            - name: http
              containerPort: 8080
            - name: ws
              containerPort: 8081
          envFrom:
            - configMapRef:
                name: agent-config
            - secretRef:
                name: agent-secrets
          env:
            - name: POD_NAME
              valueFrom:
                fieldRef:
                  fieldPath: metadata.name
            - name: OTEL_SERVICE_NAME
              value: "agent-service"
            - name: OTEL_EXPORTER_OTLP_ENDPOINT
              value: "http://jaeger-collector:4317"
          resources:
            requests:
              cpu: "500m"
              memory: "1Gi"
            limits:
              cpu: "2000m"
              memory: "4Gi"
          readinessProbe:
            httpGet:
              path: /healthz
              port: 8080
            initialDelaySeconds: 10
            periodSeconds: 10
            failureThreshold: 3
          livenessProbe:
            httpGet:
              path: /healthz
              port: 8080
            initialDelaySeconds: 30
            periodSeconds: 30
            failureThreshold: 5
          startupProbe:
            httpGet:
              path: /healthz
              port: 8080
            initialDelaySeconds: 5
            periodSeconds: 5
            failureThreshold: 12    # 最多等 60s 启动
          lifecycle:
            preStop:
              exec:
                command: ["/bin/sh", "-c", "sleep 5"]   # 等 SIGTERM 传播
      topologySpreadConstraints:
        - maxSkew: 1
          topologyKey: kubernetes.io/hostname
          whenUnsatisfiable: DoNotSchedule
          labelSelector:
            matchLabels:
              app: agent-service
```

### 1.3 HPA 配置

```yaml
# infra/k8s/base/hpa.yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: agent-service-hpa
  namespace: aiops-production
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: agent-service
  minReplicas: 2
  maxReplicas: 10
  behavior:
    scaleUp:
      stabilizationWindowSeconds: 60
      policies:
        - type: Pods
          value: 2
          periodSeconds: 60
    scaleDown:
      stabilizationWindowSeconds: 300    # 缩容保守
      policies:
        - type: Pods
          value: 1
          periodSeconds: 120
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 70
    - type: Resource
      resource:
        name: memory
        target:
          type: Utilization
          averageUtilization: 80
    - type: Pods
      pods:
        metric:
          name: aiops_agent_active_sessions
        target:
          type: AverageValue
          averageValue: "50"     # 每 Pod 平均 50 个活跃会话
```

### 1.4 Service & Ingress

```yaml
# infra/k8s/base/service.yaml
apiVersion: v1
kind: Service
metadata:
  name: agent-service
  namespace: aiops-production
spec:
  selector:
    app: agent-service
  ports:
    - name: http
      port: 80
      targetPort: 8080
    - name: ws
      port: 8081
      targetPort: 8081
  type: ClusterIP
---
# infra/k8s/base/ingress.yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: aiops-ingress
  namespace: aiops-production
  annotations:
    kubernetes.io/ingress.class: apisix
    k8s.apisix.apache.org/cors-allow-origin: "https://aiops.internal.example.com"
    k8s.apisix.apache.org/enable-websocket: "true"
spec:
  rules:
    - host: aiops.internal.example.com
      http:
        paths:
          - path: /api/v1/agent
            pathType: Prefix
            backend:
              service:
                name: agent-service
                port:
                  number: 80
          - path: /api/v1/tools
            pathType: Prefix
            backend:
              service:
                name: mcp-gateway
                port:
                  number: 80
          - path: /ws
            pathType: Prefix
            backend:
              service:
                name: agent-service
                port:
                  number: 8081
  tls:
    - hosts: [aiops.internal.example.com]
      secretName: aiops-tls-cert
```

### 1.5 NetworkPolicy（零信任网络）

```yaml
# infra/k8s/base/network-policy.yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: agent-service-netpol
  namespace: aiops-production
spec:
  podSelector:
    matchLabels:
      app: agent-service
  policyTypes: [Ingress, Egress]
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: ingress-apisix
      ports:
        - port: 8080
        - port: 8081
  egress:
    - to:
        - podSelector:
            matchLabels:
              app: mcp-gateway
      ports:
        - port: 80
    - to:
        - podSelector:
            matchLabels:
              app: postgresql
      ports:
        - port: 5432
    - to:
        - podSelector:
            matchLabels:
              app: redis
      ports:
        - port: 6379
    - to:   # LLM API 外部访问
        - ipBlock:
            cidr: 0.0.0.0/0
            except: [10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16]
      ports:
        - port: 443
```

---

## 2. 七轨版本管理

AI 系统的版本管理不同于传统微服务，模型、Prompt、知识库的变更都可能导致行为漂移。
我们定义 **7 个独立版本轨道**，每个轨道独立版本化，但发布时必须作为整体验证：

```
┌─────────────────────────────────────────────────────────────────┐
│                    Release Manifest v1.3.0                       │
│                                                                   │
│  Track 1: Code        → agent-service:v1.3.0, mcp-server:v1.2.1 │
│  Track 2: Models      → gpt-4o-2024-11-20, bge-m3, reranker     │
│  Track 3: Knowledge   → vector-index:v3, kg:2026-03-24          │
│  Track 4: Prompts     → prompt-templates:v2.3                    │
│  Track 5: Eval Sets   → eval-dataset:v2.1                       │
│  Track 6: Config      → agent-config:v1.3.0                     │
│  Track 7: Infra       → k8s-manifests:v1.3.0                    │
│                                                                   │
│  规则：任意轨道变更 → 全量评测 → 门禁通过 → 才能发布             │
└─────────────────────────────────────────────────────────────────┘
```

### 2.1 Release Manifest 完整定义

```yaml
# release-manifest.yaml
release:
  version: "v1.3.0"
  date: "2026-03-28"
  author: "ci-bot"
  previous_version: "v1.2.1"
  changelog: |
    - feat: 新增 Kafka ISR 告警关联规则
    - fix: DiagnosticNode 并行工具超时未正确处理
    - perf: RAG 检索延迟 p99 从 800ms 降至 400ms

# Track 1: 代码版本
components:
  agent_service:
    image: "registry.internal/aiops/agent-service:v1.3.0"
    git_commit: "abc123def456"
    git_branch: "main"
  mcp_gateway:
    image: "registry.internal/aiops/mcp-gateway:v1.3.0"
    git_commit: "abc123def456"
  mcp_servers:
    hdfs: "registry.internal/aiops/mcp-hdfs:v1.2.1"
    kafka: "registry.internal/aiops/mcp-kafka:v1.3.0"
    es: "registry.internal/aiops/mcp-es:v1.2.0"
    yarn: "registry.internal/aiops/mcp-yarn:v1.3.0"
  rag_service:
    image: "registry.internal/aiops/rag-service:v1.3.0"
    git_commit: "abc123def456"

# Track 2: 模型版本
models:
  primary_llm:
    provider: "openai"
    model: "gpt-4o-2024-11-20"
    temperature: 0.1
    max_tokens: 4096
  fallback_llm:
    provider: "anthropic"
    model: "claude-3-5-sonnet-20241022"
  local_llm:
    provider: "vllm"
    model: "Qwen/Qwen2.5-72B-Instruct"
    endpoint: "http://llm-service:8000/v1"
  embedding:
    model: "BAAI/bge-m3"
    dimension: 1024
  reranker:
    model: "BAAI/bge-reranker-large"

# Track 3: 知识库版本
knowledge:
  vector_index:
    collection: "ops_documents"
    version: "index-2026-03-25-v3"
    doc_count: 2847
    embedding_model: "bge-m3"
  knowledge_graph:
    version: "kg-2026-03-24"
    node_count: 1523
    relationship_count: 4891

# Track 4: Prompt 版本
prompts:
  version: "v2.3"
  git_tag: "prompts-v2.3"
  templates:
    triage: "triage_system_v2.3.txt"
    diagnostic: "diagnostic_system_v2.3.txt"
    planning: "planning_system_v2.3.txt"
    report: "report_system_v2.3.txt"

# Track 5: 评测集版本
evaluation:
  eval_set_version: "v2.1"
  case_count: 156
  last_run: "2026-03-28T10:30:00Z"
  results:
    triage_accuracy: 0.93
    diagnosis_accuracy: 0.78
    remediation_safety: 1.00
    faithfulness: 0.87
    rag_recall: 0.82
    latency_p95_ms: 12500
    security_pass_rate: 1.00

# Track 6: 运行配置版本
config:
  version: "v1.3.0"
  token_budget:
    triage: 2000
    diagnosis: 8000
    report: 3000
  circuit_breaker:
    threshold: 5
    timeout: 30
  rate_limit:
    global_rps: 100
    per_user_rps: 10

# Track 7: 基础设施版本
infra:
  k8s_manifests: "v1.3.0"
  helm_chart: "aiops-0.3.0"
  terraform_state: "s3://aiops-tfstate/v1.3.0"
```

### 2.2 Manifest 生成器

```python
# infra/ci/generate_manifest.py
"""
自动生成 Release Manifest

从各个数据源收集版本信息，组装成一份完整的 manifest。
在 CI/CD Stage 4 自动执行。
"""

import json
import subprocess
import yaml
from datetime import datetime, timezone
from pathlib import Path


def get_git_info() -> dict:
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True
    ).strip()
    branch = subprocess.check_output(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], text=True
    ).strip()
    return {"commit": commit, "branch": branch}


def get_eval_results(eval_dir: str) -> dict:
    """从最近一次评测结果中读取指标"""
    results_file = Path(eval_dir) / "latest_results.json"
    if results_file.exists():
        with open(results_file) as f:
            return json.load(f)
    return {}


def get_index_info(milvus_uri: str, collection: str) -> dict:
    """从 Milvus 获取索引版本信息"""
    try:
        from pymilvus import MilvusClient
        client = MilvusClient(uri=milvus_uri)
        stats = client.get_collection_stats(collection)
        return {
            "collection": collection,
            "doc_count": stats.get("row_count", 0),
        }
    except Exception:
        return {"collection": collection, "doc_count": -1}


def generate_manifest(version: str, registry: str) -> dict:
    git = get_git_info()
    eval_results = get_eval_results("eval/results/")

    manifest = {
        "release": {
            "version": version,
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "author": "ci-bot",
            "git_commit": git["commit"],
        },
        "components": {
            "agent_service": {
                "image": f"{registry}/agent-service:{version}",
                "git_commit": git["commit"],
            },
            "mcp_gateway": {
                "image": f"{registry}/mcp-gateway:{version}",
                "git_commit": git["commit"],
            },
        },
        "evaluation": eval_results,
    }

    return manifest


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--registry", default="registry.internal/aiops")
    parser.add_argument("--output", default="release-manifest.yaml")
    args = parser.parse_args()

    manifest = generate_manifest(args.version, args.registry)

    with open(args.output, "w") as f:
        yaml.dump(manifest, f, default_flow_style=False, allow_unicode=True)

    print(f"✅ Release manifest generated: {args.output}")
```

### 2.3 版本兼容性矩阵

| 轨道变更 | 影响范围 | 回滚粒度 | 评测要求 |
|---------|---------|---------|---------|
| Code 代码 | 全链路 | 分钟级（Argo abort） | 全量评测 |
| Model 模型 | LLM 输出质量 | 秒级（ConfigMap 切换） | 全量评测 |
| Knowledge 知识库 | RAG 召回质量 | 小时级（alias 切换） | RAG 子集评测 |
| Prompt | Agent 行为 | 秒级（ConfigMap） | 全量评测 |
| Eval Set 评测集 | 门禁标准 | 无需回滚 | 自身验证 |
| Config 配置 | 运行参数 | 秒级（ConfigMap） | 冒烟测试 |
| Infra 基础设施 | 部署拓扑 | 分钟级（kubectl rollback） | 冒烟测试 |

**核心规则：修改任何一个轨道 → 必须重新跑评测 → 通过门禁 → 才能发布。**

> **🔧 工程难点：七轨版本管理——AI 系统的版本复杂度远超传统应用**
>
> **挑战**：传统 Web 应用的版本管理只有"代码"一个维度——Git commit hash 就是版本。但 AI Agent 系统有 7 个独立变化的维度（Code 代码、Model 模型、Knowledge 知识库、Prompt 提示词、Eval Set 评测集、Config 配置、Infra 基础设施），每个维度的变更都可能影响系统行为，且它们之间存在复杂的兼容性约束——Prompt v2 可能依赖 Model v3 的输出格式（v2 Prompt 期望 JSON 输出，但 Model v2 只能输出纯文本），Knowledge v5 的向量是 BGE-M3 模型生成的（用 text-embedding-3 模型无法检索）。如果不跟踪这些跨轨道的兼容性，可能出现"Prompt 升级了但 Model 没跟上"的组合导致生产事故。更实际的问题是**回滚粒度**——传统回滚是"回滚到上个 Git commit"，但 AI 系统的故障可能是"Prompt 改坏了但代码没问题"，如果回滚整个 commit（包括代码变更），就过度回滚了。
>
> **解决方案**：`ReleaseManifest` 记录每次发布时 7 个轨道的精确版本（Code Git SHA、Model 名称+版本、Knowledge Collection 版本、Prompt 模板 hash、Eval Set hash、Config hash、Infra Kustomize overlay 版本），形成一个"快照"。版本兼容性矩阵（§2.3）定义了每个轨道变更的影响范围、回滚粒度和评测要求——Code 变更需要全量评测 + 分钟级回滚（Argo Rollouts abort），Prompt 变更只需要全量评测 + 秒级回滚（ConfigMap 切换），Config 变更只需冒烟测试 + 秒级回滚。回滚时可以只回滚单个轨道（如"只回滚 Prompt 到上个版本，保持 Code 不变"），而非全量回滚。`ManifestGenerator` 在 CI 中自动生成 Manifest，确保每次发布都有完整的版本快照，出问题时可以精确定位"是哪个轨道的变更引入了问题"。所有 Manifest 存储在 PostgreSQL 中，支持任意两个版本之间的 diff 比较（"v1.2.3 和 v1.2.2 的差异是：Prompt 从 v5 升到 v6 + Config 的 token_budget 从 12K 改为 15K"）。

---

## 3. CI/CD 6 阶段流水线（GitHub Actions）

### 3.1 流水线架构

```
┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐
│  Stage 1 │──→│  Stage 2 │──→│  Stage 3 │──→│  Stage 4 │──→│  Stage 5 │──→│  Stage 6 │
│  Build   │   │  Test    │   │  AI Eval │   │  Canary  │   │  Observe │   │  Promote │
│  2-5 min │   │  5-15min │   │ 15-30min │   │  部署 5% │   │  监控15m │   │  逐步放量 │
└──────────┘   └──────────┘   └──────────┘   └──────────┘   └──────────┘   └──────────┘
     │               │              │              │              │              │
   构建镜像       单元/集成测试   LLM 评测集    Argo Rollout    Prometheus     100% 流量
   安全扫描       覆盖率门禁     门禁检查       5% 金丝雀       自动分析       全量切换
```

### 3.2 完整 GitHub Actions 配置

```yaml
# .github/workflows/ci-cd.yml
name: AIOps CI/CD Pipeline
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true    # PR 推送新 commit 取消旧的

env:
  REGISTRY: registry.internal/aiops
  VERSION: ${{ github.sha }}
  PYTHON_VERSION: "3.12"
  GO_VERSION: "1.22"

permissions:
  contents: read
  packages: write
  id-token: write    # OIDC for Kubernetes auth

jobs:
  # ═══════════════════════════════════════
  # Stage 1: Build & Scan (2-5 min)
  # ═══════════════════════════════════════
  build:
    runs-on: ubuntu-latest
    outputs:
      image_digest: ${{ steps.docker.outputs.digest }}
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0    # 完整历史，用于版本号生成

      - name: Set up Go
        uses: actions/setup-go@v5
        with:
          go-version: ${{ env.GO_VERSION }}
          cache: true

      - name: Build Go MCP Server
        run: |
          cd go
          CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
            go build -ldflags="-s -w -X main.version=${{ env.VERSION }}" \
            -o ../bin/mcp-server ./cmd/mcp-server/
          CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
            go build -ldflags="-s -w" \
            -o ../bin/mcp-gateway ./cmd/mcp-gateway/

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

      - name: Login to Registry
        uses: docker/login-action@v3
        with:
          registry: registry.internal
          username: ${{ secrets.REGISTRY_USER }}
          password: ${{ secrets.REGISTRY_PASSWORD }}

      - name: Build & Push agent-service
        id: docker
        uses: docker/build-push-action@v5
        with:
          context: .
          file: infra/docker/Dockerfile.agent
          push: true
          tags: |
            ${{ env.REGISTRY }}/agent-service:${{ env.VERSION }}
            ${{ env.REGISTRY }}/agent-service:latest
          cache-from: type=gha
          cache-to: type=gha,mode=max
          platforms: linux/amd64

      - name: Build & Push mcp-gateway
        uses: docker/build-push-action@v5
        with:
          context: .
          file: infra/docker/Dockerfile.mcp-gateway
          push: true
          tags: ${{ env.REGISTRY }}/mcp-gateway:${{ env.VERSION }}
          cache-from: type=gha
          cache-to: type=gha,mode=max

      - name: Trivy Vulnerability Scan
        uses: aquasecurity/trivy-action@master
        with:
          image-ref: ${{ env.REGISTRY }}/agent-service:${{ env.VERSION }}
          severity: HIGH,CRITICAL
          exit-code: 1
          format: sarif
          output: trivy-results.sarif

      - name: Upload Trivy Results
        uses: github/codeql-action/upload-sarif@v3
        with:
          sarif_file: trivy-results.sarif

  # ═══════════════════════════════════════
  # Stage 2: Test (5-15 min)
  # ═══════════════════════════════════════
  test-python:
    runs-on: ubuntu-latest
    needs: build
    services:
      postgres:
        image: postgres:16-alpine
        env:
          POSTGRES_USER: test
          POSTGRES_PASSWORD: test
          POSTGRES_DB: aiops_test
        ports: ["5432:5432"]
        options: >-
          --health-cmd pg_isready
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5
      redis:
        image: redis:7-alpine
        ports: ["6379:6379"]
        options: >-
          --health-cmd "redis-cli ping"
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5
    env:
      DATABASE_URL: "postgresql://test:test@localhost:5432/aiops_test"
      REDIS_URL: "redis://localhost:6379/0"
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ env.PYTHON_VERSION }}
      - name: Install dependencies
        run: |
          cd python
          pip install poetry
          poetry install --with dev
      - name: Run linters
        run: |
          cd python
          poetry run ruff check src/ tests/
          poetry run mypy src/ --ignore-missing-imports
      - name: Run tests with coverage
        run: |
          cd python
          poetry run pytest tests/ -v \
            --cov=aiops \
            --cov-report=xml:coverage.xml \
            --cov-report=term-missing \
            --cov-fail-under=75 \
            -x --timeout=60
      - name: Upload Coverage
        uses: codecov/codecov-action@v4
        with:
          file: python/coverage.xml
          flags: python

  test-go:
    runs-on: ubuntu-latest
    needs: build
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-go@v5
        with:
          go-version: ${{ env.GO_VERSION }}
          cache: true
      - name: Run Go linter
        uses: golangci/golangci-lint-action@v4
        with:
          working-directory: go
          version: v1.59
      - name: Run Go tests
        run: |
          cd go
          go test ./... -v -race -count=1 \
            -coverprofile=coverage.out \
            -covermode=atomic \
            -timeout=120s
          go tool cover -func=coverage.out | tail -1
      - name: Check Go coverage threshold
        run: |
          cd go
          COVERAGE=$(go tool cover -func=coverage.out | tail -1 | awk '{print $3}' | sed 's/%//')
          echo "Go coverage: ${COVERAGE}%"
          if (( $(echo "$COVERAGE < 70" | bc -l) )); then
            echo "::error::Go coverage ${COVERAGE}% is below 70% threshold"
            exit 1
          fi

  # ═══════════════════════════════════════
  # Stage 3: AI Evaluation (15-30 min)
  # ═══════════════════════════════════════
  ai-eval:
    runs-on: ubuntu-latest
    needs: [test-python, test-go]
    if: github.ref == 'refs/heads/main'    # 只在主分支执行
    env:
      OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
      LANGFUSE_PUBLIC_KEY: ${{ secrets.LANGFUSE_PUBLIC_KEY }}
      LANGFUSE_SECRET_KEY: ${{ secrets.LANGFUSE_SECRET_KEY }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ env.PYTHON_VERSION }}
      - name: Install dependencies
        run: |
          cd python
          pip install poetry
          poetry install
      - name: Run AI evaluation suite
        run: |
          cd python
          poetry run python -m aiops.eval.runner \
            --dataset eval/datasets/v2.1/ \
            --output eval/results/latest_results.json \
            --parallel 4 \
            --timeout 1800
      - name: Gate check — AI quality thresholds
        run: |
          cd python
          poetry run python -m aiops.eval.gate_check \
            eval/results/latest_results.json \
            --config eval/gate_config.yaml
      - name: Upload eval results
        uses: actions/upload-artifact@v4
        with:
          name: eval-results
          path: python/eval/results/latest_results.json

  # ═══════════════════════════════════════
  # Stage 4-6: Deploy (Canary → Promote)
  # ═══════════════════════════════════════
  deploy:
    runs-on: ubuntu-latest
    needs: ai-eval
    environment: production    # 需要手动审批
    steps:
      - uses: actions/checkout@v4

      - name: Download eval results
        uses: actions/download-artifact@v4
        with:
          name: eval-results
          path: eval-results/

      - name: Generate release manifest
        run: |
          python infra/ci/generate_manifest.py \
            --version ${{ env.VERSION }} \
            --registry ${{ env.REGISTRY }} \
            --output release-manifest.yaml
          cat release-manifest.yaml

      - name: Configure Kubernetes
        uses: azure/k8s-set-context@v4
        with:
          kubeconfig: ${{ secrets.KUBECONFIG }}

      - name: Deploy Kustomize manifests
        run: |
          cd infra/k8s/overlays/production
          kustomize edit set image \
            agent-service=${{ env.REGISTRY }}/agent-service:${{ env.VERSION }} \
            mcp-gateway=${{ env.REGISTRY }}/mcp-gateway:${{ env.VERSION }}
          kustomize build . | kubectl apply -f -

      - name: Stage 4 — Start Canary (5%)
        run: |
          kubectl argo rollouts set image agent-service \
            agent=${{ env.REGISTRY }}/agent-service:${{ env.VERSION }} \
            -n aiops-production

      - name: Stage 5 — Wait for canary analysis
        run: |
          kubectl argo rollouts status agent-service \
            -n aiops-production \
            --timeout 45m

      - name: Stage 6 — Promote to 100%
        run: |
          kubectl argo rollouts promote agent-service \
            -n aiops-production

      - name: Archive release manifest
        run: |
          kubectl create configmap release-manifest-${{ env.VERSION }} \
            --from-file=release-manifest.yaml \
            -n aiops-production \
            --dry-run=client -o yaml | kubectl apply -f -

      - name: Notify deployment success
        if: success()
        run: |
          curl -X POST "${{ secrets.WECOM_WEBHOOK }}" \
            -H 'Content-Type: application/json' \
            -d '{"msgtype":"markdown","markdown":{"content":"✅ **AIOps v${{ env.VERSION }}** 部署成功\n> 评测通过 | 金丝雀验证通过 | 全量发布完成"}}'

      - name: Notify deployment failure
        if: failure()
        run: |
          curl -X POST "${{ secrets.WECOM_WEBHOOK }}" \
            -H 'Content-Type: application/json' \
            -d '{"msgtype":"markdown","markdown":{"content":"❌ **AIOps v${{ env.VERSION }}** 部署失败\n> 请检查 CI/CD 日志"}}'
```

### 3.3 门禁配置（gate_config.yaml）

```yaml
# eval/gate_config.yaml
# AI 质量门禁 — 任何指标未达标则阻止发布

gates:
  # 安全类：零容忍
  security_pass_rate:
    threshold: 1.0
    operator: ">="
    blocking: true
    description: "安全测试必须 100% 通过"

  remediation_safety:
    threshold: 1.0
    operator: ">="
    blocking: true
    description: "修复建议不能包含危险操作"

  # 质量类：必须达标
  triage_accuracy:
    threshold: 0.85
    operator: ">="
    blocking: true
    description: "分诊准确率不低于 85%"

  diagnosis_accuracy:
    threshold: 0.70
    operator: ">="
    blocking: true
    description: "诊断准确率不低于 70%"

  faithfulness:
    threshold: 0.80
    operator: ">="
    blocking: true
    description: "忠实度不低于 80%"

  rag_recall:
    threshold: 0.75
    operator: ">="
    blocking: true
    description: "RAG 召回率不低于 75%"

  # 性能类：警告但不阻止
  latency_p95_ms:
    threshold: 15000
    operator: "<="
    blocking: false
    description: "P95 延迟不超过 15s（超标警告）"
```

---

## 4. Argo Rollouts 金丝雀配置

### 4.1 Rollout 资源定义

```yaml
# infra/k8s/base/agent-rollout.yaml
apiVersion: argoproj.io/v1alpha1
kind: Rollout
metadata:
  name: agent-service
  namespace: aiops-production
spec:
  replicas: 6
  revisionHistoryLimit: 5
  selector:
    matchLabels:
      app: agent-service
  template:
    metadata:
      labels:
        app: agent-service
    spec:
      containers:
        - name: agent
          image: registry.internal/aiops/agent-service:stable
          ports:
            - containerPort: 8080
  strategy:
    canary:
      # 金丝雀 Service 分流
      canaryService: agent-service-canary
      stableService: agent-service-stable
      trafficRouting:
        apisix:
          route:
            name: agent-route
            rules:
              - backends:
                  - serviceName: agent-service-canary
                    servicePort: 80
                  - serviceName: agent-service-stable
                    servicePort: 80
      steps:
        # Step 1: 5% 流量，观察 15 分钟
        - setWeight: 5
        - pause: {duration: 15m}
        - analysis:
            templates:
              - templateName: agent-canary-analysis
            args:
              - name: canary-hash
                valueFrom:
                  podTemplateHashValue: Latest

        # Step 2: 20% 流量，观察 15 分钟
        - setWeight: 20
        - pause: {duration: 15m}
        - analysis:
            templates:
              - templateName: agent-canary-analysis

        # Step 3: 50% 流量，观察 15 分钟
        - setWeight: 50
        - pause: {duration: 15m}
        - analysis:
            templates:
              - templateName: agent-canary-analysis

        # Step 4: 全量
        - setWeight: 100
      
      # 自动回滚
      maxUnavailable: 1
      maxSurge: 2
```

### 4.2 AnalysisTemplate — 金丝雀质量检测

```yaml
# infra/k8s/base/analysis-template.yaml
apiVersion: argoproj.io/v1alpha1
kind: AnalysisTemplate
metadata:
  name: agent-canary-analysis
  namespace: aiops-production
spec:
  args:
    - name: canary-hash
  metrics:
    # 指标 1: HTTP 错误率 < 5%
    - name: error-rate
      interval: 60s
      count: 10
      failureLimit: 3
      successCondition: result[0] < 0.05
      provider:
        prometheus:
          address: http://prometheus:9090
          query: |
            sum(rate(http_requests_total{
              app="agent-service",
              pod_template_hash="{{args.canary-hash}}",
              status=~"5.."
            }[5m])) /
            sum(rate(http_requests_total{
              app="agent-service",
              pod_template_hash="{{args.canary-hash}}"
            }[5m]))

    # 指标 2: P95 延迟 < 15s（Agent 完整调用链）
    - name: latency-p95
      interval: 60s
      count: 10
      failureLimit: 3
      successCondition: result[0] < 15000
      provider:
        prometheus:
          address: http://prometheus:9090
          query: |
            histogram_quantile(0.95,
              sum(rate(aiops_agent_duration_seconds_bucket{
                pod_template_hash="{{args.canary-hash}}"
              }[5m])) by (le)
            ) * 1000

    # 指标 3: Agent 诊断准确率（从 LangFuse 实时采集）
    - name: diagnosis-quality
      interval: 300s    # 每 5 分钟检测
      count: 3
      failureLimit: 1
      successCondition: result[0] > 0.65
      provider:
        prometheus:
          address: http://prometheus:9090
          query: |
            aiops_eval_realtime_accuracy{
              metric="diagnosis",
              pod_template_hash="{{args.canary-hash}}"
            }

    # 指标 4: OOM/Crash 事件为 0
    - name: pod-restarts
      interval: 60s
      count: 10
      failureLimit: 1
      successCondition: result[0] == 0
      provider:
        prometheus:
          address: http://prometheus:9090
          query: |
            sum(increase(kube_pod_container_status_restarts_total{
              namespace="aiops-production",
              pod=~"agent-service.*"
            }[15m]))
```

### 4.3 流量切分可视化

```
时间线：                    0min     15min    30min    45min    60min
                            │        │        │        │        │
Stable (旧版本):  ████████████████████████████████████████████████
                  95%──────→ 80%────→ 50%────→ 0%
                            │        │        │        │
Canary (新版本):  ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░
                  5%───────→ 20%────→ 50%────→ 100%
                            │        │        │
                        Analysis  Analysis  Analysis
                        (pass?)   (pass?)   (pass?)
                            │        │        │
                         fail→abort fail→abort fail→abort → 自动回滚
```

---

## 5. 三层回滚策略

### 5.0 回滚决策流程

```
告警触发 / 人工发现问题
        │
        ▼
  ┌─────────────────┐
  │ 问题类型判断      │
  └─────────────────┘
        │
   ┌────┼────┬────────────┐
   │         │            │
   ▼         ▼            ▼
Prompt/    代码/         知识库/
Config     模型行为       索引
   │         │            │
   ▼         ▼            ▼
秒级回滚   分钟级回滚   小时级回滚
ConfigMap  Argo abort   Alias切换
   │         │            │
   ▼         ▼            ▼
验证生效   验证 Pod     验证 RAG
           就绪          召回
```

| 层级 | 时间 | 回滚内容 | 方式 | 验证方法 |
|------|------|---------|------|---------|
| **秒级** | <30s | Prompt/Config/Model 切换 | ConfigMap patch + rollout restart | curl healthz + 冒烟 |
| **分钟级** | 1-3min | Agent/MCP 代码 | Argo Rollouts abort | Pod Ready + 冒烟 |
| **小时级** | 5-60min | 向量索引/知识图谱 | Milvus alias / Neo4j 快照 | RAG 召回验证 |

### 5.1 秒级回滚（Prompt / Config / Model）

```bash
#!/bin/bash
# infra/ci/rollback_prompt.sh
#
# 用途：回滚 Prompt 版本或 LLM 配置
# 原理：修改 ConfigMap → 触发 rolling restart → 新 Pod 加载新配置
# 耗时：~30s

set -euo pipefail

NAMESPACE="aiops-production"
DEPLOYMENT="agent-service"

usage() {
    echo "用法:"
    echo "  回滚 Prompt:  $0 prompt v2.2"
    echo "  切换 LLM:     $0 model gpt-4o-2024-08-06"
    echo "  回滚 Config:  $0 config v1.2.1"
    exit 1
}

[[ $# -lt 2 ]] && usage

TYPE=$1
TARGET=$2

echo "🔄 开始 ${TYPE} 回滚到 ${TARGET}..."

case $TYPE in
    prompt)
        kubectl -n $NAMESPACE patch configmap agent-config --type merge \
            -p "{\"data\":{\"PROMPT_VERSION\":\"$TARGET\"}}"
        echo "✅ Prompt 版本已更新为 $TARGET"
        ;;
    model)
        kubectl -n $NAMESPACE patch configmap agent-config --type merge \
            -p "{\"data\":{\"PRIMARY_LLM_MODEL\":\"$TARGET\"}}"
        echo "✅ 主 LLM 已切换为 $TARGET"
        ;;
    config)
        # 从历史 ConfigMap 恢复
        kubectl -n $NAMESPACE get configmap "agent-config-${TARGET}" -o yaml \
            | sed 's/agent-config-'${TARGET}'/agent-config/' \
            | kubectl apply -f -
        echo "✅ 配置已回滚到 $TARGET"
        ;;
    *)
        usage
        ;;
esac

# 触发 rolling restart
kubectl -n $NAMESPACE rollout restart deployment/$DEPLOYMENT
echo "⏳ 等待 rolling restart..."
kubectl -n $NAMESPACE rollout status deployment/$DEPLOYMENT --timeout=120s

# 验证
echo "🔍 验证健康状态..."
for i in $(seq 1 5); do
    STATUS=$(kubectl -n $NAMESPACE exec deploy/$DEPLOYMENT -- \
        curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/healthz)
    if [[ "$STATUS" == "200" ]]; then
        echo "✅ 回滚完成，服务健康"
        exit 0
    fi
    sleep 2
done
echo "❌ 回滚后健康检查失败！"
exit 1
```

### 5.2 分钟级回滚（代码）

```bash
#!/bin/bash
# infra/ci/rollback_code.sh
#
# 用途：回滚 Agent/MCP 代码版本
# 原理：Argo Rollouts abort 当前发布 → 自动回到上一个 stable 版本
# 耗时：1-3 分钟

set -euo pipefail

NAMESPACE="aiops-production"
ROLLOUT="agent-service"

echo "🚨 开始代码回滚..."

# 1. 中止当前金丝雀
echo "Step 1: Aborting current rollout..."
kubectl argo rollouts abort $ROLLOUT -n $NAMESPACE

# 2. 等待回滚完成
echo "Step 2: Waiting for rollback..."
kubectl argo rollouts status $ROLLOUT -n $NAMESPACE --timeout 5m

# 3. 确认当前版本
CURRENT_IMAGE=$(kubectl -n $NAMESPACE get rollout $ROLLOUT \
    -o jsonpath='{.status.stableRS}')
echo "Step 3: Stable RS: $CURRENT_IMAGE"

# 4. 验证所有 Pod 就绪
echo "Step 4: Verifying pod readiness..."
kubectl -n $NAMESPACE wait --for=condition=Ready \
    pod -l app=agent-service --timeout=120s

READY_COUNT=$(kubectl -n $NAMESPACE get pods -l app=agent-service \
    --field-selector=status.phase=Running --no-headers | wc -l)
echo "Ready pods: $READY_COUNT"

# 5. 冒烟测试
echo "Step 5: Smoke test..."
SMOKE_RESULT=$(kubectl -n $NAMESPACE exec deploy/agent-service -- \
    curl -s http://localhost:8080/api/v1/agent/health)
echo "Smoke test result: $SMOKE_RESULT"

if echo "$SMOKE_RESULT" | grep -q '"status":"healthy"'; then
    echo "✅ 代码回滚完成，服务正常"
else
    echo "❌ 代码回滚后冒烟测试失败！"
    exit 1
fi
```

### 5.3 小时级回滚（索引 / 知识图谱）

```bash
#!/bin/bash
# infra/ci/rollback_index.sh
#
# 用途：回滚向量索引或知识图谱
# 原理：
#   - Milvus: 切换 collection alias 到旧版本
#   - Neo4j: 从快照恢复
# 耗时：5-60 分钟（取决于数据量）

set -euo pipefail

usage() {
    echo "用法:"
    echo "  回滚向量索引:  $0 milvus index-2026-03-20-v2"
    echo "  回滚知识图谱:  $0 neo4j kg-2026-03-20"
    exit 1
}

[[ $# -lt 2 ]] && usage

TYPE=$1
TARGET=$2
MILVUS_URI="http://milvus:19530"
NEO4J_URI="bolt://neo4j:7687"

case $TYPE in
    milvus)
        echo "🔄 切换 Milvus alias 到 $TARGET..."
        python3 << EOF
from pymilvus import MilvusClient, utility

client = MilvusClient(uri="$MILVUS_URI")

# 检查目标 collection 是否存在
collections = utility.list_collections()
if "$TARGET" not in collections:
    print("❌ Collection $TARGET 不存在！")
    print(f"可用 collections: {collections}")
    exit(1)

# 检查目标 collection 数据量
stats = client.get_collection_stats("$TARGET")
print(f"目标 collection 文档数: {stats.get('row_count', 'unknown')}")

# 切换 alias
try:
    utility.alter_alias(collection_name="$TARGET", alias="ops_documents")
    print("✅ alias 'ops_documents' 已切换到 $TARGET")
except Exception:
    # alias 不存在则创建
    utility.create_alias(collection_name="$TARGET", alias="ops_documents")
    print("✅ alias 'ops_documents' 已创建并指向 $TARGET")

# 验证
alias_info = utility.list_aliases(collection_name="$TARGET")
print(f"验证: {alias_info}")
EOF
        ;;

    neo4j)
        echo "🔄 从快照恢复 Neo4j 到 $TARGET..."
        python3 << EOF
from neo4j import GraphDatabase

driver = GraphDatabase.driver("$NEO4J_URI", auth=("neo4j", "password"))

with driver.session() as session:
    # 查看当前快照列表
    result = session.run("CALL dbms.listConfig() YIELD name, value WHERE name = 'dbms.backup.enabled'")
    
    # 恢复快照（需要管理员操作）
    print("⚠️  Neo4j 快照恢复需要以下手动步骤:")
    print(f"  1. neo4j-admin database restore --from-path=/backups/$TARGET default")
    print(f"  2. 重启 Neo4j: kubectl -n aiops-production rollout restart statefulset/neo4j")
    print(f"  3. 验证: MATCH (n) RETURN count(n)")

driver.close()
EOF
        ;;

    *)
        usage
        ;;
esac

# 验证 RAG 召回质量
echo "🔍 验证 RAG 召回质量..."
python3 << 'EOF'
import asyncio
import httpx

SMOKE_QUERIES = [
    "HDFS NameNode heap 使用率过高",
    "Kafka consumer lag 持续增长",
    "YARN NodeManager 频繁失联",
]

async def verify_rag():
    async with httpx.AsyncClient(timeout=30) as client:
        for query in SMOKE_QUERIES:
            resp = await client.post(
                "http://rag-service/api/v1/retrieve",
                json={"query": query, "top_k": 5},
            )
            data = resp.json()
            results = data.get("results", [])
            print(f"  查询: {query[:30]}... → 召回 {len(results)} 条")
            if len(results) == 0:
                print(f"  ❌ 零召回！可能需要进一步排查")
                return False
    return True

ok = asyncio.run(verify_rag())
if ok:
    print("✅ RAG 召回验证通过")
else:
    print("❌ RAG 召回验证失败")
    exit(1)
EOF
```

---

## 6. 多环境管理（Kustomize）

### 6.0 环境矩阵

| 环境 | 用途 | LLM Provider | 副本数 | 数据 | 特殊配置 |
|------|------|-------------|--------|------|---------|
| **dev** | 本地开发 | mock | 1 | mock | OTel 关闭 |
| **staging** | 集成测试 | openai | 2 | 脱敏生产副本 | OTel 开启 |
| **production** | 生产 | openai + 本地 | 3-10 | 真实 | 全部开启 |

### 6.1 base/kustomization.yaml

```yaml
# infra/k8s/base/kustomization.yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

resources:
  - namespace.yaml
  - agent-rollout.yaml       # Argo Rollout（替代 Deployment）
  - mcp-deployment.yaml
  - rag-deployment.yaml
  - service.yaml
  - ingress.yaml
  - hpa.yaml
  - network-policy.yaml
  - serviceaccount.yaml
  - pdb.yaml                 # PodDisruptionBudget

commonLabels:
  app.kubernetes.io/part-of: aiops
  app.kubernetes.io/managed-by: kustomize

commonAnnotations:
  team: "aiops-platform"
```

### 6.2 base/pdb.yaml (Pod Disruption Budget)

```yaml
# infra/k8s/base/pdb.yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: agent-service-pdb
  namespace: aiops-production
spec:
  minAvailable: 2    # 至少 2 个 Pod 存活
  selector:
    matchLabels:
      app: agent-service
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: mcp-gateway-pdb
  namespace: aiops-production
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app: mcp-gateway
```

### 6.3 overlays/dev/kustomization.yaml

```yaml
# infra/k8s/overlays/dev/kustomization.yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ../../base
namespace: aiops-dev

# Dev 环境：单副本、Mock LLM、关闭 OTel
patches:
  - target:
      kind: Rollout
      name: agent-service
    patch: |
      - op: replace
        path: /spec/replicas
        value: 1
      - op: replace
        path: /spec/template/spec/containers/0/resources/limits/memory
        value: "1Gi"
      - op: replace
        path: /spec/template/spec/containers/0/resources/limits/cpu
        value: "500m"
  - target:
      kind: Deployment
      name: mcp-gateway
    patch: |
      - op: replace
        path: /spec/replicas
        value: 1
  - target:
      kind: HorizontalPodAutoscaler
      name: agent-service-hpa
    patch: |
      - op: replace
        path: /spec/minReplicas
        value: 1
      - op: replace
        path: /spec/maxReplicas
        value: 2

configMapGenerator:
  - name: agent-config
    behavior: merge
    literals:
      - ENV=dev
      - LLM_PROVIDER=mock
      - OTEL_ENABLED=false
      - LOG_LEVEL=DEBUG
      - HITL_AUTO_APPROVE=true    # Dev 环境自动审批
      - RATE_LIMIT_GLOBAL_RPS=1000
      - CIRCUIT_BREAKER_ENABLED=false
```

### 6.4 overlays/staging/kustomization.yaml

```yaml
# infra/k8s/overlays/staging/kustomization.yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ../../base
namespace: aiops-staging

patches:
  - target:
      kind: Rollout
      name: agent-service
    patch: |
      - op: replace
        path: /spec/replicas
        value: 2
      - op: replace
        path: /spec/template/spec/containers/0/resources/limits/memory
        value: "2Gi"
  - target:
      kind: HorizontalPodAutoscaler
      name: agent-service-hpa
    patch: |
      - op: replace
        path: /spec/minReplicas
        value: 1
      - op: replace
        path: /spec/maxReplicas
        value: 4

configMapGenerator:
  - name: agent-config
    behavior: merge
    literals:
      - ENV=staging
      - LLM_PROVIDER=openai
      - OTEL_ENABLED=true
      - LOG_LEVEL=INFO
      - HITL_AUTO_APPROVE=false
      - RATE_LIMIT_GLOBAL_RPS=50
      - CIRCUIT_BREAKER_ENABLED=true
```

### 6.5 overlays/production/kustomization.yaml

```yaml
# infra/k8s/overlays/production/kustomization.yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ../../base
namespace: aiops-production

patches:
  - target:
      kind: Rollout
      name: agent-service
    patch: |
      - op: replace
        path: /spec/replicas
        value: 3
      - op: replace
        path: /spec/template/spec/containers/0/resources/limits/memory
        value: "4Gi"
      - op: replace
        path: /spec/template/spec/containers/0/resources/limits/cpu
        value: "2000m"
  - target:
      kind: HorizontalPodAutoscaler
      name: agent-service-hpa
    patch: |
      - op: replace
        path: /spec/minReplicas
        value: 2
      - op: replace
        path: /spec/maxReplicas
        value: 10

configMapGenerator:
  - name: agent-config
    behavior: merge
    literals:
      - ENV=production
      - LLM_PROVIDER=openai
      - OTEL_ENABLED=true
      - OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger-collector:4317
      - LOG_LEVEL=INFO
      - PROMPT_VERSION=v2.3
      - HITL_AUTO_APPROVE=false
      - RATE_LIMIT_GLOBAL_RPS=100
      - RATE_LIMIT_PER_USER_RPS=10
      - CIRCUIT_BREAKER_ENABLED=true
      - CIRCUIT_BREAKER_THRESHOLD=5
      - SENSITIVE_DATA_HANDLER_ENABLED=true
```

> **🔧 工程难点：K8s 生产部署拓扑——零信任网络与 AI 特有的资源隔离需求**
>
> **挑战**：AI Agent 系统的 K8s 部署比传统微服务更复杂——除了常规的 Deployment/Service/Ingress，还需要考虑：(1) **GPU 资源调度**——Reranker 和 Embedding 模型需要 GPU，但 GPU 节点有限且昂贵，需要与 LLM 推理服务（如果自部署）共享 GPU 资源而不是互相抢占；(2) **内存波动大**——Agent 处理一个复杂诊断请求时内存可能从 500MB 飙到 2GB（因为 `AgentState` 中积累了多轮工具返回数据），HPA 基于 CPU 的弹性扩缩无法应对内存突增，可能导致 OOM kill；(3) **安全隔离**——ops-mcp Server（可以执行重启、扩缩容等危险操作）必须与只读 MCP Server 网络隔离，防止被入侵的只读 Server 横向移动到 ops-mcp；(4) **多环境一致性**——dev/staging/production 三个环境的配置差异（LLM 模型、Token 预算、审批策略、安全级别）如果通过环境变量管理会变成"环境变量地狱"（100+ 个变量）。
>
> **解决方案**：K8s 部署使用 Kustomize 的 base + overlays 模式——`base/` 定义所有环境共享的资源（Deployment 模板、Service、PDB），`overlays/{dev,staging,production}/` 只覆盖环境差异部分（副本数、资源限制、环境变量、安全策略）。生产环境的关键配置：`agent-service` 设置 `memory.request=1Gi` / `memory.limit=3Gi`（预留内存突增空间），`readinessProbe` 检查 `/health` 端点（确保 LLM Client 连接正常），`livenessProbe` 设置 `initialDelaySeconds=30`（模型加载需要时间）。NetworkPolicy 实现零信任网络——`ops-mcp` Pod 只允许 `mcp-gateway` Pod 访问（`podSelector` + `namespaceSelector`），`agent-service` 只允许从 Ingress Controller 接收流量，各 MCP Server 之间完全不可互相访问。Pod Disruption Budget（`pdb.yaml`）确保滚动更新时至少保留 1 个 `agent-service` Pod 可用（`minAvailable=1`）。环境差异通过 Kustomize 的 `configMapGenerator` 和 `patchesStrategicMerge` 管理——dev 环境用 `gpt-4o-mini`（省钱）+ `HITL_AUTO_APPROVE=true`（跳过审批加速调试），production 用 `gpt-4o/deepseek-v3` + `HITL_AUTO_APPROVE=false` + `CIRCUIT_BREAKER_ENABLED=true`。

---

## 7. Docker 多阶段构建

### 7.1 agent-service Dockerfile

```dockerfile
# infra/docker/Dockerfile.agent
# ===== Stage 1: Go Builder (MCP Server) =====
FROM golang:1.22-alpine AS go-builder
RUN apk add --no-cache git ca-certificates
WORKDIR /build
COPY go/go.mod go/go.sum ./
RUN go mod download
COPY go/ .
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build -ldflags="-s -w -X main.buildTime=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    -o /mcp-server ./cmd/mcp-server/
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build -ldflags="-s -w" \
    -o /mcp-gateway ./cmd/mcp-gateway/

# ===== Stage 2: Python Builder =====
FROM python:3.12-slim AS py-builder
RUN pip install --no-cache-dir poetry==1.8.3
WORKDIR /build
COPY python/pyproject.toml python/poetry.lock ./
# 只安装生产依赖
RUN poetry config virtualenvs.in-project true && \
    poetry install --no-dev --no-interaction --no-ansi
COPY python/src/ ./src/

# ===== Stage 3: Runtime =====
FROM python:3.12-slim

# 安全：安装安全补丁 + 证书
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates curl tini && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 复制构建产物
COPY --from=go-builder /mcp-server /usr/local/bin/mcp-server
COPY --from=go-builder /mcp-gateway /usr/local/bin/mcp-gateway
COPY --from=py-builder /build/.venv /app/.venv
COPY --from=py-builder /build/src /app/src
COPY prompt-templates/ /app/prompts/
COPY eval/ /app/eval/

# 环境变量
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/src" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 非 root 用户
RUN groupadd --system app && \
    useradd --system --gid app --create-home app && \
    chown -R app:app /app
USER app
WORKDIR /app

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8080/healthz || exit 1

# 使用 tini 作为 PID 1（正确处理信号）
ENTRYPOINT ["tini", "--"]
CMD ["uvicorn", "aiops.web.app:app", "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "4", "--loop", "uvloop", "--http", "httptools"]
```

### 7.2 MCP Gateway Dockerfile

```dockerfile
# infra/docker/Dockerfile.mcp-gateway
FROM golang:1.22-alpine AS builder
RUN apk add --no-cache git ca-certificates
WORKDIR /build
COPY go/go.mod go/go.sum ./
RUN go mod download
COPY go/ .
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build -ldflags="-s -w" -o /mcp-gateway ./cmd/mcp-gateway/

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=builder /mcp-gateway /mcp-gateway
COPY --from=builder /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/
USER nonroot:nonroot
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s CMD ["/mcp-gateway", "-healthcheck"]
ENTRYPOINT ["/mcp-gateway"]
```

### 7.3 Docker Compose 本地开发环境

```yaml
# docker-compose.dev.yml
version: "3.9"
services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: aiops
      POSTGRES_PASSWORD: devpass
      POSTGRES_DB: aiops_dev
    ports: ["5432:5432"]
    volumes:
      - pg_data:/var/lib/postgresql/data
      - ./infra/sql/init.sql:/docker-entrypoint-initdb.d/init.sql
    healthcheck:
      test: pg_isready -U aiops
      interval: 10s
      timeout: 5s

  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]
    command: redis-server --maxmemory 256mb --maxmemory-policy allkeys-lru

  elasticsearch:
    image: elasticsearch:8.12.0
    environment:
      - discovery.type=single-node
      - xpack.security.enabled=false
      - ES_JAVA_OPTS=-Xms512m -Xmx512m
    ports: ["9200:9200"]
    volumes:
      - es_data:/usr/share/elasticsearch/data

  milvus:
    image: milvusdb/milvus:v2.4.0
    command: milvus run standalone
    environment:
      ETCD_ENDPOINTS: etcd:2379
      MINIO_ADDRESS: minio:9000
    ports: ["19530:19530"]
    depends_on: [etcd, minio]

  etcd:
    image: quay.io/coreos/etcd:v3.5.12
    command: etcd --advertise-client-urls=http://0.0.0.0:2379 --listen-client-urls=http://0.0.0.0:2379
    volumes:
      - etcd_data:/etcd

  minio:
    image: minio/minio:RELEASE.2024-03-15T01-07-19Z
    command: server /data --console-address ":9001"
    environment:
      MINIO_ACCESS_KEY: minioadmin
      MINIO_SECRET_KEY: minioadmin
    volumes:
      - minio_data:/data

  neo4j:
    image: neo4j:5-community
    environment:
      NEO4J_AUTH: neo4j/devpass
      NEO4J_PLUGINS: '["apoc"]'
    ports: ["7474:7474", "7687:7687"]
    volumes:
      - neo4j_data:/data

  jaeger:
    image: jaegertracing/all-in-one:1.56
    environment:
      COLLECTOR_OTLP_ENABLED: "true"
    ports:
      - "16686:16686"    # UI
      - "4317:4317"      # OTLP gRPC
      - "4318:4318"      # OTLP HTTP

  langfuse:
    image: langfuse/langfuse:2
    environment:
      DATABASE_URL: postgresql://aiops:devpass@postgres:5432/langfuse
      NEXTAUTH_SECRET: dev-secret-change-me
      NEXTAUTH_URL: http://localhost:3000
    ports: ["3000:3000"]
    depends_on: [postgres]

  prometheus:
    image: prom/prometheus:v2.51.0
    ports: ["9090:9090"]
    volumes:
      - ./infra/monitoring/prometheus.yml:/etc/prometheus/prometheus.yml

  grafana:
    image: grafana/grafana:10.4.0
    ports: ["3001:3000"]
    environment:
      GF_SECURITY_ADMIN_PASSWORD: admin
    volumes:
      - ./infra/monitoring/grafana/dashboards:/var/lib/grafana/dashboards
      - ./infra/monitoring/grafana/provisioning:/etc/grafana/provisioning

volumes:
  pg_data:
  es_data:
  etcd_data:
  minio_data:
  neo4j_data:
```

---

## 8. 生产上线 Checklist

### 8.1 基础设施就绪

- [ ] K8s 集群资源充足（CPU/内存/GPU 预留）
- [ ] 节点标签配置：`gpu=true`（GPU 节点）、`ssd=true`（存储节点）
- [ ] Namespace `aiops-production` 创建，ResourceQuota 设置
- [ ] NetworkPolicy 配置完成（零信任）
- [ ] PodDisruptionBudget 配置完成

### 8.2 数据层就绪

- [ ] PostgreSQL 高可用验证（Patroni 故障切换演练）
- [ ] Redis Sentinel 验证（主从切换演练）
- [ ] Milvus 集群健康（3 节点，数据同步）
- [ ] Neo4j Causal Cluster（3 Core 一致性验证）
- [ ] Elasticsearch 分片分配正常（green 状态）
- [ ] 所有数据库连接池参数优化

### 8.3 AI 系统就绪

- [ ] Release Manifest 生成并归档
- [ ] 所有 Prompt 版本锁定（git tag: `prompts-vX.Y`）
- [ ] 向量索引构建完成（alias 指向正确版本）
- [ ] 知识图谱数据加载完成
- [ ] 模型端点可达（OpenAI API / Local LLM）
- [ ] Token Budget 配置合理

### 8.4 评测与安全

- [ ] 全量评测集通过（门禁全绿）
  - [ ] triage_accuracy >= 0.85
  - [ ] diagnosis_accuracy >= 0.70
  - [ ] faithfulness >= 0.80
  - [ ] security_pass_rate == 1.00
- [ ] 安全测试通过（Prompt 注入 / RBAC / 数据脱敏）
- [ ] Trivy 镜像扫描无 HIGH/CRITICAL

### 8.5 可观测性就绪

- [ ] Prometheus 指标采集验证（30+ 自定义指标可见）
- [ ] Jaeger 追踪链路完整（Python→MCP Gateway→MCP Server）
- [ ] LangFuse Agent 行为追踪正常
- [ ] Grafana Dashboard 配置完成（4 块）
- [ ] 告警规则配置并验证（7 组）
- [ ] 告警通知渠道测试（企微 webhook）

### 8.6 运维准备

- [ ] RBAC 权限矩阵审查（5 角色 × 10 维度）
- [ ] 回滚流程演练完成（秒级 / 分钟级 / 小时级）
- [ ] 运维 Runbook 编写完成
- [ ] 值班 On-Call 排表
- [ ] 容量规划文档（未来 3 个月流量预估）

---

## 9. 运维 Runbook

### 9.1 Agent 响应慢排查

```
症状: Agent 响应时间 P95 > 15s
排查步骤:

1. 查看 Grafana Dashboard → Agent 总览面板
   - 确认是否全局性还是某个 Pod
   - 确认哪个阶段耗时最长（triage/diagnosis/report）

2. 如果 triage 慢:
   kubectl -n aiops-production logs -l app=agent-service --tail=100 | grep "triage"
   → 检查 LLM API 延迟
   → 检查规则引擎命中率（命中率低说明走了 LLM）

3. 如果 diagnosis 慢:
   → 检查 MCP 工具调用延迟（Jaeger 链路追踪）
   → 检查是否在做多轮假设-验证循环
   → 检查 RAG 检索延迟

4. 如果是 LLM 延迟:
   → 检查 OpenAI API 状态页
   → 考虑切换到 fallback 模型: ./rollback_prompt.sh model claude-3-5-sonnet

5. 紧急处理: 启用降级模式
   kubectl -n aiops-production patch configmap agent-config --type merge \
     -p '{"data":{"DEGRADED_MODE":"true"}}'
   → 跳过 LLM 诊断，直接使用规则引擎
```

### 9.2 MCP 工具调用失败排查

```
症状: 工具调用错误率 > 5%

1. 确认影响范围:
   kubectl -n aiops-production logs -l app=mcp-gateway --tail=200 | grep "ERROR"
   → 是哪些工具失败？是所有还是特定组件？

2. 如果是 HDFS 工具:
   → 检查 NameNode JMX 端口是否可达
   curl http://hdfs-namenode:9870/jmx?qry=Hadoop:service=NameNode,name=FSNamesystem

3. 如果是 TBDS 外部工具:
   → 检查 TBDS-TCS API 端点可达性
   → 检查 API Key 是否过期
   → 检查网络策略是否阻断

4. 如果是熔断触发:
   → 查看 mcp-gateway 熔断器状态
   kubectl -n aiops-production exec deploy/mcp-gateway -- curl localhost:8080/debug/breakers
   → 等待自动恢复（默认 30s half-open）
   → 或手动重置: curl -X POST localhost:8080/debug/breakers/reset

5. 如果是限流触发:
   → 调整限流参数
   kubectl -n aiops-production patch configmap mcp-config --type merge \
     -p '{"data":{"RATE_LIMIT_GLOBAL_RPS":"200"}}'
```

### 9.3 RAG 召回质量下降

```
症状: faithfulness 指标下降 / 用户反馈回答不相关

1. 验证索引完整性:
   python -m aiops.rag.verify_index --collection ops_documents --sample 100

2. 检查最近是否有索引更新:
   → 查看 release-manifest.yaml 中 knowledge.vector_index 版本
   → 如有更新且质量下降 → 执行索引回滚:
   ./rollback_index.sh milvus <previous_index_version>

3. 检查 Embedding 服务:
   curl http://embedding-service:8080/health
   → 确认 GPU 可用，模型加载正常

4. 对比新旧索引:
   python -m aiops.rag.compare_index \
     --old index-2026-03-20-v2 \
     --new index-2026-03-25-v3 \
     --queries eval/datasets/v2.1/rag_queries.yaml
```

### 9.4 Pod OOM / 频繁重启

```
症状: Pod 重启次数增加

1. 确认 OOM 还是其他原因:
   kubectl -n aiops-production describe pod <pod-name> | grep -A5 "Last State"
   kubectl -n aiops-production get events --field-selector reason=OOMKilled

2. 如果是 OOM:
   → 检查内存用量趋势（Grafana → Container Memory）
   → 临时扩大 limits:
   kubectl -n aiops-production patch rollout agent-service --type json \
     -p '[{"op":"replace","path":"/spec/template/spec/containers/0/resources/limits/memory","value":"8Gi"}]'

3. 如果是应用级错误:
   kubectl -n aiops-production logs <pod-name> --previous --tail=500
   → 检查 stack trace
   → 如果是已知 bug → 代码回滚: ./rollback_code.sh

4. 长期修复:
   → 分析内存泄漏根因
   → 考虑增加 ContextCompressor 更积极的压缩策略
   → 限制单次诊断最大工具调用数
```

---

## 10. 与其他模块集成

| 上游依赖 | 关联 |
|---------|------|
| 01-工程化基础 | Makefile、pyproject.toml、go.mod 统一管理 |
| 17-可观测性 | Prometheus 指标定义、Jaeger 追踪配置、Grafana Dashboard |
| 18-评估体系 | 评测集、门禁阈值、gate_check |
| 16-安全防护 | RBAC 权限、镜像安全扫描、网络策略 |

| 下游消费 | 关联 |
|---------|------|
| 所有 Agent（04-08） | 运行环境配置、资源限制 |
| 09-MCP Server / 10-中间件 | MCP Gateway 部署、中间件配置 |
| 12-RAG / 14-索引管线 | 向量索引版本管理、索引回滚 |

---

> **本篇总结**：覆盖了从 K8s 部署拓扑、7 轨版本管理、6 阶段 CI/CD 流水线、Argo Rollouts 金丝雀、3 层回滚策略、多环境 Kustomize、Docker 构建、生产 Checklist 到运维 Runbook 的完整部署运维体系。
