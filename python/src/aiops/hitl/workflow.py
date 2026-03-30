"""
审批工作流 — 端到端审批流程编排

WHY 独立 Workflow 而不是把流程塞进 HITLGate：
- Gate 只管"单个审批请求"的状态
- Workflow 管"一次修复操作"的完整审批链路
- 修复操作可能需要多个审批（如先审批重启，再审批配置变更）

审批流程（参考 15-HITL人机协作系统.md §4）：
1. Agent 生成修复方案 → 2. RiskClassifier 评估 → 3. 创建审批请求
→ 4. 通知审批人 → 5. 等待审批 → 6. 审批通过/拒绝/超时
→ 7. 更新 Agent State → 8. 继续/中止修复链路
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Awaitable

from aiops.core.logging import get_logger
from aiops.hitl.gate import ApprovalRequest, ApprovalStatus, HITLGate
from aiops.hitl.risk import RiskAssessment, RiskClassifier

logger = get_logger(__name__)


class WorkflowState(str, Enum):
    """工作流状态."""
    INIT = "init"                 # 初始化
    RISK_ASSESSED = "risk_assessed"  # 风险已评估
    AWAITING_APPROVAL = "awaiting_approval"  # 等待审批
    APPROVED = "approved"         # 已批准
    REJECTED = "rejected"         # 已拒绝
    EXECUTING = "executing"       # 执行中
    COMPLETED = "completed"       # 完成
    FAILED = "failed"             # 失败
    ROLLED_BACK = "rolled_back"   # 已回滚


@dataclass
class WorkflowStep:
    """工作流中的单个步骤."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    operation: str = ""
    risk_assessment: RiskAssessment | None = None
    approval_request: ApprovalRequest | None = None
    state: WorkflowState = WorkflowState.INIT
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    started_at: float | None = None
    completed_at: float | None = None


@dataclass
class ApprovalWorkflowContext:
    """审批工作流上下文."""
    workflow_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    session_id: str = ""
    steps: list[WorkflowStep] = field(default_factory=list)
    state: WorkflowState = WorkflowState.INIT
    created_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


