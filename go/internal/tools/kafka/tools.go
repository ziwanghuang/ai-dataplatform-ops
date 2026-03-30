package kafka

import (
	"context"
	"encoding/json"

	"github.com/ziwang/aiops-mcp/internal/protocol"
)

// ConsumerLagTool Kafka 消费者组 Lag 查询
type ConsumerLagTool struct{}

func (t *ConsumerLagTool) Name() string        { return "kafka_consumer_lag" }
func (t *ConsumerLagTool) Description() string  { return "消费者组 Lag：各分区 offset/committed/lag" }
func (t *ConsumerLagTool) RiskLevel() protocol.RiskLevel { return protocol.RiskNone }
func (t *ConsumerLagTool) Schema() map[string]interface{} {
	return map[string]interface{}{
		"type": "object",
		"properties": map[string]interface{}{
			"group": map[string]interface{}{
				"type":        "string",
				"description": "消费者组 ID",
			},
		},
		"required": []string{"group"},
	}
}

func (t *ConsumerLagTool) Execute(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
	group, _ := params["group"].(string)
	if group == "" {
		group = "default-consumer-group"
	}

	data := map[string]interface{}{
		"group": group,
		"state": "Stable",
		"partitions": []map[string]interface{}{
			{"topic": "events", "partition": 0, "current_offset": 1500000, "end_offset": 1500020, "lag": 20},
			{"topic": "events", "partition": 1, "current_offset": 1480000, "end_offset": 1480005, "lag": 5},
			{"topic": "events", "partition": 2, "current_offset": 1520000, "end_offset": 1520000, "lag": 0},
		},
		"total_lag": 25,
	}

	b, _ := json.MarshalIndent(data, "", "  ")
	return protocol.TextResult(string(b)), nil
}
