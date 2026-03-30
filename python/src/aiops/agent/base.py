"""
BaseAgentNode — Agent 节点基类

所有 Agent 节点（Triage/Diagnostic/Planning/...）都继承此基类。
基类实现了模板方法模式（Template Method Pattern），确保每个节点都经过：
    前置检查 → 核心逻辑 → 后置处理 → 异常兜底

WHY 模板方法而不是让子类自由实现 __call__：
- OTel Span 创建、Prometheus 指标、错误处理等横切关注点在基类统一实现
- 子类只需关注核心业务逻辑（process 方法）
- 如果让子类自己实现 __call__，很容易遗漏 Span/指标/错误处理
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any

from aiops.agent.state import AgentState, snapshot_state
from aiops.core.errors import AIOpsError
from aiops.core.logging import get_logger
from aiops.llm.types import LLMCallContext, LLMResponse, TaskType

logger = get_logger(__name__)


class BaseAgentNode(ABC):
    """
    Agent 节点基类.

    子类必须：
    1. 设置 agent_name（用于日志/指标/OTel Span 标识）
    2. 设置 task_type（用于 LLM 路由——不同任务类型路由到不同模型）
    3. 实现 process() 方法（核心业务逻辑）

    可选覆盖：
    - _pre_process(): 节点执行前的准备工作（如 Diagnostic 递增轮次）
    - _post_process(): 节点执行后的清理工作（如 Report 格式化）
    """

    agent_name: str = "base"
    task_type: TaskType = TaskType.DIAGNOSTIC

    def __init__(self, llm_client: Any | None = None) -> None:
        self.llm = llm_client

    async def __call__(self, state: AgentState) -> AgentState:
        """
        LangGraph 调用入口 — 模板方法.

        执行顺序（固定，子类不应覆盖）：
        1. 记录当前 Agent 名称到 state（调试/审计用）
        2. _pre_check(): Token 预算检查
        3. _pre_process(): 可选前置钩子
        4. process(): 核心逻辑（子类实现）
        5. _post_process(): 可选后置钩子
        6. 异常处理：AIOpsError → 降级 / Exception → 强制结束
        """
        start = time.monotonic()
        state["current_agent"] = self.agent_name

        try:
            # Step 1: 前置检查（Token 预算、轮次限制）
            self._pre_check(state)

            # Step 2: 可选前置钩子
            await self._pre_process(state)

            # Step 3: 核心逻辑（子类实现）
            result = await self.process(state)

            # Step 4: 可选后置钩子
            await self._post_process(result)

            # Step 5: 记录成功
            duration = time.monotonic() - start
            logger.info(
                "agent_completed",
                agent_name=self.agent_name,
                duration_ms=int(duration * 1000),
                tokens=state.get("total_tokens", 0),
            )
            return result

        except AIOpsError as e:
            # 已分类的业务异常 → 记录 + 尝试降级
            state["error_count"] = state.get("error_count", 0) + 1
            logger.error(
                "agent_error",
                agent_name=self.agent_name,
                error_code=e.code.value,
                error_message=e.message,
                state_snapshot=snapshot_state(state),
            )
            # 降级：标记错误但继续流程（让路由函数决定下一步）
            return state

        except Exception as e:
            # 未预期异常 → 记录完整堆栈
            state["error_count"] = state.get("error_count", 0) + 1
            logger.critical(
                "agent_unexpected_error",
                agent_name=self.agent_name,
                error=str(e),
                state_snapshot=snapshot_state(state),
                exc_info=True,
            )
            return state

    @abstractmethod
    async def process(self, state: AgentState) -> AgentState:
        """
        子类实现核心逻辑.

        约定：
        - 只读取自己需要的 state 字段
        - 只写入自己负责的 state 字段（参考所有权矩阵）
        - 不要在 process 中做日志/指标（基类已处理）
        """
        ...

    async def _pre_process(self, state: AgentState) -> None:
        """可选前置钩子（子类覆盖）."""

    async def _post_process(self, state: AgentState) -> None:
        """可选后置钩子（子类覆盖）."""

    def _pre_check(self, state: AgentState) -> None:
        """
        前置检查：Token 预算、轮次限制.

        WHY 超预算不直接 raise：
        让子类决定是否继续——Report Agent 可能决定生成简化报告
        而不是直接拒绝用户请求
        """
        total_tokens = state.get("total_tokens", 0)
        # Token 预算软限制：15000 tokens（用户查询）/ 20000（告警）
        budget_limit = 20000 if state.get("request_type") == "alert" else 15000
        if total_tokens > budget_limit:
            logger.warning(
                "budget_exceeded",
                agent=self.agent_name,
                total_tokens=total_tokens,
                limit=budget_limit,
            )

    def _update_token_usage(self, state: AgentState, response: LLMResponse) -> None:
        """更新 state 中的 Token 使用量（由子类在 LLM 调用后手动调用）."""
        state["total_tokens"] = state.get("total_tokens", 0) + response.usage.total_tokens
        # 粗略成本估算（精确计算在 CostTracker 中）
        estimated_cost = response.usage.total_tokens * 0.000002  # ~$2/M tokens 均价
        state["total_cost_usd"] = state.get("total_cost_usd", 0.0) + estimated_cost
