"""公共类型定义测试."""

from __future__ import annotations

from aiops.core.types import (
    AlertInput,
    Component,
    RiskLevel,
    Severity,
    ToolResult,
    UserQuery,
)


class TestAlertInput:
    """AlertInput 测试."""

    def test_minimal_alert(self) -> None:
        """最小化告警应能创建成功."""
        alert = AlertInput(
            alert_id="test-001",
            message="HDFS NameNode heap > 90%",
        )
        assert alert.alert_id == "test-001"
        assert alert.severity == Severity.MEDIUM
        assert alert.component == Component.UNKNOWN
        assert alert.source == "prometheus"

    def test_full_alert(self) -> None:
        """完整告警应正确解析."""
        alert = AlertInput(
            alert_id="test-002",
            source="prometheus",
            severity=Severity.CRITICAL,
            component=Component.HDFS,
            cluster_id="cluster-prod-01",
            message="HDFS NameNode heap > 90%",
            labels={"job": "hdfs-namenode"},
        )
        assert alert.severity == Severity.CRITICAL
        assert alert.component == Component.HDFS
        assert alert.labels["job"] == "hdfs-namenode"


class TestRiskLevel:
    """RiskLevel 测试."""

    def test_risk_level_ordering(self) -> None:
        """风险等级应可比较."""
        assert RiskLevel.NONE < RiskLevel.LOW
        assert RiskLevel.LOW < RiskLevel.MEDIUM
        assert RiskLevel.MEDIUM < RiskLevel.HIGH
        assert RiskLevel.HIGH < RiskLevel.CRITICAL


class TestToolResult:
    """ToolResult 测试."""

    def test_success_result(self) -> None:
        result = ToolResult(
            tool_name="hdfs_cluster_overview",
            success=True,
            data={"total_capacity": "100TB"},
            duration_ms=234.5,
        )
        assert result.success is True
        assert result.error is None

    def test_error_result(self) -> None:
        result = ToolResult(
            tool_name="hdfs_cluster_overview",
            success=False,
            error="Connection refused",
        )
        assert result.success is False
        assert result.error == "Connection refused"
