"""
LLM 结构化输出 Schema 定义

这些 Pydantic 模型通过 instructor 库控制 LLM 的输出格式。
每个模型的 field_validator 是防幻觉的关键防线——LLM 可能输出"高严重度+低置信度"
的矛盾结论，validator 会自动拒绝并要求 LLM 重试。

WHY Pydantic BaseModel 而不是 TypedDict：
instructor 库要求 Pydantic BaseModel 做结构化输出解析，
它用 JSON Schema 生成 LLM 的输出约束，再用 validator 解析验证。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


# ──────────────────────────────────────────────────
# Triage Agent 输出
# ──────────────────────────────────────────────────

class TriageOutput(BaseModel):
    """
    Triage Agent 结构化输出.

    intent × complexity × route 三维组合决定了请求的完整处理路径。
    direct_tool_name/params 仅在 route=direct_tool 时填充。
    """

    intent: Literal[
        "status_query",       # 单组件状态查询（走快速路径）
        "health_check",       # 整体健康检查
        "fault_diagnosis",    # 故障诊断（走完整流程）
        "capacity_planning",  # 容量规划
        "alert_handling",     # 告警处理
    ]

    complexity: Literal["simple", "moderate", "complex"]

    # 路由决策——Triage 判断，route_from_triage() 分发
    # WHY 抽象层：如果让 LLM 直接输出节点名，节点名重构时所有 Prompt 都要改
    route: Literal["direct_tool", "diagnosis", "alert_correlation"]

    components: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="涉及的大数据组件: hdfs/yarn/kafka/es/impala/hbase/zk",
    )

    cluster: str = Field(default="", description="目标集群标识")

    urgency: Literal["critical", "high", "medium", "low"] = "medium"

    summary: str = Field(max_length=500, description="一句话概括问题")

    # 快速路径字段（仅 route=direct_tool 时填充）
    direct_tool_name: str | None = Field(
        default=None,
        description="如果 route=direct_tool，指定要调用的工具名",
    )
    direct_tool_params: dict | None = Field(
        default=None,
        description="如果 route=direct_tool，指定工具参数",
    )


# ──────────────────────────────────────────────────
# Diagnostic Agent 输出
# ──────────────────────────────────────────────────

class EvidenceItem(BaseModel):
    """
    单条证据——诊断结论的数据支撑.

    WHY 每条证据必须有 source_tool 和 source_data：
    - 防幻觉：HallucinationDetector 会交叉验证 source_tool 是否在 collected_data 中
    - 可溯源：运维人员可以追溯到具体哪个工具返回了什么数据
    """

    claim: str = Field(description="证据描述")
    source_tool: str = Field(description="数据来源工具名")
    source_data: str = Field(description="原始数据摘要（必须是工具实际返回的）")
    supports_hypothesis: str = Field(description="支持/反驳哪个假设")
    confidence_contribution: float = Field(
        ge=-1.0, le=1.0,
        description="对整体置信度的贡献（正=支持，负=反驳）",
    )


class RemediationStepSchema(BaseModel):
    """
    修复步骤（含强制安全约束）.

    field_validator 是代码层的安全兜底——即使 LLM 忘了标记审批，
    代码也会强制高风险操作需要审批。
    """

    step_number: int
    action: str = Field(description="操作描述")
    risk_level: Literal["none", "low", "medium", "high", "critical"]
    requires_approval: bool
    rollback_action: str = Field(description="回滚方案（必填）")
    estimated_impact: str = Field(description="预估影响范围和时间")

    @field_validator("requires_approval", mode="after")
    @classmethod
    def enforce_approval_for_high_risk(cls, v: bool, info: Any) -> bool:
        """高风险操作强制需要审批——代码层安全兜底，LLM 忘了也没关系."""
        data = info.data if hasattr(info, "data") else {}
        risk = data.get("risk_level", "none")
        if risk in ("high", "critical"):
            return True
        return v

    @field_validator("rollback_action", mode="after")
    @classmethod
    def enforce_rollback_plan(cls, v: str, info: Any) -> str:
        """非只读操作必须有回滚方案."""
        data = info.data if hasattr(info, "data") else {}
        risk = data.get("risk_level", "none")
        if risk != "none" and (not v or v.strip() == ""):
            return "⚠️ 未指定回滚方案，执行前需人工确认回滚策略"
        return v


class DiagnosticOutput(BaseModel):
    """
    Diagnostic Agent 完整输出.

    这是整个系统最核心的数据结构——根因分析的结构化结论。
    每个字段都经过精心设计以确保可解释性和安全性。
    """

    # 根因
    root_cause: str = Field(description="根因描述（必须引用具体数据）")
    confidence: float = Field(ge=0.0, le=1.0, description="置信度 0-1")
    severity: Literal["critical", "high", "medium", "low", "info"]

    # 证据链（至少 1 条证据——没有证据的结论是幻觉）
    evidence: list[EvidenceItem] = Field(min_length=1, description="至少 1 条证据")

    # 因果分析
    causality_chain: str = Field(
        description="因果推理链（A→B→C 格式）",
        examples=["NN 堆内存不足 → Full GC 频繁 → RPC 处理阻塞 → DN 心跳超时"],
    )
    affected_components: list[str]

    # 修复建议
    remediation_plan: list[RemediationStepSchema] = Field(default_factory=list)

    # 是否需要更多数据（非空 + confidence < 0.6 → 触发自环）
    additional_data_needed: list[str] | None = Field(
        default=None,
        description="如果置信度 < 0.6，列出还需要采集的数据",
    )

    @model_validator(mode="after")
    def validate_confidence_severity(self) -> DiagnosticOutput:
        """
        高严重度必须有较高置信度.

        WHY：防止 LLM 输出"critical 但置信度 0.3"的矛盾结论。
        用 model_validator 而不是 field_validator，因为需要同时访问
        severity 和 confidence 两个字段（field_validator 执行时其他字段可能还没赋值）。
        """
        if self.severity in ("critical", "high") and self.confidence < 0.5:
            raise ValueError(
                f"严重度为 {self.severity} 但置信度仅 {self.confidence:.2f}，需要更多证据支撑"
            )
        return self

    @field_validator("evidence")
    @classmethod
    def validate_evidence_has_source(cls, v: list[EvidenceItem]) -> list[EvidenceItem]:
        """每条证据必须有数据来源——没有来源的"证据"可能是幻觉."""
        for item in v:
            if not item.source_tool or not item.source_data:
                raise ValueError(f"证据 '{item.claim}' 缺少数据来源，可能是幻觉")
        return v
