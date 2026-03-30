package config

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"

	"github.com/ziwang/aiops-mcp/internal/protocol"
)

// ────────────────────────────────────────────────────────────────────────────
// GetConfigTool — 获取组件配置
// WHY 配置查询是诊断基础操作：
// - 很多故障根因是配置变更（如 Heap 调小、副本数不对）
// - Diagnostic Agent 需要当前配置来验证假设
// - RiskLevel=Low（只读），但返回数据可能含敏感信息 → 脱敏处理
// ────────────────────────────────────────────────────────────────────────────

type GetConfigTool struct{}

func (t *GetConfigTool) Name() string        { return "get_config" }
func (t *GetConfigTool) Description() string  { return "获取大数据组件运行时配置（脱敏后）" }
func (t *GetConfigTool) RiskLevel() protocol.RiskLevel { return protocol.RiskLow }
func (t *GetConfigTool) Schema() map[string]interface{} {
	return map[string]interface{}{
		"type": "object",
		"properties": map[string]interface{}{
			"component": map[string]interface{}{
				"type":        "string",
				"description": "组件名称：hdfs, yarn, kafka, es, hive",
				"enum":        []string{"hdfs", "yarn", "kafka", "es", "hive"},
			},
			"config_type": map[string]interface{}{
				"type":        "string",
				"description": "配置类型：core-site, hdfs-site, yarn-site, kafka-broker, es-cluster",
				"default":     "all",
			},
			"key_filter": map[string]interface{}{
				"type":        "string",
				"description": "配置 key 过滤（支持前缀匹配，如 dfs.namenode）",
			},
		},
		"required": []string{"component"},
	}
}

func (t *GetConfigTool) Execute(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
	component, _ := params["component"].(string)
	keyFilter, _ := params["key_filter"].(string)

	if component == "" {
		return protocol.ErrorResult("component is required"), nil
	}

	configs := getComponentConfig(component)
	if configs == nil {
		return protocol.ErrorResult(fmt.Sprintf("unknown component: %s", component)), nil
	}

	// Key 过滤
	if keyFilter != "" {
		filtered := make(map[string]interface{})
		for k, v := range configs {
			if strings.HasPrefix(k, keyFilter) {
				filtered[k] = v
			}
		}
		configs = filtered
	}

	result := map[string]interface{}{
		"component":    component,
		"config_count": len(configs),
		"configs":      configs,
		"note":         "Sensitive values (passwords, keys) are redacted with ***",
	}

	b, _ := json.MarshalIndent(result, "", "  ")
	return protocol.TextResult(string(b)), nil
}

