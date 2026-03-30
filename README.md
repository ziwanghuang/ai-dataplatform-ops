# 🤖 AIOps — 大数据平台智能运维系统

> AI-Powered Big Data Platform Intelligent Operations System

基于 **LangGraph 状态机编排** + **MCP 工具协议** 的智能运维 Agent 系统，面向大数据平台（HDFS / YARN / Kafka / Elasticsearch 等）提供自动化故障诊断、根因分析、修复方案生成与执行能力。

---

## ✨ 核心特性

| 特性 | 说明 |
|------|------|
| 🧠 **7 Agent 协作** | Triage → Diagnostic → Planning → Remediation → Patrol → Report → Alert Correlation，基于 LangGraph StateGraph 确定性编排 |
| 🔧 **MCP 工具体系** | Go 实现的 8 个 MCP Server，提供 42+ 运维工具（指标查询、日志检索、配置管理、HDFS/YARN/Kafka 操作等） |
| 📚 **混合 RAG 检索** | Dense (Milvus) + Sparse (Elasticsearch BM25) + Graph-RAG (Neo4j) 三路检索 + RRF 融合 + Cross-Encoder 重排序 |
| 🛡️ **安全防护四层体系** | Prompt 注入防御 → RBAC 权限控制 → 敏感数据脱敏 → 幻觉检测 |
| 👥 **HITL 人机协作** | 5 级风控分级，高危操作自动拦截并走审批流程，支持 WebSocket 实时通知 |
| 📊 **全链路可观测** | OpenTelemetry + LangFuse + Prometheus + Grafana，从 LLM 调用到工具执行全程追踪 |
| 🔄 **多模型路由** | 按任务类型/复杂度/敏感度智能路由到最优模型（GPT-4o / Claude / DeepSeek），含语义缓存与 Token 预算控制 |
| ✅ **评估门禁** | RAGAS + DeepEval 离线评测，CI/CD 质量门禁自动拦截回归 |

---

## 🏗️ 系统架构

