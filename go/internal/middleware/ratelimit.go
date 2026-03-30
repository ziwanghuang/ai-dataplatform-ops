package middleware

import (
	"context"
	"fmt"
	"sync"

	"github.com/rs/zerolog/log"
	"github.com/ziwang/aiops-mcp/internal/pkg/errors"
	"github.com/ziwang/aiops-mcp/internal/protocol"
	"golang.org/x/time/rate"
)

// ────────────────────────────────────────────────────────────────────────────
// RateLimitMiddleware — 令牌桶限流中间件
// WHY 限流而不是让所有请求通过：
// - 大数据平台 API 有并发限制（如 NameNode RPC 线程池）
// - 防止 Agent 循环调用导致工具端过载
// - 全局限流 + 每用户限流双层保护
//
// WHY 令牌桶而不是滑动窗口：
// - 令牌桶允许突发流量（burst），更适合诊断场景
// - 诊断时可能短时间并行调用多个工具，需要 burst 容忍
// ────────────────────────────────────────────────────────────────────────────

// RateLimitConfig 限流配置
type RateLimitConfig struct {
	GlobalRPS  int `mapstructure:"global_rps"`  // 全局 QPS 限制
	PerUserRPS int `mapstructure:"per_user_rps"` // 每用户 QPS
	BurstSize  int `mapstructure:"burst_size"`   // 突发容量
}

// rateLimiterStore 管理每用户的限流器实例
type rateLimiterStore struct {
	mu       sync.RWMutex
	limiters map[string]*rate.Limiter
	perUser  rate.Limit
	burst    int
}

func newRateLimiterStore(perUserRPS int, burst int) *rateLimiterStore {
	if perUserRPS <= 0 {
		perUserRPS = 10
	}
	if burst <= 0 {
		burst = perUserRPS * 2 // 默认 burst = 2x RPS
	}
	return &rateLimiterStore{
		limiters: make(map[string]*rate.Limiter),
		perUser:  rate.Limit(perUserRPS),
		burst:    burst,
	}
}

func (s *rateLimiterStore) getLimiter(userID string) *rate.Limiter {
	s.mu.RLock()
	limiter, ok := s.limiters[userID]
	s.mu.RUnlock()
	if ok {
		return limiter
	}

	s.mu.Lock()
	defer s.mu.Unlock()
	// Double-check
	if limiter, ok = s.limiters[userID]; ok {
		return limiter
	}

	limiter = rate.NewLimiter(s.perUser, s.burst)
	s.limiters[userID] = limiter
	return limiter
}

// RateLimitMiddleware 创建限流中间件
func RateLimitMiddleware(cfg RateLimitConfig) Middleware {
	globalRPS := cfg.GlobalRPS
	if globalRPS <= 0 {
		globalRPS = 100
	}
	burstSize := cfg.BurstSize
	if burstSize <= 0 {
		burstSize = globalRPS * 2
	}

	// 全局限流器
	globalLimiter := rate.NewLimiter(rate.Limit(globalRPS), burstSize)
	// 每用户限流器存储
	userStore := newRateLimiterStore(cfg.PerUserRPS, burstSize/5)

	return func(next ToolHandler) ToolHandler {
		return func(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
			toolName, _ := ctx.Value(ctxKeyToolName).(string)

			// ① 全局限流
			if !globalLimiter.Allow() {
				log.Warn().
					Str("tool", toolName).
					Int("global_rps", globalRPS).
					Msg("rate_limit_global_exceeded")
				return nil, errors.New(errors.ToolRateLimited,
					fmt.Sprintf("global rate limit exceeded (%d RPS)", globalRPS))
			}

			// ② 每用户限流（从 context 中提取 user_id）
			userID, _ := ctx.Value(ctxKeyUserID).(string)
			if userID == "" {
				userID = "anonymous"
			}
			userLimiter := userStore.getLimiter(userID)
			if !userLimiter.Allow() {
				log.Warn().
					Str("tool", toolName).
					Str("user", userID).
					Int("per_user_rps", cfg.PerUserRPS).
					Msg("rate_limit_user_exceeded")
				return nil, errors.New(errors.ToolRateLimited,
					fmt.Sprintf("per-user rate limit exceeded for %s (%d RPS)", userID, cfg.PerUserRPS))
			}

			return next(ctx, params)
		}
	}
}
