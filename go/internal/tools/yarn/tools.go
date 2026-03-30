package yarn

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/ziwang/aiops-mcp/internal/protocol"
)

// ────────────────────────────────────────────────────────────────────────────
// QueueStatusTool — YARN 队列状态查询
// WHY 队列是 YARN 诊断核心入口：
// - 大数据平台 90% 的"任务卡住"都是队列资源不足
// - 返回 capacity/usedCapacity/maxCapacity 帮助判断资源争抢
// - 包含 pending 和 running apps 数量辅助判断积压程度
// ────────────────────────────────────────────────────────────────────────────

type QueueStatusTool struct{}

func (t *QueueStatusTool) Name() string        { return "yarn_queue_status" }
func (t *QueueStatusTool) Description() string  { return "获取 YARN 队列状态：容量/已用/待处理应用/运行应用" }
func (t *QueueStatusTool) RiskLevel() protocol.RiskLevel { return protocol.RiskNone }
func (t *QueueStatusTool) Schema() map[string]interface{} {
	return map[string]interface{}{
		"type": "object",
		"properties": map[string]interface{}{
			"queue_name": map[string]interface{}{
				"type":        "string",
				"description": "队列名称，默认 root（返回所有子队列）",
				"default":     "root",
			},
		},
	}
}

func (t *QueueStatusTool) Execute(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
	queueName, _ := params["queue_name"].(string)
	if queueName == "" {
		queueName = "root"
	}

	// Mock: 典型的 3 层队列结构 (root -> production/development/default)
	data := map[string]interface{}{
		"queue_name": queueName,
		"scheduler":  "CapacityScheduler",
		"queues": []map[string]interface{}{
			{
				"name":                "root.production",
				"state":               "RUNNING",
				"capacity":            60.0,
				"used_capacity":       55.3,
				"max_capacity":        80.0,
				"absolute_used_pct":   92.2, // ⚠️ 接近上限
				"pending_apps":        3,
				"running_apps":        12,
				"allocated_vcores":    96,
				"allocated_memory_gb": 384,
				"pending_vcores":      24,
				"pending_memory_gb":   96,
				"num_containers":      156,
			},
			{
				"name":                "root.development",
				"state":               "RUNNING",
				"capacity":            30.0,
				"used_capacity":       18.5,
				"max_capacity":        50.0,
				"absolute_used_pct":   61.7,
				"pending_apps":        0,
				"running_apps":        5,
				"allocated_vcores":    32,
				"allocated_memory_gb": 128,
				"pending_vcores":      0,
				"pending_memory_gb":   0,
				"num_containers":      48,
			},
			{
				"name":                "root.default",
				"state":               "RUNNING",
				"capacity":            10.0,
				"used_capacity":       2.1,
				"max_capacity":        20.0,
				"absolute_used_pct":   21.0,
				"pending_apps":        1,
				"running_apps":        2,
				"allocated_vcores":    4,
				"allocated_memory_gb": 16,
				"pending_vcores":      2,
				"pending_memory_gb":   8,
				"num_containers":      6,
			},
		},
		"cluster_summary": map[string]interface{}{
			"total_vcores":       192,
			"used_vcores":        132,
			"available_vcores":   60,
			"total_memory_gb":    768,
			"used_memory_gb":     528,
			"available_memory_gb": 240,
			"total_nodes":        8,
			"active_nodes":       8,
		},
	}

	b, _ := json.MarshalIndent(data, "", "  ")
	return protocol.TextResult(string(b)), nil
}

// ────────────────────────────────────────────────────────────────────────────
// ApplicationsTool — YARN 应用列表查询
// WHY 需要应用级可见性：
// - 当队列拥塞时，需要看清哪些应用占了多少资源
// - 支持按状态过滤（RUNNING/ACCEPTED/KILLED），快速定位问题应用
// ────────────────────────────────────────────────────────────────────────────

type ApplicationsTool struct{}

