"""
风险分级器 — 5 级风控模型

WHY 独立风险分级而不是让 LLM 直接判断：
- LLM 对风险的判断不稳定（同一个 restart 操作，有时判 high 有时判 medium）
- 风险分级是安全硬约束，不能有"偶尔犯错"的空间
- 规则引擎覆盖 80% 场景，LLM 只在边界 case 参与

5 级风险模型（参考 15-HITL人机协作系统.md §3.1）：
  L0-SAFE: 只读查询，无需审批
  L1-LOW: 缓存清除、日志查询，通知但不阻塞
  L2-MEDIUM: 配置变更、GC 触发，单人审批
  L3-HIGH: 服务重启、扩缩容，单人审批 + 回滚计划
  L4-CRITICAL: 退役节点、数据迁移，双人审批 + 回滚演练
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from aiops.core.logging import get_logger

logger = get_logger(__name__)


class RiskLevel(IntEnum):
    """5 级风险等级."""
    SAFE = 0         # L0: 只读
    LOW = 1          # L1: 低影响
    MEDIUM = 2       # L2: 需确认
    HIGH = 3         # L3: 需审批
    CRITICAL = 4     # L4: 需双人审批

    @property
    def label(self) -> str:
        return {0: "safe", 1: "low", 2: "medium", 3: "high", 4: "critical"}[self.value]

    @property
    def requires_approval(self) -> bool:
        return self >= RiskLevel.MEDIUM

    @property
    def requires_dual_approval(self) -> bool:
        return self >= RiskLevel.CRITICAL


@dataclass
class RiskAssessment:
    """风险评估结果."""
    level: RiskLevel
    score: float              # 0-1 综合风险分数
    factors: list[str]        # 风险因素列表
    approval_required: bool
    dual_approval: bool
    suggested_timeout: float  # 建议的审批超时秒数
    rollback_required: bool   # 是否必须有回滚计划
    blast_radius: str         # 影响范围描述


class RiskClassifier:
    """
    风险分级器.

    分级策略——按优先级匹配：
    1. 硬规则：操作类型直接映射风险等级（覆盖 ~80%）
    2. 影响范围加权：多组件/多节点操作提升一级
    3. 时间因素：业务高峰期（9-12, 14-18）提升一级
    4. 历史因素：同类操作历史失败率 > 10% 提升一级

    WHY 不用 ML 模型做风险分级：
    - 风险分级需要 100% 可解释性（出事故时要说清楚为什么判了这个等级）
    - 训练数据不够（运维操作样本少）
    - 规则引擎 + 加权的确定性更高
    """

    # 操作类型 → 基础风险等级
    OPERATION_RISK_MAP: dict[str, RiskLevel] = {
        # L0 - 只读操作
        "status": RiskLevel.SAFE,
        "query": RiskLevel.SAFE,
        "list": RiskLevel.SAFE,
        "check": RiskLevel.SAFE,
        "search": RiskLevel.SAFE,
        "overview": RiskLevel.SAFE,
        "describe": RiskLevel.SAFE,
        # L1 - 低影响
        "clear_cache": RiskLevel.LOW,
        "flush_logs": RiskLevel.LOW,
        "refresh": RiskLevel.LOW,
        # L2 - 需确认
        "update_config": RiskLevel.MEDIUM,
        "trigger_gc": RiskLevel.MEDIUM,
        "rebalance": RiskLevel.MEDIUM,
        "set_quota": RiskLevel.MEDIUM,
        # L3 - 需审批
        "restart": RiskLevel.HIGH,
        "scale": RiskLevel.HIGH,
        "failover": RiskLevel.HIGH,
        "rolling_restart": RiskLevel.HIGH,
        # L4 - 双人审批
        "decommission": RiskLevel.CRITICAL,
        "format": RiskLevel.CRITICAL,
        "delete_data": RiskLevel.CRITICAL,
        "migrate": RiskLevel.CRITICAL,
    }

    # 高风险关键词正则
    HIGH_RISK_PATTERNS: list[tuple[re.Pattern[str], RiskLevel]] = [
        (re.compile(r"restart|重启", re.IGNORECASE), RiskLevel.HIGH),
        (re.compile(r"scale|扩缩容", re.IGNORECASE), RiskLevel.HIGH),
        (re.compile(r"decommission|退役", re.IGNORECASE), RiskLevel.CRITICAL),
        (re.compile(r"delete|删除|drop", re.IGNORECASE), RiskLevel.CRITICAL),
        (re.compile(r"format|格式化", re.IGNORECASE), RiskLevel.CRITICAL),
        (re.compile(r"failover|切换", re.IGNORECASE), RiskLevel.HIGH),
        (re.compile(r"config.*change|修改配置", re.IGNORECASE), RiskLevel.MEDIUM),
    ]

    def classify(
        self,
        operation: str,
        components: list[str] | None = None,
        node_count: int = 1,
        is_business_hours: bool = False,
        historical_failure_rate: float = 0.0,
    ) -> RiskAssessment:
        """
        对操作进行风险分级.

        Args:
            operation: 操作描述或工具名
            components: 涉及的组件列表
            node_count: 影响的节点数
            is_business_hours: 是否在业务高峰期
            historical_failure_rate: 同类操作的历史失败率

        Returns:
            RiskAssessment 包含完整的风险评估
        """
        components = components or []
        factors: list[str] = []
        base_level = RiskLevel.SAFE

        # 1. 操作类型匹配
        operation_lower = operation.lower()
        for op_type, risk in self.OPERATION_RISK_MAP.items():
            if op_type in operation_lower:
                base_level = max(base_level, risk)
                factors.append(f"操作类型匹配: {op_type} → {risk.label}")
                break

        # 2. 正则模式匹配
        for pattern, risk in self.HIGH_RISK_PATTERNS:
            if pattern.search(operation):
                if risk > base_level:
                    base_level = risk
                    factors.append(f"关键词匹配: {pattern.pattern} → {risk.label}")

        # 3. 影响范围加权
        final_level = base_level
        blast_radius_parts: list[str] = []

        if len(components) > 1:
            # 多组件联动，提升一级
            final_level = min(RiskLevel.CRITICAL, RiskLevel(final_level + 1))
            factors.append(f"多组件联动: {components} (+1 级)")
            blast_radius_parts.append(f"{len(components)} 个组件")

        if node_count > 3:
            # 影响超过 3 个节点，提升一级
            final_level = min(RiskLevel.CRITICAL, RiskLevel(final_level + 1))
            factors.append(f"影响 {node_count} 节点 (+1 级)")
            blast_radius_parts.append(f"{node_count} 个节点")

        # 4. 时间因素
        if is_business_hours and final_level >= RiskLevel.MEDIUM:
            final_level = min(RiskLevel.CRITICAL, RiskLevel(final_level + 1))
            factors.append("业务高峰期 (+1 级)")
            blast_radius_parts.append("业务高峰期")

        # 5. 历史失败率
        if historical_failure_rate > 0.1 and final_level >= RiskLevel.LOW:
            final_level = min(RiskLevel.CRITICAL, RiskLevel(final_level + 1))
            factors.append(f"历史失败率 {historical_failure_rate:.0%} (+1 级)")

        # 计算综合风险分数
        score = self._calculate_score(
            base_level, final_level, len(components), node_count,
            is_business_hours, historical_failure_rate,
        )

        # 建议超时
        timeout_map = {
            RiskLevel.SAFE: 0,
            RiskLevel.LOW: 60,
            RiskLevel.MEDIUM: 300,
            RiskLevel.HIGH: 600,
            RiskLevel.CRITICAL: 1800,
        }

        blast_radius = (
            ", ".join(blast_radius_parts) if blast_radius_parts
            else f"单组件{'单节点' if node_count <= 1 else f'{node_count}节点'}"
        )

        assessment = RiskAssessment(
            level=final_level,
            score=score,
            factors=factors,
            approval_required=final_level.requires_approval,
            dual_approval=final_level.requires_dual_approval,
            suggested_timeout=timeout_map.get(final_level, 300),
            rollback_required=final_level >= RiskLevel.HIGH,
            blast_radius=blast_radius,
        )

        logger.info(
            "risk_classified",
            operation=operation,
            level=final_level.label,
            score=f"{score:.2f}",
            factors=factors,
            approval_required=assessment.approval_required,
        )

        return assessment

    @staticmethod
    def _calculate_score(
        base_level: RiskLevel,
        final_level: RiskLevel,
        component_count: int,
        node_count: int,
        is_business_hours: bool,
        failure_rate: float,
    ) -> float:
        """计算综合风险分数 (0-1)."""
        # 基础分：按等级
        base_score = final_level.value / 4.0

        # 组件数加权
        component_factor = min(component_count * 0.05, 0.15)

        # 节点数加权
        node_factor = min(node_count * 0.02, 0.1)

        # 业务时间加权
        time_factor = 0.1 if is_business_hours else 0.0

        # 历史失败率加权
        history_factor = failure_rate * 0.2

        return min(1.0, base_score + component_factor + node_factor + time_factor + history_factor)
