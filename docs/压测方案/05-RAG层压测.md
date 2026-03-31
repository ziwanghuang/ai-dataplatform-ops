# 05 — RAG 检索层压测

---

## 1. RAG 架构回顾

```
Query → QueryRewriter → ┬─ DenseRetriever (Milvus, top_k=20)
                         └─ SparseRetriever (ES BM25, top_k=20)
                         ↓
                     RRF Fusion (k=60)
                         ↓
                   MetadataFilter → Dedup
                         ↓
                  Reranker (BGE-reranker-v2-m3, 20→5)
                         ↓
                    Final Results (top_k=5)
```

**延迟预算**: < 500ms = Dense(100ms) + Sparse(50ms) + RRF(1ms) + Rerank(200ms) + 其他(50ms)

## 2. Dense Retriever (Milvus) 压测

### 2.1 关键配置

| 参数 | 值 | 说明 |
|------|-----|------|
| 向量维度 | 1024 | BGE-M3 |
| 索引类型 | IVF_FLAT | 平衡精度和速度 |
| nprobe | 16 | recall ~98% |
| 距离度量 | COSINE | 归一化后的余弦相似度 |
| top_k | 20 | 返回给 RRF |

### 2.2 Milvus 基线测试

```python
# tests/benchmark/test_rag_dense_benchmark.py
"""
Dense Retriever 微基准:
1. Embedding 延迟 (SentenceTransformer encode)
2. Milvus ANN 检索延迟
3. 端到端 Dense 检索延迟
4. 并发检索吞吐量
"""
import asyncio
import time
import pytest

from aiops.rag.dense import DenseRetriever


QUERIES = [
    "NameNode heap 使用率过高怎么处理",
    "Kafka consumer lag 持续增长",
    "ES 集群变 RED 如何排查",
    "YARN 队列资源不足",
    "HDFS 写入变慢原因分析",
    "DataNode 磁盘空间告警",
    "Spark 作业频繁 OOM",
    "Hive 查询超时优化",
    "Flink checkpoint 失败排查",
    "ZooKeeper session 超时",
]


class TestDenseBenchmark:

    @pytest.fixture
    def retriever(self):
        return DenseRetriever()

    @pytest.mark.asyncio
    async def test_mock_search_latency(self, retriever):
        """Mock 模式检索延迟 (无 Milvus)."""
        latencies = []
        for q in QUERIES:
            start = time.monotonic()
            results = await retriever.search(q, top_k=20)
            latency_ms = (time.monotonic() - start) * 1000
            latencies.append(latency_ms)
            assert len(results) > 0

        avg = sum(latencies) / len(latencies)
        p95 = sorted(latencies)[int(len(latencies) * 0.95)]
        print(f"Mock Dense: avg={avg:.1f}ms, P95={p95:.1f}ms")
        assert avg < 5  # Mock 应该 < 5ms

    @pytest.mark.asyncio
    async def test_concurrent_search(self, retriever):
        """并发检索测试 (20 并发)."""
        async def search_one(q):
            start = time.monotonic()
            results = await retriever.search(q, top_k=20)
            return (time.monotonic() - start) * 1000

        tasks = [search_one(q) for q in QUERIES * 2]  # 20 并发
        latencies = await asyncio.gather(*tasks)

        avg = sum(latencies) / len(latencies)
        max_lat = max(latencies)
        print(f"Concurrent Dense (20): avg={avg:.1f}ms, max={max_lat:.1f}ms")

    @pytest.mark.asyncio
    async def test_search_result_quality(self, retriever):
        """检索结果质量验证 (Mock 模式)."""
        results = await retriever.search("NameNode OOM", top_k=5)
        # 第一个结果应该是 NameNode OOM 相关
        assert any("namenode" in r.content.lower() for r in results)
        assert any("oom" in r.content.lower() or "heap" in r.content.lower() for r in results)
```

### 2.3 Milvus 生产模式基线 (需要 Milvus 运行)

