package middleware

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"sync"
	"time"

	"github.com/rs/zerolog/log"
	"github.com/ziwang/aiops-mcp/internal/protocol"
)

// ────────────────────────────────────────────────────────────────────────────
// CacheMiddleware — 工具调用结果缓存
// WHY 缓存工具调用结果：
// - 诊断循环中同一个工具可能被多次调用（假设-验证）
// - HDFS/YARN 状态短时间内不会剧烈变化，缓存命中能显著降低延迟
// - 只缓存 RiskLevel=None 的只读工具结果
//
// WHY 不缓存写操作：
// - 写操作每次执行结果不同（如 restart_service）
// - 缓存写操作结果会给用户"操作已执行"的错觉
//
// 缓存策略：
// - Key: SHA256(tool_name + sorted_params)
// - TTL: 默认 5 分钟，可按工具配置
// - 内存缓存（生产环境可替换为 Redis）
// - 最大条目 1000（防止内存膨胀）
// ────────────────────────────────────────────────────────────────────────────

// CacheableConfig 缓存中间件配置
type CacheableConfig struct {
	Enabled    bool          `mapstructure:"enabled"`
	DefaultTTL time.Duration `mapstructure:"default_ttl"`
	MaxEntries int           `mapstructure:"max_entries"`

	// PerToolTTL 每个工具的缓存 TTL 覆盖
	PerToolTTL map[string]time.Duration `mapstructure:"per_tool_ttl"`
}

// cacheEntry 缓存条目
type cacheEntry struct {
	result    *protocol.ToolResult
	createdAt time.Time
	ttl       time.Duration
	hitCount  int
}

func (e *cacheEntry) isExpired() bool {
	return time.Since(e.createdAt) > e.ttl
}

// cacheStore 内存缓存存储
type cacheStore struct {
	mu         sync.RWMutex
	entries    map[string]*cacheEntry
	maxEntries int

	// 统计
	hits   int64
	misses int64
	evicts int64
}

func newCacheStore(maxEntries int) *cacheStore {
	if maxEntries <= 0 {
		maxEntries = 1000
	}
	return &cacheStore{
		entries:    make(map[string]*cacheEntry),
		maxEntries: maxEntries,
	}
}

func (s *cacheStore) get(key string) (*protocol.ToolResult, bool) {
	s.mu.RLock()
	entry, ok := s.entries[key]
	s.mu.RUnlock()

	if !ok {
		s.mu.Lock()
		s.misses++
		s.mu.Unlock()
		return nil, false
	}

	if entry.isExpired() {
		s.mu.Lock()
		delete(s.entries, key)
		s.misses++
		s.mu.Unlock()
		return nil, false
	}

	s.mu.Lock()
	entry.hitCount++
	s.hits++
	s.mu.Unlock()

	return entry.result, true
}

func (s *cacheStore) set(key string, result *protocol.ToolResult, ttl time.Duration) {
	s.mu.Lock()
	defer s.mu.Unlock()

	// 超过最大条目时，淘汰最旧的条目
	if len(s.entries) >= s.maxEntries {
		s.evictOldest()
	}

	s.entries[key] = &cacheEntry{
		result:    result,
		createdAt: time.Now(),
		ttl:       ttl,
	}
}

// evictOldest 淘汰最旧的条目（简单 LRU 近似）
// 注：需要持有 mu 写锁
func (s *cacheStore) evictOldest() {
	var oldestKey string
	var oldestTime time.Time

	for k, v := range s.entries {
		if oldestKey == "" || v.createdAt.Before(oldestTime) {
			oldestKey = k
			oldestTime = v.createdAt
		}
	}

	if oldestKey != "" {
		delete(s.entries, oldestKey)
		s.evicts++
	}
}

func (s *cacheStore) stats() map[string]interface{} {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return map[string]interface{}{
		"entries":    len(s.entries),
		"max":       s.maxEntries,
		"hits":      s.hits,
		"misses":    s.misses,
		"evictions": s.evicts,
		"hit_ratio": func() float64 {
			total := s.hits + s.misses
			if total == 0 {
				return 0
			}
			return float64(s.hits) / float64(total) * 100
		}(),
	}
}

// CacheMiddleware 创建缓存中间件
func CacheMiddleware(tool protocol.Tool, cfg CacheableConfig) Middleware {
	if !cfg.Enabled {
		return func(next ToolHandler) ToolHandler { return next }
	}

	store := newCacheStore(cfg.MaxEntries)
	toolName := tool.Name()
	riskLevel := tool.RiskLevel()

	// 确定 TTL
	ttl := cfg.DefaultTTL
	if ttl == 0 {
		ttl = 5 * time.Minute
	}
	if customTTL, ok := cfg.PerToolTTL[toolName]; ok {
		ttl = customTTL
	}

	return func(next ToolHandler) ToolHandler {
		return func(ctx context.Context, params map[string]interface{}) (*protocol.ToolResult, error) {
			// 只缓存只读工具（RiskNone 或 RiskLow）
			if riskLevel > protocol.RiskLow {
				return next(ctx, params)
			}

			// 生成缓存 key
			cacheKey := generateCacheKey(toolName, params)

			// 查缓存
			if cached, ok := store.get(cacheKey); ok {
				log.Debug().
					Str("tool", toolName).
					Str("cache_key", cacheKey[:16]).
					Msg("cache_hit")
				return cached, nil
			}

			// 缓存未命中，执行工具
			result, err := next(ctx, params)

			// 只缓存成功结果
			if err == nil && result != nil && !result.IsError {
				store.set(cacheKey, result, ttl)
				log.Debug().
					Str("tool", toolName).
					Str("cache_key", cacheKey[:16]).
					Dur("ttl", ttl).
					Msg("cache_set")
			}

			return result, err
		}
	}
}

// generateCacheKey 生成缓存 key = SHA256(toolName + sorted_params_json)
func generateCacheKey(toolName string, params map[string]interface{}) string {
	// 参数序列化（json.Marshal 会按 key 字母序排列）
	paramBytes, _ := json.Marshal(params)
	input := fmt.Sprintf("%s:%s", toolName, string(paramBytes))
	hash := sha256.Sum256([]byte(input))
	return fmt.Sprintf("%x", hash)
}

// GetCacheStats 获取缓存统计（用于 /health 端点）
// 注：需要在创建时保存 store 引用，这里提供一个全局统计方式
var globalCacheStores sync.Map

// RegisterCacheStore 注册缓存存储（在 BuildMiddlewareChain 中调用）
func RegisterCacheStore(toolName string, store *cacheStore) {
	globalCacheStores.Store(toolName, store)
}

// GetAllCacheStats 获取所有工具的缓存统计
func GetAllCacheStats() map[string]interface{} {
	stats := make(map[string]interface{})
	globalCacheStores.Range(func(key, value interface{}) bool {
		toolName := key.(string)
		store := value.(*cacheStore)
		stats[toolName] = store.stats()
		return true
	})
	return stats
}
