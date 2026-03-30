"""
公共类型定义

跨模块共享的 Pydantic 模型和 TypedDict 定义。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Severity(str, Enum):
    """告警严重等级."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class Component(str, Enum):
    """大数据组件类型."""

    HDFS = "hdfs"
    YARN = "yarn"
    KAFKA = "kafka"
    ELASTICSEARCH = "elasticsearch"
    IMPALA = "impala"
    HBASE = "hbase"
    SPARK = "spark"
    FLINK = "flink"
    UNKNOWN = "unknown"


class AlertInput(BaseModel):
    """标准化告警输入."""

    alert_id: str
    source: str = "prometheus"
    severity: Severity = Severity.MEDIUM
    component: Component = Component.UNKNOWN
    cluster_id: str = ""
    message: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)


class UserQuery(BaseModel):
    """用户自然语言查询."""

    query: str
    session_id: str = ""
    user_id: str = ""
    context: dict[str, Any] = Field(default_factory=dict)


class RiskLevel(int, Enum):
    """操作风险等级 (HITL)."""

    NONE = 0       # 纯查询，自动通过
    LOW = 1        # 低风险读操作
    MEDIUM = 2     # 中风险配置变更
    HIGH = 3       # 高风险服务操作
    CRITICAL = 4   # 极高风险，需要双人审批


class ToolResult(BaseModel):
    """MCP 工具调用结果."""

    tool_name: str
    success: bool
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    duration_ms: float = 0.0
    trace_id: str = ""
