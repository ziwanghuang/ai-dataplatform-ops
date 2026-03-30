"""
Report Agent — 生成 Markdown 格式的诊断报告 + 知识沉淀

报告结构：
1. 概述（用户问题 + 一句话结论）
2. 诊断过程（调用了哪些工具、发现了什么）
3. 根因分析（因果链 + 证据链 + 置信度）
4. 修复建议（按风险等级排列）
5. 影响范围（受影响组件 + 估计恢复时间）

WHY 用 Markdown 而不是结构化 JSON：
- 运维人员习惯阅读文本报告，不习惯看 JSON
- Markdown 可以直接在企微/钉钉/Slack 中渲染
- 同时保留结构化数据在 state["diagnosis"] 中供程序消费
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aiops.agent.base import BaseAgentNode
from aiops.agent.state import AgentState
from aiops.core.logging import get_logger
from aiops.llm.types import TaskType

logger = get_logger(__name__)


class ReportNode(BaseAgentNode):
    """
    报告节点 — 汇总诊断结果生成可读报告.

    输入：diagnosis + remediation_plan + tool_calls + collected_data
    输出：final_report (Markdown) + knowledge_entry (知识沉淀)
    """

    agent_name = "report"
    task_type = TaskType.REPORT

    async def process(self, state: AgentState) -> AgentState:
        """
        报告生成主流程.

        优先用模板生成（零 Token），LLM 可选用于润色。
        """
        report = self._build_report(state)
        state["final_report"] = report

        # 构建知识沉淀条目
        state["knowledge_entry"] = self._build_knowledge_entry(state)

        logger.info(
            "report_generated",
            report_length=len(report),
            has_remediation=bool(state.get("remediation_plan")),
        )

        return state

    def _build_report(self, state: AgentState) -> str:
        """
        构建 Markdown 报告（模板方式，零 Token）.

        WHY 不用 LLM 生成报告：
        - 报告格式固定，模板就够了
        - 节省 ~2000 Token（Report 是链路最后一步，预算可能已快用完）
        - 模板生成延迟 < 1ms，LLM 需要 1-2s
        """
        diagnosis = state.get("diagnosis", {})
        remediation = state.get("remediation_plan", [])
        tool_calls = state.get("tool_calls", [])
        query = state.get("user_query", "")

        # 置信度标签
        confidence = diagnosis.get("confidence", 0)
        if confidence >= 0.8:
            confidence_label = "🟢 高置信度"
        elif confidence >= 0.6:
            confidence_label = "🟡 中等置信度"
        else:
            confidence_label = "🔴 低置信度（建议人工复核）"

        # 严重度 emoji
        severity_emoji = {
            "critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢", "info": "ℹ️",
        }.get(diagnosis.get("severity", "info"), "ℹ️")

        # ── 报告正文 ──
        sections = []

        # 1. 概述
        sections.append(f"# 🔍 AIOps 诊断报告\n")
        sections.append(f"**请求 ID**: `{state.get('request_id', 'N/A')}`")
        sections.append(f"**时间**: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        sections.append(f"**用户问题**: {query}\n")

        # 2. 诊断结论
        sections.append(f"## 📋 诊断结论\n")
        sections.append(f"- **根因**: {diagnosis.get('root_cause', '未确定')}")
        sections.append(f"- **严重度**: {severity_emoji} {diagnosis.get('severity', 'unknown')}")
        sections.append(f"- **置信度**: {confidence_label} ({confidence:.0%})")
        sections.append(f"- **因果链**: {diagnosis.get('causality_chain', 'N/A')}")
        sections.append(f"- **受影响组件**: {', '.join(diagnosis.get('affected_components', []))}\n")

        # 3. 证据链
        evidence = diagnosis.get("evidence", [])
        if evidence:
            sections.append(f"## 🔗 证据链\n")
            for i, ev in enumerate(evidence, 1):
                sections.append(f"{i}. {ev}")
            sections.append("")

        # 4. 诊断过程
        if tool_calls:
            sections.append(f"## 🛠️ 诊断过程\n")
            sections.append(f"共调用 {len(tool_calls)} 个工具，"
                          f"经过 {state.get('collection_round', 0)} 轮采集：\n")
            for call in tool_calls:
                status_icon = {"success": "✅", "error": "❌", "timeout": "⏰"}.get(
                    call.get("status", ""), "❓"
                )
                sections.append(
                    f"- {status_icon} `{call.get('tool_name', '')}` — "
                    f"{call.get('duration_ms', 0)}ms"
                )
            sections.append("")

        # 5. 修复建议
        if remediation:
            sections.append(f"## 💊 修复建议\n")
            for step in remediation:
                risk_emoji = {"none": "🟢", "low": "🟡", "medium": "🟠",
                            "high": "🔴", "critical": "⛔"}.get(step.get("risk_level", ""), "⚪")
                approval = " ⚠️ **需审批**" if step.get("requires_approval") else ""
                sections.append(
                    f"{step.get('step_number', '?')}. {risk_emoji} {step.get('action', '')}{approval}"
                )
                if step.get("rollback_action"):
                    sections.append(f"   - 回滚方案: {step['rollback_action']}")
                if step.get("estimated_impact"):
                    sections.append(f"   - 预估影响: {step['estimated_impact']}")
            sections.append("")

        # 6. 资源消耗
        sections.append(f"## 📊 资源消耗\n")
        sections.append(f"- Token 消耗: {state.get('total_tokens', 0):,}")
        sections.append(f"- 预估成本: ${state.get('total_cost_usd', 0):.4f}")
        sections.append(f"- 分诊方式: {state.get('_triage_method', 'unknown')}")

        return "\n".join(sections)

    def _build_knowledge_entry(self, state: AgentState) -> dict[str, Any]:
        """
        构建知识沉淀条目——供 KnowledgeSink 写入向量库.

        WHY 知识沉淀：
        - 下次遇到类似问题时，RAG 能检索到这次的诊断经验
        - 形成"诊断→报告→入库→下次可检索"的知识闭环
        """
        diagnosis = state.get("diagnosis", {})
        return {
            "type": "incident_diagnosis",
            "query": state.get("user_query", ""),
            "root_cause": diagnosis.get("root_cause", ""),
            "severity": diagnosis.get("severity", ""),
            "confidence": diagnosis.get("confidence", 0),
            "components": diagnosis.get("affected_components", []),
            "causality_chain": diagnosis.get("causality_chain", ""),
            "remediation_summary": [
                s.get("action", "") for s in state.get("remediation_plan", [])
            ],
            "created_at": datetime.now(UTC).isoformat(),
            "request_id": state.get("request_id", ""),
        }
