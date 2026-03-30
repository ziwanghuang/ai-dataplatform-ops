"""补全测试: RAG 引擎 + Prompt 模板 + 安全防护增强."""

from __future__ import annotations

import pytest

from aiops.rag.types import RetrievalResult, RetrievalQuery, RetrievalContext
from aiops.rag.retriever import rrf_fusion, HybridRetriever
from aiops.rag.dense import DenseRetriever, MOCK_DOCUMENTS
from aiops.rag.sparse import SparseRetriever
from aiops.rag.reranker import CrossEncoderReranker


# ──────────────────────────────────────────────────
# RAG 数据结构测试
# ──────────────────────────────────────────────────

class TestRetrievalResult:
    def test_basic_creation(self) -> None:
        r = RetrievalResult(content="test", source="test.md", score=0.85)
        assert r.content == "test"
        assert r.score == 0.85
        assert r.metadata == {}

    def test_with_metadata(self) -> None:
        r = RetrievalResult(
            content="doc", source="s.md", score=0.9,
            metadata={"component": "hdfs", "content_hash": "abc123"},
        )
        assert r.metadata["component"] == "hdfs"


class TestRetrievalContext:
    def test_to_langfuse_metadata(self) -> None:
        query = RetrievalQuery(query="test query")
        ctx = RetrievalContext(
            query=query,
            dense_results=[RetrievalResult("a", "s", 0.9)],
            sparse_results=[RetrievalResult("b", "s", 0.8)],
        )
        meta = ctx.to_langfuse_metadata()
        assert meta["rag.dense_count"] == 1
        assert meta["rag.sparse_count"] == 1
        assert meta["rag.cache_hit"] is False


# ──────────────────────────────────────────────────
# RRF 融合算法测试
# ──────────────────────────────────────────────────

class TestRRFFusion:
    def test_both_routes_have_results(self) -> None:
        """两路都有结果时，两路都排名靠前的文档应获得最高 RRF 分数."""
        dense = [
            RetrievalResult("doc_A", "a.md", 0.95, {"content_hash": "A"}),
            RetrievalResult("doc_B", "b.md", 0.85, {"content_hash": "B"}),
            RetrievalResult("doc_C", "c.md", 0.75, {"content_hash": "C"}),
        ]
        sparse = [
            RetrievalResult("doc_A", "a.md", 28.5, {"content_hash": "A"}),  # 两路都第 1
            RetrievalResult("doc_D", "d.md", 20.0, {"content_hash": "D"}),  # 只在 Sparse
            RetrievalResult("doc_B", "b.md", 15.0, {"content_hash": "B"}),  # 两路都有
        ]
        fused = rrf_fusion(dense, sparse, k=60)

        # doc_A 在两路都排第 1 → RRF 分数最高
        assert fused[0].metadata.get("content_hash") == "A"
        # doc_B 在两路都有 → 分数应高于只在一路的 doc_C 和 doc_D
        doc_b_score = next(r.score for r in fused if r.metadata.get("content_hash") == "B")
        doc_c_score = next(r.score for r in fused if r.metadata.get("content_hash") == "C")
        doc_d_score = next(r.score for r in fused if r.metadata.get("content_hash") == "D")
        assert doc_b_score > doc_c_score
        assert doc_b_score > doc_d_score

    def test_single_route_only(self) -> None:
        """只有一路有结果时，应正确返回."""
        dense = [RetrievalResult("doc_A", "a.md", 0.9, {"content_hash": "A"})]
        sparse: list[RetrievalResult] = []
        fused = rrf_fusion(dense, sparse, k=60)
        assert len(fused) == 1
        assert fused[0].metadata.get("content_hash") == "A"

    def test_empty_both(self) -> None:
        """两路都为空时应返回空列表."""
        fused = rrf_fusion([], [], k=60)
        assert fused == []

    def test_k_parameter_effect(self) -> None:
        """k 值影响分数分布——k 越大，排名差异影响越小."""
        dense = [
            RetrievalResult("top", "t.md", 0.99, {"content_hash": "T"}),
            RetrievalResult("low", "l.md", 0.50, {"content_hash": "L"}),
        ]
        # k=1: 排名差异大
        fused_k1 = rrf_fusion(dense, [], k=1)
        ratio_k1 = fused_k1[0].score / fused_k1[1].score

        # k=100: 排名差异小
        fused_k100 = rrf_fusion(dense, [], k=100)
        ratio_k100 = fused_k100[0].score / fused_k100[1].score

        assert ratio_k1 > ratio_k100  # k 小时差异更大


# ──────────────────────────────────────────────────
# Dense/Sparse/Reranker Mock 测试
# ──────────────────────────────────────────────────

