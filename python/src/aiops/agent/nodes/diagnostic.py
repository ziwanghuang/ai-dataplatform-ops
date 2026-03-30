"""
Diagnostic Agent — 根因分析（假设-验证循环）

核心流程（单轮）：
1. 按诊断计划确定本轮需要执行的工具调用
2. 并行执行工具调用（asyncio.gather，原本串行 5×2s=10s → 并行 max(2s)=2s）
3. 将工具结果 + 历史数据 + RAG 上下文送入 LLM 分析
4. LLM 输出结构化诊断结果（DiagnosticOutput，包含置信度+证据链）
5. 判断：confidence >= 0.6 → 诊断完成，< 0.6 → 标记 data_requirements（触发自环）

WHY 单轮设计 + LangGraph 自环（而不是 process 内部循环）：
- 每轮结束时 LangGraph 自动 Checkpoint，进程重启不丢数据
- 循环内的中间状态如果不被 Checkpoint，HITL 审批超时后无法从断点恢复
- 单轮设计让每次调用的输入/输出清晰可测

WHY 假设-验证（而不是"收集所有数据→一次性分析"）：
- 聚焦性：针对具体假设做验证，不漫无目的采集
- Token 节约：只把与假设相关的数据传给 LLM
- 可解释性：最终输出清晰说明"假设 X 被哪些证据支持/否定"
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

from aiops.agent.base import BaseAgentNode
from aiops.agent.state import AgentState, ToolCallRecord
from aiops.core.logging import get_logger
from aiops.llm.schemas import DiagnosticOutput, EvidenceItem
from aiops.llm.types import LLMCallContext, TaskType

logger = get_logger(__name__)

# ──────────────────────────────────────────────────
# 配置常量
# ──────────────────────────────────────────────────

# 单轮最大工具调用数——防止单轮成本爆炸
# WHY 5：每个工具调用 ~2s，5 个并行 ~2s 可接受；更多会增加 LLM 上下文长度
MAX_TOOLS_PER_ROUND = 5

# 单工具超时——一个工具卡住不应阻塞整体诊断
TOOL_TIMEOUT_SECONDS = 15.0


class DiagnosticNode(BaseAgentNode):
    """
    诊断节点——假设-验证循环.

    接收 Planning Agent 的诊断计划（task_plan + hypotheses），
    按计划并行调用工具采集数据，送入 LLM 做根因分析。
    如果置信度不够（< 0.6），设置 data_requirements 触发自环。
    """

    agent_name = "diagnostic"
    task_type = TaskType.DIAGNOSTIC

    def __init__(self, llm_client: Any | None = None, mcp_client: Any | None = None) -> None:
        super().__init__(llm_client)
        self._mcp = mcp_client

    async def process(self, state: AgentState) -> AgentState:
        """
        诊断主流程（单轮）.

        每执行一次 process()，LangGraph 会做一次 Checkpoint。
        如果 confidence < 0.6，route_from_diagnostic() 会路由回 diagnostic（自环）。
        """
        round_num = state.get("collection_round", 0) + 1
        state["collection_round"] = round_num

        logger.info(
            "diagnostic_round_start",
            round=round_num,
            max_rounds=state.get("max_collection_rounds", 5),
        )

        # ── Step 1: 确定本轮工具调用 ──────────────────
        tools_to_call = self._plan_tool_calls(state, round_num)

        # ── Step 2: 并行执行工具调用 ──────────────────
        if tools_to_call and self._mcp:
            tool_results = await self._execute_tools_parallel(tools_to_call, state)
            # 合并到 collected_data（dict.update，不覆盖之前轮次的数据）
            collected = state.get("collected_data", {})
            collected.update(tool_results)
            state["collected_data"] = collected

        # ── Step 3: LLM 分析 ─────────────────────────
        diagnosis = await self._analyze(state)

        # ── Step 4: 写入 state ────────────────────────
        state["diagnosis"] = {
            "root_cause": diagnosis.root_cause,
            "confidence": diagnosis.confidence,
            "severity": diagnosis.severity,
            "evidence": [e.claim for e in diagnosis.evidence],
            "affected_components": diagnosis.affected_components,
            "causality_chain": diagnosis.causality_chain,
            "related_alerts": [],
        }

        state["remediation_plan"] = [
            {
                "step_number": s.step_number,
                "action": s.action,
                "risk_level": s.risk_level,
                "requires_approval": s.requires_approval,
                "rollback_action": s.rollback_action,
                "estimated_impact": s.estimated_impact,
            }
            for s in diagnosis.remediation_plan
        ]

        # ── Step 5: 判断是否需要更多数据 ──────────────
        #
        # confidence < 0.6 + additional_data_needed 非空 → 设置 data_requirements
        # route_from_diagnostic() 检测到 data_requirements 非空时会路由回 diagnostic（自环）
        #
        # confidence >= 0.6 或没有更多数据可采 → 清空 data_requirements
        # route_from_diagnostic() 检测到 data_requirements 为空时会路由到 report
        if diagnosis.additional_data_needed and diagnosis.confidence < 0.6:
            state["data_requirements"] = diagnosis.additional_data_needed
            logger.info("diagnostic_need_more_data", confidence=diagnosis.confidence)
        else:
            state["data_requirements"] = []  # 清空 → 路由函数认为诊断完成

        logger.info(
            "diagnostic_round_completed",
            round=round_num,
            confidence=diagnosis.confidence,
            severity=diagnosis.severity,
            evidence_count=len(diagnosis.evidence),
        )

        return state

    # ──────────────────────────────────────────────
    # Step 1: 规划工具调用
    # ──────────────────────────────────────────────

    def _plan_tool_calls(self, state: AgentState, round_num: int) -> list[dict[str, Any]]:
        """
        根据诊断计划和当前轮次，确定本轮工具调用.

        第 1 轮：按 task_plan 执行前 N 个步骤的工具
        第 2+ 轮：按上一轮 data_requirements 执行补充调用

        WHY 区分首轮和后续轮：
        - 首轮有完整的诊断计划（Planning Agent 规划的）
        - 后续轮只有 data_requirements（Diagnostic 自己判断还缺什么数据）
        """
        if round_num == 1:
            # 首轮：按诊断计划
            plan = state.get("task_plan", [])
            calls = []
            for step in plan[:MAX_TOOLS_PER_ROUND]:
                # task_plan 格式: {"description": "...", "tools": ["tool1", "tool2"], "parameters": {...}}
                tools = step.get("tools", [])
                for tool_name in tools:
                    calls.append({
                        "name": tool_name,
                        "params": step.get("parameters", {}),
                    })
            return calls[:MAX_TOOLS_PER_ROUND]
        else:
            # 后续轮：按补充需求
            # data_requirements 格式: ["hdfs_namenode_status", "search_logs", ...]
            needed = state.get("data_requirements", [])
            return [{"name": item, "params": {}} for item in needed[:MAX_TOOLS_PER_ROUND]]

    # ──────────────────────────────────────────────
    # Step 2: 并行工具调用
    # ──────────────────────────────────────────────

    async def _execute_tools_parallel(
        self, tools_to_call: list[dict[str, Any]], state: AgentState
    ) -> dict[str, str]:
        """
        并行执行多个 MCP 工具调用.

        性能优化：原本串行 N × 2s = 2Ns → 并行 max(2s) ≈ 2s
        容错设计：每个工具独立超时和错误处理，一个失败不影响其他
        """
        async def call_single(tool_spec: dict[str, Any]) -> tuple[str, str, ToolCallRecord]:
            """执行单个工具调用（带超时和错误隔离）."""
            name = tool_spec["name"]
            params = tool_spec.get("params", {})
            start = time.monotonic()

            try:
                result = await asyncio.wait_for(
                    self._mcp.call_tool(name, params),
                    timeout=TOOL_TIMEOUT_SECONDS,
                )
                duration_ms = int((time.monotonic() - start) * 1000)
                record: ToolCallRecord = {
                    "tool_name": name,
                    "parameters": params,
                    "result": str(result)[:5000],  # 截断防 state 膨胀
                    "duration_ms": duration_ms,
                    "risk_level": "none",
                    "timestamp": datetime.now(UTC).isoformat(),
                    "status": "success",
                }
                return name, str(result), record

            except TimeoutError:
                duration_ms = int((time.monotonic() - start) * 1000)
                error_msg = f"⚠️ 工具 {name} 调用超时 ({TOOL_TIMEOUT_SECONDS}s)"
                record: ToolCallRecord = {
                    "tool_name": name, "parameters": params,
                    "result": error_msg, "duration_ms": duration_ms,
                    "risk_level": "none", "timestamp": datetime.now(UTC).isoformat(),
                    "status": "timeout",
                }
                logger.warning("tool_call_timeout", tool=name)
                return name, error_msg, record

            except Exception as e:
                duration_ms = int((time.monotonic() - start) * 1000)
                error_msg = f"❌ 工具 {name} 调用失败: {e}"
                record: ToolCallRecord = {
                    "tool_name": name, "parameters": params,
                    "result": error_msg, "duration_ms": duration_ms,
                    "risk_level": "none", "timestamp": datetime.now(UTC).isoformat(),
                    "status": "error",
                }
                logger.error("tool_call_error", tool=name, error=str(e))
                return name, error_msg, record

        # asyncio.gather 并行执行所有工具调用
        tasks = [call_single(spec) for spec in tools_to_call]
        results = await asyncio.gather(*tasks)

        # 收集结果
        tool_results: dict[str, str] = {}
        tool_records = state.get("tool_calls", [])
        for name, result_str, record in results:
            tool_results[name] = result_str
            tool_records.append(record)
        state["tool_calls"] = tool_records

        success_count = sum(1 for _, _, r in results if r["status"] == "success")
        logger.info("parallel_tools_completed", total=len(tasks), success=success_count)

        return tool_results

    # ──────────────────────────────────────────────
    # Step 3: LLM 分析
    # ──────────────────────────────────────────────

    async def _analyze(self, state: AgentState) -> DiagnosticOutput:
        """
        将采集的数据送入 LLM 进行根因分析.

        使用 instructor 结构化输出 + 3 次重试。
        失败时降级为低置信度结果（而不是报错中断流程）。
        """
        # 构建上下文信息
        collected_data = state.get("collected_data", {})
        hypotheses = state.get("hypotheses", [])
        rag_context = state.get("rag_context", [])

        # 格式化已采集数据摘要
        data_summary = "\n".join(
            f"[{tool}] {str(data)[:500]}" for tool, data in collected_data.items()
        )

        # 格式化假设列表
        hyp_text = "\n".join(
            f"- 假设 {h.get('id', '?')}: {h.get('description', '')}"
            for h in hypotheses
        ) if hypotheses else "暂无预设假设，请基于数据自行推理。"

        # 格式化 RAG 上下文
        rag_text = "\n".join(
            f"- [{ctx.get('source', '未知')}] {ctx.get('content', '')[:300]}"
            for ctx in rag_context[:5]
        ) if rag_context else "无知识库参考。"

        messages = [
            {
                "role": "system",
                "content": (
                    "你是大数据平台运维专家。基于已采集的监控数据和知识库信息，进行根因分析。\n\n"
                    "五步诊断法（必须遵循）：\n"
                    "1. 症状确认 — 确认报告的症状是否真实存在\n"
                    "2. 范围界定 — 单节点 还是 全局性问题\n"
                    "3. 时间关联 — 确认问题开始时间，关联同期事件\n"
                    "4. 根因分析 — 排除法 + 因果链 + 变更关联\n"
                    "5. 置信度评估 — 低于 0.6 说明需要更多数据\n\n"
                    f"候选假设：\n{hyp_text}\n\n"
                    f"已采集数据：\n{data_summary}\n\n"
                    f"知识库参考：\n{rag_text}\n\n"
                    "要求：\n"
                    "- 每条证据必须引用具体工具返回的数据（防幻觉）\n"
                    "- 因果链用 A→B→C 格式\n"
                    "- 高风险修复建议必须标记 requires_approval=true\n"
                    "- 如果数据不足，在 additional_data_needed 中列出还需要什么\n"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"用户问题：{state.get('user_query', '')}\n\n"
                    f"当前第 {state.get('collection_round', 1)} 轮分析。"
                    f"请基于已采集的数据进行根因分析。"
                ),
            },
        ]

        if self.llm is None:
            return self._fallback_diagnosis(state)

        try:
            context = LLMCallContext(task_type=TaskType.DIAGNOSTIC)
            result = await self.llm.chat_structured(
                messages=messages,
                response_model=DiagnosticOutput,
                context=context,
                max_retries=3,
            )
            return result

        except Exception as e:
            logger.error("diagnostic_llm_failed", error=str(e))
            return self._fallback_diagnosis(state)

    def _fallback_diagnosis(self, state: AgentState) -> DiagnosticOutput:
        """
        降级诊断——LLM 不可用时生成低置信度结果.

        WHY 不直接报错：
        低置信度结果比完全没有结果更有价值。
        Report Agent 会在报告中标注"AI 诊断置信度较低，建议人工复核"。
        """
        return DiagnosticOutput(
            root_cause="LLM 分析不可用，无法确定根因。建议人工排查。",
            confidence=0.1,
            severity="medium",
            evidence=[EvidenceItem(
                claim="自动降级：LLM 调用失败",
                source_tool="system",
                source_data="diagnostic_llm_fallback",
                supports_hypothesis="none",
                confidence_contribution=0.0,
            )],
            causality_chain="数据不足 → 无法推理",
            affected_components=state.get("target_components", []),
            remediation_plan=[],
            additional_data_needed=None,  # 不触发自环，直接出报告
        )
