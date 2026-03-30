package metrics

import (
	"context"
	"encoding/json"
	"fmt"
	"math"

	"github.com/ziwang/aiops-mcp/internal/protocol"
)

// ────────────────────────────────────────────────────────────────────────────
// QueryMetricsTool — Prometheus 指标查询
// WHY 集成 Prometheus 而不是直接查各组件 API：
// - Prometheus 是监控数据统一入口，所有组件指标汇聚于此
// - 支持 PromQL 提供灵活的聚合/计算能力（rate/avg_over_time 等）
// - Diagnostic Agent 可以构造 PromQL 验证假设
// ────────────────────────────────────────────────────────────────────────────

type QueryMetricsTool struct{}

func (t *QueryMetricsTool) Name() string        { return "query_metrics" }
func (t *QueryMetricsTool) Description() string  { return "查询 Prometheus 指标（支持 PromQL 即时查询和范围查询）" }
func (t *QueryMetricsTool) RiskLevel() protocol.RiskLevel { return protocol.RiskNone }
func (t *QueryMetricsTool) Schema() map[string]interface{} {
	return map[string]interface{}{
		"type": "object",
		"properties": map[string]interface{}{
			"query": map[string]interface{}{
				"type":        "string",
				"description": "PromQL 查询表达式",
			},
			"query_type": map[string]interface{}{
				"type":        "string",
				"description": "查询类型：instant（即时）或 range（范围）",
				"default":     "instant",
				"enum":        []string{"instant", "range"},
			},
			"time_range": map[string]interface{}{
				"type":        "string",
				"description": "范围查询时间跨度：15m, 1h, 6h, 24h",
				"default":     "1h",
			},
			"step": map[string]interface{}{
				"type":        "string",
				"description": "范围查询步长：15s, 1m, 5m",
				"default":     "1m",
			},
		},
		"required": []string{"query"},
	}
}

func (t *QueryMetricsTool) Execute(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
	query, _ := params["query"].(string)
	queryType, _ := params["query_type"].(string)
	if queryType == "" {
		queryType = "instant"
	}

	if query == "" {
		return protocol.ErrorResult("query is required"), nil
	}

	// Mock: 根据查询关键词返回合理的模拟数据
	var data interface{}

	switch {
	case containsAny(query, "cpu", "node_cpu"):
		data = mockCPUMetrics(query, queryType)
	case containsAny(query, "memory", "node_memory", "jvm_heap"):
		data = mockMemoryMetrics(query, queryType)
	case containsAny(query, "disk", "node_filesystem"):
		data = mockDiskMetrics(query, queryType)
	case containsAny(query, "network", "node_network"):
		data = mockNetworkMetrics(query, queryType)
	case containsAny(query, "kafka", "consumer_lag"):
		data = mockKafkaMetrics(query, queryType)
	case containsAny(query, "hdfs", "namenode"):
		data = mockHDFSMetrics(query, queryType)
	default:
		data = mockGenericMetrics(query, queryType)
	}

	b, _ := json.MarshalIndent(data, "", "  ")
	return protocol.TextResult(string(b)), nil
}

// containsAny 检查 query 是否包含任一关键词
func containsAny(query string, keywords ...string) bool {
	for _, kw := range keywords {
		if containsIgnoreCase(query, kw) {
			return true
		}
	}
	return false
}

func containsIgnoreCase(s, substr string) bool {
	for i := 0; i+len(substr) <= len(s); i++ {
		match := true
		for j := 0; j < len(substr); j++ {
			c1 := s[i+j]
			c2 := substr[j]
			if c1 >= 'A' && c1 <= 'Z' {
				c1 += 32
			}
			if c2 >= 'A' && c2 <= 'Z' {
				c2 += 32
			}
			if c1 != c2 {
				match = false
				break
			}
		}
		if match {
			return true
		}
	}
	return false
}

