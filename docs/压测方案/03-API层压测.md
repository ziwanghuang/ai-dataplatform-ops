# 03 — API 层压测

---

## 1. Python FastAPI API 压测

### 1.1 端点清单与优先级

| 端点 | 方法 | 优先级 | 预期延迟 | 依赖 |
|------|------|--------|----------|------|
| `/api/v1/health` | GET | P2 | < 5ms | 无 |
| `/api/v1/tools` | GET | P2 | < 10ms | ToolRegistry |
| `/api/v1/diagnose` | POST | P0 | < 10s | LLM + MCP + RAG |
| `/api/v1/alert` | POST | P0 | < 15s | LLM + MCP + RAG |
| `/api/v1/agent/diagnose` | POST | P0 | < 10s | Full Graph |
| `/api/v1/agent/alert` | POST | P1 | < 15s | Full Graph |
| `/api/v1/agent/sessions/{id}` | GET | P2 | < 50ms | Memory Store |
| `/api/v1/agent/sessions/{id}/report` | GET | P2 | < 50ms | Memory Store |
| `/api/v1/approval/pending` | GET | P1 | < 100ms | HITL Gate |
| `/api/v1/approval/{id}/approve` | POST | P1 | < 200ms | HITL Gate + Redis |
| `/api/v1/approval/{id}/reject` | POST | P1 | < 200ms | HITL Gate + Redis |

### 1.2 k6 脚本：健康检查基线

```javascript
// k6/health_check.js
import http from 'k6/http';
import { check, sleep } from 'k6';
import { Rate, Trend } from 'k6/metrics';

const errorRate = new Rate('errors');
const latencyTrend = new Trend('health_latency');

export const options = {
  scenarios: {
    // 阶梯递增：50 → 200 → 500 → 1000 VU
    ramp_up: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        { duration: '30s', target: 50 },
        { duration: '1m', target: 200 },
        { duration: '1m', target: 500 },
        { duration: '2m', target: 1000 },
        { duration: '30s', target: 0 },
      ],
    },
  },
  thresholds: {
    http_req_duration: ['p(95)<10', 'p(99)<50'],  // ms
    errors: ['rate<0.01'],
    http_req_failed: ['rate<0.01'],
  },
};

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8080';

export default function () {
  const res = http.get(`${BASE_URL}/api/v1/health`);

  check(res, {
    'status is 200': (r) => r.status === 200,
    'response has status field': (r) => JSON.parse(r.body).status === 'healthy',
    'latency < 10ms': (r) => r.timings.duration < 10,
  });

  errorRate.add(res.status !== 200);
  latencyTrend.add(res.timings.duration);
}
```

### 1.3 k6 脚本：诊断请求压测

```javascript
// k6/diagnose_load.js
import http from 'k6/http';
import { check, sleep } from 'k6';
import { SharedArray } from 'k6/data';
import { Rate, Trend, Counter } from 'k6/metrics';

// ── 自定义指标 ──
const diagnoseDuration = new Trend('diagnose_duration_ms');
const diagnoseErrors = new Rate('diagnose_errors');
const routeCounter = new Counter('diagnose_routes');

// ── 测试数据 ──
const requests = new SharedArray('diagnose_requests', function () {
  return JSON.parse(open('../test_data/diagnose_requests.json'));
});

export const options = {
  scenarios: {
    // 场景 1: 常规负载 — 5 QPS 持续 5 分钟
    normal_load: {
      executor: 'constant-arrival-rate',
      rate: 5,
      timeUnit: '1s',
      duration: '5m',
      preAllocatedVUs: 20,
      maxVUs: 50,
    },
    // 场景 2: 峰值负载 — 20 QPS 持续 2 分钟
    peak_load: {
      executor: 'constant-arrival-rate',
      rate: 20,
      timeUnit: '1s',
      duration: '2m',
      preAllocatedVUs: 50,
      maxVUs: 100,
      startTime: '6m',
    },
    // 场景 3: 尖峰冲击 — 50 QPS 持续 30s
    spike: {
      executor: 'constant-arrival-rate',
      rate: 50,
      timeUnit: '1s',
      duration: '30s',
      preAllocatedVUs: 100,
      maxVUs: 200,
      startTime: '9m',
    },
  },
  thresholds: {
    'diagnose_duration_ms': ['p(50)<5000', 'p(95)<10000', 'p(99)<15000'],
    'diagnose_errors': ['rate<0.05'],
    'http_req_failed': ['rate<0.05'],
  },
};

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8080';

export default function () {
  const reqData = requests[Math.floor(Math.random() * requests.length)];

  const payload = JSON.stringify({
    query: reqData.query,
    user_id: reqData.user_id,
    cluster_id: reqData.cluster_id,
  });

  const params = {
    headers: { 'Content-Type': 'application/json' },
    timeout: '30s',
  };

  const start = new Date();
  const res = http.post(`${BASE_URL}/api/v1/diagnose`, payload, params);
  const duration = new Date() - start;

  diagnoseDuration.add(duration);

  const success = check(res, {
    'status is 200': (r) => r.status === 200,
    'has request_id': (r) => {
      try { return JSON.parse(r.body).request_id !== ''; } catch { return false; }
    },
    'has report': (r) => {
      try { return JSON.parse(r.body).report !== ''; } catch { return false; }
    },
    'latency < 15s': (r) => r.timings.duration < 15000,
  });

  diagnoseErrors.add(!success);

  // 记录路由分布
  try {
    const body = JSON.parse(res.body);
    if (body.diagnosis && body.diagnosis.route) {
      routeCounter.add(1, { route: body.diagnosis.route });
    }
  } catch {}

  sleep(0.1); // 100ms 间隔
}
```

