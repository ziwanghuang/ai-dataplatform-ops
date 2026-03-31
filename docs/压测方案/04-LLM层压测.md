# 04 — LLM 层压测

---

## 1. 压测目标

LLM 层是整个系统延迟最大的组件（单次调用 200ms-10s），也是成本最高的组件。压测重点：

1. **延迟分布** — P50/P95/P99，尤其关注长尾
2. **Provider 容灾** — 主 Provider 挂后切换延迟
3. **熔断器行为** — 连续失败 5 次后熔断，60s 恢复
4. **Token 预算** — 日限额 500,000 token 耗尽后的降级
5. **并发安全** — 多个 Agent 节点并发调用 LLM

## 2. LLM Client 性能基线

### 2.1 pytest-benchmark 微基准

```python
# tests/benchmark/test_llm_benchmark.py
"""LLM Client 微基准测试.

运行: cd python && PYTHONPATH=src .venv/bin/python -m pytest tests/benchmark/ -v --benchmark-only
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, patch

from aiops.llm.client import LLMClient
from aiops.llm.types import LLMCallContext, TaskType


@pytest.fixture
def llm_client():
    return LLMClient()


@pytest.fixture
def triage_context():
    return LLMCallContext(task_type=TaskType.TRIAGE, request_id="bench-001")


def test_model_routing_latency(benchmark, llm_client, triage_context):
    """路由表查询延迟（应 < 0.01ms）."""
    def route():
        return llm_client._router.route(triage_context)
    result = benchmark(route)
    assert result.model == "deepseek-chat"


@pytest.mark.asyncio
async def test_llm_call_with_mock(benchmark):
    """Mock LLM 调用完整链路延迟.
    
    包含: 路由 → 构建请求 → litellm 调用(mock) → 解析响应 → 记录指标
    预期: < 5ms（不含网络）
    """
    client = LLMClient()
    context = LLMCallContext(task_type=TaskType.TRIAGE)

    mock_response = AsyncMock()
    mock_response.choices = [AsyncMock()]
    mock_response.choices[0].message.content = '{"route": "direct_tool"}'
    mock_response.choices[0].finish_reason = "stop"
    mock_response.usage.prompt_tokens = 100
    mock_response.usage.completion_tokens = 50
    mock_response.usage.total_tokens = 150

    with patch("litellm.acompletion", return_value=mock_response):
        async def call():
            return await client.chat(
                messages=[{"role": "user", "content": "test"}],
                context=context,
            )
        # benchmark 不直接支持 async，需要包装
        result = await call()
        assert result.content == '{"route": "direct_tool"}'


@pytest.mark.asyncio
async def test_structured_output_with_mock(benchmark):
    """结构化输出 (instructor) 延迟.
    
    额外开销: Pydantic validation + instructor retry logic
    预期: 比 chat() 多 ~2ms
    """
    # 类似上面，但用 chat_structured() 调用
    pass
```

### 2.2 Provider 选择延迟

```python
# tests/benchmark/test_provider_benchmark.py

from aiops.llm.providers import ProviderManager

def test_provider_selection_latency(benchmark):
    """Provider 选择延迟（含熔断检查）."""
    manager = ProviderManager()
    
    def select():
        return manager.get_available_providers(required_model="gpt-4o")
    
    result = benchmark(select)
    assert len(result) >= 1
    # 预期: < 0.01ms（纯内存操作）


def test_fallback_chain_latency(benchmark):
    """容灾链构建延迟."""
    manager = ProviderManager()
    
    def get_chain():
        return manager.get_fallback_chain("openai", "gpt-4o")
    
    result = benchmark(get_chain)
    assert len(result) >= 1
    # 预期: < 0.05ms
```

## 3. Provider 容灾压测

### 3.1 场景：主 Provider 故障

