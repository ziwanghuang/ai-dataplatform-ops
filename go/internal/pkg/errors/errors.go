package errors

import (
	"fmt"
	"net/http"
)

// Code 错误码
type Code string

const (
	// Tool errors
	ToolNotFound     Code = "AIOPS-T0301"
	ToolCallFailed   Code = "AIOPS-T0601"
	ToolTimeout      Code = "AIOPS-T0402"
	ToolParamInvalid Code = "AIOPS-T0302"
	ToolCircuitOpen  Code = "AIOPS-T0602"
	ToolRateLimited  Code = "AIOPS-T0603"

	// System errors
	InternalError Code = "AIOPS-S0701"
	ConfigError   Code = "AIOPS-S0101"
)

// MCPError MCP 业务错误
type MCPError struct {
	Code    Code   `json:"code"`
	Message string `json:"message"`
	Detail  string `json:"detail,omitempty"`
	Cause   error  `json:"-"`
}

// Error implements error interface
func (e *MCPError) Error() string {
	return fmt.Sprintf("[%s] %s", e.Code, e.Message)
}

// Unwrap implements errors.Unwrap
func (e *MCPError) Unwrap() error {
	return e.Cause
}

// HTTPStatus 返回对应 HTTP 状态码
func (e *MCPError) HTTPStatus() int {
	switch e.Code {
	case ToolNotFound:
		return http.StatusNotFound
	case ToolParamInvalid:
		return http.StatusBadRequest
	case ToolTimeout:
		return http.StatusGatewayTimeout
	case ToolRateLimited:
		return http.StatusTooManyRequests
	case ToolCircuitOpen:
		return http.StatusServiceUnavailable
	default:
		return http.StatusInternalServerError
	}
}

// New 创建 MCPError
func New(code Code, message string) *MCPError {
	return &MCPError{Code: code, Message: message}
}

// Wrap 包装底层错误
func Wrap(code Code, message string, cause error) *MCPError {
	return &MCPError{Code: code, Message: message, Cause: cause}
}

// Newf 格式化创建 MCPError
func Newf(code Code, format string, args ...interface{}) *MCPError {
	return &MCPError{Code: code, Message: fmt.Sprintf(format, args...)}
}
