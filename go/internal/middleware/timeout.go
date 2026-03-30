package middleware

import (
	"context"
	"fmt"
	"time"

	"github.com/rs/zerolog/log"
	"github.com/ziwang/aiops-mcp/internal/pkg/errors"
	"github.com/ziwang/aiops-mcp/internal/protocol"
)

// TimeoutConfig 超时配置
type TimeoutConfig struct {
	Default time.Duration            `mapstructure:"default"`
	PerTool map[string]time.Duration `mapstructure:"per_tool"`
}

// TimeoutMiddleware 超时中间件
func TimeoutMiddleware(cfg TimeoutConfig) Middleware {
	return func(next ToolHandler) ToolHandler {
		return func(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
			// 工具级超时覆盖
			timeout := cfg.Default
			if timeout == 0 {
				timeout = 30 * time.Second
			}
			toolName, _ := ctx.Value(ctxKeyToolName).(string)
			if t, ok := cfg.PerTool[toolName]; ok {
				timeout = t
			}

			ctx, cancel := context.WithTimeout(ctx, timeout)
			defer cancel()

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
				log.Warn().Str("tool", toolName).Dur("timeout", timeout).Msg("tool execution timed out")
				return nil, errors.New(errors.ToolTimeout,
					fmt.Sprintf("tool %s timed out after %s", toolName, timeout))
			case r := <-ch:
				return r.res, r.err
			}
		}
	}
}
