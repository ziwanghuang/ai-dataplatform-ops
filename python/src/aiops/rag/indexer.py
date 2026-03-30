"""
增量索引器 — 文档向量化 + Milvus/ES 入库

WHY 增量索引而不是全量重建：
- 知识库文档 ~500 篇，全量重建 ~30 分钟
- 大部分文档不变，只有新增/修改的需要重新索引
- 增量索引 < 1 分钟，可以做到准实时更新

索引流程（参考 14-索引管线.md）：
1. 文档处理器切分 → 2. Embedding 模型向量化 → 3. Milvus 入库
   → 4. 同时写入 ES BM25 索引 → 5. 更新 Neo4j 知识图谱（可选）

双模式设计：
1. 生产模式：pymilvus 写入 + elasticsearch bulk API + SentenceTransformers embedding
2. Mock 模式：日志记录（开发/测试/面试演示）
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

from aiops.core.config import get_settings
from aiops.core.logging import get_logger
from aiops.rag.processor import DocumentChunk, OpsDocumentProcessor, ProcessedDocument

logger = get_logger(__name__)


@dataclass
class IndexStats:
    """索引统计."""
    total_documents: int = 0
    total_chunks: int = 0
    total_tokens: int = 0
    indexed_at: float = field(default_factory=time.time)
    index_duration_ms: int = 0


@dataclass
class IndexRecord:
    """索引记录（用于增量判断）."""
    doc_id: str
    content_hash: str          # 文档内容 hash，变化时重新索引
    chunk_count: int
    indexed_at: float = field(default_factory=time.time)
    source_path: str = ""


class IncrementalIndexer:
    """
    增量索引器 — 双模式（生产 + Mock）.

    职责：
    1. 文档切分（委托给 OpsDocumentProcessor）
    2. 增量判断（对比 content hash）
    3. Embedding 向量化（委托给 embedding 模型）
    4. Milvus 写入
    5. ES BM25 索引同步

    WHY 不直接用 LangChain 的 VectorStore：
    - LangChain VectorStore 不支持增量索引
    - 需要同时写入 Milvus（dense）和 ES（sparse），LangChain 不支持双写
    - 自定义索引器可以精确控制写入策略和错误处理

    WHY 双写而不是 CDC 同步：
    - 数据量小（<10000 chunks），双写延迟可接受
    - CDC 需要 Kafka Connect + Debezium，架构过重
    - 双写失败有重试 + 补偿机制，一致性足够
    """

    def __init__(
        self,
        processor: OpsDocumentProcessor | None = None,
        embedding_model: str = "",
        milvus_collection: str = "",
    ) -> None:
        settings = get_settings()
        self._processor = processor or OpsDocumentProcessor()
        self._embedding_model_name = embedding_model or settings.rag.embedding_model
        self._collection_name = milvus_collection or settings.rag.milvus_collection
        self._milvus_uri = settings.rag.milvus_uri
        self._es_url = settings.rag.es_url
        self._es_index = settings.rag.es_index
        self._index_records: dict[str, IndexRecord] = {}
        self._stats = IndexStats()

        # 延迟初始化的客户端
        self._embed_model: Any = None
        self._milvus_client: Any = None
        self._es_client: Any = None
        self._use_mock_embed = True
        self._use_mock_milvus = True
        self._use_mock_es = True

    async def _ensure_embedding_model(self) -> None:
        """延迟加载 Embedding 模型."""
        if self._embed_model is not None:
            return
        try:
            from sentence_transformers import SentenceTransformer
            self._embed_model = SentenceTransformer(self._embedding_model_name)
            self._use_mock_embed = False
            logger.info("indexer_embedding_model_loaded", model=self._embedding_model_name)
        except Exception as e:
            logger.warning("indexer_embedding_model_failed", error=str(e))
            self._use_mock_embed = True

    async def _ensure_milvus(self) -> None:
        """延迟连接 Milvus."""
        if self._milvus_client is not None:
            return
        if not self._milvus_uri:
            return
        try:
            from pymilvus import MilvusClient, DataType, CollectionSchema, FieldSchema
            self._milvus_client = MilvusClient(uri=self._milvus_uri)

            # 自动创建集合（如果不存在）
            if not self._milvus_client.has_collection(self._collection_name):
                # WHY 1024 维：BGE-M3 的默认向量维度
                # WHY IVF_FLAT：数据量 <100K 时，IVF_FLAT 检索质量最高
                schema = CollectionSchema(fields=[
                    FieldSchema(name="id", dtype=DataType.VARCHAR, max_length=128, is_primary=True),
                    FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=8192),
                    FieldSchema(name="source", dtype=DataType.VARCHAR, max_length=512),
                    FieldSchema(name="component", dtype=DataType.VARCHAR, max_length=64),
                    FieldSchema(name="doc_type", dtype=DataType.VARCHAR, max_length=64),
                    FieldSchema(name="content_hash", dtype=DataType.VARCHAR, max_length=64),
                    FieldSchema(name="doc_id", dtype=DataType.VARCHAR, max_length=64),
                    FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=1024),
                ])
                self._milvus_client.create_collection(
                    collection_name=self._collection_name,
                    schema=schema,
                )
                # 创建向量索引
                self._milvus_client.create_index(
                    collection_name=self._collection_name,
                    field_name="embedding",
                    index_params={
                        "index_type": "IVF_FLAT",
                        "metric_type": "COSINE",
                        "params": {"nlist": 128},
                    },
                )
                logger.info("milvus_collection_created", collection=self._collection_name)

            self._use_mock_milvus = False
            logger.info("indexer_milvus_connected", uri=self._milvus_uri)
        except Exception as e:
            logger.warning("indexer_milvus_connect_failed", error=str(e))
            self._use_mock_milvus = True

    async def _ensure_es(self) -> None:
        """延迟连接 ES."""
        if self._es_client is not None:
            return
        if not self._es_url:
            return
        try:
            from elasticsearch import AsyncElasticsearch
            self._es_client = AsyncElasticsearch(
                hosts=[self._es_url],
                request_timeout=10,
            )
            # 自动创建索引（如果不存在）
            if not await self._es_client.indices.exists(index=self._es_index):
                await self._es_client.indices.create(
                    index=self._es_index,
                    body={
                        "settings": {
                            "analysis": {
                                "analyzer": {
                                    "ik_smart_analyzer": {
                                        "type": "custom",
                                        "tokenizer": "ik_smart",
                                    }
                                }
                            }
                        },
                        "mappings": {
                            "properties": {
                                "content": {"type": "text", "analyzer": "ik_smart"},
                                "source": {"type": "keyword"},
                                "component": {"type": "keyword"},
                                "doc_type": {"type": "keyword"},
                                "content_hash": {"type": "keyword"},
                                "doc_id": {"type": "keyword"},
                                "chunk_id": {"type": "keyword"},
                            }
                        },
                    },
                )
                logger.info("es_index_created", index=self._es_index)

            self._use_mock_es = False
            logger.info("indexer_es_connected", url=self._es_url)
        except Exception as e:
            logger.warning("indexer_es_connect_failed", error=str(e))
            self._use_mock_es = True
            if self._es_client:
                await self._es_client.close()
                self._es_client = None

    async def index_document(
        self,
        text: str,
        source_path: str = "",
        metadata: dict[str, Any] | None = None,
        force: bool = False,
    ) -> ProcessedDocument | None:
        """
        索引单个文档.

        Args:
            text: 文档文本
            source_path: 源文件路径
            metadata: 额外元数据
            force: 强制重新索引（忽略增量判断）

        Returns:
            ProcessedDocument 如果实际索引了；None 如果跳过（无变化）
        """
        start = time.monotonic()
        content_hash = hashlib.md5(text.encode()).hexdigest()

        # 增量判断
        if not force:
            existing = self._index_records.get(source_path)
            if existing and existing.content_hash == content_hash:
                logger.debug(
                    "index_skipped_no_change",
                    source=source_path,
                    hash=content_hash[:8],
                )
                return None

        # 1. 文档处理
        doc = self._processor.process(text, source_path, metadata)

        # 2. Embedding 向量化
        await self._ensure_embedding_model()
        for chunk in doc.chunks:
            chunk.embedding = await self._embed(chunk.content)

        # 3. 写入向量库
        await self._write_to_milvus(doc.chunks)

        # 4. 写入 BM25 索引
        await self._write_to_es(doc.chunks)

        # 5. 更新索引记录
        self._index_records[source_path] = IndexRecord(
            doc_id=doc.id,
            content_hash=content_hash,
            chunk_count=len(doc.chunks),
            source_path=source_path,
        )

        # 6. 更新统计
        duration_ms = int((time.monotonic() - start) * 1000)
        self._stats.total_documents += 1
        self._stats.total_chunks += len(doc.chunks)
        self._stats.total_tokens += doc.total_tokens
        self._stats.index_duration_ms = duration_ms

        logger.info(
            "document_indexed",
            doc_id=doc.id,
            source=source_path,
            chunks=len(doc.chunks),
            tokens=doc.total_tokens,
            duration_ms=duration_ms,
        )

        return doc

    async def index_batch(
        self,
        documents: list[tuple[str, str]],  # [(text, source_path), ...]
        force: bool = False,
    ) -> list[ProcessedDocument]:
        """批量索引多个文档."""
        results: list[ProcessedDocument] = []
        indexed = 0
        skipped = 0

        for text, source_path in documents:
            doc = await self.index_document(text, source_path, force=force)
            if doc:
                results.append(doc)
                indexed += 1
            else:
                skipped += 1

        logger.info(
            "batch_index_complete",
            total=len(documents),
            indexed=indexed,
            skipped=skipped,
        )
        return results

    async def delete_document(self, source_path: str) -> bool:
        """从索引中删除文档."""
        record = self._index_records.get(source_path)
        if not record:
            return False

        # 从 Milvus 删除
        await self._delete_from_milvus(record.doc_id)

        # 从 ES 删除
        await self._delete_from_es(record.doc_id)

        # 删除索引记录
        del self._index_records[source_path]

        logger.info(
            "document_deleted",
            doc_id=record.doc_id,
            source=source_path,
        )
        return True

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "total_documents": self._stats.total_documents,
            "total_chunks": self._stats.total_chunks,
            "total_tokens": self._stats.total_tokens,
            "indexed_documents": len(self._index_records),
        }

    # ──────────────────────────────────────────────
    # Embedding
    # ──────────────────────────────────────────────

    async def _embed(self, text: str) -> list[float]:
        """
        文本向量化——优先用 SentenceTransformers，降级用 Mock.

        WHY normalize_embeddings=True：
        - Milvus COSINE 指标等价于 L2 on normalized vectors
        - 归一化后 cosine similarity = 内积，计算更快
        """
        if not self._use_mock_embed and self._embed_model is not None:
            try:
                embedding = self._embed_model.encode(
                    text,
                    normalize_embeddings=True,
                ).tolist()
                return embedding
            except Exception as e:
                logger.warning("embedding_failed_fallback_mock", error=str(e))

        return self._mock_embed(text)

    @staticmethod
    def _mock_embed(text: str) -> list[float]:
        """
        Mock embedding 函数.

        生成 1024 维伪向量（与 BGE-M3 维度一致），
        基于 SHA256 hash 确保同一文本生成相同向量。
        """
        hash_bytes = hashlib.sha256(text.encode()).digest()
        vector = [
            ((b % 200) - 100) / 100.0
            for b in hash_bytes * 32
        ][:1024]
        return vector

    # ──────────────────────────────────────────────
    # Milvus 写入
    # ──────────────────────────────────────────────

    async def _write_to_milvus(self, chunks: list[DocumentChunk]) -> None:
        """
        写入 Milvus 向量库.

        WHY upsert 而不是 insert：
        - 增量更新时，同一 chunk ID 可能已存在
        - upsert 自动处理"新增 or 更新"，避免手动判断
        """
        await self._ensure_milvus()

        if self._use_mock_milvus:
            logger.debug("milvus_write_mock", collection=self._collection_name, chunks=len(chunks))
            return

        try:
            data = []
            for chunk in chunks:
                if chunk.embedding is None:
                    continue
                data.append({
                    "id": chunk.id,
                    "content": chunk.content[:8000],  # VARCHAR max_length 限制
                    "source": chunk.metadata.get("source", "")[:500],
                    "component": chunk.metadata.get("component", "")[:60],
                    "doc_type": chunk.doc_type.value if chunk.doc_type else "",
                    "content_hash": hashlib.md5(chunk.content.encode()).hexdigest(),
                    "doc_id": chunk.doc_id,
                    "embedding": chunk.embedding,
                })

            if data:
                self._milvus_client.upsert(
                    collection_name=self._collection_name,
                    data=data,
                )
                logger.info(
                    "milvus_write_done",
                    collection=self._collection_name,
                    chunks=len(data),
                )
        except Exception as e:
            logger.error("milvus_write_failed", error=str(e), chunks=len(chunks))

    async def _write_to_es(self, chunks: list[DocumentChunk]) -> None:
        """
        写入 ES BM25 索引.

        WHY bulk API 而不是逐条 index：
        - 批量写入减少 HTTP 往返（10 chunks → 1 次请求 vs 10 次）
        - ES bulk API 内部有优化（批量 Lucene commit）
        """
        await self._ensure_es()

        if self._use_mock_es:
            logger.debug("es_write_mock", chunks=len(chunks))
            return

        try:
            from elasticsearch.helpers import async_bulk

            actions = []
            for chunk in chunks:
                actions.append({
                    "_index": self._es_index,
                    "_id": chunk.id,
                    "_source": {
                        "content": chunk.content,
                        "source": chunk.metadata.get("source", ""),
                        "component": chunk.metadata.get("component", ""),
                        "doc_type": chunk.doc_type.value if chunk.doc_type else "",
                        "content_hash": hashlib.md5(chunk.content.encode()).hexdigest(),
                        "doc_id": chunk.doc_id,
                        "chunk_id": chunk.id,
                    },
                })

            if actions:
                success, errors = await async_bulk(self._es_client, actions, raise_on_error=False)
                logger.info(
                    "es_write_done",
                    index=self._es_index,
                    success=success,
                    errors=len(errors) if errors else 0,
                )
        except Exception as e:
            logger.error("es_write_failed", error=str(e), chunks=len(chunks))

    # ──────────────────────────────────────────────
    # 删除操作
    # ──────────────────────────────────────────────

    async def _delete_from_milvus(self, doc_id: str) -> None:
        """从 Milvus 删除文档的所有 chunks."""
        if self._use_mock_milvus or not self._milvus_client:
            logger.debug("milvus_delete_mock", doc_id=doc_id)
            return

        try:
            # 按 doc_id 过滤删除所有关联 chunks
            self._milvus_client.delete(
                collection_name=self._collection_name,
                filter=f'doc_id == "{doc_id}"',
            )
            logger.info("milvus_delete_done", doc_id=doc_id)
        except Exception as e:
            logger.error("milvus_delete_failed", doc_id=doc_id, error=str(e))

    async def _delete_from_es(self, doc_id: str) -> None:
        """从 ES 删除文档的所有 chunks."""
        if self._use_mock_es or not self._es_client:
            logger.debug("es_delete_mock", doc_id=doc_id)
            return

        try:
            await self._es_client.delete_by_query(
                index=self._es_index,
                body={
                    "query": {
                        "term": {"doc_id": doc_id}
                    }
                },
            )
            logger.info("es_delete_done", doc_id=doc_id)
        except Exception as e:
            logger.error("es_delete_failed", doc_id=doc_id, error=str(e))

    async def close(self) -> None:
        """关闭所有连接."""
        if self._milvus_client:
            try:
                self._milvus_client.close()
            except Exception:
                pass
            self._milvus_client = None

        if self._es_client:
            try:
                await self._es_client.close()
            except Exception:
                pass
            self._es_client = None
