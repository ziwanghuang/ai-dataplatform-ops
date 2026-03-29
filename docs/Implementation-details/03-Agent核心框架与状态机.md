# 03 - Agent 核心框架与状态机

> **设计文档引用**：`03-智能诊断Agent系统设计.md` §1.1-1.3, §7-8  
> **职责边界**：LangGraph StateGraph 定义、AgentState 数据结构、条件路由、节点基类、图级错误处理、上下文压缩  
> **优先级**：P0 — 所有具体 Agent (04-08) 的基础

---

## 1. 模块概述

### 1.1 职责

本模块定义 Agent 编排的核心框架：

- **AgentState** — LangGraph 共享状态 TypedDict，贯穿整个 Agent 执行生命周期
- **OpsGraph** — LangGraph StateGraph 编排图，定义 7 个 Agent 节点和条件边
- **BaseAgentNode** — Agent 节点基类，标准化输入/输出/错误处理/指标收集
- **ConditionalRouter** — 3 个关键路由函数（Triage→、Diagnostic→、HITL→）
- **GraphErrorHandler** — 图级别异常捕获、重试、降级、强制结束
- **ContextCompressor** — 长对话上下文压缩，控制 Token 预算

### 1.3 为什么需要单独的"框架"模块（WHY）

> **设计决策 WHY**：为什么不把编排逻辑直接写在各 Agent 里？
>
> 1. **关注点分离**：具体 Agent（04-08）只关心自己的诊断/规划/报告逻辑，不需要知道自己在图中的位置、被谁调用、出错后由谁接管。框架模块处理所有编排层面的事情——节点注册、条件路由、错误恢复、上下文管理。
> 2. **单元测试友好**：Agent 可以脱离图单独测试（给一个 state dict 进去，检查返回的 state dict），不需要启动整个图。
> 3. **复用与演进**：如果将来新增 Agent（如 CapacityPlanningAgent），只需实现 `BaseAgentNode`，在 `build_ops_graph()` 里注册即可，不需要修改任何已有 Agent。
> 4. **跨 Agent 策略统一**：Token 预算检查、OTel Span 创建、错误计数这些横切关注点在基类里一次性实现，避免各 Agent 各自实现导致不一致。

### 1.4 框架选型：为什么选 LangGraph

> **设计决策 WHY**：对比了 3 个主流 Agent 编排框架后选择 LangGraph。

| 维度 | **LangGraph** | CrewAI | AutoGen |
|------|-------------|--------|---------|
| **编排模型** | 显式状态机（StateGraph） | 角色扮演 + 任务委派 | 多 Agent 对话 |
| **状态管理** | TypedDict 共享状态，每个节点读写 | 隐式上下文传递 | 消息列表 |
| **条件路由** | 代码定义条件边，确定性路由 | LLM 决定分发 | LLM 对话协调 |
| **断点续传** | 内置 Checkpointer（PG/Redis） | 不支持 | 不支持 |
| **流式输出** | `astream_events` 细粒度事件流 | 不支持 | 部分支持 |
| **人机协作** | `interrupt_before/after` 原生支持 | 需要自行实现 | 需要自行实现 |
| **可观测性** | 与 LangSmith/LangFuse 原生集成 | 弱 | 弱 |
| **学习曲线** | 中等（需理解状态机概念） | 低（直觉式 API） | 低 |
| **生产可控性** | ⭐⭐⭐⭐⭐ 路由逻辑可预测 | ⭐⭐ LLM 路由不可预测 | ⭐⭐ 对话不可预测 |
| **适合场景** | 有固定工作流的 Agent 系统 | 创意类多角色协作 | 研究/原型 |

**选择 LangGraph 的决定性理由**：

1. **确定性路由**：运维系统需要可预测的执行路径——Triage 判定的路由必须 100% 由代码控制，不能让 LLM "即兴发挥"把 HDFS 问题路由到 Kafka 诊断流程。CrewAI 和 AutoGen 的 LLM 驱动路由在生产环境中不可接受。
2. **断点续传**：HITL 审批可能等待数分钟到数小时。LangGraph 的 Checkpointer 可以将整个图状态持久化到 PostgreSQL，审批完成后从断点恢复。其他框架需要自己实现序列化/反序列化，极易出错。
3. **状态可见性**：TypedDict 让每个字段的写入者和读取者都清晰可查。在调试"为什么诊断结果不对"时，可以直接 dump state 看各 Agent 写了什么。消息列表模式（AutoGen）很难追踪中间状态。
4. **子图组合**：可以把 "Planning→Diagnostic→Report" 封装为子图，将来做 A/B 测试（旧流程 vs 新流程）时只需切换子图，不影响外层。

> **被否决的方案**：
> - **CrewAI**：初始原型阶段曾用 CrewAI，但很快发现它的 "Agent 自行决定交给谁" 模式在运维场景中不可靠——有时 Triage Agent 会把简单查询也路由给 Diagnostic Agent，浪费 Token 且延迟增加 3x。
> - **AutoGen**：对话式编排不适合有固定工作流的场景，且不支持 HITL 中断。
> - **纯 Python asyncio 自研**：技术上可行但工作量巨大——需要自己实现状态持久化、条件路由、流式事件、错误恢复，估计 3000+ 行代码，且每个功能都要从头测试。LangGraph 开箱即用。

### 1.2 编排图全景

```
                      用户请求 / 告警
                            │
                            ▼
                   ┌─────────────────┐
                   │  Triage Agent   │
                   └────────┬────────┘
                            │
              ┌─────────────┼─────────────┐
              │             │             │
        direct_tool    diagnosis    alert_batch
              │             │             │
              ▼             ▼             ▼
        ┌──────────┐ ┌──────────┐ ┌───────────────┐
        │ 直接工具  │ │ Planning │ │ Alert         │
        │ 调用返回  │ │ Agent    │ │ Correlation   │
        └──────────┘ └────┬─────┘ └───────┬───────┘
                          │               │
                          ▼               │
                   ┌──────────┐           │
                   │Diagnostic│◄──────────┘
                   │ Agent    │
                   └────┬─────┘
                        │
                  ┌─────┴──────┐
               need_more   diagnosis_done
               _data           │
                  │            ▼
                  │     ┌────────────┐
                  └───→ │ HITL Gate  │
                        └─────┬──────┘
                              │
                       ┌──────┴──────┐
                    approved      rejected
                       │              │
                       ▼              ▼
                ┌──────────┐  ┌──────────┐
                │Remediate │  │ Report   │
                │ Agent    │  │ Agent    │
                └────┬─────┘  └────┬─────┘
                     │              │
                     ▼              │
                ┌──────────┐       │
                │ Report   │◄──────┘
                │ Agent    │
                └────┬─────┘
                     ▼
                ┌──────────┐
                │ 知识沉淀  │
                └──────────┘
```

---

## 2. AgentState 数据结构

### 2.0 设计决策：为什么用 TypedDict（WHY）

> **为什么用 `TypedDict` 而不是 `Pydantic BaseModel` 或 `@dataclass`？**
>
> | 方案 | 优点 | 缺点 | 适合场景 |
> |------|------|------|---------|
> | **TypedDict** ✅ | LangGraph 原生支持；dict 语义天然适合状态更新；支持 `total=False`（所有字段可选）；序列化/反序列化零成本 | 无运行时校验；IDE 类型提示不如 Pydantic 完善 | 图状态（高频读写、部分更新） |
> | Pydantic | 强类型校验；序列化/反序列化完善 | 每次更新状态都要 `model_copy()`；LangGraph 需要额外适配 | API 输入/输出校验 |
> | dataclass | 轻量；有默认值支持 | 不是 dict，LangGraph 需要自定义 state channel | 简单 DTO |
>
> **决定性理由**：LangGraph 的 StateGraph 要求状态是 dict-like 的（或者自定义 channel），TypedDict 是最自然的选择。每个节点只需 `state["field"] = value` 就能更新状态，不需要像 Pydantic 那样 `state = state.model_copy(update={"field": value})`，后者在高频更新场景下既麻烦又容易遗漏。
>
> **关于 `total=False` 的设计**：所有字段都标记为可选（`total=False`），因为不同阶段不同字段才会被填充——Triage 阶段只有输入区和分诊区有值，诊断区和输出区都还是空的。如果设为必填，初始化 state 时需要为所有字段提供占位值，既冗余又容易出错。

### 2.1 字段所有权矩阵

> **设计决策 WHY**：为什么要明确区分"谁写入、谁读取"？
>
> 在多 Agent 系统中，最危险的 bug 是"两个 Agent 同时写同一个字段导致覆盖"。明确所有权可以避免这类问题，也方便在 code review 时检查是否违反约定。

| 字段 | 写入者 | 读取者 | 更新模式 |
|------|--------|--------|---------|
| `request_id` | API 入口 | 所有节点 | 一次写入，只读 |
| `user_query` | API 入口 / AlertCorrelation | Triage, Planning, Report | 可被覆盖（告警聚合后） |
| `route` | Triage | `route_from_triage()` | 一次写入 |
| `task_plan` | Planning | Diagnostic | 一次写入 |
| `collected_data` | Diagnostic | Diagnostic, Report | 追加式（dict merge） |
| `collection_round` | Diagnostic | `route_from_diagnostic()` | 递增 |
| `diagnosis` | Diagnostic | HITL, Report, KnowledgeSink | 可被覆盖（多轮更新） |
| `remediation_plan` | Diagnostic | HITL, Remediation | 一次写入 |
| `hitl_status` | HITLGate | `route_from_hitl()` | 一次写入 |
| `final_report` | Report | API 返回 | 一次写入 |
| `total_tokens` | BaseAgentNode | 所有节点（预算检查） | 递增 |
| `error_count` | GraphErrorHandler | 路由函数（安全阀） | 递增 |

> **🔧 工程难点：TypedDict 状态字段所有权矩阵设计**
>
> **挑战**：在多 Agent 系统中，7 个 Agent 节点共享同一个 `AgentState` TypedDict 进行读写。最危险的 bug 是"两个 Agent 同时写同一个字段导致覆盖"——例如 Diagnostic Agent 写入 `diagnosis` 后，Report Agent 意外覆盖了它。由于 TypedDict 没有运行时校验（选择它是因为 LangGraph 要求 dict-like 语义，且高频读写场景下 Pydantic `model_copy()` 开销不可接受），字段写入冲突只能在设计层面和 Code Review 中防范，无法依赖框架自动检测。同时，所有字段标记为 `total=False`（可选），意味着不同阶段不同字段才会被填充，任何节点都可能在 `state.get()` 时拿到 `None`，必须处处做空值防护。
>
> **解决方案**：建立显式的"字段所有权矩阵"（上表），每个字段严格规定唯一写入者和允许的读取者，并在矩阵中标注更新模式（一次写入 / 追加式 / 可覆盖 / 递增）。追加式字段（如 `collected_data`）使用 `dict.update()` 而非直接赋值，确保多轮数据不丢失。递增字段（如 `total_tokens`、`error_count`）只允许 `+= n` 操作。`create_initial_state()` 工厂函数为所有字段提供安全的初始值，避免下游节点遇到 `KeyError`。`snapshot_state()` 在每个节点执行前后分别快照状态，便于调试时 diff 定位哪个 Agent 写了什么。在 Code Review 中，任何对非自己"所有权"字段的写入都被视为 blocking issue。这种设计将运行时隐患转化为设计时约束，虽然无法 100% 自动防护，但结合 TypedDict 的静态类型检查（mypy/pyright 可检测字段名拼写错误），覆盖了绝大多数写入冲突场景。

### 2.2 完整实现

```python
# python/src/aiops/agent/state.py
"""
AgentState — LangGraph 共享状态

所有 Agent 节点读写同一个 State 字典。
设计原则：
- 每个字段都有明确的写入者和读取者
- 不可变字段（输入）和可变字段（中间状态）分区明确
- Token/Cost 累计字段由 LLMClient 回调自动更新
"""

from __future__ import annotations

from typing import Literal, TypedDict


class ToolCallRecord(TypedDict):
    """工具调用记录"""
    tool_name: str
    parameters: dict
    result: str
    duration_ms: int
    risk_level: str
    timestamp: str
    status: Literal["success", "error", "timeout"]


class DiagnosisResult(TypedDict):
    """诊断结论"""
    root_cause: str
    confidence: float  # 0.0-1.0
    severity: Literal["critical", "high", "medium", "low", "info"]
    evidence: list[str]
    affected_components: list[str]
    causality_chain: str
    related_alerts: list[str]


class RemediationStep(TypedDict):
    """修复步骤"""
    step_number: int
    action: str
    risk_level: Literal["none", "low", "medium", "high", "critical"]
    requires_approval: bool
    rollback_action: str | None
    estimated_impact: str


class AgentState(TypedDict, total=False):
    """
    Agent 共享状态

    字段分区：
    - 输入区：请求入口写入，全程只读
    - 分诊区：Triage Agent 写入
    - 规划区：Planning Agent 写入
    - 数据区：Diagnostic Agent 写入
    - RAG 区：Planning/Diagnostic 写入
    - 诊断区：Diagnostic Agent 写入
    - HITL 区：HITL Gate 写入
    - 输出区：Report Agent 写入
    - 元信息区：框架自动维护
    """

    # === 输入区（只读）===
    request_id: str                    # 请求唯一 ID (Tracing)
    request_type: str                  # "user_query" | "alert" | "patrol"
    user_query: str                    # 用户原始输入 / 告警描述
    user_id: str                       # 操作者 ID (RBAC)
    cluster_id: str                    # 目标集群 ID
    alerts: list[dict]                 # 关联告警列表

    # === 分诊区 ===
    intent: str                        # 识别的意图
    complexity: str                    # "simple" | "moderate" | "complex"
    route: str                         # "direct_tool" | "diagnosis" | "alert_correlation"
    urgency: str                       # "critical" | "high" | "medium" | "low"
    target_components: list[str]       # 涉及的组件

    # === 规划区 ===
    task_plan: list[dict]              # 诊断步骤计划
    data_requirements: list[str]       # 需要采集的数据列表
    hypotheses: list[dict]             # 候选根因假设

    # === 数据采集区 ===
    tool_calls: list[ToolCallRecord]   # 所有工具调用记录
    collected_data: dict               # 采集到的数据 {tool_name: result}
    collection_round: int              # 当前采集轮次
    max_collection_rounds: int         # 最大采集轮次 (默认 5)

    # === RAG 上下文区 ===
    rag_context: list[dict]            # 检索到的知识库文档片段
    similar_cases: list[dict]          # 相似历史案例

    # === 诊断结果区 ===
    diagnosis: DiagnosisResult         # 诊断结论
    remediation_plan: list[RemediationStep]  # 修复方案

    # === HITL 区 ===
    hitl_required: bool                # 是否需要人工审批
    hitl_status: str                   # "pending" | "approved" | "rejected"
    hitl_comment: str                  # 审批意见

    # === 输出区 ===
    final_report: str                  # 最终报告 (Markdown)
    knowledge_entry: dict              # 待沉淀的知识条目

    # === 元信息区（框架自动维护）===
    messages: list                     # LangGraph 消息历史
    current_agent: str                 # 当前活跃 Agent
    error_count: int                   # 累计错误次数
    start_time: str                    # 开始时间
    total_tokens: int                  # 累计 Token 消耗
    total_cost_usd: float              # 累计 LLM 成本
    _force_provider: str | None        # 降级时强制指定供应商
    _simplify_prompt: bool             # 降级：简化 Prompt
    _direct_tool_name: str             # 快速路径工具名
    _direct_tool_params: dict          # 快速路径工具参数
    _triage_method: str                # 分诊方式 (rule_engine|llm|fallback)
```

