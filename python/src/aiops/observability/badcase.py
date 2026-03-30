"""
BadCase 追踪器 — 自动发现和收集失败案例

WHY 自动 BadCase 追踪：
- 分诊错误 → 后续全链路走偏，浪费 Token
- 诊断误判 → 运维人员信任度下降
- 手动收集 BadCase 效率低，容易遗漏
- 自动收集 → 定期分析 → 改进 Prompt/评估集 → 形成质量闭环

BadCase 判定标准（参考 18-评估体系与质量门禁.md §5）：
1. 分诊: 路由到错误路径（实际需诊断却走了快速路径）
2. 诊断: 置信度 < 0.5 或用户反馈"不准"
3. 修复: 审批被拒绝（说明方案不合理）
4. 超时: 单次诊断 > 120s
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from aiops.core.logging import get_logger

logger = get_logger(__name__)


class BadCaseType(str, Enum):
    """BadCase 类型."""
    TRIAGE_MISROUTE = "triage_misroute"       # 分诊路由错误
    LOW_CONFIDENCE = "low_confidence"          # 诊断置信度过低
    USER_REJECTED = "user_rejected"            # 用户反馈不准
    APPROVAL_REJECTED = "approval_rejected"    # 审批被拒绝
    TIMEOUT = "timeout"                        # 处理超时
    LLM_HALLUCINATION = "llm_hallucination"    # LLM 幻觉检出
    TOOL_FAILURE = "tool_failure"              # 工具调用失败
    SAFETY_VIOLATION = "safety_violation"      # 安全约束违反


class BadCaseSeverity(str, Enum):
    """BadCase 严重程度."""
    CRITICAL = "critical"   # 直接影响安全/正确性
    HIGH = "high"           # 影响用户体验
    MEDIUM = "medium"       # 可以忍受但需改进
    LOW = "low"             # 优化空间


@dataclass
class BadCase:
    """单个 BadCase 记录."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    session_id: str = ""
    type: BadCaseType = BadCaseType.LOW_CONFIDENCE
    severity: BadCaseSeverity = BadCaseSeverity.MEDIUM
    description: str = ""
    input_data: dict[str, Any] = field(default_factory=dict)
    output_data: dict[str, Any] = field(default_factory=dict)
    expected_output: dict[str, Any] | None = None
    root_cause: str = ""          # 初步分析的原因
    action_taken: str = ""        # 采取的措施
    timestamp: float = field(default_factory=time.time)
    resolved: bool = False
    tags: list[str] = field(default_factory=list)