```
┌─────────────────────────────────────────────────────────────────┐
│                        FastAPI Web Layer                        │
│              /chat (SSE)  /approve  /health  /tools             │
├─────────────────────────────────────────────────────────────────┤
│                    LangGraph State Machine                       │
│  ┌─────────┐  ┌────────────┐  ┌──────────┐  ┌──────────────┐  │
│  │ Triage  │→│ Diagnostic │→│ Planning │→│ Remediation  │  │
│  │  Agent  │  │   Agent    │  │  Agent   │  │    Agent     │  │
│  └─────────┘  └────────────┘  └──────────┘  └──────────────┘  │
│  ┌─────────┐  ┌────────────┐  ┌──────────┐                    │
│  │ Patrol  │  │  Report    │  │  Alert   │                    │
│  │  Agent  │  │  Agent     │  │ Correlat.│                    │
│  └─────────┘  └────────────┘  └──────────┘                    │
├──────────┬──────────┬──────────┬───────────────────────────────┤
│ LLM Client│ RAG Engine│ MCP Client│ HITL Gate │ Security Layer │
├──────────┴──────────┴──────────┴───────────────────────────────┤
│                  MCP Server (Go / Fiber)                        │
│  Metrics│ Log │ HDFS │ YARN │ Kafka │ ES │ Config │ Ops       │
├─────────────────────────────────────────────────────────────────┤
│  PostgreSQL │ Redis │ Milvus │ Elasticsearch │ Neo4j │ Jaeger  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 🛠️ 技术栈

### Python（Agent 服务）

| 类别 | 技术 |
|------|------|
| Agent 框架 | LangGraph ^0.2, LangChain-Core ^0.3 |
| LLM 调用 | instructor ^1.4, litellm ^1.40, openai ^1.50 |
| RAG | pymilvus ^2.4, elasticsearch ^8.12, neo4j ^5.20, sentence-transformers ^3.0 |
| Web | FastAPI ^0.115, uvicorn, websockets ^12.0 |
| 可观测 | OpenTelemetry ^1.25, LangFuse ^2.40, prometheus-client ^0.21 |
| 基础 | Pydantic ^2.7, structlog ^24.2, httpx ^0.27, Redis ^5.0, SQLAlchemy ^2.0 |
| 评测 | RAGAS ^0.1, DeepEval ^0.21 |

### Go（MCP Server）

| 类别 | 技术 |
|------|------|
| Web 框架 | Fiber v2.52 |
| 日志 | zerolog v1.33 |
| 熔断 | sony/gobreaker v1.0 |
| 限流 | golang.org/x/time (rate limiter) |
| 配置 | viper v1.19 |

### 基础设施

| 组件 | 用途 |
|------|------|
| PostgreSQL 16 | 业务数据 + LangGraph Checkpoint + 审计日志 |
| Redis 7 | 缓存 + 消息队列 + HITL 审批状态 |
| Milvus 2.4 | 向量数据库（Dense Retrieval） |
| Elasticsearch 8.14 | 日志检索 + BM25 稀疏检索 |
| Neo4j 5 | 知识图谱（Graph-RAG） |
| Jaeger 1.57 | 分布式追踪（OTLP） |
| LangFuse | LLM 追踪与评估 |
| Prometheus + Grafana | 指标采集与可视化 |

---

## 📁 项目结构

```
ai-dataplatform-ops/
├── python/                          # Python Agent 服务
│   ├── pyproject.toml               # Poetry 依赖管理
│   ├── src/aiops/
│   │   ├── agent/                   # Agent 编排层
│   │   │   ├── graph.py             #   LangGraph 状态图构建
│   │   │   ├── state.py             #   AgentState 共享状态定义
│   │   │   ├── base.py              #   Agent 节点基类
│   │   │   ├── router.py            #   条件路由函数
│   │   │   ├── compressor.py        #   上下文压缩器
│   │   │   └── nodes/               #   7 个 Agent 节点实现
│   │   │       ├── triage.py        #     意图识别 & 智能分流
│   │   │       ├── diagnostic.py    #     多维诊断 & 根因分析
│   │   │       ├── planning.py      #     修复方案规划
│   │   │       ├── remediation.py   #     方案执行
│   │   │       ├── patrol.py        #     定时巡检
│   │   │       ├── report.py        #     报告生成
│   │   │       ├── alert_correlation.py  # 告警关联分析
│   │   │       ├── hitl_gate.py     #     人机协作审批门控
│   │   │       └── knowledge_sink.py#     知识沉淀
│   │   ├── llm/                     # LLM 客户端层
│   │   │   ├── client.py            #   统一 LLM 调用入口
│   │   │   ├── router.py            #   多模型路由
│   │   │   ├── providers.py         #   多供应商适配
│   │   │   ├── cache.py             #   语义缓存
│   │   │   ├── budget.py            #   Token 预算管理
│   │   │   ├── cost.py              #   成本追踪
│   │   │   └── schemas.py           #   结构化输出 Schema
│   │   ├── rag/                     # RAG 检索引擎
│   │   │   ├── retriever.py         #   混合检索编排
│   │   │   ├── dense.py             #   Milvus 向量检索
│   │   │   ├── sparse.py            #   ES BM25 检索
│   │   │   ├── graph_rag.py         #   Neo4j 图谱检索
│   │   │   ├── reranker.py          #   Cross-Encoder 重排序
│   │   │   ├── indexer.py           #   文档索引管线
│   │   │   └── processor.py         #   文档预处理
│   │   ├── mcp_client/              # MCP 客户端
│   │   │   ├── client.py            #   MCP 协议客户端
│   │   │   ├── registry.py          #   工具注册中心
│   │   │   └── discovery.py         #   工具自动发现
│   │   ├── hitl/                    # 人机协作系统
│   │   │   ├── gate.py              #   审批门控
│   │   │   ├── risk.py              #   风险评估（5 级）
│   │   │   ├── workflow.py          #   审批工作流
│   │   │   └── notification.py      #   WebSocket 通知
│   │   ├── security/                # 安全防护
│   │   │   ├── injection.py         #   Prompt 注入检测
│   │   │   ├── rbac.py              #   RBAC 权限控制
│   │   │   └── sensitive.py         #   敏感数据脱敏
│   │   ├── observability/           # 可观测性
│   │   │   ├── tracing.py           #   OpenTelemetry 追踪
│   │   │   ├── langfuse.py          #   LangFuse 集成
│   │   │   ├── metrics.py           #   Prometheus 指标
│   │   │   └── badcase.py           #   BadCase 追踪
│   │   ├── eval/                    # 评估体系
│   │   │   ├── runner.py            #   评测运行器
│   │   │   ├── ragas_eval.py        #   RAGAS 评测
│   │   │   ├── deepeval_eval.py     #   DeepEval 评测
│   │   │   ├── llm_judge.py         #   LLM-as-Judge
│   │   │   ├── gate_check.py        #   质量门禁
│   │   │   ├── ab_test.py           #   A/B 测试
│   │   │   └── continuous_eval.py   #   持续评测
│   │   ├── prompts/                 # Prompt 模板
│   │   └── web/                     # FastAPI Web 层
│   │       ├── app.py               #   应用入口
│   │       ├── routes/              #   路由（agent/approval/health/tools）
│   │       └── middleware/           #   中间件（metrics）
│   └── tests/                       # 测试
│       ├── unit/                    #   单元测试
│       ├── integration/             #   集成测试
│       └── security/                #   安全测试
│
├── go/                              # Go MCP Server
│   ├── go.mod
│   ├── cmd/mcp-server/main.go       # MCP Server 入口
│   ├── internal/
│   │   ├── config/                  #   配置管理
│   │   ├── protocol/                #   MCP 协议处理
│   │   │   ├── handler.go           #     请求处理器
│   │   │   ├── registry.go          #     工具注册
│   │   │   └── types.go             #     协议类型定义
│   │   ├── middleware/              #   8 层洋葱模型中间件
│   │   │   ├── chain.go             #     中间件链编排
│   │   │   ├── validation.go        #     参数校验
│   │   │   ├── risk.go              #     风控拦截
│   │   │   ├── ratelimit.go         #     限流
│   │   │   ├── circuitbreaker.go    #     熔断
│   │   │   ├── cache.go             #     缓存
│   │   │   ├── timeout.go           #     超时控制
│   │   │   ├── tracing.go           #     追踪
│   │   │   └── audit.go             #     审计日志
│   │   ├── tools/                   #   8 个 MCP 工具域
│   │   │   ├── metrics/             #     指标查询
│   │   │   ├── log/                 #     日志检索
│   │   │   ├── hdfs/                #     HDFS 操作
│   │   │   ├── yarn/                #     YARN 管理
│   │   │   ├── kafka/               #     Kafka 操作
│   │   │   ├── es/                  #     Elasticsearch 操作
│   │   │   ├── config/              #     配置管理
│   │   │   └── ops/                 #     运维操作
│   │   └── pkg/                     #   公共包（errors, logger）
│   └── configs/dev.yaml             # 开发环境配置
│
├── eval/                            # 评测数据集
│   ├── datasets/                    #   测试用例（triage/diagnosis/rag/security...）
│   ├── results/                     #   评测结果
│   └── gate_config.yaml             #   质量门禁配置
│
├── infra/                           # 基础设施
│   ├── docker/
│   │   ├── Dockerfile.agent         #   Agent 服务镜像
│   │   ├── Dockerfile.mcp           #   MCP Server 镜像
│   │   └── docker-compose.dev.yml   #   本地开发环境（10 个服务）
│   ├── k8s/                         #   Kubernetes 部署
│   │   ├── base/                    #     基础资源（Deployment/Service/HPA/PDB/NetworkPolicy）
│   │   └── overlays/                #     环境覆盖（dev/staging/production）
│   ├── monitoring/                  #   监控配置
│   │   ├── prometheus.yml           #     Prometheus 采集配置
│   │   ├── alert_rules.yml          #     告警规则
│   │   └── grafana/                 #     Grafana Dashboard
│   └── ci/                          #   CI/CD 脚本
│       ├── generate_manifest.py     #     K8s 清单生成
│       ├── rollback_code.sh         #     代码回滚
│       └── rollback_prompt.sh       #     Prompt 回滚
│
├── scripts/                         # 工具脚本
│   └── demo.py                      #   演示脚本
│
├── docs/                            # 文档
│   ├── production-design/           #   生产级设计文档（8 篇）
│   ├── Implementation-details/      #   实现细节文档（21 篇）
│   └── 大数据平台AI应用方向/          #   应用方向探索
│
├── Makefile                         # 顶层构建入口
├── .env.example                     # 环境变量模板
├── .pre-commit-config.yaml          # Pre-commit 钩子
└── .github/workflows/ci.yml         # CI/CD 流水线（6 阶段）
```

---

## 🚀 快速开始

### 前置要求

- Python 3.12+
- Go 1.22+
- Docker & Docker Compose
- Poetry（Python 包管理）

### 1. 克隆 & 安装依赖

```bash
git clone https://github.com/your-org/ai-dataplatform-ops.git
cd ai-dataplatform-ops

