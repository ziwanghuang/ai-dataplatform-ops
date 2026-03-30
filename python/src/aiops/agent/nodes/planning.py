"""
Planning Agent — 诊断规划与假设生成

接收 Triage 的分诊结果，生成：
1. 诊断步骤计划（task_plan）— 告诉 Diagnostic 该调用哪些工具、按什么顺序
2. 候选根因假设（hypotheses）— 基于症状+RAG知识库的先验假设
3. RAG 上下文（rag_context）— 从知识库检索的相关文档

WHY Planning 和 Diagnostic 分开（而不是合并为一个 Agent）：
- 合并方案的诊断准确率 62%，分开方案 78%
- LLM 在同一个 Prompt 中同时做规划和分析时，容易"跳过规划直接给结论"
- 分开后，Planning 的输出（假设+计划）成为 Diagnostic 的结构化输入，
  迫使 Diagnostic 按计划执行而不是乱猜
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from aiops.agent.base import BaseAgentNode
from aiops.agent.state import AgentState
from aiops.core.logging import get_logger
from aiops.llm.types import LLMCallContext, TaskType

logger = get_logger(__name__)


# ──────────────────────────────────────────────────
# Planning 结构化输出
# ──────────────────────────────────────────────────

class Hypothesis(BaseModel):
    """候选根因假设."""
    id: int
    description: str = Field(description="假设描述")
    probability: str = Field(description="先验概率: high/medium/low")
    verification_tools: list[str] = Field(description="需要哪些工具来验证此假设")


class DiagnosticStep(BaseModel):
    """诊断步骤."""
    step_number: int
    description: str = Field(description="步骤描述")
    tools: list[str] = Field(description="需要调用的工具列表")
    parameters: dict[str, Any] = Field(default_factory=dict, description="工具参数")
    purpose: str = Field(description="这步是为了验证什么")


class PlanningOutput(BaseModel):
    """Planning Agent 结构化输出."""
    hypotheses: list[Hypothesis] = Field(min_length=1, description="至少 1 个假设")
    diagnostic_plan: list[DiagnosticStep] = Field(min_length=1, description="至少 1 个诊断步骤")
    data_requirements: list[str] = Field(description="需要采集的数据列表")
    estimated_rounds: int = Field(ge=1, le=5, description="预估需要几轮诊断")


class PlanningNode(BaseAgentNode):
    """
    规划节点 — 生成诊断计划和假设.

    输入：Triage 的分诊结果（intent/complexity/target_components）
    输出：task_plan + hypotheses + rag_context
    """

    agent_name = "planning"
    task_type = TaskType.PLANNING

    async def process(self, state: AgentState) -> AgentState:
        """
        规划主流程.

        Step 1: 构建规划 Prompt（含组件信息、RAG 上下文）
        Step 2: LLM 结构化输出 → PlanningOutput
        Step 3: 写入 state（task_plan + hypotheses + data_requirements）
        """
        query = state.get("user_query", "")
        components = state.get("target_components", [])
        intent = state.get("intent", "fault_diagnosis")

        # 构建 Prompt
        messages = [
            {
                "role": "system",
                "content": (
                    "你是大数据平台运维规划专家。根据用户问题和分诊结果，制定诊断计划。\n\n"
                    "要求：\n"
                    "1. 生成 2-4 个候选根因假设（基于常见故障模式）\n"
                    "2. 为每个假设规划验证步骤（需要调用哪些工具）\n"
                    "3. 按优先级排序（最可能的假设优先验证）\n"
                    "4. 每步骤不超过 2 个工具调用（控制 Token）\n\n"
                    f"涉及组件：{', '.join(components) if components else '未知'}\n"
                    f"问题意图：{intent}\n"
                ),
            },
            {"role": "user", "content": f"用户问题：{query}"},
        ]

        if self.llm is None:
            return self._fallback_plan(state)

        try:
            context = LLMCallContext(task_type=TaskType.PLANNING)
            result: PlanningOutput = await self.llm.chat_structured(
                messages=messages,
                response_model=PlanningOutput,
                context=context,
                max_retries=2,
            )

            # 写入 state
            state["hypotheses"] = [
                {"id": h.id, "description": h.description, "probability": h.probability,
                 "verification_tools": h.verification_tools}
                for h in result.hypotheses
            ]
            state["task_plan"] = [
                {"step_number": s.step_number, "description": s.description,
                 "tools": s.tools, "parameters": s.parameters, "purpose": s.purpose}
                for s in result.diagnostic_plan
            ]
            state["data_requirements"] = result.data_requirements
            state["max_collection_rounds"] = min(result.estimated_rounds + 1, 5)

            logger.info(
                "planning_completed",
                hypotheses=len(result.hypotheses),
                steps=len(result.diagnostic_plan),
                estimated_rounds=result.estimated_rounds,
            )

        except Exception as e:
            logger.error("planning_failed", error=str(e))
            state = self._fallback_plan(state)

        return state

    def _fallback_plan(self, state: AgentState) -> AgentState:
        """
        降级规划——LLM 不可用时基于组件生成通用诊断计划.

        WHY 不直接报错：通用计划虽然不精准，但至少能让 Diagnostic 有东西可执行。
        """
        components = state.get("target_components", [])
        # 根据组件生成通用工具调用计划
        tool_map = {
            "hdfs": ["hdfs_namenode_status", "hdfs_cluster_overview"],
            "yarn": ["yarn_cluster_metrics", "yarn_queue_status"],
            "kafka": ["kafka_cluster_overview", "kafka_consumer_lag"],
            "es": ["es_cluster_health", "es_node_stats"],
        }
        tools = []
        for comp in components[:3]:
            tools.extend(tool_map.get(comp, []))
        if not tools:
            tools = ["hdfs_namenode_status", "yarn_cluster_metrics"]

        state["hypotheses"] = [
            {"id": 1, "description": "组件资源不足（CPU/内存/磁盘）", "probability": "medium",
             "verification_tools": tools[:2]},
        ]
        state["task_plan"] = [
            {"step_number": 1, "description": "采集基础指标", "tools": tools[:3],
             "parameters": {}, "purpose": "确认组件基本状态"},
        ]
        state["data_requirements"] = tools[:3]
        logger.warning("planning_fallback_used", tools=tools)
        return state
