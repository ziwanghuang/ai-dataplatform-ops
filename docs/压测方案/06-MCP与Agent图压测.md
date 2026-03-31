# 06 — MCP 工具层压测

---

## 1. MCP Client 连接池压测

### 1.1 配置参数

| 参数 | 值 | 说明 |
|------|-----|------|
| max_connections | 100 | httpx 最大连接数 |
| max_keepalive_connections | 20 | 保持活跃连接数 |
| keepalive_expiry | 30s | 空闲连接超时 |
| connect_timeout | 5s | 建连超时 |
| read_timeout | 30s | 读取超时 |
| pool_timeout | 10s | 连接池等待超时 |

### 1.2 连接池压力测试

```python
# tests/benchmark/test_mcp_pool.py
"""
测试 httpx 连接池在高并发下的表现:
1. 50 并发 — 不应出现 pool timeout
2. 100 并发 — 达到 max_connections 上限
3. 200 并发 — 应出现排队等待
4. 连接复用率 — keepalive 是否生效
"""
import asyncio
import time
from aiops.mcp_client.client import MCPClient


async def test_connection_pool_50_concurrent():
    """50 并发工具调用."""
    client = MCPClient()
    results = []

    async def call_tool(i):
        start = time.monotonic()
        try:
            result = await client.call_tool("hdfs_namenode_status", {"cluster": "prod"})
            return {"id": i, "latency_ms": (time.monotonic()-start)*1000, "error": None}
        except Exception as e:
            return {"id": i, "latency_ms": (time.monotonic()-start)*1000, "error": str(e)}

    tasks = [call_tool(i) for i in range(50)]
    results = await asyncio.gather(*tasks)

    errors = [r for r in results if r["error"]]
    latencies = [r["latency_ms"] for r in results if not r["error"]]

    print(f"50 concurrent: success={len(latencies)}, errors={len(errors)}")
    print(f"  avg={sum(latencies)/max(len(latencies),1):.1f}ms")
    print(f"  P95={sorted(latencies)[int(len(latencies)*0.95)]:.1f}ms")

    await client.close()
    assert len(errors) == 0


async def test_connection_pool_200_concurrent():
    """200 并发 — 超过 max_connections(100)，验证排队行为."""
    client = MCPClient()

    async def call_tool(i):
        start = time.monotonic()
        try:
            await client.call_tool("hdfs_namenode_status", {})
            return (time.monotonic()-start)*1000, None
        except Exception as e:
            return (time.monotonic()-start)*1000, str(e)

    tasks = [call_tool(i) for i in range(200)]
    results = await asyncio.gather(*tasks)

    pool_timeouts = [r for r in results if r[1] and "pool" in str(r[1]).lower()]
    print(f"200 concurrent: pool_timeouts={len(pool_timeouts)}")

    await client.close()
```

## 2. MCP 缓存效果压测

### 2.1 L1 + L2 缓存命中率

```python
async def test_cache_hit_rate():
    """模拟诊断循环中的缓存命中.
    
    诊断循环典型模式:
    Round 1: hdfs_namenode_status → MISS
    Round 2: hdfs_namenode_status → HIT (30s TTL 内)
    Round 3: hdfs_cluster_overview → MISS
    Round 4: hdfs_namenode_status → HIT
    
    预期命中率: > 50% (重复调用密集)
    """
    client = MCPClient()

    calls = [
        ("hdfs_namenode_status", {"cluster": "prod"}),
        ("hdfs_namenode_status", {"cluster": "prod"}),  # should hit L1
        ("hdfs_cluster_overview", {"cluster": "prod"}),
        ("hdfs_namenode_status", {"cluster": "prod"}),  # should hit L1
        ("kafka_consumer_lag", {"group": "etl"}),
        ("hdfs_namenode_status", {"cluster": "prod"}),  # should hit L1
        ("kafka_consumer_lag", {"group": "etl"}),       # should hit L1
        ("es_cluster_health", {}),
        ("es_cluster_health", {}),                       # should hit L1
        ("hdfs_namenode_status", {"cluster": "prod"}),  # should hit L1
    ]

    for tool_name, params in calls:
        await client.call_tool(tool_name, params)

    stats = client.stats
    cache_size = stats["cache_size"]
    print(f"Cache size: {cache_size}, calls: {stats['total_calls']}")
    # L1 应该有 4 个不同的 key
    assert cache_size <= 5

    await client.close()


async def test_cache_eviction():
    """缓存淘汰测试.
    
    L1 容量: 200 条
    填充 250 条 → 应淘汰 50 条最旧的
    """
    client = MCPClient()

    # 填充 250 个不同的缓存条目
    for i in range(250):
        # 每次用不同参数避免缓存命中
        await client.call_tool("hdfs_namenode_status", {"unique_id": str(i)})

    stats = client.stats
    assert stats["cache_size"] <= 200  # 不超过 max_entries

    await client.close()
```