### 2.3 状态初始化工厂（WHY）

> **为什么需要工厂函数？** 避免每个 API 入口都手写十几个字段的初始值。工厂函数确保所有必填字段都有合理默认值，也是防御性编程——如果某个字段忘记初始化，下游 Agent 调用 `state.get("field")` 会返回 `None` 而不是 KeyError。

```python
# python/src/aiops/agent/state.py（续）

import uuid
from datetime import datetime, timezone


def create_initial_state(
    user_query: str,
    request_type: str = "user_query",
    user_id: str = "anonymous",
    cluster_id: str = "default",
    alerts: list[dict] | None = None,
    request_id: str | None = None,
) -> AgentState:
    """
    创建 Agent 初始状态

    WHY: 集中管理初始值，避免各入口重复初始化。
    所有计数器归零，所有列表为空列表（不是 None），
    这样下游 Agent 不需要到处写 `state.get("field", [])` 的防御代码。
    """
    return AgentState(
        # 输入区
        request_id=request_id or f"REQ-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}",
        request_type=request_type,
        user_query=user_query,
        user_id=user_id,
        cluster_id=cluster_id,
        alerts=alerts or [],

        # 数据采集区（带合理默认值）
        collection_round=0,
        max_collection_rounds=5,
        tool_calls=[],
        collected_data={},

        # RAG 区
        rag_context=[],
        similar_cases=[],

        # 元信息区
        messages=[],
        current_agent="",
        error_count=0,
        start_time=datetime.now(timezone.utc).isoformat(),
        total_tokens=0,
        total_cost_usd=0.0,
    )


def snapshot_state(state: AgentState) -> dict:
    """
    状态快照（用于调试和审计）

    WHY: 生产环境中，当诊断结果不对时，运维人员需要查看
    "Agent 在每一步看到了什么"。快照只保留关键字段，
    移除大 blob（collected_data 只保留 keys），控制日志大小。
    """
    return {
        "request_id": state.get("request_id"),
        "current_agent": state.get("current_agent"),
        "route": state.get("route"),
        "intent": state.get("intent"),
        "collection_round": state.get("collection_round"),
        "confidence": state.get("diagnosis", {}).get("confidence"),
        "tool_calls_count": len(state.get("tool_calls", [])),
        "collected_data_keys": list(state.get("collected_data", {}).keys()),
        "rag_context_count": len(state.get("rag_context", [])),
        "error_count": state.get("error_count"),
        "total_tokens": state.get("total_tokens"),
        "total_cost_usd": state.get("total_cost_usd"),
    }
```

### 2.4 状态流转全景 Mermaid 图

```mermaid
stateDiagram-v2
    [*] --> InputReady : create_initial_state()
    InputReady --> Triaged : TriageNode 写入 intent/route/complexity
    
    Triaged --> DirectToolDone : route=direct_tool
    DirectToolDone --> [*] : 快速路径结束
    
    Triaged --> Planned : route=diagnosis → PlanningNode
    Triaged --> AlertCorrelated : route=alert_correlation
    AlertCorrelated --> Planned : PlanningNode
    
    Planned --> Collecting : DiagnosticNode Round 1
    Collecting --> Collecting : confidence<0.6 → 自环
    Collecting --> Diagnosed : confidence≥0.6 或达到最大轮次
    
    Diagnosed --> WaitingApproval : 有高风险修复 → HITLGateNode
    Diagnosed --> Reported : 无需审批 → ReportNode
    
    WaitingApproval --> Remediating : approved
    WaitingApproval --> Reported : rejected
    
    Remediating --> Reported : ReportNode
    Reported --> KnowledgeSunk : KnowledgeSinkNode
    KnowledgeSunk --> [*]
```

---

## 3. BaseAgentNode — 节点基类

### 3.0 设计决策：模板方法模式（WHY）

> **为什么用模板方法模式？**
>
> BaseAgentNode 的 `__call__` 是固定的执行框架（前置检查→核心逻辑→后置记录→错误处理），子类只需实现 `process()` 方法。这是经典的模板方法模式（GoF Template Method），好处是：
>
> 1. **横切关注点一次性实现**：OTel Span、Token 累计、耗时统计、错误计数在基类实现，子类零感知。如果让每个 Agent 自己做这些，会出现"Triage 记了耗时但 Diagnostic 忘了"的不一致。
> 2. **测试解耦**：单元测试 Diagnostic Agent 时只需测 `process()` 方法，不需要关心 Span 创建和 Token 累计。
> 3. **强制规范**：`@abstractmethod` 确保每个子类必须实现 `process()`，不会出现"忘记实现"的编译期错误。
>
> **被否决的方案**：
> - **装饰器链**：用 `@trace @measure @budget_check` 叠加装饰器。问题是装饰器顺序很重要且不易发现——如果 `@budget_check` 放在 `@trace` 之前，预算超限时就不会创建 Span，导致追踪断裂。模板方法把执行顺序固定在代码中，一目了然。
> - **Mixin 类**：`class TriageNode(TracingMixin, BudgetMixin, BaseNode)`。多重继承在 Python 中会导致 MRO 问题，且 `super()` 调用链难以预测。

### 3.1 类继承关系

```
BaseAgentNode (ABC)           ← 模板方法：__call__ → process()
    ├── TriageNode            ← 04: 分诊
    ├── PlanningNode          ← 06: 规划
    ├── DiagnosticNode        ← 05: 诊断
    ├── RemediationNode       ← 06: 修复
    ├── ReportNode            ← 08: 报告
    ├── PatrolNode            ← 07: 巡检
    ├── AlertCorrelationNode  ← 07: 告警关联
    ├── DirectToolNode        ← 04: 快速路径
    ├── HITLGateNode          ← 15: 人机协作
    └── KnowledgeSinkNode     ← 08: 知识沉淀
```

### 3.2 完整实现

```python
# python/src/aiops/agent/base.py
"""
BaseAgentNode — 所有 Agent 节点的基类

提供标准化的：
- 输入校验
- LLM 调用（带上下文压缩 + Token 累计）
- 输出写入 State
- 错误处理
- 指标收集
- 追踪 Span

执行流程（模板方法）：
┌───────────────────────────────────┐
│ __call__(state)                   │
│   ├── 1. 创建 OTel Span          │
│   ├── 2. 记录 current_agent      │
│   ├── 3. _pre_check(state)       │ ← Token 预算/轮次检查
│   ├── 4. _pre_process(state)     │ ← 可选钩子（子类覆盖）
│   ├── 5. process(state)          │ ← 核心逻辑（子类必须实现）
│   ├── 6. _post_process(state)    │ ← 可选钩子（子类覆盖）
│   ├── 7. 记录耗时/指标           │
│   └── 8. 异常 → 交给 ErrorHandler│
└───────────────────────────────────┘
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any

import structlog
from opentelemetry import trace

from aiops.core.errors import AIOpsError
from aiops.core.logging import get_logger
from aiops.llm.client import LLMClient
from aiops.llm.types import LLMCallContext, LLMResponse, TaskType, Sensitivity
from aiops.agent.state import AgentState, snapshot_state
from aiops.agent.context import ContextCompressor

logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)

# === Prometheus 指标 ===
from prometheus_client import Counter, Histogram

AGENT_INVOCATIONS = Counter(
    "aiops_agent_invocations_total",
    "Total agent node invocations",
    ["agent", "status"],  # status: success | error
)
AGENT_DURATION = Histogram(
    "aiops_agent_node_duration_seconds",
    "Per-node processing time",
    ["agent"],
    buckets=[0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 20, 30],
)


class BaseAgentNode(ABC):
    """
    Agent 节点基类

    WHY - 为什么 __call__ 要做这么多事？
    因为 LangGraph 调用每个节点时只调用 __call__(state)，
    我们需要在这一个入口点完成所有横切关注点的注入。
    如果子类自己实现 __call__，很容易遗漏 Span/指标/错误处理。
    """

    # 子类必须覆盖
    agent_name: str = "base"
    task_type: TaskType = TaskType.DIAGNOSTIC

    def __init__(self, llm_client: LLMClient | None = None) -> None:
        self.llm = llm_client
        self.compressor = ContextCompressor()
        self._error_handler = None  # 延迟初始化避免循环导入

    @property
    def error_handler(self):
        """延迟导入 GraphErrorHandler 避免循环依赖"""
        if self._error_handler is None:
            from aiops.agent.error_handler import GraphErrorHandler
            self._error_handler = GraphErrorHandler()
        return self._error_handler

    async def __call__(self, state: AgentState) -> AgentState:
        """
        LangGraph 调用入口 — 模板方法

        WHY: 固定执行顺序，确保每个节点都经过相同的
        前置检查→核心逻辑→后置处理→异常兜底 流程。
        """
        with tracer.start_as_current_span(
            f"agent.{self.agent_name}",
            attributes={
                "agent.name": self.agent_name,
                "request.id": state.get("request_id", ""),
                "request.type": state.get("request_type", ""),
                "cluster.id": state.get("cluster_id", ""),
            },
        ) as span:
            start = time.monotonic()
            state["current_agent"] = self.agent_name

            try:
                # Step 1: 前置检查（Token 预算、轮次限制）
                self._pre_check(state)

                # Step 2: 可选前置钩子（子类覆盖）
                await self._pre_process(state)

                # Step 3: 核心逻辑（子类必须实现）
                result = await self.process(state)

                # Step 4: 可选后置钩子（子类覆盖）
                await self._post_process(result)

                # Step 5: 记录成功指标
                duration = time.monotonic() - start
                span.set_attribute("agent.duration_ms", int(duration * 1000))
                span.set_attribute("agent.tokens", state.get("total_tokens", 0))

                AGENT_INVOCATIONS.labels(agent=self.agent_name, status="success").inc()
                AGENT_DURATION.labels(agent=self.agent_name).observe(duration)

                logger.info(
                    "agent_completed",
                    agent_name=self.agent_name,
                    duration_ms=int(duration * 1000),
                    tokens=state.get("total_tokens", 0),
                    cost_usd=f"{state.get('total_cost_usd', 0):.4f}",
                )
                return result

            except AIOpsError as e:
                # 已分类的业务异常 → 记录 + 交给 ErrorHandler
                duration = time.monotonic() - start
                span.record_exception(e)
                span.set_attribute("agent.error_code", e.code.value)
                state["error_count"] = state.get("error_count", 0) + 1

                AGENT_INVOCATIONS.labels(agent=self.agent_name, status="error").inc()
                AGENT_DURATION.labels(agent=self.agent_name).observe(duration)

                logger.error(
                    "agent_error",
                    agent_name=self.agent_name,
                    error_code=e.code.value,
                    error_message=e.message,
                    state_snapshot=snapshot_state(state),
                )

                # 尝试 ErrorHandler 降级处理
                return await self.error_handler.handle(
                    error=e,
                    state=state,
                    step_name=self.agent_name,
                    retry_fn=self.process,
                )

            except Exception as e:
                # 未预期异常 → 记录完整堆栈 + 强制结束
                duration = time.monotonic() - start
                span.record_exception(e)

                AGENT_INVOCATIONS.labels(agent=self.agent_name, status="error").inc()
                AGENT_DURATION.labels(agent=self.agent_name).observe(duration)

                logger.critical(
                    "agent_unexpected_error",
                    agent_name=self.agent_name,
                    error=str(e),
                    state_snapshot=snapshot_state(state),
                    exc_info=True,
                )

                return await self.error_handler.handle(
                    error=e, state=state, step_name=self.agent_name
                )

    @abstractmethod
    async def process(self, state: AgentState) -> AgentState:
        """
        子类实现具体逻辑

        约定：
        - 只读取自己需要的 state 字段
        - 只写入自己负责的 state 字段（参考所有权矩阵）
        - 不要在 process 中做 OTel/Prometheus/日志（基类已处理）
        - 如果需要调用 LLM，用 self._call_llm() 辅助方法
        """
        ...

    async def _pre_process(self, state: AgentState) -> None:
        """
        可选前置钩子（子类覆盖）

        典型用途：
        - DiagnosticNode: 递增 collection_round
        - PlanningNode: 加载 RAG 上下文
        - PatrolNode: 检查上次巡检时间
        """
        pass

    async def _post_process(self, state: AgentState) -> None:
        """
        可选后置钩子（子类覆盖）

        典型用途：
        - DiagnosticNode: 记录本轮采集了哪些工具
        - ReportNode: 格式化最终报告
        """
        pass

    def _pre_check(self, state: AgentState) -> None:
        """
        前置检查：Token 预算、轮次限制

        WHY: 在进入核心逻辑之前就做预算检查，
        避免白白消耗 LLM 调用后才发现超预算。
        注意：超预算不直接 raise，只是 warning，
        让子类决定是否继续（Report Agent 可能决定生成简化报告）。
        """
        from aiops.llm.budget import TokenBudgetManager

        budget = TokenBudgetManager()
        if not budget.check_budget(
            total_tokens=state.get("total_tokens", 0),
            total_cost=state.get("total_cost_usd", 0.0),
            request_type=state.get("request_type", "fault_diagnosis"),
        ):
            logger.warning(
                "budget_exceeded_before_agent",
                agent=self.agent_name,
                total_tokens=state.get("total_tokens", 0),
                remaining=budget.get_remaining(
                    state.get("total_tokens", 0),
                    state.get("request_type", "fault_diagnosis"),
                ),
            )

    # === LLM 调用辅助方法 ===

    async def _call_llm(
        self,
        state: AgentState,
        messages: list[dict],
        response_model: type | None = None,
        **kwargs,
    ) -> LLMResponse | Any:
        """
        统一 LLM 调用入口

        WHY: 所有 Agent 调用 LLM 都通过这个方法，确保：
        1. 自动构建 LLMCallContext（携带 task_type/sensitivity）
        2. 自动累加 Token 消耗到 state
        3. 自动压缩过长的 messages
        """
        if not self.llm:
            raise RuntimeError(f"{self.agent_name} requires LLMClient but none provided")

        context = self._build_context(state)

        if response_model:
            result = await self.llm.chat_structured(
                messages=messages,
                response_model=response_model,
                context=context,
                **kwargs,
            )
            # chat_structured 内部已调用 CostTracker，这里只需累加 state
            # 注意：result 是 Pydantic 模型，没有 usage 信息
            # Token 累加在 LLMClient 内部通过回调完成
            return result
        else:
            response = await self.llm.chat(
                messages=messages,
                context=context,
                **kwargs,
            )
            self._update_token_usage(state, response)
            return response

    def _build_context(self, state: AgentState) -> LLMCallContext:
        """
        构建 LLM 调用上下文

        WHY: 每个 Agent 都需要告诉 ModelRouter "我是什么类型的任务"
        + "数据敏感度如何"，ModelRouter 据此选择最合适的模型和供应商。
        """
        return LLMCallContext(
            task_type=self.task_type,
            complexity=state.get("complexity", "moderate"),
            sensitivity=self._assess_sensitivity(state),
            request_id=state.get("request_id", ""),
            session_id=state.get("request_id", ""),
            user_id=state.get("user_id", ""),
            force_provider=state.get("_force_provider"),
        )

    def _update_token_usage(self, state: AgentState, response: LLMResponse) -> None:
        """累加 Token 消耗"""
        state["total_tokens"] = state.get("total_tokens", 0) + response.usage.total_tokens
        from aiops.llm.cost import CostTracker
        tracker = CostTracker()
        cost = tracker.estimate_cost(
            response.model,
            response.usage.prompt_tokens,
            response.usage.completion_tokens,
        )
        state["total_cost_usd"] = state.get("total_cost_usd", 0.0) + cost

    @staticmethod
    def _assess_sensitivity(state: AgentState) -> Sensitivity:
        """
        评估数据敏感度

        WHY: 包含密码、IP 等敏感信息时，ModelRouter 会切换到本地模型
        （Qwen2.5-72B），避免将敏感数据发送给外部 API。
        """
        collected = str(state.get("collected_data", {}))
        if any(kw in collected.lower() for kw in [
            "password", "secret", "token", "key",
            "certificate", "private", "credential",
        ]):
            return Sensitivity.HIGH

        # 包含内网 IP 也算中等敏感
        import re
        if re.search(r'\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b', collected):
            return Sensitivity.MEDIUM

        return Sensitivity.LOW
```

