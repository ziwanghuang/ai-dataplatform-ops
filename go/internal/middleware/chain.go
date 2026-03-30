package middleware

import (
	"time"

	"github.com/rs/zerolog/log"
	"github.com/ziwang/aiops-mcp/internal/config"
	"github.com/ziwang/aiops-mcp/internal/protocol"
)

// BuildMiddlewareChain 构建完整 8 层洋葱模型中间件链
// 请求流入顺序（从外到内）：
//   ① Tracing → ② Audit → ③ RateLimit → ④ Validation →
//   ⑤ Risk → ⑥ Cache → ⑦ CircuitBreaker → ⑧ Timeout → [Tool Execute]
//
// WHY 这个顺序：
// - Tracing 在最外层：确保所有请求都有 trace span，包括被拒绝的
// - Audit 在 Tracing 之后：记录所有请求日志
// - RateLimit 在前：尽早拒绝超限请求，避免浪费后续层的计算
// - Validation 在 Risk 之前：确保参数合法后再做风险评估
// - Risk 在 Cache 之前：高风险操作不应走缓存
// - Cache 在 CB 之前：缓存命中直接返回，不触发熔断器计数
// - CB 在 Timeout 之前：熔断器保护下游，Timeout 控制单次执行
// - Timeout 最内层：直接包裹工具执行
func BuildMiddlewareChain(handler ToolHandler, tool protocol.Tool, cfg *config.Config) ToolHandler {
	chain := handler

	// ⑧ Timeout（最内层）
	chain = TimeoutMiddleware(TimeoutConfig{
		Default: cfg.Middleware.Timeout.Default,
		PerTool: cfg.Middleware.Timeout.PerTool,
	})(chain)

	// ⑦ CircuitBreaker
	chain = CircuitBreakerMiddleware(tool.Name(), CBConfig{
		MaxRequests:  cfg.Middleware.CircuitBreaker.MaxRequests,
		Interval:     cfg.Middleware.CircuitBreaker.Interval,
		Timeout:      cfg.Middleware.CircuitBreaker.Timeout,
		FailureRatio: cfg.Middleware.CircuitBreaker.FailureRatio,
	})(chain)

	// ⑥ Cache — 只读工具开启缓存
	cacheTTL := cfg.Middleware.Cache.DefaultTTL
	if cacheTTL == 0 {
		cacheTTL = 5 * time.Minute
	}
	chain = CacheMiddleware(tool, CacheableConfig{
		Enabled:    cfg.Middleware.Cache.Enabled,
		DefaultTTL: cacheTTL,
		MaxEntries: 1000,
	})(chain)

	// ⑤ Risk Assessment
	chain = RiskAssessmentMiddleware(tool, DefaultRiskConfig())(chain)

	// ④ Validation
	chain = ValidationMiddleware(tool, DefaultValidationConfig())(chain)

	// ③ RateLimit
	chain = RateLimitMiddleware(RateLimitConfig{
		GlobalRPS:  cfg.Middleware.RateLimit.GlobalRPS,
		PerUserRPS: cfg.Middleware.RateLimit.PerUserRPS,
		BurstSize:  cfg.Middleware.RateLimit.GlobalRPS * 2,
	})(chain)

	// ② Audit
	chain = AuditMiddleware(AuditConfig{Enabled: true})(chain)

	// ① Tracing（最外层）
	chain = TracingMiddleware(tool.Name())(chain)

	log.Debug().
		Str("tool", tool.Name()).
		Str("risk", tool.RiskLevel().String()).
		Int("layers", 8).
		Msg("middleware chain built")

	return chain
}
