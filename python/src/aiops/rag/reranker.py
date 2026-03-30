"""
CrossEncoderReranker — Cross-Encoder 精排（Mock 实现）

生产版本使用 BGE-Reranker-v2-m3 对 (query, doc) 对直接打分。
比 bi-encoder 更准确（但更慢），所以只对 RRF 融合后的 top-20 精排。

性能：20 个候选重排 ~200ms（GPU）/ ~500ms（CPU）

Mock 版本：保持输入顺序（即 RRF 分数排序），只做 top_k 截断。
"""

from __future__ import annotations

from typing import Any

from aiops.core.logging import get_logger
from aiops.rag.types import RetrievalResult

logger = get_logger(__name__)


class CrossEncoderReranker:
    """
    Cross-Encoder 重排序器（Mock 实现）.

    生产版本：
    - sentence_transformers.CrossEncoder("BAAI/bge-reranker-v2-m3")
    - 对每个 (query, doc.content) 对打分
    - 按分数降序取 top_k
    """

    async def rerank(
        self,
        query: str,
        candidates: list[RetrievalResult],
        top_k: int = 5,
    ) -> list[RetrievalResult]:
        """
        重排序候选结果.

        Mock 版本直接按现有分数排序取 top_k。
        生产版本会用 CrossEncoder 重新打分。
        """
        # Mock: 已经按 RRF 分数排序，直接截断
        reranked = sorted(candidates, key=lambda r: r.score, reverse=True)[:top_k]

        logger.debug(
            "rerank_done",
            candidates=len(candidates),
            returned=len(reranked),
            query=query[:60],
        )

        return reranked