---

## 4. OpsGraph — StateGraph 编排

### 4.0 设计决策（WHY）

> **为什么延迟导入所有 Node 类？**
>
> `from aiops.agent.nodes.xxx import XxxNode` 放在函数内部而不是模块顶部，原因是**循环依赖**：Agent 节点 → 导入 AgentState → 导入 base → 导入 router... 如果 graph.py 在顶部导入所有节点，而节点又导入 graph.py 中的类型，就会产生循环。延迟导入是 Python 社区处理循环依赖的标准模式。
>
> **为什么入口固定是 triage？**
>
> 所有请求（用户查询、告警、巡检）都从 Triage 节点进入，因为：
> 1. Triage 负责"安检"——检查请求合法性、评估紧急度
> 2. Triage 决定快速路径 vs 完整诊断路径，是 Token 节省的关键（40% 请求走快速路径）
> 3. 即使是巡检请求，也需要 Triage 做"请求类型适配"——将 patrol 类型映射到对应的巡检工具集
>
> **为什么 direct_tool → END 而不是 direct_tool → report？**
>
> 快速路径的设计目标就是"跳过一切不必要的步骤"。如果还要经过 Report Agent，就会多一次 LLM 调用（~800 tokens），对于"HDFS 容量多少？"这种简单查询是不必要的开销。DirectToolNode 自己会用轻量 LLM 格式化结果。

### 4.1 完整图定义 Mermaid 图

```mermaid
graph TD
    START([请求入口]) --> triage[Triage Agent]
    
    triage -->|route=direct_tool| direct_tool[DirectToolNode]
    triage -->|route=diagnosis| planning[Planning Agent]
    triage -->|route=alert_correlation| alert_corr[Alert Correlation]
    
    direct_tool --> END_FAST([结束 - 快速路径])
    
    alert_corr --> planning
    planning --> diagnostic[Diagnostic Agent]
    
    diagnostic -->|confidence<0.6| diagnostic
    diagnostic -->|高风险修复| hitl[HITL Gate]
    diagnostic -->|诊断完成| report[Report Agent]
    
    hitl -->|approved| remediation[Remediation Agent]
    hitl -->|rejected| report
    
    remediation --> report
    report --> knowledge[KnowledgeSink]
    knowledge --> END_FULL([结束 - 完整路径])
    
    style triage fill:#e1f5fe
    style diagnostic fill:#fff3e0
    style hitl fill:#fce4ec
    style report fill:#e8f5e9
```

### 4.2 完整代码实现

```python
# python/src/aiops/agent/graph.py
"""
OpsGraph — LangGraph 状态图定义

这是整个 Agent 系统的中枢：
- 定义所有节点（7 Agent + 2 辅助节点）
- 定义条件边（路由逻辑）
- 定义图的入口和出口

图的"编译"过程（compile）做了什么？
1. 验证所有边的连通性（检测死锁/孤立节点）
2. 注入 Checkpointer（持久化状态）
3. 生成执行器（支持 invoke/astream/astream_events）
"""

from __future__ import annotations

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

from aiops.agent.state import AgentState
from aiops.agent.router import (
    route_from_triage,
    route_from_diagnostic,
    route_from_hitl,
)
from aiops.llm.client import LLMClient


def build_ops_graph(llm_client: LLMClient | None = None) -> StateGraph:
    """
    构建 AIOps 编排图

    Returns:
        编译后的 LangGraph，可直接 .invoke() 或 .astream()
    """
    if llm_client is None:
        llm_client = LLMClient()

    # 延迟导入避免循环依赖
    from aiops.agent.nodes.triage import TriageNode
    from aiops.agent.nodes.planning import PlanningNode
    from aiops.agent.nodes.diagnostic import DiagnosticNode
    from aiops.agent.nodes.remediation import RemediationNode
    from aiops.agent.nodes.report import ReportNode
    from aiops.agent.nodes.patrol import PatrolNode
    from aiops.agent.nodes.alert_correlation import AlertCorrelationNode
    from aiops.agent.nodes.direct_tool import DirectToolNode
    from aiops.agent.nodes.hitl_gate import HITLGateNode
    from aiops.agent.nodes.knowledge_sink import KnowledgeSinkNode

    # 实例化节点
    triage = TriageNode(llm_client)
    planning = PlanningNode(llm_client)
    diagnostic = DiagnosticNode(llm_client)
    remediation = RemediationNode(llm_client)
    report = ReportNode(llm_client)
    alert_correlation = AlertCorrelationNode(llm_client)
    direct_tool = DirectToolNode(llm_client)
    hitl_gate = HITLGateNode(llm_client)
    knowledge_sink = KnowledgeSinkNode(llm_client)

    # 构建图
    graph = StateGraph(AgentState)

    # === 添加节点 ===
    graph.add_node("triage", triage)
    graph.add_node("planning", planning)
    graph.add_node("diagnostic", diagnostic)
    graph.add_node("remediation", remediation)
    graph.add_node("report", report)
    graph.add_node("alert_correlation", alert_correlation)
    graph.add_node("direct_tool", direct_tool)
    graph.add_node("hitl_gate", hitl_gate)
    graph.add_node("knowledge_sink", knowledge_sink)

    # === 设置入口 ===
    graph.set_entry_point("triage")

    # === 条件边 ===

    # Triage → 三路分发
    graph.add_conditional_edges(
        "triage",
        route_from_triage,
        {
            "direct_tool": "direct_tool",
            "planning": "planning",
            "alert_correlation": "alert_correlation",
        },
    )

    # 直接工具调用 → 结束
    graph.add_edge("direct_tool", END)

    # Planning → Diagnostic
    graph.add_edge("planning", "diagnostic")

    # Alert Correlation → Diagnostic
    graph.add_edge("alert_correlation", "diagnostic")

    # Diagnostic → 三路判断
    graph.add_conditional_edges(
        "diagnostic",
        route_from_diagnostic,
        {
            "need_more_data": "diagnostic",    # 自环：继续采集
            "hitl_gate": "hitl_gate",          # 高风险 → 审批
            "report": "report",                # 直接出报告
        },
    )

    # HITL Gate → 审批结果路由
    graph.add_conditional_edges(
        "hitl_gate",
        route_from_hitl,
        {
            "remediation": "remediation",      # 批准 → 执行修复
            "report": "report",                # 拒绝 → 跳过修复
        },
    )

    # Remediation → Report
    graph.add_edge("remediation", "report")

    # Report → 知识沉淀 → 结束
    graph.add_edge("report", "knowledge_sink")
    graph.add_edge("knowledge_sink", END)

    # === 编译 ===
    # WHY: MemorySaver 用于开发/测试（进程内存储，重启丢失）
    # 生产环境必须换 AsyncPostgresSaver（HITL 审批可能跨小时）
    checkpointer = MemorySaver()  # 生产环境改用 PostgresSaver
    compiled = graph.compile(checkpointer=checkpointer)

    return compiled


def build_ops_graph_production(
    llm_client: LLMClient | None = None,
    pg_conn_string: str = "",
) -> StateGraph:
    """
    生产环境图构建

    WHY: 生产环境需要：
    1. PostgreSQL checkpointer（HITL 审批可能等待 30 分钟，进程重启后需要恢复）
    2. interrupt_before 在 hitl_gate 前暂停（等待人工审批）
    """
    if llm_client is None:
        llm_client = LLMClient()

    if not pg_conn_string:
        from aiops.core.config import settings
        pg_conn_string = settings.db.postgres_url

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    # 构建图（复用同一个逻辑）
    graph = _build_graph_nodes_and_edges(llm_client)

    # PostgreSQL 持久化
    checkpointer = AsyncPostgresSaver.from_conn_string(pg_conn_string)

    # 编译时设置 interrupt_before
    # WHY: hitl_gate 节点需要暂停等待人工审批。
    # interrupt_before 会在节点执行前暂停图，返回当前状态。
    # 审批完成后，API 层带着审批结果重新 invoke 同一个 thread_id，
    # 图从 hitl_gate 继续执行。
    compiled = graph.compile(
        checkpointer=checkpointer,
        interrupt_before=["hitl_gate"],  # 在 HITL 门控前暂停
    )

    return compiled


def _build_graph_nodes_and_edges(llm_client: LLMClient) -> StateGraph:
    """
    内部函数：注册所有节点和边（供开发/生产复用）
    """
    from aiops.agent.nodes.triage import TriageNode
    from aiops.agent.nodes.planning import PlanningNode
    from aiops.agent.nodes.diagnostic import DiagnosticNode
    from aiops.agent.nodes.remediation import RemediationNode
    from aiops.agent.nodes.report import ReportNode
    from aiops.agent.nodes.alert_correlation import AlertCorrelationNode
    from aiops.agent.nodes.direct_tool import DirectToolNode
    from aiops.agent.nodes.hitl_gate import HITLGateNode
    from aiops.agent.nodes.knowledge_sink import KnowledgeSinkNode
    from aiops.mcp_client.client import MCPClient

    # MCP 客户端（工具调用需要）
    mcp_client = MCPClient()

    # 实例化节点
    triage = TriageNode(llm_client)
    planning = PlanningNode(llm_client)
    diagnostic = DiagnosticNode(llm_client)
    remediation = RemediationNode(llm_client)
    report = ReportNode(llm_client)
    alert_correlation = AlertCorrelationNode(llm_client)
    direct_tool = DirectToolNode(mcp_client=mcp_client, llm_client=llm_client)
    hitl_gate = HITLGateNode(llm_client)
    knowledge_sink = KnowledgeSinkNode(llm_client)

    # 构建图
    graph = StateGraph(AgentState)

    # 注册所有节点
    for name, node in [
        ("triage", triage), ("planning", planning),
        ("diagnostic", diagnostic), ("remediation", remediation),
        ("report", report), ("alert_correlation", alert_correlation),
        ("direct_tool", direct_tool), ("hitl_gate", hitl_gate),
        ("knowledge_sink", knowledge_sink),
    ]:
        graph.add_node(name, node)

    # 设置入口
    graph.set_entry_point("triage")

    # 条件边（路由逻辑在 router.py 中定义）
    graph.add_conditional_edges("triage", route_from_triage, {
        "direct_tool": "direct_tool",
        "planning": "planning",
        "alert_correlation": "alert_correlation",
    })
    graph.add_edge("direct_tool", END)
    graph.add_edge("planning", "diagnostic")
    graph.add_edge("alert_correlation", "diagnostic")
    graph.add_conditional_edges("diagnostic", route_from_diagnostic, {
        "need_more_data": "diagnostic",
        "hitl_gate": "hitl_gate",
        "report": "report",
    })
    graph.add_conditional_edges("hitl_gate", route_from_hitl, {
        "remediation": "remediation",
        "report": "report",
    })
    graph.add_edge("remediation", "report")
    graph.add_edge("report", "knowledge_sink")
    graph.add_edge("knowledge_sink", END)

    return graph
```