```python
# tests/benchmark/test_provider_failover.py
"""
场景: OpenAI 连续返回 5xx → 触发熔断 → 切换到 Anthropic → 60s 后恢复

验证点:
1. 前 4 次失败不触发熔断
2. 第 5 次失败后立即熔断
3. 熔断后请求不发送到 OpenAI
4. 60s 后进入 HALF_OPEN，允许 1 个探测请求
5. 探测成功 → CLOSED
6. 探测失败 → 重新 OPEN
"""
import asyncio
import time
from aiops.llm.providers import ProviderManager, CircuitState


async def test_provider_failover_timing():
    manager = ProviderManager()
    
    # Phase 1: 连续 5 次失败
    for i in range(5):
        manager.record_failure("openai", Exception(f"500 error #{i+1}"))
    
    # 验证: openai 应该被熔断
    cb = manager._circuit_breakers["openai"]
    assert cb.state == CircuitState.OPEN
    
    # 验证: 可用 provider 不包含 openai
    available = manager.get_available_providers(required_model="gpt-4o")
    assert all(p.name != "openai" for p in available)
    
    # Phase 2: 等待恢复
    # 模拟时间流逝（实际测试中用 time.sleep 或 mock time）
    cb.last_failure_time = time.monotonic() - 61  # 模拟 61 秒后
    
    # 验证: 应进入 HALF_OPEN
    assert cb.is_available(recovery_timeout=60.0)
    assert cb.state == CircuitState.HALF_OPEN
    
    # Phase 3: 探测成功
    manager.record_success("openai")
    assert cb.state == CircuitState.CLOSED


async def test_failover_chain_order():
    """验证容灾链顺序.
    
    gpt-4o 挂了应该切到:
    1. claude-3-5-sonnet (tier1_reasoning)
    2. qwen2.5-72b (tier1_reasoning, local)
    不应该切到 gpt-4o-mini (不同 tier)
    """
    manager = ProviderManager()
    chain = manager.get_fallback_chain("openai", "gpt-4o")
    
    assert len(chain) >= 1
    # 第一选择应该是 anthropic 的 claude
    assert chain[0] == ("anthropic", "claude-3-5-sonnet-20241022")
```

### 3.2 Locust 脚本：LLM 容灾场景

```python
# locustfile_llm_failover.py
"""
Locust 场景: 模拟 LLM Provider 渐进故障

Timeline:
0-2min:  正常流量 (5 RPS)
2-3min:  Mock LLM 注入 50% 错误率
3-5min:  Mock LLM 注入 100% 错误率 (触发熔断)
5-7min:  Mock LLM 恢复正常
7-8min:  验证系统自动恢复
"""
from locust import HttpUser, task, between, events
import time
import requests

MOCK_LLM_URL = "http://localhost:8001"

class DiagnosticUser(HttpUser):
    wait_time = between(0.1, 0.5)
    host = "http://localhost:8080"

    @task(7)
    def diagnose_hdfs(self):
        self.client.post("/api/v1/diagnose", json={
            "query": "HDFS NameNode heap 使用率 95%",
            "user_id": f"user_{self.environment.runner.user_count}",
            "cluster_id": "prod-01",
        }, timeout=30)

    @task(3)
    def diagnose_kafka(self):
        self.client.post("/api/v1/diagnose", json={
            "query": "Kafka consumer lag 持续增长",
            "user_id": f"user_{self.environment.runner.user_count}",
            "cluster_id": "prod-01",
        }, timeout=30)


# 故障注入调度
@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    import threading

    def inject_failures():
        time.sleep(120)  # 2 min 正常
        print("[INJECT] Setting LLM error rate to 50%")
        requests.post(f"{MOCK_LLM_URL}/v1/config", json={"error_rate": 0.5})

        time.sleep(60)   # 1 min 50% 错误
        print("[INJECT] Setting LLM error rate to 100%")
        requests.post(f"{MOCK_LLM_URL}/v1/config", json={"error_rate": 1.0})

        time.sleep(120)  # 2 min 完全故障
        print("[INJECT] Recovering LLM to normal")
        requests.post(f"{MOCK_LLM_URL}/v1/config", json={"error_rate": 0.0})

    threading.Thread(target=inject_failures, daemon=True).start()
```

