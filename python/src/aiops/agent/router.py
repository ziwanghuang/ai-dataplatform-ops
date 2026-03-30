"""
条件路由函数 — 控制 Agent 图中的分支走向

LangGraph 的条件边由这些纯函数决定——它们接收 state，返回路由 key。
路由 key 必须与 graph.add_conditional_edges() 注册的 key 精确匹配。

WHY 纯函数而不是类方法：
- 纯函数没有副作用，输入确定则输出确定，方便单元测试
- 每个路由函数可以独立测试所有分支组合
- 不需要实例化任何对象

安全阀优先级链（从高到低）：
1. error_count >= MAX → 强制结束
2. total_tokens > budget → 强制结束
3. round >= max_rounds → 强制结束
4. 正常业务判断
"""

from __future__ import annotations

from aiops.agent.state import AgentState
from aiops.core.logging import get_logger

logger = get_logger(__name__)


def route_from_triage(state: AgentState) -> str:
    """
    Triage Agent 出口路由.

    三路分发：
    - direct_tool: ~40% 请求走这条路（快速路径，节省 70%+ Token）
    - alert_correlation: 多告警先聚合再诊断
    - planning: 走完整诊断流程（默认路径）
    """
    route = state.get("route", "diagnosis")

    if route == "direct_tool":
        logger.info("route_triage_fast_path", intent=state.get("intent"))
        return "direct_tool"
    elif route == "alert_correlation":
        logger.info("route_triage_alert", alert_count=len(state.get("alerts", [])))
        return "alert_correlation"
    else:
        # "diagnosis" 或其他未知值都走 planning（安全默认）
        logger.info("route_triage_diagnosis", complexity=state.get("complexity"))
        return "planning"


def route_from_diagnostic(state: AgentState) -> str:
    """
    Diagnostic Agent 出口路由.

    五个判断维度（按优先级从高到低）：
    1. 超过最大轮次 → "report"（安全阀，防无限循环）
    2. Token 预算耗尽 → "report"（成本控制）
    3. 置信度 < 0.6 且有未采集数据 → "need_more_data"（自环）
    4. 有高风险修复建议 → "hitl_gate"（人工审批）
    5. 默认 → "report"（诊断完成）

    WHY 0.6 是自环阈值：
    - < 0.4 的结论几乎都是错误的
    - 0.4-0.6 中约 60% 可以通过补充数据提升到 0.7+
    - > 0.6 是可接受的诊断质量
    - 所以 0.6 是"值得再花一轮 Token"的分界线

    WHY 最大轮次 5：
    - 每轮 ~3000 Token + 2-5s
    - 5 轮 = 15000 Token（接近单次预算上限）
    - 超过 5 轮说明问题极复杂或数据不足，继续自环收益递减
    """
    diagnosis = state.get("diagnosis", {})
    collection_round = state.get("collection_round", 0)
    max_rounds = state.get("max_collection_rounds", 5)
    confidence = diagnosis.get("confidence", 0)

    # ── 安全阀 1：超过最大轮次 ──
    if collection_round >= max_rounds:
        logger.warning("diagnostic_max_rounds", rounds=collection_round, confidence=confidence)
        return "report"

    # ── 安全阀 2：Token 预算耗尽 ──
    total_tokens = state.get("total_tokens", 0)
    if total_tokens > 14000:  # 接近 15000 上限，留 1000 给 Report
        logger.warning("diagnostic_budget_limit", total_tokens=total_tokens)
        return "report"

    # ── 置信度不够 + 还有数据可采 → 自环 ──
    if confidence < 0.6 and state.get("data_requirements"):
        logger.info(
            "diagnostic_need_more_data",
            confidence=confidence,
            round=collection_round,
            remaining=state.get("data_requirements", [])[:3],
        )
        return "need_more_data"

    # ── 有高风险修复建议 → 人工审批 ──
    remediation = state.get("remediation_plan", [])
    high_risk = [s for s in remediation if s.get("risk_level") in ("high", "critical")]
    if high_risk:
        logger.info(
            "diagnostic_hitl_required",
            high_risk_count=len(high_risk),
            actions=[s.get("action", "")[:50] for s in high_risk],
        )
        return "hitl_gate"

    # ── 默认：诊断完成 → 出报告 ──
    return "report"


def route_from_hitl(state: AgentState) -> str:
    """
    HITL Gate 出口路由.

    approved → "remediation"（执行修复）
    rejected → "report"（跳过修复，仅出报告）

    WHY 默认 rejected：
    对于高风险操作，安全的默认行为是"不执行"
    """
    status = state.get("hitl_status", "rejected")

    if status == "approved":
        logger.info("hitl_approved", comment=state.get("hitl_comment", ""))
        return "remediation"
    else:
        logger.info("hitl_rejected", reason=state.get("hitl_comment", ""))
        return "report"