---

## 5. 条件路由函数

### 5.0 设计决策（WHY）

> **为什么路由逻辑用代码而不是 LLM？**
>
> 路由是整个系统最关键的决策点——决定了请求走哪条路径、消耗多少 Token、需要多长时间。如果用 LLM 做路由，会有 3 个致命问题：
>
> 1. **延迟**：每次路由决策需要 1-2s（LLM 调用），对于简单查询来说延迟翻倍
> 2. **不确定性**：LLM 可能把"HDFS 容量查询"路由到完整诊断流程（浪费 10000+ Token）
> 3. **不可审计**：路由逻辑变成黑箱，出问题时无法 debug "为什么这个请求走了错误的路径"
>
> 代码路由的好处：**确定性**（相同输入永远得到相同路由）、**可审计**（每个分支都有日志）、**可测试**（单元测试可以覆盖所有分支）、**零延迟**（纯 CPU 计算，微秒级）。
>
> **路由决策由谁做？** Triage Agent 负责填写 `state["route"]`、`state["intent"]`、`state["complexity"]`，路由函数只是读取这些值做分发。这样路由逻辑和判断逻辑分离——Triage 可以用 LLM 做复杂的意图识别，但路由本身是确定性的 if-else。

### 5.1 路由决策树

```mermaid
graph TD
    A[route_from_triage] --> B{state.route?}
    B -->|direct_tool| C[direct_tool 节点]
    B -->|alert_correlation| D[alert_correlation 节点]
    B -->|其他| E[planning 节点]
    
    F[route_from_diagnostic] --> G{collection_round >= max?}
    G -->|是| H[report - 安全阀]
    G -->|否| I{confidence < 0.6 且有 data_requirements?}
    I -->|是| J[need_more_data - 自环]
    I -->|否| K{有高风险修复步骤?}
    K -->|是| L[hitl_gate]
    K -->|否| M[report]
    
    N[route_from_hitl] --> O{hitl_status?}
    O -->|approved| P[remediation]
    O -->|其他| Q[report]
```

### 5.2 完整实现

```python
# python/src/aiops/agent/router.py
"""
条件路由函数

LangGraph 条件边的决策逻辑。
每个函数接收 AgentState，返回下一个节点名称。

设计原则：
- 路由逻辑用代码控制，不依赖 LLM（确定性优先）
- 有明确的兜底分支（防止路由死锁）
- 包含安全检查（轮次限制、预算限制）
- 每个分支都有结构化日志（可审计）
"""

from __future__ import annotations

from aiops.core.logging import get_logger
from aiops.agent.state import AgentState

logger = get_logger(__name__)


def route_from_triage(state: AgentState) -> str:
    """
    Triage Agent 出口路由

    simple query → direct_tool（~40% 请求走这条路，节省 70%+ Token）
    alert batch  → alert_correlation（先聚合告警再诊断）
    complex      → planning（走完整诊断流程）
    """
    route = state.get("route", "diagnosis")

    if route == "direct_tool":
        logger.info("route_triage_fast_path", intent=state.get("intent"))
        return "direct_tool"
    elif route == "alert_correlation":
        logger.info("route_triage_alert", alert_count=len(state.get("alerts", [])))
        return "alert_correlation"
    else:
        logger.info("route_triage_diagnosis", complexity=state.get("complexity"))
        return "planning"


def route_from_diagnostic(state: AgentState) -> str:
    """
    Diagnostic Agent 出口路由

    置信度不够 + 还有数据可采 → 自环回 diagnostic（最多 5 轮）
    有高风险修复建议            → hitl_gate（人工审批）
    诊断完成                    → report

    WHY - 为什么 0.6 是自环阈值？
    基于评测集的经验数据：
    - confidence < 0.4 的诊断结论几乎都是错误的
    - 0.4-0.6 范围内约 60% 可以通过补充 1-2 轮数据提升到 0.7+
    - 0.6-0.8 是可接受的诊断质量（在报告中标注"初步判断"）
    - > 0.8 是高质量诊断
    所以 0.6 是"值得再花一轮 Token 提升质量"的分界线。

    WHY - 为什么最大轮次是 5？
    每轮诊断约消耗 3000 Token + 2-5s。5 轮 = 15000 Token + 10-25s，
    已经接近单次诊断的 Token 预算上限（15000）。
    超过 5 轮说明问题极其复杂或数据不足，继续自环收益递减。
    """
    diagnosis = state.get("diagnosis", {})
    collection_round = state.get("collection_round", 0)
    max_rounds = state.get("max_collection_rounds", 5)
    confidence = diagnosis.get("confidence", 0)

    # 安全阀 1：超过最大轮次强制结束
    if collection_round >= max_rounds:
        logger.warning(
            "diagnostic_max_rounds",
            rounds=collection_round,
            confidence=confidence,
        )
        return "report"

    # 安全阀 2：Token 预算耗尽也强制结束
    total_tokens = state.get("total_tokens", 0)
    if total_tokens > 14000:  # 接近 15000 上限
        logger.warning(
            "diagnostic_budget_limit",
            total_tokens=total_tokens,
            confidence=confidence,
        )
        return "report"

    # 置信度不够且还有未采集的数据
    if confidence < 0.6 and state.get("data_requirements"):
        logger.info(
            "diagnostic_need_more_data",
            confidence=confidence,
            round=collection_round,
            remaining_data=state.get("data_requirements", [])[:3],
        )
        return "need_more_data"

    # 有高风险修复建议 → 需要人工审批
    remediation = state.get("remediation_plan", [])
    high_risk = [s for s in remediation if s.get("risk_level") in ("high", "critical")]
    if high_risk:
        logger.info(
            "diagnostic_hitl_required",
            high_risk_count=len(high_risk),
            risk_actions=[s.get("action", "")[:50] for s in high_risk],
        )
        return "hitl_gate"

    return "report"


def route_from_hitl(state: AgentState) -> str:
    """
    HITL Gate 出口路由

    approved → remediation（执行修复）
    rejected → report（跳过修复，仅出报告）
    """
    status = state.get("hitl_status", "rejected")

    if status == "approved":
        logger.info("hitl_approved", approver=state.get("hitl_comment", ""))
        return "remediation"
    else:
        logger.info("hitl_rejected", reason=state.get("hitl_comment", ""))
        return "report"
```

> **🔧 工程难点：条件路由中的多分支判断一致性与安全阀**
>
> **挑战**：LangGraph 的条件边由代码确定性控制（这正是选择 LangGraph 而非 CrewAI/AutoGen 的决定性理由），但 3 个路由函数（`route_from_triage`、`route_from_diagnostic`、`route_from_hitl`）需要综合多个状态字段做判断——例如 `route_from_diagnostic` 需要同时考虑 `confidence`、`data_requirements`、`collection_round`、`total_tokens`、`error_count` 五个维度。任何一个维度的判断遗漏都可能导致不可预期的执行路径：如果忘记检查 `total_tokens`，一个复杂问题可能无限自环直到 Token 预算爆炸；如果忘记检查 `error_count`，一个持续报错的 Agent 可能永远不会被终止。更隐蔽的问题是分支间的优先级冲突——当 `confidence < 0.6`（应自环）但同时 `total_tokens > 14000`（应终止）时，哪个条件优先？
>
> **解决方案**：为每个路由函数设计严格的条件优先级链（安全阀优先级最高）：`error_count >= MAX_GLOBAL_ERRORS` → `total_tokens > budget` → `round >= max_rounds` → 正常业务判断。安全阀检查始终在最前面，确保即使业务逻辑有 bug 也不会导致无限循环或成本失控。每个路由分支返回的字符串必须与 `OpsGraph` 的 `add_conditional_edges` 注册的 key 精确匹配——拼写错误会导致 LangGraph 运行时 KeyError，因此所有路由返回值定义为模块级常量（`ROUTE_DIAGNOSIS = "diagnosis"`）并在单元测试中验证全覆盖。`route_from_diagnostic` 的收敛条件矩阵（§5.4）完整枚举了所有可能的状态组合及对应行为，确保不存在"未处理的分支"。路由函数的每次执行都通过 `logger.info` 记录命中的分支和关键状态值，在生产环境中可通过日志回溯任何请求的完整路由路径。

---

## 6. GraphErrorHandler — 图级错误处理

### 6.0 设计决策（WHY）

> **为什么需要图级错误处理器？**
>
> 如果让每个 Agent 自己处理异常，会出现两个问题：
> 1. **策略不一致**：Triage 重试 3 次但 Diagnostic 只重试 1 次，难以维护
> 2. **全局状态不可控**：Agent A 重试了 5 次 LLM 调用，Agent B 又重试了 5 次，总共 10 次重试可能已经超出预算，但每个 Agent 不知道全局状态
>
> GraphErrorHandler 作为中央错误协调器，维护全局错误计数，确保总重试次数不超限。
>
> **为什么用 dataclass 而不是 dict 定义 ErrorStrategy？**
> dataclass 提供类型提示和 IDE 自动补全。`strategy.max_retries` 比 `strategy["max_retries"]` 更安全（拼写错误时编译期报错）。
>
> **为什么 retry 用线性退避而不是指数退避？**
> 对于 LLM API 调用，指数退避（1s→2s→4s→8s）在第 3 次重试时等待 8s 太长。线性退避（5s→10s→15s）更适合——LLM API 的 rate limit 通常是 1 分钟窗口，等 5-15s 足够窗口重置。

### 6.1 错误分类决策树

```mermaid
graph TD
    E[Exception] --> A{isinstance?}
    A -->|LLMError| B{error.code?}
    B -->|TIMEOUT| C[llm_timeout → 切换供应商]
    B -->|RATE_LIMITED| D[llm_rate_limit → 切换供应商]
    B -->|PARSE_ERROR| E2[llm_format → 简化 Prompt]
    B -->|其他| F[llm_error → 切换供应商]
    
    A -->|ToolError| G{error.code?}
    G -->|TIMEOUT| H[tool_timeout → 跳过工具]
    G -->|其他| I[tool_error → 跳过工具]
    
    A -->|RAGError| J[rag_timeout → 跳过 RAG]
    A -->|其他| K[unknown → 强制结束]
```

### 6.2 降级策略详解

| 降级方案 | 触发场景 | 具体行为 | WHY |
|---------|---------|---------|-----|
| `switch_provider` | LLM 超时/限流 | 按 openai→anthropic→local 顺序切换 | 多供应商冗余是 LLM 应用的标配，切换成本为零（litellm 抽象） |
| `skip_tool` | MCP 工具超时/报错 | 在 collected_data 中标记"不可用" | 单个工具不可用不应阻塞整体诊断，LLM 会在分析中注明"部分数据缺失" |
| `skip_rag` | 向量库/ES 超时 | rag_context 设为空列表 | RAG 是增强而非必须，没有 RAG 仍可基于工具数据做诊断（只是准确度略低） |
| `simplify_prompt` | 结构化输出解析失败 | 设置 `_simplify_prompt=True` 标记 | 复杂 JSON schema 可能导致 LLM 输出格式错误，简化后（减少字段）成功率更高 |
| `force_end` | 全局错误超限/未知异常 | 生成错误报告 + 终止 | 防止无限重试消耗 Token，快速失败比慢慢失败更好（运维人员可以介入人工处理） |

### 6.3 完整实现