class BadCaseTracker:
    """
    BadCase 自动追踪和收集.

    职责：
    1. 自动检测 BadCase（根据预设规则）
    2. 收集上下文数据（输入/输出/中间状态）
    3. 聚合统计（按类型/严重度/时间维度）
    4. 导出为评估数据集（扩充 eval/ 中的测试集）

    WHY 不直接用 LangFuse 做 BadCase：
    - LangFuse 记录所有 trace，BadCase 需要筛选和分析
    - BadCase 有自己的分类体系和严重度
    - 需要导出为评估数据集的能力
    """

    # 自动检测规则
    CONFIDENCE_THRESHOLD: float = 0.5       # 置信度低于此值判定为 BadCase
    TIMEOUT_THRESHOLD: float = 120.0        # 处理时间超过此值判定为超时
    MAX_STORED_CASES: int = 10000

    def __init__(self) -> None:
        self._cases: list[BadCase] = []
        self._stats: dict[str, int] = {}

    def record(self, case: BadCase) -> None:
        """手动记录一个 BadCase."""
        self._cases.append(case)
        self._update_stats(case)

        # 容量控制
        if len(self._cases) > self.MAX_STORED_CASES:
            self._cases = self._cases[-self.MAX_STORED_CASES:]

        logger.warning(
            "badcase_recorded",
            case_id=case.id,
            type=case.type.value,
            severity=case.severity.value,
            session_id=case.session_id,
            description=case.description[:200],
        )

    def check_triage_result(
        self,
        session_id: str,
        route: str,
        input_query: str,
        triage_output: dict[str, Any],
    ) -> BadCase | None:
        """
        检查分诊结果是否为 BadCase.

        规则：
        - route=direct_tool 但查询包含"诊断/故障/排查" → 可能误分
        - route=diagnosis 但查询是"XXX状态/XXX列表" → 可能过度诊断
        """
        query_lower = input_query.lower()
        diagnosis_keywords = {"诊断", "故障", "排查", "异常", "报错", "宕机", "卡住", "不可用"}
        simple_keywords = {"状态", "列表", "查看", "多少", "版本"}

        if route == "direct_tool":
            if any(kw in query_lower for kw in diagnosis_keywords):
                case = BadCase(
                    session_id=session_id,
                    type=BadCaseType.TRIAGE_MISROUTE,
                    severity=BadCaseSeverity.HIGH,
                    description=f"疑似分诊错误: 查询含诊断关键词但路由到 direct_tool",
                    input_data={"query": input_query},
                    output_data=triage_output,
                    tags=["auto_detected", "triage"],
                )
                self.record(case)
                return case

        elif route == "diagnosis":
            if any(kw in query_lower for kw in simple_keywords) and not any(
                kw in query_lower for kw in diagnosis_keywords
            ):
                case = BadCase(
                    session_id=session_id,
                    type=BadCaseType.TRIAGE_MISROUTE,
                    severity=BadCaseSeverity.MEDIUM,
                    description=f"疑似过度诊断: 简单查询路由到完整诊断链路",
                    input_data={"query": input_query},
                    output_data=triage_output,
                    tags=["auto_detected", "triage", "over_diagnosis"],
                )
                self.record(case)
                return case

        return None

    def check_diagnostic_result(
        self,
        session_id: str,
        confidence: float,
        diagnostic_output: dict[str, Any],
    ) -> BadCase | None:
        """检查诊断结果——置信度过低判定为 BadCase."""
        if confidence < self.CONFIDENCE_THRESHOLD:
            case = BadCase(
                session_id=session_id,
                type=BadCaseType.LOW_CONFIDENCE,
                severity=BadCaseSeverity.HIGH,
                description=f"诊断置信度过低: {confidence:.2f} < {self.CONFIDENCE_THRESHOLD}",
                output_data=diagnostic_output,
                tags=["auto_detected", "diagnostic"],
            )
            self.record(case)
            return case
        return None

    def check_timeout(
        self,
        session_id: str,
        elapsed_seconds: float,
        context: dict[str, Any] | None = None,
    ) -> BadCase | None:
        """检查处理是否超时."""
        if elapsed_seconds > self.TIMEOUT_THRESHOLD:
            case = BadCase(
                session_id=session_id,
                type=BadCaseType.TIMEOUT,
                severity=BadCaseSeverity.MEDIUM,
                description=f"处理超时: {elapsed_seconds:.1f}s > {self.TIMEOUT_THRESHOLD}s",
                input_data=context or {},
                tags=["auto_detected", "timeout"],
            )
            self.record(case)
            return case
        return None

    def record_user_feedback(
        self,
        session_id: str,
        feedback: str,
        rating: int,
        context: dict[str, Any] | None = None,
    ) -> BadCase | None:
        """记录用户反馈——低评分自动标记为 BadCase."""
        if rating <= 2:
            case = BadCase(
                session_id=session_id,
                type=BadCaseType.USER_REJECTED,
                severity=BadCaseSeverity.HIGH,
                description=f"用户评分 {rating}/5: {feedback}",
                input_data=context or {},
                tags=["user_feedback"],
            )
            self.record(case)
            return case
        return None

    def get_cases(
        self,
        type_filter: BadCaseType | None = None,
        severity_filter: BadCaseSeverity | None = None,
        limit: int = 100,
    ) -> list[BadCase]:
        """获取 BadCase 列表（支持过滤）."""
        filtered = self._cases
        if type_filter:
            filtered = [c for c in filtered if c.type == type_filter]
        if severity_filter:
            filtered = [c for c in filtered if c.severity == severity_filter]
        return filtered[-limit:]

    def export_for_eval(self, limit: int = 50) -> list[dict[str, Any]]:
        """
        导出 BadCase 为评估数据集格式.

        这些 case 可以直接加入 eval/datasets/ 做回归测试。
        """
        return [
            {
                "id": c.id,
                "type": c.type.value,
                "input": c.input_data,
                "actual_output": c.output_data,
                "expected_output": c.expected_output,
                "description": c.description,
                "tags": c.tags,
            }
            for c in self._cases[-limit:]
            if c.input_data  # 只导出有输入的 case
        ]

    def get_summary(self) -> dict[str, Any]:
        """获取 BadCase 统计汇总."""
        by_type: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        for c in self._cases:
            by_type[c.type.value] = by_type.get(c.type.value, 0) + 1
            by_severity[c.severity.value] = by_severity.get(c.severity.value, 0) + 1

        return {
            "total_cases": len(self._cases),
            "by_type": by_type,
            "by_severity": by_severity,
            "unresolved": sum(1 for c in self._cases if not c.resolved),
        }

    def _update_stats(self, case: BadCase) -> None:
        key = f"{case.type.value}:{case.severity.value}"
        self._stats[key] = self._stats.get(key, 0) + 1
