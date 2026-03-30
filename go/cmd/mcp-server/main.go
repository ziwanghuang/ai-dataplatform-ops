package main

import (
	"context"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/gofiber/fiber/v2"
	"github.com/rs/zerolog/log"
	"github.com/ziwang/aiops-mcp/internal/config"
	"github.com/ziwang/aiops-mcp/internal/middleware"
	"github.com/ziwang/aiops-mcp/internal/pkg/logger"
	"github.com/ziwang/aiops-mcp/internal/protocol"
	toolsconfig "github.com/ziwang/aiops-mcp/internal/tools/config"
	"github.com/ziwang/aiops-mcp/internal/tools/es"
	"github.com/ziwang/aiops-mcp/internal/tools/hdfs"
	"github.com/ziwang/aiops-mcp/internal/tools/kafka"
	logtools "github.com/ziwang/aiops-mcp/internal/tools/log"
	"github.com/ziwang/aiops-mcp/internal/tools/metrics"
	"github.com/ziwang/aiops-mcp/internal/tools/ops"
	"github.com/ziwang/aiops-mcp/internal/tools/yarn"
)

// Version 由编译时注入 -ldflags
var Version = "dev"

func main() {
	configPath := flag.String("config", "configs/dev.yaml", "config file path")
	flag.Parse()

	// ── 初始化 Logger ──
	logger.Setup("dev", Version)

	// ── 加载配置 ──
	cfg, err := config.Load(*configPath)
	if err != nil {
		log.Fatal().Err(err).Str("config", *configPath).Msg("failed to load config")
	}

	// ── 注册工具 ──
	registry := protocol.NewRegistry()
	registerTools(registry)

	log.Info().
		Int("tools", registry.Count()).
		Msg("tools registered")

	// ── 创建 Handler ──
	// 中间件工厂：为每个工具构建完整的中间件链
	mwFactory := func(tool protocol.Tool, handler protocol.ToolHandler) protocol.ToolHandler {
		mwHandler := middleware.BuildMiddlewareChain(
			// 转换类型：protocol.ToolHandler → middleware.ToolHandler
			func(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
				return handler(ctx, params)
			},
			tool,
			cfg,
		)
		// 转换回 protocol.ToolHandler
		return func(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
			return mwHandler(ctx, params)
		}
	}
	handler := protocol.NewHandler(registry, mwFactory)

	// ── 创建 Fiber HTTP Server ──
	app := fiber.New(fiber.Config{
		AppName:      "AIOps MCP Server",
		ReadTimeout:  30 * time.Second,
		WriteTimeout: 30 * time.Second,
		IdleTimeout:  120 * time.Second,
	})

	// 健康检查
	app.Get("/health", func(c *fiber.Ctx) error {
		return c.JSON(fiber.Map{
			"status":  "healthy",
			"version": Version,
			"tools":   registry.Count(),
		})
	})

	// MCP JSON-RPC 端点
	// 接收 JSON-RPC 请求，转发给 Handler 处理
	app.Post("/mcp", func(c *fiber.Ctx) error {
		// 从 HTTP Header 提取 trace_id（Python MCP Client 通过 Header 传递）
		traceID := c.Get("X-Trace-ID", "no-trace")
		ctx := middleware.WithToolContext(c.Context(), "", traceID)

		// 读取请求体
		body := c.Body()
		if len(body) == 0 {
			return c.Status(http.StatusBadRequest).JSON(fiber.Map{
				"error": "empty request body",
			})
		}

		// 处理 JSON-RPC 请求
		resp, err := handler.HandleRequest(ctx, body)
		if err != nil {
			log.Error().Err(err).Msg("handle request failed")
			return c.Status(http.StatusInternalServerError).JSON(fiber.Map{
				"error": err.Error(),
			})
		}

		c.Set("Content-Type", "application/json")
		return c.Send(resp)
	})

	// 工具列表（调试用）
	app.Get("/tools", func(c *fiber.Ctx) error {
		defs := registry.ListDefinitions()
		return c.JSON(fiber.Map{
			"tools": defs,
			"count": len(defs),
		})
	})

	// ── 启动服务 ──
	addr := fmt.Sprintf("%s:%d", cfg.Server.Host, cfg.Server.Port)
	log.Info().
		Str("version", Version).
		Str("addr", addr).
		Int("tools", registry.Count()).
		Msg("MCP Server starting")

	// Graceful Shutdown
	go func() {
		if err := app.Listen(addr); err != nil {
			log.Fatal().Err(err).Msg("server listen failed")
		}
	}()

	// 等待终止信号
	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
	sig := <-quit

	log.Info().Str("signal", sig.String()).Msg("shutting down...")

	// 优雅关闭：等待当前请求完成（最多 10 秒）
	if err := app.ShutdownWithTimeout(10 * time.Second); err != nil {
		log.Error().Err(err).Msg("shutdown error")
	}

	log.Info().Msg("server stopped")
}

// registerTools 注册所有 MCP 工具
// WHY 集中注册：
// - 所有工具在一个地方注册，方便查看和管理
// - 新增工具只需在这里加一行，不需要改其他文件
// - CI 可以检查"是否所有工具都已注册"
func registerTools(registry *protocol.Registry) {
	// ── HDFS 工具 (Sprint 2) ──
	registry.Register(&hdfs.NamenodeStatusTool{})
	registry.Register(&hdfs.ClusterOverviewTool{})

	// ── Kafka 工具 (Sprint 2) ──
	registry.Register(&kafka.ConsumerLagTool{})

	// ── 日志搜索 (Sprint 2) ──
	registry.Register(&logtools.SearchLogsTool{})

	// ── Elasticsearch 工具 (Sprint 4) ──
	registry.Register(&es.ClusterHealthTool{})
	registry.Register(&es.NodeStatsTool{})
	registry.Register(&es.IndexStatsTool{})

	// ── YARN 工具 (Sprint 4) ──
	registry.Register(&yarn.QueueStatusTool{})
	registry.Register(&yarn.ApplicationsTool{})
	registry.Register(&yarn.ClusterMetricsTool{})
	registry.Register(&yarn.KillApplicationTool{})    // RiskLevel=High

	// ── Prometheus 指标工具 (Sprint 4) ──
	registry.Register(&metrics.QueryMetricsTool{})
	registry.Register(&metrics.AlertQueryTool{})

	// ── 配置管理工具 (Sprint 4) ──
	registry.Register(&toolsconfig.GetConfigTool{})
	registry.Register(&toolsconfig.DiffConfigTool{})

	// ── 运维操作工具 (Sprint 4) ──
	registry.Register(&ops.RestartServiceTool{})       // RiskLevel=Critical
	registry.Register(&ops.ScaleResourceTool{})        // RiskLevel=High
	registry.Register(&ops.RollbackConfigTool{})       // RiskLevel=High
}

func init() {
	// 确保 panic 时有日志
	defer func() {
		if r := recover(); r != nil {
			fmt.Fprintf(os.Stderr, "panic: %v\n", r)
			os.Exit(1)
		}
	}()
	// 确保 io 包被使用（Fiber 内部需要）
	_ = io.Discard
}