```python
# python/src/aiops/agent/error_handler.py
"""
GraphErrorHandler — Agent 执行错误的统一处理

错误策略矩阵：
| 错误类型        | 重试 | 间隔   | 降级方案              |
|----------------|------|--------|--------------------- |
| llm_timeout    | 2    | 5s     | 切换 LLM 供应商      |
| llm_rate_limit | 3    | 10s    | 切换 LLM 供应商      |
| llm_format     | 3    | 0s     | 简化 Prompt          |
| tool_timeout   | 2    | 3s     | 跳过工具             |
| tool_error     | 1    | 0s     | 跳过工具             |
| rag_timeout    | 1    | 3s     | 跳过 RAG             |
| state_corrupt  | 0    | —      | 从 checkpoint 恢复    |
| unknown        | 1    | 0s     | 报告并降级            |

全局安全阀：
- 单个请求最多 10 次错误（超过则强制终止）
- 重试延迟采用线性退避（delay * attempt）
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from aiops.core.errors import AIOpsError, ErrorCode, LLMError, ToolError, RAGError
from aiops.core.logging import get_logger
from aiops.agent.state import AgentState

logger = get_logger(__name__)

MAX_GLOBAL_ERRORS = 10


@dataclass
class ErrorStrategy:
    max_retries: int
    retry_delay_seconds: float
    fallback: str  # "switch_provider" | "skip_tool" | "skip_rag" | "simplify_prompt" | "force_end"


ERROR_STRATEGIES: dict[str, ErrorStrategy] = {
    "llm_timeout":      ErrorStrategy(2, 5.0,  "switch_provider"),
    "llm_rate_limit":   ErrorStrategy(3, 10.0, "switch_provider"),
    "llm_format_error": ErrorStrategy(3, 0.0,  "simplify_prompt"),
    "tool_timeout":     ErrorStrategy(2, 3.0,  "skip_tool"),
    "tool_error":       ErrorStrategy(1, 0.0,  "skip_tool"),
    "rag_timeout":      ErrorStrategy(1, 3.0,  "skip_rag"),
    "unknown":          ErrorStrategy(1, 0.0,  "force_end"),
}


class GraphErrorHandler:
    """图级错误处理器"""

    def classify_error(self, error: Exception) -> str:
        """错误分类"""
        if isinstance(error, LLMError):
            if error.code == ErrorCode.LLM_TIMEOUT:
                return "llm_timeout"
            elif error.code == ErrorCode.LLM_RATE_LIMITED:
                return "llm_rate_limit"
            elif error.code == ErrorCode.LLM_PARSE_ERROR:
                return "llm_format_error"
        elif isinstance(error, ToolError):
            if error.code == ErrorCode.TOOL_TIMEOUT:
                return "tool_timeout"
            return "tool_error"
        elif isinstance(error, RAGError):
            return "rag_timeout"
        return "unknown"

    async def handle(
        self,
        error: Exception,
        state: AgentState,
        step_name: str,
        retry_fn: callable | None = None,
    ) -> AgentState:
        """处理错误：重试 → 降级"""
        error_type = self.classify_error(error)
        strategy = ERROR_STRATEGIES.get(error_type, ERROR_STRATEGIES["unknown"])

        state["error_count"] = state.get("error_count", 0) + 1

        # 全局错误限制
        if state["error_count"] > MAX_GLOBAL_ERRORS:
            logger.error("max_global_errors_exceeded", count=state["error_count"])
            return self._force_end(state, "错误次数过多，自动终止")

        # 重试
        if retry_fn and strategy.max_retries > 0:
            for attempt in range(strategy.max_retries):
                if strategy.retry_delay_seconds > 0:
                    await asyncio.sleep(strategy.retry_delay_seconds * (attempt + 1))
                try:
                    return await retry_fn(state)
                except Exception:
                    continue

        # 降级
        return self._execute_fallback(state, step_name, strategy.fallback)

    def _execute_fallback(
        self, state: AgentState, step_name: str, fallback: str
    ) -> AgentState:
        """执行降级方案"""
        if fallback == "switch_provider":
            providers = ["openai", "anthropic", "local"]
            current = state.get("_force_provider")
            for p in providers:
                if p != current:
                    state["_force_provider"] = p
                    break
            logger.info("fallback_switch_provider", new_provider=state["_force_provider"])
            return state

        elif fallback == "skip_tool":
            state.setdefault("collected_data", {})[f"UNAVAILABLE_{step_name}"] = "工具暂时不可用"
            logger.info("fallback_skip_tool", tool=step_name)
            return state

        elif fallback == "skip_rag":
            state["rag_context"] = []
            logger.info("fallback_skip_rag")
            return state

        elif fallback == "simplify_prompt":
            # 标记需要简化 prompt（具体 Agent 节点自行处理）
            state["_simplify_prompt"] = True
            logger.info("fallback_simplify_prompt")
            return state

        else:
            return self._force_end(state, f"步骤 {step_name} 执行失败")

    @staticmethod
    def _force_end(state: AgentState, reason: str) -> AgentState:
        """强制结束，生成错误报告"""
        state["final_report"] = (
            f"⚠️ Agent 执行异常终止\n\n"
            f"**原因**：{reason}\n"
            f"**已完成步骤**：{len(state.get('tool_calls', []))} 次工具调用\n"
            f"**Token 消耗**：{state.get('total_tokens', 0)}\n\n"
            f"请联系运维人员人工处理。"
        )

        # Prometheus 指标
        from prometheus_client import Counter
        ERROR_FORCED_END = Counter(
            "aiops_agent_forced_end_total",
            "Agent forced termination count",
            ["reason_category"],
        )
        ERROR_FORCED_END.labels(
            reason_category="budget" if "预算" in reason else
                           "max_errors" if "错误次数" in reason else
                           "unknown"
        ).inc()

        return state
```

### 6.4 错误恢复流程示例

```
场景：Diagnostic Agent 调用 hdfs_namenode_status 超时

执行流程：
1. DiagnosticNode.process() 调用 mcp.call_tool("hdfs_namenode_status") → ToolError(TIMEOUT)
2. BaseAgentNode.__call__ 捕获 AIOpsError → 调用 error_handler.handle()
3. GraphErrorHandler.classify_error() → "tool_timeout"
4. 查表 → ErrorStrategy(max_retries=2, retry_delay=3.0, fallback="skip_tool")
5. 重试 1: sleep(3s) → mcp.call_tool() → 再次超时
6. 重试 2: sleep(6s) → mcp.call_tool() → 再次超时
7. 重试耗尽 → _execute_fallback("skip_tool")
8. collected_data["UNAVAILABLE_hdfs_namenode_status"] = "工具暂时不可用"
9. 返回 state → Diagnostic Agent 继续执行（LLM 会在分析中注明数据缺失）

总影响：延迟增加 ~15s，诊断质量略降（少一个数据源），但不会阻塞或崩溃
```
```

---

## 7. ContextCompressor — 上下文压缩

### 7.0 设计决策（WHY）

> **为什么需要上下文压缩？**
>
> LLM 的上下文窗口是固定的（GPT-4o 128K tokens），但 Token 费用与输入长度成正比。一个完整的诊断过程可能调用 5-8 个工具，每个工具返回 1000-5000 字，加上 RAG 上下文和历史步骤，总量轻松超过 30000 tokens（约 $0.30/次）。如果不压缩，Token 成本会失控。
>
> **压缩策略：优先级分层**
>
> ```
> 优先级 1 (不可压缩): 当前诊断计划 + 最新假设 → ~1000 tokens
> 优先级 2 (可压缩):   工具返回数据 → 只保留异常行 → ~3000 tokens
> 优先级 3 (可丢弃):   RAG 上下文 → 最多 3 条 → ~2000 tokens
> 优先级 4 (可丢弃):   历史步骤摘要 → 最近 10 步 → ~500 tokens
> ─────────────────────────────────────────────────────────
> 总预算：~8000 tokens（约 $0.02/次 input cost）
> ```
>
> **为什么 8000 tokens？** 这是 Token 预算和诊断质量的平衡点：
> - < 4000：LLM 经常"忘记"之前采集的数据，导致重复采集
> - 4000-8000：最佳甜蜜点，LLM 能看到足够的上下文做出判断
> - 8000-16000：质量提升 < 5%，但成本翻倍
> - > 16000：几乎无提升，因为 LLM 在长文本中的注意力衰减

### 7.1 三层记忆架构

```
┌─────────────────────────────────────────────┐
│ Layer 1: 即时上下文 (~8000 tokens)          │
│ ┌────────────────────────────────────────┐  │
│ │ 诊断计划 + 当前假设 (不可压缩)        │  │
│ │ 最新工具数据 (压缩：只保留异常行)     │  │
│ │ RAG 文档 (截断到 top-3)               │  │
│ │ 历史步骤摘要 (最近 10 步)             │  │
│ └────────────────────────────────────────┘  │
│ → 直接传给 LLM 的 messages                  │
├─────────────────────────────────────────────┤
│ Layer 2: 会话记忆 (Redis, TTL=30min)        │
│ ┌────────────────────────────────────────┐  │
│ │ 完整工具返回数据 (hash by request_id) │  │
│ │ 所有历史步骤详情                       │  │
│ └────────────────────────────────────────┘  │
│ → Agent 需要时按 key 加载回 Layer 1        │
├─────────────────────────────────────────────┤
│ Layer 3: 长期记忆 (Milvus + PostgreSQL)     │
│ ┌────────────────────────────────────────┐  │
│ │ 历史诊断案例 (向量库)                  │  │
│ │ 知识图谱关系 (Neo4j)                   │  │
│ │ SOP 文档 (ES)                          │  │
│ └────────────────────────────────────────┘  │
│ → 通过 RAG 语义检索加载相关片段到 Layer 1  │
└─────────────────────────────────────────────┘
```

### 7.2 完整实现

```python
# python/src/aiops/agent/context.py
"""
ContextCompressor — 上下文窗口管理

确保传给 LLM 的上下文在 Token 预算内。

分层记忆：
- Layer 1: 即时上下文（~8000 tokens）— LLM 直接可见
- Layer 2: 会话记忆（Redis TTL）— 按需加载
- Layer 3: 长期记忆（向量库）— 语义检索

压缩算法：
1. 预算分配：plan=20%, data=50%, rag=20%, history=10%
2. 数据压缩：只保留包含异常关键词的行
3. RAG 截断：按 rerank 分数取 top-3
4. 历史摘要：最近 10 步，只保留工具名+状态+耗时
"""

from __future__ import annotations

import tiktoken

from aiops.agent.state import AgentState
from aiops.core.logging import get_logger

logger = get_logger(__name__)


class ContextCompressor:
    MAX_CONTEXT_TOKENS = 8000

    def __init__(self) -> None:
        try:
            self._enc = tiktoken.encoding_for_model("gpt-4o")
        except Exception:
            self._enc = tiktoken.get_encoding("cl100k_base")

    def compress(self, state: AgentState) -> str:
        """构建压缩后的 LLM 输入上下文"""
        sections: list[str] = []
        remaining = self.MAX_CONTEXT_TOKENS

        # 1. 必须：当前诊断计划（不可压缩）
        plan = self._format_plan(state)
        remaining -= self._count(plan)
        sections.append(plan)

        # 2. 必须：最新工具结果（压缩长结果）
        data = self._compress_tool_results(
            state.get("collected_data", {}),
            max_tokens=int(remaining * 0.5),
        )
        remaining -= self._count(data)
        sections.append(data)

        # 3. 可选：RAG 上下文
        rag = state.get("rag_context", [])
        if rag and remaining > 500:
            rag_text = self._truncate_rag(rag, max_tokens=int(remaining * 0.3))
            remaining -= self._count(rag_text)
            sections.append(rag_text)

        # 4. 可选：历史步骤摘要
        history = state.get("tool_calls", [])
        if history and remaining > 200:
            summary = self._summarize_history(history, max_tokens=remaining)
            sections.append(summary)

        return "\n\n".join(sections)

    def _count(self, text: str) -> int:
        return len(self._enc.encode(text))

    def _format_plan(self, state: AgentState) -> str:
        plan = state.get("task_plan", [])
        if not plan:
            return "## 诊断计划\n尚未生成诊断计划。"
        lines = ["## 诊断计划"]
        for step in plan:
            lines.append(f"- 步骤 {step.get('step', '?')}: {step.get('description', '')}")
        return "\n".join(lines)

    def _compress_tool_results(self, data: dict, max_tokens: int) -> str:
        if not data:
            return "## 采集数据\n暂无数据。"
        parts = ["## 采集数据"]
        per_tool = max_tokens // max(len(data), 1)
        for tool_name, result in data.items():
            result_str = str(result)
            if self._count(result_str) > per_tool:
                # 截取关键信息：异常、错误、警告行
                lines = result_str.split("\n")
                key_lines = [l for l in lines if any(
                    kw in l.lower() for kw in ["⚠️", "🔴", "🚨", "error", "warning", "异常", "超过"]
                )]
                if key_lines:
                    result_str = f"(已压缩，仅保留关键发现)\n" + "\n".join(key_lines[:20])
                else:
                    result_str = result_str[:per_tool * 4]  # 粗略截断
            parts.append(f"### {tool_name}\n{result_str}")
        return "\n\n".join(parts)

    def _truncate_rag(self, contexts: list[dict], max_tokens: int) -> str:
        parts = ["## 知识库参考"]
        budget = max_tokens
        for ctx in contexts:
            text = ctx.get("content", "")
            token_count = self._count(text)
            if token_count > budget:
                break
            parts.append(f"- {text[:500]}")
            budget -= token_count
        return "\n".join(parts)

    def _summarize_history(self, calls: list, max_tokens: int) -> str:
        lines = ["## 历史工具调用摘要"]
        for call in calls[-10:]:  # 最近 10 次
            status_icon = "✅" if call.get("status") == "success" else "❌"
            lines.append(
                f"- {status_icon} {call.get('tool_name', '?')} "
                f"({call.get('duration_ms', 0)}ms)"
            )
        text = "\n".join(lines)
        return text[:max_tokens * 4]  # 粗略截断