func mockCPUMetrics(query, queryType string) map[string]interface{} {
	if queryType == "range" {
		return map[string]interface{}{
			"status": "success",
			"query":  query,
			"data": map[string]interface{}{
				"resultType": "matrix",
				"result": []map[string]interface{}{
					{
						"metric": map[string]string{"instance": "node-01:9100", "mode": "idle"},
						"values": generateTimeSeries(12, 25.0, 35.0),
					},
					{
						"metric": map[string]string{"instance": "node-02:9100", "mode": "idle"},
						"values": generateTimeSeries(12, 15.0, 85.0), // ⚠️ CPU spike
					},
				},
			},
		}
	}
	return map[string]interface{}{
		"status": "success",
		"query":  query,
		"data": map[string]interface{}{
			"resultType": "vector",
			"result": []map[string]interface{}{
				{"metric": map[string]string{"instance": "node-01:9100"}, "value": []interface{}{1711782240, "72.5"}},
				{"metric": map[string]string{"instance": "node-02:9100"}, "value": []interface{}{1711782240, "89.3"}},
				{"metric": map[string]string{"instance": "node-03:9100"}, "value": []interface{}{1711782240, "45.1"}},
			},
		},
	}
}

func mockMemoryMetrics(query, queryType string) map[string]interface{} {
	return map[string]interface{}{
		"status": "success",
		"query":  query,
		"data": map[string]interface{}{
			"resultType": "vector",
			"result": []map[string]interface{}{
				{"metric": map[string]string{"instance": "node-01:9100"}, "value": []interface{}{1711782240, "68.2"}},
				{"metric": map[string]string{"instance": "node-02:9100"}, "value": []interface{}{1711782240, "91.7"}}, // ⚠️
				{"metric": map[string]string{"instance": "node-03:9100"}, "value": []interface{}{1711782240, "55.0"}},
			},
		},
	}
}

func mockDiskMetrics(query, queryType string) map[string]interface{} {
	return map[string]interface{}{
		"status": "success",
		"query":  query,
		"data": map[string]interface{}{
			"resultType": "vector",
			"result": []map[string]interface{}{
				{"metric": map[string]string{"instance": "node-01:9100", "mountpoint": "/data"}, "value": []interface{}{1711782240, "72.0"}},
				{"metric": map[string]string{"instance": "node-02:9100", "mountpoint": "/data"}, "value": []interface{}{1711782240, "93.5"}},
				{"metric": map[string]string{"instance": "node-03:9100", "mountpoint": "/data"}, "value": []interface{}{1711782240, "58.0"}},
			},
		},
	}
}

func mockNetworkMetrics(query, queryType string) map[string]interface{} {
	return map[string]interface{}{
		"status": "success",
		"query":  query,
		"data": map[string]interface{}{
			"resultType": "vector",
			"result": []map[string]interface{}{
				{"metric": map[string]string{"instance": "node-01:9100", "device": "eth0"}, "value": []interface{}{1711782240, "125000000"}},
				{"metric": map[string]string{"instance": "node-02:9100", "device": "eth0"}, "value": []interface{}{1711782240, "890000000"}},
			},
		},
	}
}

func mockKafkaMetrics(query, queryType string) map[string]interface{} {
	return map[string]interface{}{
		"status": "success",
		"query":  query,
		"data": map[string]interface{}{
			"resultType": "vector",
			"result": []map[string]interface{}{
				{"metric": map[string]string{"topic": "events", "group": "etl-pipeline"}, "value": []interface{}{1711782240, "15200"}},
				{"metric": map[string]string{"topic": "events", "group": "realtime-analytics"}, "value": []interface{}{1711782240, "25"}},
				{"metric": map[string]string{"topic": "logs", "group": "log-archiver"}, "value": []interface{}{1711782240, "500"}},
			},
		},
	}
}

func mockHDFSMetrics(query, queryType string) map[string]interface{} {
	return map[string]interface{}{
		"status": "success",
		"query":  query,
		"data": map[string]interface{}{
			"resultType": "vector",
			"result": []map[string]interface{}{
				{"metric": map[string]string{"name": "nn1"}, "value": []interface{}{1711782240, "2048"}},
				{"metric": map[string]string{"name": "nn2"}, "value": []interface{}{1711782240, "1024"}},
			},
		},
	}
}

func mockGenericMetrics(query, queryType string) map[string]interface{} {
	return map[string]interface{}{
		"status": "success",
		"query":  query,
		"data": map[string]interface{}{
			"resultType": "vector",
			"result": []map[string]interface{}{
				{"metric": map[string]string{"job": "unknown"}, "value": []interface{}{1711782240, "42.0"}},
			},
		},
		"warning": fmt.Sprintf("Mock data for unrecognized query: %s", query),
	}
}