class TestDenseRetriever:
    @pytest.mark.asyncio
    async def test_search_returns_results(self) -> None:
        """Mock Dense 应返回与查询相关的文档."""
        retriever = DenseRetriever()
        results = await retriever.search("NameNode OOM 堆内存")
        assert len(results) > 0
        # 应优先返回 NameNode 相关文档
        assert any("NameNode" in r.content for r in results)

    @pytest.mark.asyncio
    async def test_search_no_match(self) -> None:
        """完全不相关的查询应返回空或少量结果."""
        retriever = DenseRetriever()
        results = await retriever.search("xyz不存在的关键词abc")
        # Mock 实现可能返回空
        assert isinstance(results, list)

    def test_mock_documents_count(self) -> None:
        """Mock 知识库应有足够的文档."""
        assert len(MOCK_DOCUMENTS) >= 5


class TestSparseRetriever:
    @pytest.mark.asyncio
    async def test_search_exact_match(self) -> None:
        """BM25 Mock 应对精确关键词匹配."""
        retriever = SparseRetriever()
        results = await retriever.search("dfs.namenode.handler.count 配置")
        assert len(results) > 0

    @pytest.mark.asyncio
    async def test_search_chinese(self) -> None:
        """应支持中文查询."""
        retriever = SparseRetriever()
        results = await retriever.search("消费延迟 Kafka")
        assert len(results) > 0


class TestCrossEncoderReranker:
    @pytest.mark.asyncio
    async def test_rerank_returns_top_k(self) -> None:
        """应返回 top_k 个结果."""
        reranker = CrossEncoderReranker()
        candidates = [
            RetrievalResult(f"doc_{i}", f"s{i}.md", float(i) / 10)
            for i in range(10)
        ]
        results = await reranker.rerank("test", candidates, top_k=3)
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_rerank_empty(self) -> None:
        """空候选应返回空."""
        reranker = CrossEncoderReranker()
        results = await reranker.rerank("test", [], top_k=5)
        assert results == []


# ──────────────────────────────────────────────────
# HybridRetriever 集成测试
# ──────────────────────────────────────────────────

class TestHybridRetriever:
    @pytest.mark.asyncio
    async def test_full_pipeline(self) -> None:
        """端到端混合检索——Dense + Sparse → RRF → Rerank."""
        retriever = HybridRetriever(
            dense_retriever=DenseRetriever(),
            sparse_retriever=SparseRetriever(),
            reranker=CrossEncoderReranker(),
        )
        results = await retriever.retrieve("NameNode OOM 怎么处理", top_k=3)
        assert len(results) > 0
        assert len(results) <= 3
        # 结果应包含 HDFS 相关文档
        assert any("NameNode" in r.content or "hdfs" in r.source for r in results)

    @pytest.mark.asyncio
    async def test_single_route_fallback(self) -> None:
        """只有一路时应正常工作（降级）."""
        retriever = HybridRetriever(
            dense_retriever=DenseRetriever(),
            sparse_retriever=None,  # 无 Sparse
            reranker=CrossEncoderReranker(),
        )
        results = await retriever.retrieve("HDFS 容量")
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_with_filters(self) -> None:
        """带元数据过滤应只返回匹配的文档."""
        retriever = HybridRetriever(
            dense_retriever=DenseRetriever(),
            sparse_retriever=SparseRetriever(),
            reranker=CrossEncoderReranker(),
        )
        results = await retriever.retrieve(
            "集群问题",
            filters={"components": ["kafka"]},
            top_k=5,
        )
        # 过滤后应只有 kafka 相关文档
        for r in results:
            assert r.metadata.get("component", "") in ("kafka", "")


# ──────────────────────────────────────────────────
# Prompt 模板测试
# ──────────────────────────────────────────────────

class TestPromptTemplates:
    def test_triage_prompt_has_placeholders(self) -> None:
        from aiops.prompts.triage import TRIAGE_SYSTEM_PROMPT
        assert "{available_tools}" in TRIAGE_SYSTEM_PROMPT
        assert "{alert_context}" in TRIAGE_SYSTEM_PROMPT
        assert "direct_tool" in TRIAGE_SYSTEM_PROMPT

    def test_triage_prompt_format(self) -> None:
        from aiops.prompts.triage import TRIAGE_SYSTEM_PROMPT
        result = TRIAGE_SYSTEM_PROMPT.format(
            available_tools="- hdfs_cluster_overview: HDFS 概览",
            alert_context="",
            cluster_context="集群: prod-01",
        )
        assert "hdfs_cluster_overview" in result
        assert "prod-01" in result

    def test_diagnostic_prompt_has_placeholders(self) -> None:
        from aiops.prompts.diagnostic import DIAGNOSTIC_SYSTEM_PROMPT
        assert "{hypotheses}" in DIAGNOSTIC_SYSTEM_PROMPT
        assert "{collected_data_summary}" in DIAGNOSTIC_SYSTEM_PROMPT
        assert "五步法" in DIAGNOSTIC_SYSTEM_PROMPT

    def test_planning_prompt(self) -> None:
        from aiops.prompts.planning import PLANNING_SYSTEM_PROMPT
        assert "假设" in PLANNING_SYSTEM_PROMPT
        assert "HDFS" in PLANNING_SYSTEM_PROMPT
        assert "YARN" in PLANNING_SYSTEM_PROMPT