func (t *ApplicationsTool) Name() string        { return "yarn_applications" }
func (t *ApplicationsTool) Description() string  { return "查询 YARN 应用列表：按队列/状态/用户过滤" }
func (t *ApplicationsTool) RiskLevel() protocol.RiskLevel { return protocol.RiskNone }
func (t *ApplicationsTool) Schema() map[string]interface{} {
	return map[string]interface{}{
		"type": "object",
		"properties": map[string]interface{}{
			"queue": map[string]interface{}{
				"type":        "string",
				"description": "队列名称过滤",
			},
			"state": map[string]interface{}{
				"type":        "string",
				"description": "应用状态过滤：RUNNING, ACCEPTED, KILLED, FAILED, FINISHED",
				"default":     "RUNNING",
			},
			"user": map[string]interface{}{
				"type":        "string",
				"description": "提交用户过滤",
			},
			"limit": map[string]interface{}{
				"type":        "integer",
				"description": "返回条数限制",
				"default":     20,
			},
		},
	}
}

func (t *ApplicationsTool) Execute(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
	state, _ := params["state"].(string)
	if state == "" {
		state = "RUNNING"
	}

	// Mock: 混合 Spark/Flink/MapReduce 应用
	apps := []map[string]interface{}{
		{
			"app_id":          "application_1711750000_0042",
			"name":            "spark-etl-user-behavior-daily",
			"type":            "SPARK",
			"user":            "etl_user",
			"queue":           "root.production",
			"state":           "RUNNING",
			"final_status":    "UNDEFINED",
			"progress":        68.5,
			"started_time":    "2026-03-30T02:00:00Z",
			"elapsed_time_s":  24120,
			"allocated_vcores": 32,
			"allocated_mb":    131072,
			"running_containers": 33,
			"diagnostics":     "",
		},
		{
			"app_id":          "application_1711750000_0038",
			"name":            "flink-realtime-event-processor",
			"type":            "FLINK",
			"user":            "streaming",
			"queue":           "root.production",
			"state":           "RUNNING",
			"final_status":    "UNDEFINED",
			"progress":        100.0, // Flink streaming 永远 100%
			"started_time":    "2026-03-28T00:00:00Z",
			"elapsed_time_s":  205200,
			"allocated_vcores": 48,
			"allocated_mb":    196608,
			"running_containers": 49,
			"diagnostics":     "",
		},
		{
			"app_id":          "application_1711750000_0045",
			"name":            "hive-query-report-monthly",
			"type":            "MAPREDUCE",
			"user":            "analytics",
			"queue":           "root.production",
			"state":           "RUNNING",
			"final_status":    "UNDEFINED",
			"progress":        12.3,
			"started_time":    "2026-03-30T07:30:00Z",
			"elapsed_time_s":  4380,
			"allocated_vcores": 16,
			"allocated_mb":    65536,
			"running_containers": 17,
			"diagnostics":     "",
		},
		{
			"app_id":          "application_1711750000_0047",
			"name":            "spark-ml-feature-engineering",
			"type":            "SPARK",
			"user":            "ml_team",
			"queue":           "root.development",
			"state":           "RUNNING",
			"final_status":    "UNDEFINED",
			"progress":        45.0,
			"started_time":    "2026-03-30T06:00:00Z",
			"elapsed_time_s":  9780,
			"allocated_vcores": 24,
			"allocated_mb":    98304,
			"running_containers": 25,
			"diagnostics":     "",
		},
		{
			"app_id":          "application_1711750000_0048",
			"name":            "spark-adhoc-debug-query",
			"type":            "SPARK",
			"user":            "dev_test",
			"queue":           "root.development",
			"state":           "ACCEPTED", // ⚠️ 等待资源
			"final_status":    "UNDEFINED",
			"progress":        0.0,
			"started_time":    "2026-03-30T08:15:00Z",
			"elapsed_time_s":  1500,
			"allocated_vcores": 0,
			"allocated_mb":    0,
			"running_containers": 0,
			"diagnostics":     "Application is waiting for resources",
		},
	}

	// 简单的状态过滤
	var filtered []map[string]interface{}
	for _, app := range apps {
		if state == "" || app["state"] == state {
			filtered = append(filtered, app)
		}
	}

	result := map[string]interface{}{
		"total_matching": len(filtered),
		"state_filter":   state,
		"applications":   filtered,
	}

	b, _ := json.MarshalIndent(result, "", "  ")
	return protocol.TextResult(string(b)), nil
}

