"""
HITL Gate — 人机协作审批门控

当 Diagnostic Agent 生成包含高风险修复建议（risk=high/critical）时，
流程在此处暂停，等待人工审批。

审批流程：
1. HITLGate 检查 remediation_plan 中是否有高风险操作
2. 如果有 → 设置 hitl_required=True + hitl_status="pending"
3. LangGraph interrupt_before 暂停图执行，state 写入 Checkpoint
4. 外部（企微 Bot / WebSocket / API）推送审批消息给值班人员
5. 值班人员点击"同意"或"拒绝"
6. API 层带着审批结果恢复图执行（invoke with same thread_id）
7. route_from_hitl() 根据审批结果路由到 remediation 或 report

WHY 超时自动拒绝（fail-safe）而不是自动通过：
对于高风险操作，安全的默认行为是"不执行"而非"执行"。
宁可一次误拒（Agent 重试或人工处理），也不要一次误批（服务中断）。

补全内容：
- 接入真实 HITLGate 实例（不再硬编码 approved）
- 环境感知：DEV 自动审批，PRODUCTION 走真实审批
- 审批超时 fail-safe
"""

from __future__ import annotations

from typing import Any

from aiops.agent.base import BaseAgentNode
from aiops.agent.state import AgentState
from aiops.core.config import get_settings
from aiops.core.logging import get_logger
from aiops.hitl.gate import ApprovalStatus, HITLGate
from aiops.llm.types import TaskType

logger = get_logger(__name__)


class HITLGateNode(BaseAgentNode):
    """
    HITL 门控节点 — 判断是否需要人工审批并管理审批流程.

    双模式：
    - DEV 环境：自动批准（通过 HITLGate 的 auto_approve_dev）
    - PRODUCTION 环境：创建审批请求，轮询等待结果

    WHY 接入 HITLGate 而不是直接写 state：
    - HITLGate 提供完整的审批生命周期（创建/审批/拒绝/超时）
    - 审批记录可追溯（审计合规）
    - 支持双人审批（critical 操作）
    - 统一的审批接口，可对接企微/飞书/WebSocket
    """

    agent_name = "hitl_gate"
    task_type = TaskType.REMEDIATION  # 复用 remediation 的 token 预算

    def __init__(self, llm_client: Any | None = None, **kwargs: Any) -> None:
        super().__init__(llm_client, **kwargs)
        settings = get_settings()
        is_dev = settings.env.value == "dev"
        self._gate = HITLGate(auto_approve_dev=is_dev)
        self._timeout = settings.hitl.timeout_seconds

    async def process(self, state: AgentState) -> AgentState:
        """
        HITL 门控主流程.

        1. 检查 remediation_plan 中是否有高风险操作
        2. 无高风险 → 直接通过
        3. 有高风险 → 创建审批请求 → 等待结果
        4. 已有外部审批结果 → 直接使用
        """
        plan = state.get("remediation_plan", [])
        high_risk = [s for s in plan if s.get("risk_level") in ("high", "critical")]

        if not high_risk:
            # 没有高风险操作，自动通过
            state["hitl_required"] = False
            state["hitl_status"] = "approved"
            state["hitl_comment"] = "No high-risk operations"
            logger.info("hitl_gate_auto_approved", reason="no high-risk steps")
            return state

        state["hitl_required"] = True

        # 检查是否已有外部审批结果（由 API 层注入）
        # WHY 先检查：LangGraph resume 时 state 已包含审批结果
        if state.get("hitl_status") in ("approved", "rejected"):
            logger.info(
                "hitl_gate_external_result",
                status=state["hitl_status"],
                comment=state.get("hitl_comment", ""),
            )
            return state

        # 为每个高风险操作创建审批请求
        session_id = state.get("session_id", "unknown")
        approval_ids = []

        for step in high_risk:
            request = self._gate.create_request(
                session_id=session_id,
                operation=step.get("action", step.get("description", "unknown operation")),
                risk_level=step.get("risk_level", "high"),
                context={
                    "root_cause": state.get("root_cause", ""),
                    "confidence": state.get("confidence", 0),
                    "step_detail": step,
                },
                timeout_seconds=self._timeout,
            )
            approval_ids.append(request.id)
            step["approval_id"] = request.id

            logger.info(
                "hitl_request_created",
                request_id=request.id,
                operation=step.get("action", "")[:80],
                risk_level=step.get("risk_level"),
                approval_type=request.approval_type.value,
            )

        # 检查所有审批请求的状态
        # HITLGate 在 DEV 模式下会自动审批（auto_approve_dev=True）
        all_approved = True
        any_rejected = False
        comments = []

        for aid in approval_ids:
            req = self._gate.check_status(aid)
            if req.status in (ApprovalStatus.APPROVED, ApprovalStatus.AUTO_APPROVED):
                comments.append(f"[{aid}] approved")
            elif req.status == ApprovalStatus.REJECTED:
                any_rejected = True
                comments.append(f"[{aid}] rejected: {req.reject_reason or 'no reason'}")
            elif req.status == ApprovalStatus.TIMEOUT:
                any_rejected = True
                comments.append(f"[{aid}] timeout (fail-safe rejection)")
            else:
                all_approved = False
                comments.append(f"[{aid}] pending")

        # 更新状态
        if any_rejected:
            state["hitl_status"] = "rejected"
            state["hitl_comment"] = "; ".join(comments)
            logger.warning(
                "hitl_gate_rejected",
                approval_ids=approval_ids,
                comments=comments,
            )
        elif all_approved:
            state["hitl_status"] = "approved"
            state["hitl_comment"] = "; ".join(comments)
            logger.info(
                "hitl_gate_approved",
                approval_ids=approval_ids,
                high_risk_count=len(high_risk),
            )
        else:
            # 还有未决审批——生产环境中这里会被 interrupt_before 暂停
            state["hitl_status"] = "pending"
            state["hitl_comment"] = "Waiting for approval"
            state["_pending_approval_ids"] = approval_ids
            logger.info(
                "hitl_gate_pending",
                pending_count=sum(1 for c in comments if "pending" in c),
            )

        return state

    @property
    def gate(self) -> HITLGate:
        """暴露 gate 实例，供 API 层查询/操作审批."""
        return self._gate
