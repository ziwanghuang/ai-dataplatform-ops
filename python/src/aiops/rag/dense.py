"""
DenseRetriever — Milvus 向量检索

双模式设计：
1. 生产模式：pymilvus MilvusClient + BGE-M3 Embedding → ANN 近邻检索
2. Mock 模式：内存关键词匹配（开发/测试/面试演示）

切换逻辑：
- 有 Milvus URI → 尝试连接 → 成功则用生产模式
- 连接失败或无 URI → 自动降级到 Mock
- 环境变量 RAG_MILVUS_URI 控制

WHY BGE-M3 而不是 OpenAI text-embedding-3：
- BGE-M3 本地部署，无 API 调用延迟（~10ms vs ~200ms）
- 1024 维向量，中英文混合效果 SOTA
- 免费，无 token 计费，适合高频检索场景
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from aiops.core.config import get_settings
from aiops.core.logging import get_logger
from aiops.rag.types import RetrievalResult

logger = get_logger(__name__)


# ──────────────────────────────────────────────────
# Mock 知识库——模拟 Milvus 中存储的运维文档
# ──────────────────────────────────────────────────

MOCK_DOCUMENTS: list[dict[str, Any]] = [
    {
        "content": "NameNode OOM 排查指南：当 NameNode heap 使用率持续超过 90% 时，首先检查 GC 日志确认 Full GC 频率。"
                   "如果 Full GC > 10次/分钟，建议增加 NameNode 堆内存（-Xmx）。同时检查是否有小文件过多导致元数据膨胀。",
        "source": "docs/sop/hdfs-namenode-oom.md",
        "metadata": {"component": "hdfs", "doc_type": "sop", "content_hash": "nn_oom_001"},
    },
    {
        "content": "HDFS 写入变慢排查：写入慢通常与 DataNode 磁盘 IO、网络带宽或 NameNode RPC 队列长度有关。"
                   "首先检查 NameNode RPC 处理延迟和队列长度，然后检查 DataNode 磁盘 IO util。",
        "source": "docs/sop/hdfs-write-slow.md",
        "metadata": {"component": "hdfs", "doc_type": "sop", "content_hash": "hdfs_slow_001"},
    },
    {
        "content": "Kafka 消费延迟处理：消费延迟（Consumer Lag）持续增长通常因为消费者处理速度跟不上生产者。"
                   "排查步骤：1. 检查消费者实例数和分区数是否匹配 2. 检查消费者 GC 暂停 3. 检查下游系统瓶颈。",
        "source": "docs/sop/kafka-consumer-lag.md",
        "metadata": {"component": "kafka", "doc_type": "sop", "content_hash": "kafka_lag_001"},
    },
    {
        "content": "YARN 队列资源不足：当队列 Pending 应用数持续增长，检查队列容量配置和实际资源使用。"
                   "常见原因：1. 队列最大容量设置过低 2. 有大作业独占资源 3. NodeManager 节点故障导致可用资源减少。",
        "source": "docs/sop/yarn-queue-full.md",
        "metadata": {"component": "yarn", "doc_type": "sop", "content_hash": "yarn_queue_001"},
    },
    {
        "content": "ES 集群 Red 状态处理：集群变 Red 说明有主分片未分配。步骤：1. GET _cluster/health?level=indices 确认哪个索引 Red "
                   "2. GET _cat/shards?v&h=index,shard,prirep,state,unassigned.reason 查看未分配原因 "
                   "3. 常见原因：磁盘空间不足、节点离线、分片分配规则冲突。",
        "source": "docs/sop/es-cluster-red.md",
        "metadata": {"component": "es", "doc_type": "sop", "content_hash": "es_red_001"},
    },
    {
        "content": "NameNode HA 故障转移：当 Active NameNode 宕机时，Standby NameNode 应在 30 秒内自动接管。"
                   "如果自动故障转移失败，检查 ZKFC 进程状态和 ZooKeeper 连接。手动故障转移命令：hdfs haadmin -failover nn1 nn2。",
        "source": "docs/sop/hdfs-namenode-ha-failover.md",
        "metadata": {"component": "hdfs", "doc_type": "sop", "content_hash": "nn_ha_001"},
    },
    {
        "content": "dfs.namenode.handler.count 配置说明：控制 NameNode 处理 RPC 请求的线程数。默认值 10，"
                   "大集群（100+ DataNode）建议设置为 20-40。过低会导致 RPC 队列积压，客户端超时。"
                   "修改后需要重启 NameNode 生效。",
        "source": "docs/reference/hdfs-config.md",
        "metadata": {"component": "hdfs", "doc_type": "reference", "content_hash": "hdfs_config_001"},
    },
    {
        "content": "历史案例：2025-12-15 NameNode Full GC 导致集群写入阻塞。根因：NameNode heap 4GB 不够（管理 800 万块）。"
                   "解决：增加到 8GB 后恢复正常。建议：每 500 万块对应 4GB heap。",
        "source": "docs/incidents/2025-12-15-nn-gc.md",
        "metadata": {"component": "hdfs", "doc_type": "incident", "content_hash": "incident_nn_gc_001"},
    },
]


class DenseRetriever:
    """
    Milvus 向量检索 — 双模式（生产 + Mock）.

    生产模式：
    - pymilvus MilvusClient 连接 Milvus 2.x
    - SentenceTransformers BGE-M3 做 Embedding（1024 维）
    - IVF_FLAT 索引，nprobe=16
    - FP16 精度，延迟 ~50ms/query

    Mock 模式：
    - 关键词匹配模拟语义搜索
    - 不依赖 Milvus 和 GPU

    WHY 在 __init__ 时不立即连接：
    - 延迟连接（lazy connect），第一次 search 时才建立连接
    - 避免应用启动慢（Milvus 连接 ~500ms）
    - 如果整个请求不需要 RAG，完全不连接
    """

    def __init__(
        self,
        milvus_uri: str = "",
        collection: str = "",
        embedding_model: str = "",
    ) -> None:
        settings = get_settings()
        self._milvus_uri = milvus_uri or settings.rag.milvus_uri
        self._collection = collection or settings.rag.milvus_collection
        self._embedding_model_name = embedding_model or settings.rag.embedding_model

        # 延迟初始化的客户端
        self._milvus_client: Any = None
        self._embed_model: Any = None
        self._use_mock: bool = not self._milvus_uri
        self._connected: bool = False

    async def search(
        self,
        query: str,
        top_k: int = 20,
        collection: str = "ops_documents",
        filter_expr: str = "",
    ) -> list[RetrievalResult]:
        """
        向量检索——优先用 Milvus，失败降级到 Mock.

        流程：
        1. 确保连接（lazy connect）
        2. Embedding: query → 1024 维向量
        3. Milvus ANN 检索
        4. 结果转换为 RetrievalResult
        """
        if self._use_mock:
            return await self._mock_search(query, top_k)

        try:
            return await self._milvus_search(query, top_k, collection, filter_expr)
        except Exception as e:
            logger.warning(
                "dense_milvus_failed_fallback_mock",
                error=str(e),
                query=query[:80],
            )
            return await self._mock_search(query, top_k)

    async def _ensure_connected(self) -> None:
        """延迟连接 Milvus + 加载 Embedding 模型."""
        if self._connected:
            return

        try:
            from pymilvus import MilvusClient

            self._milvus_client = MilvusClient(uri=self._milvus_uri)

            # 检查集合是否存在
            if not self._milvus_client.has_collection(self._collection):
                logger.warning(
                    "milvus_collection_not_found",
                    collection=self._collection,
                    msg="Will use mock retrieval",
                )
                self._use_mock = True
                return

            logger.info(
                "milvus_connected",
                uri=self._milvus_uri,
                collection=self._collection,
            )
        except Exception as e:
            logger.warning("milvus_connect_failed", error=str(e))
            self._use_mock = True
            return

        # 加载 Embedding 模型
        try:
            from sentence_transformers import SentenceTransformer

            self._embed_model = SentenceTransformer(self._embedding_model_name)
            logger.info("embedding_model_loaded", model=self._embedding_model_name)
        except Exception as e:
            logger.warning("embedding_model_load_failed", error=str(e))
            self._use_mock = True
            return

        self._connected = True

    async def _milvus_search(
        self,
        query: str,
        top_k: int,
        collection: str,
        filter_expr: str,
    ) -> list[RetrievalResult]:
        """
        Milvus 生产检索.

        步骤：
        1. model.encode(query) → 1024 维向量
        2. milvus.search(embedding, top_k, filter_expr) → ANN 近邻
        3. 结果转换

        WHY nprobe=16：
        - IVF_FLAT 的 nprobe 控制搜索精度 vs 速度
        - nprobe=1: 最快但 recall 低
        - nprobe=16: recall ~98%，延迟 ~50ms（我们的数据量下）
        - nprobe=64: recall ~99.5% 但延迟翻倍，不值得
        """
        await self._ensure_connected()

        if self._use_mock:
            return await self._mock_search(query, top_k)

        start = time.monotonic()

        # 1. Embedding
        query_embedding = self._embed_model.encode(query, normalize_embeddings=True).tolist()

        # 2. Milvus 检索
        search_params = {"metric_type": "COSINE", "params": {"nprobe": 16}}
        target_collection = collection or self._collection

        results = self._milvus_client.search(
            collection_name=target_collection,
            data=[query_embedding],
            limit=top_k,
            output_fields=["content", "source", "component", "doc_type", "content_hash"],
            search_params=search_params,
            filter=filter_expr or None,
        )

        # 3. 结果转换
        retrieval_results: list[RetrievalResult] = []
        for hits in results:
            for hit in hits:
                entity = hit.get("entity", {})
                retrieval_results.append(
                    RetrievalResult(
                        content=entity.get("content", ""),
                        source=entity.get("source", ""),
                        score=hit.get("distance", 0.0),
                        metadata={
                            "component": entity.get("component", ""),
                            "doc_type": entity.get("doc_type", ""),
                            "content_hash": entity.get("content_hash", ""),
                        },
                    )
                )

        duration_ms = int((time.monotonic() - start) * 1000)
        logger.info(
            "dense_milvus_search_done",
            collection=target_collection,
            results=len(retrieval_results),
            duration_ms=duration_ms,
        )

        return retrieval_results

    async def _mock_search(
        self,
        query: str,
        top_k: int,
    ) -> list[RetrievalResult]:
        """
        Mock 向量检索——用关键词匹配近似语义相似度.

        WHY 保留 Mock：
        - 开发/测试不需要启动 Milvus 容器
        - 单元测试需要确定性结果
        - 面试演示时不依赖外部基础设施
        """
        start = time.monotonic()
        query_lower = query.lower()

        scored: list[tuple[float, dict[str, Any]]] = []
        for doc in MOCK_DOCUMENTS:
            content_lower = doc["content"].lower()
            source_lower = doc["source"].lower()
            query_words = set(query_lower.split())
            content_words = set(content_lower.split())
            overlap = len(query_words & content_words)
            source_bonus = sum(1 for w in query_words if w in source_lower) * 0.5
            score = (overlap + source_bonus) / max(len(query_words), 1)
            if score > 0:
                scored.append((score, doc))

        scored.sort(key=lambda x: x[0], reverse=True)

        results = [
            RetrievalResult(
                content=doc["content"],
                source=doc["source"],
                score=min(score, 1.0),
                metadata=dict(doc["metadata"]),
            )
            for score, doc in scored[:top_k]
        ]

        duration_ms = int((time.monotonic() - start) * 1000)
        logger.debug("dense_mock_search_done", results=len(results), duration_ms=duration_ms)

        return results

    async def close(self) -> None:
        """关闭 Milvus 连接."""
        if self._milvus_client:
            try:
                self._milvus_client.close()
                logger.info("milvus_client_closed")
            except Exception as e:
                logger.warning("milvus_close_error", error=str(e))
            finally:
                self._milvus_client = None
                self._connected = False
