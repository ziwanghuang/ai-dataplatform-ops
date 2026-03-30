"""
Token 预算管理器 — 按任务类型限额

WHY 需要 Token 预算而不是"用多少算多少"：
- 单次诊断链路可能调用 LLM 6-10 次（分诊+诊断+规划+报告）
- 不加控制，一个 Kafka 集群 50 个 Topic 的诊断可以吃掉 100K Token
- 按任务类型设定上限，超限时自动降级（缩减上下文/切换小模型）

预算模型（参考 02-LLM客户端与多模型路由.md §5.1）：
- 每个 session 有总预算上限
- 每个 Agent 节点有独立的分配额度
- 超额时先压缩上下文，再降级模型，最后拒绝调用
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from aiops.core.logging import get_logger
from aiops.llm.types import TaskType

logger = get_logger(__name__)


class BudgetAction(str, Enum):
    """预算不足时的处理动作."""
    ALLOW = "allow"               # 允许调用
    COMPRESS = "compress"         # 压缩上下文后允许
    DOWNGRADE = "downgrade"       # 降级到小模型
    REJECT = "reject"             # 拒绝调用


@dataclass
class TaskBudget:
    """单个任务类型的预算配置."""
    task_type: TaskType
    max_tokens_per_call: int     # 单次调用上限
    max_tokens_per_session: int  # 单个 session 中该任务的总上限
    warning_threshold: float = 0.8  # 使用率达到 80% 时告警
    allow_downgrade: bool = True     # 是否允许降级到小模型


@dataclass
class SessionBudget:
    """单个 Session 的预算状态."""
    session_id: str
    total_budget: int
    used_tokens: int = 0
    task_usage: dict[str, int] = field(default_factory=dict)  # task_type → used
    call_count: int = 0
    created_at: float = field(default_factory=time.monotonic)

    @property
    def remaining(self) -> int:
        return max(0, self.total_budget - self.used_tokens)

    @property
    def usage_rate(self) -> float:
        return self.used_tokens / self.total_budget if self.total_budget > 0 else 0.0


class TokenBudgetManager:
    """
    Token 预算管理器.

    职责：
    1. 按任务类型分配预算
    2. 实时追踪使用量
    3. 预算不足时返回降级策略
    4. 汇总报告

    WHY 不用 Redis 做全局限额：
    - 开发阶段不依赖 Redis
    - 单机 Agent 进程内的预算足够用内存管理
    - 生产多实例部署时再升级到 Redis 原子计数器
    """

    # 默认预算配置（参考 02-LLM客户端与多模型路由.md §5.1 表格）
    DEFAULT_BUDGETS: dict[TaskType, TaskBudget] = {
        TaskType.TRIAGE: TaskBudget(
            task_type=TaskType.TRIAGE,
            max_tokens_per_call=2_000,      # 分诊结构化输出，Token 可控
            max_tokens_per_session=5_000,
        ),
        TaskType.DIAGNOSTIC: TaskBudget(
            task_type=TaskType.DIAGNOSTIC,
            max_tokens_per_call=8_000,      # 诊断需要大上下文
            max_tokens_per_session=40_000,  # 可能 4 轮自环
        ),
        TaskType.PLANNING: TaskBudget(
            task_type=TaskType.PLANNING,
            max_tokens_per_call=6_000,
            max_tokens_per_session=15_000,
        ),
        TaskType.REMEDIATION: TaskBudget(
            task_type=TaskType.REMEDIATION,
            max_tokens_per_call=4_000,
            max_tokens_per_session=10_000,
        ),
        TaskType.REPORT: TaskBudget(
            task_type=TaskType.REPORT,
            max_tokens_per_call=6_000,
            max_tokens_per_session=10_000,
            allow_downgrade=True,  # 报告可以用小模型
        ),
        TaskType.PATROL: TaskBudget(
            task_type=TaskType.PATROL,
            max_tokens_per_call=3_000,
            max_tokens_per_session=8_000,
        ),
        TaskType.ALERT_CORRELATION: TaskBudget(
            task_type=TaskType.ALERT_CORRELATION,
            max_tokens_per_call=6_000,
            max_tokens_per_session=15_000,
        ),
    }

    # 整个 Session 的全局上限
    DEFAULT_SESSION_BUDGET: int = 100_000

    def __init__(
        self,
        session_budget: int = DEFAULT_SESSION_BUDGET,
        task_budgets: dict[TaskType, TaskBudget] | None = None,
    ) -> None:
        self._session_budget = session_budget
        self._task_budgets = task_budgets or self.DEFAULT_BUDGETS
        self._sessions: dict[str, SessionBudget] = {}

    def check_budget(
        self,
        session_id: str,
        task_type: TaskType,
        estimated_tokens: int,
    ) -> BudgetAction:
        """
        检查预算是否足够，返回允许/降级/拒绝策略.

        Args:
            session_id: 会话 ID
            task_type: 任务类型
            estimated_tokens: 本次调用预估的 Token 数

        Returns:
            BudgetAction 告诉调用方该怎么做
        """
        session = self._get_or_create_session(session_id)
        task_budget = self._task_budgets.get(task_type)

        # 1. 全局预算检查
        if session.remaining < estimated_tokens:
            logger.warning(
                "budget_session_exceeded",
                session_id=session_id,
                remaining=session.remaining,
                estimated=estimated_tokens,
            )
            return BudgetAction.REJECT

        # 2. 任务级预算检查
        if task_budget:
            task_used = session.task_usage.get(task_type.value, 0)
            task_remaining = task_budget.max_tokens_per_session - task_used

            if task_remaining < estimated_tokens:
                if task_budget.allow_downgrade:
                    logger.warning(
                        "budget_task_exceeded_downgrade",
                        task_type=task_type.value,
                        task_remaining=task_remaining,
                        estimated=estimated_tokens,
                    )
                    return BudgetAction.DOWNGRADE
                return BudgetAction.REJECT

            # 单次调用上限检查
            if estimated_tokens > task_budget.max_tokens_per_call:
                logger.info(
                    "budget_compress_needed",
                    task_type=task_type.value,
                    max_per_call=task_budget.max_tokens_per_call,
                    estimated=estimated_tokens,
                )
                return BudgetAction.COMPRESS

            # 使用率告警
            usage_rate = task_used / task_budget.max_tokens_per_session
            if usage_rate >= task_budget.warning_threshold:
                logger.warning(
                    "budget_warning",
                    task_type=task_type.value,
                    usage_rate=f"{usage_rate:.1%}",
                    remaining=task_remaining,
                )

        return BudgetAction.ALLOW

    def consume(
        self,
        session_id: str,
        task_type: TaskType,
        actual_tokens: int,
    ) -> None:
        """
        记录实际 Token 消耗.

        在 LLM 调用成功后调用此方法。
        """
        session = self._get_or_create_session(session_id)
        session.used_tokens += actual_tokens
        session.call_count += 1

        task_key = task_type.value
        session.task_usage[task_key] = session.task_usage.get(task_key, 0) + actual_tokens

        logger.debug(
            "budget_consumed",
            session_id=session_id,
            task_type=task_key,
            tokens=actual_tokens,
            session_used=session.used_tokens,
            session_remaining=session.remaining,
        )

    def get_session_report(self, session_id: str) -> dict[str, Any]:
        """获取 Session 的预算使用报告."""
        session = self._sessions.get(session_id)
        if not session:
            return {"error": f"Session {session_id} not found"}

        return {
            "session_id": session_id,
            "total_budget": session.total_budget,
            "used_tokens": session.used_tokens,
            "remaining": session.remaining,
            "usage_rate": f"{session.usage_rate:.1%}",
            "call_count": session.call_count,
            "task_breakdown": {
                task: {
                    "used": used,
                    "budget": self._task_budgets[TaskType(task)].max_tokens_per_session
                    if TaskType(task) in self._task_budgets else "unlimited",
                }
                for task, used in session.task_usage.items()
            },
        }

    def reset_session(self, session_id: str) -> None:
        """重置 Session 预算（新的诊断请求）."""
        if session_id in self._sessions:
            del self._sessions[session_id]
            logger.info("budget_session_reset", session_id=session_id)

    def _get_or_create_session(self, session_id: str) -> SessionBudget:
        if session_id not in self._sessions:
            self._sessions[session_id] = SessionBudget(
                session_id=session_id,
                total_budget=self._session_budget,
            )
        return self._sessions[session_id]
