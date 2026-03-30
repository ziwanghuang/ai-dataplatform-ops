package middleware

import (
	"context"
	"fmt"
	"strings"

	"github.com/rs/zerolog/log"
	"github.com/ziwang/aiops-mcp/internal/pkg/errors"
	"github.com/ziwang/aiops-mcp/internal/protocol"
)

// ────────────────────────────────────────────────────────────────────────────
// ValidationMiddleware — 参数校验中间件
// WHY 在中间件层而不是在每个工具内部校验：
// - DRY 原则：required/type/enum/range 等通用校验不需要每个工具重复实现
// - Schema 驱动：利用工具注册时声明的 JSON Schema 做自动校验
// - 安全兜底：防止 LLM 生成的参数绕过工具校验
//
// 校验层次：
// 1. required 字段是否存在
// 2. 字段类型是否匹配
// 3. enum 值是否在允许范围内
// 4. 注入检测（参数值不应包含特殊字符）
// ────────────────────────────────────────────────────────────────────────────

// ValidationConfig 校验配置
type ValidationConfig struct {
	// StrictMode 严格模式：拒绝 Schema 中未声明的额外参数
	StrictMode bool `mapstructure:"strict_mode"`

	// MaxParamLength 单个参数值最大长度（防止注入攻击）
	MaxParamLength int `mapstructure:"max_param_length"`

	// InjectionDetection 是否启用注入检测
	InjectionDetection bool `mapstructure:"injection_detection"`
}

// DefaultValidationConfig 默认校验配置
func DefaultValidationConfig() ValidationConfig {
	return ValidationConfig{
		StrictMode:         false,
		MaxParamLength:     4096,
		InjectionDetection: true,
	}
}

// ValidationMiddleware 创建参数校验中间件
func ValidationMiddleware(tool protocol.Tool, cfg ValidationConfig) Middleware {
	schema := tool.Schema()
	toolName := tool.Name()

	return func(next ToolHandler) ToolHandler {
		return func(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
			traceID, _ := ctx.Value(ctxKeyTraceID).(string)

			// ① required 字段校验
			if err := validateRequired(schema, params); err != nil {
				log.Warn().
					Str("trace_id", traceID).
					Str("tool", toolName).
					Str("error", err.Error()).
					Msg("validation_failed: missing required")
				return nil, errors.New(errors.ToolParamInvalid, err.Error())
			}

			// ② enum 值校验
			if err := validateEnums(schema, params); err != nil {
				log.Warn().
					Str("trace_id", traceID).
					Str("tool", toolName).
					Str("error", err.Error()).
					Msg("validation_failed: invalid enum")
				return nil, errors.New(errors.ToolParamInvalid, err.Error())
			}

			// ③ 参数长度校验
			maxLen := cfg.MaxParamLength
			if maxLen <= 0 {
				maxLen = 4096
			}
			if err := validateParamLength(params, maxLen); err != nil {
				log.Warn().
					Str("trace_id", traceID).
					Str("tool", toolName).
					Str("error", err.Error()).
					Msg("validation_failed: param too long")
				return nil, errors.New(errors.ToolParamInvalid, err.Error())
			}

			// ④ 注入检测
			if cfg.InjectionDetection {
				if err := detectInjection(params); err != nil {
					log.Warn().
						Str("trace_id", traceID).
						Str("tool", toolName).
						Str("error", err.Error()).
						Msg("validation_failed: injection detected")
					return nil, errors.New(errors.ToolParamInvalid, err.Error())
				}
			}

			// ⑤ 严格模式：拒绝未声明参数
			if cfg.StrictMode {
				if err := validateNoExtraParams(schema, params); err != nil {
					log.Warn().
						Str("trace_id", traceID).
						Str("tool", toolName).
						Str("error", err.Error()).
						Msg("validation_failed: extra params")
					return nil, errors.New(errors.ToolParamInvalid, err.Error())
				}
			}

			return next(ctx, params)
		}
	}
}

// validateRequired 校验 required 字段
func validateRequired(schema map[string]interface{}, params map[string]interface{}) error {
	required, ok := schema["required"]
	if !ok {
		return nil
	}

	reqList, ok := required.([]string)
	if !ok {
		// 尝试 []interface{} 转换
		if reqIface, ok := required.([]interface{}); ok {
			for _, r := range reqIface {
				if s, ok := r.(string); ok {
					reqList = append(reqList, s)
				}
			}
		}
	}

	for _, field := range reqList {
		val, exists := params[field]
		if !exists || val == nil {
			return fmt.Errorf("required parameter '%s' is missing", field)
		}
		// 空字符串也视为缺失
		if s, ok := val.(string); ok && s == "" {
			return fmt.Errorf("required parameter '%s' is empty", field)
		}
	}
	return nil
}

// validateEnums 校验 enum 约束
func validateEnums(schema map[string]interface{}, params map[string]interface{}) error {
	properties, ok := schema["properties"].(map[string]interface{})
	if !ok {
		return nil
	}

	for paramName, paramValue := range params {
		propDef, ok := properties[paramName].(map[string]interface{})
		if !ok {
			continue
		}

		enumValues, ok := propDef["enum"]
		if !ok {
			continue
		}

		strVal := fmt.Sprintf("%v", paramValue)
		valid := false

		switch ev := enumValues.(type) {
		case []string:
			for _, allowed := range ev {
				if strVal == allowed {
					valid = true
					break
				}
			}
		case []interface{}:
			for _, allowed := range ev {
				if strVal == fmt.Sprintf("%v", allowed) {
					valid = true
					break
				}
			}
		}

		if !valid {
			return fmt.Errorf("parameter '%s' value '%s' is not in allowed enum values", paramName, strVal)
		}
	}
	return nil
}

// validateParamLength 校验参数长度
func validateParamLength(params map[string]interface{}, maxLen int) error {
	for key, val := range params {
		if s, ok := val.(string); ok {
			if len(s) > maxLen {
				return fmt.Errorf("parameter '%s' exceeds max length (%d > %d)", key, len(s), maxLen)
			}
		}
	}
	return nil
}

// detectInjection 检测常见注入模式
// WHY 检测这些模式：
// - LLM 可能被 prompt injection 诱导生成恶意参数
// - 即使工具是 Mock，也要演示安全防护能力
var injectionPatterns = []string{
	"$(", "`",       // Shell 命令注入
	"&&", "||", ";", // Shell 命令链
	"<script",       // XSS
	"../", "..\\",   // 路径遍历
	"DROP TABLE", "DELETE FROM", "INSERT INTO", // SQL 注入
	"__import__", "eval(", "exec(", // Python 注入
}

func detectInjection(params map[string]interface{}) error {
	for key, val := range params {
		s, ok := val.(string)
		if !ok {
			continue
		}
		lowerS := strings.ToLower(s)
		for _, pattern := range injectionPatterns {
			if strings.Contains(lowerS, strings.ToLower(pattern)) {
				return fmt.Errorf("potential injection detected in parameter '%s': pattern '%s'",
					key, pattern)
			}
		}
	}
	return nil
}

// validateNoExtraParams 严格模式：拒绝未声明参数
func validateNoExtraParams(schema map[string]interface{}, params map[string]interface{}) error {
	properties, ok := schema["properties"].(map[string]interface{})
	if !ok {
		return nil
	}

	for key := range params {
		if _, declared := properties[key]; !declared {
			return fmt.Errorf("unknown parameter '%s' (strict mode)", key)
		}
	}
	return nil
}