func getComponentConfig(component string) map[string]interface{} {
	switch component {
	case "hdfs":
		return map[string]interface{}{
			"dfs.namenode.name.dir":                    "/data/nn",
			"dfs.replication":                          3,
			"dfs.blocksize":                            134217728, // 128MB
			"dfs.namenode.handler.count":               100,
			"dfs.namenode.rpc-address.nn1":             "hdfs-nn1:8020",
			"dfs.namenode.rpc-address.nn2":             "hdfs-nn2:8020",
			"dfs.namenode.http-address.nn1":            "hdfs-nn1:9870",
			"dfs.namenode.http-address.nn2":            "hdfs-nn2:9870",
			"dfs.ha.automatic-failover.enabled":        true,
			"dfs.namenode.heap_size_mb":                4096,
			"dfs.datanode.max.transfer.threads":        4096,
			"dfs.datanode.balance.bandwidthPerSec":     104857600, // 100MB/s
			"dfs.namenode.checkpoint.period":           3600,
			"dfs.namenode.safemode.threshold-pct":      0.999,
			"dfs.permissions.superusergroup":           "hdfs",
			"dfs.namenode.audit.log.async":             true,
			"dfs.client.failover.proxy.provider":       "org.apache.hadoop.hdfs.server.namenode.ha.ConfiguredFailoverProxyProvider",
		}
	case "yarn":
		return map[string]interface{}{
			"yarn.resourcemanager.ha.enabled":           true,
			"yarn.resourcemanager.ha.rm-ids":            "rm1,rm2",
			"yarn.nodemanager.resource.memory-mb":       98304,
			"yarn.nodemanager.resource.cpu-vcores":      24,
			"yarn.scheduler.capacity.root.queues":       "production,development,default",
			"yarn.scheduler.capacity.root.production.capacity":   60,
			"yarn.scheduler.capacity.root.production.max-capacity": 80,
			"yarn.scheduler.capacity.root.development.capacity":   30,
			"yarn.scheduler.capacity.root.development.max-capacity": 50,
			"yarn.scheduler.capacity.root.default.capacity":       10,
			"yarn.scheduler.minimum-allocation-mb":     1024,
			"yarn.scheduler.maximum-allocation-mb":     32768,
			"yarn.scheduler.minimum-allocation-vcores": 1,
			"yarn.scheduler.maximum-allocation-vcores": 8,
			"yarn.nodemanager.aux-services":            "mapreduce_shuffle,spark_shuffle",
			"yarn.log-aggregation-enable":              true,
			"yarn.log-aggregation.retain-seconds":      604800, // 7 days
		}
	case "kafka":
		return map[string]interface{}{
			"broker.id":                       1,
			"num.network.threads":             8,
			"num.io.threads":                  16,
			"num.partitions":                  12,
			"default.replication.factor":       3,
			"min.insync.replicas":             2,
			"log.retention.hours":             168, // 7 days
			"log.retention.bytes":             -1,
			"log.segment.bytes":              1073741824, // 1GB
			"auto.create.topics.enable":      false,
			"unclean.leader.election.enable":  false,
			"message.max.bytes":              10485760,   // 10MB
			"replica.fetch.max.bytes":        10485760,
			"group.initial.rebalance.delay.ms": 3000,
			"inter.broker.protocol.version":   "3.6",
			"log.dirs":                        "/data/kafka-logs",
			"zookeeper.connect":              "zk1:2181,zk2:2181,zk3:2181/kafka",
			"sasl.mechanism.inter.broker":     "PLAIN",
			"sasl.jaas.config":               "***REDACTED***",
		}
	case "es":
		return map[string]interface{}{
			"cluster.name":                       "ops-es-cluster",
			"node.name":                          "${HOSTNAME}",
			"path.data":                          "/data/elasticsearch",
			"path.logs":                          "/var/log/elasticsearch",
			"network.host":                       "0.0.0.0",
			"http.port":                          9200,
			"discovery.seed_hosts":               []string{"es-01", "es-02", "es-03"},
			"cluster.initial_master_nodes":       []string{"es-01", "es-02", "es-03"},
			"xpack.security.enabled":             true,
			"xpack.security.transport.ssl.enabled": true,
			"xpack.monitoring.collection.enabled":  true,
			"indices.memory.index_buffer_size":     "10%",
			"thread_pool.write.queue_size":         200,
			"thread_pool.search.queue_size":        1000,
			"action.destructive_requires_name":     true,
			"ES_JAVA_OPTS":                         "-Xms8g -Xmx8g",
		}
	case "hive":
		return map[string]interface{}{
			"hive.metastore.uris":                 "thrift://hive-metastore:9083",
			"hive.metastore.warehouse.dir":         "/user/hive/warehouse",
			"hive.exec.dynamic.partition.mode":     "nonstrict",
			"hive.exec.max.dynamic.partitions":     10000,
			"hive.exec.parallel":                   true,
			"hive.exec.parallel.thread.number":     8,
			"hive.tez.container.size":              4096,
			"hive.tez.java.opts":                   "-Xmx3276m",
			"hive.vectorized.execution.enabled":    true,
			"hive.server2.thrift.port":             10000,
			"javax.jdo.option.ConnectionURL":       "jdbc:mysql://metastore-db:3306/hive_meta",
			"javax.jdo.option.ConnectionPassword":  "***REDACTED***",
			"javax.jdo.option.ConnectionUserName":  "hive",
		}
	default:
		return nil
	}
}

// ────────────────────────────────────────────────────────────────────────────
// DiffConfigTool — 配置对比（当前 vs 基线）
// WHY：
// - 故障排查核心问题："最近改了什么？"
// - 对比当前运行时配置和基线配置，高亮差异
// - RiskLevel=Low（只读比对）
// ────────────────────────────────────────────────────────────────────────────

type DiffConfigTool struct{}

