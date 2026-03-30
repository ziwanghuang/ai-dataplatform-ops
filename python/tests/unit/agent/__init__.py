"""Sprint 3: Agent 框架 + Triage + Diagnostic 测试."""

from __future__ import annotations

from aiops.agent.state import AgentState, create_initial_state, snapshot_state
from aiops.agent.router import route_from_triage, route_from_diagnostic, route_from_hitl
from aiops.agent.nodes.triage import TriageRuleEngine
from aiops.llm.schemas import TriageOutput, DiagnosticOutput, EvidenceItem, RemediationStepSchema

import pytest


class TestAgentState:
    def test_create_initial_state(self) -> None:
        state = create_initial_state("HDFS 容量多少？")
        assert state["user_query"] == "HDFS 容量多少？"
        assert state["request_type"] == "user_query"
        assert state["collection_round"] == 0
        assert state["total_tokens"] == 0
        assert state["request_id"].startswith("REQ-")

    def test_snapshot_state(self) -> None:
        state = create_initial_state("test")
        state["current_agent"] = "triage"
        state["route"] = "direct_tool"
        snap = snapshot_state(state)
        assert snap["current_agent"] == "triage"
        assert snap["route"] == "direct_tool"
        assert "collected_data_keys" in snap


class TestRouting:
    def test_route_direct_tool(self) -> None:
        state: AgentState = {"route": "direct_tool"}  # type: ignore[typeddict-item]
        assert route_from_triage(state) == "direct_tool"

    def test_route_alert_correlation(self) -> None:
        state: AgentState = {"route": "alert_correlation", "alerts": [{}, {}]}  # type: ignore[typeddict-item]
        assert route_from_triage(state) == "alert_correlation"

    def test_route_diagnosis_default(self) -> None:
        state: AgentState = {"route": "diagnosis"}  # type: ignore[typeddict-item]
        assert route_from_triage(state) == "planning"

    def test_diagnostic_max_rounds(self) -> None:
        state: AgentState = {"collection_round": 5, "max_collection_rounds": 5, "diagnosis": {"confidence": 0.3}}  # type: ignore
        assert route_from_diagnostic(state) == "report"

    def test_diagnostic_budget_limit(self) -> None:
        state: AgentState = {"collection_round": 1, "max_collection_rounds": 5, "total_tokens": 15000, "diagnosis": {"confidence": 0.3}}  # type: ignore
        assert route_from_diagnostic(state) == "report"

    def test_diagnostic_need_more_data(self) -> None:
        state: AgentState = {"collection_round": 1, "max_collection_rounds": 5, "total_tokens": 5000, "diagnosis": {"confidence": 0.4}, "data_requirements": ["hdfs_namenode_status"]}  # type: ignore
        assert route_from_diagnostic(state) == "need_more_data"

    def test_diagnostic_hitl_required(self) -> None:
        state: AgentState = {"collection_round": 1, "max_collection_rounds": 5, "total_tokens": 5000, "diagnosis": {"confidence": 0.8}, "data_requirements": [], "remediation_plan": [{"risk_level": "high", "action": "restart"}]}  # type: ignore
        assert route_from_diagnostic(state) == "hitl_gate"

    def test_diagnostic_done(self) -> None:
        state: AgentState = {"collection_round": 1, "max_collection_rounds": 5, "total_tokens": 5000, "diagnosis": {"confidence": 0.8}, "data_requirements": [], "remediation_plan": []}  # type: ignore
        assert route_from_diagnostic(state) == "report"

    def test_hitl_approved(self) -> None:
        state: AgentState = {"hitl_status": "approved"}  # type: ignore
        assert route_from_hitl(state) == "remediation"

    def test_hitl_rejected(self) -> None:
        state: AgentState = {"hitl_status": "rejected"}  # type: ignore
        assert route_from_hitl(state) == "report"


class TestTriageRuleEngine:
    def setup_method(self) -> None:
        self.engine = TriageRuleEngine()

    def test_hdfs_capacity(self) -> None:
        result = self.engine.try_fast_match("HDFS 容量多少？")
        assert result is not None
        assert result[0] == "hdfs_cluster_overview"

    def test_kafka_lag(self) -> None:
        result = self.engine.try_fast_match("Kafka 消费延迟多少")
        assert result is not None
        assert result[0] == "kafka_consumer_lag"

    def test_namenode_status(self) -> None:
        result = self.engine.try_fast_match("NameNode 状态怎么样")
        assert result is not None
        assert result[0] == "hdfs_namenode_status"

    def test_es_health(self) -> None:
        result = self.engine.try_fast_match("ES 集群健康状态")
        assert result is not None
        assert result[0] == "es_cluster_health"

    def test_no_match(self) -> None:
        result = self.engine.try_fast_match("为什么 HDFS 写入变慢了")
        assert result is None  # 需要 LLM 分诊，规则引擎不处理

    def test_yarn_queue(self) -> None:
        result = self.engine.try_fast_match("队列状态")
        assert result is not None
        assert result[0] == "yarn_queue_status"

    def test_alerts(self) -> None:
        result = self.engine.try_fast_match("当前告警列表")
        assert result is not None
        assert result[0] == "query_alerts"


class TestSchemas:
    def test_triage_output_valid(self) -> None:
        output = TriageOutput(
            intent="status_query", complexity="simple", route="direct_tool",
            components=["hdfs"], summary="查询 HDFS 容量",
            direct_tool_name="hdfs_cluster_overview",
        )
        assert output.intent == "status_query"

    def test_diagnostic_output_confidence_validation(self) -> None:
        """高严重度 + 低置信度应被拒绝."""
        with pytest.raises(Exception):
            DiagnosticOutput(
                root_cause="test", confidence=0.3, severity="critical",
                evidence=[EvidenceItem(claim="test", source_tool="t", source_data="d", supports_hypothesis="h", confidence_contribution=0.5)],
                causality_chain="A→B", affected_components=["hdfs"],
            )

    def test_remediation_step_force_approval(self) -> None:
        """高风险步骤应强制需要审批."""
        step = RemediationStepSchema(
            step_number=1, action="重启 NameNode", risk_level="high",
            requires_approval=False,  # 故意设为 False
            rollback_action="回滚",
            estimated_impact="影响集群",
        )
        assert step.requires_approval is True  # validator 强制改为 True