### 2.2 L2 Redis 缓存测试

```python
async def test_l2_redis_cache():
    """L2 Redis 缓存.
    
    场景: L1 未命中 → L2 Redis 命中 → 回填 L1
    模拟: 两个 MCPClient 实例共享 Redis
    """
    # Client A 写入
    client_a = MCPClient(redis_url="redis://localhost:6379/0")
    await client_a.call_tool("hdfs_namenode_status", {"test": "l2"})

    # Client B 应该能从 Redis 读到
    client_b = MCPClient(redis_url="redis://localhost:6379/0")
    client_b._cache.clear()  # 清空 L1
    result = await client_b.call_tool("hdfs_namenode_status", {"test": "l2"})
    assert result is not None  # 应该命中 L2

    await client_a.close()
    await client_b.close()


async def test_redis_failure_fallback():
    """Redis 挂了不影响功能.
    
    MCPClient 初始化时如果 Redis 不可用:
    - _use_redis = False
    - 所有缓存走 L1 内存
    - 不应有任何报错
    """
    client = MCPClient(redis_url="redis://nonexistent:6379/0")
    assert client._use_redis is False

    # 正常调用工具
    result = await client.call_tool("hdfs_namenode_status", {})
    assert result is not None

    await client.close()
```

## 3. 批量并行调用压测

### 3.1 Semaphore(5) 并发控制

```python
async def test_batch_call_semaphore():
    """验证 batch_call_tools 的 Semaphore(5) 限制.
    
    发送 20 个并行工具调用，验证:
    1. 最多 5 个同时执行
    2. 总延迟 ≈ 4 × 单次延迟（20/5=4 批）
    3. 所有结果正确返回
    """
    client = MCPClient()

    calls = [
        (f"hdfs_namenode_status", {"batch": str(i)}) for i in range(20)
    ]

    start = time.monotonic()
    results = await client.batch_call_tools(calls, max_concurrency=5)
    total_ms = (time.monotonic() - start) * 1000

    success = [r for r in results if r["error"] is None]
    errors = [r for r in results if r["error"] is not None]

    print(f"Batch 20 (max_concurrent=5): total={total_ms:.0f}ms")
    print(f"  success={len(success)}, errors={len(errors)}")

    # 20 个调用 / 5 并发 = 4 批，每批 ~50ms = ~200ms
    assert total_ms < 2000  # 宽松上限

    await client.close()


async def test_batch_call_timeout():
    """批量调用中单个超时不影响其他.
    
    per_tool_timeout=15s
    如果一个工具超时，其他工具应正常返回
    """
    client = MCPClient()

    calls = [
        ("hdfs_namenode_status", {}),          # 正常
        ("search_logs", {"query": "ERROR"}),   # 正常
        ("nonexistent_tool", {}),              # 会失败
    ]

    results = await client.batch_call_tools(calls, per_tool_timeout=5.0)
    success = [r for r in results if r["error"] is None]
    assert len(success) >= 2  # 至少 2 个成功

    await client.close()
```

## 4. Go 中间件链压测

### 4.1 8 层中间件开销

```go
// go/internal/middleware/chain_bench_test.go
package middleware_test

import (
    "context"
    "testing"
    "time"

    "github.com/ziwang/aiops-mcp/internal/config"
    "github.com/ziwang/aiops-mcp/internal/middleware"
    "github.com/ziwang/aiops-mcp/internal/protocol"
)

// mockTool 实现 protocol.Tool 接口
type mockTool struct{}
func (t *mockTool) Name() string           { return "benchmark_tool" }
func (t *mockTool) Description() string    { return "benchmark" }
func (t *mockTool) Schema() interface{}    { return nil }
func (t *mockTool) RiskLevel() protocol.RiskLevel { return protocol.RiskNone }
func (t *mockTool) Execute(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
    return &protocol.ToolResult{
        Content: []protocol.Content{{Type: "text", Text: "ok"}},
    }, nil
}

func BenchmarkFullChain(b *testing.B) {
    cfg := &config.Config{
        Middleware: config.MiddlewareConfig{
            Timeout:        config.TimeoutConfig{Default: 30 * time.Second},
            CircuitBreaker: config.CircuitBreakerConfig{MaxRequests: 5, Interval: 60 * time.Second, Timeout: 30 * time.Second, FailureRatio: 0.5},
            RateLimit:      config.RateLimitConfig{GlobalRPS: 1000000, PerUserRPS: 100000},
            Cache:          config.CacheConfig{Enabled: true, DefaultTTL: 5 * time.Minute},
        },
    }

    tool := &mockTool{}
    handler := middleware.BuildMiddlewareChain(tool.Execute, tool, cfg)

    ctx := context.Background()
    params := map[string]interface{}{"test": true}

    b.ResetTimer()
    b.RunParallel(func(pb *testing.PB) {
        for pb.Next() {
            handler(ctx, params)
        }
    })
    // 预期: 每个请求增加 < 0.1ms 开销 (8 层总计)
}
```

