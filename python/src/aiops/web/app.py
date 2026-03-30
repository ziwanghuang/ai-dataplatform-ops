"""
FastAPI 应用入口 — Web API 层

提供 HTTP API 让外部系统与 Agent 交互：
- POST /api/v1/diagnose — 提交诊断请求
- POST /api/v1/alert — 接收告警
- GET  /api/v1/health — 健康检查
- GET  /api/v1/tools — 列出可用工具

WHY FastAPI 而不是 Flask：
- 原生 async 支持（Agent 链路全异步）
- 自动生成 OpenAPI 文档
- Pydantic 原生集成（请求/响应校验）
- 性能接近 Go 的 Fiber（uvicorn + uvloop）
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from aiops.agent.state import create_initial_state
from aiops.core.config import settings
from aiops.core.logging import get_logger, setup_logging

logger = get_logger(__name__)


# ──────────────────────────────────────────────────
# 请求/响应模型
# ──────────────────────────────────────────────────

class DiagnoseRequest(BaseModel):
    """诊断请求."""
    query: str = Field(min_length=1, max_length=2000, description="用户问题描述")
    user_id: str = Field(default="anonymous", description="用户 ID")
    cluster_id: str = Field(default="default", description="集群 ID")


class AlertRequest(BaseModel):
    """告警请求."""
    alerts: list[dict[str, Any]] = Field(min_length=1, description="告警列表")
    cluster_id: str = Field(default="default")


class DiagnoseResponse(BaseModel):
    """诊断响应."""
    request_id: str
    report: str
    diagnosis: dict[str, Any] | None = None
    remediation_plan: list[dict[str, Any]] = Field(default_factory=list)
    total_tokens: int = 0
    total_cost_usd: float = 0.0


class HealthResponse(BaseModel):
    """健康检查响应."""
    status: str
    version: str
    env: str


# ──────────────────────────────────────────────────
# 应用生命周期
# ──────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    应用生命周期管理.

    启动时初始化：日志 → 配置 → 构建图
    关闭时清理：连接池 → 日志 flush
    """
    # ── 启动 ──
    setup_logging()
    logger.info("app_starting", version=settings.version, env=settings.env.value)

    # 构建 Agent 图（延迟初始化，避免启动时加载 LLM 客户端）
    # 实际的 graph 在首次请求时构建（lazy init）
    app.state.graph = None

    yield

    # ── 关闭 ──
    logger.info("app_shutting_down")


# ──────────────────────────────────────────────────
# FastAPI 应用
# ──────────────────────────────────────────────────

app = FastAPI(
    title="AIOps Agent API",
    description="大数据平台智能运维 Agent 系统",
    version=settings.version,
    lifespan=lifespan,
)


def _get_graph(app_instance: FastAPI) -> Any:
    """延迟构建图——首次请求时初始化."""
    if app_instance.state.graph is None:
        from aiops.agent.graph import build_ops_graph
        app_instance.state.graph = build_ops_graph()
        logger.info("ops_graph_initialized")
    return app_instance.state.graph


# ──────────────────────────────────────────────────
# API 路由
# ──────────────────────────────────────────────────

@app.post("/api/v1/diagnose", response_model=DiagnoseResponse)
async def diagnose(request: DiagnoseRequest) -> DiagnoseResponse:
    """
    提交诊断请求.

    流程：创建初始 state → invoke 图 → 返回报告
    """
    state = create_initial_state(
        user_query=request.query,
        request_type="user_query",
        user_id=request.user_id,
        cluster_id=request.cluster_id,
    )

    try:
        graph = _get_graph(app)
        result = await graph.ainvoke(state)

        return DiagnoseResponse(
            request_id=result.get("request_id", ""),
            report=result.get("final_report", "诊断未完成"),
            diagnosis=result.get("diagnosis"),
            remediation_plan=result.get("remediation_plan", []),
            total_tokens=result.get("total_tokens", 0),
            total_cost_usd=result.get("total_cost_usd", 0.0),
        )

    except Exception as e:
        logger.error("diagnose_api_error", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"诊断失败: {e}") from e


@app.post("/api/v1/alert", response_model=DiagnoseResponse)
async def handle_alert(request: AlertRequest) -> DiagnoseResponse:
    """接收告警并触发诊断."""
    # 从告警列表中提取摘要作为 query
    alert_summary = "; ".join(
        a.get("message", str(a))[:100] for a in request.alerts[:5]
    )

    state = create_initial_state(
        user_query=f"告警处理：{alert_summary}",
        request_type="alert",
        cluster_id=request.cluster_id,
        alerts=request.alerts,
    )

    try:
        graph = _get_graph(app)
        result = await graph.ainvoke(state)

        return DiagnoseResponse(
            request_id=result.get("request_id", ""),
            report=result.get("final_report", "告警处理未完成"),
            diagnosis=result.get("diagnosis"),
            remediation_plan=result.get("remediation_plan", []),
            total_tokens=result.get("total_tokens", 0),
            total_cost_usd=result.get("total_cost_usd", 0.0),
        )

    except Exception as e:
        logger.error("alert_api_error", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"告警处理失败: {e}") from e


@app.get("/api/v1/tools")
async def list_tools(role: str = "engineer") -> dict[str, Any]:
    """列出当前用户可用的工具."""
    from aiops.mcp_client.registry import ToolRegistry
    registry = ToolRegistry()
    tools = registry.filter_for_user(role)
    return {
        "total": len(tools),
        "role": role,
        "tools": tools,
    }


@app.get("/api/v1/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """健康检查."""
    return HealthResponse(
        status="healthy",
        version=settings.version,
        env=settings.env.value,
    )
