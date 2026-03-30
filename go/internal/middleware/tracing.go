package middleware

import (
	"context"
	"time"

	"github.com/rs/zerolog/log"
	"github.com/ziwang/aiops-mcp/internal/protocol"
)

// TracingMiddleware 追踪中间件（简化版，Sprint 5 集成 OTel）
func TracingMiddleware(toolName string) Middleware {
	return func(next ToolHandler) ToolHandler {
		return func(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
			traceID, _ := ctx.Value(ctxKeyTraceID).(string)
			start := time.Now()

			log.Debug().
				Str("trace_id", traceID).
				Str("tool", toolName).
				Msg("tool_call_start")

			result, err := next(ctx, params)
			duration := time.Since(start)

			log.Debug().
				Str("trace_id", traceID).
				Str("tool", toolName).
				Dur("duration", duration).
				Bool("error", err != nil).
				Msg("tool_call_end")

			return result, err
		}
	}
}
