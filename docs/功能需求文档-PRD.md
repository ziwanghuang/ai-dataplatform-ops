# AIOps 大数据平台智能运维系统 — 功能需求文档（PRD）

> **文档版本**: v1.0  
> **项目代号**: AI DataPlatform Ops Agent  
> **最后更新**: 2026-04-05  
> **目标读者**: 开发工程师、技术面试官、架构评审者

---

## 目录

1. [项目概述](#1-项目概述)
2. [系统总体架构](#2-系统总体架构)
3. [核心功能模块](#3-核心功能模块)
   - 3.1 [智能分诊系统（Triage）](#31-智能分诊系统triage)
   - 3.2 [诊断规划系统（Planning）](#32-诊断规划系统planning)
   - 3.3 [根因分析系统（Diagnostic）](#33-根因分析系统diagnostic)
   - 3.4 [修复执行系统（Remediation）](#34-修复执行系统remediation)
   - 3.5 [报告生成系统（Report）](#35-报告生成系统report)
   - 3.6 [知识沉淀系统（KnowledgeSink）](#36-知识沉淀系统knowledgesink)
   - 3.7 [快速查询路径（DirectTool）](#37-快速查询路径directtool)
   - 3.8 [告警关联分析（AlertCorrelation）](#38-告警关联分析alertcorrelation)
   - 3.9 [定时巡检系统（Patrol）](#39-定时巡检系统patrol)
4. [MCP 工具服务](#4-mcp-工具服务)
   - 4.1 [MCP Server（Go）](#41-mcp-servergo)
   - 4.2 [MCP Client（Python）](#42-mcp-clientpython)
   - 4.3 [工具注册中心](#43-工具注册中心)
5. [RAG 知识检索引擎](#5-rag-知识检索引擎)
   - 5.1 [混合检索编排](#51-混合检索编排)
   - 5.2 [Dense 向量检索](#52-dense-向量检索)
   - 5.3 [Sparse BM25 检索](#53-sparse-bm25-检索)
   - 5.4 [Graph-RAG 图谱检索](#54-graph-rag-图谱检索)
   - 5.5 [Cross-Encoder 重排序](#55-cross-encoder-重排序)
6. [LLM 客户端与多模型路由](#6-llm-客户端与多模型路由)
7. [人机协作系统（HITL）](#7-人机协作系统hitl)
8. [安全防护体系](#8-安全防护体系)
   - 8.1 [Prompt 注入防御](#81-prompt-注入防御)
   - 8.2 [RBAC 权限控制](#82-rbac-权限控制)
   - 8.3 [上下文压缩](#83-上下文压缩)
9. [Web API 层](#9-web-api-层)
10. [可观测性体系](#10-可观测性体系)
11. [评估与质量门禁](#11-评估与质量门禁)
12. [Agent 编排与状态管理](#12-agent-编排与状态管理)
13. [非功能需求](#13-非功能需求)
14. [术语表](#14-术语表)

---

## 1. 项目概述

### 1.1 背景

大数据平台（HDFS / YARN / Kafka / Elasticsearch 等）的运维面临如下挑战：

- **故障诊断耗时长**：值班人员平均 30-60 分钟定位根因
- **知识碎片化**：历史故障经验散落在 Wiki / 文档 / 人脑中，难以快速召回
- **告警风暴**：同一根因触发 5-10 条告警，逐条处理效率极低
- **操作风险高**：重启、扩缩容等高危操作缺乏系统化的审批机制

### 1.2 产品定位

本系统是基于 **LangGraph 状态机编排** + **MCP 工具协议** 的智能运维 Agent，提供：

1. **自动化故障诊断**：从告警/用户描述出发，自动采集数据 → 假设推理 → 根因定位
2. **修复方案生成与受控执行**：按风险等级分级执行，高危操作走人工审批
3. **知识闭环沉淀**：诊断经验自动入库，下次同类问题秒级召回

### 1.3 目标用户

| 角色 | 使用场景 |
|------|---------|
| 值班运维 | 收到告警 → 输入问题描述 → 获取诊断报告和修复建议 |
| 平台 SRE | 审批高风险操作 → 查看诊断详情 → 确认执行 |
| 平台管理员 | 查看系统指标 → 配置用户权限 → 管理知识库 |
| Alertmanager | 告警自动转发 → 系统自动诊断处理 |

### 1.4 技术栈

| 层次 | 技术 |
|------|------|
| Agent 编排 | Python 3.12+ / LangGraph ^0.2 / LangChain-Core ^0.3 |
| LLM 调用 | litellm ^1.40 / instructor ^1.4（结构化输出） |
| 工具服务 | Go 1.22+ / Fiber v2.52 / MCP JSON-RPC 2.0 协议 |
| RAG 引擎 | Milvus 2.4 / Elasticsearch 8.14 / Neo4j 5 / BGE-M3 Embedding |
| Web 层 | FastAPI ^0.115 / uvicorn |
| 存储 | PostgreSQL 16 / Redis 7 |
| 可观测 | OpenTelemetry / LangFuse / Prometheus + Grafana / Jaeger |

---

## 2. 系统总体架构

```
┌──────────────────────────────────────────────────────────────────┐
│                      FastAPI Web Layer (L5)                       │
│          POST /diagnose  POST /alert  GET /tools  GET /health     │
├──────────────────────────────────────────────────────────────────┤
│                  LangGraph State Machine (L3)                     │
│  ┌─────────┐  ┌────────────┐  ┌──────────┐  ┌──────────────┐   │
│  │ Triage  │→│ Planning   │→│Diagnostic│→│ Remediation  │   │
│  │  Agent  │  │   Agent    │  │  Agent   │  │    Agent     │   │
│  └─────────┘  └────────────┘  └──────────┘  └──────────────┘   │
│  ┌─────────┐  ┌────────────┐  ┌──────────┐  ┌──────────────┐   │
│  │ Direct  │  │  Report    │  │  Alert   │  │  Knowledge   │   │
│  │  Tool   │  │  Agent     │  │ Correlat.│  │    Sink      │   │
│  └─────────┘  └────────────┘  └──────────┘  └──────────────┘   │
├─────────┬──────────┬──────────┬──────────┬───────────────────────┤
│LLM Client│RAG Engine│MCP Client│ HITL Gate │  Security Layer (L4) │
├─────────┴──────────┴──────────┴──────────┴───────────────────────┤
│                   MCP Server (Go/Fiber) (L1-L2)                   │
│  Metrics │ Log │ HDFS │ YARN │ Kafka │ ES │ Config │ Ops         │
├──────────────────────────────────────────────────────────────────┤
│  PostgreSQL │  Redis  │  Milvus  │ Elasticsearch │ Neo4j │ Jaeger │
└──────────────────────────────────────────────────────────────────┘
```

### 流程总览

```
请求入口 → Triage（分诊）
   ├── 快速路径（~40%）→ DirectTool → END
   ├── 多告警 → AlertCorrelation → Planning → Diagnostic → ...
   └── 完整诊断 → Planning → Diagnostic ─┐
                                           │
       ┌── need_more_data（自环，≤5轮）────┘
       │
       Diagnostic → ┬→ HITL Gate → ┬→ Remediation → Report → KnowledgeSink → END
                     │              └→ Report → KnowledgeSink → END
                     └→ Report → KnowledgeSink → END
```

**9 个节点，12 条边**，支持条件分支和自环。

---

## 3. 核心功能模块

### 3.1 智能分诊系统（Triage）

#### 3.1.1 功能概述

每个请求的第一个处理节点——"急诊分诊台"。快速判断用户意图、问题复杂度和路由路径。

#### 3.1.2 功能需求

| 需求 ID | 需求描述 | 优先级 |
|---------|---------|--------|
| TRIAGE-001 | **双路径架构**：规则引擎（Path A）+ LLM 分诊（Path B）并存 | P0 |
| TRIAGE-002 | **规则引擎**：15+ 条正则规则覆盖 HDFS/YARN/Kafka/ES/通用查询，零 Token、<1ms | P0 |
| TRIAGE-003 | **LLM 分诊**：使用 DeepSeek-V3 结构化输出，~500 Token、800ms | P0 |
| TRIAGE-004 | **四维分诊输出**：intent（5 种意图）、complexity（3 级）、route（3 条路径）、urgency（4 级） | P0 |
| TRIAGE-005 | **后置修正**：多告警（≥3）强制走 alert_correlation；告警请求不走快速路径 | P0 |
| TRIAGE-006 | **降级兜底**：LLM 不可用时默认走 diagnosis 路径（宁可慢不可停） | P0 |
| TRIAGE-007 | **分诊方式追踪**：记录 `_triage_method`（rule_engine / llm / fallback）用于可观测 | P1 |

#### 3.1.3 分诊维度定义

**意图（Intent）**：

| 值 | 含义 | 典型查询 |
|----|------|---------|
| status_query | 状态查询 | "HDFS 容量多少" |
| health_check | 健康检查 | "Kafka 集群是否正常" |
| fault_diagnosis | 故障诊断 | "NameNode 为什么 OOM" |
| capacity_planning | 容量规划 | "HDFS 还能用多久" |
| alert_handling | 告警处理 | 来自 Alertmanager 的告警 |

**复杂度（Complexity）**：

| 值 | 含义 | 预估处理路径 |
|----|------|------------|
| simple | 单工具可答 | DirectTool 快速路径 |
| moderate | 2-3 工具联合 | 1-2 轮诊断 |
| complex | 多轮多工具 | 3-5 轮诊断 |

**路由（Route）**：

| 值 | 目标 | 占比 |
|----|------|-----|
| direct_tool | 快速路径 → END | ~40% |
| diagnosis | 完整诊断链路 | ~50% |
| alert_correlation | 多告警聚合 → 诊断 | ~10% |

**紧急度（Urgency）**：critical / high / medium / low

#### 3.1.4 规则引擎覆盖范围

共 15 条规则，按优先级排序，匹配到第一条即返回：

| 分类 | 规则数 | 示例模式 | 目标工具 |
|------|--------|---------|---------|
| HDFS | 5 | `hdfs.*容量`, `namenode.*状态`, `安全模式`, `datanode.*列表`, `块.*报告` | hdfs_cluster_overview / hdfs_namenode_status / ... |
| YARN | 3 | `yarn.*资源`, `队列.*状态`, `应用.*列表` | yarn_cluster_metrics / yarn_queue_status / ... |
| Kafka | 3 | `kafka.*延迟`, `kafka.*集群`, `topic.*列表` | kafka_consumer_lag / kafka_cluster_overview / ... |
| ES | 2 | `es.*健康`, `es.*节点` | es_cluster_health / es_node_stats |
| 通用 | 2 | `告警.*列表`, `日志.*搜索` | query_alerts / search_logs |

#### 3.1.5 性能要求

| 指标 | 目标 |
|------|------|
| 规则引擎延迟 | < 1ms |
| LLM 分诊延迟 | < 1.5s |
| 综合准确率 | ≥ 96%（规则 99% × 30% + LLM 94% × 70%） |

---

### 3.2 诊断规划系统（Planning）

#### 3.2.1 功能概述

接收 Triage 的分诊结果，生成诊断计划和候选根因假设，为 Diagnostic Agent 提供执行蓝图。

#### 3.2.2 功能需求

| 需求 ID | 需求描述 | 优先级 |
|---------|---------|--------|
| PLAN-001 | **假设生成**：基于用户问题 + 组件信息，生成 2-4 个候选根因假设 | P0 |
| PLAN-002 | **诊断计划**：为每个假设规划验证步骤（工具调用列表 + 参数 + 验证目的） | P0 |
| PLAN-003 | **轮次预估**：预估需要 1-5 轮数据采集 | P1 |
| PLAN-004 | **Pydantic 结构化输出**：PlanningOutput 强类型约束（至少 1 个假设、至少 1 个步骤） | P0 |
| PLAN-005 | **降级规划**：LLM 不可用时基于组件生成通用诊断计划 | P0 |

#### 3.2.3 输出数据结构

```python
PlanningOutput:
  hypotheses: list[Hypothesis]     # 候选假设（id, description, probability, verification_tools）
  diagnostic_plan: list[DiagnosticStep]  # 诊断步骤（step_number, description, tools, parameters, purpose）
  data_requirements: list[str]     # 需要采集的工具列表
  estimated_rounds: int            # 预估轮次（1-5）
```

---

### 3.3 根因分析系统（Diagnostic）

#### 3.3.1 功能概述

核心诊断引擎，采用"假设-验证循环"（Hypothesis-Verification Loop）进行根因分析。

#### 3.3.2 功能需求

| 需求 ID | 需求描述 | 优先级 |
|---------|---------|--------|
| DIAG-001 | **假设-验证循环**：按诊断计划并行调用工具，将结果送入 LLM 分析 | P0 |
| DIAG-002 | **并行工具调用**：asyncio.gather 并行执行，单轮最多 5 个工具 | P0 |
| DIAG-003 | **单工具超时隔离**：每个工具独立 15s 超时，一个失败不阻塞其他 | P0 |
| DIAG-004 | **自环机制**：confidence < 0.6 且有 data_requirements → 触发自环（最多 5 轮） | P0 |
| DIAG-005 | **五步诊断法**：LLM 必须按"症状确认→范围界定→时间关联→根因分析→置信度评估"推理 | P0 |
| DIAG-006 | **证据链绑定**：每条证据必须引用具体工具返回的数据，防止 LLM 幻觉 | P0 |
| DIAG-007 | **结构化诊断输出**：DiagnosticOutput 含 root_cause / confidence / severity / evidence / causality_chain / remediation_plan | P0 |
| DIAG-008 | **降级诊断**：LLM 不可用时返回 confidence=0.1 的低置信度结果 | P0 |

#### 3.3.3 自环安全阀

5 道安全阀，按优先级从高到低检查：

1. **error_count ≥ MAX** → 强制结束
2. **total_tokens > 14000** → 强制结束（留 1000 给 Report）
3. **collection_round ≥ max_rounds（5）** → 强制结束
4. **confidence < 0.6 + data_requirements 非空** → 自环
5. **有 high/critical 修复建议** → 转 HITL Gate

#### 3.3.4 工具调用规划逻辑

| 轮次 | 数据来源 | 说明 |
|------|---------|------|
| 第 1 轮 | state.task_plan | Planning Agent 规划的步骤 |
| 第 2+ 轮 | state.data_requirements | Diagnostic 上一轮判断还需要什么数据 |

---

### 3.4 修复执行系统（Remediation）

#### 3.4.1 功能概述

渐进式放权的修复执行系统（当前 Phase 2），按风险等级分级执行修复操作。

#### 3.4.2 功能需求

| 需求 ID | 需求描述 | 优先级 |
|---------|---------|--------|
| REM-001 | **渐进式放权**（3 阶段）：Phase 1 仅建议 → Phase 2 低风险自动执行 → Phase 3 带回滚自动执行 | P0 |
| REM-002 | **风险等级执行矩阵**：按 risk_level × environment 决定执行策略 | P0 |
| REM-003 | **通过 MCP 执行修复**：调用 Go MCP Server 的运维工具 | P0 |
| REM-004 | **单步失败不阻塞**：一个步骤执行失败，后续步骤继续执行 | P0 |
| REM-005 | **执行结果记录**：每步记录 execution_status / execution_result / execution_note | P1 |
| REM-006 | **高风险强制审批标记**：risk=high/critical 的步骤强制 requires_approval=True | P0 |
| REM-007 | **回滚方案校验**：无回滚方案的步骤标注"⚠️ 未指定回滚方案" | P1 |

#### 3.4.3 Phase 2 执行矩阵

| 风险等级 | DEV 环境 | PRODUCTION 环境 |
|----------|---------|----------------|
| none / low | ✅ 自动执行 | ✅ 自动执行 |
| medium | ✅ 自动执行 | ⏳ 审批后执行 |
| high | 📋 仅建议 | ⏳ 审批后执行 |
| critical | 📋 仅建议 | ⏳ 双人审批后执行 |

---

### 3.5 报告生成系统（Report）

#### 3.5.1 功能概述

汇总诊断结果，生成 Markdown 格式的运维诊断报告。

#### 3.5.2 功能需求

| 需求 ID | 需求描述 | 优先级 |
|---------|---------|--------|
| RPT-001 | **Markdown 模板生成**：零 Token，< 1ms 延迟 | P0 |
| RPT-002 | **6 段式报告结构**：概述 → 诊断结论 → 证据链 → 诊断过程 → 修复建议 → 资源消耗 | P0 |
| RPT-003 | **置信度标签**：🟢 高（≥0.8）/ 🟡 中等（≥0.6）/ 🔴 低（<0.6，建议人工复核） | P0 |
| RPT-004 | **严重度 Emoji**：🔴 critical / 🟠 high / 🟡 medium / 🟢 low / ℹ️ info | P1 |
| RPT-005 | **修复建议风险标注**：每条建议标注风险等级 Emoji + 审批要求 + 回滚方案 | P0 |
| RPT-006 | **知识沉淀条目构建**：生成 knowledge_entry 供 KnowledgeSink 消费 | P0 |
| RPT-007 | **资源消耗透明**：报告中包含 Token 消耗、预估成本、分诊方式 | P1 |

---

### 3.6 知识沉淀系统（KnowledgeSink）

#### 3.6.1 功能概述

将诊断结果写入向量库，形成"诊断→报告→入库→下次 RAG 可检索"的知识闭环。

#### 3.6.2 功能需求

| 需求 ID | 需求描述 | 优先级 |
|---------|---------|--------|
| KS-001 | **质量过滤**：仅沉淀 confidence > 0.5 的诊断结果 | P0 |
| KS-002 | **Markdown 格式沉淀**：使用自然语言格式（Embedding 效果更好） | P0 |
| KS-003 | **通过 IncrementalIndexer 双写**：Milvus 向量 + ES 全文索引 | P0 |
| KS-004 | **去重机制**：同一 session 的诊断结果不重复入库 | P1 |
| KS-005 | **失败不阻塞**：知识沉淀失败不影响主诊断流程（增值操作，非关键路径） | P0 |
| KS-006 | **沉淀内容包含**：根因、修复步骤、涉及组件、置信度、原始查询 | P0 |

---

### 3.7 快速查询路径（DirectTool）

#### 3.7.1 功能概述

处理 ~40% 的简单查询请求，直接调用工具返回结果，跳过 Planning/Diagnostic/Report 全部环节。

#### 3.7.2 功能需求

| 需求 ID | 需求描述 | 优先级 |
|---------|---------|--------|
| DT-001 | **由 Triage 规则引擎触发**：匹配后设置 `_direct_tool_name` 和 `_direct_tool_params` | P0 |
| DT-002 | **单次 MCP 工具调用**：调用指定工具获取数据 | P0 |
| DT-003 | **模板格式化**：零 Token，结果格式化为 Markdown 代码块 | P0 |
| DT-004 | **直接写入 final_report → END**：不经过 Report Agent | P0 |
| DT-005 | **MCP 未配置时 Mock 降级**：返回说明性文字 | P1 |

#### 3.7.3 性能指标

| 指标 | 快速路径 | 完整链路 |
|------|---------|---------|
| 延迟 | 2-3s | 12-15s |
| Token 消耗 | ~2K | ~15K |
| 成本 | 节省 70%+ | 基准 |

---

### 3.8 告警关联分析（AlertCorrelation）

#### 3.8.1 功能概述

多告警同时到达时，先关联分析因果关系，合并为一次统一诊断。

#### 3.8.2 功能需求

| 需求 ID | 需求描述 | 优先级 |
|---------|---------|--------|
| AC-001 | **组件推断**：从告警标签 / message 中提取涉及组件 | P0 |
| AC-002 | **按组件分组**：将多个告警按组件聚合 | P0 |
| AC-003 | **聚合描述生成**：生成统一的问题描述覆盖 user_query | P0 |
| AC-004 | **紧急度评估**：取所有告警中最高严重度 | P0 |
| AC-005 | **默认标记为 complex**：多告警场景默认复杂度=complex | P1 |

#### 3.8.3 组件推断规则

| 组件 | 关键词匹配 |
|------|-----------|
| hdfs | hdfs, namenode, datanode, dfs, block |
| yarn | yarn, resourcemanager, nodemanager, queue, application |
| kafka | kafka, broker, consumer, topic, partition, lag |
| es | elasticsearch, es_cluster, shard, index |
| zk | zookeeper, zk |

#### 3.8.4 核心价值

- 5 告警独立诊断：5 × 15K Token = 75K Token
- 关联后统一诊断：1 × 20K Token = 20K Token
- **节省 73% Token，诊断质量更高**

---

### 3.9 定时巡检系统（Patrol）

#### 3.9.1 功能概述

定时主动检查集群健康状态，发现异常趋势后触发诊断链路。

#### 3.9.2 功能需求

| 需求 ID | 需求描述 | 优先级 |
|---------|---------|--------|
| PAT-001 | **5 项巡检覆盖**：HDFS NN 健康、HDFS 块报告、YARN 资源、Kafka Lag、ES 健康 | P0 |
| PAT-002 | **异常触发诊断**：发现异常时修改 state 走诊断流程 | P0 |
| PAT-003 | **全部正常 → 健康报告**：生成"所有组件健康"的简报直接结束 | P0 |
| PAT-004 | **定时触发**：request_type="patrol"，由 APScheduler 定时触发（默认每 5 分钟） | P1 |
| PAT-005 | **工具调用失败标记为异常**：单个工具调用异常计入 anomalies | P0 |

#### 3.9.3 巡检清单

| 检查项 | 工具 | 组件 | 说明 |
|--------|------|------|------|
| hdfs_health | hdfs_namenode_status | hdfs | NameNode 健康检查 |
| hdfs_blocks | hdfs_block_report | hdfs | 块健康报告 |
| yarn_resources | yarn_cluster_metrics | yarn | YARN 资源检查 |
| kafka_lag | kafka_consumer_lag | kafka | 消费延迟检查 |
| es_health | es_cluster_health | es | ES 集群健康检查 |

---

## 4. MCP 工具服务

### 4.1 MCP Server（Go）

#### 4.1.1 功能概述

Go 实现的 MCP Server，通过 JSON-RPC 2.0 协议为 Python Agent 提供大数据平台运维工具。

#### 4.1.2 功能需求

| 需求 ID | 需求描述 | 优先级 |
|---------|---------|--------|
| MCP-001 | **JSON-RPC 2.0 协议**：标准 request/response/error 结构 | P0 |
| MCP-002 | **Fiber HTTP Server**：/mcp 路由接收工具调用请求 | P0 |
| MCP-003 | **8 个工具域**：HDFS / YARN / Kafka / ES / Config / Metrics / Log / Ops | P0 |
| MCP-004 | **5 级风险等级**：None / Low / Medium / High / Critical | P0 |
| MCP-005 | **Tool 接口标准化**：Name() / Description() / Schema() / RiskLevel() / Execute() | P0 |
| MCP-006 | **工具注册中心**：线程安全的 Registry（sync.RWMutex） | P0 |
| MCP-007 | **Graceful Shutdown**：信号处理 + 连接排空 | P1 |

#### 4.1.3 工具清单

| 域 | 工具数 | 示例工具 | 说明 |
|----|--------|---------|------|
| HDFS | 8 | hdfs_namenode_status, hdfs_cluster_overview | NameNode 状态、集群概览、块报告等 |
| YARN | 7 | yarn_cluster_metrics, yarn_queue_status | 集群资源、队列状态、应用列表等 |
| Kafka | 7 | kafka_consumer_lag, kafka_cluster_overview | 消费延迟、Topic 列表、ISR 检查等 |
| ES | 6 | es_cluster_health, es_node_stats | 集群健康、节点统计、分片分配等 |
| Config | - | 配置管理工具 | 配置查询与更新 |
| Metrics | - | query_metrics | Prometheus 指标查询 |
| Log | 1 | search_logs | ES + Lucene 语法日志搜索 |
| Ops | 6 | ops_restart_service, ops_decommission_node | 重启/扩缩容/退役（高危操作） |

#### 4.1.4 中间件链（8 层洋葱模型）

| 层序 | 中间件 | 功能 |
|------|--------|------|
| 1 | validation | 参数校验 |
| 2 | risk | 风控拦截（高风险工具附加检查） |
| 3 | ratelimit | 限流（golang.org/x/time） |
| 4 | circuitbreaker | 熔断（sony/gobreaker） |
| 5 | cache | 只读工具缓存 |
| 6 | timeout | 超时控制 |
| 7 | tracing | 追踪（OpenTelemetry） |
| 8 | audit | 审计日志 |

---

### 4.2 MCP Client（Python）

#### 4.2.1 功能需求

| 需求 ID | 需求描述 | 优先级 |
|---------|---------|--------|
| MCPC-001 | **JSON-RPC 2.0 通信**：httpx.AsyncClient 连接池 | P0 |
| MCPC-002 | **双层缓存**：L1 内存 dict（200 容量 LRU）+ L2 Redis（30s TTL） | P0 |
| MCPC-003 | **可缓存白名单**：仅只读查询类工具缓存（9 个） | P0 |
| MCPC-004 | **动态超时**：按风险等级映射（none:15s → critical:120s） | P0 |
| MCPC-005 | **并行批量调用**：batch_call_tools + Semaphore 限并发（默认 5） | P0 |
| MCPC-006 | **结果截断**：> 5000 字符自动截断，防 state 膨胀 | P1 |
| MCPC-007 | **健康检查**：定期检查后端健康状态 | P1 |

---

### 4.3 工具注册中心

#### 4.3.1 功能需求

| 需求 ID | 需求描述 | 优先级 |
|---------|---------|--------|
| REG-001 | **42 个内部 MCP 工具元数据**：name / component / risk / source / description | P0 |
| REG-002 | **按组件索引**：list_by_component() | P0 |
| REG-003 | **按风险过滤**：list_by_risk(max_risk) | P0 |
| REG-004 | **RBAC 动态过滤**：filter_for_user(user_role, components) | P0 |
| REG-005 | **Prompt 注入格式化**：format_for_prompt() 生成 LLM 可读的工具列表 | P0 |
| REG-006 | **动态发现**：update_from_discovery() 支持运行时注册新工具 | P2 |

#### 4.3.2 RBAC 过滤规则

| 用户角色 | 最大可用风险等级 |
|---------|----------------|
| viewer | none |
| operator | none |
| engineer | medium |
| admin | high |
| super | critical |

---

## 5. RAG 知识检索引擎

### 5.1 混合检索编排

#### 5.1.1 功能概述

6 步流水线：QueryRewriter → Dense ∥ Sparse → RRF 融合 → MetadataFilter → Dedup → Rerank

#### 5.1.2 功能需求

| 需求 ID | 需求描述 | 优先级 |
|---------|---------|--------|
| RAG-001 | **并行双路检索**：asyncio.gather + return_exceptions=True 容错 | P0 |
| RAG-002 | **RRF 融合算法**：Reciprocal Rank Fusion，k=60（不依赖原始分数量纲） | P0 |
| RAG-003 | **元数据过滤**：按 components / doc_type / min_date 过滤 | P1 |
| RAG-004 | **content_hash 去重**：同一文档被 Dense 和 Sparse 同时召回时去重 | P0 |
| RAG-005 | **单路降级**：任一路失败时自动降级为单路检索（效果差 ~15% 但可用） | P0 |
| RAG-006 | **Reranker 降级**：超时时降级为 RRF 分数排序 | P0 |

#### 5.1.3 性能目标

| 指标 | 目标 |
|------|------|
| 总延迟 | < 500ms |
| 单路 Recall@20 | > 81% |
| 混合 Recall@20 | > 96%（+15pp） |
| Precision@5 | > 70% |

---

### 5.2 Dense 向量检索

#### 5.2.1 功能需求

| 需求 ID | 需求描述 | 优先级 |
|---------|---------|--------|
| DENSE-001 | **Milvus 2.4 向量数据库**：BGE-M3 Embedding（1024 维）+ IVF_FLAT + COSINE | P0 |
| DENSE-002 | **延迟连接**：首次 search 时才建立 Milvus 连接 | P0 |
| DENSE-003 | **Mock 降级**：Milvus 不可用时用 8 条内置文档的关键词匹配 | P0 |
| DENSE-004 | **nprobe=16**：recall ~98%，延迟 ~50ms | P1 |

### 5.3 Sparse BM25 检索

#### 5.3.1 功能需求

| 需求 ID | 需求描述 | 优先级 |
|---------|---------|--------|
| SPARSE-001 | **AsyncElasticsearch**：ik_smart 中文分词 + multi_match（content^1, source^0.5） | P0 |
| SPARSE-002 | **延迟连接**：首次 search 时才建立 ES 连接 | P0 |
| SPARSE-003 | **Mock 降级**：ES 不可用时用精确子串匹配 | P0 |

### 5.4 Graph-RAG 图谱检索

#### 5.4.1 功能需求

| 需求 ID | 需求描述 | 优先级 |
|---------|---------|--------|
| GRAPH-001 | **Neo4j 图谱检索**：实体匹配 → 2-hop 子图遍历 → 因果链查找 → SOP 关联 | P1 |
| GRAPH-002 | **图数据模型**：Component / Incident / SOP / Person 节点，DEPENDS_ON / CAUSED_BY / RESOLVED_BY / SIMILAR_TO 边 | P1 |
| GRAPH-003 | **组件拓扑查询**：get_component_topology(component, depth) | P1 |
| GRAPH-004 | **因果路径查询**：find_causal_path(source, target) | P1 |
| GRAPH-005 | **Mock 知识**：内置 9 组件拓扑 + 5 条因果链 + 3 个历史案例 | P0 |

#### 5.4.2 Mock 内置因果链

| 触发场景 | 因果链 | 涉及组件 |
|---------|--------|---------|
| NameNode OOM | NN 堆不足 → Full GC → RPC 阻塞 → DN 心跳超时 → Block 汇报延迟 | hdfs-namenode, hdfs-datanode |
| Kafka Lag | Consumer 变慢 → Lag 堆积 → 数据过期 → 丢失风险 | kafka-consumer, kafka-broker |
| ES Red | 主分片丢失 → 副本无法提升 → 索引不可写入 → 日志采集阻塞 | es-master, es-data |
| YARN 队列满 | 资源耗尽 → 任务排队 → SLA 超时 → 业务投诉 | yarn-rm, yarn-nm |
| ZK Session 超时 | Session 超时 → Leader 重选 → 组件闪断 → 服务中断 | zk, hdfs-nn, kafka |

### 5.5 Cross-Encoder 重排序

#### 5.5.1 功能需求

| 需求 ID | 需求描述 | 优先级 |
|---------|---------|--------|
| RERANK-001 | **对 RRF top-20 精排到 top-5**：生产版使用 BGE-Reranker-v2-m3 | P1 |
| RERANK-002 | **Mock 实现**：当前按 RRF 分数排序截断 | P0 |
| RERANK-003 | **延迟目标**：20 候选重排 ~200ms（GPU）/ ~500ms（CPU） | P2 |

---

## 6. LLM 客户端与多模型路由

### 6.1 功能需求

| 需求 ID | 需求描述 | 优先级 |
|---------|---------|--------|
| LLM-001 | **统一调用入口**：LLMClient 封装路由 / 重试 / 超时 / 容灾 | P0 |
| LLM-002 | **文本响应**：chat() → litellm.acompletion → LLMResponse | P0 |
| LLM-003 | **结构化输出**：chat_structured() → instructor.from_litellm → Pydantic Model | P0 |
| LLM-004 | **三维路由**：(task_type × complexity × sensitivity) → ModelConfig | P0 |
| LLM-005 | **强制供应商覆盖**：force_provider 支持降级切换 | P1 |

### 6.2 多模型路由表

| 任务类型 | 默认模型 | 高敏感度模型 | 选型理由 |
|---------|---------|------------|---------|
| Triage | DeepSeek-V3 | - | 低成本（¥1/M token），分诊准确率 94% |
| Diagnostic | GPT-4o | Qwen2.5-72B（本地） | 强推理能力 |
| Planning (simple) | GPT-4o-mini | Qwen2.5-72B（本地） | 轻量且足够 |
| Planning (complex) | GPT-4o | Qwen2.5-72B（本地） | 复杂规划需要强推理 |
| Report | GPT-4o-mini | - | 轻量，报告主要靠模板 |
| Alert Correlation | GPT-4o | - | 需要理解多告警关系 |
| Remediation | GPT-4o | - | 修复方案需要精确 |
| Patrol | GPT-4o-mini | - | 巡检结论简单 |

---

## 7. 人机协作系统（HITL）

### 7.1 功能需求

| 需求 ID | 需求描述 | 优先级 |
|---------|---------|--------|
| HITL-001 | **双模式**：DEV 自动批准 / PRODUCTION 走真实审批 | P0 |
| HITL-002 | **审批类型**：SINGLE（高风险）/ DUAL（critical，需两个不同审批人）/ AUTO（DEV） | P0 |
| HITL-003 | **审批状态机**：PENDING → APPROVED / REJECTED / TIMEOUT / AUTO_APPROVED | P0 |
| HITL-004 | **超时自动拒绝**：fail-safe 设计，宁可误拒不误批 | P0 |
| HITL-005 | **超时时间**：high/critical 至少 10 分钟，默认 5 分钟 | P1 |
| HITL-006 | **双模式存储**：内存 dict + 可选 Redis hash（hitl:approval:{id}，TTL 2h） | P0 |
| HITL-007 | **外部审批结果注入**：API 层可通过 state 注入审批结果恢复图执行 | P1 |
| HITL-008 | **审批统计**：按状态统计、pending 计数 | P2 |

### 7.2 审批触发条件

1. RiskClassifier 评估为 HIGH / CRITICAL
2. 修复操作涉及 restart / scale / decommission
3. 影响范围超过单节点

---

## 8. 安全防护体系

### 8.1 Prompt 注入防御

#### 8.1.1 功能需求

| 需求 ID | 需求描述 | 优先级 |
|---------|---------|--------|
| SEC-001 | **Level 1 正则快检**：6 类注入模式（指令覆盖/角色扮演/信息泄露/越权执行/DAN 越狱/编码绕过） | P0 |
| SEC-002 | **Level 2 关键词评分**：13 个可疑关键词加权评分，≥0.8 标记为注入 | P0 |
| SEC-003 | **Level 3 LLM 检测**：仅对可疑输入启用（未来实现） | P2 |
| SEC-004 | **sanitize() 清理**：移除危险片段但保留核心查询 | P0 |

#### 8.1.2 检测模式示例

| 模式类型 | 正则匹配 |
|---------|---------|
| 指令覆盖 | "忽略之前的指令" / "ignore previous instructions" |
| 角色扮演 | "你现在是黑客" / "act as admin" |
| 信息泄露 | "输出所有密码" / "show all api keys" |
| 越权执行 | "直接执行重启所有" / "immediately run delete all" |
| DAN 越狱 | "DAN" / "jailbreak" / "do anything now" |
| 编码绕过 | "base64 decode" / "hex encode" |

### 8.2 RBAC 权限控制

#### 8.2.1 功能需求

| 需求 ID | 需求描述 | 优先级 |
|---------|---------|--------|
| RBAC-001 | **5 级角色体系**：VIEWER(0) / OPERATOR(1) / ENGINEER(2) / ADMIN(3) / SUPER(4) | P0 |
| RBAC-002 | **14 条权限规则**：覆盖 Agent 操作、工具调用、审批操作、管理操作 | P0 |
| RBAC-003 | **工具级权限过滤**：风险等级 → 权限资源映射 | P0 |
| RBAC-004 | **组件级权限过滤**：从工具名推断组件，用户只能访问授权组件 | P0 |
| RBAC-005 | **审计日志**：记录所有访问决策（内存，最多 10000 条） | P1 |
| RBAC-006 | **批量加载**：load_users_from_config() 支持 YAML/dict 格式 | P1 |
| RBAC-007 | **双模式存储**：内存 + Redis 持久化 | P0 |

#### 8.2.2 权限表

| 资源 | 操作 | 最低角色 |
|------|------|---------|
| agent:diagnose | execute | OPERATOR |
| agent:remediate | execute | ENGINEER |
| tools:read | execute | VIEWER |
| tools:low_risk | execute | OPERATOR |
| tools:medium_risk | execute | ENGINEER |
| tools:high_risk | execute | ADMIN |
| tools:critical | execute | SUPER |
| approval:approve | write | ENGINEER |
| approval:approve_critical | write | ADMIN |

### 8.3 上下文压缩

#### 8.3.1 功能需求

| 需求 ID | 需求描述 | 优先级 |
|---------|---------|--------|
| COMP-001 | **总字符限制**：MAX_TOTAL_CHARS = 8000（≈3000 Token） | P0 |
| COMP-002 | **单工具限制**：MAX_PER_TOOL_CHARS = 2000 | P0 |
| COMP-003 | **RAG 限制**：MAX_RAG_CHARS = 1500，最多 3 条 | P0 |
| COMP-004 | **相似案例限制**：MAX_CASES_CHARS = 1000，最多 2 条 | P1 |
| COMP-005 | **分级截断策略**：空白行 → 单工具截断 → RAG 截断 → 硬截断 | P0 |

---

## 9. Web API 层

### 9.1 功能需求

| 需求 ID | 需求描述 | 优先级 |
|---------|---------|--------|
| API-001 | **POST /api/v1/diagnose**：提交诊断请求，同步返回诊断报告 | P0 |
| API-002 | **POST /api/v1/alert**：接收 Alertmanager 告警，触发诊断 | P0 |
| API-003 | **GET /api/v1/tools**：列出当前角色可用的工具（RBAC 过滤） | P0 |
| API-004 | **GET /api/v1/health**：健康检查（返回 status / version / env） | P0 |
| API-005 | **POST /api/v1/agent/diagnose**：增强版诊断端点（带 session_id / thread_id / checkpoint） | P1 |
| API-006 | **POST /api/v1/agent/alert**：增强版告警端点（告警不返回 500，降级为 accepted） | P0 |
| API-007 | **GET /api/v1/agent/sessions/{id}**：查询诊断会话状态 | P1 |
| API-008 | **GET /api/v1/agent/sessions/{id}/report**：获取诊断报告 | P1 |

### 9.2 请求/响应模型

**DiagnoseRequest**:

```json
{
  "query": "HDFS NameNode 为什么 OOM？",
  "user_id": "zhangsan",
  "cluster_id": "default"
}
```

**DiagnoseResponse**:

```json
{
  "request_id": "REQ-20260405-abc123",
  "report": "# 🔍 AIOps 诊断报告\n...",
  "diagnosis": {
    "root_cause": "NameNode heap 不足...",
    "confidence": 0.85,
    "severity": "high",
    "evidence": ["..."],
    "causality_chain": "A → B → C"
  },
  "remediation_plan": [...],
  "total_tokens": 12500,
  "total_cost_usd": 0.0156
}
```

**AlertRequest**:

```json
{
  "alerts": [
    {"message": "NameNode heap > 90%", "severity": "critical", "component": "hdfs"},
    {"message": "DataNode heartbeat timeout", "severity": "high", "component": "hdfs"}
  ],
  "cluster_id": "default"
}
```

---

## 10. 可观测性体系

### 10.1 功能需求

| 需求 ID | 需求描述 | 优先级 |
|---------|---------|--------|
| OBS-001 | **Prometheus 指标**：20+ 自定义指标覆盖 Agent / RAG / LLM / MCP / Triage | P0 |
| OBS-002 | **Agent 层指标**：AGENT_INVOCATIONS / AGENT_DURATION / AGENT_TOKENS | P0 |
| OBS-003 | **RAG 层指标**：RAG_RETRIEVAL_DURATION / RAG_RESULTS_COUNT / CACHE_HIT/MISS | P0 |
| OBS-004 | **LLM 层指标**：LLM_CALL_DURATION / LLM_CALL_TOTAL / LLM_TOKEN_USAGE | P0 |
| OBS-005 | **MCP 层指标**：MCP_TOOL_CALL_DURATION / MCP_TOOL_CALL_TOTAL | P0 |
| OBS-006 | **Triage 专用指标**：TRIAGE_ROUTE_TOTAL / TRIAGE_RULE_ENGINE_HIT | P1 |
| OBS-007 | **OpenTelemetry 追踪**：全链路 Trace（每个 Agent 节点 = 一个 Span） | P1 |
| OBS-008 | **LangFuse 集成**：LLM 调用追踪与评估 | P1 |
| OBS-009 | **Grafana Dashboard**：预置监控面板 | P2 |
| OBS-010 | **告警规则**：5 组告警（Agent / RAG / LLM / MCP / SLA） | P2 |

### 10.2 关键指标定义

| 指标名 | 类型 | 标签 | 说明 |
|--------|------|------|------|
| aiops_agent_invocations_total | Counter | agent, status | Agent 节点调用次数 |
| aiops_agent_duration_seconds | Histogram | agent | Agent 节点执行耗时 |
| aiops_agent_tokens_total | Counter | agent, model | Token 消耗 |
| aiops_rag_retrieval_duration_seconds | Histogram | retriever_type | RAG 检索耗时 |
| aiops_llm_call_duration_seconds | Histogram | model, task_type | LLM 调用耗时 |
| aiops_mcp_tool_call_total | Counter | tool_name, status | MCP 工具调用次数 |
| aiops_triage_route_total | Counter | method, route | 分诊路由决策分布 |

---

## 11. 评估与质量门禁

### 11.1 功能需求

| 需求 ID | 需求描述 | 优先级 |
|---------|---------|--------|
| EVAL-001 | **评估数据集**：v2.1 共 92 条（triage 40 + diagnosis 12 + rag 12 + remediation 12 + security 16） | P0 |
| EVAL-002 | **7 项质量门禁**：CI/CD Pipeline 中阻断不达标的发布 | P0 |
| EVAL-003 | **LLM-as-Judge**：使用 LLM 评价诊断质量 | P1 |
| EVAL-004 | **A/B 测试框架**：对比不同模型/Prompt 的效果 | P2 |
| EVAL-005 | **BadCase 自动发现**：识别低分用例 | P2 |
| EVAL-006 | **持续评估流水线**：线上持续跑评估 | P2 |

### 11.2 质量门禁阈值

| 指标 | 阈值 | 是否阻断 | 说明 |
|------|------|---------|------|
| triage_accuracy | ≥ 0.85 | ✅ 阻断 | 分诊意图准确率 |
| route_accuracy | ≥ 0.85 | ❌ 告警 | 分诊路由准确率 |
| diagnosis_accuracy | ≥ 0.70 | ✅ 阻断 | 诊断根因准确率 |
| remediation_safety | ≥ 1.00 | ✅ 阻断 | 修复安全率（零容忍） |
| security_pass_rate | ≥ 1.00 | ✅ 阻断 | 安全合规率（零容忍） |
| has_evidence | ≥ 0.80 | ✅ 阻断 | 诊断证据引用率 |
| component_recall | ≥ 0.75 | ❌ 告警 | 组件识别召回率 |

---

## 12. Agent 编排与状态管理

### 12.1 AgentState 定义

共享状态分 9 个区域，18+ 个字段：

| 分区 | 字段 | 写入者 | 说明 |
|------|------|--------|------|
| **输入区** | request_id, request_type, user_query, user_id, cluster_id, alerts | API 入口 | 只读 |
| **分诊区** | intent, complexity, route, urgency, target_components | Triage | 分诊结果 |
| **规划区** | task_plan, data_requirements, hypotheses | Planning | 诊断计划 |
| **数据采集区** | tool_calls, collected_data, collection_round, max_collection_rounds | Diagnostic | 工具调用记录 |
| **RAG 区** | rag_context, similar_cases | Planning/Diagnostic | 检索结果 |
| **诊断结果区** | diagnosis, remediation_plan | Diagnostic | 诊断结论 |
| **HITL 区** | hitl_required, hitl_status, hitl_comment | HITL Gate | 审批状态 |
| **输出区** | final_report, knowledge_entry | Report | 最终输出 |
| **元信息区** | messages, current_agent, error_count, start_time, total_tokens, total_cost_usd | 框架 | 运行时元数据 |

### 12.2 条件路由函数

| 路由函数 | 来源节点 | 可能路由 | 判断依据 |
|---------|---------|---------|---------|
| route_from_triage | Triage | direct_tool / planning / alert_correlation | state.route |
| route_from_diagnostic | Diagnostic | need_more_data / hitl_gate / report | confidence / rounds / risk |
| route_from_hitl | HITL Gate | remediation / report | hitl_status |

### 12.3 状态工厂

`create_initial_state()` 提供所有字段的合理默认值，避免下游 Agent 写防御代码。

### 12.4 Mock 图

LangGraph 未安装时降级为 5 节点线性执行（Triage → Planning → Diagnostic → Report → KnowledgeSink）。

---

## 13. 非功能需求

### 13.1 性能

| 场景 | 延迟目标 | Token 预算 |
|------|---------|-----------|
| 快速查询（direct_tool） | < 3s | < 2K |
| 单轮诊断 | < 10s | < 8K |
| 完整诊断（3-5 轮） | < 30s | < 15K |
| 巡检 | < 15s | < 5K |

### 13.2 可用性

| 需求 | 目标 |
|------|------|
| LLM API 不可用时 | 每个 Agent 节点有降级兜底 |
| Milvus 不可用时 | Dense 降级为 Mock 关键词匹配 |
| ES 不可用时 | Sparse 降级为 Mock 子串匹配 |
| Neo4j 不可用时 | GraphRAG 降级为 Mock 拓扑知识 |
| Redis 不可用时 | 缓存/审批/RBAC 降级为内存模式 |
| MCP Server 不可用时 | Agent 使用 Mock 图线性执行 |

### 13.3 安全性

| 需求 | 措施 |
|------|------|
| Prompt 注入 | 正则 + 关键词双重检测 |
| 权限控制 | 5 级 RBAC + 工具级 + 组件级 |
| 高危操作 | HITL 审批（超时自动拒绝） |
| 数据脱敏 | 敏感数据脱敏（设计中） |
| 幻觉防御 | evidence 必须引用工具返回数据 + model_validator |

### 13.4 可部署性

| 需求 | 方案 |
|------|------|
| 容器化 | Dockerfile.agent（3 阶段构建 + tini） + Dockerfile.mcp（distroless） |
| K8s 部署 | Kustomize base + 3 overlays（dev/staging/production） |
| CI/CD | GitHub Actions 6 阶段 Pipeline |
| 本地开发 | Docker Compose 10+ 服务 |

---

## 14. 术语表

| 术语 | 说明 |
|------|------|
| MCP | Model Context Protocol，AI Agent 与工具之间的通信协议 |
| LangGraph | LangChain 团队的状态机编排框架 |
| RRF | Reciprocal Rank Fusion，混合检索结果融合算法 |
| HITL | Human-in-the-Loop，人机协作 |
| Dense Retrieval | 基于向量相似度的语义检索 |
| Sparse Retrieval | 基于 BM25 的精确关键词检索 |
| Graph-RAG | 基于知识图谱的检索增强生成 |
| Cross-Encoder | 对 (query, doc) 对直接打分的精排模型 |
| instructor | 基于 Pydantic 的 LLM 结构化输出库 |
| litellm | 统一 LLM API 调用库（支持 100+ 供应商） |
| AgentState | LangGraph 共享状态 TypedDict |
| fail-safe | 安全默认行为（如超时自动拒绝） |

---

> **文档结束**。本文档基于实际代码实现编写，所有功能需求均可追溯到对应源文件。
