package ops

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"github.com/ziwang/aiops-mcp/internal/protocol"
)

// ────────────────────────────────────────────────────────────────────────────
// RestartServiceTool — 重启服务（高风险）
// WHY RiskLevel=Critical:
// - 重启生产服务可能导致短暂不可用
// - NameNode 重启尤其危险（HA 切换、safemode 等待）
// - 必须经过 HITL 双人审批才能执行
// - Remediation Agent 的最终手段
// ────────────────────────────────────────────────────────────────────────────

type RestartServiceTool struct{}

func (t *RestartServiceTool) Name() string        { return "restart_service" }
func (t *RestartServiceTool) Description() string  { return "重启大数据组件服务（Critical：需要双人审批）" }
func (t *RestartServiceTool) RiskLevel() protocol.RiskLevel { return protocol.RiskCritical }
func (t *RestartServiceTool) Schema() map[string]interface{} {
	return map[string]interface{}{
		"type": "object",
		"properties": map[string]interface{}{
			"service": map[string]interface{}{
				"type":        "string",
				"description": "服务名称",
				"enum":        []string{"hdfs-namenode", "hdfs-datanode", "yarn-rm", "yarn-nm", "kafka-broker", "es-node", "hive-metastore", "hive-server2", "zookeeper"},
			},
			"host": map[string]interface{}{
				"type":        "string",
				"description": "目标主机",
			},
			"restart_type": map[string]interface{}{
				"type":        "string",
				"description": "重启类型：graceful（优雅）/ force（强制）",
				"default":     "graceful",
				"enum":        []string{"graceful", "force"},
			},
			"reason": map[string]interface{}{
				"type":        "string",
				"description": "重启原因（审计必填）",
			},
			"pre_check": map[string]interface{}{
				"type":        "boolean",
				"description": "是否执行前置检查（默认 true）",
				"default":     true,
			},
		},
		"required": []string{"service", "host", "reason"},
	}
}

func (t *RestartServiceTool) Execute(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
	service, _ := params["service"].(string)
	host, _ := params["host"].(string)
	restartType, _ := params["restart_type"].(string)
	reason, _ := params["reason"].(string)

	if service == "" || host == "" {
		return protocol.ErrorResult("service and host are required"), nil
	}
	if reason == "" {
		return protocol.ErrorResult("reason is required for audit trail"), nil
	}
	if restartType == "" {
		restartType = "graceful"
	}

	// Mock: 模拟重启执行过程
	now := time.Now().UTC().Format(time.RFC3339)

	preChecks := []map[string]interface{}{
		{"check": "service_exists", "status": "passed", "detail": fmt.Sprintf("Service %s found on %s", service, host)},
		{"check": "ha_state", "status": "passed", "detail": "HA standby available, failover safe"},
		{"check": "no_critical_tasks", "status": "passed", "detail": "No critical long-running tasks on this node"},
		{"check": "disk_space", "status": "passed", "detail": "Sufficient disk space for logs"},
	}

	// 模拟执行步骤
	steps := []map[string]interface{}{
		{"step": 1, "action": "pre_check", "status": "completed", "duration_s": 2.1},
		{"step": 2, "action": "drain_connections", "status": "completed", "duration_s": 5.3},
		{"step": 3, "action": "stop_service", "status": "completed", "duration_s": 8.7},
		{"step": 4, "action": "wait_for_shutdown", "status": "completed", "duration_s": 3.2},
		{"step": 5, "action": "start_service", "status": "completed", "duration_s": 12.5},
		{"step": 6, "action": "health_check", "status": "completed", "duration_s": 4.8},
	}

	totalDuration := 0.0
	for _, s := range steps {
		totalDuration += s["duration_s"].(float64)
	}

	data := map[string]interface{}{
		"action":           "restart_service",
		"service":          service,
		"host":             host,
		"restart_type":     restartType,
		"reason":           reason,
		"status":           "success",
		"started_at":       now,
		"total_duration_s": totalDuration,
		"pre_checks":       preChecks,
		"execution_steps":  steps,
		"post_status": map[string]interface{}{
			"service_state": "RUNNING",
			"health":        "healthy",
			"uptime_s":      4.8,
			"pid":           42195,
		},
		"rollback_available": true,
		"rollback_command":   fmt.Sprintf("ops rollback restart --service %s --host %s", service, host),
	}

	b, _ := json.MarshalIndent(data, "", "  ")
	return protocol.TextResult(string(b)), nil
}

// ────────────────────────────────────────────────────────────────────────────
// ScaleResourceTool — 资源扩缩容
// WHY RiskLevel=High:
// - 扩容一般安全，但缩容可能影响运行中的任务
// - 需要单人审批
// - 常见场景：YARN 队列扩容、Kafka 分区数调整
// ────────────────────────────────────────────────────────────────────────────

type ScaleResourceTool struct{}

