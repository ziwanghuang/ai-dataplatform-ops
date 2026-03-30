"""MCPClient — Python 侧 MCP 工具调用入口."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

import httpx

from aiops.core.config import settings
from aiops.core.errors import ErrorCode, ToolError
from aiops.core.logging import get_logger
from aiops.mcp_client.registry import ToolRegistry

logger = get_logger(__name__)

TOOL_TIMEOUT_MAP: dict[str, float] = {
    "none": 15.0, "low": 20.0, "medium": 30.0, "high": 60.0, "critical": 120.0,
}

CACHEABLE_PREFIXES = frozenset([
    "hdfs_cluster_overview", "hdfs_namenode_status", "hdfs_datanode_list",
    "yarn_cluster_metrics", "yarn_queue_status",
    "kafka_cluster_overview", "kafka_topic_list",
    "es_cluster_health", "es_node_stats",
])

CACHE_TTL_SECONDS = 30


class MCPClient:
    """
    MCP 工具统一调用客户端.

    缓存策略（双模式）：
    - 内存 dict（默认）：容量 200，LRU 淘汰，适合单实例
    - Redis string（可选）：带 TTL，多实例共享，适合生产
    - 切换：有 Redis 配置时自动启用，不可用时降级为内存

    WHY 双层而不是只用 Redis：
    - 内存缓存 <0.01ms，Redis ~1ms，热数据走内存更快
    - Redis 做跨实例共享 + 持久化
    - 内存是 Redis 的本地 L1 缓存
    """

    def __init__(self, redis_url: str = "") -> None:
        self._registry = ToolRegistry()
        self._internal_client = httpx.AsyncClient(
            base_url=settings.mcp.gateway_url,
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=10.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=30),
        )
        # L1: 内存缓存
        self._cache: dict[str, tuple[float, str]] = {}
        # L2: Redis 缓存（可选）
        self._redis: Any = None
        self._use_redis = False
        if redis_url or settings.db.redis_url:
            self._init_redis(redis_url or settings.db.redis_url)

        self._backend_healthy: dict[str, bool] = {"internal": True}
        self._call_count = 0
        self._error_count = 0

    def _init_redis(self, redis_url: str) -> None:
        """尝试连接 Redis."""
        try:
            import redis as redis_lib
            self._redis = redis_lib.Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=3,
            )
            self._redis.ping()
            self._use_redis = True
            logger.info("mcp_redis_cache_connected")
        except Exception as e:
            logger.warning("mcp_redis_cache_failed_using_memory", error=str(e))
            self._redis = None

    async def call_tool(
        self,
        tool_name: str,
        params: dict[str, Any] | None = None,
        timeout_override: float | None = None,
    ) -> str:
        """调用 MCP 工具（自动路由到正确后端）."""
        params = params or {}
        self._call_count += 1

        tool_info = self._registry.get(tool_name)
        if not tool_info:
            raise ToolError(code=ErrorCode.TOOL_NOT_FOUND, message=f"Unknown tool: {tool_name}")

        # 缓存检查
        cache_key = self._cache_key(tool_name, params)
        if tool_name in CACHEABLE_PREFIXES:
            cached = self._get_cache(cache_key)
            if cached is not None:
                return cached

        tool_risk = tool_info.get("risk", "none")
        timeout = timeout_override or TOOL_TIMEOUT_MAP.get(tool_risk, 15.0)

        try:
            result = await self._call_internal(tool_name, params, timeout)
            if tool_name in CACHEABLE_PREFIXES:
                self._set_cache(cache_key, result)
            return result
        except ToolError:
            self._error_count += 1
            raise
        except Exception as e:
            self._error_count += 1
            raise ToolError(
                code=ErrorCode.TOOL_CALL_FAILED,
                message=f"Unexpected error calling {tool_name}: {e}",
            ) from e

    async def batch_call_tools(
        self,
        calls: list[tuple[str, dict[str, Any]]],
        max_concurrency: int = 5,
        per_tool_timeout: float = 15.0,
    ) -> list[dict[str, Any]]:
        """并行批量调用多个工具."""
        semaphore = asyncio.Semaphore(max_concurrency)

        async def _call_one(tool_name: str, params: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                try:
                    result = await asyncio.wait_for(
                        self.call_tool(tool_name, params),
                        timeout=per_tool_timeout,
                    )
                    return {"tool": tool_name, "result": result, "error": None}
                except (TimeoutError, ToolError) as e:
                    return {"tool": tool_name, "result": None, "error": str(e)}

        tasks = [_call_one(name, params) for name, params in calls]
        results = await asyncio.gather(*tasks)
        return list(results)

    async def _call_internal(self, tool_name: str, params: dict[str, Any], timeout: float) -> str:
        if not self._backend_healthy.get("internal", True):
            raise ToolError(code=ErrorCode.TOOL_CALL_FAILED, message=f"Internal MCP backend unhealthy")

        payload = {
            "jsonrpc": "2.0", "id": self._call_count,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": params},
        }

        try:
            resp = await self._internal_client.post("/mcp", json=payload, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()

            if "error" in data and data["error"]:
                error_msg = data["error"].get("message", "Unknown MCP error")
                raise ToolError(code=ErrorCode.TOOL_CALL_FAILED, message=f"MCP error: {error_msg}")

            result = data.get("result", {})
            contents = result.get("content", [])
            text = "\n".join(c.get("text", "") for c in contents if c.get("type") == "text")

            if len(text) > 5000:
                text = text[:5000] + f"\n... (truncated, total {len(text)} chars)"
            return text

        except httpx.TimeoutException:
            raise ToolError(code=ErrorCode.TOOL_TIMEOUT, message=f"Tool {tool_name} timed out after {timeout}s")
        except httpx.HTTPStatusError as e:
            raise ToolError(code=ErrorCode.TOOL_CALL_FAILED, message=f"HTTP error: {e.response.status_code}")

    async def list_tools(self, user_role: str | None = None) -> list[dict[str, Any]]:
        if user_role:
            return self._registry.filter_for_user(user_role)
        return self._registry.list_all()

    async def health_check(self) -> dict[str, bool]:
        try:
            resp = await self._internal_client.get("/health", timeout=5.0)
            self._backend_healthy["internal"] = resp.status_code == 200
        except Exception:
            self._backend_healthy["internal"] = False
        return dict(self._backend_healthy)

    async def close(self) -> None:
        await self._internal_client.aclose()
        self._cache.clear()

    @staticmethod
    def _cache_key(tool_name: str, params: dict[str, Any]) -> str:
        param_str = json.dumps(params, sort_keys=True)
        return hashlib.md5(f"{tool_name}:{param_str}".encode()).hexdigest()

    def _get_cache(self, key: str) -> str | None:
        """查询缓存（L1 内存 → L2 Redis → miss）."""
        # L1: 内存
        if key in self._cache:
            ts, value = self._cache[key]
            if time.time() - ts < CACHE_TTL_SECONDS:
                return value
            del self._cache[key]

        # L2: Redis
        if self._use_redis and self._redis:
            try:
                redis_key = f"mcp:cache:{key}"
                value = self._redis.get(redis_key)
                if value is not None:
                    # 回填 L1
                    self._cache[key] = (time.time(), value)
                    return value
            except Exception:
                pass  # Redis 失败不影响功能

        return None

    def _set_cache(self, key: str, value: str) -> None:
        """写入缓存（L1 + L2 双写）."""
        # L1: 内存
        self._cache[key] = (time.time(), value)
        if len(self._cache) > 200:
            oldest_key = min(self._cache, key=lambda k: self._cache[k][0])
            del self._cache[oldest_key]

        # L2: Redis（带 TTL）
        if self._use_redis and self._redis:
            try:
                redis_key = f"mcp:cache:{key}"
                self._redis.setex(redis_key, CACHE_TTL_SECONDS, value)
            except Exception:
                pass  # Redis 失败不影响功能

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "total_calls": self._call_count,
            "total_errors": self._error_count,
            "cache_size": len(self._cache),
            "backend_health": dict(self._backend_healthy),
        }