// generateTimeSeries 生成模拟时序数据（用于 range 查询）
func generateTimeSeries(points int, baseVal, peakVal float64) [][]interface{} {
	result := make([][]interface{}, points)
	baseTime := 1711778640 // 固定基准时间
	for i := 0; i < points; i++ {
		ts := baseTime + i*300 // 5 分钟间隔
		// 正弦波模拟负载波动
		ratio := math.Sin(float64(i) / float64(points) * math.Pi)
		val := baseVal + (peakVal-baseVal)*ratio
		result[i] = []interface{}{ts, fmt.Sprintf("%.2f", val)}
	}
	return result
}

// ────────────────────────────────────────────────────────────────────────────
// AlertQueryTool — 查询当前活跃的 Prometheus 告警
// WHY 独立于 query_metrics：
// - 告警有自己的数据结构（labels/annotations/state/activeAt）
// - AlertCorrelation Agent 直接消费告警列表
// ────────────────────────────────────────────────────────────────────────────

type AlertQueryTool struct{}

func (t *AlertQueryTool) Name() string        { return "query_active_alerts" }
func (t *AlertQueryTool) Description() string  { return "查询当前活跃的 Prometheus 告警规则" }
func (t *AlertQueryTool) RiskLevel() protocol.RiskLevel { return protocol.RiskNone }
func (t *AlertQueryTool) Schema() map[string]interface{} {
	return map[string]interface{}{
		"type": "object",
		"properties": map[string]interface{}{
			"severity": map[string]interface{}{
				"type":        "string",
				"description": "按严重程度过滤：critical, warning, info",
			},
			"component": map[string]interface{}{
				"type":        "string",
				"description": "按组件过滤：hdfs, kafka, yarn, es",
			},
		},
	}
}

func (t *AlertQueryTool) Execute(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
	severity, _ := params["severity"].(string)
	component, _ := params["component"].(string)

	alerts := []map[string]interface{}{
		{
			"alert_name":   "HDFSNamenodeHeapHigh",
			"severity":     "warning",
			"component":    "hdfs",
			"state":        "firing",
			"active_since": "2026-03-30T07:15:00Z",
			"labels":       map[string]string{"namenode": "nn1", "instance": "hdfs-nn1:9870"},
			"annotations":  map[string]string{"summary": "NameNode heap usage above 80%", "value": "87.5%"},
		},
		{
			"alert_name":   "ESNodeDiskHigh",
			"severity":     "critical",
			"component":    "es",
			"state":        "firing",
			"active_since": "2026-03-30T06:30:00Z",
			"labels":       map[string]string{"node": "es-data-02", "instance": "es-data-02:9200"},
			"annotations":  map[string]string{"summary": "ES data node disk usage above 90%", "value": "93.0%"},
		},
		{
			"alert_name":   "KafkaConsumerLagHigh",
			"severity":     "warning",
			"component":    "kafka",
			"state":        "firing",
			"active_since": "2026-03-30T08:00:00Z",
			"labels":       map[string]string{"group": "etl-pipeline", "topic": "events"},
			"annotations":  map[string]string{"summary": "Consumer group lag exceeds threshold", "value": "15200"},
		},
		{
			"alert_name":   "YARNQueueNearCapacity",
			"severity":     "warning",
			"component":    "yarn",
			"state":        "firing",
			"active_since": "2026-03-30T07:45:00Z",
			"labels":       map[string]string{"queue": "root.production"},
			"annotations":  map[string]string{"summary": "YARN queue usage above 90% of max capacity", "value": "92.2%"},
		},
	}

	// 简单过滤
	var filtered []map[string]interface{}
	for _, a := range alerts {
		if severity != "" && a["severity"] != severity {
			continue
		}
		if component != "" && a["component"] != component {
			continue
		}
		filtered = append(filtered, a)
	}
	if filtered == nil {
		filtered = []map[string]interface{}{}
	}

	result := map[string]interface{}{
		"total_firing": len(filtered),
		"filters":      map[string]string{"severity": severity, "component": component},
		"alerts":       filtered,
	}

	b, _ := json.MarshalIndent(result, "", "  ")
	return protocol.TextResult(string(b)), nil
}
