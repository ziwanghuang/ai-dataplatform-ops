package es

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/ziwang/aiops-mcp/internal/protocol"
)

// ────────────────────────────────────────────────────────────────────────────
// ClusterHealthTool — ES 集群健康状态查询
// WHY 单独工具而不是合并到 node_stats：
// - cluster_health 是最常用的诊断入口（类似 HDFS safe_mode 检查）
// - 返回数据量小、延迟低，适合 Triage 快速路径
// - risk=none，不需要审批就能调用
// ────────────────────────────────────────────────────────────────────────────

type ClusterHealthTool struct{}

func (t *ClusterHealthTool) Name() string        { return "es_cluster_health" }
func (t *ClusterHealthTool) Description() string  { return "获取 Elasticsearch 集群健康状态：status/nodes/shards/unassigned" }
func (t *ClusterHealthTool) RiskLevel() protocol.RiskLevel { return protocol.RiskNone }
func (t *ClusterHealthTool) Schema() map[string]interface{} {
	return map[string]interface{}{
		"type": "object",
		"properties": map[string]interface{}{
			"cluster_name": map[string]interface{}{
				"type":        "string",
				"description": "ES 集群名称，默认查所有",
				"default":     "ops-es-cluster",
			},
		},
	}
}

func (t *ClusterHealthTool) Execute(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
	clusterName, _ := params["cluster_name"].(string)
	if clusterName == "" {
		clusterName = "ops-es-cluster"
	}

	// Mock: 模拟一个 yellow 状态集群（常见的诊断场景）
	data := map[string]interface{}{
		"cluster_name":                     clusterName,
		"status":                           "yellow",
		"timed_out":                        false,
		"number_of_nodes":                  5,
		"number_of_data_nodes":             3,
		"active_primary_shards":            42,
		"active_shards":                    84,
		"relocating_shards":                0,
		"initializing_shards":              0,
		"unassigned_shards":                3,
		"delayed_unassigned_shards":        0,
		"number_of_pending_tasks":          0,
		"number_of_in_flight_fetch":        0,
		"task_max_waiting_in_queue_millis": 0,
		"active_shards_percent_as_number":  96.55,
	}

	b, _ := json.MarshalIndent(data, "", "  ")
	return protocol.TextResult(string(b)), nil
}

// ────────────────────────────────────────────────────────────────────────────
// NodeStatsTool — ES 节点级别指标查询
// WHY 节点粒度：
// - Diagnostic Agent 定位根因时需要知道哪个节点出问题
// - 返回 JVM heap/GC/disk/search rate 等关键指标
// ────────────────────────────────────────────────────────────────────────────

type NodeStatsTool struct{}

func (t *NodeStatsTool) Name() string        { return "es_node_stats" }
func (t *NodeStatsTool) Description() string  { return "获取 ES 节点级指标：JVM heap/GC 时间/磁盘/索引速率/搜索速率" }
func (t *NodeStatsTool) RiskLevel() protocol.RiskLevel { return protocol.RiskNone }
func (t *NodeStatsTool) Schema() map[string]interface{} {
	return map[string]interface{}{
		"type": "object",
		"properties": map[string]interface{}{
			"node_id": map[string]interface{}{
				"type":        "string",
				"description": "节点 ID 或名称，空表示查所有",
			},
		},
	}
}

