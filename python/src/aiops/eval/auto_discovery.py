"""
BadCase 自动发现 — 从诊断结果中自动检测潜在问题

WHY 自动发现：
- 依赖人工上报覆盖盲区严重（用户不一定知道 AI 回答是错的）
- 自动检测是从"被动等上报"到"主动找问题"的关键升级

检测策略：
1. 低置信度诊断 — confidence < 0.5
2. 幻觉检测 — 有诊断结论但无证据
3. 工具调用全失败 — 所有 tool_call 返回 error
4. 修复操作被回滚 — rollback 被执行（最严重）

设计决策：
- 多源检测：不依赖单一信号，交叉验证
- 异步上报：检测逻辑不阻塞主流程（< 10ms）
- 人工确认环节：自动发现的状态是 auto_detected，需人工确认
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from prometheus_client import Counter

from aiops.core.logging import get_logger

logger = get_logger(__name__)

AUTO_DETECTED = Counter(
    "aiops_badcase_auto_detected_total",
    "Bad cases detected automatically",
    ["detection_source"],
)


@dataclass
class DetectionSignal:
    """检测信号."""

    source: str  # 检测来源
    severity: str  # 严重程度（low/medium/high/critical）
    category: str  # BadCase 类型
    reason: str  # 检测理由
    confidence: float  # 检测置信度（0-1）
    evidence: dict  # 支撑证据


class BadCaseAutoDiscovery:
    """
    BadCase 自动发现引擎.

    在每次诊断后运行，检测潜在 BadCase 并异步上报。
    检测逻辑轻量（< 10ms），不影响响应延迟。
    """

    def __init__(self, badcase_tracker: Any = None) -> None:
        self._tracker = badcase_tracker
        self._detectors = [
            self._detect_low_confidence,
            self._detect_hallucination,
            self._detect_tool_failures,
            self._detect_rollback,
        ]

    async def analyze_diagnosis_result(
        self,
        request_id: str,
        query: str,
        result: dict,
        trace_id: str = "",
    ) -> list[DetectionSignal]:
        """
        分析单次诊断结果，检测潜在 BadCase.

        在每次诊断完成后调用。
        """
        signals: list[DetectionSignal] = []

        for detector in self._detectors:
            signal = await detector(request_id, query, result)
            if signal:
                signals.append(signal)

        # 如果检测到信号，异步上报（取最严重的）
        if signals:
            worst = max(signals, key=lambda s: self._severity_rank(s.severity))
            asyncio.create_task(
                self._auto_report(
                    request_id=request_id,
                    query=query,
                    signal=worst,
                    trace_id=trace_id,
                )
            )

        return signals

    async def _detect_low_confidence(
        self, request_id: str, query: str, result: dict
    ) -> DetectionSignal | None:
        """
        检测低置信度诊断.

        WHY threshold=0.5：
        - < 0.5 意味着 Agent 自己都不确定
        - 但不是 0：有些复杂场景确实置信度较低但结果正确
        """
        confidence = result.get("confidence", 1.0)
        if confidence < 0.5:
            return DetectionSignal(
                source="low_confidence",
                severity="medium",
                category="diagnosis_error",
                reason=f"诊断置信度过低: {confidence:.2f} (threshold: 0.5)",
                confidence=1.0 - confidence,
                evidence={
                    "confidence": confidence,
                    "root_cause": result.get("root_cause", ""),
                },
            )
        return None

    async def _detect_hallucination(
        self, request_id: str, query: str, result: dict
    ) -> DetectionSignal | None:
        """
        检测幻觉（诊断结论无证据支撑）.

        启发式规则：
        1. 有诊断结论但没有 evidence → 可能幻觉
        2. evidence 为空但 confidence > 0.7 → 过度自信无证据 = 幻觉
        """
        root_cause = result.get("root_cause", "")
        evidence = result.get("evidence", [])
        confidence = result.get("confidence", 0)

        if root_cause and not evidence:
            return DetectionSignal(
                source="hallucination_heuristic",
                severity="high",
                category="hallucination",
                reason="诊断结论无证据支撑（evidence 为空）",
                confidence=0.7,
                evidence={"root_cause": root_cause, "evidence_count": 0},
            )

        if root_cause and len(evidence) == 0 and confidence > 0.7:
            return DetectionSignal(
                source="overconfident_hallucination",
                severity="high",
                category="hallucination",
                reason=f"高置信度({confidence:.2f})但无证据，疑似幻觉",
                confidence=0.8,
                evidence={"confidence": confidence, "root_cause": root_cause},
            )

        return None

    async def _detect_tool_failures(
        self, request_id: str, query: str, result: dict
    ) -> DetectionSignal | None:
        """
        检测工具调用全失败.

        WHY 全失败才上报：
        - 单个工具失败可能是暂时性的（网络超时）
        - 全失败意味着 Agent 在"无数据"情况下生成结论 → 高幻觉风险
        """
        tools_used = result.get("tools_used", [])
        tool_errors = result.get("tool_errors", [])

        if tools_used and len(tool_errors) == len(tools_used):
            return DetectionSignal(
                source="all_tools_failed",
                severity="high",
                category="tool_failure",
                reason=f"所有 {len(tools_used)} 个工具调用均失败",
                confidence=0.9,
                evidence={
                    "tools": tools_used,
                    "errors": tool_errors[:5],
                },
            )
        return None

    async def _detect_rollback(
        self, request_id: str, query: str, result: dict
    ) -> DetectionSignal | None:
        """
        检测修复操作被回滚.

        WHY critical：修复被回滚意味着 AI 建议的操作产生了负面影响。
        """
        remediation = result.get("remediation", {})
        if remediation.get("rollback_executed"):
            return DetectionSignal(
                source="rollback_detected",
                severity="critical",
                category="remediation_unsafe",
                reason="修复操作被回滚，建议的修复方案产生了负面影响",
                confidence=1.0,
                evidence={
                    "original_action": remediation.get("action", ""),
                    "rollback_reason": remediation.get("rollback_reason", ""),
                },
            )
        return None

    async def _auto_report(
        self,
        request_id: str,
        query: str,
        signal: DetectionSignal,
        trace_id: str,
    ) -> None:
        """异步上报自动发现的 BadCase."""
        case_id = (
            f"AUTO_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
            f"_{request_id[:8]}"
        )

        try:
            if self._tracker:
                await self._tracker.report(
                    case_id=case_id,
                    request_id=request_id,
                    reason=f"[自动发现-{signal.source}] {signal.reason}",
                    reporter="auto_discovery",
                    category=signal.category,
                    severity=signal.severity,
                    user_query=query,
                    trace_id=trace_id,
                )

            AUTO_DETECTED.labels(detection_source=signal.source).inc()
            logger.info(
                "badcase_auto_reported",
                case_id=case_id,
                source=signal.source,
                severity=signal.severity,
            )
        except Exception as e:
            logger.error("badcase_auto_report_failed", error=str(e))

    @staticmethod
    def _severity_rank(severity: str) -> int:
        return {"low": 0, "medium": 1, "high": 2, "critical": 3}.get(severity, 0)
