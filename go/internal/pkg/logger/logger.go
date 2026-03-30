package logger

import (
	"io"
	"os"
	"time"

	"github.com/rs/zerolog"
	"github.com/rs/zerolog/log"
)

// Setup 初始化全局 logger
func Setup(env string, version string) {
	zerolog.TimeFieldFormat = time.RFC3339Nano

	var writer io.Writer
	if env == "dev" {
		// 开发环境：彩色控制台
		writer = zerolog.ConsoleWriter{Out: os.Stdout, TimeFormat: "15:04:05"}
	} else {
		// 生产环境：JSON
		writer = os.Stdout
	}

	log.Logger = zerolog.New(writer).
		With().
		Timestamp().
		Str("service", "aiops-mcp").
		Str("version", version).
		Str("env", env).
		Logger()

	if env == "dev" {
		zerolog.SetGlobalLevel(zerolog.DebugLevel)
	} else {
		zerolog.SetGlobalLevel(zerolog.InfoLevel)
	}
}

// WithTraceID 创建带 trace_id 的子 logger
func WithTraceID(traceID string) zerolog.Logger {
	return log.With().Str("trace_id", traceID).Logger()
}

// WithTool 创建带工具上下文的子 logger
func WithTool(traceID, toolName string) zerolog.Logger {
	return log.With().
		Str("trace_id", traceID).
		Str("tool_name", toolName).
		Logger()
}