## 4. Token 预算压测

### 4.1 预算耗尽场景

```python
# tests/benchmark/test_token_budget.py
"""
场景: 高频诊断请求持续消耗 Token，验证:
1. 80% 预算时触发告警
2. 100% 预算时降级（使用更便宜的模型 or 拒绝服务）
3. 预算重置后恢复

Token 预算配置:
- 日限额: 500,000 tokens
- Triage: 2,000/次
- Diagnostic: 15,000/次
- Planning: 8,000/次
- Report: 5,000/次
- 完整诊断 ≈ 30,000 tokens/次
- 日限额可支撑 ≈ 16 次完整诊断
"""

async def test_token_budget_exhaustion():
    """模拟 Token 预算耗尽."""
    from aiops.llm.budget import TokenBudgetManager

    budget = TokenBudgetManager(daily_limit=500_000)

    # 模拟 15 次完整诊断（每次 30,000 tokens）
    for i in range(15):
        budget.record_usage("triage", 2000)
        budget.record_usage("planning", 8000)
        budget.record_usage("diagnostic", 15000)
        budget.record_usage("report", 5000)
        
        remaining = budget.remaining
        pct = budget.usage_percentage

        if pct >= 0.8:
            print(f"[WARN] Iteration {i+1}: Budget at {pct*100:.0f}%, remaining: {remaining}")

    # 第 16 次应该被拒绝或降级
    assert budget.usage_percentage >= 0.9
```

### 4.2 成本追踪验证

```python
async def test_cost_tracking_accuracy():
    """验证成本追踪准确性.
    
    模型定价 (per 1M tokens):
    - deepseek-chat: $0.14 input / $0.28 output
    - gpt-4o: $2.5 input / $10 output
    - gpt-4o-mini: $0.15 input / $0.6 output
    
    一次完整诊断的预期成本:
    - Triage (deepseek): 2K tokens × $0.14/1M ≈ $0.0003
    - Planning (gpt-4o): 8K tokens × $2.5/1M ≈ $0.02
    - Diagnostic (gpt-4o): 15K tokens × $2.5/1M ≈ $0.0375
    - Report (gpt-4o-mini): 5K tokens × $0.15/1M ≈ $0.00075
    - 总计: ≈ $0.06/次
    """
    pass
```

## 5. 并发 LLM 调用安全

### 5.1 测试要点

| 测试项 | 风险 | 验证方法 |
|--------|------|----------|
| 多协程共享 LLMClient | 状态竞争 | 100 并发 asyncio.gather |
| ModelRouter 线程安全 | 路由表读写竞争 | 读写混合压测 |
| Provider 熔断器竞态 | 计数器不准确 | 并发 record_failure |
| Token 预算并发扣减 | 超支 | 100 并发 record_usage |

### 5.2 并发安全测试

```python
async def test_concurrent_llm_calls():
    """100 并发 LLM 调用不应出现竞态."""
    client = LLMClient()

    async def single_call(i):
        context = LLMCallContext(
            task_type=TaskType.TRIAGE,
            request_id=f"concurrent-{i}",
        )
        # 使用 Mock LLM
        return await client.chat(
            messages=[{"role": "user", "content": f"Test query #{i}"}],
            context=context,
        )

    # 100 并发
    tasks = [single_call(i) for i in range(100)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    errors = [r for r in results if isinstance(r, Exception)]
    assert len(errors) == 0, f"Got {len(errors)} errors in concurrent calls"
```

## 6. 关注指标

| 指标 | Prometheus Name | 阈值 |
|------|-----------------|------|
| LLM 调用延迟 | `aiops_llm_call_duration_seconds` | P95 < 5s |
| LLM 调用失败率 | `aiops_llm_call_total{status=error}` | < 5% |
| Token 消耗 | `aiops_llm_token_usage_total` | 日限额内 |
| Provider 熔断状态 | 日志 `circuit_breaker_transition` | 故障时触发 |
| 模型路由分布 | `aiops_triage_route_total{method}` | rule_engine > 60% |
