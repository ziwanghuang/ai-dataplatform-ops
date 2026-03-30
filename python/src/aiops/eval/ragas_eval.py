"""
RAGAS 评估集成 — RAG 质量评估

WHY RAGAS：
- RAG 评估的行业标准框架
- 提供 4 个核心指标：Faithfulness / Relevance / Context Recall / Answer Correctness
- 自动化评估，不需要人工标注

评估指标（参考 18-评估体系与质量门禁.md §3.2）：
1. Faithfulness: 回答是否基于检索到的上下文（防幻觉）
2. Answer Relevance: 回答是否与问题相关
3. Context Relevance: 检索的上下文是否与问题相关
4. Context Recall: 检索是否覆盖了所有需要的信息
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from aiops.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class RAGEvalSample:
    """单个 RAG 评估样本."""
    id: str
    question: str
    answer: str                    # Agent 生成的回答
    contexts: list[str]            # 检索到的上下文
    ground_truth: str = ""         # 标准答案（用于 context recall）
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RAGEvalScore:
    """单个样本的评估分数."""
    sample_id: str
    faithfulness: float = 0.0      # 是否忠于上下文 (0-1)
    answer_relevance: float = 0.0  # 回答是否相关 (0-1)
    context_relevance: float = 0.0 # 上下文是否相关 (0-1)
    context_recall: float = 0.0    # 上下文覆盖率 (0-1)
    overall: float = 0.0           # 综合得分


@dataclass
class RAGEvalReport:
    """RAG 评估报告."""
    total_samples: int
    scores: list[RAGEvalScore]
    avg_faithfulness: float = 0.0
    avg_answer_relevance: float = 0.0
    avg_context_relevance: float = 0.0
    avg_context_recall: float = 0.0
    avg_overall: float = 0.0
    duration_ms: int = 0


class RAGASEvaluator:
    """
    RAGAS 评估器.

    职责：
    1. 收集 Agent 的 RAG 输入/输出数据
    2. 调用 RAGAS 框架评估
    3. 聚合和报告
    4. 依赖不存在时降级为规则评估

    WHY 封装 RAGAS 而不是直接调用：
    - RAGAS 是可选依赖（CI 环境可能不安装）
    - 需要适配 AIOps 特定的评估场景
    - 降级方案：RAGAS 不可用时用简单的关键词匹配评估
    """

    def __init__(self, use_ragas: bool = True) -> None:
        self._use_ragas = use_ragas
        self._ragas_available = False

        if use_ragas:
            try:
                import ragas  # noqa: F401
                self._ragas_available = True
                logger.info("ragas_available")
            except ImportError:
                logger.warning("ragas_not_installed", fallback="rule_based")

    async def evaluate(
        self,
        samples: list[RAGEvalSample],
    ) -> RAGEvalReport:
        """
        评估 RAG 质量.

        如果 RAGAS 可用，使用 RAGAS 评估。
        否则降级为规则评估。
        """
        start = time.monotonic()

        if self._ragas_available:
            scores = await self._ragas_evaluate(samples)
        else:
            scores = self._rule_based_evaluate(samples)

        # 计算平均值
        n = len(scores)
        report = RAGEvalReport(
            total_samples=n,
            scores=scores,
            avg_faithfulness=sum(s.faithfulness for s in scores) / n if n else 0,
            avg_answer_relevance=sum(s.answer_relevance for s in scores) / n if n else 0,
            avg_context_relevance=sum(s.context_relevance for s in scores) / n if n else 0,
            avg_context_recall=sum(s.context_recall for s in scores) / n if n else 0,
            avg_overall=sum(s.overall for s in scores) / n if n else 0,
            duration_ms=int((time.monotonic() - start) * 1000),
        )

        logger.info(
            "rag_eval_complete",
            samples=n,
            avg_faithfulness=f"{report.avg_faithfulness:.2f}",
            avg_relevance=f"{report.avg_answer_relevance:.2f}",
            avg_overall=f"{report.avg_overall:.2f}",
        )

        return report

    async def _ragas_evaluate(
        self,
        samples: list[RAGEvalSample],
    ) -> list[RAGEvalScore]:
        """使用 RAGAS 框架评估."""
        try:
            from ragas import evaluate as ragas_evaluate
            from ragas.metrics import (
                answer_relevancy,
                context_precision,
                context_recall,
                faithfulness,
            )
            from datasets import Dataset

            # 构建 RAGAS Dataset
            data = {
                "question": [s.question for s in samples],
                "answer": [s.answer for s in samples],
                "contexts": [s.contexts for s in samples],
                "ground_truth": [s.ground_truth for s in samples],
            }
            dataset = Dataset.from_dict(data)

            # 运行评估
            result = ragas_evaluate(
                dataset=dataset,
                metrics=[
                    faithfulness,
                    answer_relevancy,
                    context_precision,
                    context_recall,
                ],
            )

            # 转换结果
            scores: list[RAGEvalScore] = []
            df = result.to_pandas()
            for i, sample in enumerate(samples):
                row = df.iloc[i] if i < len(df) else {}
                score = RAGEvalScore(
                    sample_id=sample.id,
                    faithfulness=float(row.get("faithfulness", 0)),
                    answer_relevance=float(row.get("answer_relevancy", 0)),
                    context_relevance=float(row.get("context_precision", 0)),
                    context_recall=float(row.get("context_recall", 0)),
                )
                score.overall = (
                    score.faithfulness * 0.3 +
                    score.answer_relevance * 0.3 +
                    score.context_relevance * 0.2 +
                    score.context_recall * 0.2
                )
                scores.append(score)

            return scores

        except Exception as e:
            logger.error("ragas_evaluate_failed", error=str(e))
            return self._rule_based_evaluate(samples)

    def _rule_based_evaluate(
        self,
        samples: list[RAGEvalSample],
    ) -> list[RAGEvalScore]:
        """
        规则评估（RAGAS 不可用时的降级方案）.

        评估逻辑：
        - Faithfulness: 回答中的关键词是否出现在上下文中
        - Relevance: 问题中的关键词是否出现在回答中
        - Context: 上下文中的关键词是否与问题相关
        """
        scores: list[RAGEvalScore] = []

        for sample in samples:
            # 关键词提取（简化版）
            question_words = set(sample.question.lower().split())
            answer_words = set(sample.answer.lower().split())
            context_words = set(
                word
                for ctx in sample.contexts
                for word in ctx.lower().split()
            )

            # Faithfulness: 回答词在上下文中的占比
            if answer_words:
                faithfulness = len(answer_words & context_words) / len(answer_words)
            else:
                faithfulness = 0.0

            # Answer Relevance: 问题词在回答中的占比
            if question_words:
                relevance = len(question_words & answer_words) / len(question_words)
            else:
                relevance = 0.0

            # Context Relevance: 问题词在上下文中的占比
            if question_words:
                ctx_relevance = len(question_words & context_words) / len(question_words)
            else:
                ctx_relevance = 0.0

            # Context Recall
            if sample.ground_truth:
                gt_words = set(sample.ground_truth.lower().split())
                recall = len(gt_words & context_words) / len(gt_words) if gt_words else 0
            else:
                recall = ctx_relevance  # 没有标准答案时用 context relevance 代替

            overall = faithfulness * 0.3 + relevance * 0.3 + ctx_relevance * 0.2 + recall * 0.2

            scores.append(RAGEvalScore(
                sample_id=sample.id,
                faithfulness=min(1.0, faithfulness),
                answer_relevance=min(1.0, relevance),
                context_relevance=min(1.0, ctx_relevance),
                context_recall=min(1.0, recall),
                overall=min(1.0, overall),
            ))

        return scores
