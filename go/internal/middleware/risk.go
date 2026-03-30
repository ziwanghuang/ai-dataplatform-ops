package middleware

import (
	"context"
	"fmt"
	"strings"

	"github.com/rs/zerolog/log"
	"github.com/ziwang/aiops-mcp/internal/protocol"
)

// ────────────────────────────────────────────────────────────────────────────
// RiskAssessmentMiddleware — 风险评估中间件
// WHY 在中间件层做风险评估而不是在 Agent 层：
// - Defense-in-depth：即使 Agent 逻辑有 bug 跳过了风控，工具层仍然有兜底
// - 中间件可以访问工具元数据（RiskLevel）做统一判断
// - 高风险操作必须携带审批令牌，否则直接拒绝
//
// WHY 不直接用 RiskLevel 静态拒绝：
// - 同一工具不同参数风险不同（如 restart_service + graceful vs force）
// - 中间件可以做更细粒度的动态风险评估
// ────────────────────────────────────────────────────────────────────────────

// RiskConfig 风险评估配置
type RiskConfig struct {
	// EnforcementMode 执行模式：
	// - "enforce": 高风险操作没有审批令牌直接拒绝
	// - "audit": 只记录日志不拒绝（开发阶段用）
	EnforcementMode string `mapstructure:"enforcement_mode"`

	// HighRiskKeywords 高风险参数关键词
	HighRiskKeywords []string `mapstructure:"high_risk_keywords"`
}

// DefaultRiskConfig 默认风险配置
func DefaultRiskConfig() RiskConfig {
	return RiskConfig{
		EnforcementMode: "audit", // 开发阶段默认 audit 模式
		HighRiskKeywords: []string{
			"force",       // 强制操作
			"delete",      // 删除
			"drop",        // 删除
			"format",      // 格式化
			"truncate",    // 截断
			"decommission", // 退役节点
			"kill",        // 终止
		},
	}
}

// RiskAssessmentMiddleware 创建风险评估中间件
func RiskAssessmentMiddleware(tool protocol.Tool, cfg RiskConfig) Middleware {
	if len(cfg.HighRiskKeywords) == 0 {
		cfg = DefaultRiskConfig()
	}

	return func(next ToolHandler) ToolHandler {
		return func(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
			toolName := tool.Name()
			staticRisk := tool.RiskLevel()
			traceID, _ := ctx.Value(ctxKeyTraceID).(string)

			// ① 静态风险评估（基于工具声明的 RiskLevel）
			dynamicRisk := staticRisk

			// ② 动态风险评估（基于参数内容）
			paramRisk := assessParamRisk(params, cfg.HighRiskKeywords)
			if paramRisk > dynamicRisk {
				dynamicRisk = paramRisk
			}

			// ③ 检查是否携带审批令牌
			approvalToken, _ := ctx.Value(ctxKeyApprovalToken).(string)
			hasApproval := approvalToken != ""

			log.Info().
				Str("trace_id", traceID).
				Str("tool", toolName).
				Str("static_risk", staticRisk.String()).
				Str("dynamic_risk", dynamicRisk.String()).
				Bool("has_approval", hasApproval).
				Msg("risk_assessment")

			// ④ 风险决策
			if dynamicRisk >= protocol.RiskHigh && !hasApproval {
				if cfg.EnforcementMode == "enforce" {
					log.Warn().
						Str("trace_id", traceID).
						Str("tool", toolName).
						Str("risk", dynamicRisk.String()).
						Msg("risk_blocked: high risk operation without approval")

					return protocol.ErrorResult(
						fmt.Sprintf("⚠️ Operation blocked: tool '%s' has risk level '%s'. "+
							"Approval token required. Submit through HITL approval workflow.",
							toolName, dynamicRisk.String())), nil
				}
				// audit 模式：只记录不阻止
				log.Warn().
					Str("trace_id", traceID).
					Str("tool", toolName).
					Str("risk", dynamicRisk.String()).
					Str("mode", "audit").
					Msg("risk_audit: would block in enforce mode")
			}

			// 在 context 中传递风险等级（下游中间件/审计可用）
			ctx = context.WithValue(ctx, ctxKeyRiskLevel, dynamicRisk)

			return next(ctx, params)
		}
	}
}

// assessParamRisk 根据参数内容评估动态风险
func assessParamRisk(params map[string]interface{}, keywords []string) protocol.RiskLevel {
	risk := protocol.RiskNone

	for key, value := range params {
		strVal := fmt.Sprintf("%v", value)
		lowerKey := strings.ToLower(key)
		lowerVal := strings.ToLower(strVal)

		// 检查参数值是否包含高风险关键词
		for _, kw := range keywords {
			if strings.Contains(lowerVal, kw) || strings.Contains(lowerKey, kw) {
				if risk < protocol.RiskHigh {
					risk = protocol.RiskHigh
				}
			}
		}

		// 特殊检查：force restart 提升为 Critical
		if lowerKey == "restart_type" && lowerVal == "force" {
			risk = protocol.RiskCritical
		}

		// 特殊检查：批量操作（如影响多个节点）
		if lowerKey == "hosts" || lowerKey == "nodes" {
			if arr, ok := value.([]interface{}); ok && len(arr) > 3 {
				if risk < protocol.RiskHigh {
					risk = protocol.RiskHigh
				}
			}
		}
	}

	return risk
}