# 安装所有依赖
make install
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填写 LLM API Key 等配置
```

### 3. 启动本地开发环境

```bash
# 启动基础设施（PostgreSQL, Redis, Milvus, ES, Neo4j, Jaeger, LangFuse, Prometheus, Grafana）
make dev-up

# 启动完整开发环境（基础设施 + MCP Server + Agent 服务）
make dev
```

服务启动后可访问：

| 服务 | 地址 |
|------|------|
| Agent API | http://localhost:8080 |
| MCP Server | http://localhost:3000 |
| Jaeger UI | http://localhost:16686 |
| LangFuse | http://localhost:3001 |
| Prometheus | http://localhost:9090 |
| Grafana | http://localhost:3002 |
| Neo4j Browser | http://localhost:7474 |

### 4. 停止服务

```bash
make dev-down
```

---

## 🧪 测试

```bash
# 运行全部测试
make test

# 仅 Python 单元测试（含覆盖率）
make test-python

# 仅 Go 测试
make test-go

# 集成测试（需要 Docker Compose 环境）
make test-integration

# 安全测试（Prompt 注入等）
make test-security
```

---

## 📏 代码质量

```bash
# 全部 lint 检查
make lint

# 自动格式化
make format
```

- **Python**: Ruff (lint + format) + Mypy (类型检查)
- **Go**: golangci-lint
- **Pre-commit**: 提交前自动检查

---

## 📦 构建 & 部署

### Docker 镜像构建

```bash
# 构建所有镜像
make build

