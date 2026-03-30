"""
工具自动发现 — MCP Server 启动时拉取工具列表

WHY 自动发现而不是硬编码工具列表：
- Go MCP Server 的工具可以热加载（新增工具不需要重启 Python Agent）
- 实际工具列表可能与 Registry 的静态列表有差异（版本升级、灰度发布）
- 发现过程同时验证 MCP Server 的连通性

发现流程：
1. Agent 启动时 → GET /mcp (tools/list) → 获取所有注册工具的 schema
2. 对比本地 Registry → 新增的注册，移除的标记为不可用
3. 后续定时刷新（间隔可配）
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from aiops.core.config import settings
from aiops.core.logging import get_logger
from aiops.mcp_client.registry import ToolRegistry

logger = get_logger(__name__)


@dataclass
class DiscoveryResult:
    """发现结果."""
    discovered_tools: list[dict[str, Any]]
    new_tools: int = 0
    updated_tools: int = 0
    removed_tools: int = 0
    timestamp: float = field(default_factory=time.time)
    error: str | None = None


class ToolDiscovery:
    """
    工具自动发现服务.

    职责：
    1. 启动时从 MCP Server 拉取工具列表
    2. 定时刷新（检测新增/移除的工具）
    3. 同步到 ToolRegistry
    4. 健康检查

    WHY 不直接在 MCPClient 里做发现：
    - 发现是启动时 / 定期的操作，不是每次调用都做
    - 发现失败不应该阻塞工具调用（本地 Registry 有静态列表兜底）
    - 需要独立的重试和容错策略
    """

    def __init__(
        self,
        registry: ToolRegistry,
        refresh_interval: float = 300.0,  # 5 分钟刷新一次
    ) -> None:
        self._registry = registry
        self._refresh_interval = refresh_interval
        self._last_discovery: DiscoveryResult | None = None
        self._running = False
        self._refresh_task: asyncio.Task[None] | None = None

    async def discover(self) -> DiscoveryResult:
        """
        从 MCP Server 拉取工具列表并同步到 Registry.

        Returns:
            DiscoveryResult 包含发现的工具和同步统计
        """
        try:
            async with httpx.AsyncClient(
                base_url=settings.mcp.gateway_url,
                timeout=httpx.Timeout(connect=5.0, read=10.0),
            ) as client:
                # JSON-RPC tools/list 请求
                payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/list",
                    "params": {},
                }
                resp = await client.post("/mcp", json=payload)
                resp.raise_for_status()
                data = resp.json()

                if "error" in data and data["error"]:
                    error_msg = data["error"].get("message", "Unknown error")
                    result = DiscoveryResult(
                        discovered_tools=[],
                        error=f"MCP Server error: {error_msg}",
                    )
                    self._last_discovery = result
                    return result

                # 解析工具列表
                tools_data = data.get("result", {}).get("tools", [])
                discovered_tools = self._parse_tools(tools_data)

                # 同步到 Registry
                new_count = self._registry.update_from_discovery(discovered_tools)

                # 检测移除的工具
                remote_names = {t["name"] for t in discovered_tools}
                local_names = {t["name"] for t in self._registry.list_all()}
                removed_names = local_names - remote_names
                # 注意：不自动移除，只告警（防止网络波动误删）
                if removed_names:
                    logger.warning(
                        "tools_missing_from_remote",
                        missing_tools=list(removed_names),
                        count=len(removed_names),
                    )

                result = DiscoveryResult(
                    discovered_tools=discovered_tools,
                    new_tools=new_count,
                    removed_tools=len(removed_names),
                )

                self._last_discovery = result
                logger.info(
                    "discovery_completed",
                    total_discovered=len(discovered_tools),
                    new_tools=new_count,
                    missing_tools=len(removed_names),
                )
                return result

        except httpx.ConnectError:
            error = "Cannot connect to MCP Server"
            logger.warning("discovery_connect_failed", error=error)
            result = DiscoveryResult(discovered_tools=[], error=error)
            self._last_discovery = result
            return result

        except Exception as e:
            error = f"Discovery failed: {e}"
            logger.error("discovery_failed", error=error)
            result = DiscoveryResult(discovered_tools=[], error=error)
            self._last_discovery = result
            return result

    async def start_periodic_refresh(self) -> None:
        """启动定期刷新任务."""
        if self._running:
            return

        self._running = True
        self._refresh_task = asyncio.create_task(self._refresh_loop())
        logger.info(
            "discovery_refresh_started",
            interval_seconds=self._refresh_interval,
        )

    async def stop_periodic_refresh(self) -> None:
        """停止定期刷新."""
        self._running = False
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
        logger.info("discovery_refresh_stopped")

    async def _refresh_loop(self) -> None:
        """定期刷新循环."""
        while self._running:
            try:
                await asyncio.sleep(self._refresh_interval)
                if self._running:
                    await self.discover()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("discovery_refresh_error", error=str(e))
                # 失败后等待更长时间再重试
                await asyncio.sleep(self._refresh_interval * 2)

    def _parse_tools(self, raw_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        解析 MCP Server 返回的工具 schema.

        MCP 工具 schema 格式:
        {
            "name": "hdfs_namenode_status",
            "description": "...",
            "inputSchema": { "type": "object", "properties": {...} }
        }

        转换为 Registry 内部格式：
        {
            "name": "...",
            "component": "...",
            "risk": "...",
            "source": "internal",
            "description": "...",
            "input_schema": {...}
        }
        """
        parsed: list[dict[str, Any]] = []

        for tool in raw_tools:
            name = tool.get("name", "")
            if not name:
                continue

            # 从工具名推断组件（hdfs_xxx → hdfs）
            component = self._infer_component(name)

            # 从 schema 或名称推断风险等级
            risk = self._infer_risk(name, tool)

            parsed.append({
                "name": name,
                "component": component,
                "risk": risk,
                "source": "internal",
                "description": tool.get("description", ""),
                "input_schema": tool.get("inputSchema", {}),
            })

        return parsed

    @staticmethod
    def _infer_component(tool_name: str) -> str:
        """从工具名推断所属组件."""
        prefix_map = {
            "hdfs_": "hdfs",
            "yarn_": "yarn",
            "kafka_": "kafka",
            "es_": "es",
            "zk_": "zk",
            "ops_": "ops",
            "query_metrics": "metrics",
            "search_logs": "log",
            "query_alerts": "alert",
            "query_events": "event",
            "query_topology": "topology",
        }
        for prefix, component in prefix_map.items():
            if tool_name.startswith(prefix) or tool_name == prefix:
                return component
        return "unknown"

    @staticmethod
    def _infer_risk(tool_name: str, tool_schema: dict[str, Any]) -> str:
        """从工具名和 schema 推断风险等级."""
        # 高风险操作关键词
        high_risk_keywords = {"restart", "scale", "decommission", "delete", "drop"}
        medium_risk_keywords = {"update", "config", "gc", "clear"}

        name_lower = tool_name.lower()
        for keyword in high_risk_keywords:
            if keyword in name_lower:
                return "high" if "decommission" not in name_lower else "critical"
        for keyword in medium_risk_keywords:
            if keyword in name_lower:
                return "medium"

        return "none"

    @property
    def last_discovery(self) -> DiscoveryResult | None:
        return self._last_discovery

    @property
    def is_running(self) -> bool:
        return self._running
