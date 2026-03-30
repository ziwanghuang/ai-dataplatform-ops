"""
Remediation Agent — 受控修复执行

Phase 1-3 渐进式放权：
- Phase 1: 生成修复建议（只标记 suggested）
- Phase 2: 低风险自动执行 + 高风险审批后执行 ← **当前**
- Phase 3: 带回滚的自动执行 + 影响监控（TODO）

WHY 分阶段实现：
生产环境中直接让 AI 执行修复操作风险极高。
Phase 1 证明了修复建议的准确率 >90% 后，Phase 2 放开低风险操作的自动执行。
每个 Phase 的切换通过环境变量 REMEDIATION_PHASE 控制，不改代码。

Phase 2 执行策略：
- risk=none/low → 自动执行（通过 MCP 调用）
- risk=medium → 单人审批后执行
- risk=high/critical → 双人审批后执行，且必须有回滚方案
"""

from __future__ import annotations

from typing import Any

from aiops.agent.base import BaseAgentNode
from aiops.agent.state import AgentState
from aiops.core.config import get_settings
from aiops.core.logging import get_logger
from aiops.llm.types import TaskType

logger = get_logger(__name__)


class RemediationNode(BaseAgentNode):
    """
    修复节点 — Phase 2: 低风险自动执行 + 高风险记录建议.

    执行矩阵：
    | 风险等级   | DEV 环境        | PRODUCTION 环境  |
    |-----------|-----------------|-----------------|
    | none/low  | 自动执行         | 自动执行         |
    | medium    | 自动执行(DEV)    | 审批后执行       |
    | high      | 建议(不执行)     | 审批后执行       |
    | critical  | 建议(不执行)     | 双人审批后执行   |

    WHY DEV 模式下 medium 也自动执行：
    - 开发环境没有真实集群，MCP 返回 Mock 结果
    - 自动执行不会造成任何影响
    - 但 high/critical 即使在 DEV 也不执行——保持安全意识
    """

    agent_name = "remediation"
    task_type = TaskType.REMEDIATION

    # 低风险阈值——低于等于此级别的操作可自动执行
    AUTO_EXECUTE_MAX_RISK = {"none", "low"}
    # DEV 模式额外允许 medium 自动执行
    DEV_AUTO_EXECUTE_MAX_RISK = {"none", "low", "medium"}

    def __init__(
        self,
        llm_client: Any | None = None,
        mcp_client: Any | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(llm_client, **kwargs)
        self._mcp = mcp_client
        settings = get_settings()
        self._is_dev = settings.env.value == "dev"

    async def process(self, state: AgentState) -> AgentState:
        """
        修复主流程（Phase 2）.

        1. 验证修复方案完整性
        2. 按风险等级分类：可执行 vs 仅建议
        3. 执行低风险操作（通过 MCP）
        4. 记录执行结果到 state
        """
        plan = state.get("remediation_plan", [])

        if not plan:
            logger.info("remediation_no_plan", reason="没有修复建议")
            return state

        validated_plan = []
        executed_count = 0
        suggested_count = 0

        for step in plan:
            risk = step.get("risk_level", "medium")
            validated_step = await self._process_step(step, risk, state)
            validated_plan.append(validated_step)

            if validated_step["execution_status"] == "executed":
                executed_count += 1
            else:
                suggested_count += 1

        state["remediation_plan"] = validated_plan
        state["remediation_summary"] = {
            "total_steps": len(validated_plan),
            "executed": executed_count,
            "suggested_only": suggested_count,
            "high_risk_steps": sum(
                1 for s in validated_plan
                if s.get("risk_level") in ("high", "critical")
            ),
        }

        logger.info(
            "remediation_completed",
            total_steps=len(validated_plan),
            executed=executed_count,
            suggested=suggested_count,
        )

        return state

    async def _process_step(
        self,
        step: dict[str, Any],
        risk: str,
        state: AgentState,
    ) -> dict[str, Any]:
        """
        处理单个修复步骤.

        决策逻辑：
        1. 验证完整性（回滚方案等）
        2. 判断是否可自动执行
        3. 可执行则调用 MCP 工具
        4. 不可执行则标记为 suggested
        """
        # 确保高风险步骤有审批标记
        if risk in ("high", "critical"):
            step["requires_approval"] = True

        # 确保有回滚方案
        if not step.get("rollback_action"):
            step["rollback_action"] = "⚠️ 未指定回滚方案，需人工确认"

        # 判断是否可自动执行
        allowed_risks = (
            self.DEV_AUTO_EXECUTE_MAX_RISK if self._is_dev
            else self.AUTO_EXECUTE_MAX_RISK
        )

        can_execute = (
            risk in allowed_risks
            and self._mcp is not None
            and step.get("tool_name")  # 必须有工具名才能执行
            and state.get("hitl_status") != "rejected"  # 审批未被拒绝
        )

        if can_execute:
            step = await self._execute_step(step)
        else:
            step["execution_status"] = "suggested"
            step["execution_note"] = self._get_skip_reason(risk, step)

        return step

    async def _execute_step(self, step: dict[str, Any]) -> dict[str, Any]:
        """
        通过 MCP 执行修复操作.

        WHY try/except 包裹而不是让异常传播：
        - 单步执行失败不应阻塞后续步骤
        - 失败的步骤标记为 failed，让 Report Agent 汇总
        - 操作员可以根据报告手动重试失败步骤
        """
        tool_name = step.get("tool_name", "")
        tool_params = step.get("tool_params", {})

        try:
            result = await self._mcp.call_tool(tool_name, tool_params)
            step["execution_status"] = "executed"
            step["execution_result"] = str(result)[:1000]
            step["execution_note"] = "Auto-executed (low risk)"

            logger.info(
                "remediation_step_executed",
                tool=tool_name,
                risk=step.get("risk_level"),
                result_preview=str(result)[:100],
            )

        except Exception as e:
            step["execution_status"] = "failed"
            step["execution_error"] = str(e)
            step["execution_note"] = f"Execution failed: {e}"

            logger.warning(
                "remediation_step_failed",
                tool=tool_name,
                risk=step.get("risk_level"),
                error=str(e),
            )

        return step

    def _get_skip_reason(self, risk: str, step: dict[str, Any]) -> str:
        """生成跳过执行的原因说明."""
        if risk in ("high", "critical"):
            return f"Risk level '{risk}' requires manual approval and execution"
        if not self._mcp:
            return "MCP client not configured"
        if not step.get("tool_name"):
            return "No tool specified for this step"
        return "Skipped by policy"
