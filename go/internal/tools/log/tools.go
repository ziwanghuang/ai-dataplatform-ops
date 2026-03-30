package log

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/ziwang/aiops-mcp/internal/protocol"
)

// SearchLogsTool 日志搜索工具
type SearchLogsTool struct{}

func (t *SearchLogsTool) Name() string        { return "search_logs" }
func (t *SearchLogsTool) Description() string  { return "日志搜索（ES + Lucene 语法）" }
func (t *SearchLogsTool) RiskLevel() protocol.RiskLevel { return protocol.RiskNone }
func (t *SearchLogsTool) Schema() map[string]interface{} {
	return map[string]interface{}{
		"type": "object",
		"properties": map[string]interface{}{
			"query": map[string]interface{}{
				"type":        "string",
				"description": "搜索关键词（Lucene 语法）",
			},
			"index": map[string]interface{}{
				"type":        "string",
				"description": "ES 索引名",
				"default":     "logs-*",
			},
			"time_range": map[string]interface{}{
				"type":        "string",
				"description": "时间范围：15m, 1h, 24h",
				"default":     "1h",
			},
			"limit": map[string]interface{}{
				"type":        "integer",
				"description": "返回条数",
				"default":     20,
			},
		},
		"required": []string{"query"},
	}
}

func (t *SearchLogsTool) Execute(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
	query, _ := params["query"].(string)
	index, _ := params["index"].(string)
	if index == "" {
		index = "logs-*"
	}

	data := map[string]interface{}{
		"query":       query,
		"index":       index,
		"total_hits":  42,
		"returned":    3,
		"hits": []map[string]interface{}{
			{
				"timestamp": "2026-03-30T01:15:23Z",
				"level":     "ERROR",
				"service":   "hdfs-namenode",
				"message":   fmt.Sprintf("Found match for '%s': java.lang.OutOfMemoryError: GC overhead limit exceeded", query),
			},
			{
				"timestamp": "2026-03-30T01:14:50Z",
				"level":     "WARN",
				"service":   "hdfs-namenode",
				"message":   fmt.Sprintf("Related to '%s': GC pause exceeded threshold: 5.2s", query),
			},
			{
				"timestamp": "2026-03-30T01:14:30Z",
				"level":     "INFO",
				"service":   "hdfs-namenode",
				"message":   "Block report received from DataNode dn-03, 250000 blocks",
			},
		},
	}

	b, _ := json.MarshalIndent(data, "", "  ")
	return protocol.TextResult(string(b)), nil
}
