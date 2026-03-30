"""
工具管理路由 — 工具列表、直接调用、健康检查
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from aiops.core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/tools", tags=["tools"])


class ToolCallRequest(BaseModel):
    """工具调用请求."""
    tool_name: str
    params: dict[str, Any] = Field(default_factory=dict)
    timeout: float | None = None


class ToolCallResponse(BaseModel):
    """工具调用响应."""
    tool_name: str
    result: str
    cached: bool = False
    latency_ms: int = 0


@router.get("/")
async def list_tools(
    component: str | None = None,
    risk_level: str | None = None,
) -> dict[str, Any]:
    """
    列出可用工具.

    支持按组件和风险等级过滤。
    """
    from aiops.mcp_client.registry import ToolRegistry

    registry = ToolRegistry()
    tools = registry.list_all()

    if component:
        tools = [t for t in tools if t.get("component") == component]
    if risk_level:
        from aiops.mcp_client.registry import RiskLevel
        max_risk = RiskLevel(risk_level).order
        tools = [t for t in tools if RiskLevel(t["risk"]).order <= max_risk]

    return {
        "total": len(tools),
        "tools": tools,
        "components": registry.list_components(),
    }


@router.post("/call", response_model=ToolCallResponse)
async def call_tool(request: ToolCallRequest) -> ToolCallResponse:
    """
    直接调用工具（绕过 Agent 链路）.

    用于调试和运维人员手动操作。
    需要 RBAC 鉴权（当前版本跳过）。
    """
    logger.info(
        "direct_tool_call",
        tool_name=request.tool_name,
        params=request.params,
    )

    try:
        from aiops.mcp_client.client import MCPClient

        client = MCPClient()
        result = await client.call_tool(
            request.tool_name,
            request.params,
            timeout_override=request.timeout,
        )
        await client.close()

        return ToolCallResponse(
            tool_name=request.tool_name,
            result=result,
        )

    except Exception as e:
        logger.error("tool_call_failed", tool=request.tool_name, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/health")
async def tools_health() -> dict[str, Any]:
    """MCP Server 健康检查."""
    try:
        from aiops.mcp_client.client import MCPClient

        client = MCPClient()
        health = await client.health_check()
        await client.close()
        return {"status": "ok", "backends": health}
    except Exception as e:
        return {"status": "degraded", "error": str(e)}
