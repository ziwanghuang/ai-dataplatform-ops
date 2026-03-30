"""全局 pytest fixture."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from aiops.core.config import Environment, Settings


@pytest.fixture
def test_settings() -> Settings:
    """测试用配置（覆盖敏感值）."""
    return Settings(
        env=Environment.DEV,
        debug=True,
        llm={"openai_api_key": "test-key", "primary_model": "gpt-4o-mini"},
        db={"postgres_url": "postgresql+asyncpg://test:test@localhost:5432/test"},
    )


@pytest.fixture
def mock_llm_client() -> AsyncMock:
    """Mock LLM 客户端."""
    client = AsyncMock()
    client.chat.return_value = "Mock LLM response"
    client.chat_structured.return_value = {"intent": "diagnosis", "urgency": "high"}
    return client


@pytest.fixture
def mock_mcp_client() -> AsyncMock:
    """Mock MCP 客户端."""
    client = AsyncMock()
    client.call_tool.return_value = {"status": "success", "data": {}}
    return client


@pytest.fixture
def sample_alert() -> dict[str, Any]:
    """标准告警样本 — 多个测试复用."""
    return {
        "alert_id": "alert-20260329-001",
        "source": "prometheus",
        "severity": "critical",
        "component": "hdfs",
        "cluster_id": "cluster-prod-01",
        "message": "HDFS NameNode heap usage > 90%",
        "timestamp": "2026-03-29T10:15:00Z",
        "labels": {"job": "hdfs-namenode", "instance": "nn01:9870"},
    }