// ────────────────────────────────────────────────────────────────────────────
// ClusterMetricsTool — YARN 集群整体指标
// WHY：
// - 宏观视角：总资源/已用/可用，一眼判断集群是否整体过载
// - 辅助 AlertCorrelation：多个队列同时告警时先看集群级别
// ────────────────────────────────────────────────────────────────────────────

type ClusterMetricsTool struct{}

func (t *ClusterMetricsTool) Name() string        { return "yarn_cluster_metrics" }
func (t *ClusterMetricsTool) Description() string  { return "获取 YARN 集群整体指标：资源总量/已用/可用/节点状态" }
func (t *ClusterMetricsTool) RiskLevel() protocol.RiskLevel { return protocol.RiskNone }
func (t *ClusterMetricsTool) Schema() map[string]interface{} {
	return map[string]interface{}{"type": "object", "properties": map[string]interface{}{}}
}

func (t *ClusterMetricsTool) Execute(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
	data := map[string]interface{}{
		"cluster_id":          "yarn-prod-01",
		"resource_manager_ha": true,
		"active_rm":           "rm1.cluster.local",
		"standby_rm":          "rm2.cluster.local",
		"total_vcores":        192,
		"used_vcores":         132,
		"available_vcores":    60,
		"vcore_usage_pct":     68.75,
		"total_memory_gb":     768,
		"used_memory_gb":      528,
		"available_memory_gb": 240,
		"memory_usage_pct":    68.75,
		"total_nodes":         8,
		"active_nodes":        8,
		"unhealthy_nodes":     0,
		"decommissioned_nodes": 0,
		"lost_nodes":          0,
		"apps_submitted":      156,
		"apps_completed":      128,
		"apps_running":        17,
		"apps_pending":        4,
		"apps_failed":         5,
		"apps_killed":         2,
		"containers_allocated": 210,
		"containers_reserved":  8,
		"containers_pending":   12,
	}

	b, _ := json.MarshalIndent(data, "", "  ")
	return protocol.TextResult(string(b)), nil
}

// ────────────────────────────────────────────────────────────────────────────
// KillApplicationTool — 终止 YARN 应用（高风险操作）
// WHY 需要 kill 能力：
// - 诊断出失控应用后，Remediation 需要终止它释放资源
// - RiskLevel=High 要求 HITL 审批
// ────────────────────────────────────────────────────────────────────────────

type KillApplicationTool struct{}

func (t *KillApplicationTool) Name() string        { return "yarn_kill_application" }
func (t *KillApplicationTool) Description() string  { return "终止 YARN 应用（高风险：需要审批）" }
func (t *KillApplicationTool) RiskLevel() protocol.RiskLevel { return protocol.RiskHigh }
func (t *KillApplicationTool) Schema() map[string]interface{} {
	return map[string]interface{}{
		"type": "object",
		"properties": map[string]interface{}{
			"app_id": map[string]interface{}{
				"type":        "string",
				"description": "YARN 应用 ID（如 application_xxx）",
			},
			"reason": map[string]interface{}{
				"type":        "string",
				"description": "终止原因（写入审计日志）",
			},
		},
		"required": []string{"app_id", "reason"},
	}
}

func (t *KillApplicationTool) Execute(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
	appID, _ := params["app_id"].(string)
	reason, _ := params["reason"].(string)

	if appID == "" {
		return protocol.ErrorResult("app_id is required"), nil
	}
	if reason == "" {
		return protocol.ErrorResult("reason is required for audit trail"), nil
	}

	// Mock: 模拟终止操作
	data := map[string]interface{}{
		"action":     "kill_application",
		"app_id":     appID,
		"reason":     reason,
		"status":     "KILLED",
		"message":    fmt.Sprintf("Application %s has been killed. Reason: %s", appID, reason),
		"killed_at":  "2026-03-30T08:44:00Z",
		"freed_vcores":  16,
		"freed_memory_gb": 64,
	}

	b, _ := json.MarshalIndent(data, "", "  ")
	return protocol.TextResult(string(b)), nil
}