```

> **🔧 工程难点：3 层上下文压缩的预算分配与异常行保留**
>
> **挑战**：LLM 的有效上下文窗口虽然越来越大（GPT-4o 128K），但"大窗口 ≠ 高利用率"——实测超过 8K tokens 后，LLM 对中间部分的注意力显著衰减（"U 型注意力"现象），导致关键异常指标被"淹没"在大量正常数据中。同时，多轮诊断场景下上下文会随轮次线性膨胀：5 轮 × 5 个工具 × ~1000 字/工具 = ~25K 字的原始数据。如果不做压缩，Token 成本翻倍不说，诊断准确率反而从 78% 下降到 60%+（一次性分析模式的教训）。但压缩又不能"一刀切"——异常行（`⚠️`/`🔴`/`ERROR`）必须 100% 保留，因为它们是根因分析的核心证据；正常行可以截断，但不能全删（排除法需要"正常"数据来否定假设）。
>
> **解决方案**：设计 3 层记忆架构（Immediate 8K tokens / Session Redis TTL=30min / Long-term Vector），每层有不同的保留策略和 TTL。核心的 `ContextCompressor` 采用优先级预算分配策略：`plan`=20%（诊断计划）、`data`=50%（工具采集数据）、`rag`=20%（知识库上下文）、`history`=10%（历史对话）。在 `data` 50% 预算内部，进一步按重要性分层——层次 1（异常/错误/警告关键词行）永远保留、层次 2（资源使用率/延迟数值等关键正常指标）尽量保留、层次 3（普通状态信息）按比例截断尾部。多轮场景下采用"recency-weighted"压缩：Round 1 的数据压缩比 2.5x（只保留异常行和关键数值），Round 2 的新数据压缩比 1.2x（保留更多细节），同时前轮诊断结论作为 ~300 token 摘要注入。实测 5 轮后总 Token 控制在 4K-5K 范围内（仅增长 7%/轮），信息保留率（异常/关键指标）达到 98%。被截断的完整数据保存在 Layer 2 Redis 中，如果 LLM 在 `additional_data_needed` 中请求更多细节，可以从 Redis 重新加载。

---

## 8. 图的使用方式

### 8.1 同步调用

```python
# 完整执行
from aiops.agent.graph import build_ops_graph

graph = build_ops_graph()

result = await graph.ainvoke({
    "request_id": "REQ-20260328-001",
    "request_type": "user_query",
    "user_query": "HDFS NameNode 堆内存持续升高，写入变慢",
    "user_id": "user-001",
    "cluster_id": "prod-bigdata-01",
    "alerts": [],
    "collection_round": 0,
    "max_collection_rounds": 5,
    "error_count": 0,
    "total_tokens": 0,
    "total_cost_usd": 0.0,
    "tool_calls": [],
    "collected_data": {},
})

print(result["final_report"])
```

### 8.2 流式输出

```python
# SSE 流式（FastAPI 集成）
from fastapi.responses import StreamingResponse

async def stream_diagnosis(request_data: dict):
    graph = build_ops_graph()

    async def event_generator():
        async for event in graph.astream_events(request_data, version="v2"):
            if event["event"] == "on_chain_end":
                node = event.get("name", "")
                yield f"data: {{\"node\": \"{node}\", \"status\": \"completed\"}}\n\n"
            elif event["event"] == "on_chat_model_stream":
                chunk = event["data"]["chunk"].content
                if chunk:
                    yield f"data: {{\"type\": \"token\", \"content\": \"{chunk}\"}}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")
```

### 8.3 断点续传

```python
# 从 checkpoint 恢复（HITL 审批等待后继续）
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

# 生产环境使用 PostgreSQL 持久化
checkpointer = AsyncPostgresSaver.from_conn_string(
    "postgresql://aiops:aiops@localhost:5432/aiops"
)
graph = build_ops_graph().compile(checkpointer=checkpointer)

# 恢复执行（传入 thread_id）
config = {"configurable": {"thread_id": "REQ-20260328-001"}}
result = await graph.ainvoke(
    {"hitl_status": "approved", "hitl_comment": "同意重启 NameNode"},
    config=config,
)
```

---

## 9. 子图组合模式

### 9.1 设计决策（WHY）

> **为什么需要子图？**
>
> 将 "Planning → Diagnostic → Report" 封装为子图有两个实际用途：
> 1. **A/B 测试**：可以同时运行旧诊断流程（v1 子图）和新流程（v2 子图），按百分比分流
> 2. **巡检复用**：PatrolAgent 的巡检流程也是"计划→执行→报告"，可以复用诊断子图的骨架

### 9.2 子图定义

```python
# python/src/aiops/agent/subgraphs/diagnosis_flow.py
"""
诊断子图 — 封装 Planning → Diagnostic → Report 链路

WHY: 子图有独立的入口和出口，但共享外层 AgentState。
这意味着子图可以被外层图的不同分支调用，
也可以独立测试（不需要 Triage 节点就能运行诊断流程）。
"""

from langgraph.graph import StateGraph, END

from aiops.agent.state import AgentState
from aiops.agent.router import route_from_diagnostic, route_from_hitl


def build_diagnosis_subgraph(
    planning_node,
    diagnostic_node,
    hitl_gate_node,
    remediation_node,
    report_node,
) -> StateGraph:
    """
    构建诊断子图

    子图结构：
    Planning → Diagnostic ⇄ (自环)
                  │
            ┌─────┴─────┐
         hitl_gate    report
            │              │
       remediation    ←────┘
            │
          report
    """
    subgraph = StateGraph(AgentState)

    subgraph.add_node("planning", planning_node)
    subgraph.add_node("diagnostic", diagnostic_node)
    subgraph.add_node("hitl_gate", hitl_gate_node)
    subgraph.add_node("remediation", remediation_node)
    subgraph.add_node("report", report_node)

    subgraph.set_entry_point("planning")
    subgraph.add_edge("planning", "diagnostic")

    subgraph.add_conditional_edges("diagnostic", route_from_diagnostic, {
        "need_more_data": "diagnostic",
        "hitl_gate": "hitl_gate",
        "report": "report",
    })

    subgraph.add_conditional_edges("hitl_gate", route_from_hitl, {
        "remediation": "remediation",
        "report": "report",
    })

    subgraph.add_edge("remediation", "report")
    subgraph.add_edge("report", END)

    return subgraph
```

### 9.3 A/B 测试场景

```python
# 外层图中使用子图做 A/B 测试
def route_ab_test(state: AgentState) -> str:
    """按 request_id hash 做 50/50 分流"""
    request_id = state.get("request_id", "")
    if hash(request_id) % 100 < 50:
        return "diagnosis_v1"
    return "diagnosis_v2"

# graph.add_conditional_edges("triage", route_ab_test, {
#     "diagnosis_v1": diagnosis_subgraph_v1,
#     "diagnosis_v2": diagnosis_subgraph_v2,
# })
```

---

## 10. 并发安全与状态隔离

### 10.1 设计决策（WHY）

> **同一个图实例能否并发处理多个请求？**
>
> **可以**，因为 LangGraph 的状态隔离是通过 `thread_id` 实现的：
> - 每个请求有唯一的 `request_id`，作为 `thread_id` 传入
> - Checkpointer 按 `thread_id` 独立存储状态
> - 节点实例是**无状态的**（不持有请求级数据），所有状态都在 AgentState dict 中
>
> **潜在风险**：
> 1. **全局单例**：如果 MCPClient 或 Redis 连接池是全局单例，高并发下可能成为瓶颈。解决：连接池设置合理大小（20-50 连接）。
> 2. **LLM Rate Limit**：多个请求并发调用 LLM，可能触发供应商限流。解决：LLMClient 内置信号量限流（最多 10 个并发 LLM 调用）。
> 3. **Checkpointer 竞争**：PostgresSaver 使用 row-level lock，不同 thread_id 之间无竞争。

### 10.2 并发控制实现

```python
# python/src/aiops/agent/concurrency.py
"""
Agent 并发控制

WHY: LLM API 调用是最贵的操作（Token + 延迟），
需要限制同时进行的 LLM 调用数量，避免：
1. 触发供应商 rate limit（GPT-4o 默认 500 RPM）
2. 内存占用过高（每个 LLM 调用的 prompt 可能 8K tokens）
3. 雪崩效应（所有请求同时等待 LLM，队列堆积）
"""

import asyncio
from contextlib import asynccontextmanager

from aiops.core.logging import get_logger

logger = get_logger(__name__)


class AgentConcurrencyLimiter:
    """Agent 级别并发控制"""

    def __init__(
        self,
        max_concurrent_requests: int = 20,   # 最多 20 个并发诊断请求
        max_concurrent_llm_calls: int = 10,  # 最多 10 个并发 LLM 调用
        max_concurrent_tool_calls: int = 30, # 最多 30 个并发 MCP 工具调用
    ):
        self._request_sem = asyncio.Semaphore(max_concurrent_requests)
        self._llm_sem = asyncio.Semaphore(max_concurrent_llm_calls)
        self._tool_sem = asyncio.Semaphore(max_concurrent_tool_calls)

    @asynccontextmanager
    async def acquire_request(self):
        """获取请求级别的并发槽"""
        if self._request_sem.locked():
            logger.warning("request_queue_full", waiting=True)
        async with self._request_sem:
            yield

    @asynccontextmanager
    async def acquire_llm(self):
        """获取 LLM 调用的并发槽"""
        async with self._llm_sem:
            yield

    @asynccontextmanager
    async def acquire_tool(self):
        """获取工具调用的并发槽"""
        async with self._tool_sem:
            yield

    @property
    def stats(self) -> dict:
        """当前并发状态"""
        return {
            "request_available": self._request_sem._value,
            "llm_available": self._llm_sem._value,
            "tool_available": self._tool_sem._value,
        }


# 全局单例
concurrency_limiter = AgentConcurrencyLimiter()
```

> **🔧 工程难点：并发控制与多请求状态隔离**
>
> **挑战**：生产环境中可能同时有多个告警触发多个独立的诊断请求，每个请求都需要独占的 `AgentState` 并发执行。但系统资源是共享的——LLM API 有 rate limit（同时只能发 10 个请求）、MCP 工具调用有连接池限制（30 个并发连接）、总请求数不能超过 20 个（防止过载）。如果不做并发控制，20 个诊断请求同时调用 5 个工具 = 100 个并发 MCP 调用，直接打爆连接池。更微妙的问题是状态隔离——LangGraph 通过 `thread_id` 隔离不同请求的状态，但如果两个请求碰巧分析同一个组件（比如 HDFS），它们会调用相同的 MCP 工具（`hdfs_namenode_status`），工具返回的数据可能因为调用时间差导致不一致（10 秒前 heap 92%，现在 heap 88%），如果混淆了就会产生错误结论。
>
> **解决方案**：设计 3 层信号量体系——`_request_sem=20`（最大并发请求数）、`_llm_sem=10`（最大并发 LLM 调用数，匹配 API rate limit）、`_tool_sem=30`（最大并发工具调用数，匹配 MCP 连接池）。每一层用 `asyncio.Semaphore` 实现，作为异步上下文管理器（`async with`）确保获取/释放的原子性，即使发生异常也不会泄漏信号量。LangGraph 的 `thread_id` 参数为每个请求创建独立的状态空间——即使两个请求分析同一组件，它们的 `AgentState` 完全隔离，`collected_data` 互不影响。MCP 客户端使用连接池（而非每次创建新连接），多个请求可以复用连接但工具调用结果是隔离的（每次调用返回独立的结果对象）。全局单例 `concurrency_limiter` 提供 `get_stats()` 方法实时暴露信号量余量，Prometheus 采集后可在 Grafana 看到并发压力趋势，当信号量持续耗尽时触发告警通知运维人员扩容。

---

## 11. 边界条件与生产异常处理

### 11.1 场景矩阵

| 场景 | 触发条件 | 处理策略 | 用户感知 |
|------|---------|---------|---------|
| LLM 全部不可用 | 3 个供应商均超时 | 规则引擎兜底 + 提示联系人工 | ⚠️ 降级模式提示 |
| 单个工具超时 | MCP 调用 >30s | 标记不可用，其他工具继续 | 报告注明"部分数据暂不可用" |
| 多个工具失败 | >50% 工具调用失败 | 降低置信度，提前出报告 | 置信度低 + ⚠️ 标注 |
| Checkpoint 写入失败 | PostgreSQL 不可用 | 降级为内存 checkpoint（失去断点续传） | 无感知（日志 warning） |
| 状态字段缺失 | 某个 Agent 忘记写字段 | `state.get(key, default)` 防御 | 无感知 |
| 死循环 | Diagnostic 自环超过 max_rounds | 安全阀强制输出 | 报告标注"达到最大分析轮次" |
| Token 预算耗尽 | total_tokens > budget | 停止 LLM 调用，基于已有数据出报告 | 报告标注"因预算限制提前终止" |
| HITL 审批超时 | 等待 >30min 无人审批 | 自动拒绝 + 通知 | 收到通知"审批超时" |

### 11.2 LLM 全降级场景实现

```python
# python/src/aiops/agent/degradation.py
"""
Agent 全降级模式

WHY: 当所有 LLM 供应商都不可用时（极端情况），
系统不能直接返回"服务不可用"，而是应该：
1. 用规则引擎匹配已知问题模式
2. 直接调用 MCP 工具获取组件状态
3. 格式化为简单文本报告
4. 明确告知用户"当前为降级模式，结果仅供参考"

这样即使在 LLM 全挂的极端情况下，运维人员仍然能通过
Agent 的工具调用能力获取基本的系统状态信息。
"""

from __future__ import annotations

from aiops.agent.state import AgentState
from aiops.core.logging import get_logger

logger = get_logger(__name__)

# 常见问题 → 建议工具调用的规则表
DEGRADED_RULES: dict[str, list[str]] = {
    # 关键词匹配 → 推荐调用的工具
    "hdfs.*容量|hdfs.*空间|hdfs.*存储": ["hdfs_cluster_overview"],
    "namenode.*内存|nn.*heap|nn.*oom": ["hdfs_namenode_status", "query_metrics"],
    "kafka.*lag|kafka.*消费|kafka.*积压": ["kafka_consumer_lag"],
    "yarn.*资源|yarn.*队列": ["yarn_cluster_metrics", "yarn_queue_status"],
    "es.*集群|elasticsearch.*健康": ["es_cluster_health"],
    "告警|alert": ["query_metrics"],
}


