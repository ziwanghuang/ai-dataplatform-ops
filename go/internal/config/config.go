package config

import (
	"fmt"
	"time"

	"github.com/spf13/viper"
)

// Config 全局配置
type Config struct {
	Server     ServerConfig     `mapstructure:"server"`
	MCP        MCPConfig        `mapstructure:"mcp"`
	Middleware MiddlewareConfig  `mapstructure:"middleware"`
	Database   DatabaseConfig   `mapstructure:"database"`
	Tracing    TracingConfig    `mapstructure:"tracing"`
}

// ServerConfig HTTP 服务配置
type ServerConfig struct {
	Host string `mapstructure:"host"`
	Port int    `mapstructure:"port"`
	Mode string `mapstructure:"mode"` // "stdio" | "http"
}

// MCPConfig MCP 协议配置
type MCPConfig struct {
	ToolTimeout   time.Duration `mapstructure:"tool_timeout"`
	MaxConcurrent int           `mapstructure:"max_concurrent"`
}

// MiddlewareConfig 中间件配置
type MiddlewareConfig struct {
	Timeout        TimeoutConfig        `mapstructure:"timeout"`
	CircuitBreaker CircuitBreakerConfig `mapstructure:"circuit_breaker"`
	RateLimit      RateLimitConfig      `mapstructure:"rate_limit"`
	Cache          CacheConfig          `mapstructure:"cache"`
}

// TimeoutConfig 超时配置
type TimeoutConfig struct {
	Default time.Duration            `mapstructure:"default"`
	PerTool map[string]time.Duration `mapstructure:"per_tool"`
}

// CircuitBreakerConfig 熔断器配置
type CircuitBreakerConfig struct {
	MaxRequests  uint32        `mapstructure:"max_requests"`
	Interval     time.Duration `mapstructure:"interval"`
	Timeout      time.Duration `mapstructure:"timeout"`
	FailureRatio float64       `mapstructure:"failure_ratio"`
}

// RateLimitConfig 限流配置
type RateLimitConfig struct {
	GlobalRPS  int `mapstructure:"global_rps"`
	PerUserRPS int `mapstructure:"per_user_rps"`
}

// CacheConfig 缓存配置
type CacheConfig struct {
	Enabled    bool          `mapstructure:"enabled"`
	DefaultTTL time.Duration `mapstructure:"default_ttl"`
	RedisURL   string        `mapstructure:"redis_url"`
}

// DatabaseConfig 数据库配置
type DatabaseConfig struct {
	PostgresURL string `mapstructure:"postgres_url"`
	RedisURL    string `mapstructure:"redis_url"`
}

// TracingConfig 链路追踪配置
type TracingConfig struct {
	Enabled  bool   `mapstructure:"enabled"`
	Endpoint string `mapstructure:"endpoint"`
}

// Load 从配置文件加载配置
func Load(configPath string) (*Config, error) {
	v := viper.New()
	v.SetConfigFile(configPath)
	v.AutomaticEnv()

	// 默认值
	v.SetDefault("server.host", "0.0.0.0")
	v.SetDefault("server.port", 3000)
	v.SetDefault("server.mode", "http")
	v.SetDefault("mcp.tool_timeout", "30s")
	v.SetDefault("mcp.max_concurrent", 10)
	v.SetDefault("middleware.timeout.default", "30s")
	v.SetDefault("middleware.circuit_breaker.max_requests", 5)
	v.SetDefault("middleware.circuit_breaker.interval", "60s")
	v.SetDefault("middleware.circuit_breaker.timeout", "30s")
	v.SetDefault("middleware.circuit_breaker.failure_ratio", 0.5)
	v.SetDefault("middleware.rate_limit.global_rps", 100)
	v.SetDefault("middleware.rate_limit.per_user_rps", 10)
	v.SetDefault("middleware.cache.enabled", true)
	v.SetDefault("middleware.cache.default_ttl", "5m")
	v.SetDefault("middleware.cache.redis_url", "redis://localhost:6379/0")
	v.SetDefault("database.redis_url", "redis://localhost:6379/0")
	v.SetDefault("tracing.enabled", false)
	v.SetDefault("tracing.endpoint", "localhost:4317")

	if err := v.ReadInConfig(); err != nil {
		return nil, fmt.Errorf("read config: %w", err)
	}

	var cfg Config
	if err := v.Unmarshal(&cfg); err != nil {
		return nil, fmt.Errorf("unmarshal config: %w", err)
	}

	return &cfg, nil
}
