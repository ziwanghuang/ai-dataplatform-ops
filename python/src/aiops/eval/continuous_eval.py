"""
持续评估流水线 — 每日自动评估生产质量

WHY 每日评估而不是实时评估：
- 实时评估成本太高（每次诊断都跑 RAGAS → $$$）
- 每日评估用前一天的生产数据做离线评估
- 足够发现趋势性退化，又不会增加线上延迟

评估数据来源：
- 从 LangFuse 拉取前一天的 trace
- 用 trace 中的 input/output/contexts 构建评估用例
- RAGAS 评估 RAG 质量 + LLM-as-Judge 评估诊断质量
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from prometheus_client import Gauge

from aiops.core.logging import get_logger

logger = get_logger(__name__)

# 持续评估指标（Prometheus Gauge，可升可降）
DAILY_FAITHFULNESS = Gauge(
    "aiops_daily_eval_faithfulness",
    "Daily evaluation faithfulness score",
)
DAILY_RELEVANCY = Gauge(
    "aiops_daily_eval_relevancy",
    "Daily evaluation answer relevancy score",
)
DAILY_JUDGE_SCORE = Gauge(
    "aiops_daily_eval_judge_weighted",
    "Daily LLM-as-Judge weighted score",
)
QUALITY_TREND_ALERT = Gauge(
    "aiops_quality_trend_alert",
    "1 if quality is trending down, 0 otherwise",
)


class ContinuousEvaluator:
    """
    持续评估流水线.

    每日从 LangFuse 拉取 trace → RAGAS + LLM Judge 评估 → 趋势分析。
    """

    def __init__(
        self,
        langfuse_client: Any = None,
        ragas_evaluator: Any = None,
        llm_judge: Any = None,
        results_dir: str = "eval/results/daily",
    ) -> None:
        self._langfuse = langfuse_client
        self._ragas = ragas_evaluator
        self._judge = llm_judge
        self._results_dir = Path(results_dir)
        self._results_dir.mkdir(parents=True, exist_ok=True)

    async def run_daily_eval(
        self, target_date: datetime | None = None
    ) -> dict:
        """
        运行每日评估.

        Args:
            target_date: 目标日期，默认评估昨天的数据。支持回填。
        """
        if target_date is None:
            target_date = datetime.now(timezone.utc) - timedelta(days=1)

        date_str = target_date.strftime("%Y-%m-%d")
        logger.info("daily_eval_started", date=date_str)

        # 1. 从 LangFuse 拉取 traces
        traces = await self._fetch_traces(target_date)
        if not traces:
            logger.info("daily_eval_no_traces", date=date_str)
            return {"date": date_str, "status": "no_data"}

        # 2. 构建评估用例
        eval_cases = self._build_eval_cases(traces)

        # 3. RAGAS 评估（如果可用）
        ragas_scores: dict[str, float | None] = {
            "faithfulness": None,
            "answer_relevancy": None,
        }
        if self._ragas and eval_cases.get("rag_cases"):
            try:
                ragas_result = await self._ragas.evaluate(
                    eval_cases["rag_cases"]
                )
                ragas_scores["faithfulness"] = ragas_result.avg_faithfulness
                ragas_scores["answer_relevancy"] = (
                    ragas_result.avg_answer_relevance
                )
                DAILY_FAITHFULNESS.set(ragas_result.avg_faithfulness)
                DAILY_RELEVANCY.set(ragas_result.avg_answer_relevance)
            except Exception as e:
                logger.warning("daily_ragas_failed", error=str(e))

        # 4. LLM-as-Judge 评估（抽样）
        judge_avg: float | None = None
        judge_sample_size = 0
        diagnosis_cases = eval_cases.get("diagnosis_cases", [])
        if self._judge and diagnosis_cases:
            try:
                # WHY 抽样 30 条：成本控制，30 条足够发现趋势
                sample = diagnosis_cases[:30]
                judge_results = await self._judge.judge_batch(sample)
                valid_results = [
                    jr for jr in judge_results if jr.scores is not None
                ]
                if valid_results:
                    judge_avg = sum(
                        jr.weighted_score for jr in valid_results
                    ) / len(valid_results)
                    judge_sample_size = len(valid_results)
                    DAILY_JUDGE_SCORE.set(judge_avg)
            except Exception as e:
                logger.warning("daily_judge_failed", error=str(e))

        # 5. 趋势分析
        trend = self._analyze_trend(date_str)

        # 6. 保存结果
        report = {
            "date": date_str,
            "status": "completed",
            "trace_count": len(traces),
            "eval_case_count": sum(len(v) for v in eval_cases.values()),
            "ragas": ragas_scores,
            "llm_judge": {
                "avg_weighted_score": judge_avg,
                "sample_size": judge_sample_size,
            },
            "trend": trend,
        }

        output_path = self._results_dir / f"{date_str}.json"
        with open(output_path, "w") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

        logger.info("daily_eval_completed", date=date_str, traces=len(traces))
        return report

    async def _fetch_traces(self, target_date: datetime) -> list[dict]:
        """从 LangFuse 拉取指定日期的 traces."""
        if not self._langfuse:
            logger.debug("langfuse_not_configured")
            return []

        start = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)

        try:
            traces = self._langfuse.get_traces(
                from_timestamp=start,
                to_timestamp=end,
                limit=500,  # 每天最多评估 500 条
            )
            return [
                t.dict() if hasattr(t, "dict") else t for t in traces
            ]
        except Exception as e:
            logger.error("langfuse_fetch_failed", error=str(e))
            return []

    def _build_eval_cases(self, traces: list[dict]) -> dict:
        """从 traces 构建评估用例."""
        rag_cases: list[dict] = []
        diagnosis_cases: list[dict] = []

        for trace in traces:
            input_data = trace.get("input", {})
            output_data = trace.get("output", {})
            metadata = trace.get("metadata", {})

            # RAG 评估用例
            if metadata.get("contexts"):
                from aiops.eval.ragas_eval import RAGEvalSample

                rag_cases.append(
                    RAGEvalSample(
                        id=trace.get("id", ""),
                        question=input_data.get("query", ""),
                        answer=output_data.get("root_cause", ""),
                        contexts=metadata["contexts"],
                        ground_truth=metadata.get("ground_truth", ""),
                    )
                )

            # 诊断评估用例（for LLM Judge）
            if output_data.get("root_cause"):
                diagnosis_cases.append(
                    {
                        "case_id": trace.get("id", ""),
                        "query": input_data.get("query", ""),
                        "diagnosis_result": output_data.get("root_cause", ""),
                        "evidence": output_data.get("evidence", []),
                        "ground_truth": metadata.get("ground_truth", ""),
                    }
                )

        return {"rag_cases": rag_cases, "diagnosis_cases": diagnosis_cases}

    def _analyze_trend(self, current_date: str) -> dict:
        """
        评估趋势分析（week-over-week）.

        WHY week-over-week 而不是 day-over-day：
        - 单日波动太大（样本量不同、工作日/周末差异）
        - 周粒度平滑了波动，更能反映真实趋势
        """
        recent_results: list[dict] = []
        for i in range(14):
            date = (
                datetime.strptime(current_date, "%Y-%m-%d") - timedelta(days=i)
            )
            result_file = self._results_dir / f"{date.strftime('%Y-%m-%d')}.json"
            if result_file.exists():
                with open(result_file) as f:
                    recent_results.append(json.load(f))

        if len(recent_results) < 7:
            return {"status": "insufficient_data", "days_available": len(recent_results)}

        # 本周 vs 上周平均
        this_week = recent_results[:7]
        last_week = recent_results[7:14]

        def avg_metric(results: list[dict], path: str) -> float | None:
            """提取嵌套指标平均值."""
            values = []
            for r in results:
                parts = path.split(".")
                val = r
                for p in parts:
                    if isinstance(val, dict):
                        val = val.get(p)
                    else:
                        val = None
                        break
                if val is not None:
                    values.append(float(val))
            return sum(values) / len(values) if values else None

        this_faith = avg_metric(this_week, "ragas.faithfulness")
        last_faith = avg_metric(last_week, "ragas.faithfulness")
        this_judge = avg_metric(this_week, "llm_judge.avg_weighted_score")
        last_judge = avg_metric(last_week, "llm_judge.avg_weighted_score")

        # 退化检测
        trending_down = False
        if this_faith is not None and last_faith is not None:
            if this_faith < last_faith * 0.9:  # 10% 退化
                trending_down = True
        if this_judge is not None and last_judge is not None:
            if this_judge < last_judge * 0.9:
                trending_down = True

        QUALITY_TREND_ALERT.set(1 if trending_down else 0)

        return {
            "status": "analyzed",
            "this_week_faithfulness": this_faith,
            "last_week_faithfulness": last_faith,
            "this_week_judge_score": this_judge,
            "last_week_judge_score": last_judge,
            "trending_down": trending_down,
        }
