package protocol

import (
	"fmt"
	"sync"
)

// Registry 工具注册中心
type Registry struct {
	mu    sync.RWMutex
	tools map[string]Tool
}

// NewRegistry 创建新的工具注册中心
func NewRegistry() *Registry {
	return &Registry{tools: make(map[string]Tool)}
}

// Register 注册工具
func (r *Registry) Register(tool Tool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.tools[tool.Name()] = tool
}

// Get 获取工具
func (r *Registry) Get(name string) (Tool, error) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	tool, ok := r.tools[name]
	if !ok {
		return nil, fmt.Errorf("tool not found: %s", name)
	}
	return tool, nil
}

// ListDefinitions 列出所有工具定义
func (r *Registry) ListDefinitions() []ToolDefinition {
	r.mu.RLock()
	defer r.mu.RUnlock()
	defs := make([]ToolDefinition, 0, len(r.tools))
	for _, t := range r.tools {
		defs = append(defs, ToolDefinition{
			Name:        t.Name(),
			Description: t.Description(),
			InputSchema: t.Schema(),
		})
	}
	return defs
}

// Count 返回已注册工具数量
func (r *Registry) Count() int {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return len(r.tools)
}
