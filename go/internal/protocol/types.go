package protocol

import (
	"context"
	"encoding/json"
)

// ToolHandler 工具执行函数签名
type ToolHandler func(ctx context.Context, params map[string]interface{}) (*ToolResult, error)

// Request MCP JSON-RPC 2.0 请求
type Request struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      interface{}     `json:"id"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params,omitempty"`
}

// Response MCP JSON-RPC 2.0 响应
type Response struct {
	JSONRPC string      `json:"jsonrpc"`
	ID      interface{} `json:"id"`
	Result  interface{} `json:"result,omitempty"`
	Error   *RPCError   `json:"error,omitempty"`
}

// RPCError JSON-RPC 错误
type RPCError struct {
	Code    int         `json:"code"`
	Message string      `json:"message"`
	Data    interface{} `json:"data,omitempty"`
}

// ToolDefinition MCP 工具定义
type ToolDefinition struct {
	Name        string                 `json:"name"`
	Description string                 `json:"description"`
	InputSchema map[string]interface{} `json:"inputSchema"`
}

// ToolCallParams 工具调用参数
type ToolCallParams struct {
	Name      string                 `json:"name"`
	Arguments map[string]interface{} `json:"arguments"`
}

// ToolResult 工具调用结果
type ToolResult struct {
	Content []ContentBlock `json:"content"`
	IsError bool           `json:"isError,omitempty"`
}

// ContentBlock 内容块
type ContentBlock struct {
	Type string `json:"type"` // "text"
	Text string `json:"text"`
}

// RiskLevel 工具风险等级
type RiskLevel int

const (
	RiskNone     RiskLevel = 0 // 只读
	RiskLow      RiskLevel = 1
	RiskMedium   RiskLevel = 2
	RiskHigh     RiskLevel = 3
	RiskCritical RiskLevel = 4
)

// String 返回风险等级字符串
func (r RiskLevel) String() string {
	switch r {
	case RiskNone:
		return "none"
	case RiskLow:
		return "low"
	case RiskMedium:
		return "medium"
	case RiskHigh:
		return "high"
	case RiskCritical:
		return "critical"
	default:
		return "unknown"
	}
}

// Tool 工具接口
type Tool interface {
	Name() string
	Description() string
	Schema() map[string]interface{}
	RiskLevel() RiskLevel
	Execute(ctx context.Context, params map[string]interface{}) (*ToolResult, error)
}

// TextResult 构建文本结果的便捷方法
func TextResult(text string) *ToolResult {
	return &ToolResult{
		Content: []ContentBlock{{Type: "text", Text: text}},
	}
}

// ErrorResult 构建错误结果的便捷方法
func ErrorResult(text string) *ToolResult {
	return &ToolResult{
		Content: []ContentBlock{{Type: "text", Text: text}},
		IsError: true,
	}
}
