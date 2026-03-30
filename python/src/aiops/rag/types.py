"""
RAG 数据结构定义

WHY 用 dataclass 而不是 Pydantic：
- 检索一次产生 40+ 个 RetrievalResult 对象，dataclass 比 Pydantic 快 3x
- RetrievalResult 只在 Python 进程内部传递，不需要 JSON Schema / validation
- 与 Milvus/ES 返回的 dict 手动赋值更透明
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RetrievalResult:
    """
    单个检索结果.

    metadata 包含：
    - component: str   — 关联的大数据组件（hdfs/yarn/kafka/...）
    - doc_type: str    — sop / incident / alert_handbook / reference
    - version: str     — 文档版本
    - chunk_index: int — 在原文档中的位置
    - content_hash: str — 内容哈希（用于去重）
    """
    content: str         # 文档内容
    source: str          # 来源（文件路径/URL）
    score: float         # 相关度分数（cosine similarity 或 BM25 score）
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RetrievedDocument:
    """
    检索到的文档——GraphRAG 使用.

    WHY 与 RetrievalResult 分开：
    - RetrievalResult 是 Dense/Sparse 检索层的输出，字段轻量
    - RetrievedDocument 带 id 字段，用于图谱关联和去重
    - GraphRAG 返回的文档需要追溯到图节点（通过 id）
    """
    id: str                  # 文档/节点 ID（对应 Neo4j node ID 或因果链 ID）
    content: str             # 文档内容
    source: str              # 来源标识
    score: float = 0.0       # 相关度分数
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_retrieval_result(self) -> RetrievalResult:
        """转换为 RetrievalResult（用于与 Dense/Sparse 结果融合）."""
        return RetrievalResult(
            content=self.content,
            source=self.source,
            score=self.score,
            metadata=self.metadata,
        )


@dataclass
class RetrievalQuery:
    """检索请求."""
    query: str                        # 原始查询
    rewritten_query: str = ""         # 改写后的查询
    filters: dict[str, Any] = field(default_factory=dict)
    top_k: int = 5
    collection: str = "ops_documents"
    include_dense: bool = True
    include_sparse: bool = True

    @property
    def text(self) -> str:
        """兼容属性——GraphRAG 等模块通过 .text 访问查询文本."""
        return self.query


@dataclass
class RetrievalContext:
    """
    检索上下文——记录完整的检索过程，用于可观测性和调试.

    WHY 记录中间结果：
    1. LangFuse 追踪：每次检索作为一个 span，通过 to_langfuse_metadata() 序列化
    2. 在线 A/B 测试：完整"实验日志"支持离线分析不同配置的效果
    3. 用户透明度：Diagnostic 输出引用检索来源时可追溯
    """
    query: RetrievalQuery
    dense_results: list[RetrievalResult] = field(default_factory=list)
    sparse_results: list[RetrievalResult] = field(default_factory=list)
    fused_results: list[RetrievalResult] = field(default_factory=list)
    reranked_results: list[RetrievalResult] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    cache_hit: bool = False

    def to_langfuse_metadata(self) -> dict[str, Any]:
        """转换为 LangFuse span metadata 格式."""
        return {
            "rag.query": self.query.query[:200],
            "rag.rewritten_query": self.query.rewritten_query[:200],
            "rag.collection": self.query.collection,
            "rag.dense_count": len(self.dense_results),
            "rag.sparse_count": len(self.sparse_results),
            "rag.fused_count": len(self.fused_results),
            "rag.final_count": len(self.reranked_results),
            "rag.cache_hit": self.cache_hit,
            "rag.timings": self.timings,
            "rag.errors": self.errors,
        }
