package hdfs

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/ziwang/aiops-mcp/internal/protocol"
)

// NamenodeStatusTool HDFS NameNode 状态查询（Mock 实现）
type NamenodeStatusTool struct{}

func (t *NamenodeStatusTool) Name() string        { return "hdfs_namenode_status" }
func (t *NamenodeStatusTool) Description() string  { return "获取 HDFS NameNode 状态：heap 使用率/RPC 延迟/安全模式/HA 状态" }
func (t *NamenodeStatusTool) RiskLevel() protocol.RiskLevel { return protocol.RiskNone }
func (t *NamenodeStatusTool) Schema() map[string]interface{} {
	return map[string]interface{}{
		"type": "object",
		"properties": map[string]interface{}{
			"namenode": map[string]interface{}{
				"type":        "string",
				"description": "NameNode 标识：nn1, nn2, active",
				"default":     "active",
			},
		},
	}
}

func (t *NamenodeStatusTool) Execute(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
	nn, _ := params["namenode"].(string)
	if nn == "" {
		nn = "active"
	}

	// Mock 数据
	data := map[string]interface{}{
		"hostname":         fmt.Sprintf("hdfs-nn-%s.cluster.local", nn),
		"ha_state":         "active",
		"heap_used_mb":     2048,
		"heap_max_mb":      4096,
		"heap_usage_pct":   50.0,
		"rpc_avg_latency_ms": 1.2,
		"rpc_queue_length": 0,
		"safe_mode":        false,
		"blocks_total":     1250000,
		"blocks_missing":   0,
		"blocks_under_replicated": 12,
		"live_datanodes":   48,
		"dead_datanodes":   0,
		"decom_datanodes":  1,
	}

	b, _ := json.MarshalIndent(data, "", "  ")
	return protocol.TextResult(string(b)), nil
}

// ClusterOverviewTool HDFS 集群概览
type ClusterOverviewTool struct{}

func (t *ClusterOverviewTool) Name() string        { return "hdfs_cluster_overview" }
func (t *ClusterOverviewTool) Description() string  { return "获取 HDFS 集群概览：总容量/已用/剩余/块数量/副本不足块" }
func (t *ClusterOverviewTool) RiskLevel() protocol.RiskLevel { return protocol.RiskNone }
func (t *ClusterOverviewTool) Schema() map[string]interface{} {
	return map[string]interface{}{"type": "object", "properties": map[string]interface{}{}}
}

func (t *ClusterOverviewTool) Execute(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
	data := map[string]interface{}{
		"total_capacity_tb":    120.0,
		"used_capacity_tb":     87.3,
		"remaining_capacity_tb": 32.7,
		"usage_pct":            72.75,
		"total_blocks":         1250000,
		"missing_blocks":       0,
		"under_replicated":     12,
		"corrupt_blocks":       0,
		"total_datanodes":      49,
		"live_datanodes":       48,
		"dead_datanodes":       0,
		"decommissioning":      1,
	}

	b, _ := json.MarshalIndent(data, "", "  ")
	return protocol.TextResult(string(b)), nil
}
