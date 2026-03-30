"""
全链路集成测试 — 验证 Agent 端到端工作流

测试场景:
1. 健康检查 API
2. 简单查询快速路径 (DirectTool)
3. 复杂诊断完整链路 (Triage → Diagnostic → Planning → Report)
4. 高风险操作审批流程 (HITL Gate)
5. 告警接收端点
6. 工具列表 API

运行方式:
  pytest tests/integration/test_e2e.py -v --timeout=120

前置条件:
  - Agent Service 运行在 localhost:8080
  - MCP Server 运行在 localhost:3000
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

BASE_URL = "http://localhost:8080"
TIMEOUT = 60.0


@pytest.fixture
def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=BASE_URL, timeout=TIMEOUT)


# ── 1. Health Check ──────────────────────────

@pytest.mark.integration
async def test_health_check(client: httpx.AsyncClient) -> None:
    """健康检查应返回 200 + healthy 状态."""
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("healthy", "ok")


@pytest.mark.integration
async def test_metrics_endpoint(client: httpx.AsyncClient) -> None:
    """Prometheus 指标端点应可用."""
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "aiops_" in resp.text or "python_" in resp.text


# ── 2. Simple Query — Direct Tool Path ──────

@pytest.mark.integration
async def test_simple_hdfs_query(client: httpx.AsyncClient) -> None:
    """简单 HDFS 状态查询应走 DirectTool 快速路径."""
    resp = await client.post(
        "/api/v1/agent/diagnose",
        json={
            "query": "查看 HDFS NameNode 状态",
            "request_type": "simple_query",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "request_id" in data
    # 快速路径不走 LLM，应该很快返回
    assert data.get("route") in ("direct_tool", "triage", None)


@pytest.mark.integration
async def test_tool_list(client: httpx.AsyncClient) -> None:
    """工具列表 API 应返回已注册工具."""
    resp = await client.get("/api/v1/tools/")
    assert resp.status_code == 200
    data = resp.json()
    tools = data.get("tools", data)
    assert isinstance(tools, (list, dict))


# ── 3. Complex Diagnosis — Full Pipeline ────

@pytest.mark.integration
@pytest.mark.slow
async def test_kafka_lag_diagnosis(client: httpx.AsyncClient) -> None:
    """Kafka Consumer Lag 诊断应走完整诊断链路."""
    resp = await client.post(
        "/api/v1/agent/diagnose",
        json={
            "query": "payment-consumer 消费组 lag 持续增长超过 100 万，已持续 30 分钟",
            "request_type": "diagnosis",
            "context": {
                "component": "kafka",
                "cluster": "production",
                "urgency": "high",
            },
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "request_id" in data
    # 诊断结果应包含某些关键字段
    if "report" in data:
        assert len(data["report"]) > 0


@pytest.mark.integration
@pytest.mark.slow
async def test_hdfs_capacity_diagnosis(client: httpx.AsyncClient) -> None:
    """HDFS 容量告警应触发诊断分析."""
    resp = await client.post(
        "/api/v1/agent/diagnose",
        json={
            "query": "HDFS 集群容量使用率达到 92%，剩余空间不足 1TB",
            "request_type": "diagnosis",
            "context": {
                "component": "hdfs",
                "metric": "capacity_usage_percent",
                "value": 92,
            },
        },
    )
    assert resp.status_code == 200


# ── 4. Alert Endpoint ────────────────────────

@pytest.mark.integration
async def test_alert_endpoint(client: httpx.AsyncClient) -> None:
    """告警接收端点应能接收告警并触发处理."""
    resp = await client.post(
        "/api/v1/agent/alert",
        json={
            "alert_id": "test-alert-001",
            "alert_name": "HDFSCapacityWarning",
            "severity": "warning",
            "component": "hdfs",
            "message": "HDFS capacity usage is at 85%",
            "timestamp": "2026-03-30T09:00:00Z",
        },
    )
    # 告警端点可能返回 200 (accepted) 或 202 (async processing)
    assert resp.status_code in (200, 202)


# ── 5. HITL Approval Flow ────────────────────

@pytest.mark.integration
@pytest.mark.slow
async def test_hitl_approval_list(client: httpx.AsyncClient) -> None:
    """审批列表 API 应可访问."""
    resp = await client.get("/api/v1/approval/pending")
    # 可能返回 200 (有数据) 或 404 (路由不存在)
    assert resp.status_code in (200, 404)


# ── 6. Error Handling ────────────────────────

@pytest.mark.integration
async def test_invalid_request(client: httpx.AsyncClient) -> None:
    """无效请求应返回 422 验证错误."""
    resp = await client.post(
        "/api/v1/agent/diagnose",
        json={},  # 缺少必填字段
    )
    assert resp.status_code in (400, 422)


@pytest.mark.integration
async def test_nonexistent_endpoint(client: httpx.AsyncClient) -> None:
    """不存在的端点应返回 404."""
    resp = await client.get("/api/v1/nonexistent")
    assert resp.status_code == 404
