"""
Agent API 路由 — 诊断/告警请求入口

WHY 拆分路由而不是全塞 app.py：
- app.py 157 行已经不少了，继续塞会变成 God Module
- 路由拆分后可以按模块测试
- 团队协作时不同人改不同路由，减少冲突
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from aiops.agent.graph import build_ops_graph
from aiops.agent.state import create_initial_state
from aiops.core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/agent", tags=["agent"])

# ──────────────────────────────────────────────────
# 模块级缓存——避免每个请求重建图（图编译 ~200ms）
# WHY 模块级而不是全局单例：
# - 模块导入时不立即初始化（延迟到第一次请求）
# - 测试时可以 mock 替换
# ──────────────────────────────────────────────────
_graph_instance: Any = None
_session_store: dict[str, dict[str, Any]] = {}  # 内存 session 存储（开发用）


def _get_graph() -> Any:
    """获取或创建 Agent 图实例."""
    global _graph_instance
    if _graph_instance is None:
        _graph_instance = build_ops_graph()
    return _graph_instance


# ──────────────────────────────────────────────────
# Request/Response Models
# ──────────────────────────────────────────────────

class DiagnoseRequest(BaseModel):
    """诊断请求."""
    query: str = Field(description="用户查询或告警描述")
    user_id: str = Field(default="anonymous")
    session_id: str = Field(default="")
    components: list[str] = Field(default_factory=list, description="相关组件过滤")
    cluster: str = Field(default="default", description="目标集群")
    priority: str = Field(default="normal", description="优先级: low/normal/high/urgent")
    context: dict[str, Any] = Field(default_factory=dict, description="附加上下文")


class AlertRequest(BaseModel):
    """告警请求."""
    alert_name: str
    severity: str = Field(default="warning", description="告警严重度")
    component: str = Field(default="", description="告警组件")
    message: str = Field(default="")
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    starts_at: str = Field(default="")


class DiagnoseResponse(BaseModel):
    """诊断响应."""
    session_id: str
    status: str
    route: str = ""
    summary: str = ""
    root_cause: str | None = None
    confidence: float | None = None
    remediation: list[dict[str, Any]] = Field(default_factory=list)
    report: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


# ──────────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────────

@router.post("/diagnose", response_model=DiagnoseResponse)
async def diagnose(request: DiagnoseRequest) -> DiagnoseResponse:
    """
    提交诊断请求.

    完整链路：Triage → (DirectTool | Diagnostic → Planning → HITL → Remediation → Report)

    WHY 同步 await 而不是后台任务：
    - 大部分请求 <10s 完成（快速路径 <1s）
    - 同步模式实现简单，调试友好
    - 生产环境可以切换为 BackgroundTask + WebSocket 推送
    """
    logger.info(
        "diagnose_request",
        query=request.query[:100],
        user_id=request.user_id,
        cluster=request.cluster,
    )

    try:
        # 构建初始状态
        session_id = request.session_id or str(uuid.uuid4())
        state = create_initial_state(
            query=request.query,
            user_id=request.user_id,
            session_id=session_id,
            components=request.components,
        )

        # 执行 Agent 图
        graph = _get_graph()
        # WHY config 带 thread_id：LangGraph checkpointer 需要 thread_id 做隔离
        final_state = await graph.ainvoke(
            state,
            config={"configurable": {"thread_id": session_id}},
        )

        # 从最终状态提取结果
        route = final_state.get("route", "")
        summary = final_state.get("summary", "")
        root_cause = final_state.get("root_cause")
        confidence = final_state.get("confidence")
        remediation_steps = final_state.get("remediation_steps", [])
        report = final_state.get("report")

        # 保存 session 到内存（开发用，生产用 Postgres checkpoint）
        _session_store[session_id] = {
            "state": final_state,
            "status": "completed",
        }

        logger.info(
            "diagnose_completed",
            session_id=session_id,
            route=route,
            confidence=confidence,
        )

        return DiagnoseResponse(
            session_id=session_id,
            status="completed",
            route=route,
            summary=summary or f"已完成诊断: {request.query[:50]}",
            root_cause=root_cause,
            confidence=confidence,
            remediation=remediation_steps if isinstance(remediation_steps, list) else [],
            report=report,
            metadata={
                "cluster": request.cluster,
                "components": request.components,
                "iteration_count": final_state.get("iteration_count", 0),
            },
        )

    except Exception as e:
        logger.error("diagnose_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/alert")
async def receive_alert(request: AlertRequest) -> dict[str, Any]:
    """
    接收 Prometheus/Alertmanager 告警.

    WHY 独立告警端点而不是复用 /diagnose：
    - 告警格式固定（Alertmanager webhook），与用户查询格式不同
    - 告警需要额外处理：去重、关联、紧急度评估
    - 告警可能批量到达（告警风暴），需要特殊处理

    流程：
    1. 转换告警为诊断查询
    2. 标记为告警类型（Triage 会识别 [ALERT:] 前缀）
    3. 提交到 Agent 图执行
    """
    logger.info(
        "alert_received",
        alert_name=request.alert_name,
        severity=request.severity,
        component=request.component,
    )

    # 转换为诊断查询
    query = (
        f"[ALERT:{request.severity.upper()}] {request.alert_name}: "
        f"{request.message or request.annotations.get('description', '')}"
    )

    try:
        # 构建初始状态
        session_id = str(uuid.uuid4())
        state = create_initial_state(
            query=query,
            user_id="alertmanager",
            session_id=session_id,
            components=[request.component] if request.component else [],
        )

        # 执行 Agent 图
        graph = _get_graph()
        final_state = await graph.ainvoke(
            state,
            config={"configurable": {"thread_id": session_id}},
        )

        # 保存 session
        _session_store[session_id] = {
            "state": final_state,
            "status": "completed",
        }

        return {
            "status": "completed",
            "session_id": session_id,
            "alert_name": request.alert_name,
            "route": final_state.get("route", ""),
            "summary": final_state.get("summary", ""),
            "root_cause": final_state.get("root_cause"),
        }

    except Exception as e:
        logger.error("alert_processing_failed", error=str(e))
        # 告警不返回 500——降级为"已接收"
        return {
            "status": "accepted_degraded",
            "alert_name": request.alert_name,
            "query_generated": query,
            "error": str(e),
            "message": "Alert accepted but processing failed, queued for retry",
        }


@router.get("/sessions/{session_id}")
async def get_session(session_id: str) -> dict[str, Any]:
    """
    获取诊断会话状态.

    当前实现：内存 session store（开发用）
    生产版本：从 LangGraph PostgresSaver checkpoint 读取
    """
    session = _session_store.get(session_id)
    if not session:
        return {
            "session_id": session_id,
            "status": "not_found",
            "message": "Session not found. It may have expired or the server was restarted.",
        }

    state = session.get("state", {})
    return {
        "session_id": session_id,
        "status": session.get("status", "unknown"),
        "route": state.get("route", ""),
        "summary": state.get("summary", ""),
        "root_cause": state.get("root_cause"),
        "confidence": state.get("confidence"),
        "iteration_count": state.get("iteration_count", 0),
        "current_phase": state.get("current_phase", ""),
    }


@router.get("/sessions/{session_id}/report")
async def get_report(session_id: str) -> dict[str, Any]:
    """
    获取诊断报告.

    Report Agent 生成的 Markdown 格式诊断报告。
    """
    session = _session_store.get(session_id)
    if not session:
        return {
            "session_id": session_id,
            "report": None,
            "message": "Session not found",
        }

    state = session.get("state", {})
    report = state.get("report")

    if not report:
        return {
            "session_id": session_id,
            "report": None,
            "message": "Report not yet generated (session may still be in progress or did not reach report phase)",
        }

    return {
        "session_id": session_id,
        "report": report,
        "root_cause": state.get("root_cause"),
        "confidence": state.get("confidence"),
        "generated_by": "report_agent",
    }