### 1.4 k6 脚本：告警风暴压测

```javascript
// k6/alert_storm.js
import http from 'k6/http';
import { check } from 'k6';
import { SharedArray } from 'k6/data';
import { Rate, Trend } from 'k6/metrics';

const alertDuration = new Trend('alert_duration_ms');
const alertErrors = new Rate('alert_errors');
const degradedRate = new Rate('alert_degraded');

const alerts = new SharedArray('alerts', function () {
  return JSON.parse(open('../test_data/alert_requests.json'));
});

export const options = {
  scenarios: {
    // 告警风暴: 100 条告警在 10 秒内同时到达
    storm: {
      executor: 'shared-iterations',
      vus: 100,
      iterations: 100,
      maxDuration: '30s',
    },
    // 持续告警流: 2 条/秒 持续 5 分钟
    steady: {
      executor: 'constant-arrival-rate',
      rate: 2,
      timeUnit: '1s',
      duration: '5m',
      preAllocatedVUs: 10,
      maxVUs: 30,
      startTime: '1m',
    },
  },
  thresholds: {
    'alert_errors': ['rate<0.1'],
    'alert_duration_ms': ['p(95)<20000'],
  },
};

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8080';

export default function () {
  const alertData = alerts[Math.floor(Math.random() * alerts.length)];
  // 用 /api/v1/alert 批量接口
  const payload = JSON.stringify({
    alerts: [alertData],
    cluster_id: alertData.labels?.cluster || 'default',
  });

  const res = http.post(`${BASE_URL}/api/v1/alert`, payload, {
    headers: { 'Content-Type': 'application/json' },
    timeout: '30s',
  });

  alertDuration.add(res.timings.duration);

  const ok = check(res, {
    'status is 200': (r) => r.status === 200,
    'not 500': (r) => r.status !== 500,
  });

  alertErrors.add(!ok);

  // 检查是否降级
  try {
    const body = JSON.parse(res.body);
    degradedRate.add(body.status === 'accepted_degraded');
  } catch {}
}
```

### 1.5 关注指标

| 指标 | 采集方式 | 告警阈值 |
|------|----------|----------|
| `http_requests_total{status}` | Prometheus | 5xx > 5% |
| `http_request_duration_seconds` | Prometheus | P95 > 15s |
| `http_requests_in_progress` | Prometheus | > 50 |
| `aiops_triage_route_total{route}` | Prometheus | 路由分布异常 |
| k6 `http_req_duration` | k6 输出 | P99 > 30s |
| k6 `http_req_failed` | k6 输出 | > 5% |

---

## 2. Go MCP Server 压测

### 2.1 端点清单

| 端点 | 方法 | 预期延迟 | 限流 |
|------|------|----------|------|
| `/health` | GET | < 1ms | 无 |
| `/tools` | GET | < 5ms | 无 |
| `/mcp` | POST | 10-500ms | 100 RPS 全局 / 10 RPS/user |

### 2.2 k6 脚本：MCP 工具调用

```javascript
// k6/mcp_tools.js
import http from 'k6/http';
import { check, sleep } from 'k6';
import { Rate, Trend, Counter } from 'k6/metrics';

const toolDuration = new Trend('mcp_tool_duration');
const toolErrors = new Rate('mcp_tool_errors');
const cacheHits = new Counter('mcp_cache_hits');

// MCP JSON-RPC 请求模板
const TOOL_CALLS = [
  { name: 'hdfs_namenode_status', params: { cluster: 'prod-01' } },
  { name: 'hdfs_cluster_overview', params: { cluster: 'prod-01' } },
  { name: 'kafka_consumer_lag', params: { group: 'app-etl', topic: 'events' } },
  { name: 'search_logs', params: { component: 'hdfs', query: 'ERROR', limit: 50 } },
  { name: 'es_cluster_health', params: {} },
  { name: 'es_node_stats', params: {} },
  { name: 'yarn_queue_status', params: { queue: 'default' } },
  { name: 'yarn_cluster_metrics', params: {} },
  { name: 'prometheus_query_metrics', params: { query: 'up', range: '5m' } },
];

export const options = {
  scenarios: {
    // 正常: 50 RPS
    normal: {
      executor: 'constant-arrival-rate',
      rate: 50,
      timeUnit: '1s',
      duration: '3m',
      preAllocatedVUs: 50,
      maxVUs: 100,
    },
    // 触达限流: 120 RPS (超过 100 RPS 全局限制)
    rate_limit_test: {
      executor: 'constant-arrival-rate',
      rate: 120,
      timeUnit: '1s',
      duration: '1m',
      preAllocatedVUs: 100,
      maxVUs: 200,
      startTime: '4m',
    },
  },
  thresholds: {
    'mcp_tool_duration': ['p(95)<500', 'p(99)<2000'],
    'mcp_tool_errors': ['rate<0.05'],
  },
};

const MCP_URL = __ENV.MCP_URL || 'http://localhost:3000';
let callId = 0;

export default function () {
  const tool = TOOL_CALLS[Math.floor(Math.random() * TOOL_CALLS.length)];
  callId++;

  const payload = JSON.stringify({
    jsonrpc: '2.0',
    id: callId,
    method: 'tools/call',
    params: { name: tool.name, arguments: tool.params },
  });

  const res = http.post(`${MCP_URL}/mcp`, payload, {
    headers: {
      'Content-Type': 'application/json',
      'X-Trace-ID': `k6-${__VU}-${__ITER}`,
      'X-User-ID': `user_${__VU % 10}`,  // 10 个模拟用户
    },
    timeout: '10s',
  });

  toolDuration.add(res.timings.duration);

  const ok = check(res, {
    'status is 200': (r) => r.status === 200,
    'no error in body': (r) => {
      try {
        const body = JSON.parse(r.body);
        return !body.error;
      } catch { return false; }
    },
  });

  toolErrors.add(!ok);

  // 检查 429 (限流)
  if (res.status === 429) {
    // 限流是预期行为，不算错误
    toolErrors.add(false);
  }

  sleep(0.01);
}
```