# 单独构建
make build-agent    # Agent 服务镜像
make build-mcp      # MCP Server 镜像
```

### Kubernetes 部署

项目使用 Kustomize 管理多环境部署：

```bash
# 开发环境
kubectl apply -k infra/k8s/overlays/dev/

# 预发布环境
kubectl apply -k infra/k8s/overlays/staging/

# 生产环境
kubectl apply -k infra/k8s/overlays/production/
```

### CI/CD 流水线

6 阶段自动化流水线（`.github/workflows/ci.yml`）：

1. **Build & Scan** — 镜像构建 + 安全扫描（2-5 min）
2. **Test** — 单元测试 + 集成测试 + 安全测试（5-15 min）
3. **AI Evaluation** — RAGAS/DeepEval 离线评测 + 质量门禁（15-30 min，仅 main 分支）
4. **Canary Deploy** — 金丝雀发布
5. **Observe** — 灰度观察
6. **Promote** — 全量发布

---

## 📊 评测体系

```bash
# 离线评测
make eval

# 质量门禁检查
make eval-gate
```

评测数据集位于 `eval/datasets/`，包含：
- Triage 分流准确率测试
- Diagnosis 诊断质量测试
- RAG 检索质量测试
- Remediation 修复方案测试
- Security 安全防护测试

---

## 🚢 推送到远端服务器

使用 rsync 将项目推送到远端服务器，自动排除不需要同步的文件：

```bash
rsync -avz \
    --exclude='.git/' \
    --exclude='.github/' \
    --exclude='__pycache__/' \
    --exclude='.workbuddy' \
    --exclude='venv' \
    --exclude='python/.venv' \
    /Users/ziwh666/GitHub/ai-dataplatform-ops \
    root@182.43.22.165:/data/github/
```

```bash
git fetch origin && git reset --hard origin/main
```

> 💡 **免密推送**：建议先配置 SSH 密钥认证，执行一次 `ssh-copy-id root@your-server-ip` 即可免密。

---

## 📚 文档导航

### 生产级设计文档（`docs/production-design/`）

| 编号 | 文档 | 内容 |
|------|------|------|
| 01 | 系统总体架构与设计理念 | 整体架构、技术选型、设计原则 |
| 02 | 数据采集与集成层设计 | 数据管道、Flink 作业、Kafka 集成 |
| 03 | 智能诊断 Agent 系统设计 | Agent 编排、状态机、诊断流程 |
| 04 | RAG 知识库与运维知识管理 | 混合检索、知识图谱、文档管线 |
| 05 | 可观测性与全链路追踪设计 | OTel、LangFuse、指标体系 |
| 06 | 安全防护与生产落地难点 | 四层安全、HITL、风控 |
| 07 | 部署架构与 CI/CD 流水线 | K8s 编排、金丝雀发布、回滚策略 |
| 08 | 工程化落地难点补充分析 | 成本控制、评估体系、渐进式上线 |

### 实现细节文档（`docs/Implementation-details/`）

共 21 篇，覆盖从工程化基础到部署运维的完整实现细节，包括：Agent 核心框架、各 Agent 节点实现、MCP Server/中间件/客户端、RAG 引擎、HITL 系统、安全防护、可观测性、评估体系等。

---

## 📄 License

Private — Internal Use Only