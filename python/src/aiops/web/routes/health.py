"""
健康检查路由 — 系统健康状态
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter

from aiops.core.config import settings
from aiops.core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["health"])

_start_time = time.time()


@router.get("/healthz")
async def health_check() -> dict[str, Any]:
    """
    系统健康检查.

    K8s liveness probe 使用此端点。
    """
    return {
        "status": "healthy",
        "uptime_seconds": int(time.time() - _start_time),
        "version": "1.0.0",
        "environment": settings.environment,
    }


@router.get("/readyz")
async def readiness_check() -> dict[str, Any]:
    """
    就绪检查.

    K8s readiness probe 使用此端点。
    检查所有依赖是否就绪。
    """
    checks: dict[str, bool] = {}

    # MCP Server 连通性
    try:
        from aiops.mcp_client.client import MCPClient
        client = MCPClient()
        health = await client.health_check()
        await client.close()
        checks["mcp_server"] = health.get("internal", False)
    except Exception:
        checks["mcp_server"] = False

    # 整体状态
    all_ready = all(checks.values())

    return {
        "status": "ready" if all_ready else "not_ready",
        "checks": checks,
    }


@router.get("/api/v1/metrics")
async def get_metrics() -> dict[str, Any]:
    """
    获取系统指标（JSON 格式）.

    Prometheus metrics 通过 /metrics 端点暴露（中间件处理）。
    此端点提供 JSON 格式的指标概览。
    """
    return {
        "agent": {
            "total_requests": 0,
            "active_sessions": 0,
        },
        "llm": {
            "total_calls": 0,
            "cache_hit_rate": "0%",
        },
        "tools": {
            "total_calls": 0,
            "error_rate": "0%",
        },
    }