func (t *DiffConfigTool) Name() string        { return "diff_config" }
func (t *DiffConfigTool) Description() string  { return "对比组件当前配置与基线配置，显示差异项" }
func (t *DiffConfigTool) RiskLevel() protocol.RiskLevel { return protocol.RiskLow }
func (t *DiffConfigTool) Schema() map[string]interface{} {
	return map[string]interface{}{
		"type": "object",
		"properties": map[string]interface{}{
			"component": map[string]interface{}{
				"type":        "string",
				"description": "组件名称：hdfs, yarn, kafka, es",
				"enum":        []string{"hdfs", "yarn", "kafka", "es"},
			},
			"baseline": map[string]interface{}{
				"type":        "string",
				"description": "基线标识：last_stable（上次稳定版）或日期标签",
				"default":     "last_stable",
			},
		},
		"required": []string{"component"},
	}
}

func (t *DiffConfigTool) Execute(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
	component, _ := params["component"].(string)
	baseline, _ := params["baseline"].(string)
	if baseline == "" {
		baseline = "last_stable"
	}

	if component == "" {
		return protocol.ErrorResult("component is required"), nil
	}

	// Mock: 模拟配置漂移场景
	diffs := getConfigDiffs(component)

	result := map[string]interface{}{
		"component":    component,
		"baseline":     baseline,
		"baseline_date": "2026-03-25T00:00:00Z",
		"current_date":  "2026-03-30T08:44:00Z",
		"total_changes": len(diffs),
		"changes":       diffs,
		"risk_assessment": assessDiffRisk(diffs),
	}

	b, _ := json.MarshalIndent(result, "", "  ")
	return protocol.TextResult(string(b)), nil
}

func getConfigDiffs(component string) []map[string]interface{} {
	switch component {
	case "hdfs":
		return []map[string]interface{}{
			{
				"key":            "dfs.namenode.heap_size_mb",
				"baseline_value": 8192,
				"current_value":  4096,
				"change_type":    "modified",
				"risk":           "high",
				"note":           "Heap 减半可能导致 GC 压力增大",
			},
			{
				"key":            "dfs.namenode.handler.count",
				"baseline_value": 200,
				"current_value":  100,
				"change_type":    "modified",
				"risk":           "medium",
				"note":           "Handler 数减少可能导致 RPC 排队",
			},
		}
	case "kafka":
		return []map[string]interface{}{
			{
				"key":            "num.io.threads",
				"baseline_value": 16,
				"current_value":  8,
				"change_type":    "modified",
				"risk":           "medium",
				"note":           "IO 线程减半",
			},
			{
				"key":            "log.retention.hours",
				"baseline_value": 168,
				"current_value":  72,
				"change_type":    "modified",
				"risk":           "low",
				"note":           "日志保留从 7 天缩减为 3 天",
			},
		}
	case "yarn":
		return []map[string]interface{}{
			{
				"key":            "yarn.scheduler.capacity.root.production.max-capacity",
				"baseline_value": 90,
				"current_value":  80,
				"change_type":    "modified",
				"risk":           "medium",
				"note":           "Production 队列最大容量下调",
			},
		}
	case "es":
		return []map[string]interface{}{
			{
				"key":            "ES_JAVA_OPTS",
				"baseline_value": "-Xms16g -Xmx16g",
				"current_value":  "-Xms8g -Xmx8g",
				"change_type":    "modified",
				"risk":           "high",
				"note":           "JVM Heap 减半，可能导致频繁 GC",
			},
			{
				"key":            "thread_pool.write.queue_size",
				"baseline_value": 500,
				"current_value":  200,
				"change_type":    "modified",
				"risk":           "medium",
				"note":           "写入队列缩短，高峰期可能拒绝请求",
			},
		}
	default:
		return []map[string]interface{}{}
	}
}

func assessDiffRisk(diffs []map[string]interface{}) map[string]interface{} {
	high, medium, low := 0, 0, 0
	for _, d := range diffs {
		switch d["risk"] {
		case "high":
			high++
		case "medium":
			medium++
		case "low":
			low++
		}
	}
	overallRisk := "low"
	if high > 0 {
		overallRisk = "high"
	} else if medium > 0 {
		overallRisk = "medium"
	}
	return map[string]interface{}{
		"overall_risk":  overallRisk,
		"high_changes":  high,
		"medium_changes": medium,
		"low_changes":   low,
		"recommendation": func() string {
			if high > 0 {
				return "建议立即回滚高风险配置变更"
			}
			if medium > 0 {
				return "建议关注中风险变更，评估是否需要回滚"
			}
			return "配置变更风险可控"
		}(),
	}
}
