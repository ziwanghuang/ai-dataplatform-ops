package middleware

import (
	"context"
	"fmt"
	"sync"
	"time"

	"github.com/rs/zerolog/log"
	"github.com/sony/gobreaker"
	"github.com/ziwang/aiops-mcp/internal/pkg/errors"
	"github.com/ziwang/aiops-mcp/internal/protocol"
)

// CBConfig 熔断器配置
type CBConfig struct {
	MaxRequests  uint32        `mapstructure:"max_requests"`
	Interval     time.Duration `mapstructure:"interval"`
	Timeout      time.Duration `mapstructure:"timeout"`
	FailureRatio float64       `mapstructure:"failure_ratio"`
}

// 每个工具独立的熔断器
var (
	cbMu       sync.RWMutex
	breakers   = make(map[string]*gobreaker.CircuitBreaker)
)

func getOrCreateBreaker(name string, cfg CBConfig) *gobreaker.CircuitBreaker {
	cbMu.RLock()
	cb, ok := breakers[name]
	cbMu.RUnlock()
	if ok {
		return cb
	}

	cbMu.Lock()
	defer cbMu.Unlock()
	// double-check
	if cb, ok = breakers[name]; ok {
		return cb
	}

	maxReq := cfg.MaxRequests
	if maxReq == 0 {
		maxReq = 5
	}
	interval := cfg.Interval
	if interval == 0 {
		interval = 60 * time.Second
	}
	cbTimeout := cfg.Timeout
	if cbTimeout == 0 {
		cbTimeout = 30 * time.Second
	}
	ratio := cfg.FailureRatio
	if ratio == 0 {
		ratio = 0.5
	}

	cb = gobreaker.NewCircuitBreaker(gobreaker.Settings{
		Name:        name,
		MaxRequests: maxReq,
		Interval:    interval,
		Timeout:     cbTimeout,
		ReadyToTrip: func(counts gobreaker.Counts) bool {
			if counts.Requests < 5 {
				return false // 冷启动保护
			}
			failureRatio := float64(counts.TotalFailures) / float64(counts.Requests)
			return failureRatio >= ratio
		},
		OnStateChange: func(name string, from gobreaker.State, to gobreaker.State) {
			log.Warn().
				Str("breaker", name).
				Str("from", from.String()).
				Str("to", to.String()).
				Msg("circuit breaker state changed")
		},
	})
	breakers[name] = cb
	return cb
}

// CircuitBreakerMiddleware 熔断器中间件
func CircuitBreakerMiddleware(toolName string, cfg CBConfig) Middleware {
	return func(next ToolHandler) ToolHandler {
		cb := getOrCreateBreaker(toolName, cfg)

		return func(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
			result, err := cb.Execute(func() (interface{}, error) {
				return next(ctx, params)
			})

			if err != nil {
				if err == gobreaker.ErrOpenState || err == gobreaker.ErrTooManyRequests {
					log.Warn().Str("tool", toolName).Msg("circuit breaker open, rejecting request")
					return nil, errors.New(errors.ToolCircuitOpen,
						fmt.Sprintf("circuit breaker open for tool %s", toolName))
				}
				return nil, err
			}

			return result.(*protocol.ToolResult), nil
		}
	}
}
