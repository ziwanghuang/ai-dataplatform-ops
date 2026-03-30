"""
SparseRetriever — Elasticsearch BM25 全文检索

双模式设计：
1. 生产模式：AsyncElasticsearch + ik_smart 中文分词 + multi_match + function_score
2. Mock 模式：关键词子串匹配（不依赖 ES）

WHY AsyncElasticsearch 而不是同步版：
- 与 Dense 并行执行时不阻塞事件循环
- asyncio.gather(dense, sparse) 需要两路都是 async

WHY ik_smart 而不是 ik_max_word：
- ik_max_word 产生过多短 token，BM25 噪音大
- ik_smart 切分更合理，如 "NameNode 堆内存" → ["NameNode", "堆内存"]
- 运维文档以精确术语为主，ik_smart 足够
"""

from __future__ import annotations

import time
from typing import Any

from aiops.core.config import get_settings
from aiops.core.logging import get_logger
from aiops.rag.dense import MOCK_DOCUMENTS
from aiops.rag.types import RetrievalResult

logger = get_logger(__name__)


class SparseRetriever:
    """
    ES BM25 检索 — 双模式（生产 + Mock）.

    生产模式：
    - AsyncElasticsearch 连接 ES 集群
    - multi_match: content^1, source^0.5（source 权重低，避免路径噪音）
    - ik_smart 中文分词
    - function_score 时间衰减（最近的文档更相关）

    Mock 模式：
    - 精确子串匹配模拟 BM25
    - 比 Dense 更偏向精确关键词匹配
    """

    def __init__(
        self,
        es_url: str = "",
        es_index: str = "",
    ) -> None:
        settings = get_settings()
        self._es_url = es_url or settings.rag.es_url
        self._es_index = es_index or settings.rag.es_index

        # 延迟初始化
        self._es_client: Any = None
        self._use_mock: bool = not self._es_url
        self._connected: bool = False

    async def search(
        self,
        query: str,
        top_k: int = 20,
        index: str | None = None,
    ) -> list[RetrievalResult]:
        """
        BM25 检索——优先用 ES，失败降级到 Mock.

        生产流程：
        1. multi_match 查询（content, source）
        2. ik_smart 中文分词
        3. BM25 打分
        4. 结果转换为 RetrievalResult
        """
        if self._use_mock:
            return await self._mock_search(query, top_k)

        try:
            return await self._es_search(query, top_k, index)
        except Exception as e:
            logger.warning(
                "sparse_es_failed_fallback_mock",
                error=str(e),
                query=query[:80],
            )
            return await self._mock_search(query, top_k)

    async def _ensure_connected(self) -> None:
        """延迟连接 ES."""
        if self._connected:
            return

        try:
            from elasticsearch import AsyncElasticsearch

            self._es_client = AsyncElasticsearch(
                hosts=[self._es_url],
                request_timeout=10,
                retry_on_timeout=True,
                max_retries=2,
            )

            # 健康检查
            info = await self._es_client.info()
            logger.info(
                "es_connected",
                url=self._es_url,
                version=info.get("version", {}).get("number", "unknown"),
            )
            self._connected = True

        except Exception as e:
            logger.warning("es_connect_failed", error=str(e))
            self._use_mock = True
            if self._es_client:
                await self._es_client.close()
                self._es_client = None

    async def _es_search(
        self,
        query: str,
        top_k: int,
        index: str | None,
    ) -> list[RetrievalResult]:
        """
        ES 生产检索.

        WHY multi_match 而不是 match：
        - 需要同时搜索 content 和 source 字段
        - content 权重 1.0，source 权重 0.5
        - best_fields 类型：取最佳匹配字段的分数（避免分数膨胀）

        WHY 不用 function_score 时间衰减（当前阶段）：
        - 知识库文档更新频率低，时间衰减收益小
        - 保持查询简单，后续按需添加
        """
        await self._ensure_connected()

        if self._use_mock:
            return await self._mock_search(query, top_k)

        start = time.monotonic()
        target_index = index or self._es_index

        # 构建查询
        body: dict[str, Any] = {
            "query": {
                "multi_match": {
                    "query": query,
                    "fields": ["content", "source^0.5"],
                    "type": "best_fields",
                    "analyzer": "ik_smart",
                }
            },
            "size": top_k,
            "_source": ["content", "source", "component", "doc_type", "content_hash"],
        }

        response = await self._es_client.search(
            index=target_index,
            body=body,
        )

        # 结果转换
        results: list[RetrievalResult] = []
        hits = response.get("hits", {}).get("hits", [])
        for hit in hits:
            src = hit.get("_source", {})
            results.append(
                RetrievalResult(
                    content=src.get("content", ""),
                    source=src.get("source", ""),
                    score=hit.get("_score", 0.0),
                    metadata={
                        "component": src.get("component", ""),
                        "doc_type": src.get("doc_type", ""),
                        "content_hash": src.get("content_hash", ""),
                    },
                )
            )

        duration_ms = int((time.monotonic() - start) * 1000)
        logger.info(
            "sparse_es_search_done",
            index=target_index,
            results=len(results),
            duration_ms=duration_ms,
        )

        return results

    async def _mock_search(
        self,
        query: str,
        top_k: int,
    ) -> list[RetrievalResult]:
        """
        Mock BM25 检索——精确子串匹配.

        WHY 精确匹配而不是分词匹配：
        - 模拟 BM25 对精确术语（配置参数名、错误码）的优势
        - 与 Dense 的模糊语义匹配形成互补
        """
        start = time.monotonic()
        query_lower = query.lower()

        scored: list[tuple[float, dict[str, Any]]] = []
        for doc in MOCK_DOCUMENTS:
            content_lower = doc["content"].lower()
            score = 0.0
            for word in query_lower.split():
                if len(word) >= 2:
                    if word in content_lower:
                        score += 2.0
                    if word in doc["source"].lower():
                        score += 1.0
            if score > 0:
                scored.append((score, doc))

        scored.sort(key=lambda x: x[0], reverse=True)

        results = [
            RetrievalResult(
                content=doc["content"],
                source=doc["source"],
                score=score,
                metadata=dict(doc["metadata"]),
            )
            for score, doc in scored[:top_k]
        ]

        duration_ms = int((time.monotonic() - start) * 1000)
        logger.debug("sparse_mock_search_done", results=len(results), duration_ms=duration_ms)

        return results

    async def close(self) -> None:
        """关闭 ES 连接."""
        if self._es_client:
            try:
                await self._es_client.close()
                logger.info("es_client_closed")
            except Exception as e:
                logger.warning("es_close_error", error=str(e))
            finally:
                self._es_client = None
                self._connected = False