### 4.2 各中间件独立开销

```go
func BenchmarkTracingOnly(b *testing.B)       { /* ... */ }
func BenchmarkAuditOnly(b *testing.B)         { /* ... */ }
func BenchmarkRateLimitOnly(b *testing.B)     { /* ... */ }
func BenchmarkValidationOnly(b *testing.B)    { /* ... */ }
func BenchmarkRiskOnly(b *testing.B)          { /* ... */ }
func BenchmarkCacheOnly_Miss(b *testing.B)    { /* ... */ }
func BenchmarkCacheOnly_Hit(b *testing.B)     { /* ... */ }
func BenchmarkCircuitBreakerOnly(b *testing.B) { /* ... */ }
func BenchmarkTimeoutOnly(b *testing.B)       { /* ... */ }
```

## 5. MCP 关注指标

| 指标 | Prometheus Name | 目标 |
|------|-----------------|------|
| 工具调用延迟 | `aiops_mcp_tool_call_duration_seconds` | P95 < 500ms |
| 工具调用失败率 | `aiops_mcp_tool_call_total{status=error}` | < 3% |
| 连接池使用率 | httpx 内部 | < 80% |
| L1 缓存命中率 | 自定义 | > 40% (诊断循环) |
| Goroutine 数量 | Go runtime | < 500 |


---

# 07 — Agent 图编排压测

---

## 1. StateGraph 性能

### 1.1 图编译延迟

```python
async def test_graph_compilation_latency():
    """图编译延迟 (首次 vs 缓存).
    
    首次编译: ~200ms (导入所有节点 + 构建 StateGraph)
    后续调用: < 1ms (模块级缓存)
    """
    import importlib
    from aiops.agent import graph as graph_module

    # 重新加载模块清除缓存
    importlib.reload(graph_module)

    start = time.monotonic()
    g1 = graph_module.build_ops_graph()
    first_ms = (time.monotonic() - start) * 1000
    print(f"First compilation: {first_ms:.0f}ms")

    start = time.monotonic()
    g2 = graph_module.build_ops_graph()
    second_ms = (time.monotonic() - start) * 1000
    print(f"Second (cached): {second_ms:.2f}ms")

    assert first_ms < 500
    assert second_ms < 10
```

### 1.2 Diagnostic 自环安全阀

```python
async def test_diagnostic_loop_limit():
    """Diagnostic 自环上限为 5 轮.
    
    风险: 如果 LLM 持续返回低置信度，自环永不退出 → 请求超时
    安全阀: route_from_diagnostic 在 iteration_count >= 5 时强制退出
    """
    from aiops.agent.router import route_from_diagnostic
    from aiops.agent.state import create_initial_state

    state = create_initial_state(query="test", user_id="test")
    state["iteration_count"] = 5
    state["confidence"] = 0.3  # 低置信度，正常应继续循环

    route = route_from_diagnostic(state)
    assert route == "report"  # 强制退出到 report，不再自环
```

### 1.3 并发图执行

```python
async def test_concurrent_graph_invocations():
    """多个诊断请求并发执行 Agent 图.
    
    验证:
    1. 每个请求有独立的 state（互不干扰）
    2. MemorySaver checkpoint 的线程安全
    3. 无死锁
    """
    from aiops.agent.graph import build_ops_graph

    graph = build_ops_graph()

    async def run_diagnosis(i):
        state = create_initial_state(
            query=f"Test query #{i}",
            user_id=f"user_{i}",
            session_id=f"session_{i}",
        )
        result = await graph.ainvoke(
            state,
            config={"configurable": {"thread_id": f"session_{i}"}},
        )
        return result

    tasks = [run_diagnosis(i) for i in range(10)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    errors = [r for r in results if isinstance(r, Exception)]
    assert len(errors) == 0, f"Got {len(errors)} errors"
```

## 2. 节点级性能基线

| 节点 | 预期延迟 | LLM 调用 | MCP 调用 |
|------|----------|----------|----------|
| TriageNode | 200-500ms | 1 (DeepSeek) | 0 |
| PlanningNode | 1-2s | 1 (GPT-4o/mini) + RAG | 0 |
| DiagnosticNode | 2-5s/轮 | 1 (GPT-4o) | 1-3 (并行) |
| RemediationNode | 500ms-2s | 1 (GPT-4o) | 0-1 |
| ReportNode | 1-2s | 1 (GPT-4o-mini) | 0 |
| AlertCorrelationNode | 1-3s | 1 (GPT-4o) | 0 |
| DirectToolNode | 100-500ms | 0 | 1 |
| HITLGateNode | < 100ms | 0 | 0 (查 Redis) |
| KnowledgeSinkNode | 100-500ms | 0 | 0 (写 Milvus/ES) |
