package middleware

import (
	"context"
	"time"

	"github.com/rs/zerolog/log"
	"github.com/ziwang/aiops-mcp/internal/protocol"
)

// AuditConfig 审计配置
type AuditConfig struct {
	Enabled bool `mapstructure:"enabled"`
}

// AuditMiddleware 审计日志中间件
func AuditMiddleware(cfg AuditConfig) Middleware {
	return func(next ToolHandler) ToolHandler {
		return func(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
			toolName, _ := ctx.Value(ctxKeyToolName).(string)
			traceID, _ := ctx.Value(ctxKeyTraceID).(string)
			start := time.Now()

			// 前置：记录请求
			if cfg.Enabled {
				log.Info().
					Str("trace_id", traceID).
					Str("tool", toolName).
					Interface("params", params).
					Msg("audit_tool_call_start")
			}

			// 执行
			result, err := next(ctx, params)
			duration := time.Since(start)

			// 后置：记录结果（异步写入生产环境应该用 goroutine + channel）
			if cfg.Enabled {
				event := log.Info().
					Str("trace_id", traceID).
					Str("tool", toolName).
					Dur("duration", duration)

				if err != nil {
					event.Err(err).Msg("audit_tool_call_error")
				} else if result != nil && result.IsError {
					event.Bool("tool_error", true).Msg("audit_tool_call_done")
				} else {
					event.Msg("audit_tool_call_done")
				}
			}

			return result, err
		}
	}
}