func (t *NodeStatsTool) Execute(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
	nodeID, _ := params["node_id"].(string)

	// Mock: 3 个数据节点，其中 data-02 有 JVM 压力
	nodes := []map[string]interface{}{
		{
			"node_id":              "data-01",
			"name":                 "es-data-01",
			"roles":                []string{"data", "ingest"},
			"jvm_heap_used_mb":     3072,
			"jvm_heap_max_mb":      8192,
			"jvm_heap_usage_pct":   37.5,
			"jvm_gc_old_count":     12,
			"jvm_gc_old_time_ms":   450,
			"jvm_gc_young_count":   1280,
			"jvm_gc_young_time_ms": 8500,
			"disk_total_gb":        500.0,
			"disk_used_gb":         320.5,
			"disk_usage_pct":       64.1,
			"indexing_rate_per_s":  1200.0,
			"search_rate_per_s":   350.0,
			"search_latency_ms":   12.5,
		},
		{
			"node_id":              "data-02",
			"name":                 "es-data-02",
			"roles":                []string{"data", "ingest"},
			"jvm_heap_used_mb":     7168,
			"jvm_heap_max_mb":      8192,
			"jvm_heap_usage_pct":   87.5, // ⚠️ 高堆使用率
			"jvm_gc_old_count":     89,   // ⚠️ old GC 频繁
			"jvm_gc_old_time_ms":   15200,
			"jvm_gc_young_count":   5600,
			"jvm_gc_young_time_ms": 28000,
			"disk_total_gb":        500.0,
			"disk_used_gb":         465.2,
			"disk_usage_pct":       93.0, // ⚠️ 磁盘快满
			"indexing_rate_per_s":  200.0,
			"search_rate_per_s":   80.0,
			"search_latency_ms":   245.0, // ⚠️ 搜索延迟高
		},
		{
			"node_id":              "data-03",
			"name":                 "es-data-03",
			"roles":                []string{"data"},
			"jvm_heap_used_mb":     2560,
			"jvm_heap_max_mb":      8192,
			"jvm_heap_usage_pct":   31.25,
			"jvm_gc_old_count":     8,
			"jvm_gc_old_time_ms":   320,
			"jvm_gc_young_count":   980,
			"jvm_gc_young_time_ms": 6200,
			"disk_total_gb":        500.0,
			"disk_used_gb":         290.0,
			"disk_usage_pct":       58.0,
			"indexing_rate_per_s":  1100.0,
			"search_rate_per_s":   420.0,
			"search_latency_ms":   8.2,
		},
	}

	// 按 node_id 过滤
	var result interface{}
	if nodeID != "" {
		for _, n := range nodes {
			if n["node_id"] == nodeID || n["name"] == nodeID {
				result = n
				break
			}
		}
		if result == nil {
			return protocol.ErrorResult(fmt.Sprintf("node not found: %s", nodeID)), nil
		}
	} else {
		result = map[string]interface{}{
			"total_nodes": len(nodes),
			"nodes":       nodes,
		}
	}

	b, _ := json.MarshalIndent(result, "", "  ")
	return protocol.TextResult(string(b)), nil
}

// ────────────────────────────────────────────────────────────────────────────
// IndexStatsTool — ES 索引统计查询
// WHY：
// - 运维场景中经常需要查看特定索引的大小、文档数、分片分布
// - 帮助诊断写入瓶颈或查询慢的根因
// ────────────────────────────────────────────────────────────────────────────

type IndexStatsTool struct{}

func (t *IndexStatsTool) Name() string        { return "es_index_stats" }
func (t *IndexStatsTool) Description() string  { return "获取 ES 索引统计：文档数/存储大小/分片数/索引速率" }
func (t *IndexStatsTool) RiskLevel() protocol.RiskLevel { return protocol.RiskNone }
func (t *IndexStatsTool) Schema() map[string]interface{} {
	return map[string]interface{}{
		"type": "object",
		"properties": map[string]interface{}{
			"index_pattern": map[string]interface{}{
				"type":        "string",
				"description": "索引名或通配符（如 logs-*），默认 _all",
				"default":     "_all",
			},
		},
	}
}

func (t *IndexStatsTool) Execute(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
	pattern, _ := params["index_pattern"].(string)
	if pattern == "" {
		pattern = "_all"
	}

	// Mock: 常见的日志和指标索引
	data := map[string]interface{}{
		"pattern": pattern,
		"indices": []map[string]interface{}{
			{
				"index":            "logs-2026.03.30",
				"health":           "green",
				"status":           "open",
				"primary_shards":   5,
				"replica_shards":   1,
				"docs_count":       12500000,
				"docs_deleted":     150000,
				"store_size_gb":    8.5,
				"indexing_rate":    1500.0,
				"search_rate":     200.0,
			},
			{
				"index":            "logs-2026.03.29",
				"health":           "green",
				"status":           "open",
				"primary_shards":   5,
				"replica_shards":   1,
				"docs_count":       28000000,
				"docs_deleted":     350000,
				"store_size_gb":    19.2,
				"indexing_rate":    0.0,
				"search_rate":     50.0,
			},
			{
				"index":            "metrics-system-2026.03.30",
				"health":           "yellow",
				"status":           "open",
				"primary_shards":   3,
				"replica_shards":   1,
				"docs_count":       5200000,
				"docs_deleted":     20000,
				"store_size_gb":    3.1,
				"indexing_rate":    800.0,
				"search_rate":     150.0,
			},
			{
				"index":            "alerts-2026.03",
				"health":           "green",
				"status":           "open",
				"primary_shards":   2,
				"replica_shards":   1,
				"docs_count":       85000,
				"docs_deleted":     500,
				"store_size_gb":    0.12,
				"indexing_rate":    5.0,
				"search_rate":     30.0,
			},
		},
		"total_docs":     45785000,
		"total_size_gb":  30.92,
		"total_indices":  4,
	}

	b, _ := json.MarshalIndent(data, "", "  ")
	return protocol.TextResult(string(b)), nil
}