class ApprovalWorkflow:
    """
    审批工作流编排器.

    职责：
    1. 编排修复操作的风险评估 → 审批 → 执行的完整流程
    2. 支持多步骤审批（一次修复可能包含多个操作）
    3. 管理回滚点
    4. 提供 WebSocket/轮询接口给前端

    WHY 不用现成的工作流引擎（如 Temporal/Prefect）：
    - 审批工作流足够简单（线性流程，无复杂分支）
    - 引入外部工作流引擎是过度工程化
    - 自己实现可以深度集成 LangGraph 的 interrupt 机制
    """

    def __init__(
        self,
        gate: HITLGate | None = None,
        risk_classifier: RiskClassifier | None = None,
    ) -> None:
        self._gate = gate or HITLGate()
        self._classifier = risk_classifier or RiskClassifier()
        self._workflows: dict[str, ApprovalWorkflowContext] = {}
        self._listeners: list[Callable[[str, WorkflowState], Awaitable[None]]] = []

    async def create_workflow(
        self,
        session_id: str,
        operations: list[dict[str, Any]],
        metadata: dict[str, Any] | None = None,
    ) -> ApprovalWorkflowContext:
        """
        创建审批工作流.

        Args:
            session_id: 关联的诊断会话
            operations: 操作列表，每个包含 {operation, components, node_count}
            metadata: 附加元数据（诊断结果、证据链等）

        Returns:
            ApprovalWorkflowContext
        """
        ctx = ApprovalWorkflowContext(
            session_id=session_id,
            metadata=metadata or {},
        )

        # 为每个操作创建步骤
        for op in operations:
            step = WorkflowStep(
                operation=op.get("operation", ""),
            )

            # 风险评估
            assessment = self._classifier.classify(
                operation=step.operation,
                components=op.get("components", []),
                node_count=op.get("node_count", 1),
                is_business_hours=op.get("is_business_hours", False),
            )
            step.risk_assessment = assessment
            step.state = WorkflowState.RISK_ASSESSED

            ctx.steps.append(step)

        ctx.state = WorkflowState.RISK_ASSESSED
        self._workflows[ctx.workflow_id] = ctx

        logger.info(
            "workflow_created",
            workflow_id=ctx.workflow_id,
            session_id=session_id,
            step_count=len(ctx.steps),
            risk_levels=[
                s.risk_assessment.level.label
                for s in ctx.steps
                if s.risk_assessment
            ],
        )

        return ctx

    async def submit_for_approval(
        self,
        workflow_id: str,
    ) -> ApprovalWorkflowContext:
        """
        提交工作流中的所有需审批步骤.

        Returns:
            更新后的 context
        """
        ctx = self._get_workflow(workflow_id)

        for step in ctx.steps:
            if not step.risk_assessment:
                continue

            if step.risk_assessment.approval_required:
                # 创建审批请求
                request = self._gate.create_request(
                    session_id=ctx.session_id,
                    operation=step.operation,
                    risk_level=step.risk_assessment.level.label,
                    context={
                        "workflow_id": workflow_id,
                        "step_id": step.id,
                        "blast_radius": step.risk_assessment.blast_radius,
                        "factors": step.risk_assessment.factors,
                    },
                    timeout_seconds=step.risk_assessment.suggested_timeout,
                )
                step.approval_request = request
                step.state = WorkflowState.AWAITING_APPROVAL
            else:
                # 不需要审批，直接标记为已批准
                step.state = WorkflowState.APPROVED

        # 更新整体状态
        has_pending = any(
            s.state == WorkflowState.AWAITING_APPROVAL for s in ctx.steps
        )
        ctx.state = (
            WorkflowState.AWAITING_APPROVAL if has_pending
            else WorkflowState.APPROVED
        )

        # 通知监听者
        await self._notify_listeners(workflow_id, ctx.state)

        return ctx

    async def check_approval_status(
        self,
        workflow_id: str,
    ) -> ApprovalWorkflowContext:
        """检查工作流的审批状态."""
        ctx = self._get_workflow(workflow_id)

        all_approved = True
        any_rejected = False

        for step in ctx.steps:
            if step.approval_request and step.state == WorkflowState.AWAITING_APPROVAL:
                # 检查单个请求的状态
                updated = self._gate.check_status(step.approval_request.id)
                step.approval_request = updated

                if updated.status == ApprovalStatus.APPROVED:
                    step.state = WorkflowState.APPROVED
                elif updated.status == ApprovalStatus.AUTO_APPROVED:
                    step.state = WorkflowState.APPROVED
                elif updated.status in (ApprovalStatus.REJECTED, ApprovalStatus.TIMEOUT):
                    step.state = WorkflowState.REJECTED
                    any_rejected = True
                else:
                    all_approved = False

        # 更新整体状态
        if any_rejected:
            ctx.state = WorkflowState.REJECTED
        elif all_approved:
            ctx.state = WorkflowState.APPROVED

        return ctx

    async def wait_for_approval(
        self,
        workflow_id: str,
        poll_interval: float = 2.0,
        max_wait: float = 600.0,
    ) -> ApprovalWorkflowContext:
        """
        轮询等待审批完成.

        WHY 轮询而不是 WebSocket：
        - Agent 链路是 asyncio 协程，轮询最简单
        - WebSocket 需要额外的连接管理
        - 生产环境可以升级到 Redis Pub/Sub
        """
        start = time.monotonic()

        while (time.monotonic() - start) < max_wait:
            ctx = await self.check_approval_status(workflow_id)

            if ctx.state in (
                WorkflowState.APPROVED,
                WorkflowState.REJECTED,
            ):
                return ctx

            await asyncio.sleep(poll_interval)

        # 超时
        ctx = self._get_workflow(workflow_id)
        ctx.state = WorkflowState.REJECTED
        logger.warning(
            "workflow_wait_timeout",
            workflow_id=workflow_id,
            waited_seconds=max_wait,
        )
        return ctx

    def add_listener(
        self,
        callback: Callable[[str, WorkflowState], Awaitable[None]],
    ) -> None:
        """添加状态变更监听器."""
        self._listeners.append(callback)

    def get_workflow(self, workflow_id: str) -> ApprovalWorkflowContext | None:
        """获取工作流（外部接口）."""
        return self._workflows.get(workflow_id)

    def list_active_workflows(self) -> list[ApprovalWorkflowContext]:
        """列出所有活跃工作流."""
        active_states = {
            WorkflowState.INIT,
            WorkflowState.RISK_ASSESSED,
            WorkflowState.AWAITING_APPROVAL,
            WorkflowState.EXECUTING,
        }
        return [
            w for w in self._workflows.values()
            if w.state in active_states
        ]

    def _get_workflow(self, workflow_id: str) -> ApprovalWorkflowContext:
        ctx = self._workflows.get(workflow_id)
        if not ctx:
            raise ValueError(f"Workflow not found: {workflow_id}")
        return ctx

    async def _notify_listeners(
        self,
        workflow_id: str,
        state: WorkflowState,
    ) -> None:
        """通知所有监听者."""
        for listener in self._listeners:
            try:
                await listener(workflow_id, state)
            except Exception as e:
                logger.error(
                    "listener_error",
                    workflow_id=workflow_id,
                    error=str(e),
                )