func (t *ScaleResourceTool) Name() string        { return "scale_resource" }
func (t *ScaleResourceTool) Description() string  { return "资源扩缩容操作（High：需要审批）" }
func (t *ScaleResourceTool) RiskLevel() protocol.RiskLevel { return protocol.RiskHigh }
func (t *ScaleResourceTool) Schema() map[string]interface{} {
	return map[string]interface{}{
		"type": "object",
		"properties": map[string]interface{}{
			"resource_type": map[string]interface{}{
				"type":        "string",
				"description": "资源类型",
				"enum":        []string{"yarn_queue_capacity", "kafka_partitions", "es_replicas", "hdfs_replication"},
			},
			"target": map[string]interface{}{
				"type":        "string",
				"description": "目标（如队列名、topic 名、索引名）",
			},
			"current_value": map[string]interface{}{
				"type":        "number",
				"description": "当前值",
			},
			"desired_value": map[string]interface{}{
				"type":        "number",
				"description": "目标值",
			},
			"reason": map[string]interface{}{
				"type":        "string",
				"description": "变更原因",
			},
		},
		"required": []string{"resource_type", "target", "desired_value", "reason"},
	}
}

func (t *ScaleResourceTool) Execute(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
	resourceType, _ := params["resource_type"].(string)
	target, _ := params["target"].(string)
	desiredValue, _ := params["desired_value"].(float64)
	currentValue, _ := params["current_value"].(float64)
	reason, _ := params["reason"].(string)

	if resourceType == "" || target == "" || reason == "" {
		return protocol.ErrorResult("resource_type, target, and reason are required"), nil
	}

	direction := "scale_up"
	if desiredValue < currentValue {
		direction = "scale_down"
	}

	data := map[string]interface{}{
		"action":        "scale_resource",
		"resource_type": resourceType,
		"target":        target,
		"current_value": currentValue,
		"desired_value": desiredValue,
		"direction":     direction,
		"reason":        reason,
		"status":        "success",
		"applied_at":    time.Now().UTC().Format(time.RFC3339),
		"effective_in":  "30s",
		"notes": func() string {
			if direction == "scale_down" {
				return "⚠️ Scale down operation. Running tasks may be affected."
			}
			return "Scale up operation applied. Resources will be available shortly."
		}(),
		"rollback_available": true,
		"rollback_value":     currentValue,
	}

	b, _ := json.MarshalIndent(data, "", "  ")
	return protocol.TextResult(string(b)), nil
}

// ────────────────────────────────────────────────────────────────────────────
// RollbackConfigTool — 配置回滚
// WHY RiskLevel=High:
// - 回滚到基线配置本身也是配置变更
// - 某些配置变更需要重启才能生效
// - 需要审批确认
// ────────────────────────────────────────────────────────────────────────────

type RollbackConfigTool struct{}

func (t *RollbackConfigTool) Name() string        { return "rollback_config" }
func (t *RollbackConfigTool) Description() string  { return "回滚组件配置到基线版本（High：需要审批）" }
func (t *RollbackConfigTool) RiskLevel() protocol.RiskLevel { return protocol.RiskHigh }
func (t *RollbackConfigTool) Schema() map[string]interface{} {
	return map[string]interface{}{
		"type": "object",
		"properties": map[string]interface{}{
			"component": map[string]interface{}{
				"type":        "string",
				"description": "组件名称",
				"enum":        []string{"hdfs", "yarn", "kafka", "es"},
			},
			"config_keys": map[string]interface{}{
				"type":        "array",
				"description": "要回滚的配置 key 列表（空表示全部回滚）",
				"items":       map[string]interface{}{"type": "string"},
			},
			"baseline": map[string]interface{}{
				"type":        "string",
				"description": "回滚目标基线",
				"default":     "last_stable",
			},
			"requires_restart": map[string]interface{}{
				"type":        "boolean",
				"description": "回滚后是否需要重启",
				"default":     false,
			},
			"reason": map[string]interface{}{
				"type":        "string",
				"description": "回滚原因",
			},
		},
		"required": []string{"component", "reason"},
	}
}

func (t *RollbackConfigTool) Execute(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
	component, _ := params["component"].(string)
	reason, _ := params["reason"].(string)
	baseline, _ := params["baseline"].(string)
	requiresRestart, _ := params["requires_restart"].(bool)

	if component == "" || reason == "" {
		return protocol.ErrorResult("component and reason are required"), nil
	}
	if baseline == "" {
		baseline = "last_stable"
	}

	// Mock: 模拟配置回滚
	rolledBackKeys := []map[string]interface{}{}

	switch component {
	case "hdfs":
		rolledBackKeys = []map[string]interface{}{
			{"key": "dfs.namenode.heap_size_mb", "from": 4096, "to": 8192},
			{"key": "dfs.namenode.handler.count", "from": 100, "to": 200},
		}
	case "es":
		rolledBackKeys = []map[string]interface{}{
			{"key": "ES_JAVA_OPTS", "from": "-Xms8g -Xmx8g", "to": "-Xms16g -Xmx16g"},
			{"key": "thread_pool.write.queue_size", "from": 200, "to": 500},
		}
	default:
		rolledBackKeys = []map[string]interface{}{
			{"key": "example.config", "from": "current", "to": "baseline"},
		}
	}

	data := map[string]interface{}{
		"action":           "rollback_config",
		"component":        component,
		"baseline":         baseline,
		"reason":           reason,
		"status":           "success",
		"rolled_back_keys": rolledBackKeys,
		"total_changes":    len(rolledBackKeys),
		"applied_at":       time.Now().UTC().Format(time.RFC3339),
		"requires_restart": requiresRestart,
		"restart_note": func() string {
			if requiresRestart {
				return "Configuration requires service restart to take effect. Use restart_service tool."
			}
			return "Configuration applied dynamically, no restart needed."
		}(),
	}

	b, _ := json.MarshalIndent(data, "", "  ")
	return protocol.TextResult(string(b)), nil
}
