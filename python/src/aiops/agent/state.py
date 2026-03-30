"""
AgentState — LangGraph 共享状态

这是整个 Agent 系统的"数据总线"——所有 Agent 节点通过读写这个 TypedDict 进行协作。

设计原则：
1. 每个字段都有明确的写入者和读取者（参考所有权矩阵）
2. 使用 total=False 使所有字段可选——不同阶段渐进式填充
3. 追加式字段用 dict.update()，递增字段只允许 += 

WHY TypedDict 而不是 Pydantic：
- LangGraph 的 StateGraph 要求状态是 dict-like 的
- state["field"] = value 直接更新，不需要 model_copy()
- total=False 让所有字段可选，适合渐进式填充
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal, TypedDict


# ──────────────────────────────────────────────────
# 嵌套类型定义
# ──────────────────────────────────────────────────

class ToolCallRecord(TypedDict):
    """工具调用记录——审计日志用，追踪每一次工具调用的完整信息."""

    tool_name: str
    parameters: dict[str, Any]
    result: str              # 截断到 5000 字符，避免 state 膨胀
    duration_ms: int
    risk_level: str          # "none" | "low" | "medium" | "high" | "critical"
    timestamp: str           # ISO 格式
    status: Literal["success", "error", "timeout"]


class DiagnosisResult(TypedDict):
    """诊断结论——Diagnostic Agent 的核心输出."""

    root_cause: str          # 根因描述（必须引用具体数据，防幻觉）
    confidence: float        # 0.0-1.0，< 0.6 触发自环补充采集
    severity: Literal["critical", "high", "medium", "low", "info"]
    evidence: list[str]      # 证据列表（每条引用具体工具返回的数据）
    affected_components: list[str]
    causality_chain: str     # "A→B→C" 格式的因果链
    related_alerts: list[str]


class RemediationStep(TypedDict):
    """修复步骤——每条建议标注风险级别和回滚方案."""

    step_number: int
    action: str
    risk_level: Literal["none", "low", "medium", "high", "critical"]
    requires_approval: bool   # high/critical 强制为 True（代码兜底）
    rollback_action: str | None
    estimated_impact: str


# ──────────────────────────────────────────────────
# 核心状态定义
# ──────────────────────────────────────────────────

class AgentState(TypedDict, total=False):
    """
    Agent 共享状态——LangGraph StateGraph 的数据结构.

    字段分区：
    ┌─────────────────────────────────────────────┐
    │ 输入区 — API 入口写入，全程只读              │
    │ 分诊区 — Triage Agent 写入                  │
    │ 规划区 — Planning Agent 写入                │
    │ 数据区 — Diagnostic Agent 写入              │
    │ RAG 区 — Planning/Diagnostic 写入           │
    │ 诊断区 — Diagnostic Agent 写入              │
    │ HITL 区 — HITL Gate 写入                    │
    │ 输出区 — Report Agent 写入                  │
    │ 元信息区 — 框架自动维护                      │
    └─────────────────────────────────────────────┘
    """

    # === 输入区（只读）===
    # 写入者: API 入口
    # 读取者: 所有节点
    request_id: str                    # 唯一标识，贯穿 OTel Trace
    request_type: str                  # "user_query" | "alert" | "patrol"
    user_query: str                    # 原始输入（AlertCorrelation 可覆盖）
    user_id: str                       # RBAC 权限控制
    cluster_id: str                    # 多集群数据隔离
    alerts: list[dict[str, Any]]       # 关联告警（alert 类型请求）

    # === 分诊区 ===
    # 写入者: Triage Agent
    # 读取者: route_from_triage(), Planning Agent
    intent: str                        # 5 分类意图
    complexity: str                    # "simple" | "moderate" | "complex"
    route: str                         # "direct_tool" | "diagnosis" | "alert_correlation"
    urgency: str                       # "critical" | "high" | "medium" | "low"
    target_components: list[str]       # 涉及的大数据组件

    # === 规划区 ===
    # 写入者: Planning Agent
    # 读取者: Diagnostic Agent
    task_plan: list[dict[str, Any]]    # 诊断步骤计划
    data_requirements: list[str]       # 需要采集的数据（空=诊断完成）
    hypotheses: list[dict[str, Any]]   # 候选根因假设

    # === 数据采集区 ===
    # 写入者: Diagnostic Agent
    # 读取者: Diagnostic, Report, 路由函数
    tool_calls: list[ToolCallRecord]   # 审计日志（追加式，不覆盖）
    collected_data: dict[str, Any]     # 采集数据 {tool_name: result}（dict merge 式）
    collection_round: int              # 当前轮次（递增，用于安全阀检查）
    max_collection_rounds: int         # 最大轮次（默认 5，可配置）

    # === RAG 上下文区 ===
    # 写入者: Planning Agent
    # 读取者: Diagnostic Agent, Report Agent
    rag_context: list[dict[str, Any]]  # 检索到的知识库文档片段
    similar_cases: list[dict[str, Any]]  # 相似历史案例

    # === 诊断结果区 ===
    # 写入者: Diagnostic Agent
    # 读取者: HITL Gate, Report, route_from_diagnostic()
    diagnosis: DiagnosisResult
    remediation_plan: list[RemediationStep]

    # === HITL 区 ===
    # 写入者: HITL Gate
    # 读取者: route_from_hitl(), Remediation Agent
    hitl_required: bool
    hitl_status: str                   # "pending" | "approved" | "rejected"
    hitl_comment: str

    # === 输出区 ===
    # 写入者: Report Agent
    # 读取者: API 层返回
    final_report: str                  # Markdown 格式的最终报告
    knowledge_entry: dict[str, Any]    # 待沉淀的知识条目

    # === 元信息区（框架自动维护）===
    messages: list[Any]                # LangGraph 消息历史
    current_agent: str                 # 当前活跃 Agent（调试用）
    error_count: int                   # 累计错误次数（安全阀检查）
    start_time: str                    # ISO 格式开始时间
    total_tokens: int                  # 累计 Token 消耗
    total_cost_usd: float              # 累计 LLM 成本
    _force_provider: str | None        # 降级时强制指定供应商
    _simplify_prompt: bool             # 降级标记：简化 Prompt
    _direct_tool_name: str             # 快速路径：工具名
    _direct_tool_params: dict[str, Any]  # 快速路径：工具参数
    _triage_method: str                # 分诊方式 (rule_engine|llm|fallback)


# ──────────────────────────────────────────────────
# 状态工厂
# ──────────────────────────────────────────────────

def create_initial_state(
    user_query: str,
    request_type: str = "user_query",
    user_id: str = "anonymous",
    cluster_id: str = "default",
    alerts: list[dict[str, Any]] | None = None,
    request_id: str | None = None,
) -> AgentState:
    """
    创建 Agent 初始状态.

    WHY 工厂函数：
    - 集中管理初始值，避免各入口重复初始化
    - 所有计数器归零、所有列表为空列表（不是 None）
    - 下游 Agent 不需要到处写 state.get("field", []) 的防御代码
    """
    return AgentState(
        # 输入区
        request_id=request_id or f"REQ-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}",
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
        start_time=datetime.now(UTC).isoformat(),
        total_tokens=0,
        total_cost_usd=0.0,
    )


def snapshot_state(state: AgentState) -> dict[str, Any]:
    """
    状态快照（用于调试和审计日志）.

    WHY 只保留关键字段：
    - 生产环境 collected_data 可能很大（多个工具返回结果）
    - 完整 dump 到日志会导致 ES 索引膨胀
    - 快照只保留"能定位问题"的关键信息
    """
    return {
        "request_id": state.get("request_id"),
        "current_agent": state.get("current_agent"),
        "route": state.get("route"),
        "intent": state.get("intent"),
        "collection_round": state.get("collection_round"),
        "confidence": (state.get("diagnosis") or {}).get("confidence"),
        "tool_calls_count": len(state.get("tool_calls", [])),
        "collected_data_keys": list(state.get("collected_data", {}).keys()),
        "error_count": state.get("error_count"),
        "total_tokens": state.get("total_tokens"),
    }