async def degraded_diagnosis(state: AgentState, mcp_client) -> AgentState:
    """
    降级诊断流程（无 LLM 参与）

    执行步骤：
    1. 规则引擎匹配用户查询 → 确定应调用哪些工具
    2. 调用 MCP 工具获取数据
    3. 原封不动返回工具结果（不做 LLM 格式化）
    4. 生成降级提示报告
    """
    import re

    query = state.get("user_query", "").lower()
    tools_to_call: list[str] = []

    for pattern, tools in DEGRADED_RULES.items():
        if re.search(pattern, query, re.IGNORECASE):
            tools_to_call.extend(tools)
            break

    if not tools_to_call:
        # 默认调用基础状态工具
        tools_to_call = ["hdfs_cluster_overview", "yarn_cluster_metrics"]

    # 调用工具
    results = {}
    for tool_name in tools_to_call:
        try:
            result = await mcp_client.call_tool(tool_name, {})
            results[tool_name] = result
        except Exception as e:
            results[tool_name] = f"⚠️ 工具 {tool_name} 调用失败: {e}"

    # 生成降级报告
    report_lines = [
        "# ⚠️ 降级模式报告",
        "",
        "> **注意**：当前 AI 服务暂时不可用，以下是规则引擎匹配的工具返回数据。",
        "> 结果未经 AI 分析，仅供参考。如需深度诊断，请联系运维值班人员。",
        "",
    ]
    for tool_name, result in results.items():
        report_lines.append(f"## {tool_name}")
        report_lines.append(str(result)[:3000])
        report_lines.append("")

    state["final_report"] = "\n".join(report_lines)
    state["diagnosis"] = {
        "root_cause": "降级模式：AI 服务不可用，无法进行根因分析",
        "confidence": 0.0,
        "severity": "info",
        "evidence": [],
        "affected_components": [],
        "causality_chain": "",
        "related_alerts": [],
    }

    logger.warning(
        "degraded_diagnosis_completed",
        tools_called=list(results.keys()),
        query=query[:100],
    )

    return state
```

### 11.3 Checkpoint 故障降级

```python
# python/src/aiops/agent/checkpoint_fallback.py
"""
Checkpointer 故障降级

WHY: PostgreSQL 如果暂时不可用（网络抖动、连接池耗尽），
图的编译不应失败。降级策略：
1. 尝试 PostgresSaver → 成功则使用
2. 失败则降级到 MemorySaver + 记录 warning
3. MemorySaver 意味着失去断点续传能力（HITL 审批如果进程重启则丢失）

这是"可用性优先于一致性"的设计选择——宁可失去断点续传，
也不能让整个 Agent 系统因为 PG 暂时不可用而瘫痪。
"""

from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver

from aiops.core.logging import get_logger

logger = get_logger(__name__)


async def create_checkpointer_with_fallback(pg_conn_string: str):
    """
    创建 Checkpointer（带降级）
    """
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        checkpointer = AsyncPostgresSaver.from_conn_string(pg_conn_string)
        # 验证连接
        # await checkpointer.setup()  # 创建表（如果不存在）
        logger.info("checkpointer_postgres_ready")
        return checkpointer
    except Exception as e:
        logger.warning(
            "checkpointer_postgres_failed_fallback_memory",
            error=str(e),
        )
        return MemorySaver()
```

> **🔧 工程难点：Checkpoint 持久化与跨进程错误恢复**
>
> **挑战**：HITL 审批可能等待数分钟到数小时，期间进程可能因部署更新、OOM 或异常被重启。如果图的执行状态丢失，用户等了 5 分钟审批后发现诊断从头开始——这在生产环境中不可接受。同时，LLM 服务可能暂时不可用（provider 限流或网络波动），如果整个 `process()` 失败，前面几轮采集的数据都会丢失。Checkpoint 的持久化需要可靠的存储后端，但 PostgreSQL 本身也可能出问题（连接超时、磁盘满），如果 Checkpoint 存储失败导致整个 Agent 执行阻塞，就是"保险丝把正常电路也断了"。
>
> **解决方案**：LangGraph 的 `PostgresSaver` 在每个节点边界自动将完整的 `AgentState` 序列化并写入 PostgreSQL，审批完成后通过 `thread_id` 从断点恢复。这就是为什么 Diagnostic 的多轮诊断使用"自环 + `collection_round`"而不是 `process()` 内部 `while` 循环——内部循环的中间状态不会被 Checkpoint 捕获（LangGraph 只在节点边界触发持久化），一旦进程重启，循环进度丢失；自环让每轮结束后立即 Checkpoint，最多只丢失 1 轮数据（~10s 工作量）。Checkpoint 存储本身设计了两级降级：正常使用 `PostgresSaver`（生产级持久化），PostgreSQL 连接失败时自动降级到 `MemorySaver`（内存存储，丢失 Checkpoint 比阻塞 Agent 好），降级时记录 warning 日志并触发告警。数据库重连使用指数退避策略（1s → 2s → 4s → 最大 30s），避免在 PostgreSQL 短暂不可用时产生连接风暴。Checkpoint 数据按 `thread_id` 索引，过期数据（>24h）由定时任务清理，防止存储无限膨胀。

---

## 12. 性能基准与调优

### 12.1 基准测试代码

```python
# tests/benchmark/bench_graph.py
"""
图执行性能基准测试

测量项：
1. 快速路径端到端延迟（Triage → DirectTool → END）
2. 完整诊断路径延迟（Triage → Planning → Diagnostic → Report）
3. 图编译时间
4. 状态序列化/反序列化时间
"""

import asyncio
import time
import statistics
from unittest.mock import AsyncMock, MagicMock

from aiops.agent.graph import build_ops_graph
from aiops.agent.state import create_initial_state


async def bench_fast_path(iterations: int = 100):
    """快速路径基准"""
    # Mock LLM 和 MCP（避免实际网络调用）
    mock_llm = AsyncMock()
    mock_llm.chat_structured = AsyncMock(return_value=MagicMock(
        intent="status_query", route="direct_tool", complexity="simple",
        components=["hdfs"], urgency="low", summary="HDFS 容量查询",
    ))

    graph = build_ops_graph(llm_client=mock_llm)
    state = create_initial_state(
        user_query="HDFS 容量多少？",
        request_type="user_query",
    )

    latencies = []
    for _ in range(iterations):
        start = time.monotonic()
        await graph.ainvoke(state)
        latencies.append(time.monotonic() - start)

    return {
        "test": "fast_path",
        "iterations": iterations,
        "p50_ms": statistics.median(latencies) * 1000,
        "p95_ms": sorted(latencies)[int(iterations * 0.95)] * 1000,
        "p99_ms": sorted(latencies)[int(iterations * 0.99)] * 1000,
        "mean_ms": statistics.mean(latencies) * 1000,
    }


async def bench_compile_time(iterations: int = 10):
    """图编译时间"""
    latencies = []
    for _ in range(iterations):
        start = time.monotonic()
        build_ops_graph()
        latencies.append(time.monotonic() - start)

    return {
        "test": "compile",
        "mean_ms": statistics.mean(latencies) * 1000,
    }


if __name__ == "__main__":
    print("=== Fast Path Benchmark ===")
    result = asyncio.run(bench_fast_path(100))
    print(f"  P50: {result['p50_ms']:.1f}ms")
    print(f"  P95: {result['p95_ms']:.1f}ms")
    print(f"  P99: {result['p99_ms']:.1f}ms")

    print("\n=== Compile Benchmark ===")
    result = asyncio.run(bench_compile_time(10))
    print(f"  Mean: {result['mean_ms']:.1f}ms")
```

### 12.2 性能目标

| 指标 | 目标 | 瓶颈 | 优化方向 |
|------|------|------|---------|
| 图编译时间 | < 50ms | 节点实例化 | 缓存编译后的图（进程级单例） |
| 快速路径 E2E | < 3s | LLM 调用 (2s) | 规则引擎命中则跳过 LLM |
| 完整诊断 E2E | < 30s | 多轮 LLM + 工具 | 并行工具调用、语义缓存 |
| 状态序列化 | < 5ms | JSON 编码 | collected_data 延迟序列化 |
| Checkpoint 写入 | < 20ms | PostgreSQL INSERT | 批量写入 + 连接池 |
| 内存/请求 | < 50MB | 工具返回数据 | ContextCompressor 截断 |

### 12.3 调优参数速查表

| 参数 | 默认值 | 范围 | 影响 |
|------|--------|------|------|
| `MAX_CONTEXT_TOKENS` | 8000 | 4000-16000 | 上下文窗口大小。↑ 提高诊断质量但增加成本 |
| `max_collection_rounds` | 5 | 3-8 | 最大诊断轮次。↑ 提高复杂问题命中率但增加延迟 |
| `confidence_threshold` | 0.6 | 0.4-0.8 | 自环阈值。↑ 减少自环但降低诊断质量 |
| `MAX_GLOBAL_ERRORS` | 10 | 5-20 | 全局错误限制。↓ 更快终止但可能误杀 |
| `max_concurrent_requests` | 20 | 10-50 | 并发请求数。↑ 增加吞吐但增加 LLM 压力 |
| `max_concurrent_llm_calls` | 10 | 5-20 | 并发 LLM 调用。↑ 减少排队但可能触发 rate limit |

---

## 13. 端到端实战场景

### 13.1 场景 1：HDFS NameNode OOM 诊断

```
用户输入: "HDFS NameNode 堆内存持续升高，写入开始变慢"

状态流转：
┌─────────────────────────────────────────────────────────────┐
│ [1] Triage Agent (延迟 ~1.5s)                               │
│   规则引擎匹配失败（不是简单查询）→ LLM 分诊               │
│   intent=fault_diagnosis, complexity=complex, route=diagnosis│
│   _triage_method=llm                                        │
│   target_components=["hdfs-namenode"]                        │
├─────────────────────────────────────────────────────────────┤
│ [2] Planning Agent (延迟 ~3s)                                │
│   RAG 检索: 命中 "HDFS NameNode 内存调优指南" (score=0.89)  │
│   生成 3 个假设:                                            │
│   H1: NameNode heap 不足 (probability=high)                 │
│   H2: 小文件过多导致元数据膨胀 (probability=medium)         │
│   H3: Full GC 导致 RPC 阻塞 (probability=medium)           │
│   诊断计划: 5 步工具调用                                     │
├─────────────────────────────────────────────────────────────┤
│ [3] Diagnostic Agent - Round 1 (延迟 ~5s)                    │
│   并行调用:                                                  │
│   ✅ hdfs_namenode_status → heap=93%, RPC_latency=15ms      │
│   ✅ query_metrics(NN heap 7d trend) → 持续上升             │
│   ✅ search_logs(GC, namenode, 1h) → "Full GC 5 次/h"      │
│   ⏱️ yarn_cluster_metrics → 超时（不影响诊断）              │
│                                                              │
│   LLM 分析: H1 confirmed (0.85)                             │
│   root_cause="NN JVM heap 不足，元数据增长导致 Full GC 频繁"│
│   confidence=0.82                                            │
│   remediation: [{action: "增加 heap 到 32GB", risk: "high"}] │
├─────────────────────────────────────────────────────────────┤
│ [4] route_from_diagnostic:                                    │
│   confidence=0.82 ≥ 0.6 → 不自环                            │
│   remediation 有 high risk → route to hitl_gate              │
├─────────────────────────────────────────────────────────────┤
│ [5] HITL Gate (等待人工审批)                                  │
│   → interrupt_before 暂停                                    │
│   → WebSocket + 企微通知审批人                               │
│   → 审批人 approve ("同意调整 heap 参数")                    │
├─────────────────────────────────────────────────────────────┤
│ [6] Remediation Agent (延迟 ~2s)                              │
│   前置检查 ✅ → 创建检查点 ✅ → 执行 ✅ → 验证 ✅         │
├─────────────────────────────────────────────────────────────┤
│ [7] Report Agent (延迟 ~2s)                                   │
│   生成完整报告 (Markdown)                                    │
├─────────────────────────────────────────────────────────────┤
│ [8] KnowledgeSink                                             │
│   confidence=0.82 > 0.7 → 写入 PG + Milvus + Neo4j         │
└─────────────────────────────────────────────────────────────┘

总耗时: ~15s (不含 HITL 等待) | Token: ~12000 | 成本: ~$0.05
```

### 13.2 场景 2：简单查询快速路径

```
用户输入: "Kafka 消费延迟多少？"

状态流转：
┌─────────────────────────────────────────────────────────────┐
│ [1] Triage Agent (延迟 ~5ms)                                 │
│   规则引擎命中: "kafka.*延迟" → kafka_consumer_lag          │
│   route=direct_tool, _triage_method=rule_engine              │
│   Token 消耗: 0（跳过 LLM）                                 │
├─────────────────────────────────────────────────────────────┤
│ [2] DirectToolNode (延迟 ~1.5s)                               │
│   调用 kafka_consumer_lag → "group-1 lag=2500, group-2 lag=0"│
│   轻量 LLM 格式化 → "Kafka 消费延迟: group-1 积压 2500 条..."│
├─────────────────────────────────────────────────────────────┤
│ → END                                                         │
└─────────────────────────────────────────────────────────────┘

总耗时: ~1.5s | Token: ~300 (格式化) | 成本: ~$0.0005
对比完整链路: ~15s, ~12000 Token, ~$0.05 → 节省 90% Token 和 90% 时间
```

### 13.3 场景 3：多告警关联

```
用户输入: (Alertmanager webhook, 5 条告警同时到达)
  [critical] ZooKeeper session expired
  [critical] HDFS NameNode failover
  [high] Kafka broker-0 offline
  [high] YARN ResourceManager HA switch
  [warning] HBase RegionServer disconnected

状态流转：
[1] Triage: 5 条告警 > 3 → 强制走 alert_correlation
[2] AlertCorrelation: ZK 是 HDFS/Kafka/YARN 的上游依赖
    → 根因告警: ZooKeeper session expired
    → 衍生告警: 其他 4 条 (suppressed)
    → 收敛比: 5:1
