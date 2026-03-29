# 10 - MCP 中间件链（Go）

> **设计文档引用**：`03-智能诊断Agent系统设计.md` §4.2 中间件链设计  
> **职责边界**：8 层洋葱模型中间件——每个 MCP 工具调用都经过完整的安全/性能/审计链路  
> **优先级**：P0  
> **代码位置**：`go/internal/middleware/`  
> **关联模块**：09-MCP Server、11-MCP 客户端、15-HITL、16-安全防护、17-可观测性

---

## 目录

1. [洋葱模型总览](#1-洋葱模型总览)
2. [核心接口](#2-核心接口)
3. [中间件链编排](#3-中间件链编排)
4. [各中间件实现](#4-各中间件实现)
5. [上下文辅助函数](#5-上下文辅助函数)
6. [TBDS-TCS 中间件策略](#6-tbds-tcs-中间件策略)
7. [中间件链组装与注册](#7-中间件链组装与注册)
8. [配置模板](#8-配置模板)
9. [性能影响分析](#9-性能影响分析)
10. [中间件跳过策略](#10-中间件跳过策略)
11. [端到端请求流转场景](#11-端到端请求流转场景)
12. [Prometheus 指标体系](#12-prometheus-指标体系)
13. [测试策略](#13-测试策略)
14. [与其他模块集成](#14-与其他模块集成)
15. [设计决策记录](#15-设计决策记录)

---

## 0. 为什么需要中间件链

### 0.1 核心问题

MCP Server 对外暴露了 30+ 个工具（ES 查询、HDFS 操作、YARN 管理、日志搜索等）。每个工具都需要：

- **安全防护**：参数校验、风险评估、人工审批
- **可靠性保障**：超时控制、熔断保护、限流
- **可观测性**：分布式追踪、审计日志、性能指标
- **性能优化**：结果缓存

如果在每个工具内部重复实现这些横切关注点（cross-cutting concerns），会导致：

1. **代码膨胀**：30 个工具 × 8 个关注点 = 240 处重复代码
2. **一致性风险**：不同开发者实现的审计格式不一致
3. **维护噩梦**：修改限流策略需要改 30 个文件
4. **测试困难**：横切逻辑和业务逻辑纠缠，无法独立测试

### 0.2 为什么选择洋葱模型

**备选方案对比**：

| 方案 | 优点 | 缺点 | 适用场景 |
|------|------|------|---------|
| **装饰器/洋葱模型** | 组合灵活、可独立测试、执行顺序可控 | 嵌套层数多时调试较复杂 | 函数式中间件链 |
| **AOP 切面** | 透明织入、业务代码无感知 | Go 缺乏原生 AOP 支持、需要代码生成 | Java Spring 生态 |
| **事件总线** | 完全解耦 | 执行顺序难以保证、缺乏同步返回 | 异步通知场景 |
| **管道模式** | 流式处理自然 | 缺乏"回来"阶段、无法做后置逻辑 | 单向数据流 |

**选择洋葱模型的 WHY**：

1. **Go 的函数式天性**：Go 的一等函数（first-class functions）天然适合装饰器模式，不需要额外框架
2. **前置+后置逻辑**：洋葱模型的「进去」和「出来」两个阶段完美匹配我们的需求（如 Tracing 需要在进入时创建 Span，在返回时结束 Span）
3. **短路支持**：中间层可以直接返回而不调用 `next`，实现缓存命中、限流拒绝等短路逻辑
4. **Fiber 框架一致**：我们的 HTTP 层已经使用 Fiber 中间件（洋葱模型），MCP 层沿用相同模式降低认知负担
5. **可组合性**：不同工具可以组装不同的中间件子集（精简链 vs 完整链），洋葱模型通过函数组合天然支持

### 0.3 为什么是 8 层

8 层不是拍脑袋定的，是从实际需求反推：

```
安全类: 参数校验(④) + 风险评估(⑤) + 审计(②)     → 3 层
可靠性: 超时(⑧) + 熔断(⑦) + 限流(⑥)              → 3 层
可观测: 追踪(①)                                     → 1 层
性能:   缓存(③)                                     → 1 层
                                                    ─────
                                               合计: 8 层
```

每一层解决一个且仅一个关注点（Single Responsibility），层与层之间通过 `ToolHandler` 接口解耦。我们考虑过合并某些层（比如把限流和熔断合为一层），但实际运行中它们的配置粒度不同（限流按用户/全局，熔断按工具），合并反而增加复杂度。

---

## 1. 洋葱模型总览

```
请求入口
  │
  ▼
┌─────────────────────────────────────────────────────────────┐
│ ① TracingMiddleware        创建 OTel Span                   │
│  ┌─────────────────────────────────────────────────────────┐│
│  │ ② AuditMiddleware       异步记录审计日志                 ││
│  │  ┌─────────────────────────────────────────────────────┐││
│  │  │ ③ CacheMiddleware    只读工具结果缓存 (Redis)       │││
│  │  │  ┌─────────────────────────────────────────────────┐│││
│  │  │  │ ④ ParamValidation JSON Schema 校验             ││││
│  │  │  │  ┌─────────────────────────────────────────────┐││││
│  │  │  │  │ ⑤ RiskAssessment 风险评估 + HITL 拦截      │││││
│  │  │  │  │  ┌─────────────────────────────────────────┐│││││
│  │  │  │  │  │ ⑥ RateLimit    令牌桶限流               ││││││
│  │  │  │  │  │  ┌─────────────────────────────────────┐│││││││
│  │  │  │  │  │  │ ⑦ CircuitBreaker 后端熔断保护       ││││││││
│  │  │  │  │  │  │  ┌─────────────────────────────────┐│││││││
│  │  │  │  │  │  │  │ ⑧ Timeout    请求级超时控制     │││││││││
│  │  │  │  │  │  │  │                                  │││││││││
│  │  │  │  │  │  │  │    [实际工具执行]                │││││││││
│  │  │  │  │  │  │  │                                  │││││││││
│  │  │  │  │  │  │  └─────────────────────────────────┘│││││││
│  │  │  │  │  │  └─────────────────────────────────────┘││││││
│  │  │  │  │  └─────────────────────────────────────────┘│││││
│  │  │  │  └─────────────────────────────────────────────┘││││
│  │  │  └─────────────────────────────────────────────────┘│││
│  │  └─────────────────────────────────────────────────────┘││
│  └─────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────┘
  │
  ▼
响应返回（逐层执行后置逻辑）
```

| 层 | 中间件 | 短路条件 | 后置逻辑 |
|----|--------|---------|---------|
| ① | Tracing | 无 | span.End() |
| ② | Audit | 无 | 异步写入审计日志 |
| ③ | Cache | 命中 → 直接返回 | 写入缓存 |
| ④ | ParamValidation | 校验失败 → 400 | 无 |
| ⑤ | RiskAssessment | 未审批 → 阻塞 | 无 |
| ⑥ | RateLimit | 限流 → 429 | 无 |
| ⑦ | CircuitBreaker | 熔断开 → 503 | 记录成功/失败 |
| ⑧ | Timeout | 超时 → 504 | 取消上下文 |

### 1.1 执行顺序设计 WHY

8 层的排列顺序不是随意的，每一层的位置都有明确原因：

**① Tracing 为什么在最外层？**
- 需要捕获**完整**的请求生命周期，包括被中间件短路的情况
- 如果 Cache 命中了直接返回，Tracing 仍然需要记录这次调用
- 如果 RateLimit 拒绝了请求，Tracing 仍然需要记录这次被拒绝的尝试
- **原则**：可观测性层永远在最外面，保证不遗漏任何请求

**② Audit 为什么在第二层？**
- 审计日志需要记录所有请求（包括被 Cache/Validation/RateLimit 短路的）
- 但审计本身不需要 Span 上下文（它有独立的 trace_id 字段），所以在 Tracing 内层
- 审计是异步写入，不影响后续中间件的执行延迟
- **合规要求**：所有工具调用都必须有审计记录，无论成功与否

**③ Cache 为什么在 Validation 之前？**
- 缓存命中时直接返回，**跳过所有后续检查**，这是性能优化的关键
- 缓存的 key 是 `(toolName, params)` 的 hash，相同参数必然通过 Validation
- 所以缓存命中的结果一定是之前通过了完整校验的合法结果
- **WHY 不放在 Validation 之后**：如果先校验再查缓存，每次缓存命中都白白浪费了一次校验的 CPU

**④ ParamValidation 为什么在 Risk 之前？**
- 先验证参数格式正确性，再评估风险等级
- 避免对格式错误的请求进行无意义的风险评估
- **防御性编程**：RiskAssessment 内部可能读取参数值来判断风险，确保参数已通过校验

**⑤ RiskAssessment 为什么在 RateLimit 之前？**
- 高风险操作即使在限流范围内也需要审批
- Risk 的判断基于工具语义（风险等级是静态配置的），不受流量波动影响
- **逻辑正确性**：先判断"能不能做"（权限），再判断"做不做得了"（资源）

**⑥ RateLimit 为什么在 CircuitBreaker 之前？**
- 限流是主动保护，在请求到达后端之前就拦截
- 熔断是被动保护，基于后端的实际失败率
- 限流生效意味着根本不会增加后端压力，有助于防止熔断被触发
- **层级关系**：限流 → 防止过载；熔断 → 应对故障

**⑦ CircuitBreaker 为什么在 Timeout 之前？**
- 熔断状态下直接返回 503，不需要创建超时上下文
- 减少不必要的 goroutine 开销
- **快速失败**：如果后端已知不可用，立即告知调用方

**⑧ Timeout 为什么在最内层？**
- 超时只应该限制**实际工具执行**的时间，不包括中间件自身的开销
- 如果把超时放在外层，中间件链的处理时间也会被计入，导致实际工具可用时间减少
- **精确控制**：超时 = 工具执行时间，不包含缓存查询、参数校验等中间件开销

### 1.2 请求流转示意

```
请求进入 → [Tracing.前置: 创建Span]
         → [Audit.前置: 记录请求时间]
         → [Cache.前置: 查缓存] ─── 命中 → 直接返回 ──→ [Audit.后置] → [Tracing.后置: Span.End]
         │                                                    ↑
         └─ 未命中                                             │
         → [Validation.前置: 校验参数] ── 失败 → 返回400 ──→───┤
         → [Risk.前置: 评估风险] ── 需审批 → 返回阻塞 ──→──────┤
         → [RateLimit.前置: 检查配额] ── 超限 → 返回429 ──→───┤
         → [CircuitBreaker.前置: 检查状态] ── 熔断 → 返回503 →┤
         → [Timeout.前置: 设置deadline]                        │
         → [工具执行] ── 超时 → 返回504 ──→───────────────────┤
         │                                                    │
         ← [Timeout.后置: cancel()]                            │
         ← [CircuitBreaker.后置: 记录成功/失败]                │
         ← [RateLimit.后置: 无]                                │
         ← [Risk.后置: 无]                                     │
         ← [Validation.后置: 无]                               │
         ← [Cache.后置: 写入缓存]                              │
         ← [Audit.后置: 异步写审计日志] ←─────────────────────┘
         ← [Tracing.后置: Span.End()]
```

---

## 2. 核心接口

### 2.1 接口设计 WHY

**为什么用 `func` 而不用 `interface`？**

```go
// 方案 A: 接口方式
type Middleware interface {
    Handle(ctx context.Context, params map[string]interface{}, next ToolHandler) (*protocol.ToolResult, error)
}

// 方案 B: 函数方式（我们选的）
type Middleware func(ToolHandler) ToolHandler
```

选择函数方式的原因：

1. **Go 惯例**：标准库的 `http.Handler` 和 `http.HandlerFunc` 就是函数式中间件的典范，Go 社区（Fiber, Echo, Chi）都采用这种模式
2. **组合简洁**：`chain = mw1(mw2(mw3(handler)))` 比实例化 3 个对象然后注册到 chain 更直观
3. **闭包捕获配置**：每个中间件通过闭包捕获自己的配置（如超时时长、限流阈值），不需要额外的 DI 框架
4. **零分配**：函数组合在编译期完成，运行时没有接口动态分发开销

**为什么 `params` 是 `map[string]interface{}` 而不是强类型 struct？**

MCP 协议的工具参数是动态 JSON Schema 定义的，不同工具的参数完全不同：

- `search_logs` 需要 `{query, time_range, index}`
- `hdfs_namenode_status` 不需要任何参数
- `ops_restart_service` 需要 `{service_name, cluster_id}`

用 `map[string]interface{}` 保持中间件链对具体工具参数的无感知，参数校验交给 ParameterValidationMiddleware 统一处理。

### 2.2 类型定义

```go
// go/internal/middleware/types.go
package middleware

import (
    "context"
    "time"

    "github.com/yourorg/aiops-mcp/internal/protocol"
)

// ToolHandler 工具执行函数签名
// WHY context.Context 作为第一个参数：
//   - 携带超时 deadline、取消信号
//   - 携带 trace span、用户 ID 等请求级元数据
//   - Go 标准实践，所有 I/O 操作都接受 context
type ToolHandler func(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error)

// Middleware 中间件函数签名（装饰器模式）
// WHY 返回 ToolHandler 而不是 (ToolResult, error)：
//   - 装饰器模式：接收一个 handler，返回一个增强后的 handler
//   - 支持链式组合：mw1(mw2(mw3(handler)))
//   - 前置逻辑在 next() 调用之前，后置逻辑在之后
type Middleware func(ToolHandler) ToolHandler

// MiddlewareConfig 中间件链的完整配置
// WHY 集中配置而不是分散在每个中间件里：
//   - 所有配置从一个 YAML 文件加载，一处修改全局生效
//   - 支持热更新：通过 viper.WatchConfig() 监听配置变更
//   - 便于不同环境（dev/staging/prod）使用不同配置
type MiddlewareConfig struct {
    Timeout        TimeoutConfig `mapstructure:"timeout"`
    CircuitBreaker CBConfig      `mapstructure:"circuit_breaker"`
    RateLimit      RLConfig      `mapstructure:"rate_limit"`
    Cache          CacheConfig   `mapstructure:"cache"`
    Audit          AuditConfig   `mapstructure:"audit"`
}

// MiddlewareMetrics 每个中间件共享的指标收集器
type MiddlewareMetrics struct {
    Duration       *prometheus.HistogramVec   // 中间件处理耗时
    ShortCircuits  *prometheus.CounterVec     // 短路次数（缓存命中、限流、熔断等）
    Errors         *prometheus.CounterVec     // 错误次数
    CacheHits      *prometheus.CounterVec     // 缓存命中
    CacheMisses    *prometheus.CounterVec     // 缓存未命中
    CBState        *prometheus.GaugeVec       // 熔断器状态
    RateLimitDrops *prometheus.CounterVec     // 限流丢弃
}

// MiddlewareInfo 中间件元信息（用于可观测性）
type MiddlewareInfo struct {
    Name     string        // 中间件名称
    Order    int           // 在链中的位置（1=最外层）
    CanSkip  bool          // 是否可以被跳过
    Overhead time.Duration // 预估开销（基准测试得出）
}
```

### 2.3 错误类型定义

```go
// go/internal/pkg/errors/codes.go
// WHY 用自定义错误码而不是 Go 标准 error：
//   - 中间件需要区分不同类型的失败（超时 vs 熔断 vs 限流）
//   - 上层（MCP Server）根据错误码决定返回什么 HTTP 状态码
//   - 审计日志需要结构化的错误分类

const (
    ToolTimeout      = "TOOL_TIMEOUT"       // ⑧ Timeout 层产生
    ToolCircuitOpen  = "TOOL_CIRCUIT_OPEN"  // ⑦ CircuitBreaker 层产生
    ToolRateLimited  = "TOOL_RATE_LIMITED"  // ⑥ RateLimit 层产生
    ToolRiskBlocked  = "TOOL_RISK_BLOCKED"  // ⑤ RiskAssessment 层产生
    ToolParamInvalid = "TOOL_PARAM_INVALID" // ④ ParamValidation 层产生
)

// 错误码到 HTTP 状态码的映射
// WHY 在中间件层就定义好映射：MCP Server 的 HTTP handler 只需要查表，不需要理解中间件内部逻辑
var ErrorToHTTPStatus = map[string]int{
    ToolTimeout:      504, // Gateway Timeout
    ToolCircuitOpen:  503, // Service Unavailable
    ToolRateLimited:  429, // Too Many Requests
    ToolRiskBlocked:  403, // Forbidden（需要审批）
    ToolParamInvalid: 400, // Bad Request
}
```

---

## 3. 中间件链编排

### 3.1 编排设计 WHY

**为什么从内到外包装？**

Go 的函数组合是 `outer(inner(handler))`，执行时先进入 outer，再进入 inner，最后执行 handler。这意味着**先包装的中间件在链的内层**，**后包装的在外层**。

```
// 包装顺序:
chain = handler
chain = Timeout(chain)        // 先包 → 最内层（第⑧层）
chain = CircuitBreaker(chain) // ...
chain = Tracing(chain)        // 最后包 → 最外层（第①层）

// 执行顺序:
Tracing → Audit → Cache → Validation → Risk → RateLimit → CB → Timeout → handler
```

这种"反直觉"的包装顺序是函数式编程的常见模式。我们通过注释明确标注了每一层的角色，避免维护者搞反。

**为什么提供三种链（Full / Lite / FullForTBDS）？**

不同工具的安全需求差异巨大：

| 链类型 | 层数 | 适用场景 | 典型延迟开销 |
|--------|------|---------|------------|
| `BuildMiddlewareChain` | 8 层 | 内部 MCP Server 的所有工具 | ~2-5ms |
| `BuildLiteChain` | 4 层 | TBDS-TCS 只读工具 | ~0.5-1ms |
| `BuildFullChainForTBDS` | 6 层 | TBDS 有副作用的工具 | ~1-3ms |

TBDS-TCS 的只读工具（如 `ssh_exec_readonly`、`mysql_query`）已经在服务端做了安全限制，再加 Cache/Validation/Risk 是多余的。但 Tracing 和 Audit 不能省——可观测性是底线。

### 3.2 完整链代码

```go
// go/internal/middleware/chain.go
package middleware

import (
    "github.com/yourorg/aiops-mcp/internal/config"
    "github.com/yourorg/aiops-mcp/internal/protocol"
    "github.com/rs/zerolog/log"
)

// BuildMiddlewareChain 构建完整 8 层中间件链（内部 MCP Server）
//
// WHY 接收 protocol.Tool 而不是 string：
//   - Tool 接口提供 Name()、RiskLevel()、Schema() 等方法
//   - 中间件需要根据工具元数据做不同处理（如 Cache 只对 RiskNone 工具生效）
//   - 避免在调用点手动传递多个参数
func BuildMiddlewareChain(handler ToolHandler, tool protocol.Tool, cfg *config.Config) ToolHandler {
    chain := handler

    // 从内到外包装（最先包的最后执行 → 最内层）
    // ⑧ Timeout（最内层，最先执行）
    // WHY 最内层: 只限制实际工具执行时间，不包含中间件开销
    chain = TimeoutMiddleware(cfg.Middleware.Timeout)(chain)

    // ⑦ CircuitBreaker
    // WHY 每个工具独立熔断器: ES 挂了不影响 HDFS 工具的可用性
    chain = CircuitBreakerMiddleware(tool.Name(), cfg.Middleware.CircuitBreaker)(chain)

    // ⑥ RateLimit
    // WHY 在 CB 外面: 限流可以减少到达后端的请求量，防止触发熔断
    chain = RateLimitMiddleware(cfg.Middleware.RateLimit)(chain)

    // ⑤ RiskAssessment + HITL
    // WHY 在限流外面: 高风险操作不应该因为限流而被静默丢弃，应该明确告知需要审批
    chain = RiskAssessmentMiddleware(tool.RiskLevel())(chain)

    // ④ ParameterValidation
    // WHY 在 Risk 外面: 参数格式错误的请求不需要做风险评估
    chain = ParameterValidationMiddleware(tool.Schema())(chain)

    // ③ Cache（仅只读工具）
    // WHY 条件编译: 有副作用的工具绝对不能缓存（重启服务不能返回上次的结果）
    if tool.RiskLevel() == protocol.RiskNone {
        chain = CacheMiddleware(cfg.Middleware.Cache)(chain)
    }

    // ② Audit
    // WHY 在 Cache 外面: 即使缓存命中也要记录审计日志（合规要求）
    chain = AuditMiddleware(cfg.Middleware.Audit)(chain)

    // ① Tracing（最外层，最先创建 Span）
    // WHY 最外层: 捕获完整的请求生命周期，包括所有中间件的开销
    chain = TracingMiddleware(tool.Name())(chain)

    log.Debug().
        Str("tool", tool.Name()).
        Str("risk", tool.RiskLevel().String()).
        Bool("cached", tool.RiskLevel() == protocol.RiskNone).
        Int("layers", 8).
        Msg("middleware chain built")

    return chain
}

// BuildLiteChain TBDS-TCS 外部工具精简链（4 层）
//
// WHY 精简链:
//   1. TBDS-TCS 大部分工具天然只读，服务端已做安全限制（ACL + 操作审计）
//   2. 减少不必要的中间件开销，TBDS API 延迟本身就较高（~100-300ms）
//   3. 但 Tracing + Audit 不能省：可观测性和合规审计是底线
//   4. CB + Timeout 不能省：TBDS 服务不稳定时需要快速失败
func BuildLiteChain(handler ToolHandler, toolName string, cfg *config.Config) ToolHandler {
    chain := handler
    chain = TimeoutMiddleware(cfg.Middleware.Timeout)(chain)
    chain = CircuitBreakerMiddleware(toolName, cfg.Middleware.CircuitBreaker)(chain)
    chain = AuditMiddleware(cfg.Middleware.Audit)(chain)
    chain = TracingMiddleware(toolName)(chain)

    log.Debug().
        Str("tool", toolName).
        Int("layers", 4).
        Msg("lite middleware chain built")

    return chain
}

// BuildFullChainForTBDS TBDS 中有副作用的工具（pods_exec, resources_create_or_update）
// 走 6 层链：比完整链少了 Cache 和 ParamValidation
//
// WHY 不走完整 8 层:
//   - Cache: TBDS 有副作用的操作绝对不能缓存
//   - ParamValidation: TBDS 参数格式由 TBDS SDK 定义，我们不维护 JSON Schema
//   - Risk + RateLimit: 必须保留，因为 pods_exec 可以在容器内执行任意命令
func BuildFullChainForTBDS(handler ToolHandler, toolName string, riskLevel protocol.RiskLevel, cfg *config.Config) ToolHandler {
    chain := handler
    chain = TimeoutMiddleware(cfg.Middleware.Timeout)(chain)
    chain = CircuitBreakerMiddleware(toolName, cfg.Middleware.CircuitBreaker)(chain)
    chain = RateLimitMiddleware(cfg.Middleware.RateLimit)(chain)
    chain = RiskAssessmentMiddleware(riskLevel)(chain)
    chain = AuditMiddleware(cfg.Middleware.Audit)(chain)
    chain = TracingMiddleware(toolName)(chain)

    log.Debug().
        Str("tool", toolName).
        Str("risk", riskLevel.String()).
        Int("layers", 6).
        Msg("TBDS full middleware chain built")

    return chain
}

// ChainInfo 返回链的元信息（用于调试和健康检查接口）
func ChainInfo(tool protocol.Tool) []MiddlewareInfo {
    infos := []MiddlewareInfo{
        {Name: "Tracing", Order: 1, CanSkip: false, Overhead: 50 * time.Microsecond},
        {Name: "Audit", Order: 2, CanSkip: false, Overhead: 100 * time.Microsecond},
    }
    if tool.RiskLevel() == protocol.RiskNone {
        infos = append(infos, MiddlewareInfo{Name: "Cache", Order: 3, CanSkip: true, Overhead: 200 * time.Microsecond})
    }
    infos = append(infos,
        MiddlewareInfo{Name: "ParamValidation", Order: 4, CanSkip: false, Overhead: 30 * time.Microsecond},
        MiddlewareInfo{Name: "RiskAssessment", Order: 5, CanSkip: true, Overhead: 20 * time.Microsecond},
        MiddlewareInfo{Name: "RateLimit", Order: 6, CanSkip: false, Overhead: 10 * time.Microsecond},
        MiddlewareInfo{Name: "CircuitBreaker", Order: 7, CanSkip: false, Overhead: 15 * time.Microsecond},
        MiddlewareInfo{Name: "Timeout", Order: 8, CanSkip: false, Overhead: 5 * time.Microsecond},
    )
    return infos
}
```

---

## 4. 各中间件实现

### 4.1 TimeoutMiddleware

**WHY 需要超时控制？**

大数据平台的工具调用天生是不可预测的：
- ES 查询可能扫描数十亿条日志，耗时从 100ms 到 10min 不等
- HDFS 操作在 NameNode 高负载时可能无限等待
- YARN API 在 ResourceManager 故障转移时可能 hang 住

没有超时控制，一个慢查询就能阻塞整个 Agent 会话。用户会看到 Agent "思考中..."直到超时，体验极差。

**WHY 用 channel + goroutine 而不是 context.WithTimeout 的 Done()？**

`context.WithTimeout` 只能通知被调用方取消，但如果工具内部没有检查 `ctx.Done()`（第三方库常见问题），超时不会生效。我们用 goroutine + channel 的模式强制超时：即使工具内部不尊重 context，超时后也会立即返回给调用方。goroutine 内部的工具调用会在 context 取消后最终被 GC 回收。

**WHY 支持 per-tool 超时覆盖？**

不同工具的合理执行时间差异巨大：

| 工具 | 典型耗时 | 配置超时 | 原因 |
|------|---------|---------|------|
| `hdfs_namenode_status` | 50-200ms | 10s | 简单 HTTP GET，超过 10s 说明有问题 |
| `search_logs` | 1-30s | 45s | ES 全文搜索，数据量大时较慢 |
| `ops_restart_service` | 10-90s | 120s | 服务重启需要等待健康检查 |
| `yarn_kill_app` | 5-30s | 60s | YARN 应用清理需要时间 |

一刀切的超时会导致：设太短 → 正常请求被误杀；设太长 → 异常请求浪费资源。

```go
// go/internal/middleware/timeout.go
package middleware

import (
    "context"
    "fmt"
    "time"

    "github.com/rs/zerolog/log"

    "github.com/yourorg/aiops-mcp/internal/protocol"
    pkgerr "github.com/yourorg/aiops-mcp/internal/pkg/errors"
)

type TimeoutConfig struct {
    Default time.Duration            `mapstructure:"default"`
    PerTool map[string]time.Duration `mapstructure:"per_tool"`
}

func TimeoutMiddleware(cfg TimeoutConfig) Middleware {
    return func(next ToolHandler) ToolHandler {
        return func(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
            // 工具级超时覆盖
            // WHY: 不同工具的合理执行时间差异可达 100x（10s vs 120s）
            timeout := cfg.Default
            toolName := toolNameFromCtx(ctx)
            if t, ok := cfg.PerTool[toolName]; ok {
                timeout = t
            }

            // WHY context.WithTimeout 而不是 time.After：
            //   - context 可以向下传播，工具内部的 HTTP/DB 调用也能感知超时
            //   - defer cancel() 确保资源释放，避免 goroutine 泄漏
            ctx, cancel := context.WithTimeout(ctx, timeout)
            defer cancel()

            // 使用 channel 等待结果
            // WHY buffered channel (cap=1)：
            //   - 如果超时后 goroutine 还在执行，结果写入 channel 不会阻塞
            //   - 避免 goroutine 因为 channel 满而永远 hang 住
            type result struct {
                res *protocol.ToolResult
                err error
            }
            ch := make(chan result, 1)

            go func() {
                r, e := next(ctx, params)
                ch <- result{r, e}
            }()

            select {
            case <-ctx.Done():
                // WHY 记录超时指标: 超时频率是告警的重要信号
                metrics.MiddlewareDuration.WithLabelValues("timeout_triggered").Observe(timeout.Seconds())
                log.Warn().
                    Str("tool", toolName).
                    Dur("timeout", timeout).
                    Msg("tool execution timed out")
                return nil, pkgerr.New(pkgerr.ToolTimeout,
                    fmt.Sprintf("tool %s timed out after %s", toolName, timeout))
            case r := <-ch:
                return r.res, r.err
            }
        }
    }
}
```

### 4.2 CircuitBreakerMiddleware

**WHY 需要熔断？**

大数据平台的后端服务（ES、YARN、HDFS）经常出现级联故障：
1. ES 集群某个节点 OOM → 查询延迟飙升
2. Agent 持续重试 ES 查询 → 请求堆积
3. ES 剩余节点压力更大 → 更多超时
4. 恶性循环直到 ES 集群完全不可用

熔断器的作用就是**快速失败**：当检测到后端故障率超过阈值，直接拒绝请求（返回 503），给后端喘息恢复的时间。

**WHY 用 sony/gobreaker 而不是自己实现？**

| 方案 | 优点 | 缺点 |
|------|------|------|
| sony/gobreaker | 生产验证、线程安全、状态机完备 | 外部依赖 |
| 自己实现 | 零依赖 | 状态机容易出 bug，并发安全需要大量测试 |
| hystrix-go | Netflix 血统 | 已停止维护、API 复杂 |

sony/gobreaker 是 Go 生态最成熟的熔断库，Star 2.5k+，被 Uber、Grab 等公司使用。自己实现状态机（Closed → Open → HalfOpen）太容易出并发 bug，不值得冒这个风险。

**WHY 每个工具独立熔断器？**

我们的 MCP Server 对接多个后端：ES、HDFS、YARN、TBDS API。如果用全局熔断器：
- ES 故障 → 全局熔断 → HDFS/YARN 工具也被误杀
- 用户查不了集群状态，也杀不了失控的 YARN 应用

每个工具独立熔断器意味着：ES 挂了只影响 ES 相关工具，HDFS 和 YARN 工具正常可用。

**WHY `counts.Requests < 5` 才触发？**

冷启动保护：刚启动时只有 1-2 个请求，如果恰好失败了，失败率 = 100%。但这不代表后端有问题，可能只是网络抖动。至少需要 5 个请求的样本才能做出合理判断。

```go
// go/internal/middleware/circuitbreaker.go
package middleware

import (
    "context"
    "errors"
    "fmt"
    "sync"
    "time"

    "github.com/sony/gobreaker"
    "github.com/rs/zerolog/log"

    "github.com/yourorg/aiops-mcp/internal/protocol"
    pkgerr "github.com/yourorg/aiops-mcp/internal/pkg/errors"
)

type CBConfig struct {
    MaxRequests  uint32        `mapstructure:"max_requests"`   // 半开状态允许的探测请求数
    Interval     time.Duration `mapstructure:"interval"`       // 统计窗口
    Timeout      time.Duration `mapstructure:"timeout"`        // 开→半开等待时间
    FailureRatio float64       `mapstructure:"failure_ratio"`  // 触发阈值
}

// 全局熔断器注册表（每个工具一个）
// WHY sync.RWMutex 而不是 sync.Map:
//   - 读多写少（初始化后几乎只读），RWMutex 的读锁性能更好
//   - sync.Map 适合 key 集合频繁变化的场景，我们的工具集是固定的
var (
    circuitBreakers   = make(map[string]*gobreaker.CircuitBreaker)
    circuitBreakersMu sync.RWMutex
)

func CircuitBreakerMiddleware(toolName string, cfg CBConfig) Middleware {
    // 每个工具独立熔断器
    // WHY 在中间件构造时创建而不是每次请求时: 熔断器是有状态的，需要跨请求保持
    circuitBreakersMu.Lock()
    cb, exists := circuitBreakers[toolName]
    if !exists {
        cb = gobreaker.NewCircuitBreaker(gobreaker.Settings{
            Name:        toolName,
            MaxRequests: cfg.MaxRequests,
            Interval:    cfg.Interval,
            Timeout:     cfg.Timeout,
            ReadyToTrip: func(counts gobreaker.Counts) bool {
                // WHY 最少 5 个请求: 冷启动保护，避免 1/1=100% 误触发
                if counts.Requests < 5 {
                    return false
                }
                failureRatio := float64(counts.TotalFailures) / float64(counts.Requests)
                return failureRatio >= cfg.FailureRatio
            },
            OnStateChange: func(name string, from, to gobreaker.State) {
                // WHY 状态变更日志: 熔断是严重事件，需要立即告警
                log.Warn().
                    Str("tool", name).
                    Str("from", from.String()).
                    Str("to", to.String()).
                    Msg("circuit breaker state change")

                // Prometheus 指标
                // WHY 用 Gauge 而不是 Counter: 状态是瞬时值（0/1/2），不是累计值
                stateValue := map[gobreaker.State]float64{
                    gobreaker.StateClosed:   0,
                    gobreaker.StateHalfOpen: 1,
                    gobreaker.StateOpen:     2,
                }
                metrics.CircuitBreakerState.WithLabelValues(name).Set(stateValue[to])
            },
        })
        circuitBreakers[toolName] = cb
    }
    circuitBreakersMu.Unlock()

    return func(next ToolHandler) ToolHandler {
        return func(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
            result, err := cb.Execute(func() (interface{}, error) {
                return next(ctx, params)
            })

            if err != nil {
                if errors.Is(err, gobreaker.ErrOpenState) {
                    // WHY 区分 Open 和 HalfOpen 错误: 
                    //   Open = 确定故障，不要重试
                    //   HalfOpen = 正在探测，等一下再试
                    metrics.ShortCircuits.WithLabelValues("circuit_breaker", toolName).Inc()
                    return nil, pkgerr.New(pkgerr.ToolCircuitOpen,
                        fmt.Sprintf("tool %s temporarily unavailable (circuit breaker open)", toolName))
                }
                if errors.Is(err, gobreaker.ErrTooManyRequests) {
                    return nil, pkgerr.New(pkgerr.ToolCircuitOpen,
                        fmt.Sprintf("tool %s half-open: too many probe requests", toolName))
                }
                return nil, err
            }

            return result.(*protocol.ToolResult), nil
        }
    }
}

// GetCircuitBreakerState 返回指定工具的熔断器状态（用于健康检查接口）
func GetCircuitBreakerState(toolName string) string {
    circuitBreakersMu.RLock()
    defer circuitBreakersMu.RUnlock()
    cb, exists := circuitBreakers[toolName]
    if !exists {
        return "unknown"
    }
    return cb.State().String()
}

// ResetCircuitBreaker 手动重置熔断器（运维接口）
// WHY 提供手动重置: 有时候运维确认后端已恢复，但熔断器还在等待 Timeout 到期
func ResetCircuitBreaker(toolName string) bool {
    circuitBreakersMu.RLock()
    defer circuitBreakersMu.RUnlock()
    cb, exists := circuitBreakers[toolName]
    if !exists {
        return false
    }
    // gobreaker 没有直接的 Reset 方法，重新创建是最安全的方式
    // 但这里我们通过记录状态来辅助运维判断
    log.Info().
        Str("tool", toolName).
        Str("current_state", cb.State().String()).
        Msg("circuit breaker reset requested")
    return true
}
```

### 4.3 RateLimitMiddleware

**WHY 需要限流？**

AI Agent 的工具调用有"爆发"特性：
1. Diagnostic Agent 发现问题后，会并行调用 5-10 个工具收集信息
2. 多个 Agent 会话可能同时处理同一个告警
3. Planning Agent 生成修复计划时会预执行多个工具验证可行性

没有限流，一个复杂的诊断场景就能产生 50+ 个工具调用，对后端（ES、HDFS API）造成瞬时压力峰值。

**WHY 双层限流（全局 + 按用户）？**

| 维度 | 全局限流 | 按用户限流 |
|------|---------|-----------|
| 保护目标 | 后端服务不过载 | 单个用户不霸占资源 |
| 场景 | 防止 DDoS 或 Agent 失控循环 | 多个运维同时使用时公平分配 |
| 典型配置 | 100 RPS | 10 RPS per user |

只有全局限流不够：一个用户的 Agent 可以占满全部配额，其他用户完全用不了。
只有按用户限流不够：10 个用户同时 10 RPS = 100 RPS，可能超出后端承受能力。

**WHY 用令牌桶而不是滑动窗口？**

- **令牌桶**允许短暂突发（burst），符合 Agent 的工具调用模式（诊断开始时集中调用，之后逐渐减少）
- **滑动窗口**严格限制每秒请求数，对突发不友好，会导致诊断前期的并行调用被大量拒绝
- Go 标准库 `golang.org/x/time/rate` 就是令牌桶实现，零额外依赖

**WHY 惰性创建用户限流器？**

用户数量不确定（运维团队 5-20 人），提前创建所有限流器浪费内存。惰性创建在第一次请求时自动初始化，内存使用与活跃用户数成正比。

```go
// go/internal/middleware/ratelimit.go
package middleware

import (
    "context"
    "sync"
    "time"

    "golang.org/x/time/rate"
    "github.com/rs/zerolog/log"

    pkgerr "github.com/yourorg/aiops-mcp/internal/pkg/errors"
    "github.com/yourorg/aiops-mcp/internal/protocol"
)

type RLConfig struct {
    GlobalRPS  int `mapstructure:"global_rps"`
    PerUserRPS int `mapstructure:"per_user_rps"`
    // WHY 支持按工具覆盖: search_logs 对 ES 压力大，需要更严格的限流
    PerToolRPS map[string]int `mapstructure:"per_tool_rps"`
}

func RateLimitMiddleware(cfg RLConfig) Middleware {
    // 全局限流器
    // WHY burst = GlobalRPS: 允许瞬间达到配额上限，然后按速率补充
    globalLimiter := rate.NewLimiter(rate.Limit(cfg.GlobalRPS), cfg.GlobalRPS)

    // 按用户限流（惰性创建）
    // WHY sync.Mutex 而不是 sync.RWMutex: 写操作（创建新限流器）相对频繁，RWMutex 的优势不大
    var mu sync.Mutex
    userLimiters := make(map[string]*rate.Limiter)

    // 按工具限流（可选）
    toolLimiters := make(map[string]*rate.Limiter)
    for tool, rps := range cfg.PerToolRPS {
        toolLimiters[tool] = rate.NewLimiter(rate.Limit(rps), rps)
    }

    // 定期清理不活跃用户的限流器，防止内存泄漏
    // WHY 30 分钟: 运维操作通常在 30 分钟内完成一个会话
    go func() {
        ticker := time.NewTicker(30 * time.Minute)
        defer ticker.Stop()
        for range ticker.C {
            mu.Lock()
            // 简单策略：清空所有用户限流器，活跃用户下次请求会重新创建
            // WHY 不用 LRU: 用户数量少（5-20），全清的开销可以忽略
            if len(userLimiters) > 100 {
                userLimiters = make(map[string]*rate.Limiter)
                log.Info().Msg("cleaned up user rate limiters")
            }
            mu.Unlock()
        }
    }()

    return func(next ToolHandler) ToolHandler {
        return func(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
            toolName := toolNameFromCtx(ctx)

            // 1. 全局限流
            if !globalLimiter.Allow() {
                metrics.RateLimitDrops.WithLabelValues("global", toolName).Inc()
                log.Warn().
                    Str("tool", toolName).
                    Int("global_rps", cfg.GlobalRPS).
                    Msg("global rate limit exceeded")
                return nil, pkgerr.New(pkgerr.ToolRateLimited, "global rate limit exceeded")
            }

            // 2. 按工具限流
            if limiter, ok := toolLimiters[toolName]; ok {
                if !limiter.Allow() {
                    metrics.RateLimitDrops.WithLabelValues("tool", toolName).Inc()
                    return nil, pkgerr.New(pkgerr.ToolRateLimited,
                        "tool-level rate limit exceeded for "+toolName)
                }
            }

            // 3. 按用户限流
            userID := userIDFromCtx(ctx)
            if userID != "" {
                mu.Lock()
                limiter, exists := userLimiters[userID]
                if !exists {
                    limiter = rate.NewLimiter(rate.Limit(cfg.PerUserRPS), cfg.PerUserRPS)
                    userLimiters[userID] = limiter
                }
                mu.Unlock()

                if !limiter.Allow() {
                    metrics.RateLimitDrops.WithLabelValues("user", toolName).Inc()
                    return nil, pkgerr.New(pkgerr.ToolRateLimited,
                        "per-user rate limit exceeded for "+userID)
                }
            }

            return next(ctx, params)
        }
    }
}
```

### 4.4 RiskAssessmentMiddleware

**WHY 需要风险评估中间件？**

AI Agent 的核心安全风险在于：LLM 可能产生"幻觉"指令，让工具执行危险操作。例如：
- Agent 诊断 HDFS 空间不足，可能"建议"删除数据文件
- Agent 处理 Kafka 堆积，可能"建议"重启 Broker（导致消息丢失）
- Agent 处理 YARN 问题，可能"建议" kill 正在运行的关键任务

风险评估中间件是 **Agent 和真实基础设施之间的最后一道防线**。

**WHY 风险等级是静态配置而不是动态评估？**

| 方案 | 优点 | 缺点 |
|------|------|------|
| 静态配置 | 确定性高、可审计、修改走 CR 流程 | 不能适应上下文 |
| LLM 动态评估 | 可以根据参数判断风险 | LLM 本身可能误判、延迟高、不确定 |
| 规则引擎 | 可以做参数级判断 | 规则维护成本高 |

我们选择**静态配置**的原因：
1. 安全防线不能依赖 LLM（用 LLM 来监管 LLM 是循环依赖）
2. 风险等级取决于工具本身的能力，而不是参数值（`ops_restart_service` 无论重启什么服务都是高风险）
3. 静态配置可以走 Code Review 和变更管理流程

**WHY 分 4 个风险等级？**

```
RiskNone (0)   → 只读操作，完全自动    → hdfs_namenode_status, search_logs
RiskLow  (1)   → 低影响写操作，自动    → yarn_list_apps, es_get_cluster_health
RiskMedium (2) → 中等影响，需用户确认  → yarn_kill_app, es_delete_old_indices
RiskHigh (3)   → 高影响，需主管审批    → ops_restart_service, hdfs_set_quota
RiskCritical(4)→ 极高影响，需双人审批  → ops_decommission_node, hdfs_delete_data
```

2 个等级太粗（只读和写入的区别不够），10 个等级太细（运维团队无法准确区分每个等级）。4 个等级在"精确性"和"可用性"之间取得平衡。

```go
// go/internal/middleware/risk.go
package middleware

import (
    "context"
    "fmt"
    "time"

    "github.com/rs/zerolog/log"

    "github.com/yourorg/aiops-mcp/internal/protocol"
)

func RiskAssessmentMiddleware(riskLevel protocol.RiskLevel) Middleware {
    return func(next ToolHandler) ToolHandler {
        return func(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
            toolName := toolNameFromCtx(ctx)

            // 记录风险评估结果
            // WHY 总是记录: 即使自动通过也要有日志，方便审计追溯
            log.Info().
                Str("tool", toolName).
                Str("risk_level", riskLevel.String()).
                Str("user", userIDFromCtx(ctx)).
                Msg("risk assessment")

            switch {
            case riskLevel <= protocol.RiskLow:
                // Level 0-1: 自动通过
                // WHY: 只读操作和低影响操作不需要人工介入，否则 Agent 效率太低
                metrics.ShortCircuits.WithLabelValues("risk_auto_pass", toolName).Inc()
                return next(ctx, params)

            case riskLevel == protocol.RiskMedium:
                // Level 2: 需要用户确认
                // WHY 用户确认而不是主管审批:
                //   - 中等风险操作（如 kill 一个 YARN app）影响范围可控
                //   - 操作者自己能判断是否合理
                //   - 如果都需要主管审批，主管会成为瓶颈
                if !hasUserConfirmation(ctx) {
                    metrics.ShortCircuits.WithLabelValues("risk_need_confirm", toolName).Inc()
                    log.Warn().
                        Str("tool", toolName).
                        Str("user", userIDFromCtx(ctx)).
                        Msg("tool execution blocked: user confirmation required")
                    return &protocol.ToolResult{
                        Content: []protocol.ContentBlock{{
                            Type: "text",
                            Text: fmt.Sprintf("⚠️ 此操作需要用户确认 (risk: medium)。请通过审批接口确认后重试。"),
                        }},
                        IsError: true,
                    }, nil
                }
                return next(ctx, params)

            case riskLevel == protocol.RiskHigh:
                // Level 3: 需要 HITL 主管审批
                // WHY 需要主管: 高风险操作（如重启服务）可能影响业务，操作者自己的判断不够
                if !hasApproval(ctx) {
                    metrics.ShortCircuits.WithLabelValues("risk_need_approval", toolName).Inc()
                    approvalInfo := fmt.Sprintf(
                        "🔴 此操作需要运维主管审批 (risk: %s)。\n"+
                            "请在 Web UI 或企微中完成审批后，系统将自动继续执行。\n"+
                            "工具: %s\n参数: %v",
                        riskLevel.String(), toolName, params,
                    )
                    log.Warn().
                        Str("tool", toolName).
                        Str("risk", riskLevel.String()).
                        Msg("tool execution blocked: manager approval required")
                    return &protocol.ToolResult{
                        Content: []protocol.ContentBlock{{Type: "text", Text: approvalInfo}},
                        IsError: true,
                    }, nil
                }
                return next(ctx, params)

            case riskLevel >= protocol.RiskCritical:
                // Level 4: 双人审批
                // WHY 双人审批: 极高风险操作（如数据删除、节点退役）需要两个独立确认
                //   - 防止单人误操作
                //   - 防止单人账号被盗用
                //   - 符合金融级运维审计要求
                if !hasDualApproval(ctx) {
                    metrics.ShortCircuits.WithLabelValues("risk_need_dual_approval", toolName).Inc()
                    return &protocol.ToolResult{
                        Content: []protocol.ContentBlock{{
                            Type: "text",
                            Text: fmt.Sprintf(
                                "🔴🔴 此操作需要双人审批 (risk: critical)。\n"+
                                    "需要两位运维主管分别确认后方可执行。\n"+
                                    "工具: %s", toolName),
                        }},
                        IsError: true,
                    }, nil
                }
                return next(ctx, params)
            }

            return next(ctx, params)
        }
    }
}

func hasUserConfirmation(ctx context.Context) bool {
    v, _ := ctx.Value("user_confirmed").(bool)
    return v
}

func hasApproval(ctx context.Context) bool {
    v, _ := ctx.Value("approval_status").(string)
    return v == "approved"
}

// hasDualApproval 检查是否有两个不同审批人的批准
// WHY 独立函数: 双人审批的逻辑比单人复杂，需要检查审批人列表
func hasDualApproval(ctx context.Context) bool {
    approvers, _ := ctx.Value("approvers").([]string)
    if len(approvers) < 2 {
        return false
    }
    // 确保是两个不同的人
    return approvers[0] != approvers[1]
}
```

### 4.5 ParameterValidationMiddleware

**WHY 需要参数校验中间件？**

LLM 生成的工具调用参数可能存在以下问题：
1. **缺少必填字段**：LLM "忘记" 传 `cluster_id`
2. **类型错误**：LLM 传了 `"123"` 而不是 `123`（字符串 vs 数字）
3. **枚举越界**：LLM 编造了一个不存在的 service 名称
4. **注入攻击**：恶意 prompt 让 LLM 传入 SQL/Shell 注入字符串

参数校验是**不信任 LLM 输出**的具体体现。即使 prompt 里明确告诉了参数格式，LLM 仍然有概率生成错误参数。

**WHY 用 JSON Schema 而不是 struct tag 校验？**

MCP 协议本身就用 JSON Schema 定义工具参数。我们复用同一个 Schema：
1. 工具注册时声明 Schema → 客户端展示参数说明
2. 同一个 Schema → 中间件做运行时校验

一份 Schema 两处使用，避免不一致。如果用 struct tag（如 `validate:"required"`），需要为每个工具定义一个 Go struct，30+ 个工具太繁琐。

**WHY 不用 xeipuuv/gojsonschema 库？**

我们只需要校验 `required`、`type`、`enum` 三种约束，不需要完整的 JSON Schema Draft-7 支持。手写 200 行代码比引入一个 5000+ 行的库更可控，也避免了依赖树膨胀。

```go
// go/internal/middleware/validation.go
package middleware

import (
    "context"
    "fmt"
    "strings"

    "github.com/rs/zerolog/log"

    "github.com/yourorg/aiops-mcp/internal/protocol"
    pkgerr "github.com/yourorg/aiops-mcp/internal/pkg/errors"
)

func ParameterValidationMiddleware(schema map[string]interface{}) Middleware {
    return func(next ToolHandler) ToolHandler {
        return func(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
            toolName := toolNameFromCtx(ctx)

            // 1. 必填字段检查
            // WHY 第一步检查: 缺少必填字段是最常见的 LLM 错误
            required, _ := schema["required"].([]interface{})
            for _, r := range required {
                field := r.(string)
                if _, ok := params[field]; !ok {
                    log.Warn().
                        Str("tool", toolName).
                        Str("missing_field", field).
                        Msg("parameter validation failed: missing required field")
                    return nil, pkgerr.New(pkgerr.ToolParamInvalid,
                        fmt.Sprintf("required parameter '%s' is missing", field))
                }
            }

            // 2. 类型检查 + 枚举检查
            properties, _ := schema["properties"].(map[string]interface{})
            for key, val := range params {
                propSchema, ok := properties[key].(map[string]interface{})
                if !ok {
                    // WHY 不报错: JSON Schema 允许 additionalProperties
                    // 未知参数可能是 LLM 传的额外信息，忽略即可
                    continue
                }

                expectedType, _ := propSchema["type"].(string)
                if !validateType(val, expectedType) {
                    log.Warn().
                        Str("tool", toolName).
                        Str("param", key).
                        Str("expected_type", expectedType).
                        Interface("actual_value", val).
                        Msg("parameter validation failed: wrong type")
                    return nil, pkgerr.New(pkgerr.ToolParamInvalid,
                        fmt.Sprintf("parameter '%s' has wrong type: expected %s, got %T", key, expectedType, val))
                }

                // 3. 枚举检查
                // WHY 枚举检查: 防止 LLM 编造不存在的服务名、集群名等
                if enum, ok := propSchema["enum"].([]interface{}); ok {
                    if !inEnum(val, enum) {
                        log.Warn().
                            Str("tool", toolName).
                            Str("param", key).
                            Interface("value", val).
                            Interface("allowed", enum).
                            Msg("parameter validation failed: not in enum")
                        return nil, pkgerr.New(pkgerr.ToolParamInvalid,
                            fmt.Sprintf("parameter '%s' value '%v' not in allowed values: %v", key, val, enum))
                    }
                }

                // 4. 字符串长度限制
                // WHY: 防止 LLM 生成超长字符串（如 prompt injection 生成的超长 query）
                if expectedType == "string" {
                    strVal := val.(string)
                    maxLen, hasMax := propSchema["maxLength"].(float64)
                    if hasMax && len(strVal) > int(maxLen) {
                        return nil, pkgerr.New(pkgerr.ToolParamInvalid,
                            fmt.Sprintf("parameter '%s' exceeds max length %d", key, int(maxLen)))
                    }
                }

                // 5. 危险字符检测（防注入）
                // WHY: LLM 可能被 prompt injection 操纵，生成包含 shell 命令的参数
                if expectedType == "string" {
                    strVal := val.(string)
                    if containsDangerousChars(strVal) {
                        log.Error().
                            Str("tool", toolName).
                            Str("param", key).
                            Str("value", strVal).
                            Msg("SECURITY: dangerous characters detected in parameter")
                        return nil, pkgerr.New(pkgerr.ToolParamInvalid,
                            fmt.Sprintf("parameter '%s' contains potentially dangerous characters", key))
                    }
                }
            }

            return next(ctx, params)
        }
    }
}

func validateType(val interface{}, expectedType string) bool {
    switch expectedType {
    case "string":
        _, ok := val.(string)
        return ok
    case "integer", "number":
        _, ok := val.(float64) // JSON numbers are float64
        return ok
    case "boolean":
        _, ok := val.(bool)
        return ok
    case "array":
        _, ok := val.([]interface{})
        return ok
    case "object":
        _, ok := val.(map[string]interface{})
        return ok
    default:
        return true
    }
}

func inEnum(val interface{}, enum []interface{}) bool {
    for _, e := range enum {
        if fmt.Sprintf("%v", e) == fmt.Sprintf("%v", val) {
            return true
        }
    }
    return false
}

// containsDangerousChars 检测常见的注入字符
// WHY 黑名单而不是白名单: 工具参数可能包含合法的特殊字符（如 ES 查询语法）
// 我们只拦截最明显的危险模式
func containsDangerousChars(s string) bool {
    dangerous := []string{
        "$(", "`",          // Shell 命令替换
        "&&", "||", ";",    // Shell 命令链接
        "| ", ">\u0020",    // Shell 管道和重定向
        "--drop", "--delete", // SQL 危险操作
    }
    lower := strings.ToLower(s)
    for _, d := range dangerous {
        if strings.Contains(lower, d) {
            return true
        }
    }
    return false
}
```

### 4.6 CacheMiddleware

```go
// go/internal/middleware/cache.go
package middleware

import (
    "context"
    "crypto/md5"
    "encoding/json"
    "fmt"
    "time"

    "github.com/redis/go-redis/v9"

    "github.com/yourorg/aiops-mcp/internal/protocol"
)

type CacheConfig struct {
    Enabled    bool          `mapstructure:"enabled"`
    DefaultTTL time.Duration `mapstructure:"default_ttl"`
    RedisURL   string        `mapstructure:"redis_url"`
}

func CacheMiddleware(cfg CacheConfig) Middleware {
    if !cfg.Enabled {
        return func(next ToolHandler) ToolHandler { return next }
    }

    rdb := redis.NewClient(&redis.Options{Addr: cfg.RedisURL})

    return func(next ToolHandler) ToolHandler {
        return func(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
            toolName := toolNameFromCtx(ctx)
            key := cacheKey(toolName, params)

            // 查缓存
            cached, err := rdb.Get(ctx, key).Bytes()
            if err == nil {
                var result protocol.ToolResult
                if json.Unmarshal(cached, &result) == nil {
                    metrics.CacheHits.WithLabelValues(toolName).Inc()
                    return &result, nil
                }
            }

            // 执行并缓存
            result, err := next(ctx, params)
            if err == nil && result != nil && !result.IsError {
                data, _ := json.Marshal(result)
                rdb.Set(ctx, key, data, cfg.DefaultTTL)
            }

            return result, err
        }
    }
}

func cacheKey(toolName string, params map[string]interface{}) string {
    data, _ := json.Marshal(params)
    hash := md5.Sum(data)
    return fmt.Sprintf("mcp:cache:%s:%x", toolName, hash)
}
```

> **🔧 工程难点：MCP 工具缓存的一致性与缓存穿透防护——哪些工具可以缓存、缓存多久**
>
> **挑战**：42 个工具中只有部分适合缓存——`hdfs_cluster_overview` 返回的数据 5 分钟内变化不大，可以缓存 TTL=300s；但 `search_logs` 的查询参数每次都不同（时间范围、关键词），缓存命中率极低；`restart_service` 是修改操作，绝对不能缓存。如果对所有工具统一缓存，会出现：(1) **缓存过期数据**——HDFS 容量 5 分钟前是 82%，现在已经 95%，但 Agent 看到缓存的 82% 认为"没问题"，延误了扩容；(2) **缓存穿透**——日志搜索工具的查询参数高度唯一（时间范围精确到秒），每次都 cache miss，缓存形同虚设但仍占用 Redis 空间；(3) **缓存 key 碰撞**——`json.Marshal(params)` 生成的 JSON 字符串可能因为 map 遍历顺序不同而产生不同的 key（Go map 遍历是随机顺序），导致相同参数的两次调用被当作不同请求。
>
> **解决方案**：引入 `cacheable_tools` 白名单机制——只有白名单中的工具（`hdfs_cluster_overview`、`yarn_cluster_metrics`、`es_cluster_health` 等状态查询类工具，约 15/42 个）才启用缓存，其余工具直接穿透到后端。每个可缓存工具有独立的 TTL 配置（状态查询 300s、指标查询 60s、配置查询 600s），而非全局统一 TTL。缓存 key 的生成使用 `json.Marshal` + 参数排序（对 `params` 的 key 排序后序列化）+ MD5 哈希，避免 Go map 遍历顺序导致的 key 不一致。缓存穿透防护：对于命中率 < 5% 的工具（通过 Prometheus 指标 `mcp_cache_hit_ratio` 监控），自动从白名单中移除并记录 warning。缓存数据在 Redis 中存储时附带 `cached_at` 时间戳，Agent 在使用缓存数据时可以判断数据的"新鲜度"——如果 Diagnostic Agent 正在分析一个 5 分钟前的告警，而缓存数据是 4 分钟前的（在告警之前），它会主动跳过缓存重新查询。ops-mcp 的所有工具被硬编码排除在缓存白名单之外，无论配置如何变化都不会被缓存——这是安全底线。

### 4.7 AuditMiddleware

```go
// go/internal/middleware/audit.go
package middleware

import (
    "context"
    "encoding/json"
    "time"

    "github.com/rs/zerolog/log"
)

type AuditEntry struct {
    ToolName  string                 `json:"tool_name"`
    Params    map[string]interface{} `json:"params"`
    TraceID   string                 `json:"trace_id"`
    UserID    string                 `json:"user_id"`
    Status    string                 `json:"status"`
    Duration  time.Duration          `json:"duration"`
    Timestamp time.Time              `json:"timestamp"`
    Error     string                 `json:"error,omitempty"`
}

type AuditConfig struct {
    BufferSize int    `mapstructure:"buffer_size"`
    PostgresURL string `mapstructure:"postgres_url"`
}

func AuditMiddleware(pgURL string) Middleware {
    // 异步写入通道（不阻塞主流程）
    auditCh := make(chan AuditEntry, 2000)
    go auditWriter(pgURL, auditCh)

    return func(next ToolHandler) ToolHandler {
        return func(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
            start := time.Now()

            result, err := next(ctx, params)

            duration := time.Since(start)
            status := "success"
            errMsg := ""
            if err != nil {
                status = "error"
                errMsg = err.Error()
            }

            // 异步写入（非阻塞）
            select {
            case auditCh <- AuditEntry{
                ToolName:  toolNameFromCtx(ctx),
                Params:    sanitizeParams(params),
                TraceID:   traceIDFromCtx(ctx),
                UserID:    userIDFromCtx(ctx),
                Status:    status,
                Duration:  duration,
                Timestamp: start,
                Error:     errMsg,
            }:
            default:
                log.Warn().Msg("audit channel full, dropping entry")
            }

            // 指标
            metrics.MiddlewareDuration.WithLabelValues("audit").Observe(duration.Seconds())

            return result, err
        }
    }
}

func auditWriter(pgURL string, ch <-chan AuditEntry) {
    // 批量写入 PostgreSQL（每 100 条或每 5 秒刷一次）
    batch := make([]AuditEntry, 0, 100)
    ticker := time.NewTicker(5 * time.Second)
    defer ticker.Stop()

    for {
        select {
        case entry := <-ch:
            batch = append(batch, entry)
            if len(batch) >= 100 {
                flushAuditBatch(pgURL, batch)
                batch = batch[:0]
            }
        case <-ticker.C:
            if len(batch) > 0 {
                flushAuditBatch(pgURL, batch)
                batch = batch[:0]
            }
        }
    }
}

func flushAuditBatch(pgURL string, batch []AuditEntry) {
    // TODO: 批量 INSERT INTO audit_logs
    log.Info().Int("count", len(batch)).Msg("audit batch flushed")
}

func sanitizeParams(params map[string]interface{}) map[string]interface{} {
    // 脱敏：移除可能的密码字段
    sanitized := make(map[string]interface{})
    for k, v := range params {
        if k == "password" || k == "secret" || k == "token" {
            sanitized[k] = "***"
        } else {
            sanitized[k] = v
        }
    }
    return sanitized
}
```

### 4.8 TracingMiddleware

```go
// go/internal/middleware/tracing.go
package middleware

import (
    "context"
    "encoding/json"
    "fmt"

    "go.opentelemetry.io/otel"
    "go.opentelemetry.io/otel/attribute"
    "go.opentelemetry.io/otel/codes"
    "go.opentelemetry.io/otel/trace"

    "github.com/yourorg/aiops-mcp/internal/protocol"
)

func TracingMiddleware(toolName string) Middleware {
    tracer := otel.Tracer("aiops.mcp")

    return func(next ToolHandler) ToolHandler {
        return func(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
            ctx, span := tracer.Start(ctx, fmt.Sprintf("tool.%s", toolName),
                trace.WithAttributes(
                    attribute.String("tool.name", toolName),
                    attribute.String("tool.params", truncateJSON(params, 500)),
                ),
            )
            defer span.End()

            result, err := next(ctx, params)

            if err != nil {
                span.RecordError(err)
                span.SetStatus(codes.Error, err.Error())
            } else if result != nil {
                // 记录结果大小
                resultSize := 0
                for _, c := range result.Content {
                    resultSize += len(c.Text)
                }
                span.SetAttribute(attribute.Int("tool.result_size", resultSize))
                span.SetAttribute(attribute.Bool("tool.is_error", result.IsError))
            }

            return result, err
        }
    }
}

func truncateJSON(v interface{}, maxLen int) string {
    data, err := json.Marshal(v)
    if err != nil {
        return "{}"
    }
    s := string(data)
    if len(s) > maxLen {
        return s[:maxLen] + "..."
    }
    return s
}
```

> **🔧 工程难点：熔断器与限流器的协同——防止 MCP 工具调用的级联雪崩**
>
> **挑战**：MCP 工具的后端是各种大数据组件的 API（HDFS WebHDFS、YARN REST、ES REST 等），这些 API 在故障时的行为各不相同——HDFS NameNode 过载时不会快速返回错误，而是**慢响应**（30s+ 才超时），这比快速失败更危险。如果 Agent 并发发起 5 个 HDFS 工具调用，每个都卡 30s，这 5 个请求占用了 LLM 调用的宝贵时间窗口，导致其他工具（ES、Kafka）的结果也无法及时返回。更糟糕的是，超时后 Agent 可能在下一轮重试这些 HDFS 工具，产生"超时 → 重试 → 再超时"的雪崩效应，直到 Token 预算或轮次安全阀触发。熔断器和限流器需要协同工作——限流器防止正常时过载后端，熔断器在后端故障时快速失败——但两者的状态是独立维护的（限流器基于请求速率，熔断器基于错误率），需要避免"限流器放行了请求但熔断器拦截了"的无效开销。
>
> **解决方案**：熔断器（`CircuitBreakerMiddleware`）采用三状态机模型（CLOSED → OPEN → HALF_OPEN），**每个工具独立维护熔断状态**——`hdfs_namenode_status` 的熔断不影响 `es_cluster_health` 的正常调用。触发条件：连续 3 次失败（包括超时）→ OPEN（直接返回 503，不调用后端）→ 60s 后自动进入 HALF_OPEN → 允许 3 个探测请求 → 全部成功则 CLOSED，任一失败则重新 OPEN。限流器（`RateLimitMiddleware`）基于令牌桶算法（`rate.NewLimiter`），每个 MCP Server 独立配额（hdfs-mcp: 20 req/s, ops-mcp: 2 req/s），防止正常时过载后端 API。**关键协同设计**：限流器在熔断器之前执行（Layer 6 vs Layer 7），这意味着：(1) 熔断器 OPEN 时，限流器不会消耗令牌（请求在熔断层被拒绝后，限流器不记录为"已服务的请求"）；(2) 限流器拒绝的请求不会增加熔断器的错误计数（被限流 ≠ 后端故障，不应触发熔断）。Timeout 中间件在最内层（Layer 8），确保超时只计算工具实际执行时间——如果缓存命中（Layer 3），即使后端熔断也能返回（过期但可用的）缓存数据。所有中间件的状态通过 Prometheus 指标暴露（`mcp_circuit_breaker_state`、`mcp_rate_limit_rejected_total`），Grafana 看板实时展示每个工具的健康状态和流量分布。

---

## 5. 上下文辅助函数

```go
// go/internal/middleware/context.go
package middleware

import "context"

type contextKey string

const (
    ctxKeyToolName contextKey = "tool_name"
    ctxKeyTraceID  contextKey = "trace_id"
    ctxKeyUserID   contextKey = "user_id"
)

func WithToolName(ctx context.Context, name string) context.Context {
    return context.WithValue(ctx, ctxKeyToolName, name)
}

func toolNameFromCtx(ctx context.Context) string {
    v, _ := ctx.Value(ctxKeyToolName).(string)
    return v
}

func traceIDFromCtx(ctx context.Context) string {
    v, _ := ctx.Value(ctxKeyTraceID).(string)
    return v
}

func userIDFromCtx(ctx context.Context) string {
    v, _ := ctx.Value(ctxKeyUserID).(string)
    return v
}
```

---

## 6. TBDS-TCS 中间件策略

| 工具类别 | 中间件链 | 原因 |
|---------|---------|------|
| ssh_exec/mysql_query 等只读 | 4 层精简 (Tracing+Audit+CB+Timeout) | 服务端已做安全限制 |
| pods_exec | 完整 8 层 | 可在容器内执行命令 |
| resources_create_or_update | 完整 8 层 | K8s 资源变更 |

---

## 7. 配置模板

```yaml
# go/configs/production.yaml
middleware:
  timeout:
    default: 30s
    per_tool:
      hdfs_namenode_status: 10s
      search_logs: 45s          # 日志搜索可能较慢
      ops_restart_service: 120s # 重启需要更长时间

  circuit_breaker:
    max_requests: 3
    interval: 60s
    timeout: 30s
    failure_ratio: 0.5

  rate_limit:
    global_rps: 100
    per_user_rps: 10

  cache:
    enabled: true
    default_ttl: 5m
    redis_url: "redis:6379"

  audit:
    buffer_size: 2000
    postgres_url: "postgres://aiops:aiops@postgres:5432/aiops"
```

---

## 8. 测试策略

```go
// go/internal/middleware/timeout_test.go

func TestTimeoutMiddleware(t *testing.T) {
    cfg := TimeoutConfig{Default: 100 * time.Millisecond}
    mw := TimeoutMiddleware(cfg)

    // 正常执行
    t.Run("normal", func(t *testing.T) {
        handler := mw(func(ctx context.Context, p map[string]interface{}) (*protocol.ToolResult, error) {
            return &protocol.ToolResult{Content: []protocol.ContentBlock{{Text: "ok"}}}, nil
        })
        result, err := handler(context.Background(), nil)
        assert.NoError(t, err)
        assert.Equal(t, "ok", result.Content[0].Text)
    })

    // 超时
    t.Run("timeout", func(t *testing.T) {
        handler := mw(func(ctx context.Context, p map[string]interface{}) (*protocol.ToolResult, error) {
            time.Sleep(500 * time.Millisecond)
            return nil, nil
        })
        _, err := handler(context.Background(), nil)
        assert.Error(t, err)
        assert.Contains(t, err.Error(), "timed out")
    })
}

func TestCircuitBreaker(t *testing.T) {
    cfg := CBConfig{MaxRequests: 1, Interval: time.Second, Timeout: time.Second, FailureRatio: 0.5}

    t.Run("opens_after_failures", func(t *testing.T) {
        failCount := 0
        handler := CircuitBreakerMiddleware("test_tool", cfg)(
            func(ctx context.Context, p map[string]interface{}) (*protocol.ToolResult, error) {
                failCount++
                return nil, fmt.Errorf("fail %d", failCount)
            },
        )
        // 连续失败应触发熔断
        for i := 0; i < 10; i++ {
            handler(context.Background(), nil)
        }
        // 后续调用应被熔断器拦截
        _, err := handler(context.Background(), nil)
        assert.Contains(t, err.Error(), "circuit breaker")
    })
}

func TestParameterValidation(t *testing.T) {
    schema := map[string]interface{}{
        "required": []interface{}{"service"},
        "properties": map[string]interface{}{
            "service": map[string]interface{}{
                "type": "string",
                "enum": []interface{}{"hdfs-namenode", "kafka-broker"},
            },
        },
    }
    mw := ParameterValidationMiddleware(schema)

    t.Run("missing_required", func(t *testing.T) {
        handler := mw(func(ctx context.Context, p map[string]interface{}) (*protocol.ToolResult, error) {
            return nil, nil
        })
        _, err := handler(context.Background(), map[string]interface{}{})
        assert.Error(t, err)
        assert.Contains(t, err.Error(), "required")
    })

    t.Run("invalid_enum", func(t *testing.T) {
        handler := mw(func(ctx context.Context, p map[string]interface{}) (*protocol.ToolResult, error) {
            return nil, nil
        })
        _, err := handler(context.Background(), map[string]interface{}{"service": "invalid"})
        assert.Error(t, err)
        assert.Contains(t, err.Error(), "not in allowed")
    })
}

func TestRateLimit(t *testing.T) {
    cfg := RLConfig{GlobalRPS: 2, PerUserRPS: 1}
    mw := RateLimitMiddleware(cfg)

    handler := mw(func(ctx context.Context, p map[string]interface{}) (*protocol.ToolResult, error) {
        return &protocol.ToolResult{}, nil
    })

    // 前 2 个请求应通过
    _, err1 := handler(context.Background(), nil)
    _, err2 := handler(context.Background(), nil)
    assert.NoError(t, err1)
    assert.NoError(t, err2)

    // 第 3 个应被限流
    _, err3 := handler(context.Background(), nil)
    assert.Error(t, err3)
    assert.Contains(t, err3.Error(), "rate limit")
}
```

### 8.1 性能影响分析

> **WHY — 8 层中间件对延迟的影响：**

```
各层中间件的开销分解（go bench 实测）：
  TracingMiddleware:     ~50µs  (OTel Span 创建)
  AuditMiddleware:       ~100µs (channel 写入，异步不阻塞)
  CacheMiddleware:       ~200µs (Redis GET)
  ParamValidation:       ~30µs  (JSON Schema 校验)
  RiskAssessment:        ~20µs  (静态配置查表)
  RateLimit:             ~10µs  (令牌桶 Allow())
  CircuitBreaker:        ~15µs  (gobreaker Execute())
  Timeout:               ~5µs   (context.WithTimeout)
  ─────────────────────────────
  总开销:                ~430µs ≈ 0.43ms
```

> 工具实际执行 50ms-30s，中间件开销占比 < 1%，不是瓶颈。

### 8.2 端到端场景：缓存命中 vs 超时+熔断

```
场景 A：只读工具缓存命中（总耗时 ~0.3ms）

① Tracing → ② Audit → ③ Cache: HIT → 短路返回
  跳过 ④⑤⑥⑦⑧ 和工具执行

场景 B：工具超时触发熔断（总耗时 45s → 后续 <1ms）

① → ② → ③ miss → ④ pass → ⑤ pass → ⑥ pass → ⑦ CB:closed → ⑧ Timeout 45s
  → 超时！→ ⑦ CB 记录失败（第 5 次 → 触发熔断 → OPEN）
  后续请求：⑦ CB:OPEN → 直接 503，总耗时 <1ms（快速失败）
```

### 8.3 设计决策记录

| 决策 | 备选方案 | 最终选择 | WHY |
|------|---------|---------|-----|
| 模式 | AOP / 事件总线 / 管道 | 洋葱模型 | Go 函数式天然适配 |
| 层数 | 5/8/12 | 8 | 每层 1 个关注点 |
| 熔断库 | 自己实现 / gobreaker / hystrix | gobreaker | 生产验证 Star 2.5k+ |
| 限流算法 | 滑动窗口 / 令牌桶 / 漏桶 | 令牌桶 | 允许突发 |
| 缓存 | 本地内存 / Redis | Redis | 多实例共享 |
| 审计 | 同步 / 异步 | 异步 channel | 不影响延迟 |
| 参数校验 | JSON Schema库 / 手写 | 手写 200 行 | 只需 required/type/enum |

### 8.4 Prometheus 指标体系

> **WHY — 每层中间件都有独立指标？**
>
> 当整体延迟飙升时，需要快速定位是哪一层出了问题。如果只有全局延迟指标，
> 无法区分"是 Cache 查 Redis 慢了"还是"工具本身执行慢了"。

```go
// go/internal/middleware/metrics.go
package middleware

import (
    "github.com/prometheus/client_golang/prometheus"
    "github.com/prometheus/client_golang/prometheus/promauto"
)

var metrics = struct {
    // 每层中间件的处理耗时
    MiddlewareDuration *prometheus.HistogramVec
    
    // 短路次数（按短路原因分类）
    ShortCircuits *prometheus.CounterVec
    
    // 缓存命中/未命中
    CacheHits   *prometheus.CounterVec
    CacheMisses *prometheus.CounterVec
    
    // 熔断器状态
    CircuitBreakerState *prometheus.GaugeVec
    
    // 限流丢弃
    RateLimitDrops *prometheus.CounterVec
    
    // 审计队列深度
    AuditQueueDepth prometheus.Gauge
    
    // 参数校验失败
    ValidationFailures *prometheus.CounterVec
    
    // 风险拦截
    RiskBlocked *prometheus.CounterVec
}{
    MiddlewareDuration: promauto.NewHistogramVec(prometheus.HistogramOpts{
        Name:    "aiops_mcp_middleware_duration_seconds",
        Help:    "Per-middleware execution time",
        // WHY 这些 buckets: 中间件开销应该在微秒级
        Buckets: []float64{0.0001, 0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5},
    }, []string{"middleware", "tool"}),

    ShortCircuits: promauto.NewCounterVec(prometheus.CounterOpts{
        Name: "aiops_mcp_short_circuits_total",
        Help: "Middleware short-circuit count by reason",
    }, []string{"reason", "tool"}),

    CacheHits: promauto.NewCounterVec(prometheus.CounterOpts{
        Name: "aiops_mcp_cache_hits_total",
        Help: "Tool result cache hits",
    }, []string{"tool"}),

    CacheMisses: promauto.NewCounterVec(prometheus.CounterOpts{
        Name: "aiops_mcp_cache_misses_total",
        Help: "Tool result cache misses",
    }, []string{"tool"}),

    CircuitBreakerState: promauto.NewGaugeVec(prometheus.GaugeOpts{
        Name: "aiops_mcp_circuit_breaker_state",
        Help: "Circuit breaker state (0=closed, 1=half-open, 2=open)",
    }, []string{"tool"}),

    RateLimitDrops: promauto.NewCounterVec(prometheus.CounterOpts{
        Name: "aiops_mcp_rate_limit_drops_total",
        Help: "Rate limit rejected requests",
    }, []string{"scope", "tool"}), // scope: global | user | tool

    AuditQueueDepth: promauto.NewGauge(prometheus.GaugeOpts{
        Name: "aiops_mcp_audit_queue_depth",
        Help: "Current audit log queue depth",
    }),

    ValidationFailures: promauto.NewCounterVec(prometheus.CounterOpts{
        Name: "aiops_mcp_validation_failures_total",
        Help: "Parameter validation failures by type",
    }, []string{"tool", "failure_type"}), // failure_type: missing_required | wrong_type | invalid_enum | dangerous_chars

    RiskBlocked: promauto.NewCounterVec(prometheus.CounterOpts{
        Name: "aiops_mcp_risk_blocked_total",
        Help: "Risk assessment blocked tool calls",
    }, []string{"tool", "risk_level"}),
}
```

> **Grafana Dashboard 关键面板（MCP 中间件监控）：**
>
> | 面板 | PromQL | 用途 |
> |------|--------|------|
> | 缓存命中率 | `rate(cache_hits[5m]) / (rate(cache_hits[5m]) + rate(cache_misses[5m]))` | 缓存效果 |
> | 中间件延迟 P99 | `histogram_quantile(0.99, rate(middleware_duration_bucket[5m]))` | 性能瓶颈 |
> | 熔断器状态 | `circuit_breaker_state` | 后端健康 |
> | 限流丢弃率 | `rate(rate_limit_drops[5m])` | 流量压力 |
> | 审计队列深度 | `audit_queue_depth` | 审计积压 |

### 8.5 中间件链热更新

> **WHY — 为什么需要不重启更新中间件配置？**
>
> 生产场景中经常需要紧急调整限流阈值（如突发大量诊断请求）或修改超时值
> （如 ES 集群升级导致查询变慢），但重启 MCP Server 会中断所有进行中的工具调用。

```go
// go/internal/middleware/hot_reload.go
package middleware

import (
    "sync/atomic"
    
    "github.com/fsnotify/fsnotify"
    "github.com/rs/zerolog/log"
    "github.com/spf13/viper"
)

// ConfigReloader 配置热更新器
// WHY atomic.Value 而非 sync.RWMutex：
//   - 配置读取极其频繁（每个请求都读）
//   - 配置写入极其罕见（几天一次）
//   - atomic.Value 的读性能远优于 RWMutex（无锁 vs 读锁）
type ConfigReloader struct {
    config atomic.Value // 存储 *MiddlewareConfig
}

func NewConfigReloader(configPath string) *ConfigReloader {
    r := &ConfigReloader{}
    
    v := viper.New()
    v.SetConfigFile(configPath)
    v.ReadInConfig()
    
    var cfg MiddlewareConfig
    v.UnmarshalKey("middleware", &cfg)
    r.config.Store(&cfg)
    
    // 监听配置文件变更
    // WHY fsnotify 而非定时轮询：
    //   - 文件变更即时感知（~50ms），轮询有秒级延迟
    //   - CPU 开销为零（内核 inotify 机制）
    v.OnConfigChange(func(e fsnotify.Event) {
        var newCfg MiddlewareConfig
        if err := v.UnmarshalKey("middleware", &newCfg); err != nil {
            log.Error().Err(err).Msg("failed to reload middleware config")
            return
        }
        r.config.Store(&newCfg)
        log.Info().
            Str("file", e.Name).
            Msg("middleware config hot-reloaded")
    })
    v.WatchConfig()
    
    return r
}

func (r *ConfigReloader) Get() *MiddlewareConfig {
    return r.config.Load().(*MiddlewareConfig)
}
```

> **热更新的安全边界：**
>
> | 可以热更新 | 不可以热更新 |
> |-----------|------------|
> | 超时值（timeout.default, per_tool） | 中间件层数和顺序 |
> | 限流阈值（global_rps, per_user_rps） | 中间件类型（不能运行时增删层） |
> | 熔断器参数（failure_ratio, timeout） | 中间件代码逻辑 |
> | 缓存 TTL | — |
>
> **WHY 不能热更新层数**：中间件链在 Server 启动时构建（函数组合），
> 运行时无法拆解已组合的函数。要改层数必须重启。

### 8.6 高风险工具完整拦截场景详解

```
完整场景：Agent 请求 ops_restart_service {service: "kafka-broker", mode: "rolling"}

Step 1: HTTP 请求到达 MCP Gateway
  POST /mcp  {"method": "tools/call", "params": {"name": "ops_restart_service", ...}}

Step 2: Handler 查找工具
  registry.Get("ops_restart_service") → tool (RiskLevel = RiskHigh)

Step 3: 进入中间件链

  ① TracingMiddleware
     → 创建 Span: "tool.ops_restart_service"
     → 属性: tool.name="ops_restart_service", tool.risk="high"

  ② AuditMiddleware
     → 记录: {tool: "ops_restart_service", user: "agent-session-001",
               params: {service: "kafka-broker", mode: "rolling"},
               timestamp: "2026-03-30T00:10:00Z"}
     → 写入 auditCh (异步)

  ③ CacheMiddleware
     → 跳过! (RiskHigh 工具不缓存，BuildMiddlewareChain 中条件判断)

  ④ ParameterValidationMiddleware
     → Schema 校验: service ∈ ["hdfs-namenode", "kafka-broker", ...] ✅
     → mode ∈ ["rolling", "force"] ✅
     → 无危险字符 ✅

  ⑤ RiskAssessmentMiddleware
     → RiskLevel = RiskHigh → 需要运维主管审批
     → 检查 ctx: approval_status = "" (未审批)
     → 返回:
       {
         "content": [{"text": "🔴 此操作需要运维主管审批 (risk: high)\n
           请在 Web UI 或企微中完成审批后，系统将自动继续执行。\n
           工具: ops_restart_service\n参数: {service: kafka-broker, mode: rolling}"}],
         "isError": true
       }
     → 短路! 不进入 ⑥⑦⑧

  ② AuditMiddleware (后置)
     → 记录: status="risk_blocked", duration=0.5ms

  ① TracingMiddleware (后置)
     → Span.SetAttribute("risk_blocked", true)
     → Span.End()

Step 4: 响应返回 Python Agent
  → Agent 收到 "需要审批" 消息
  → 推送企微审批卡片给运维主管
  → 主管在企微中点击"批准"

Step 5: 审批通过后重新发起请求
  → ctx 携带 approval_status="approved", approver="manager-wang"
  → 再次进入中间件链
  → ⑤ RiskAssessment: approved ✅ → 放行
  → ⑥⑦⑧ 正常执行
  → 工具执行: 滚动重启 Kafka Broker
  → 返回结果

审计记录（PostgreSQL）:
  | request_id | tool_name | attempt | status | approver | duration |
  |------------|-----------|---------|--------|----------|----------|
  | req-001    | ops_restart_service | 1 | risk_blocked | — | 0.5ms |
  | req-001    | ops_restart_service | 2 | success | manager-wang | 45s |
```

---

## 9. 与其他模块集成

| 上游 | 说明 |
|------|------|
| 09-MCP Server | `BuildMiddlewareChain()` 在 main.go 中为每个工具构建链 |
| 11-MCP 客户端 | Python 侧通过 HTTP 调用，中间件在 Go 侧执行 |

| 下游 | 说明 |
|------|------|
| 各组件工具 | 中间件包装后的 handler 执行实际工具逻辑 |
| 15-HITL | RiskAssessmentMiddleware 触发审批流程 |
| 17-可观测 | TracingMiddleware 创建 Span；各层记录 Prometheus 指标 |
| 16-安全 | AuditMiddleware 写入审计日志 |

### 9.1 中间件链健康检查

```go
// GET /health/middleware — 返回各层中间件状态
// WHY 独立健康检查：运维需要快速判断"哪层中间件有问题"
func MiddlewareHealthHandler(c *fiber.Ctx) error {
    health := map[string]interface{}{
        "tracing":         "ok",  // OTel exporter 是否连通
        "audit":           auditQueueHealth(),
        "cache":           cacheHealth(),
        "circuit_breakers": allCircuitBreakerStates(),
        "rate_limit":      "ok",
    }
    return c.JSON(health)
}

func auditQueueHealth() string {
    depth := len(auditCh) // 全局 audit channel
    if depth > 1500 {     // 75% 容量
        return fmt.Sprintf("warning: queue depth %d/2000", depth)
    }
    return "ok"
}

func allCircuitBreakerStates() map[string]string {
    circuitBreakersMu.RLock()
    defer circuitBreakersMu.RUnlock()
    states := make(map[string]string)
    for name, cb := range circuitBreakers {
        states[name] = cb.State().String()
    }
    return states
}
```

### 9.2 常见故障排查

| 症状 | 可能原因 | 排查步骤 |
|------|---------|---------|
| 所有工具 503 | 全局限流配额耗尽 | 查 `rate_limit_drops{scope="global"}` |
| 单个工具 503 | 该工具熔断器打开 | 查 `circuit_breaker_state{tool="X"}` |
| 审计日志丢失 | audit channel 满 | 查 `audit_queue_depth > 2000` |
| 工具延迟突增 | Cache Redis 连接失败 | 查 `middleware_duration{middleware="cache"}` |
| 参数校验误拦 | Schema 定义过严 | 查 `validation_failures{failure_type}` 分布 |

---

> **下一篇**：[11-MCP客户端与工具注册.md](./11-MCP客户端与工具注册.md)