```python
@pytest.mark.skipif(not MILVUS_AVAILABLE, reason="Milvus not running")
class TestDenseProduction:

    @pytest.mark.asyncio
    async def test_embedding_latency(self):
        """Embedding 单次延迟.
        
        BGE-M3 encode 单条查询:
        - CPU: ~50ms
        - GPU: ~10ms
        """
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer("BAAI/bge-m3")
        
        latencies = []
        for q in QUERIES:
            start = time.monotonic()
            model.encode(q, normalize_embeddings=True)
            latencies.append((time.monotonic() - start) * 1000)
        
        avg = sum(latencies) / len(latencies)
        print(f"Embedding: avg={avg:.1f}ms")
        assert avg < 100  # CPU 模式

    @pytest.mark.asyncio
    async def test_milvus_ann_latency(self):
        """Milvus ANN 检索延迟 (不含 Embedding).
        
        预期: < 50ms for 100K vectors with IVF_FLAT nprobe=16
        """
        retriever = DenseRetriever()
        # 预先 encode query
        results = await retriever.search("NameNode OOM", top_k=20)
        assert len(results) > 0

    @pytest.mark.asyncio
    async def test_dense_throughput(self):
        """Dense 检索吞吐量.
        
        目标: > 20 QPS (单线程)
        """
        retriever = DenseRetriever()
        count = 50
        start = time.monotonic()
        
        for i in range(count):
            q = QUERIES[i % len(QUERIES)]
            await retriever.search(q, top_k=20)
        
        elapsed = time.monotonic() - start
        qps = count / elapsed
        print(f"Dense QPS: {qps:.1f}")
        assert qps > 10
```

## 3. Sparse Retriever (Elasticsearch) 压测

### 3.1 ES 基线测试

```bash
# ES 基线: esrally 跑标准 benchmark
esrally race --track=geonames --target-hosts=localhost:9200 --pipeline=benchmark-only

# 自定义 rally track (运维文档检索)
# 需要准备 ops_docs track
```

### 3.2 BM25 检索延迟

```python
class TestSparseBenchmark:

    @pytest.mark.asyncio
    async def test_bm25_search_latency(self):
        """BM25 单次检索延迟.
        
        预期: < 50ms for 100K documents
        """
        from aiops.rag.sparse import SparseRetriever
        retriever = SparseRetriever()
        
        latencies = []
        for q in QUERIES:
            start = time.monotonic()
            results = await retriever.search(q, top_k=20)
            latencies.append((time.monotonic() - start) * 1000)
        
        avg = sum(latencies) / len(latencies)
        p95 = sorted(latencies)[int(len(latencies) * 0.95)]
        print(f"Sparse BM25: avg={avg:.1f}ms, P95={p95:.1f}ms")

    @pytest.mark.asyncio
    async def test_bm25_concurrent(self):
        """ES BM25 并发检索 (50 并发)."""
        from aiops.rag.sparse import SparseRetriever
        retriever = SparseRetriever()
        
        async def search_one(q):
            start = time.monotonic()
            await retriever.search(q, top_k=20)
            return (time.monotonic() - start) * 1000
        
        tasks = [search_one(q) for q in QUERIES * 5]
        latencies = await asyncio.gather(*tasks)
        print(f"Concurrent BM25 (50): avg={sum(latencies)/len(latencies):.1f}ms")
```

## 4. Hybrid Retriever 端到端压测

### 4.1 混合检索延迟