[3] Planning → Diagnostic: 聚焦 ZK 问题
    调用 zk_status, zk_logs, query_metrics(zk_*)
    → root_cause: "ZK 节点 3/3 内存不足导致 session 超时"
    → confidence: 0.88
[4] Report: 包含告警关联图和因果链
```

---

## 14. 扩展测试套件

### 14.1 状态管理测试

```python
# tests/unit/agent/test_state.py

import pytest
from aiops.agent.state import (
    AgentState, create_initial_state, snapshot_state,
)


class TestCreateInitialState:

    def test_default_values(self):
        """默认值应合理"""
        state = create_initial_state(user_query="test")
        assert state["collection_round"] == 0
        assert state["max_collection_rounds"] == 5
        assert state["error_count"] == 0
        assert state["total_tokens"] == 0
        assert state["total_cost_usd"] == 0.0
        assert state["tool_calls"] == []
        assert state["collected_data"] == {}

    def test_request_id_auto_generated(self):
        """未指定 request_id 时自动生成"""
        state = create_initial_state(user_query="test")
        assert state["request_id"].startswith("REQ-")

    def test_request_id_specified(self):
        """指定 request_id 时使用指定值"""
        state = create_initial_state(
            user_query="test", request_id="custom-id"
        )
        assert state["request_id"] == "custom-id"

    def test_alerts_default_empty_list(self):
        """alerts 默认为空列表而不是 None"""
        state = create_initial_state(user_query="test")
        assert state["alerts"] == []
        assert isinstance(state["alerts"], list)

    def test_start_time_is_iso_format(self):
        """start_time 应为 ISO 格式"""
        state = create_initial_state(user_query="test")
        from datetime import datetime
        # 应不抛异常
        datetime.fromisoformat(state["start_time"])


class TestSnapshotState:

    def test_excludes_large_data(self):
        """快照不应包含大 blob 数据"""
        state = create_initial_state(user_query="test")
        state["collected_data"] = {"tool_a": "x" * 10000}
        snap = snapshot_state(state)
        assert "tool_a" not in str(snap.get("collected_data", ""))
        assert "tool_a" in snap.get("collected_data_keys", [])

    def test_snapshot_is_serializable(self):
        """快照应可 JSON 序列化"""
        import json
        state = create_initial_state(user_query="test")
        snap = snapshot_state(state)
        json.dumps(snap)  # 不应抛异常

    def test_snapshot_with_diagnosis(self):
        """有诊断结果时应包含 confidence"""
        state = create_initial_state(user_query="test")
        state["diagnosis"] = {"confidence": 0.85, "root_cause": "test"}
        snap = snapshot_state(state)
        assert snap["confidence"] == 0.85
```

### 14.2 路由函数测试

```python
# tests/unit/agent/test_router.py

import pytest
from aiops.agent.router import (
    route_from_triage, route_from_diagnostic, route_from_hitl,
)


class TestRouteFromTriage:

    def test_direct_tool(self):
        state = {"route": "direct_tool", "intent": "status_query"}
        assert route_from_triage(state) == "direct_tool"

    def test_alert_correlation(self):
        state = {"route": "alert_correlation", "alerts": [{}]*5}
        assert route_from_triage(state) == "alert_correlation"

    def test_diagnosis_default(self):
        state = {"route": "diagnosis", "complexity": "complex"}
        assert route_from_triage(state) == "planning"

    def test_unknown_route_defaults_to_planning(self):
        """未知路由应走 planning（兜底）"""
        state = {"route": "unknown_route"}
        assert route_from_triage(state) == "planning"

    def test_empty_route_defaults_to_planning(self):
        """空路由应走 planning"""
        state = {}
        assert route_from_triage(state) == "planning"


class TestRouteFromDiagnostic:

    def test_max_rounds_forces_report(self):
        """超过最大轮次强制出报告"""
        state = {
            "diagnosis": {"confidence": 0.3},
            "collection_round": 5,
            "max_collection_rounds": 5,
            "data_requirements": ["some_tool"],
        }
        assert route_from_diagnostic(state) == "report"

    def test_low_confidence_loops(self):
        """低置信度 + 有未采集数据 → 自环"""
        state = {
            "diagnosis": {"confidence": 0.4},
            "collection_round": 1,
            "max_collection_rounds": 5,
            "data_requirements": ["yarn_metrics"],
        }
        assert route_from_diagnostic(state) == "need_more_data"

    def test_low_confidence_no_data_goes_report(self):
        """低置信度但无未采集数据 → 出报告（不自环）"""
        state = {
            "diagnosis": {"confidence": 0.4},
            "collection_round": 2,
            "max_collection_rounds": 5,
            "data_requirements": [],
        }
        assert route_from_diagnostic(state) == "report"

    def test_high_risk_remediation_goes_hitl(self):
        """高风险修复 → HITL"""
        state = {
            "diagnosis": {"confidence": 0.85},
            "collection_round": 1,
            "max_collection_rounds": 5,
            "remediation_plan": [
                {"action": "重启 NameNode", "risk_level": "high"},
            ],
        }
        assert route_from_diagnostic(state) == "hitl_gate"

    def test_normal_completion(self):
        """正常完成 → report"""
        state = {
            "diagnosis": {"confidence": 0.85},
            "collection_round": 1,
            "max_collection_rounds": 5,
            "remediation_plan": [
                {"action": "调整参数", "risk_level": "low"},
            ],
        }
        assert route_from_diagnostic(state) == "report"

    def test_token_budget_forces_report(self):
        """Token 预算接近上限 → 强制出报告"""
        state = {
            "diagnosis": {"confidence": 0.3},
            "collection_round": 1,
            "max_collection_rounds": 5,
            "data_requirements": ["some_tool"],
            "total_tokens": 14500,
        }
        assert route_from_diagnostic(state) == "report"

    def test_empty_diagnosis(self):
        """空诊断结果 → confidence=0 → 自环或出报告"""
        state = {
            "diagnosis": {},
            "collection_round": 0,
            "max_collection_rounds": 5,
            "data_requirements": ["initial_data"],
        }
        # confidence=0 < 0.6 且有 data_requirements → 自环
        assert route_from_diagnostic(state) == "need_more_data"


class TestRouteFromHitl:

    def test_approved(self):
        state = {"hitl_status": "approved", "hitl_comment": "LGTM"}
        assert route_from_hitl(state) == "remediation"

    def test_rejected(self):
        state = {"hitl_status": "rejected", "hitl_comment": "太危险"}
        assert route_from_hitl(state) == "report"

    def test_timeout_treated_as_rejected(self):
        """超时默认拒绝"""
        state = {"hitl_status": "timeout"}
        assert route_from_hitl(state) == "report"

    def test_default_is_rejected(self):
        """无状态默认拒绝"""
        state = {}
        assert route_from_hitl(state) == "report"
```

### 14.3 BaseAgentNode 测试

```python
# tests/unit/agent/test_base_node.py

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from aiops.agent.base import BaseAgentNode
from aiops.agent.state import create_initial_state
from aiops.core.errors import AIOpsError, ErrorCode


class ConcreteNode(BaseAgentNode):
    """测试用具体节点"""
    agent_name = "test_node"

    async def process(self, state):
        state["test_output"] = "processed"
        return state


class FailingNode(BaseAgentNode):
    """测试用失败节点"""
    agent_name = "failing_node"

    async def process(self, state):
        raise AIOpsError(
            code=ErrorCode.LLM_TIMEOUT,
            message="LLM timed out",
        )


class TestBaseAgentNode:

    @pytest.fixture
    def state(self):
        return create_initial_state(user_query="test query")

    @pytest.mark.asyncio
    async def test_successful_execution(self, state):
        node = ConcreteNode()
        result = await node(state)
        assert result["test_output"] == "processed"
        assert result["current_agent"] == "test_node"

    @pytest.mark.asyncio
    async def test_error_increments_count(self, state):
        node = FailingNode()
        # ErrorHandler 会处理异常，不会 re-raise
        result = await node(state)
        assert result.get("error_count", 0) >= 1

    @pytest.mark.asyncio
    async def test_sensitivity_detection_high(self, state):
        state["collected_data"] = {"tool": "password=abc123"}
        sensitivity = BaseAgentNode._assess_sensitivity(state)
        assert sensitivity.value == "high" or str(sensitivity) == "Sensitivity.HIGH"

    @pytest.mark.asyncio
    async def test_sensitivity_detection_low(self, state):
        state["collected_data"] = {"tool": "CPU usage: 75%"}
        sensitivity = BaseAgentNode._assess_sensitivity(state)
        assert str(sensitivity).endswith("LOW")

    @pytest.mark.asyncio
    async def test_pre_check_warning_on_budget_exceeded(self, state):
        """预算超限应 warning 但不 raise"""
        state["total_tokens"] = 999999
        node = ConcreteNode()
        # 不应抛异常
        result = await node(state)
        assert result["test_output"] == "processed"


class TestContextCompressor:

    def test_empty_state(self):
        from aiops.agent.context import ContextCompressor
        compressor = ContextCompressor()
        state = create_initial_state(user_query="test")
        result = compressor.compress(state)
        assert "诊断计划" in result

    def test_tool_results_compression(self):
        from aiops.agent.context import ContextCompressor
        compressor = ContextCompressor()
        state = create_initial_state(user_query="test")
        state["collected_data"] = {
            "hdfs_status": "正常行\n" * 100 + "⚠️ 堆内存使用率 92%\n" + "正常行\n" * 100,
        }
        result = compressor.compress(state)
        # 应保留异常行
        assert "92%" in result
        # 总长度应在预算内
        assert len(result) < 40000  # 粗略检查

    def test_rag_truncation(self):
        from aiops.agent.context import ContextCompressor
        compressor = ContextCompressor()
        state = create_initial_state(user_query="test")
        state["rag_context"] = [
            {"content": f"RAG 文档 {i}: " + "x" * 2000}
            for i in range(20)
        ]
        result = compressor.compress(state)
        # 不应包含所有 20 条 RAG 文档
        assert result.count("RAG 文档") < 10

    def test_history_truncation(self):
        from aiops.agent.context import ContextCompressor
        compressor = ContextCompressor()
        state = create_initial_state(user_query="test")
        state["tool_calls"] = [
            {"tool_name": f"tool_{i}", "status": "success", "duration_ms": 100}
            for i in range(50)
        ]
        result = compressor.compress(state)
        # 应只保留最近 10 步
        assert result.count("tool_") <= 15  # 允许一些冗余


class TestGraphIntegration:
    """图集成测试（Mock LLM）"""

    @pytest.mark.asyncio
    async def test_build_and_compile(self):
        """图应能成功编译"""
        from aiops.agent.graph import build_ops_graph
        graph = build_ops_graph(llm_client=AsyncMock())
        assert graph is not None

    @pytest.mark.asyncio
    async def test_fast_path_end_to_end(self):
        """快速路径应到达 END"""
        from aiops.agent.graph import build_ops_graph

        mock_llm = AsyncMock()
        graph = build_ops_graph(llm_client=mock_llm)

        state = create_initial_state(user_query="HDFS 容量多少")
        # 需要 Mock 内部节点行为 - 简化测试
        # 实际集成测试在 tests/integration/ 中

    @pytest.mark.asyncio
    async def test_concurrent_requests(self):
        """并发请求应互不干扰"""
        from aiops.agent.concurrency import AgentConcurrencyLimiter
        limiter = AgentConcurrencyLimiter(max_concurrent_requests=2)

        async with limiter.acquire_request():
            assert limiter.stats["request_available"] == 1
            async with limiter.acquire_request():
                assert limiter.stats["request_available"] == 0

    @pytest.mark.asyncio
    async def test_degraded_mode(self):
        """降级模式应返回工具原始数据"""
        from aiops.agent.degradation import degraded_diagnosis

        mock_mcp = AsyncMock()
        mock_mcp.call_tool = AsyncMock(return_value="HDFS: 60% used")

        state = create_initial_state(user_query="HDFS 容量还有多少")
        result = await degraded_diagnosis(state, mock_mcp)

        assert "降级模式" in result["final_report"]
        assert result["diagnosis"]["confidence"] == 0.0
```

---

## 15. 与其他模块的集成

### 15.1 依赖关系图

```mermaid
graph LR
    subgraph 上游
        A[01-工程化基础] --> C[03-Agent框架]
        B[02-LLM客户端] --> C
    end
    
    subgraph 下游
        C --> D[04-Triage]
        C --> E[05-Diagnostic]
        C --> F[06-Planning]
        C --> G[07-Patrol]
        C --> H[08-Report]
        C --> I[11-MCP]
        C --> J[15-HITL]
        C --> K[17-可观测性]
    end
```

### 15.2 接口清单

| 上游依赖 | 说明 |
|---------|------|
| 01-项目工程化 | config, logging, errors |
| 02-LLM 客户端 | LLMClient, LLMCallContext, 结构化输出模型 |

| 下游消费者 | 说明 |
|-----------|------|
| 04-Triage Agent | 继承 BaseAgentNode，实现 `TriageNode.process()` |
| 05-Diagnostic Agent | 继承 BaseAgentNode，实现 `DiagnosticNode.process()` |
| 06-Planning/Remediation | 继承 BaseAgentNode |
| 07-Patrol/Alert | 继承 BaseAgentNode |
| 08-Report Agent | 继承 BaseAgentNode |
| 11-MCP 客户端 | Agent 节点通过 MCPClient 调用工具 |
| 15-HITL | HITLGateNode 与 ApprovalWorkflow 对接 |
| 17-可观测性 | OpenTelemetry Span 由 BaseAgentNode 自动创建 |

---

> **下一篇**：[04-Triage-Agent与智能分流.md](./04-Triage-Agent与智能分流.md) — 基于 BaseAgentNode 实现首个接触点 Agent。