### 2.3 限流精度验证

```javascript
// k6/rate_limit_verify.js
// 用 vegeta 更精确，但 k6 也可以做

import http from 'k6/http';
import { Counter } from 'k6/metrics';

const acceptedCount = new Counter('accepted');
const rejectedCount = new Counter('rejected');

export const options = {
  scenarios: {
    // 精确发送 150 RPS（超过 100 RPS 限制）
    constant_rate: {
      executor: 'constant-arrival-rate',
      rate: 150,
      timeUnit: '1s',
      duration: '1m',
      preAllocatedVUs: 200,
      maxVUs: 300,
    },
  },
};

const MCP_URL = __ENV.MCP_URL || 'http://localhost:3000';

export default function () {
  const res = http.post(`${MCP_URL}/mcp`, JSON.stringify({
    jsonrpc: '2.0', id: 1,
    method: 'tools/call',
    params: { name: 'hdfs_namenode_status', arguments: {} },
  }), {
    headers: { 'Content-Type': 'application/json', 'X-User-ID': 'perf-test' },
  });

  if (res.status === 200) acceptedCount.add(1);
  else if (res.status === 429) rejectedCount.add(1);
}

export function handleSummary(data) {
  const accepted = data.metrics.accepted?.values?.count || 0;
  const rejected = data.metrics.rejected?.values?.count || 0;
  const total = accepted + rejected;
  const acceptRate = total > 0 ? (accepted / total * 100).toFixed(1) : 0;

  console.log(`=== 限流精度验证 ===`);
  console.log(`总请求: ${total}, 通过: ${accepted} (${acceptRate}%), 拒绝: ${rejected}`);
  console.log(`预期通过率: ~66.7% (100/150)`);
  console.log(`偏差: ${Math.abs(parseFloat(acceptRate) - 66.7).toFixed(1)}%`);

  return {};
}
```

### 2.4 Go 微基准测试

```go
// go/internal/middleware/ratelimit_bench_test.go
package middleware_test

import (
    "context"
    "testing"

    "github.com/ziwang/aiops-mcp/internal/middleware"
    "github.com/ziwang/aiops-mcp/internal/protocol"
)

func BenchmarkRateLimitMiddleware(b *testing.B) {
    cfg := middleware.RateLimitConfig{
        GlobalRPS:  1000000, // 不触发限流
        PerUserRPS: 1000000,
    }
    mw := middleware.RateLimitMiddleware(cfg)
    handler := mw(func(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
        return &protocol.ToolResult{Content: []protocol.Content{{Type: "text", Text: "ok"}}}, nil
    })

    ctx := context.Background()
    params := map[string]interface{}{"test": true}

    b.ResetTimer()
    b.RunParallel(func(pb *testing.PB) {
        for pb.Next() {
            handler(ctx, params)
        }
    })
}

func BenchmarkCacheMiddleware(b *testing.B) {
    // 类似结构，测试缓存命中和未命中路径
}

func BenchmarkFullMiddlewareChain(b *testing.B) {
    // 8 层中间件完整链路的开销
}
```

### 2.5 关注指标

| 指标 | 来源 | 预期 |
|------|------|------|
| Go HTTP ReadTimeout 触发次数 | Go 日志 | 0 (正常负载) |
| Goroutine 数量 | `runtime.NumGoroutine()` | < 500 |
| GC Pause | `runtime/debug.ReadGCStats` | P99 < 10ms |
| 限流拒绝率 | k6 rejected/total | ~33% at 150 RPS |
| 熔断器状态变化 | Go 日志 | 压力注入时触发 |
| 缓存命中率 | Go `/health` stats | > 50% (重复工具调用) |
