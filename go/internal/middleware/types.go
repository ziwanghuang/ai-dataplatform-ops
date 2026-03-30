package middleware

import (
	"context"

	"github.com/ziwang/aiops-mcp/internal/protocol"
)

// ToolHandler 工具执行函数签名
type ToolHandler func(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error)

// Middleware 中间件函数签名（装饰器模式）
type Middleware func(ToolHandler) ToolHandler

// ────────────────────────────────────────────────────────────────────────────
// Context Keys
// WHY 集中定义 context key：
// - 避免各中间件各自定义导致 key 冲突
// - 类型安全：ctxKey 是 unexported 类型，外部包无法构造相同的 key
// ────────────────────────────────────────────────────────────────────────────

type ctxKey string

const (
	ctxKeyToolName      ctxKey = "tool_name"
	ctxKeyTraceID       ctxKey = "trace_id"
	ctxKeyUserID        ctxKey = "user_id"
	ctxKeyApprovalToken ctxKey = "approval_token"
	ctxKeyRiskLevel     ctxKey = "risk_level"
)

// WithToolContext 在 context 中注入工具名和 trace_id
func WithToolContext(ctx context.Context, toolName, traceID string) context.Context {
	ctx = context.WithValue(ctx, ctxKeyToolName, toolName)
	ctx = context.WithValue(ctx, ctxKeyTraceID, traceID)
	return ctx
}

// WithUserContext 在 context 中注入用户信息
func WithUserContext(ctx context.Context, userID string) context.Context {
	return context.WithValue(ctx, ctxKeyUserID, userID)
}

// WithApprovalToken 在 context 中注入审批令牌
func WithApprovalToken(ctx context.Context, token string) context.Context {
	return context.WithValue(ctx, ctxKeyApprovalToken, token)
}
