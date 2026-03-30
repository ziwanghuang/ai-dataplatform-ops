package protocol

import (
	"context"
	"encoding/json"
	"fmt"
)

// Handler JSON-RPC 请求处理器
type Handler struct {
	registry   *Registry
	middleware func(Tool, ToolHandler) ToolHandler
}

// NewHandler 创建处理器
func NewHandler(registry *Registry, mw func(Tool, ToolHandler) ToolHandler) *Handler {
	return &Handler{registry: registry, middleware: mw}
}

// HandleRequest 处理 JSON-RPC 请求
func (h *Handler) HandleRequest(ctx context.Context, raw []byte) ([]byte, error) {
	var req Request
	if err := json.Unmarshal(raw, &req); err != nil {
		return h.errorResponse(nil, -32700, "Parse error")
	}

	switch req.Method {
	case "tools/list":
		return h.handleToolsList(req)
	case "tools/call":
		return h.handleToolsCall(ctx, req)
	default:
		return h.errorResponse(req.ID, -32601, fmt.Sprintf("Method not found: %s", req.Method))
	}
}

func (h *Handler) handleToolsList(req Request) ([]byte, error) {
	defs := h.registry.ListDefinitions()
	return h.successResponse(req.ID, map[string]interface{}{"tools": defs})
}

func (h *Handler) handleToolsCall(ctx context.Context, req Request) ([]byte, error) {
	var params ToolCallParams
	if err := json.Unmarshal(req.Params, &params); err != nil {
		return h.errorResponse(req.ID, -32602, "Invalid params")
	}

	tool, err := h.registry.Get(params.Name)
	if err != nil {
		return h.errorResponse(req.ID, -32602, err.Error())
	}

	// 包装中间件链
	handler := func(ctx context.Context, p map[string]interface{}) (*ToolResult, error) {
		return tool.Execute(ctx, p)
	}
	if h.middleware != nil {
		handler = h.middleware(tool, handler)
	}

	result, err := handler(ctx, params.Arguments)
	if err != nil {
		return h.successResponse(req.ID, &ToolResult{
			Content: []ContentBlock{{Type: "text", Text: fmt.Sprintf("Error: %s", err)}},
			IsError: true,
		})
	}

	return h.successResponse(req.ID, result)
}

func (h *Handler) successResponse(id interface{}, result interface{}) ([]byte, error) {
	return json.Marshal(Response{JSONRPC: "2.0", ID: id, Result: result})
}

func (h *Handler) errorResponse(id interface{}, code int, msg string) ([]byte, error) {
	return json.Marshal(Response{JSONRPC: "2.0", ID: id, Error: &RPCError{Code: code, Message: msg}})
}
