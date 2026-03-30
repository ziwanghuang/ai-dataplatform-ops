"""
DeepEval 集成 — 自定义大数据诊断准确性评估指标

WHY 在 RAGAS 之外还用 DeepEval：
- RAGAS 专注 RAG 管道质量（检索+生成）
- DeepEval 提供更通用的 LLM 评估，特别是自定义 Metric
- 内置 pytest 插件，CI/CD 集成更简单
- 两者互补，不是替代

适用场景：
- 诊断报告的 Faithfulness → DeepEval（更灵活）
- RAG 检索质量 → RAGAS（更专精）
- 自定义评估指标 → DeepEval（原生支持）
"""

from __future__ import annotations

from typing import Any

from aiops.core.logging import get_logger

logger = get_logger(__name__)


class DeepEvalRunner:
    """
    DeepEval 评估运行器.

    封装 DeepEval 库，提供：
    1. Answer Relevancy 评估
    2. Faithfulness 评估
    3. 自定义 BigDataDiagnosisAccuracy GEval 指标
    4. pytest 集成支持
    """

    def __init__(self, model: str = "gpt-4o") -> None:
        self._model = model
        self._available = False

        try:
            from deepeval.metrics import (  # noqa: F401
                AnswerRelevancyMetric,
                FaithfulnessMetric,
                GEval,
            )

            # WHY threshold=0.5：初始阈值较低，随着模型改进逐步提高
            self._relevancy = AnswerRelevancyMetric(
                threshold=0.5, model=model
            )
            self._faithfulness = FaithfulnessMetric(
                threshold=0.5, model=model
            )

            # 自定义指标：大数据组件诊断准确性
            self._diagnosis_accuracy = GEval(
                name="BigDataDiagnosisAccuracy",
                criteria=(
                    "评估 AI 对大数据组件（HDFS/YARN/Kafka/ES/Impala）故障诊断的准确性。"
                    "考虑以下维度："
                    "1. 是否正确识别了故障组件"
                    "2. 根因分析是否指向了正确的方向"
                    "3. 使用的指标/日志证据是否有效"
                    "4. 修复建议是否适用于该组件"
                ),
                evaluation_params=[
                    "input",
                    "actual_output",
                    "expected_output",
                    "retrieval_context",
                ],
                model=model,
                threshold=0.5,
            )
            self._available = True
            logger.info("deepeval_available", model=model)

        except ImportError:
            logger.warning(
                "deepeval_not_installed",
                message="DeepEval 未安装，评估功能降级",
            )

    @property
    def available(self) -> bool:
        return self._available

    async def evaluate_diagnosis(self, cases: list[dict]) -> dict:
        """
        评估诊断质量.

        cases 格式：
        [{"query": "HDFS NameNode heap 95%",
          "diagnosis_result": "根因：NN JVM heap 不足...",
          "ground_truth": "NameNode JVM heap 内存不足",
          "contexts": ["NN heap: 95%", "GC time: 5s"]}]

        Returns:
            汇总结果字典
        """
        if not self._available:
            return self._fallback_evaluate(cases)

        try:
            from deepeval import evaluate as deepeval_evaluate
            from deepeval.test_case import LLMTestCase

            test_cases = []
            for case in cases:
                tc = LLMTestCase(
                    input=case["query"],
                    actual_output=case["diagnosis_result"],
                    expected_output=case.get("ground_truth", ""),
                    retrieval_context=case.get("contexts", []),
                )
                test_cases.append(tc)

            # 运行所有指标
            metrics = [
                self._relevancy,
                self._faithfulness,
                self._diagnosis_accuracy,
            ]
            results = deepeval_evaluate(test_cases, metrics)

            # 汇总
            summary: dict[str, Any] = {
                "total_cases": len(test_cases),
                "answer_relevancy": self._avg_score(
                    results, "AnswerRelevancyMetric"
                ),
                "faithfulness": self._avg_score(
                    results, "FaithfulnessMetric"
                ),
                "diagnosis_accuracy": self._avg_score(
                    results, "BigDataDiagnosisAccuracy"
                ),
                "per_case": [],
            }

            for i, tc_result in enumerate(results):
                per_case: dict[str, Any] = {"case_index": i}
                for metric_result in tc_result:
                    per_case[metric_result.metric_name] = {
                        "score": metric_result.score,
                        "passed": metric_result.success,
                        "reason": metric_result.reason,
                    }
                summary["per_case"].append(per_case)

            logger.info(
                "deepeval_completed",
                total=len(test_cases),
                relevancy=summary["answer_relevancy"],
                faithfulness=summary["faithfulness"],
                diagnosis_accuracy=summary["diagnosis_accuracy"],
            )
            return summary

        except Exception as e:
            logger.error("deepeval_evaluate_failed", error=str(e))
            return self._fallback_evaluate(cases)

    def _fallback_evaluate(self, cases: list[dict]) -> dict:
        """DeepEval 不可用时的降级评估."""
        scores: list[float] = []
        for case in cases:
            result_text = case.get("diagnosis_result", "").lower()
            gt_text = case.get("ground_truth", "").lower()

            if not gt_text:
                scores.append(0.5)  # 无 ground truth 给中间分
                continue

            # 简单关键词匹配
            gt_words = set(gt_text.split())
            result_words = set(result_text.split())
            overlap = len(gt_words & result_words) / max(len(gt_words), 1)
            scores.append(min(1.0, overlap))

        avg_score = sum(scores) / len(scores) if scores else 0.0

        return {
            "total_cases": len(cases),
            "answer_relevancy": avg_score,
            "faithfulness": avg_score,
            "diagnosis_accuracy": avg_score,
            "per_case": [],
            "_fallback": True,
        }

    @staticmethod
    def _avg_score(results: list, metric_name: str) -> float:
        """计算特定指标的平均分."""
        scores = []
        for tc_result in results:
            for mr in tc_result:
                if mr.metric_name == metric_name and mr.score is not None:
                    scores.append(mr.score)
        return sum(scores) / len(scores) if scores else 0.0
