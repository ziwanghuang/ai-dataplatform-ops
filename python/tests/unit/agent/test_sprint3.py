"""Sprint 3: Agent 框架 + Triage + Diagnostic 测试."""

from __future__ import annotations

import pytest

from aiops.agent.state import AgentState, create_initial_state, snapshot_state
from aiops.agent.router import route_from_triage, route_from_diagnostic, route_from_hitl
from aiops.agent.nodes.triage import TriageRuleEngine
from aiops.llm.schemas import TriageOutput, DiagnosticOutput, EvidenceItem, RemediationStepSchema


class TestAgentState:
    def test_create_initial_state(self) -> None:
        state = create_initial_state("HDFS 容量多少？")
        assert state["user_query"] == "HDFS 容量多少？"
        assert state["collection_round"] == 0
        assert state["request_id"].startswith("REQ-")

    def test_snapshot_state(self) -> None:
        state = create_initial_state("test")
        state["route"] = "direct_tool"
        snap = snapshot_state(state)
        assert snap["route"] == "direct_tool"


class TestRouting:
    def test_direct_tool(self) -> None:
        assert route_from_triage({"route": "direct_tool"}) == "direct_tool"  # type: ignore

    def test_alert_correlation(self) -> None:
        assert route_from_triage({"route": "alert_correlation", "alerts": []}) == "alert_correlation"  # type: ignore

    def test_diagnosis_default(self) -> None:
        assert route_from_triage({"route": "diagnosis"}) == "planning"  # type: ignore

    def test_diagnostic_max_rounds(self) -> None:
        state = {"collection_round": 5, "max_collection_rounds": 5, "diagnosis": {"confidence": 0.3}}
        assert route_from_diagnostic(state) == "report"  # type: ignore

    def test_diagnostic_need_more_data(self) -> None:
        state = {"collection_round": 1, "max_collection_rounds": 5, "total_tokens": 5000,
                 "diagnosis": {"confidence": 0.4}, "data_requirements": ["hdfs_namenode_status"]}
        assert route_from_diagnostic(state) == "need_more_data"  # type: ignore

    def test_diagnostic_hitl(self) -> None:
        state = {"collection_round": 1, "max_collection_rounds": 5, "total_tokens": 5000,
                 "diagnosis": {"confidence": 0.8}, "data_requirements": [],
                 "remediation_plan": [{"risk_level": "high", "action": "restart"}]}
        assert route_from_diagnostic(state) == "hitl_gate"  # type: ignore

    def test_diagnostic_done(self) -> None:
        state = {"collection_round": 1, "max_collection_rounds": 5, "total_tokens": 5000,
                 "diagnosis": {"confidence": 0.8}, "data_requirements": [], "remediation_plan": []}
        assert route_from_diagnostic(state) == "report"  # type: ignore

    def test_hitl_approved(self) -> None:
        assert route_from_hitl({"hitl_status": "approved"}) == "remediation"  # type: ignore

    def test_hitl_rejected(self) -> None:
        assert route_from_hitl({"hitl_status": "rejected"}) == "report"  # type: ignore


class TestTriageRuleEngine:
    def setup_method(self) -> None:
        self.engine = TriageRuleEngine()

    def test_hdfs_capacity(self) -> None:
        result = self.engine.try_fast_match("HDFS 容量多少？")
        assert result is not None and result[0] == "hdfs_cluster_overview"

    def test_kafka_lag(self) -> None:
        result = self.engine.try_fast_match("Kafka 消费延迟多少")
        assert result is not None and result[0] == "kafka_consumer_lag"

    def test_namenode_status(self) -> None:
        result = self.engine.try_fast_match("NameNode 状态怎么样")
        assert result is not None and result[0] == "hdfs_namenode_status"

    def test_no_match_complex_query(self) -> None:
        assert self.engine.try_fast_match("为什么 HDFS 写入变慢了") is None

    def test_yarn_queue(self) -> None:
        result = self.engine.try_fast_match("队列状态")
        assert result is not None and result[0] == "yarn_queue_status"

    def test_alerts(self) -> None:
        result = self.engine.try_fast_match("当前告警列表")
        assert result is not None and result[0] == "query_alerts"


class TestSchemas:
    def test_triage_output(self) -> None:
        output = TriageOutput(
            intent="status_query", complexity="simple", route="direct_tool",
            summary="查询容量", direct_tool_name="hdfs_cluster_overview",
        )
        assert output.route == "direct_tool"

    def test_high_severity_low_confidence_rejected(self) -> None:
        with pytest.raises(Exception):
            DiagnosticOutput(
                root_cause="test", confidence=0.3, severity="critical",
                evidence=[EvidenceItem(claim="t", source_tool="t", source_data="d",
                                       supports_hypothesis="h", confidence_contribution=0.5)],
                causality_chain="A→B", affected_components=["hdfs"],
            )

    def test_remediation_force_approval(self) -> None:
        step = RemediationStepSchema(
            step_number=1, action="重启 NameNode", risk_level="high",
            requires_approval=False, rollback_action="回滚", estimated_impact="集群影响",
        )
        assert step.requires_approval is True