```python
class TestHybridBenchmark:

    @pytest.mark.asyncio
    async def test_hybrid_retrieval_latency(self):
        """完整混合检索延迟 (Dense ∥ Sparse → RRF → Rerank).
        
        延迟拆解:
        - Dense + Sparse 并行: max(Dense, Sparse) ≈ 100ms
        - RRF: < 1ms
        - Rerank: ~200ms (Cross-Encoder)
        - 总计: ~300ms
        目标: P95 < 500ms
        """
        from aiops.rag.retriever import HybridRetriever
        from aiops.rag.dense import DenseRetriever
        from aiops.rag.sparse import SparseRetriever
        from aiops.rag.reranker import Reranker
        
        retriever = HybridRetriever(
            dense_retriever=DenseRetriever(),
            sparse_retriever=SparseRetriever(),
            reranker=Reranker(),
        )
        
        latencies = []
        for q in QUERIES:
            start = time.monotonic()
            results = await retriever.retrieve(q, top_k=5)
            latencies.append((time.monotonic() - start) * 1000)
        
        avg = sum(latencies) / len(latencies)
        p95 = sorted(latencies)[int(len(latencies) * 0.95)]
        print(f"Hybrid: avg={avg:.1f}ms, P95={p95:.1f}ms")
        assert p95 < 500

    @pytest.mark.asyncio
    async def test_rrf_fusion_latency(self):
        """RRF 融合算法延迟 (纯计算).
        
        输入: Dense 20 + Sparse 20 = 最多 40 个候选
        预期: < 1ms
        """
        from aiops.rag.retriever import rrf_fusion
        from aiops.rag.types import RetrievalResult
        
        dense = [RetrievalResult(content=f"dense_{i}", source=f"d{i}", score=1.0-i*0.05)
                 for i in range(20)]
        sparse = [RetrievalResult(content=f"sparse_{i}", source=f"s{i}", score=10.0-i*0.5)
                  for i in range(20)]
        
        # 运行 1000 次取平均
        start = time.monotonic()
        for _ in range(1000):
            rrf_fusion(dense, sparse, k=60)
        avg_us = (time.monotonic() - start) / 1000 * 1_000_000
        print(f"RRF fusion: {avg_us:.1f} μs")
        assert avg_us < 1000  # < 1ms
```

### 4.2 降级场景测试

```python
class TestRAGDegradation:

    @pytest.mark.asyncio
    async def test_dense_failure_fallback(self):
        """Dense 失败时降级为 Sparse 单路.
        
        质量影响: recall 下降 ~15%
        延迟影响: 减少 ~100ms (少了 Dense)
        """
        retriever = HybridRetriever(
            dense_retriever=None,  # 模拟 Dense 不可用
            sparse_retriever=SparseRetriever(),
        )
        results = await retriever.retrieve("NameNode OOM", top_k=5)
        assert len(results) > 0  # 应该有结果

    @pytest.mark.asyncio
    async def test_sparse_failure_fallback(self):
        """Sparse 失败时降级为 Dense 单路."""
        retriever = HybridRetriever(
            dense_retriever=DenseRetriever(),
            sparse_retriever=None,
        )
        results = await retriever.retrieve("NameNode OOM", top_k=5)
        assert len(results) > 0

    @pytest.mark.asyncio
    async def test_both_failure(self):
        """双路全挂时返回空结果."""
        retriever = HybridRetriever(dense_retriever=None, sparse_retriever=None)
        results = await retriever.retrieve("NameNode OOM", top_k=5)
        assert results == []

    @pytest.mark.asyncio
    async def test_reranker_failure_fallback(self):
        """Reranker 失败时降级为 RRF 分数排序.
        
        质量影响: 精度下降 ~8%
        """
        class FailingReranker:
            async def rerank(self, query, docs, top_k):
                raise Exception("Reranker timeout")
        
        retriever = HybridRetriever(
            dense_retriever=DenseRetriever(),
            sparse_retriever=SparseRetriever(),
            reranker=FailingReranker(),
        )
        results = await retriever.retrieve("NameNode OOM", top_k=5)
        assert len(results) > 0  # 应该用 RRF 分数兜底
```

## 5. RAG 关注指标

| 指标 | Prometheus Name | 目标 |
|------|-----------------|------|
| Dense 检索延迟 | `aiops_rag_retrieval_duration_seconds{retriever_type=dense}` | P95 < 150ms |
| Sparse 检索延迟 | `aiops_rag_retrieval_duration_seconds{retriever_type=sparse}` | P95 < 80ms |
| Hybrid 总延迟 | `aiops_rag_retrieval_duration_seconds{retriever_type=hybrid}` | P95 < 500ms |
| Reranker 延迟 | `aiops_rag_reranker_duration_seconds` | P95 < 300ms |
| 检索结果数 | `aiops_rag_results_count` | avg > 3 |
| 缓存命中率 | `aiops_rag_cache_hit_total / (hit+miss)` | > 20% |
