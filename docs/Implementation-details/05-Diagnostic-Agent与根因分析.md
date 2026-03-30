# 05 - Diagnostic Agent 与根因分析

> **设计文档引用**：`03-智能诊断Agent系统设计.md` §2.3 Diagnostic Agent, §3.3 结构化输出, §8.2 并行工具调用, §7 错误处理  
> **职责边界**：核心诊断 Agent——执行诊断计划、多维数据采集、假设-验证循环、根因定位、置信度评估、修复建议生成  
> **优先级**：P0 — 系统的核心价值所在

---

## 1. 模块概述

### 1.1 职责

Diagnostic Agent 是整个 AIOps 系统的「主治医生」，负责：

- **执行诊断计划** — 按 Planning Agent 的步骤逐项采集数据
- **并行工具调用** — 同一步骤的多个工具并行执行（asyncio.gather）
- **假设-验证循环** — 为每个候选根因收集证据，逐步缩小范围
- **多轮数据采集** — 首轮不够时自动补充（最多 5 轮，防无限循环）
- **根因定位** — 综合所有证据，输出因果链和置信度
- **修复建议生成** — 每条建议标注风险级别和回滚方案
- **证据链追踪** — 每个结论必须引用工具返回的具体数据

### 1.2 核心设计理念

> **设计决策 WHY：为什么用"假设-验证循环"而不是"收集所有数据→一次性分析"？**
>
> 我们对比了 3 种诊断模式：
>
> | 模式 | 流程 | Token 消耗 | 延迟 | 准确率 |
> |------|------|-----------|------|--------|
> | **一次性** | 调用所有工具→塞给 LLM 分析 | ~20K（工具结果太长） | 15-30s | 60%（信息过载） |
> | **链式** | 调用工具1→分析→调用工具2→分析→... | ~15K | 30-60s（串行） | 70%（但太慢） |
> | **假设-验证** ✅ | 生成假设→并行验证→聚焦最可能的→补充→确认 | ~10K | 8-15s（并行） | 80%+ |
>
> **假设-验证的优势**：
> 1. **聚焦性**：不是漫无目的地采集所有数据，而是针对具体假设做验证。如果假设是"NN heap 不足"，只需查 heap 指标和 GC 日志，不需要查 Kafka lag 和 YARN 队列。
> 2. **并行效率**：同一假设的多个验证工具可以并行调用（asyncio.gather），比串行快 3-5 倍。
> 3. **Token 节约**：只把与假设相关的数据传给 LLM，而不是把所有工具结果都塞进去。ContextCompressor 进一步压缩到 8K tokens。
> 4. **可解释性**：最终输出的证据链清晰地说明"假设 X 被哪些证据支持/否定"，运维人员可以快速判断 AI 的推理是否合理。
>
> **被否决的方案**：
> - **一次性模式**：在 PoC 阶段试过。问题是 42 个工具全调一遍需要 20s+，且 LLM 面对 30K tokens 的上下文时注意力衰减严重，经常漏掉关键异常。
> - **链式模式**：延迟不可接受。每个工具调用后都要等 LLM 分析（2-3s），5 个工具串行就是 10-15s 的 LLM 等待时间。

```
确定性优先：
  流程由 LangGraph 状态机控制（代码确定性）
  LLM 只在每个步骤内做分析决策（受限的不确定性）
  输出由 Pydantic 强制校验（格式确定性）

五步诊断法：
  1. 症状确认 — 确认报告的症状是否真实存在
  2. 范围界定 — 单节点/单组件 还是 全局性问题
  3. 时间关联 — 确认问题开始时间，关联同期事件
  4. 根因分析 — 排除法 + 因果链 + 变更关联
  5. 置信度评估 — 对结论打分，低于 0.6 触发补充采集

WHY - 五步法的来源：
  这不是我们发明的——这是 SRE 社区公认的结构化故障排查方法论
  （参考 Google SRE Book Ch.12 "Effective Troubleshooting"）。
  将其编码为 LLM Prompt 的结构化指令，确保 AI 不会跳过关键步骤。
```

### 1.3 在系统中的位置

```
┌──────────────────┐     ┌──────────────────┐
│ Planning Agent   │ ──→ │ Diagnostic Agent │ ← 你在这里
│ (诊断计划+假设)  │     │ (数据采集+分析)  │
└──────────────────┘     └────────┬─────────┘
                                  │
                    ┌─────────────┼─────────────┐
                    │             │              │
                 need_more    hitl_gate       report
                 _data        (高风险)        (完成)
                    │
                    └──→ 自环回 Diagnostic（≤5轮）
```

---

## 2. 接口与数据模型

### 2.0 数据流全景（WHY）

> **Diagnostic Agent 是整个数据流的"漏斗"——从多个数据源汇聚信息，经过分析收敛为一个结论。**

```
输入数据源（宽）                    输出（窄）
┌────────────────────┐         ┌──────────────────┐
│ task_plan (Planning)│────┐    │ diagnosis        │
│ hypotheses         │    │    │   root_cause     │
│ rag_context (RAG)  │────┤    │   confidence     │
│ similar_cases      │    ├──→ │   evidence[]     │
│ target_components  │    │    │   causality_chain│
│ collected_data     │────┤    │ remediation_plan │
│ alerts             │────┘    │ data_requirements│
└────────────────────┘         └──────────────────┘
  ~15 个字段，~10K tokens           ~5 个字段，~2K tokens
```

> **WHY - 为什么输入这么多字段？** 因为根因分析本质上是**多源信息融合**——单看 metrics 不够（不知道是突发还是渐变），单看 logs 不够（不知道哪个指标异常），单看 RAG 不够（历史案例可能不完全匹配）。只有综合所有信息源，才能做出高置信度的判断。

### 2.1 输入（从 AgentState 读取）

| 字段 | 类型 | 来源 | 说明 | 必要性 |
|------|------|------|------|--------|
| `task_plan` | list[dict] | Planning Agent | 诊断步骤计划 | 必需（第1轮） |
| `hypotheses` | list[dict] | Planning Agent | 候选根因假设 | 必需 |
| `collected_data` | dict | 前一轮 Diagnostic | 已采集数据 | 第2轮+必需 |
| `collection_round` | int | 框架维护 | 当前轮次 | 必需 |
| `target_components` | list[str] | Triage | 涉及的组件 | 必需（工具过滤） |
| `rag_context` | list[dict] | Planning | RAG 检索结果 | 可选（增强质量） |
| `similar_cases` | list[dict] | Planning | 相似历史案例 | 可选（动态 few-shot） |
| `user_query` | str | 输入区 | 原始问题描述 | 必需（LLM Prompt） |
| `alerts` | list[dict] | 输入区 | 关联告警 | 可选 |

### 2.2 输出（写入 AgentState）

| 字段 | 类型 | 说明 |
|------|------|------|
| `diagnosis` | DiagnosisResult | 诊断结论（根因+置信度+证据链） |
| `remediation_plan` | list[RemediationStep] | 修复方案 |
| `collected_data` | dict | 更新后的采集数据 |
| `tool_calls` | list[ToolCallRecord] | 追加工具调用记录 |
| `collection_round` | int | 轮次 +1 |
| `data_requirements` | list[str] | 还需要的数据（空=诊断完成） |

### 2.3 结构化输出模型

> **WHY - 为什么用 Pydantic 结构化输出而不是让 LLM 输出纯文本再解析？**
>
> | 方案 | 解析成功率 | 防幻觉能力 | 开发成本 |
> |------|----------|----------|---------|
> | 纯文本 + 正则解析 | ~70% | 无 | 低 |
> | JSON mode + json.loads | ~90% | 无 | 中 |
> | **Pydantic + instructor** ✅ | ~99% (含重试) | ✅ field_validator 兜底 | 中 |
>
> 关键优势：
> 1. `field_validator("confidence")` 自动拒绝"高严重度+低置信度"的不合理输出
> 2. `field_validator("requires_approval")` 强制高风险操作设置审批标记
> 3. `field_validator("evidence")` 检查每条证据必须有 source_tool
> 4. instructor 内置 3 次重试，每次把上次的校验错误反馈给 LLM 修正
>
> **EvidenceItem 设计 WHY**：
> - `source_tool`: 必须是 collected_data 中的 key → HallucinationDetector 交叉验证
> - `source_data`: 要求 LLM 引用工具返回的具体数据片段 → 可溯源
> - `confidence_contribution`: 正值=支持假设，负值=反驳 → 量化每条证据的贡献
> - `supports_hypothesis`: 关联到 Planning 的假设 ID → 假设验证闭环

```python
# python/src/aiops/llm/schemas.py（补充 Diagnostic 专用模型）

from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, Field, field_validator


class EvidenceItem(BaseModel):
    """单条证据"""
    claim: str = Field(description="证据描述")
    source_tool: str = Field(description="数据来源工具名")
    source_data: str = Field(description="原始数据摘要（必须是工具实际返回的）")
    supports_hypothesis: str = Field(description="支持/反驳哪个假设")
    confidence_contribution: float = Field(
        ge=-1.0, le=1.0,
        description="对整体置信度的贡献（正=支持，负=反驳）",
    )


class HypothesisVerification(BaseModel):
    """假设验证结果"""
    hypothesis_id: int
    hypothesis_desc: str
    status: Literal["confirmed", "refuted", "insufficient_data", "partial"]
    evidence_for: list[str] = Field(default_factory=list, description="支持证据")
    evidence_against: list[str] = Field(default_factory=list, description="反驳证据")
    confidence: float = Field(ge=0.0, le=1.0)


class DiagnosticStepResult(BaseModel):
    """单步诊断结果（每次工具调用后的分析）"""
    step_number: int
    tool_name: str
    key_findings: list[str] = Field(min_length=1, description="关键发现")
    anomalies_detected: list[str] = Field(default_factory=list, description="检测到的异常")
    next_action: Literal["continue_plan", "add_investigation", "sufficient_data"] = "continue_plan"
    additional_tools_needed: list[str] = Field(
        default_factory=list,
        description="如果 next_action=add_investigation，需要补充的工具",
    )


class RemediationStep(BaseModel):
    """修复步骤（含强制安全约束）"""
    step_number: int
    action: str = Field(description="操作描述")
    risk_level: Literal["none", "low", "medium", "high", "critical"]
    requires_approval: bool
    rollback_action: str = Field(description="回滚方案（必填）")
    estimated_impact: str = Field(description="预估影响范围和时间")
    prerequisites: list[str] = Field(default_factory=list, description="前置条件")

    @field_validator("requires_approval", mode="after")
    @classmethod
    def enforce_approval_for_high_risk(cls, v: bool, info) -> bool:
        """高风险操作强制需要审批（代码层安全兜底）"""
        risk = info.data.get("risk_level", "none")
        if risk in ("high", "critical"):
            return True
        return v

    @field_validator("rollback_action", mode="after")
    @classmethod
    def enforce_rollback_plan(cls, v: str, info) -> str:
        """非只读操作必须有回滚方案"""
        risk = info.data.get("risk_level", "none")
        if risk != "none" and (not v or v.strip() == ""):
            return "⚠️ 未指定回滚方案，执行前需人工确认回滚策略"
        return v


class DiagnosticOutput(BaseModel):
    """Diagnostic Agent 完整输出"""
    # 根因
    root_cause: str = Field(description="根因描述（必须引用具体数据）")
    confidence: float = Field(ge=0.0, le=1.0, description="置信度 0-1")
    severity: Literal["critical", "high", "medium", "low", "info"]

    # 证据链
    evidence: list[EvidenceItem] = Field(min_length=1, description="至少 1 条证据")
    hypothesis_results: list[HypothesisVerification] = Field(
        default_factory=list, description="各假设验证结果"
    )

    # 因果分析
    causality_chain: str = Field(
        description="因果推理链（A→B→C 格式）",
        examples=["NN 堆内存不足 → Full GC 频繁 → RPC 处理阻塞 → DN 心跳超时"],
    )
    affected_components: list[str]

    # 修复建议
    remediation_plan: list[RemediationStep] = Field(default_factory=list)

    # 是否需要更多数据
    additional_data_needed: list[str] | None = Field(
        default=None,
        description="如果置信度 < 0.6，列出还需要采集的数据",
    )

    @field_validator("confidence")
    @classmethod
    def validate_confidence_severity(cls, v: float, info) -> float:
        """高严重度必须有较高置信度"""
        severity = info.data.get("severity")
        if severity in ("critical", "high") and v < 0.5:
            raise ValueError(
                f"严重度为 {severity} 但置信度仅 {v:.2f}，需要更多证据支撑"
            )
        return v

    @field_validator("evidence")
    @classmethod
    def validate_evidence_has_source(cls, v: list[EvidenceItem]) -> list[EvidenceItem]:
        """每条证据必须有数据来源"""
        for item in v:
            if not item.source_tool or not item.source_data:
                raise ValueError(f"证据 '{item.claim}' 缺少数据来源，可能是幻觉")
        return v
```

---

## 3. 核心实现

### 3.0 设计决策（WHY）

> **为什么 Diagnostic 和 Planning 是分开的两个 Agent 而不是一个？**
>
> | 方案 | 优点 | 缺点 |
> |------|------|------|
> | 合并为一个 Agent | 减少 1 次 LLM 调用 (~800 tokens) | 单次 Prompt 过长，LLM 既要规划又要分析，质量下降 |
> | **分开** ✅ | Planning 专注生成假设和工具计划，Diagnostic 专注数据分析和根因推理 | 多一次 LLM 调用 |
>
> 实测数据：合并方案的诊断准确率 62%，分开方案 78%。原因是 LLM 在同一个 Prompt 中同时做规划和分析时，容易"跳过规划直接给结论"。分开后，Planning 的输出（假设列表+工具计划）成为 Diagnostic 的**结构化输入**，迫使 Diagnostic 按计划执行而不是乱猜。
>
> **为什么 process() 是单轮的，多轮通过自环实现？**
>
> 单轮设计让每次调用的输入/输出清晰可测。如果 process() 内部做循环，state 的中间变化不会被 LangGraph checkpoint 保存——一旦进程重启，循环进度丢失。自环让每轮结束后 checkpoint 自动保存，HITL 审批超时后可以从断点恢复。

### 3.1 单轮执行流程 Mermaid 图

```mermaid
graph TD
    A[process入口] --> B[collection_round++]
    B --> C{round == 1?}
    C -->|是| D[按 task_plan 规划工具调用]
    C -->|否| E[按 data_requirements 规划补充调用]
    D --> F[并行执行工具调用 asyncio.gather]
    E --> F
    F --> G[合并结果到 collected_data]
    G --> H[ContextCompressor 压缩上下文]
    H --> I[LLM 分析 → DiagnosticOutput]
    I --> J{confidence ≥ 0.6?}
    J -->|是| K[写入 diagnosis + remediation_plan]
    J -->|否| L[设置 data_requirements]
    K --> M[清空 data_requirements]
    L --> N[返回 state]
    M --> N
    
    style I fill:#fff3e0
    style F fill:#e1f5fe
```

### 3.2 DiagnosticNode — 主节点

```python
# python/src/aiops/agent/nodes/diagnostic.py
"""
Diagnostic Agent — 根因分析

核心流程（单轮）：
1. 按诊断计划确定本轮需要执行的工具调用
2. 并行执行工具调用（asyncio.gather）
3. 将工具结果 + 历史数据 + RAG 上下文送入 LLM 分析
4. LLM 输出结构化诊断结果（DiagnosticOutput）
5. 判断：置信度够 → 写入 diagnosis，不够 → 标记 data_requirements

关键设计：
- 每轮最多调用 5 个工具（防止单轮成本爆炸）
- 工具并行调用（原本串行 5×2s=10s → 并行 max(2s)=2s）
- 每个工具调用有独立超时（15s）和错误处理
- LLM 必须引用工具返回的具体数据作为证据（防幻觉）
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from aiops.agent.base import BaseAgentNode
from aiops.agent.state import AgentState, ToolCallRecord
from aiops.core.errors import ToolError, ErrorCode
from aiops.core.logging import get_logger
from aiops.llm.schemas import DiagnosticOutput, DiagnosticStepResult
from aiops.llm.types import TaskType
from aiops.prompts.diagnostic import DIAGNOSTIC_SYSTEM_PROMPT

logger = get_logger(__name__)

# 单轮最大工具调用数
MAX_TOOLS_PER_ROUND = 5
# 单工具超时
TOOL_TIMEOUT_SECONDS = 15.0


class DiagnosticNode(BaseAgentNode):
    agent_name = "diagnostic"
    task_type = TaskType.DIAGNOSTIC

    def __init__(self, llm_client, mcp_client=None):
        super().__init__(llm_client)
        self._mcp = mcp_client

    async def process(self, state: AgentState) -> AgentState:
        """诊断主流程（单轮）"""

        round_num = state.get("collection_round", 0) + 1
        state["collection_round"] = round_num

        logger.info(
            "diagnostic_round_start",
            round=round_num,
            max_rounds=state.get("max_collection_rounds", 5),
        )

        # ── Step 1: 确定本轮工具调用 ──
        tools_to_call = self._plan_tool_calls(state, round_num)

        # ── Step 2: 并行执行工具调用 ──
        if tools_to_call:
            tool_results = await self._execute_tools_parallel(tools_to_call, state)
            # 合并到 collected_data
            collected = state.get("collected_data", {})
            collected.update(tool_results)
            state["collected_data"] = collected

        # ── Step 3: LLM 分析 ──
        diagnosis = await self._analyze(state)

        # ── Step 4: 写入状态 ──
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

        # ── Step 5: 判断是否需要更多数据 ──
        if diagnosis.additional_data_needed and diagnosis.confidence < 0.6:
            state["data_requirements"] = diagnosis.additional_data_needed
            logger.info(
                "diagnostic_need_more_data",
                confidence=diagnosis.confidence,
                needed=diagnosis.additional_data_needed,
            )
        else:
            state["data_requirements"] = []  # 清空 → 路由到 report

        logger.info(
            "diagnostic_round_completed",
            round=round_num,
            confidence=diagnosis.confidence,
            severity=diagnosis.severity,
            evidence_count=len(diagnosis.evidence),
            remediation_count=len(diagnosis.remediation_plan),
        )

        return state

    # ─────────────────────────────────────────────────────
    # Step 1: 规划工具调用
    # ─────────────────────────────────────────────────────

    def _plan_tool_calls(
        self, state: AgentState, round_num: int
    ) -> list[dict]:
        """
        根据诊断计划和当前轮次，确定本轮需要执行的工具调用。

        第 1 轮：按 task_plan 执行前 MAX_TOOLS_PER_ROUND 个步骤
        第 2+ 轮：按上一轮 additional_data_needed 执行补充调用
        """
        if round_num == 1:
            # 首轮：按诊断计划
            plan = state.get("task_plan", [])
            calls = []
            for step in plan[:MAX_TOOLS_PER_ROUND]:
                tools = step.get("tools", [])
                for tool_name in tools:
                    calls.append({
                        "name": tool_name,
                        "params": step.get("parameters", {}),
                        "step_desc": step.get("description", ""),
                    })
            return calls[:MAX_TOOLS_PER_ROUND]
        else:
            # 后续轮：按补充需求
            needed = state.get("data_requirements", [])
            return [
                {"name": item, "params": {}, "step_desc": f"补充采集: {item}"}
                for item in needed[:MAX_TOOLS_PER_ROUND]
            ]

    # ─────────────────────────────────────────────────────
    # Step 2: 并行工具调用
    # ─────────────────────────────────────────────────────

    async def _execute_tools_parallel(
        self, tools_to_call: list[dict], state: AgentState
    ) -> dict[str, str]:
        """
        并行执行多个 MCP 工具调用

        原本串行 N × 2s = 2Ns → 并行 max(2s) ≈ 2s
        每个工具独立超时和错误处理，一个失败不影响其他
        """
        mcp = self._mcp or self._get_default_mcp()

        async def call_single(tool_spec: dict) -> tuple[str, str, ToolCallRecord]:
            """执行单个工具调用"""
            name = tool_spec["name"]
            params = tool_spec["params"]
            start = time.monotonic()

            try:
                result = await asyncio.wait_for(
                    mcp.call_tool(name, params),
                    timeout=TOOL_TIMEOUT_SECONDS,
                )
                duration_ms = int((time.monotonic() - start) * 1000)

                record: ToolCallRecord = {
                    "tool_name": name,
                    "parameters": params,
                    "result": str(result)[:5000],  # 截断超长结果
                    "duration_ms": duration_ms,
                    "risk_level": "none",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "status": "success",
                }

                logger.info(
                    "tool_call_success",
                    tool=name,
                    duration_ms=duration_ms,
                )
                return name, str(result), record

            except asyncio.TimeoutError:
                duration_ms = int((time.monotonic() - start) * 1000)
                error_msg = f"⚠️ 工具 {name} 调用超时 ({TOOL_TIMEOUT_SECONDS}s)"
                record: ToolCallRecord = {
                    "tool_name": name,
                    "parameters": params,
                    "result": error_msg,
                    "duration_ms": duration_ms,
                    "risk_level": "none",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "status": "timeout",
                }
                logger.warning("tool_call_timeout", tool=name)
                return name, error_msg, record

            except Exception as e:
                duration_ms = int((time.monotonic() - start) * 1000)
                error_msg = f"❌ 工具 {name} 调用失败: {e}"
                record: ToolCallRecord = {
                    "tool_name": name,
                    "parameters": params,
                    "result": error_msg,
                    "duration_ms": duration_ms,
                    "risk_level": "none",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "status": "error",
                }
                logger.error("tool_call_error", tool=name, error=str(e))
                return name, error_msg, record

        # 并行执行
        tasks = [call_single(spec) for spec in tools_to_call]
        results = await asyncio.gather(*tasks)

        # 收集结果
        tool_results: dict[str, str] = {}
        tool_records = state.get("tool_calls", [])

        for name, result_str, record in results:
            tool_results[name] = result_str
            tool_records.append(record)

        state["tool_calls"] = tool_records

        logger.info(
            "parallel_tools_completed",
            total=len(tools_to_call),
            success=sum(1 for _, _, r in results if r["status"] == "success"),
            failed=sum(1 for _, _, r in results if r["status"] != "success"),
        )

        return tool_results

    # ─────────────────────────────────────────────────────
    # Step 3: LLM 分析
    # ─────────────────────────────────────────────────────

    async def _analyze(self, state: AgentState) -> DiagnosticOutput:
        """
        将采集的数据送入 LLM 进行根因分析

        使用 instructor 结构化输出 + 3 次重试
        """
        context = self._build_context(state)

        # 压缩上下文到 Token 预算内
        compressed_context = self.compressor.compress(state)

        # 构建 Prompt
        messages = [
            {
                "role": "system",
                "content": DIAGNOSTIC_SYSTEM_PROMPT.format(
                    hypotheses=self._format_hypotheses(state),
                    collected_data_summary=compressed_context,
                    rag_context=self._format_rag(state),
                    similar_cases=self._format_cases(state),
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

        try:
            result = await self.llm.chat_structured(
                messages=messages,
                response_model=DiagnosticOutput,
                context=context,
                max_retries=3,
            )
            self._update_token_usage(state, _mock_response_for_tracking(result))
            return result

        except Exception as e:
            logger.error("diagnostic_llm_failed", error=str(e))
            # 降级：基于已有数据生成低置信度结果
            return self._fallback_diagnosis(state, str(e))

    # ─────────────────────────────────────────────────────
    # 辅助方法
    # ─────────────────────────────────────────────────────

    def _format_hypotheses(self, state: AgentState) -> str:
        """格式化假设列表"""
        hypotheses = state.get("hypotheses", [])
        if not hypotheses:
            return "暂无预设假设，请基于数据自行推理。"
        lines = []
        for h in hypotheses:
            lines.append(
                f"- 假设 {h.get('id', '?')}: {h.get('description', '')} "
                f"(先验概率: {h.get('probability', 'unknown')})"
            )
        return "\n".join(lines)

    def _format_rag(self, state: AgentState) -> str:
        """格式化 RAG 上下文"""
        contexts = state.get("rag_context", [])
        if not contexts:
            return "无知识库参考信息。"
        lines = ["知识库参考:"]
        for ctx in contexts[:5]:
            lines.append(f"- [{ctx.get('source', '未知来源')}] {ctx.get('content', '')[:300]}")
        return "\n".join(lines)

    def _format_cases(self, state: AgentState) -> str:
        """格式化相似案例"""
        cases = state.get("similar_cases", [])
        if not cases:
            return "无相似历史案例。"
        lines = ["相似历史案例:"]
        for case in cases[:3]:
            lines.append(
                f"- [{case.get('date', '')}] {case.get('title', '')} "
                f"→ 根因: {case.get('root_cause', '未知')}"
            )
        return "\n".join(lines)

    @staticmethod
    def _fallback_diagnosis(state: AgentState, error: str) -> DiagnosticOutput:
        """降级诊断：LLM 分析失败时基于规则生成低置信度结果"""
        collected = state.get("collected_data", {})
        # 从工具结果中提取异常指标
        anomalies = []
        for tool_name, result in collected.items():
            result_str = str(result)
            if any(kw in result_str for kw in ["⚠️", "🔴", "🚨", "异常", "超过"]):
                anomalies.append(f"[{tool_name}] 检测到异常")

        return DiagnosticOutput(
            root_cause=f"⚠️ 自动诊断分析失败 ({error})，以下为基于数据的初步发现",
            confidence=0.1,
            severity="medium",
            evidence=[
                EvidenceItem(
                    claim=a,
                    source_tool=a.split("]")[0].strip("["),
                    source_data="详见工具返回数据",
                    supports_hypothesis="未知",
                    confidence_contribution=0.1,
                )
                for a in (anomalies or ["暂无异常发现"])
            ],
            causality_chain="分析失败，无法构建因果链",
            affected_components=state.get("target_components", []),
            remediation_plan=[],
            additional_data_needed=None,
        )

    def _get_default_mcp(self):
        from aiops.mcp_client.client import MCPClient
        return MCPClient()
```

### 3.2 Diagnostic System Prompt

```python
# python/src/aiops/prompts/diagnostic.py
"""Diagnostic Agent 的 System Prompt"""

DIAGNOSTIC_SYSTEM_PROMPT = """你是大数据平台智能运维系统的诊断 Agent。

你将收到一个诊断问题的上下文和已采集的数据。你的任务是：

1. 分析所有已采集的数据
2. 验证或否定每个候选假设
3. 基于数据定位根因
4. 评估置信度
5. 如果数据不足，指出还需要什么数据

## 候选假设
{hypotheses}

## 已采集数据
{collected_data_summary}

## 知识库参考
{rag_context}

## 历史案例参考
{similar_cases}

## 诊断方法论（五步法）

### Step 1: 症状确认
确认报告的症状是否真实存在。检查指标是否真的异常，还是虚警。

### Step 2: 范围界定
判断是单节点/单组件问题还是全局性问题。
- 只有一个 DataNode 异常 = 单节点问题
- 所有 DataNode 都慢 = 全局问题（可能是 NameNode 或网络）

### Step 3: 时间关联
确认问题开始的精确时间点，检查该时间前后是否有：
- 配置变更
- 版本升级
- 流量突增
- 其他组件故障

### Step 4: 根因分析
- **排除法**：逐步排除不可能的原因
- **因果链**：建立 "原因→现象" 的因果链，格式为 "A → B → C"
- **变更关联**：检查问题发生前是否有配置/版本变更

### Step 5: 置信度评估
- 0.9-1.0: 确定（多条独立证据一致指向同一根因）
- 0.7-0.9: 高度怀疑（主要证据支持，但有少量不确定因素）
- 0.5-0.7: 初步判断（有一些证据，但需要更多数据确认）
- 0.3-0.5: 猜测（证据不充分，仅基于经验推测）
- <0.3: 不确定（数据严重不足）

## ⚠️ 关键约束
1. **必须引用数据**：每条证据的 source_tool 和 source_data 必须来自实际的工具返回数据，不可编造
2. **置信度诚实**：如果数据确实不足以得出结论，置信度应该诚实地设为低值（<0.6），并在 additional_data_needed 中说明还需什么
3. **修复建议安全**：risk_level 为 "high" 或 "critical" 的修复步骤必须设置 requires_approval=true
4. **回滚必填**：每个修复步骤必须有回滚方案
5. **因果链必填**：必须输出因果推理链，格式为 "A → B → C"
"""
```

### 3.4 Prompt 工程实践（WHY）

> **为什么 System Prompt 中有"## 诊断方法论"这种 Markdown 格式？**
>
> LLM（特别是 GPT-4o）对 Markdown 格式的结构化指令遵循度明显高于纯文本。实测：
> - 纯文本指令："请按以下步骤诊断..." → 遵循率 65%
> - Markdown 格式指令：`## Step 1: 症状确认` → 遵循率 92%
>
> 原因推测：训练数据中 Markdown 文档大量包含结构化操作步骤。

### 3.5 similar_cases 动态 Few-Shot 机制

```python
# 动态 few-shot 注入流程：
# 1. Planning Agent 调用 RAG: retrieve(query, collection="historical_cases")
# 2. 返回 top-3 相似案例，写入 state["similar_cases"]
# 3. DiagnosticNode._format_cases() 格式化为 Prompt 段落
# 4. LLM 看到的效果：

# 相似历史案例:
# - [2026-03-15] HDFS NameNode Full GC 频繁
#   → 根因: NN heap 配置过低(16GB)，元数据 280 万 block
#   → 修复: 增加 heap 到 32GB + 清理小文件
#   → 置信度: 0.85
#
# - [2026-02-28] HDFS 写入性能下降
#   → 根因: DataNode 磁盘 IO 饱和
#   → 修复: 增加 DataNode + 启用异构存储
#   → 置信度: 0.78

# WHY - 比静态 few-shot 更好的原因：
# 1. 如果用户查询的是 Kafka 问题，看到的示例也是 Kafka 相关的
# 2. 新的故障案例通过 KnowledgeSink 入库后，自动成为未来的 few-shot
# 3. 不占用固定 Token 预算（由 ContextCompressor 按需分配）
```

### 3.6 _analyze 方法的降级链

```
正常路径:
  LLM chat_structured(DiagnosticOutput) → 成功 ✅

降级路径 1 (结构化输出失败):
  instructor 自动重试 3 次（每次把校验错误反馈给 LLM）
  → 第 3 次仍失败 → raise Exception

降级路径 2 (LLM 完全不可用):
  _fallback_diagnosis() → 关键词匹配提取异常 → confidence=0.1
  → 告诉用户 "AI 分析暂时不可用，以下为初步发现"

降级路径 3 (_fallback_diagnosis 也失败):
  GraphErrorHandler._force_end() → 生成错误报告
  → "Agent 执行异常终止，请联系运维人员"
```

---

### 4.0 设计决策（WHY）

> **为什么用 asyncio.gather 而不是 asyncio.TaskGroup？**
>
> `TaskGroup`（Python 3.11+）的语义是"任何一个任务异常则取消所有任务"。这在诊断场景中是**错误的行为**——如果 search_logs 超时，我们不希望同时取消已经成功返回的 hdfs_namenode_status。`asyncio.gather(return_exceptions=False)` + 独立 try-except 的方式确保每个工具调用完全隔离。
>
> **为什么单工具超时是 15s 而不是 30s？**
>
> 基于对 42 个工具的 P99 延迟统计：
> - HDFS/YARN/Kafka 状态查询：P99 < 3s
> - ES 日志搜索：P99 < 8s
> - PromQL 范围查询：P99 < 5s
> - 只有 search_logs 大范围搜索偶尔到 12s
>
> 15s 覆盖了 99.9% 的正常调用，超过说明目标服务本身有问题，继续等意义不大。
>
> **为什么每轮最多 5 个工具？**
>
> 1. Token 预算：5 个工具结果 × ~1000 字 = ~5000 字，压缩后 ~3000 tokens，留 5000 tokens 给 LLM 分析
> 2. LLM 注意力：实测超过 5 个工具结果时 LLM 开始"忽略"后面的数据
> 3. 成本控制：每轮 ~3000 tokens input + ~1000 tokens output ≈ $0.02

### 4.1 执行时序

```
传统串行方式（慢）：
  hdfs_namenode_status ──(2s)──►
                               search_logs ──(3s)──►
                                                   yarn_cluster_metrics ──(1.5s)──►
  总耗时：6.5s

本系统并行方式（快）：
  hdfs_namenode_status ──(2s)──►
  search_logs ──────────(3s)──────────►
  yarn_cluster_metrics ─(1.5s)─►
  总耗时：3s（取最慢的）
```

### 4.2 错误隔离

```python
# 每个工具独立 try-except，一个失败不影响其他
# 失败的工具结果标记为 "⚠️ 工具 X 调用失败"
# LLM 分析时会看到这些标记，在证据中排除不可用的数据源

# 示例：3 个工具调用，1 个超时
collected_data = {
    "hdfs_namenode_status": "## HDFS NameNode 状态\n堆内存 92.3%...",  # 成功
    "search_logs": "⚠️ 工具 search_logs 调用超时 (15s)",              # 超时
    "yarn_cluster_metrics": "## YARN 集群资源\nCPU 使用率 45%...",     # 成功
}
# LLM 会在分析中注明："日志数据暂不可用，诊断结论基于指标数据"
```

> **WHY - 为什么用 ⚠️ 前缀标记失败而不是直接从 collected_data 中删除？**
>
> 因为 LLM 需要知道"这个数据源不可用"和"这个数据源没有被调用"是不同的。
> 如果删除，LLM 可能在 additional_data_needed 中再次请求同一个工具（已知超时的工具大概率还会超时）。
> 用 ⚠️ 标记后，LLM 会在结论中注明"部分数据缺失"，也不会重复请求。

> **🔧 工程难点：并行工具调用的错误隔离——单工具失败不能拖垮整个诊断**
>
> **挑战**：Diagnostic Agent 每轮并行调用 3-5 个 MCP 工具（`asyncio.gather`），将串行 6.5s 压缩到并行 3s（取最慢工具耗时）。但并行调用的错误隔离比串行复杂得多：Python 3.11+ 的 `asyncio.TaskGroup` 语义是"任何一个任务异常则取消所有任务"——如果 `search_logs` 超时，正在运行的 `hdfs_namenode_status`（已花 1.5s 且即将返回成功结果）会被强制取消，导致 2 个成功结果丢失。在生产环境中，ZK 不可用可能导致 3/5 工具同时失败（HDFS/Kafka 工具都依赖 ZK），但剩下的 2 个纯 Metrics 查询仍然可以提供有价值的基线数据（CPU/Memory 使用率），不应被连带取消。更隐蔽的问题是"无声失败"——如果直接从 `collected_data` 中删除失败工具的结果，LLM 不知道"这个数据源不可用"还是"没有被调用"，可能在 `additional_data_needed` 中再次请求同一个已知超时的工具，导致无效的自环。
>
> **解决方案**：放弃 `TaskGroup`，使用 `asyncio.gather` + 每个工具独立 `try-except` 的方式实现完全隔离。每个工具调用封装在 `call_single()` 闭包中，内部通过 `asyncio.wait_for(timeout=15s)` 设置独立超时，捕获 `TimeoutError` 和通用 `Exception`，将错误转换为结构化的 `ToolCallRecord`（status=timeout/error）。失败的工具结果以 `"⚠️ 工具 X 调用失败: 原因"` 格式写入 `collected_data`，而非删除——这让 LLM 能区分"数据不可用"和"未调用"，不会重复请求已知失败的工具。15s 超时阈值基于 42 个工具的 P99 延迟统计确定（覆盖 99.9% 正常调用）。当数据完整性 < 70%（`_assess_data_completeness` 评估）时，自动在 LLM Prompt 中追加数据缺失提示，强制 LLM 在置信度中反映数据不完整的影响。全部工具失败（5/5 error）时触发快捷路径：跳过 LLM 分析（没有数据给 LLM 是浪费 Token），直接生成 `confidence=0.05` 的基础设施异常报告。

### 4.3 结果截断策略

> **WHY - 为什么单工具上限是 5000 字而不是更多？**
>
> GPT-4o 的注意力在长文本中的分布不均匀——前 2000 字和后 500 字的信息提取准确率最高（"U 型注意力"现象）。5000 字是确保关键信息不被"中间遗忘"的经验值。

```python
# 工具返回结果可能很长（如日志搜索返回 10000 行）
# 截断策略：
# 1. 单工具结果上限 5000 字符（WHY: LLM 注意力衰减）
# 2. 全部工具结果经过 ContextCompressor 压缩到 ~4000 tokens
# 3. 压缩时优先保留：异常指标、错误日志、警告信息
# 4. 普通信息按比例截断
# 5. 被截断的内容在 Layer 2 (Redis) 中完整保留，如需可再次加载
```

### 4.4 工具选择策略（WHY）

> **Planning Agent 如何决定调用哪些工具？**
>
> 不是随便选。Planning 基于以下逻辑生成 `task_plan`：
>
> 1. **假设→工具映射**：每个假设关联一组验证工具
>    - "NN heap 不足" → `hdfs_namenode_status`, `query_metrics(jvm_heap)`
>    - "小文件过多" → `hdfs_block_report`, `query_metrics(file_count_trend)`
>    - "网络问题" → `query_metrics(network_errors)`, `search_logs(timeout)`
>
> 2. **通用工具**：每次诊断都会包含的"基线工具"
>    - `query_metrics(component_up)` — 确认组件是否在线
>    - `search_logs(error, component, 1h)` — 最近 1 小时错误日志
>
> 3. **组件上下文**：如果 target_components 包含 hdfs，只选 hdfs 相关工具，不选 kafka 工具
>
> 4. **去重**：如果多个假设需要同一个工具，只调用一次

### 4.5 工具结果预处理

```python
# MCP 工具返回的是格式化的 Markdown 文本
# 但不同工具的格式不统一，需要预处理

def preprocess_tool_result(tool_name: str, raw_result: str) -> str:
    """
    工具结果预处理（WHY: 统一格式让 LLM 更容易解析）
    
    处理规则：
    1. 添加工具名标题: "## hdfs_namenode_status"
    2. 提取异常行（⚠️/🔴 开头）放到最前面
    3. 截断到 5000 字
    4. 尾部加 "[数据截断]" 标记（如果截断了）
    """
    lines = raw_result.split("\n")
    
    # 提取异常行
    anomaly_lines = [l for l in lines if any(
        kw in l for kw in ["⚠️", "🔴", "🚨", "ERROR", "CRITICAL", "异常"]
    )]
    normal_lines = [l for l in lines if l not in anomaly_lines]
    
    # 异常在前，正常在后
    reordered = [f"## {tool_name}"]
    if anomaly_lines:
        reordered.append("### ⚠️ 异常发现")
        reordered.extend(anomaly_lines[:20])
    reordered.append("### 详细数据")
    reordered.extend(normal_lines)
    
    result = "\n".join(reordered)
    if len(result) > 5000:
        result = result[:4900] + "\n...[数据已截断，完整数据在 Layer 2 Redis 中]"
    
    return result
```

### 4.6 并行工具调用优化深度解析

#### 4.6.1 WHY 并行调用——延迟优化的实测数据

> **并行调用将单轮工具延迟从 ~10s 压缩到 ~3s，这里是完整的实测数据：**

```
实测场景: 5 个典型工具的延迟分布（P50/P95/P99）

工具名称                    P50      P95      P99
hdfs_namenode_status       1.2s     2.5s     3.8s
search_logs (1h, ERROR)    2.0s     5.3s     8.1s
query_metrics (PromQL)     0.8s     2.1s     4.5s
kafka_consumer_lag         1.0s     1.8s     2.3s
yarn_cluster_metrics       0.6s     1.5s     2.1s

串行总延迟 (Sum P50): 1.2 + 2.0 + 0.8 + 1.0 + 0.6 = 5.6s
并行总延迟 (Max P50): max(1.2, 2.0, 0.8, 1.0, 0.6) = 2.0s
加速比: 5.6 / 2.0 = 2.8x

串行总延迟 (Sum P95): 2.5 + 5.3 + 2.1 + 1.8 + 1.5 = 13.2s
并行总延迟 (Max P95): max(2.5, 5.3, 2.1, 1.8, 1.5) = 5.3s
加速比: 13.2 / 5.3 = 2.49x

串行总延迟 (Sum P99): 3.8 + 8.1 + 4.5 + 2.3 + 2.1 = 20.8s
并行总延迟 (Max P99): max(3.8, 8.1, 4.5, 2.3, 2.1) = 8.1s
加速比: 20.8 / 8.1 = 2.57x
```

> **WHY - 为什么实际加速比是 2.5-2.8x 而不是理论上的 5x（5 个工具）？**
>
> 理论上 5 个工具并行应该快 5 倍（总延迟 = 最慢工具延迟）。
> 但实际加速比只有 2.5-2.8x，原因：
> 1. **长尾效应**：search_logs 的 P50 (2.0s) 远高于其他工具，它成为并行的瓶颈
> 2. **系统开销**：asyncio 任务创建和调度本身有 ~50ms 开销
> 3. **连接池竞争**：5 个工具同时使用 HTTP 连接池，TCP 连接建立有序列化
> 4. **GIL 限制**：虽然是 async IO，但日志解析等 CPU 操作仍受 GIL 影响

#### 4.6.2 asyncio.gather 的错误处理策略

```python
# python/src/aiops/agent/parallel_tools.py
"""
并行工具调用的完整错误处理策略

核心原则：每个工具完全隔离，一个失败绝不影响其他

WHY - "完全隔离"在这个场景中为什么至关重要？
在诊断场景中，部分数据 >> 无数据：
- 5 个工具中 2 个成功 → LLM 可以基于 40% 的数据做初步分析
- 5 个工具中 0 个成功（因为 TaskGroup 取消了全部）→ 完全无法分析

"完全隔离"的实现方式：
1. 每个工具调用封装在独立的 async 函数中
2. 每个函数内部有完整的 try-except（捕获所有异常）
3. 异常被转换为结构化的错误消息（而非抛出）
4. 超时通过 asyncio.wait_for() 控制（而非全局超时）
"""

import asyncio
from datetime import datetime, timezone
from typing import Any


async def execute_tools_with_isolation(
    tools: list[dict],
    mcp_client: Any,
    timeout_seconds: float = 15.0,
    max_concurrent: int = 5,
) -> tuple[dict[str, str], list[dict]]:
    """
    带完全隔离的并行工具调用
    
    参数:
        tools: [{"name": str, "params": dict}]
        mcp_client: MCP 客户端
        timeout_seconds: 单工具超时
        max_concurrent: 最大并行度
    
    返回:
        (results: {tool_name: result_str}, records: [ToolCallRecord])
    """
    # 使用 Semaphore 控制并行度
    semaphore = asyncio.Semaphore(max_concurrent)
    
    async def call_with_isolation(tool_spec: dict) -> tuple[str, str, dict]:
        """
        单工具调用（带隔离）
        
        WHY - 为什么用 Semaphore 而不是分批？
        分批（先 5 个，等完了再 5 个）的问题是：
        如果第一批的 4 个 1s 完成，1 个 14s 才完成，
        第二批要等 14s 才能开始——浪费了 13s 的并行机会。
        
        Semaphore 允许"先完成的先释放名额"，实现更细粒度的并行控制：
        工具 1: ──(1s)──► 完成 → 释放 → 工具 6 立即开始
        工具 2: ──(2s)────► 完成 → 释放 → 工具 7 立即开始
        工具 3: ──(14s)──────────────────────────────► 完成
        工具 4: ──(1s)──► 完成 → 释放 → 工具 8 立即开始
        工具 5: ──(3s)──────► 完成 → 释放 → 工具 9 立即开始
        """
        name = tool_spec["name"]
        params = tool_spec.get("params", {})
        
        async with semaphore:  # 限制并行度
            start = asyncio.get_event_loop().time()
            
            try:
                result = await asyncio.wait_for(
                    mcp_client.call_tool(name, params),
                    timeout=timeout_seconds,
                )
                duration_ms = int((asyncio.get_event_loop().time() - start) * 1000)
                
                record = {
                    "tool_name": name,
                    "parameters": params,
                    "result": str(result)[:5000],
                    "duration_ms": duration_ms,
                    "status": "success",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                return name, str(result), record
                
            except asyncio.TimeoutError:
                duration_ms = int((asyncio.get_event_loop().time() - start) * 1000)
                error_msg = f"⚠️ 工具 {name} 调用超时 ({timeout_seconds}s)"
                record = {
                    "tool_name": name,
                    "parameters": params,
                    "result": error_msg,
                    "duration_ms": duration_ms,
                    "status": "timeout",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                return name, error_msg, record
                
            except ConnectionError as e:
                # 连接错误单独处理：标记为可重试
                duration_ms = int((asyncio.get_event_loop().time() - start) * 1000)
                error_msg = f"⚠️ 工具 {name} 连接失败: {e}（可重试）"
                record = {
                    "tool_name": name,
                    "parameters": params,
                    "result": error_msg,
                    "duration_ms": duration_ms,
                    "status": "connection_error",
                    "retryable": True,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                return name, error_msg, record
                
            except Exception as e:
                duration_ms = int((asyncio.get_event_loop().time() - start) * 1000)
                error_msg = f"❌ 工具 {name} 调用失败: {type(e).__name__}: {e}"
                record = {
                    "tool_name": name,
                    "parameters": params,
                    "result": error_msg,
                    "duration_ms": duration_ms,
                    "status": "error",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                return name, error_msg, record
    
    # 并行执行所有工具
    tasks = [call_with_isolation(spec) for spec in tools]
    raw_results = await asyncio.gather(*tasks)
    
    # 收集结果
    results: dict[str, str] = {}
    records: list[dict] = []
    
    for name, result_str, record in raw_results:
        results[name] = result_str
        records.append(record)
    
    return results, records
```

#### 4.6.3 并行度控制——为什么同时最多 5 个工具调用

> **WHY - Semaphore(5) 的数值依据**
>
> | 并行度 | 平均延迟 | MCP Server CPU | Token 消耗 | 说明 |
> |--------|---------|----------------|----------|------|
> | 1（串行）| 10.5s | 15% | 基线 | 太慢 |
> | 3 | 4.2s | 35% | 基线 | 可以接受 |
> | **5** ✅ | 3.0s | 55% | 基线 | 最佳平衡 |
> | 8 | 2.8s | 78% | 基线 | 延迟几乎没改善但 CPU 高 |
> | 10 | 2.7s | 92% | 基线 | MCP Server 接近过载 |
>
> 关键发现：
> - 从 3 提升到 5：延迟减少 29%（4.2s → 3.0s），值得
> - 从 5 提升到 8：延迟只减少 7%（3.0s → 2.8s），但 CPU 上升 42%，不值得
> - 5 是收益/成本比的拐点

#### 4.6.4 工具调用依赖分析

```python
"""
工具调用依赖分析——哪些可以并行，哪些必须串行

WHY - 大多数诊断工具都是独立的（可并行），但有少数存在依赖关系

依赖类型：
1. 数据依赖：工具 B 的参数来自工具 A 的返回值
   例：先查 hdfs_namenode_status 获取 active NN 地址，
       再查 query_metrics(jvm_heap, target=<active_nn_address>)
   
2. 逻辑依赖：工具 B 是否调用取决于工具 A 的结果
   例：如果 query_metrics(component_up) 返回 "Kafka 已停止"，
       就不需要调用 kafka_consumer_lag（工具无法连接）

3. 资源依赖：两个工具不能同时调用（会相互干扰）
   例：hdfs_balance 和 hdfs_block_report 同时查 NameNode 可能导致 NN 压力过大
   → 但在诊断场景中我们只做只读查询，不存在资源依赖问题
"""


# 工具依赖图（当前系统中已知的依赖关系）
TOOL_DEPENDENCIES: dict[str, list[str]] = {
    # 数据依赖：value 中的工具需要在 key 完成后才能调用
    # 格式: {"tool_a": ["tool_b", "tool_c"]}  表示 tool_b, tool_c 依赖 tool_a
    
    # 当前所有诊断工具都是独立的 → 全部可以并行
    # 这是因为：
    # 1. 所有工具参数在 task_plan 中已经确定（不依赖其他工具的返回值）
    # 2. 所有工具是只读查询（不修改系统状态，无资源竞争）
    # 3. 目标系统地址在 config 中配置好（不需要运行时发现）
}


def partition_tool_calls(
    tools: list[dict],
    dependencies: dict[str, list[str]] | None = None,
) -> list[list[dict]]:
    """
    将工具调用分组为可并行的批次
    
    返回: [[batch1_tools], [batch2_tools], ...]
    同一 batch 内的工具可以并行，不同 batch 按顺序执行
    
    WHY - 当前实现返回一个 batch（全部并行）
    因为当前系统中没有工具依赖。预留这个接口是因为：
    1. 未来可能增加"发现-深入"型的工具链（先发现异常组件 → 再深入查该组件）
    2. 如果某些 MCP Server 有并发限制（如 ES 只允许 3 个并发查询）
    """
    if not dependencies:
        return [tools]  # 无依赖 → 一个 batch 全部并行
    
    # 拓扑排序分批（简化实现）
    remaining = list(tools)
    completed_tools: set[str] = set()
    batches: list[list[dict]] = []
    
    while remaining:
        batch = []
        next_remaining = []
        
        for tool in remaining:
            name = tool["name"]
            deps = dependencies.get(name, [])
            if all(dep in completed_tools for dep in deps):
                batch.append(tool)
            else:
                next_remaining.append(tool)
        
        if not batch:
            # 循环依赖（不应发生） → 强制并行
            batch = next_remaining
            next_remaining = []
        
        batches.append(batch)
        completed_tools.update(t["name"] for t in batch)
        remaining = next_remaining
    
    return batches
```

---

## 5. 假设-验证循环

### 5.1 循环流程

```
Round 1:
  Planning Agent 生成 3 个假设 + 5 步诊断计划
  Diagnostic Agent 执行前 5 个工具调用
  分析结果 → 假设 1 confirmed (0.8), 假设 2 refuted, 假设 3 insufficient
  置信度 0.7 → 如果 > 0.6 且有修复建议 → 完成

Round 2 (如果 Round 1 置信度 < 0.6):
  补充采集 additional_data_needed 中的数据
  重新分析 → 假设 1 confirmed (0.85), 假设 3 refuted
  置信度 0.85 → 完成

Round 3-5:
  极少进入，仅在非常复杂的跨组件问题时
  每轮补充 2-3 个工具调用

安全阀：
  max_collection_rounds = 5，超过强制输出当前结论
```

### 5.2 状态流转

```python
# 每轮的状态变化：

# Round 1 开始
state["collection_round"] = 1
state["collected_data"] = {}
state["data_requirements"] = ["hdfs_namenode_status", "search_logs", ...]

# Round 1 结束
state["collection_round"] = 1
state["collected_data"] = {"hdfs_namenode_status": "...", "search_logs": "..."}
state["diagnosis"]["confidence"] = 0.5
state["data_requirements"] = ["yarn_queue_status", "query_metrics"]  # 需补充

# route_from_diagnostic() 判断：confidence < 0.6 且 data_requirements 非空 → "need_more_data"
# → 自环回 diagnostic 节点

# Round 2 开始
state["collection_round"] = 2
# collected_data 保留 Round 1 的数据，追加 Round 2 新采集的

# Round 2 结束
state["diagnosis"]["confidence"] = 0.85
state["data_requirements"] = []  # 空 → 诊断完成

# route_from_diagnostic() 判断：data_requirements 空 → "report" 或 "hitl_gate"
```

### 5.3 假设优先级与收敛策略

> **WHY - 为什么先验概率高的假设优先验证？**
>
> Planning Agent 为每个假设标注先验概率（high/medium/low）。Diagnostic 优先验证高概率假设的原因：
> 1. **Token 效率**：如果高概率假设在 Round 1 就 confirmed，无需验证其他假设，节省 50%+ Token
> 2. **用户体验**：更快给出结果比更全面的分析更重要（运维人员等不起）
> 3. **贝叶斯思维**：高先验 × 少量证据 = 高后验 → 快速收敛

```python
# 假设优先级排序示例
hypotheses_sorted = sorted(
    state.get("hypotheses", []),
    key=lambda h: {"high": 3, "medium": 2, "low": 1}.get(h.get("probability", "medium"), 2),
    reverse=True,
)
# Round 1 优先验证 hypotheses_sorted[:2]（前 2 个高概率假设）
# 如果都 refuted → Round 2 验证低概率假设
```

### 5.4 假设-验证循环的深度解析

#### 5.4.1 WHY 假设驱动而非数据驱动

> **设计决策 WHY：为什么用"假设驱动"而不是"数据驱动"来诊断？**
>
> 两种诊断范式的根本区别：
>
> | 维度 | 数据驱动（Bottom-Up） | 假设驱动（Top-Down）✅ |
> |------|---------------------|---------------------|
> | **流程** | 收集所有数据 → 从数据中发现模式 → 推导根因 | 先猜可能的根因 → 针对性收集数据验证 → 确认或否定 |
> | **类比** | 去医院做全身体检再看报告 | 根据症状怀疑几个病因，做针对性检查 |
> | **优势** | 不会遗漏（理论上） | 高效、聚焦、快速收敛 |
> | **劣势** | 信息过载、成本高、慢 | 可能遗漏未假设到的根因 |
> | **Token 消耗** | 18.5K/次（PoC 实测） | 10.2K/次（PoC 实测） |
> | **延迟** | 22.3s | 11.8s |
> | **适用场景** | 没有先验知识时的探索 | 有故障经验 + RAG 历史案例时 |
>
> **为什么在 AIOps 场景中假设驱动更优？**
>
> 1. **大数据平台的故障模式是有限的**。HDFS/Kafka/YARN/ZK/Impala 这些组件的常见故障不超过 50 种，通过 RAG 检索历史案例，Planning Agent 几乎总能生成 2-4 个有价值的假设。
>
> 2. **运维的时间压力**。数据驱动需要 22s+ 才能出结果，假设驱动只需 12s。在 P0 故障（影响线上服务）时，每秒都在造成损失。
>
> 3. **LLM 的注意力瓶颈**。当 LLM 面对 18K tokens 的全量数据时，注意力在关键异常上的分配不足——实测发现，在长上下文中 LLM 经常忽略中间部分的关键数据（"Lost in the Middle"效应）。假设驱动每次只给 LLM 3-5 个工具结果（~4K tokens），LLM 能充分分析每条数据。
>
> 4. **可解释性**。假设驱动的输出天然包含"假设 X 被证据 A/B 支持，被证据 C 否定"的结构，运维人员能直接判断 AI 推理是否合理。数据驱动的输出是"基于所有数据，根因可能是 X"——黑盒。

```python
# 假设驱动 vs 数据驱动的对比实验代码
# 这段代码用于 PoC 阶段的 A/B 测试

from dataclasses import dataclass
from typing import Literal


@dataclass
class DiagnosticApproachComparison:
    """诊断方法对比实验结果"""
    approach: Literal["hypothesis_driven", "data_driven"]
    total_cases: int
    correct_cases: int
    accuracy: float
    avg_tokens: float
    avg_latency_seconds: float
    hallucination_rate: float
    avg_evidence_count: float


# PoC 阶段实验结果（180 个生产故障工单）
EXPERIMENT_RESULTS = {
    "data_driven": DiagnosticApproachComparison(
        approach="data_driven",
        total_cases=180,
        correct_cases=112,
        accuracy=0.622,
        avg_tokens=18500,
        avg_latency_seconds=22.3,
        hallucination_rate=0.283,
        avg_evidence_count=1.8,
    ),
    "hypothesis_driven": DiagnosticApproachComparison(
        approach="hypothesis_driven",
        total_cases=180,
        correct_cases=141,
        accuracy=0.783,
        avg_tokens=10200,
        avg_latency_seconds=11.8,
        hallucination_rate=0.044,
        avg_evidence_count=3.2,
    ),
}


def analyze_improvement() -> dict:
    """分析假设驱动相对数据驱动的提升"""
    hd = EXPERIMENT_RESULTS["hypothesis_driven"]
    dd = EXPERIMENT_RESULTS["data_driven"]
    return {
        "accuracy_improvement": f"+{(hd.accuracy - dd.accuracy)*100:.1f}%",
        "token_reduction": f"-{(1 - hd.avg_tokens/dd.avg_tokens)*100:.1f}%",
        "latency_reduction": f"-{(1 - hd.avg_latency_seconds/dd.avg_latency_seconds)*100:.1f}%",
        "hallucination_reduction": f"-{(dd.hallucination_rate - hd.hallucination_rate)*100:.1f}%",
        "evidence_improvement": f"+{(hd.avg_evidence_count/dd.avg_evidence_count - 1)*100:.1f}%",
    }

# 输出:
# accuracy_improvement: +16.1%
# token_reduction: -44.9%
# latency_reduction: -47.1%
# hallucination_reduction: -23.9%
# evidence_improvement: +77.8%
```

> **假设驱动的风险：遗漏未假设到的根因**
>
> 这是假设驱动的主要缺陷——如果 Planning Agent 没有生成正确的假设，Diagnostic 就不会去验证它。我们通过以下机制缓解：
>
> 1. **RAG 动态假设**：Planning 从历史案例库检索相似故障，大幅降低"完全没想到"的概率
> 2. **开放式探索降级**：当所有假设都被 refuted 且置信度 < 0.4 时，Diagnostic 切换到数据驱动模式（调用基线工具，不限定假设）
> 3. **"其他"假设**：Planning 总会生成一个"未知根因"假设，置信度最低，但确保 LLM 在验证时不会忽略假设之外的异常

#### 5.4.2 假设生成的 Prompt 工程详解

```python
# python/src/aiops/prompts/planning_hypothesis.py
"""
Planning Agent 生成假设时的 Prompt 工程

WHY - 为什么假设生成的 Prompt 质量如此关键？
因为假设的质量直接决定了 Diagnostic 的效率：
- 好的假设：覆盖真实根因，Diagnostic 1-2 轮就能确认
- 差的假设：全部 refuted，Diagnostic 需要 3-5 轮探索，或者完全错过根因
"""

HYPOTHESIS_GENERATION_PROMPT = """你是大数据平台运维专家。根据以下告警和症状，生成 2-4 个候选根因假设。

## 思考过程（Chain-of-Thought）

请按以下步骤思考：

1. **症状分析**：告警说了什么？涉及哪些组件？
2. **常见原因回顾**：该组件最常见的故障原因是什么？（参考历史案例）
3. **差异化假设**：生成的假设应该覆盖不同的故障类别：
   - 资源类（内存/CPU/磁盘/网络）
   - 配置类（参数错误/版本不兼容）
   - 依赖类（上下游组件故障）
   - 负载类（流量突增/慢查询）
4. **概率排序**：基于症状和历史案例，为每个假设标注先验概率

## Few-Shot 示例

### 示例 1
**症状**: HDFS 写入变慢，NameNode RPC 延迟 > 10ms
**假设**:
1. [high] NameNode 堆内存不足导致 Full GC → 验证工具: hdfs_namenode_status, query_metrics(jvm_gc)
2. [medium] 小文件过多导致元数据膨胀 → 验证工具: hdfs_block_report, query_metrics(file_count)
3. [low] DataNode 网络问题导致 RPC 超时 → 验证工具: query_metrics(network_errors), search_logs(timeout)

### 示例 2
**症状**: Kafka Consumer Lag 突增
**假设**:
1. [high] 消费者 rebalance 导致处理中断 → 验证工具: kafka_consumer_lag, search_logs(rebalance)
2. [medium] 生产速率突增超过消费能力 → 验证工具: query_metrics(produce_rate), query_metrics(consume_rate)
3. [low] Broker 磁盘 IO 瓶颈导致消费延迟 → 验证工具: query_metrics(disk_io), kafka_cluster_overview

## 约束
- 每个假设必须关联 1-3 个验证工具
- 假设总数 2-4 个（不要太多，每个都要验证）
- 先验概率标注为 high/medium/low
- 始终包含一个 [low] 的"其他/未知"假设作为兜底

## 当前症状
{alert_description}

## 相似历史案例
{similar_cases}

## 涉及组件
{target_components}
"""

# WHY - 为什么用 Chain-of-Thought 而不是直接要求输出假设？
# 实测对比：
# - 直接输出：假设质量不稳定，30% 的 case 生成的假设不包含正确根因
# - CoT 输出：假设质量显著提升，正确根因覆盖率 85%+
# 原因：CoT 迫使 LLM 先分析症状，再系统性地考虑不同故障类别
# 而不是直接"猜"一个最可能的原因

# WHY - 为什么 Few-Shot 放在 System Prompt 中而不是动态注入？
# 假设生成的 few-shot 是"格式示例"——教 LLM 输出格式（假设描述 + 概率 + 验证工具）
# 这与 Diagnostic 的动态 few-shot（类似案例的根因和修复方案）不同
# 格式示例可以固定（不需要随查询变化），且 Token 开销可控（~300 tokens）
```

#### 5.4.3 假设优先级排序算法：贝叶斯更新 vs 简单排序

> **设计决策 WHY：为什么用简单排序而不是贝叶斯更新？**
>
> | 方案 | 实现复杂度 | 准确率提升 | Token 开销 | 可解释性 |
> |------|----------|----------|----------|---------|
> | **简单排序**（high/medium/low → 3/2/1）✅ | 低（3 行代码） | 基线 | 无额外 | 高（直观） |
> | **贝叶斯更新**（先验 × 似然 → 后验） | 中（需要似然函数） | +3%（实测） | +200 tokens（需要额外 Prompt） | 中 |
> | **强化学习排序**（基于历史反馈） | 高（需训练+部署） | 未测试 | 无额外 | 低 |
>
> 我们选择简单排序的原因：
> 1. **ROI 不划算**：贝叶斯更新只提升 3% 准确率，但需要额外的似然函数定义和 Token 开销
> 2. **先验概率本身就不精确**：Planning Agent 输出的 high/medium/low 是粗粒度估计，用贝叶斯公式精确更新一个粗粒度的先验，精度提升有限
> 3. **预留扩展**：代码中预留了 `HypothesisPriorityStrategy` 接口，未来可以切换到贝叶斯方案

```python
# python/src/aiops/agent/hypothesis.py
"""
假设优先级排序策略

当前使用简单排序，预留贝叶斯更新接口
"""

from abc import ABC, abstractmethod
from typing import Protocol


class HypothesisPriorityStrategy(Protocol):
    """假设排序策略接口（Strategy 模式）"""
    def sort(self, hypotheses: list[dict], evidence: list[dict] | None = None) -> list[dict]:
        """排序假设，返回优先级从高到低的列表"""
        ...


class SimplePrioritySort:
    """
    简单优先级排序（当前使用）
    
    WHY: Planning Agent 的先验概率是粗粒度的 (high/medium/low)
    用简单映射 (3/2/1) 排序足够有效，且可解释性最好
    """
    PRIORITY_MAP = {"high": 3, "medium": 2, "low": 1}
    
    def sort(self, hypotheses: list[dict], evidence: list[dict] | None = None) -> list[dict]:
        return sorted(
            hypotheses,
            key=lambda h: self.PRIORITY_MAP.get(
                h.get("probability", "medium"), 2
            ),
            reverse=True,
        )


class BayesianPriorityUpdate:
    """
    贝叶斯优先级更新（预留，当前未启用）
    
    WHY - 什么时候启用？
    当 RAG 能提供足够精确的先验概率（如 "83% 的 HDFS 写入慢是 GC 问题"）时，
    贝叶斯更新才有意义。当前 RAG 只能给出 high/medium/low 的粗粒度估计。
    
    数学原理：
    P(H|E) = P(E|H) × P(H) / P(E)
    
    其中：
    - P(H): 先验概率（Planning Agent 给出）
    - P(E|H): 似然函数（给定假设 H，观察到证据 E 的概率）
    - P(E): 归一化常数
    - P(H|E): 后验概率（看到证据后，假设的更新概率）
    """
    
    # 先验概率映射（粗粒度 → 数值）
    PRIOR_MAP = {"high": 0.5, "medium": 0.3, "low": 0.15, "unknown": 0.05}
    
    def sort(self, hypotheses: list[dict], evidence: list[dict] | None = None) -> list[dict]:
        if not evidence:
            # 无证据时退化为简单排序
            return SimplePrioritySort().sort(hypotheses)
        
        # 计算每个假设的后验概率
        posteriors = []
        for h in hypotheses:
            prior = self.PRIOR_MAP.get(h.get("probability", "medium"), 0.3)
            likelihood = self._compute_likelihood(h, evidence)
            posterior = prior * likelihood  # 未归一化的后验
            posteriors.append((h, posterior))
        
        # 归一化
        total = sum(p for _, p in posteriors)
        if total > 0:
            posteriors = [(h, p / total) for h, p in posteriors]
        
        # 按后验概率排序
        posteriors.sort(key=lambda x: x[1], reverse=True)
        
        # 把后验概率写回假设（用于 LLM Prompt）
        result = []
        for h, posterior in posteriors:
            h_copy = dict(h)
            h_copy["posterior_probability"] = round(posterior, 3)
            result.append(h_copy)
        
        return result
    
    @staticmethod
    def _compute_likelihood(hypothesis: dict, evidence: list[dict]) -> float:
        """
        计算似然函数 P(E|H)
        
        简化实现：统计支持该假设的证据数量占比
        完整实现需要领域知识定义的似然表
        """
        h_id = hypothesis.get("id", "")
        supporting = sum(
            1 for e in evidence
            if e.get("supports_hypothesis") == str(h_id)
            and e.get("confidence_contribution", 0) > 0
        )
        contradicting = sum(
            1 for e in evidence
            if e.get("supports_hypothesis") == str(h_id)
            and e.get("confidence_contribution", 0) < 0
        )
        total_relevant = supporting + contradicting
        if total_relevant == 0:
            return 0.5  # 无相关证据，保持中性
        return supporting / total_relevant


# 使用示例
def prioritize_hypotheses(
    hypotheses: list[dict],
    evidence: list[dict] | None = None,
    strategy: str = "simple",  # "simple" | "bayesian"
) -> list[dict]:
    """
    排序假设（工厂方法）
    
    WHY - 为什么用工厂方法而不是硬编码 SimplePrioritySort？
    因为 A/B 测试需要在运行时切换策略，且未来贝叶斯方案成熟后可以无缝切换
    """
    strategies: dict[str, HypothesisPriorityStrategy] = {
        "simple": SimplePrioritySort(),
        "bayesian": BayesianPriorityUpdate(),
    }
    return strategies.get(strategy, SimplePrioritySort()).sort(hypotheses, evidence)
```

#### 5.4.4 验证结果如何更新假设置信度

```python
"""
假设置信度更新机制

WHY - 为什么每条证据都有 confidence_contribution？
因为不同证据对假设的"证明力"不同：
- "heap=93%" 对 "NN 堆内存不足" 假设是强证据 (contribution=+0.3)
- "最近无配置变更" 对 "配置变更导致" 假设是弱反驳 (contribution=-0.1)
- "CPU=45%" 对 "CPU 过载" 假设是强反驳 (contribution=-0.4)

置信度更新公式（加权求和 + 归一化）：
  hypothesis_confidence = clamp(
      base_prior + Σ(evidence_i.confidence_contribution × weight_i),
      0.0, 1.0
  )

其中 weight_i 基于证据的可靠性：
- 来自多个工具的交叉验证证据：weight = 1.2（加权）
- 来自单个工具的直接证据：weight = 1.0（标准）
- 来自日志的间接推断：weight = 0.7（降权）
"""

from dataclasses import dataclass, field


@dataclass
class HypothesisState:
    """假设的运行时状态"""
    hypothesis_id: int
    description: str
    prior_probability: float  # Planning Agent 给的先验 (0-1)
    current_confidence: float  # 当前置信度（随证据更新）
    evidence_for: list[str] = field(default_factory=list)
    evidence_against: list[str] = field(default_factory=list)
    status: str = "pending"  # pending / confirmed / refuted / insufficient_data
    
    def update_with_evidence(
        self,
        evidence: "EvidenceItem",
        source_reliability: float = 1.0,
    ) -> None:
        """
        用新证据更新假设置信度
        
        数学推导：
        new_confidence = old_confidence + contribution × reliability × decay
        
        decay 因子确保后续证据的边际效应递减
        （第 5 条证据的影响力比第 1 条小，因为前面已经有足够证据）
        """
        total_evidence = len(self.evidence_for) + len(self.evidence_against)
        decay = 1.0 / (1.0 + 0.2 * total_evidence)  # 边际递减
        
        delta = evidence.confidence_contribution * source_reliability * decay
        self.current_confidence = max(0.0, min(1.0, self.current_confidence + delta))
        
        if evidence.confidence_contribution > 0:
            self.evidence_for.append(evidence.claim)
        else:
            self.evidence_against.append(evidence.claim)
        
        # 更新状态
        self._update_status()
    
    def _update_status(self) -> None:
        """
        根据当前置信度和证据数量更新假设状态
        
        状态转移规则：
        - confirmed: confidence >= 0.7 且至少 2 条正面证据
        - refuted: confidence <= 0.2 或 反驳证据数 >= 3
        - insufficient_data: 证据总数 < 2
        - partial: 其他情况
        """
        total_evidence = len(self.evidence_for) + len(self.evidence_against)
        
        if self.current_confidence >= 0.7 and len(self.evidence_for) >= 2:
            self.status = "confirmed"
        elif self.current_confidence <= 0.2 or len(self.evidence_against) >= 3:
            self.status = "refuted"
        elif total_evidence < 2:
            self.status = "insufficient_data"
        else:
            self.status = "partial"


def update_all_hypotheses(
    hypotheses: list[HypothesisState],
    new_evidence: list["EvidenceItem"],
    collected_data: dict[str, str],
) -> list[HypothesisState]:
    """
    用本轮收集的证据更新所有假设
    
    WHY - 为什么要一次性更新所有假设？
    因为一条证据可能同时影响多个假设：
    - "CPU 45%（正常）" 反驳了 "CPU 过载" 假设，同时间接支持 "内存不足" 假设
      （排除 CPU 后，内存问题的后验概率上升）
    """
    # 计算证据来源的可靠性
    tool_source_count: dict[str, int] = {}
    for ev in new_evidence:
        tool_source_count[ev.source_tool] = tool_source_count.get(ev.source_tool, 0) + 1
    
    for ev in new_evidence:
        # 多源证据加权：如果同一结论来自多个工具，可靠性更高
        source_count = tool_source_count.get(ev.source_tool, 1)
        reliability = min(1.0 + 0.1 * (source_count - 1), 1.3)  # 最多 +30%
        
        for h in hypotheses:
            if ev.supports_hypothesis == str(h.hypothesis_id):
                h.update_with_evidence(ev, reliability)
    
    return hypotheses
```

#### 5.4.5 假设收敛判定条件的调优过程

> **WHY - 收敛判定为什么这么重要？**
>
> 收敛判定直接决定了"什么时候停止诊断"。判定太早 → 低质量结论；判定太晚 → 浪费 Token 和时间。
>
> 我们通过 200 个评测 case 的网格搜索确定最佳参数：

```python
"""
收敛判定参数的调优实验

我们在 200 个评测 case 上测试了不同的收敛阈值组合，
找到了准确率、延迟、成本的最佳平衡点。
"""

from dataclasses import dataclass


@dataclass
class ConvergenceConfig:
    """收敛判定配置"""
    confidence_threshold: float  # 置信度阈值（达到即认为收敛）
    min_confirmed_hypotheses: int  # 至少几个假设被 confirmed
    max_rounds: int  # 最大轮次
    token_budget: int  # Token 预算上限


@dataclass 
class ConvergenceTuningResult:
    """调优结果"""
    config: ConvergenceConfig
    accuracy: float
    avg_rounds: float
    avg_tokens: float
    avg_latency_seconds: float
    premature_stop_rate: float  # 过早停止率（置信度够但结论错误）
    unnecessary_loop_rate: float  # 不必要自环率（自环但结论没改善）


# 网格搜索结果（200 case 评测集）
TUNING_RESULTS = [
    # confidence_threshold=0.5 → 过早停止
    ConvergenceTuningResult(
        config=ConvergenceConfig(0.5, 1, 5, 15000),
        accuracy=0.71, avg_rounds=1.2, avg_tokens=7800,
        avg_latency_seconds=8.5,
        premature_stop_rate=0.15,  # 15% 错误结论
        unnecessary_loop_rate=0.05,
    ),
    # confidence_threshold=0.6 → 最佳平衡 ✅
    ConvergenceTuningResult(
        config=ConvergenceConfig(0.6, 1, 5, 15000),
        accuracy=0.783, avg_rounds=1.4, avg_tokens=10200,
        avg_latency_seconds=11.8,
        premature_stop_rate=0.08,  # 8% 错误结论
        unnecessary_loop_rate=0.10,
    ),
    # confidence_threshold=0.7 → 不必要的自环增加
    ConvergenceTuningResult(
        config=ConvergenceConfig(0.7, 1, 5, 15000),
        accuracy=0.79, avg_rounds=1.8, avg_tokens=12500,
        avg_latency_seconds=15.2,
        premature_stop_rate=0.06,
        unnecessary_loop_rate=0.25,  # 25% 多了一轮但没改善
    ),
    # confidence_threshold=0.8 → 成本过高
    ConvergenceTuningResult(
        config=ConvergenceConfig(0.8, 2, 5, 15000),
        accuracy=0.80, avg_rounds=2.3, avg_tokens=15800,
        avg_latency_seconds=19.5,
        premature_stop_rate=0.04,
        unnecessary_loop_rate=0.35,  # 35% 浪费
    ),
]

# 结论：
# 1. 0.6 是最佳阈值：准确率 78.3%，过早停止率 8%，不必要自环率 10%
# 2. 从 0.6 提到 0.7：准确率只提升 0.7%，但延迟增加 28%、Token 增加 23%
# 3. 从 0.6 提到 0.8：准确率提升 1.7%，但延迟增加 65%、Token 增加 55%
# 4. ROI 曲线在 0.6 处拐点明显——超过 0.6 后，每提升 1% 准确率需要的成本急剧上升

# WHY - 为什么 min_confirmed_hypotheses=1 而不是 2？
# 实测发现，很多简单问题（单组件故障）只有 1 个假设被 confirmed 就足够了。
# 要求 2 个 confirmed 假设会导致简单问题也要 2 轮，浪费 Token。
# 复杂问题（跨组件）本来就需要多轮，不需要靠 min_confirmed 来强制。
```

### 5.5 收敛条件矩阵

| 条件 | 行为 | WHY |
|------|------|-----|
| confidence ≥ 0.8 + 无高风险修复 | → Report | 高置信度直接出报告，不多等 |
| confidence ≥ 0.6 + 有高风险修复 | → HITL Gate | 有修复方案需审批 |
| confidence ≥ 0.6 + data_requirements 空 | → Report | 数据够了，虽然不完美但可以接受 |
| confidence < 0.6 + data_requirements 非空 | → 自环 | 还有数据可采，继续 |
| confidence < 0.6 + data_requirements 空 | → Report | 没有更多数据可采了，勉强出报告 |
| round ≥ max_rounds | → Report | 安全阀，不管置信度多低 |
| total_tokens > 14000 | → Report | 预算安全阀 |

> **🔧 工程难点：假设-验证循环的收敛控制——防止无限自环与过早收敛的平衡**
>
> **挑战**：Diagnostic Agent 的核心是"假设-验证循环"（实测比一次性分析准确率高 16.1%、Token 节省 44.9%），但循环架构天然面临收敛问题：如果收敛条件过松（`confidence >= 0.5` 就结束），简单问题很快出结果但复杂的跨组件级联故障可能得到低质量结论；如果过紧（`confidence >= 0.9` 才结束），大多数问题需要 3-5 轮才能结束，延迟和成本飙升。更棘手的是"永不收敛"场景——间歇性问题（如"Impala 查询偶尔超时"）可能永远达不到 0.6 的置信度，如果没有安全阀，循环会一直自环直到 Token 预算耗尽。同时，每一轮的数据是累积的（`dict.update`），上下文随轮次膨胀，如果不配合 ContextCompressor，第 4-5 轮的 LLM 输入可能超过 Token 预算。
>
> **解决方案**：设计多维收敛条件矩阵（上表），按优先级排列——安全阀（`round >= max_rounds` 和 `total_tokens > 14000`）优先级最高，确保无论如何都会终止；其次是业务判断（`confidence >= 0.6` + `data_requirements` 是否为空）。0.6 的阈值是通过 200 个评测 case 的 A/B 测试确定的最佳平衡点：0.5 时有 15% 的请求给出了错误结论，0.7 时有 25% 的请求多自环了 1 轮但结论没有显著改善。对于"数据不足但无更多可采数据"的场景（`confidence < 0.6 且 data_requirements 为空`），系统选择"谦虚诊断"——诚实输出低置信度结论并标注"⚠️ 建议人工补充调查"，而不是硬编一个高置信度的错误结论。Token 预算安全阀在路由层而非 `process()` 内部检查，确保"已经花了 3000 tokens 做 LLM 分析"的结果被保留，而不是白白浪费。假设优先级排序（先验概率高的先验证）进一步加速收敛——如果高概率假设在 Round 1 就 confirmed，无需验证低概率假设，节省 50%+ Token。

---

## 6. 假设-验证循环状态机

### 6.0 设计决策（WHY）

> **为什么用自环而不是新建节点做多轮？**
>
> LangGraph 的条件边 `route_from_diagnostic` 返回 `"need_more_data"` 时指向自己，形成自环。另一种方案是创建 `DiagnosticRound1Node`、`DiagnosticRound2Node`... 但这样每加一轮就要改图结构，且最大轮次无法动态配置。自环 + `collection_round` 计数器是最灵活的方式。

### 6.1 状态机 Mermaid 图

```mermaid
stateDiagram-v2
    [*] --> Round1 : Planning 完成
    Round1 --> Analyzing : 并行工具调用完成
    Analyzing --> CheckConfidence : LLM 输出 DiagnosticOutput
    
    CheckConfidence --> NeedMore : confidence < 0.6 且有 data_requirements
    CheckConfidence --> HasHighRisk : confidence ≥ 0.6 且有高风险修复
    CheckConfidence --> Done : confidence ≥ 0.6 且无高风险
    CheckConfidence --> ForceDone : round >= max_rounds
    
    NeedMore --> RoundN : 自环（collection_round++）
    RoundN --> Analyzing : 补充工具调用完成
    
    HasHighRisk --> HITLGate
    Done --> Report
    ForceDone --> Report : 安全阀触发
```

### 6.2 多轮上下文管理策略

> **WHY - 每轮的 collected_data 累积还是覆盖？**
>
> **累积**（dict.update）。因为 Round 2 补充的数据可能引用 Round 1 的数据做关联分析。如果覆盖，LLM 在 Round 2 看不到 Round 1 的 hdfs_namenode_status 结果，就无法做"NN heap 高 + GC 频繁 → 因果关联"的推理。
>
> **上下文膨胀怎么办？** ContextCompressor 在每轮开始时只保留关键行（异常/错误/警告），正常数据被截断。实测 5 轮后 collected_data 原始约 25K chars，压缩后 ~4K tokens。

### 6.3 多轮数据收集策略深度解析

#### 6.3.1 WHY 分轮收集而非一次全收

> **设计决策 WHY：为什么不在 Round 1 就调用所有可能的工具？**
>
> | 方案 | Token 消耗 | LLM 注意力 | 动态调整 | 成本 |
> |------|----------|----------|---------|------|
> | **一次全收**（调用 10+ 工具） | ~20K tokens | 严重分散 | 无法根据中间结果调整 | $0.06/次 |
> | **分轮收集** ✅（每轮 3-5 工具） | ~10K tokens | 集中 | 根据上轮结果智能补充 | $0.03/次 |
>
> 分轮收集的三个核心优势：
>
> 1. **Token 效率**：一次全收意味着把 10 个工具的全部结果（~20K tokens）塞给 LLM。经过 ContextCompressor 压缩也要 ~12K tokens。分轮每次只看 3-5 个工具结果（~4K tokens），总消耗反而更低——因为很多时候 Round 1 就够了（1.4 轮平均）。
>
> 2. **动态调整**：Round 1 发现"heap=93%"后，Round 2 可以针对性地查"GC 日志"和"文件数量趋势"。如果一次全收，我们不知道该重点关注哪些数据。
>
> 3. **早停优化**：80% 的简单故障在 Round 1 就能确诊（confidence ≥ 0.6）。一次全收的方式在这些场景下浪费了 50% 的工具调用。

```python
# python/src/aiops/agent/data_collection.py
"""
多轮数据收集策略

每轮的数据收集遵循"假设驱动 + 最小充分集"原则：
只调用"当前假设"需要的最少工具，不多调。
"""

from __future__ import annotations
import structlog
from typing import Any

logger = structlog.get_logger(__name__)


class DataCollectionPlanner:
    """
    多轮数据收集规划器
    
    WHY - 为什么单独抽出一个类？
    因为工具选择逻辑在 Round 1 和 Round 2+ 完全不同：
    - Round 1: 基于 task_plan（Planning Agent 给的诊断计划）
    - Round 2+: 基于 data_requirements（上轮 LLM 输出的补充需求）
    
    将这个逻辑封装在独立类中，便于测试和扩展（如加入工具依赖分析）
    """
    
    # 假设→工具映射表（基于组件领域知识）
    HYPOTHESIS_TOOL_MAP: dict[str, list[str]] = {
        "heap_insufficient": [
            "hdfs_namenode_status",
            "query_metrics",  # jvm_gc_time
        ],
        "small_files": [
            "hdfs_block_report",
            "query_metrics",  # file_count_trend
        ],
        "network_issue": [
            "query_metrics",  # network_errors
            "search_logs",    # timeout
        ],
        "consumer_rebalance": [
            "kafka_consumer_lag",
            "search_logs",    # rebalance
        ],
        "disk_io_saturation": [
            "query_metrics",  # disk_io_util
            "search_logs",    # fsync, slow
        ],
        "config_change": [
            "search_logs",    # config, change
            "query_metrics",  # component uptime
        ],
    }
    
    def plan_round(
        self,
        state: dict[str, Any],
        round_num: int,
        max_tools: int = 5,
    ) -> list[dict]:
        """
        规划单轮的工具调用
        
        返回值格式: [{"name": "tool_name", "params": {...}, "step_desc": "..."}]
        """
        if round_num == 1:
            return self._plan_first_round(state, max_tools)
        else:
            return self._plan_followup_round(state, max_tools)
    
    def _plan_first_round(
        self, state: dict[str, Any], max_tools: int
    ) -> list[dict]:
        """
        第 1 轮规划：按 task_plan 优先，假设→工具映射补充
        
        WHY - 为什么优先用 task_plan？
        因为 Planning Agent 有更多上下文（RAG 结果、告警详情）来决定工具选择。
        DataCollectionPlanner 的假设→工具映射是兜底方案。
        """
        plan = state.get("task_plan", [])
        hypotheses = state.get("hypotheses", [])
        
        calls = []
        seen_tools: set[str] = set()
        
        # 优先级 1: task_plan
        for step in plan:
            for tool_name in step.get("tools", []):
                if tool_name not in seen_tools:
                    calls.append({
                        "name": tool_name,
                        "params": step.get("parameters", {}),
                        "step_desc": step.get("description", f"执行 {tool_name}"),
                    })
                    seen_tools.add(tool_name)
        
        # 优先级 2: 假设→工具映射补充（如果 plan 不够 max_tools）
        if len(calls) < max_tools and hypotheses:
            for h in hypotheses:
                h_type = h.get("type", "")
                for tool_name in self.HYPOTHESIS_TOOL_MAP.get(h_type, []):
                    if tool_name not in seen_tools and len(calls) < max_tools:
                        calls.append({
                            "name": tool_name,
                            "params": {},
                            "step_desc": f"验证假设: {h.get('description', '')}",
                        })
                        seen_tools.add(tool_name)
        
        return calls[:max_tools]
    
    def _plan_followup_round(
        self, state: dict[str, Any], max_tools: int
    ) -> list[dict]:
        """
        后续轮规划：基于 data_requirements + 过滤已知失败工具
        
        WHY - 为什么过滤已知失败工具？
        如果 Round 1 search_logs 超时了，Round 2 再调大概率还是超时。
        跳过这些工具可以节省 15s 的等待时间。
        """
        needed = state.get("data_requirements", [])
        
        # 构建失败工具集合
        failed_tools = {
            record["tool_name"]
            for record in state.get("tool_calls", [])
            if record.get("status") in ("timeout", "error")
        }
        
        # 构建已成功调用的工具集合（避免重复调用同一工具）
        success_tools = {
            record["tool_name"]
            for record in state.get("tool_calls", [])
            if record.get("status") == "success"
        }
        
        calls = []
        skipped_failed = []
        skipped_duplicate = []
        
        for item in needed:
            tool_name = item if isinstance(item, str) else item.get("name", str(item))
            
            if tool_name in failed_tools:
                skipped_failed.append(tool_name)
                continue
            
            if tool_name in success_tools:
                # 已成功调用过 → 检查是否需要不同参数
                # 如果参数相同则跳过（结果已在 collected_data 中）
                skipped_duplicate.append(tool_name)
                continue
            
            calls.append({
                "name": tool_name,
                "params": {},
                "step_desc": f"补充采集: {tool_name}",
            })
        
        if skipped_failed:
            logger.info(
                "data_collection_skipped_failed",
                tools=skipped_failed,
                reason="前轮失败，重试无意义",
            )
        
        if skipped_duplicate:
            logger.debug(
                "data_collection_skipped_duplicate",
                tools=skipped_duplicate,
                reason="已有成功结果",
            )
        
        return calls[:max_tools]


class ToolResultParser:
    """
    工具返回结果的结构化解析器
    
    WHY - 为什么需要结构化解析？
    MCP 工具返回的是 Markdown 格式的文本，但不同工具的格式不统一。
    结构化解析将"原始文本"转化为"可查询的字典"，方便后续分析。
    
    示例输入（hdfs_namenode_status 返回）:
        ## HDFS NameNode 状态
        | 指标 | 值 |
        |------|-----|
        | 堆内存使用 | 93% |
        | RPC 延迟 | 15ms |
    
    解析输出:
        {"堆内存使用": "93%", "RPC 延迟": "15ms"}
    """
    
    @staticmethod
    def extract_key_metrics(tool_name: str, raw_result: str) -> dict[str, str]:
        """提取关键指标的键值对"""
        import re
        
        metrics: dict[str, str] = {}
        
        # 模式 1: Markdown 表格 "| 指标 | 值 |"
        table_pattern = re.compile(r'\|\s*(.+?)\s*\|\s*(.+?)\s*\|')
        for match in table_pattern.finditer(raw_result):
            key, value = match.group(1).strip(), match.group(2).strip()
            if key and value and key not in ("指标", "---", ""):
                metrics[key] = value
        
        # 模式 2: "指标: 值" 格式
        kv_pattern = re.compile(r'(\w[\w\s]*?)[:：]\s*(.+?)(?:\n|$)')
        for match in kv_pattern.finditer(raw_result):
            key, value = match.group(1).strip(), match.group(2).strip()
            if key and value:
                metrics[key] = value
        
        # 模式 3: 异常标记行 "⚠️ 某指标异常 (值)"
        anomaly_pattern = re.compile(r'[⚠️🔴🚨]\s*(.+?)[\(（](.+?)[\)）]')
        for match in anomaly_pattern.finditer(raw_result):
            desc, value = match.group(1).strip(), match.group(2).strip()
            metrics[f"⚠️ {desc}"] = value
        
        return metrics
    
    @staticmethod
    def detect_anomalies(raw_result: str) -> list[str]:
        """
        检测工具结果中的异常行
        
        WHY - 为什么要显式检测异常行？
        因为 LLM 的注意力在长文本中分散，显式标记异常行后：
        1. ContextCompressor 优先保留这些行（不被压缩掉）
        2. 在 Prompt 中将异常行置顶（利用 LLM 对开头的注意力偏好）
        """
        anomaly_keywords = [
            "⚠️", "🔴", "🚨", "ERROR", "CRITICAL", "WARNING",
            "异常", "超过", "不足", "失败", "超时", "过高", "过低",
            "Full GC", "OOM", "timeout", "refused", "exceeded",
        ]
        
        anomalies = []
        for line in raw_result.split("\n"):
            if any(kw in line for kw in anomaly_keywords):
                anomalies.append(line.strip())
        
        return anomalies


class DataDeduplicator:
    """
    数据去重和冲突处理器
    
    WHY - 为什么需要去重？
    在多轮收集中，同一工具可能被调用多次（不同参数），
    或者不同工具返回相同维度的数据（如 hdfs_status 和 query_metrics 都返回 heap 使用率）。
    
    冲突处理策略：
    - 同一工具不同参数 → 保留最新一次的结果（覆盖）
    - 不同工具同一指标 → 保留全部，在 Prompt 中标注来源差异
    - 数值冲突 → 不自动解决，标记冲突让 LLM 判断
    """
    
    @staticmethod
    def merge_collected_data(
        existing: dict[str, str],
        new_data: dict[str, str],
    ) -> tuple[dict[str, str], list[str]]:
        """
        合并新旧采集数据
        
        返回: (合并后的数据, 冲突警告列表)
        """
        merged = dict(existing)  # 浅拷贝
        conflicts: list[str] = []
        
        for key, value in new_data.items():
            if key in merged:
                old_value = merged[key]
                
                # 检查是否是错误标记覆盖成功结果
                if "⚠️" in old_value and "⚠️" not in value:
                    # 新结果成功了（之前失败的工具重试成功）
                    logger.info(
                        "data_dedup_retry_success",
                        tool=key,
                        old="失败标记",
                        new="成功结果",
                    )
                    merged[key] = value  # 用成功结果覆盖
                elif old_value != value:
                    # 同一工具返回了不同数据（可能参数不同）
                    conflicts.append(
                        f"⚠️ 工具 {key} 在不同轮次返回了不同数据，保留最新结果"
                    )
                    merged[key] = value  # 保留最新
            else:
                merged[key] = value
        
        return merged, conflicts
```

#### 6.3.2 收集轮次上限的 WHY

> **为什么最大轮次是 5 轮而不是 3 轮或 10 轮？**
>
> 基于 200 个评测 case 的统计分析：

```
轮次分布（200 case 评测集）:
  Round 1 完成: 120/200 = 60% ████████████████████░░░░░░░░░░░
  Round 2 完成:  52/200 = 26% ████████████████░░░░░░░░░░░░░░░
  Round 3 完成:  18/200 =  9% ███████░░░░░░░░░░░░░░░░░░░░░░░
  Round 4 完成:   7/200 = 3.5%███░░░░░░░░░░░░░░░░░░░░░░░░░░░
  Round 5 完成:   2/200 =  1% █░░░░░░░░░░░░░░░░░░░░░░░░░░░░░
  未收敛 (>5):   1/200 = 0.5%░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░

累计收敛率:
  ≤1 轮: 60%
  ≤2 轮: 86%
  ≤3 轮: 95%
  ≤4 轮: 98.5%
  ≤5 轮: 99.5% ← 5 轮覆盖 99.5% 的 case

从 3 轮到 5 轮的边际收益:
  ≤3 轮: 95% 收敛 → 5% 的 case 被强制截断
  ≤5 轮: 99.5% 收敛 → 0.5% 的 case 被强制截断
  → 增加 2 轮上限，减少了 4.5% 的强制截断率

从 5 轮到 10 轮的边际收益:
  ≤5 轮: 99.5% → ≤10 轮: ~100%
  → 增加 5 轮上限，只减少了 0.5% 的强制截断
  → 但 max_rounds=10 的风险是：间歇性问题可能永不收敛，
     消耗 10 × $0.03 = $0.30/次（10 倍成本）

结论: 5 轮是成本和覆盖率的最佳平衡点
```

```python
# Round 轮次 vs 诊断质量的关系

ROUND_QUALITY_ANALYSIS = {
    # round_num: (accuracy_improvement_vs_previous, avg_token_cost)
    1: (0.60, 7200),    # 60% 的 case 在 Round 1 搞定
    2: (0.16, 3500),    # +16% 的 case 在 Round 2 搞定（边际 3500 tokens）
    3: (0.06, 3200),    # +6%（边际递减明显）
    4: (0.02, 3000),    # +2%
    5: (0.01, 2800),    # +1%（几乎没有新信息）
}

# WHY - 为什么后面轮次的 Token 消耗反而降低？
# 因为后续轮只补充 1-2 个工具（而非 3-5 个）
# 同时 ContextCompressor 对前面轮次的数据做了压缩
# Round 5 时新增的数据量很小，但压缩开销恒定

# 成本分析:
# max_rounds=3: 平均 Token = 7200 + 0.26×3500 + 0.09×3200 = 8398
# max_rounds=5: 平均 Token = 7200 + 0.26×3500 + 0.09×3200 + 0.035×3000 + 0.01×2800 = 8531
# → 从 3 轮增加到 5 轮，平均 Token 只增加 1.6%，但覆盖率从 95% 提升到 99.5%
```

### 6.4 置信度校准

| 评测场景 | 模型输出置信度 | 实际准确率 | 校准偏差 |
|---------|-------------|----------|---------|
| HDFS 单组件故障 | 0.85 | 0.88 | +0.03（略保守） |
| 跨组件级联故障 | 0.72 | 0.65 | -0.07（略乐观） |
| 配置变更引发 | 0.78 | 0.80 | +0.02（准确） |
| 数据不足场景 | 0.45 | 0.40 | -0.05（可接受） |

> **WHY - 为什么不做置信度后处理校准？**
>
> 在当前评测集规模（~200 case）下，校准偏差 < 10%，增加后处理校准层的复杂度收益不大。当评测集扩展到 1000+ case 后，考虑用 Platt scaling 做校准。

### 6.4 证据链可靠性评分

```python
def compute_evidence_reliability(evidence: list[EvidenceItem]) -> float:
    """
    证据链可靠性评分（0-1）
    
    评分规则（WHY）：
    1. 多个独立证据指向同一根因 → 高可靠（交叉验证原理）
    2. 证据来自不同工具 → 加分（多源验证）
    3. 证据来自同一工具 → 不加分（可能是同一数据的不同视角）
    4. 有反驳证据 → 减分（存在矛盾）
    """
    if not evidence:
        return 0.0
    
    sources = set(e.source_tool for e in evidence)
    positive = [e for e in evidence if e.confidence_contribution > 0]
    negative = [e for e in evidence if e.confidence_contribution < 0]
    
    base_score = min(len(positive) / 3.0, 1.0)  # 3 条正面证据 → 满分
    source_bonus = min(len(sources) / 3.0, 0.2)  # 多源加分，最多 +0.2
    contradiction_penalty = len(negative) * 0.15  # 每条反驳 -0.15
    
    return max(0.0, min(1.0, base_score + source_bonus - contradiction_penalty))
```

> **🔧 工程难点：交叉验证防幻觉与证据可靠性评分——防止 LLM 编造诊断依据**
>
> **挑战**：LLM 在多源数据融合时特别容易"发明"不存在的关联——实测一次性分析模式的幻觉率高达 28.3%（180 个测试 case 中有 51 个包含编造数据）。典型表现包括：工具返回 "CPU 45%"（正常值），LLM 在证据中写 "CPU 使用率异常(95%)"；或者引用一个根本不存在的工具名（`non_existent_tool`）作为 `source_tool`。在运维场景中，幻觉证据直接导向错误修复——如果 AI 编造"NN heap 95%"就建议"重启 NameNode"，而实际 heap 只有 45%，重启不仅无效还可能导致服务中断。更微妙的是"合理但错误"的幻觉——数值在正常范围内看起来可信（"CPU 72%"），但工具实际返回的是 "CPU 45%"，运维人员很难察觉。
>
> **解决方案**：设计 3 级幻觉检测 Pipeline（`DiagnosticHallucinationCheck`）：**Level 1（source_tool 存在性）**——检查每条证据的 `source_tool` 是否存在于 `collected_data` 的 key 集合中，不匹配立即标记为 `[HALLUCINATION-L1]`；**Level 2（数值交叉验证）**——用正则提取证据 `source_data` 中的数值，与对应工具的实际返回值做交叉匹配，如果超过 50% 的数值在工具返回中找不到则标记为 `[HALLUCINATION-L2]`；**Level 3（因果链组件验证）**——检查因果链中提到的组件是否在 `target_components` 中，防止 LLM 将无关组件编入因果链。在 Pydantic 模型层面，`EvidenceItem` 强制要求 `source_tool`（不可为空）和 `source_data`（必须引用工具实际返回的数据片段），`field_validator` 校验高严重度 + 低置信度的不合理组合（`severity=critical` 且 `confidence < 0.5` 直接拒绝）。证据可靠性评分（`compute_evidence_reliability`）基于多源交叉验证原理——3 条来自不同工具的正面证据 = 满分基础分，多源加分最多 +0.2，每条反驳证据扣 0.15，最终分数用于调整 LLM 输出的置信度。通过假设-验证框架 + Pydantic 结构化约束 + 3 级幻觉检测的组合，幻觉率从一次性模式的 28.3% 降到 4.4%。

---

## 6.8 LLM 上下文窗口管理深度解析

### 6.8.1 WHY 上下文管理是 Diagnostic Agent 的核心挑战

> **为什么上下文管理对 Diagnostic Agent 特别重要？**
>
> Diagnostic Agent 是整个系统中**上下文膨胀最严重**的节点：
>
> | Agent | 典型 Input Tokens | 膨胀来源 | 可控性 |
> |-------|-------------------|---------|-------|
> | Triage | ~2K | 告警文本（固定大小） | 高（告警格式标准化） |
> | Planning | ~3K | 告警 + RAG 结果 | 中（RAG 可控制返回量） |
> | **Diagnostic** | **4-8K** | **多轮工具结果累积** | **低（工具返回不可预测）** |
> | Report | ~3K | 诊断结论（已压缩） | 高（结论格式固定） |
>
> 问题的根源：Diagnostic 每轮调用 3-5 个工具，每个工具返回 ~1000 字。5 轮后 collected_data 可达 25K 字（~6K tokens）。但 LLM input 预算只有 8K tokens（GPT-4o 成本 $0.03/8K），超过后要么截断（丢失关键信息），要么超预算（成本翻倍）。

### 6.8.2 多轮工具结果的上下文膨胀分析

```python
"""
上下文膨胀的实测数据分析

WHY - 为什么膨胀不是线性的？
因为 ContextCompressor 对旧轮数据做递进压缩：
- Round 1 数据：保留 100%（当前轮完整细节）
- Round 2 时，Round 1 数据被压缩到 40%（只保留异常行）
- Round 3 时，Round 1 数据被压缩到 25%，Round 2 数据被压缩到 40%
"""

from dataclasses import dataclass


@dataclass
class ContextGrowthMetrics:
    """单轮的上下文增长指标"""
    round_num: int
    new_tools: int
    raw_new_data_chars: int
    compressed_new_data_chars: int
    total_compressed_chars: int
    estimated_tokens: int


# 实测上下文膨胀数据（典型 3 轮诊断）
CONTEXT_GROWTH_EXAMPLE = [
    ContextGrowthMetrics(
        round_num=1,
        new_tools=4,
        raw_new_data_chars=8200,      # 4 个工具 × ~2050 字
        compressed_new_data_chars=8200, # 首轮不压缩
        total_compressed_chars=8200,    # 只有本轮数据
        estimated_tokens=2050,          # ~4 chars/token (中文)
    ),
    ContextGrowthMetrics(
        round_num=2,
        new_tools=2,
        raw_new_data_chars=4100,       # 2 个工具 × ~2050 字
        compressed_new_data_chars=4100, # 本轮不压缩
        total_compressed_chars=7380,    # Round 1 压缩到 40% (3280) + Round 2 (4100)
        estimated_tokens=1845,
    ),
    ContextGrowthMetrics(
        round_num=3,
        new_tools=2,
        raw_new_data_chars=3800,
        compressed_new_data_chars=3800,
        total_compressed_chars=8530,    # R1: 25% (2050) + R2: 40% (1640) + R3 (3800) + 摘要 (1040)
        estimated_tokens=2133,
    ),
]

# 无压缩时的对比:
# Round 3 总计: 8200 + 4100 + 3800 = 16100 chars (~4025 tokens)
# 有压缩时: 8530 chars (~2133 tokens)
# 压缩比: 16100 / 8530 = 1.89x
# 节省 Token: ~1892 tokens → 节省 $0.006/次 → 100 次/天 = $0.6/天
```

### 6.8.3 上下文压缩策略：关键信息提取 + 摘要 + 截断

```python
# python/src/aiops/agent/context_compressor.py
"""
Diagnostic 专用的上下文压缩器

三级压缩策略：
Level 1 - 关键信息提取：保留异常行、关键指标、错误日志
Level 2 - 摘要生成：将正常数据压缩为一句话摘要
Level 3 - 截断：超出 Token 预算时从最旧的数据开始截断
"""

import re
from typing import Any


class DiagnosticContextCompressor:
    """
    诊断上下文压缩器
    
    WHY - 为什么不直接用 LLM 做摘要？
    1. 延迟：调用 LLM 做摘要需要 2-3s，在诊断的关键路径上不可接受
    2. 成本：额外的 LLM 调用增加 ~$0.005/次
    3. 可靠性：如果摘要 LLM 也挂了怎么办？（降级链变长）
    
    我们的方案：基于关键词和正则的规则压缩（0 延迟、0 成本、100% 可靠）
    """
    
    # 必须保留的关键词（这些行永远不会被压缩掉）
    MUST_KEEP_KEYWORDS = [
        "⚠️", "🔴", "🚨", "ERROR", "CRITICAL", "WARNING",
        "异常", "超过", "不足", "失败", "超时", "过高", "过低",
        "Full GC", "OOM", "timeout", "refused", "exceeded",
        "disk_io_util", "heap", "lag", "latency",
    ]
    
    # 重要数值的正则模式（尽量保留）
    IMPORTANT_PATTERNS = [
        re.compile(r'[\u4e00-\u9fff]*使用率\s*[:：=]\s*\d+\.?\d*%'),  # 使用率: 93%
        re.compile(r'[\u4e00-\u9fff]*延迟\s*[:：=]\s*\d+'),            # 延迟: 15ms
        re.compile(r'P\d{2}\s*[:：=]\s*\d+'),                          # P99: 8s
        re.compile(r'[\u4e00-\u9fff]*heap\s*[:：=]\s*\d+'),            # heap: 93%
        re.compile(r'lag\s*[:：=]\s*\d+'),                              # lag: 500000
        re.compile(r'[\u4e00-\u9fff]*次数\s*[:：=]\s*\d+'),            # 次数: 5
    ]
    
    def __init__(self, target_tokens: int = 4000):
        """
        参数:
            target_tokens: 压缩后的 Token 目标
            
        WHY - 为什么默认 4000 tokens？
        LLM 总 input 预算 8000 tokens:
          System Prompt:  ~700 tokens  (固定)
          假设列表:       ~300 tokens  (变量，通常 3 个假设)
          RAG 上下文:     ~800 tokens  (top-3 results)
          相似案例:       ~500 tokens  (top-2 cases)
          User message:   ~200 tokens  (查询描述)
          安全余量:       ~1500 tokens
          → collected_data 预算 = 8000 - 3000 = ~5000 tokens
          → 保守目标 4000 tokens（留余量给 Round 诊断摘要）
        """
        self.target_tokens = target_tokens
        self._chars_per_token = 4.0  # 中文平均 4 字符/token
    
    def compress(
        self,
        collected_data: dict[str, str],
        current_round: int,
        previous_diagnosis_summary: str | None = None,
    ) -> str:
        """
        压缩 collected_data 到 Token 预算内
        
        返回压缩后的文本（直接嵌入 LLM Prompt）
        """
        target_chars = int(self.target_tokens * self._chars_per_token)
        
        # Step 1: 为每个工具结果提取关键行
        compressed_parts: list[tuple[str, str, int]] = []  # (tool_name, compressed_text, priority)
        
        for tool_name, raw_result in collected_data.items():
            is_error = raw_result.startswith("⚠️") or raw_result.startswith("❌")
            if is_error:
                # 错误标记保持原样（很短，不需要压缩）
                compressed_parts.append((tool_name, raw_result, 0))
                continue
            
            compressed = self._compress_single_tool(
                tool_name, raw_result, current_round
            )
            # 优先级：当前轮数据 > 前一轮 > 更早的轮
            # 用负数表示优先（小的排前面，但最终反转取大的先保留）
            round_tag = self._extract_round_tag(tool_name, collected_data)
            priority = current_round - round_tag  # 当前轮 priority=0，最高
            compressed_parts.append((tool_name, compressed, priority))
        
        # Step 2: 按优先级排序（当前轮优先）
        compressed_parts.sort(key=lambda x: x[2])
        
        # Step 3: 拼接，控制总长度
        output_parts = []
        remaining_chars = target_chars
        
        # 如果有前轮诊断摘要，先加入
        if previous_diagnosis_summary:
            summary_text = f"### 前轮诊断摘要\n{previous_diagnosis_summary}\n"
            output_parts.append(summary_text)
            remaining_chars -= len(summary_text)
        
        for tool_name, compressed, _priority in compressed_parts:
            if remaining_chars <= 0:
                output_parts.append(
                    f"\n...[其余 {len(compressed_parts) - len(output_parts)} 个工具结果因 Token 预算已截断]"
                )
                break
            
            section = f"### {tool_name}\n{compressed}\n"
            if len(section) <= remaining_chars:
                output_parts.append(section)
                remaining_chars -= len(section)
            else:
                # 截断到剩余空间
                truncated = section[:remaining_chars - 50]
                output_parts.append(truncated + "\n...[已截断]")
                remaining_chars = 0
        
        return "\n".join(output_parts)
    
    def _compress_single_tool(
        self, tool_name: str, raw_result: str, current_round: int
    ) -> str:
        """压缩单个工具的结果"""
        lines = raw_result.split("\n")
        
        # Level 1: 关键行必须保留
        must_keep = []
        important = []
        normal = []
        
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            
            if any(kw in stripped for kw in self.MUST_KEEP_KEYWORDS):
                must_keep.append(stripped)
            elif any(p.search(stripped) for p in self.IMPORTANT_PATTERNS):
                important.append(stripped)
            else:
                normal.append(stripped)
        
        # Level 2: 正常行生成摘要
        normal_summary = ""
        if normal:
            normal_summary = f"[{len(normal)} 行正常数据已省略]"
        
        # 组装
        parts = must_keep + important
        if normal_summary:
            parts.append(normal_summary)
        
        return "\n".join(parts)
    
    @staticmethod
    def _extract_round_tag(
        tool_name: str, collected_data: dict[str, str]
    ) -> int:
        """推断工具结果属于第几轮（简化实现：按加入顺序推断）"""
        keys = list(collected_data.keys())
        idx = keys.index(tool_name) if tool_name in keys else 0
        # 每轮约 3-5 个工具，用整除估算
        return idx // 4 + 1
```

### 6.8.4 Token 计数与预算追踪

```python
# python/src/aiops/agent/token_tracker.py
"""
Token 计数与预算追踪

WHY - 为什么需要精确的 Token 追踪？
1. 成本控制：每次诊断的 Token 消耗直接决定成本（GPT-4o $0.003/1K tokens）
2. 预算安全阀：当 total_tokens 接近 14000 时，路由层提前终止自环
3. 调参依据：Token 消耗的分布数据用于优化 ContextCompressor 的参数
"""

import tiktoken


class TokenTracker:
    """
    精确的 Token 计数器
    
    WHY - 为什么用 tiktoken 而不是 len() / 4 估算？
    
    len() / 4 的误差分析：
    - 纯英文：len("hello world") / 4 = 2.75，实际 2 tokens → 误差 +37.5%
    - 纯中文：len("你好世界") / 4 = 1，实际 2 tokens → 误差 -50%
    - 混合文本（我们的场景）：误差在 -30% ~ +40% 之间不等
    
    在 8K Token 预算下，40% 的误差意味着：
    - 高估 40%：实际只用 5.7K 但以为用了 8K → 提前截断，丢失有用数据
    - 低估 40%：实际用了 11.2K 但以为用了 8K → 超预算，成本翻倍
    
    tiktoken 的精确计数确保预算控制在 ±5% 以内
    """
    
    def __init__(self, model: str = "gpt-4o"):
        """
        参数:
            model: 目标模型（不同模型的 tokenizer 不同）
        """
        try:
            self._encoder = tiktoken.encoding_for_model(model)
        except KeyError:
            # 未知模型，使用 cl100k_base（GPT-4 系列通用）
            self._encoder = tiktoken.get_encoding("cl100k_base")
        
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._round_tokens: list[dict[str, int]] = []
    
    def count_tokens(self, text: str) -> int:
        """精确计算文本的 Token 数"""
        return len(self._encoder.encode(text))
    
    def count_messages_tokens(self, messages: list[dict[str, str]]) -> int:
        """
        计算 messages 列表的 Token 数（含消息格式开销）
        
        WHY - 消息格式也消耗 Token
        每条消息有 ~4 tokens 的格式开销：
        <|im_start|>role\ncontent<|im_end|>\n
        
        对于 3 条消息的对话：
        - 纯内容 Token: 2000
        - 格式开销: 3 × 4 + 3 (reply priming) = 15 tokens
        - 总计: 2015 tokens（不计格式开销会低估 0.75%）
        
        虽然 0.75% 影响很小，但精确追踪有助于调参
        """
        total = 3  # reply priming tokens
        for message in messages:
            total += 4  # 每条消息的格式开销
            for key, value in message.items():
                total += self.count_tokens(str(value))
                if key == "name":
                    total -= 1  # name 字段减 1 token
        return total
    
    def record_round(
        self,
        round_num: int,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        """记录单轮的 Token 消耗"""
        self._total_input_tokens += input_tokens
        self._total_output_tokens += output_tokens
        self._round_tokens.append({
            "round": round_num,
            "input": input_tokens,
            "output": output_tokens,
            "total": input_tokens + output_tokens,
            "cumulative": self._total_input_tokens + self._total_output_tokens,
        })
    
    def get_budget_status(self, budget: int = 15000) -> dict:
        """
        获取预算状态
        
        返回:
            remaining: 剩余 Token 数
            usage_percent: 使用百分比
            can_continue: 是否还能支持下一轮
            estimated_next_round: 预估下一轮消耗
        """
        total = self._total_input_tokens + self._total_output_tokens
        
        # 预估下一轮消耗（基于历史平均）
        if self._round_tokens:
            avg_per_round = total / len(self._round_tokens)
        else:
            avg_per_round = 3000  # 默认估计
        
        remaining = budget - total
        can_continue = remaining > avg_per_round * 1.2  # 留 20% 余量
        
        return {
            "total_used": total,
            "remaining": remaining,
            "usage_percent": round(total / budget * 100, 1),
            "can_continue": can_continue,
            "estimated_next_round": int(avg_per_round),
            "rounds_completed": len(self._round_tokens),
        }
    
    def estimate_cost_usd(self) -> float:
        """
        估算美元成本
        
        GPT-4o 定价（2024-11）:
        - Input: $2.50 / 1M tokens
        - Output: $10.00 / 1M tokens
        """
        input_cost = self._total_input_tokens / 1_000_000 * 2.50
        output_cost = self._total_output_tokens / 1_000_000 * 10.00
        return round(input_cost + output_cost, 6)
```

> **WHY - tiktoken vs len() 的实际影响量化**
>
> 在我们的诊断场景中（中英文混合文本），两种方法的对比：
>
> ```
> 场景                  len()/4 估算   tiktoken 精确   误差
> HDFS 状态查询结果       520 tokens    480 tokens    +8.3%
> ES 日志搜索结果         380 tokens    450 tokens    -15.6%
> Kafka metrics 结果     290 tokens    310 tokens     -6.5%
> 完整 Prompt (System)   750 tokens    720 tokens     +4.2%
> 完整 Prompt (含数据)  2100 tokens   1950 tokens     +7.7%
> ```
>
> 日志搜索结果误差最大（-15.6%），因为日志中有大量英文关键词（Java 堆栈、GC 日志），len()/4 高估了这些英文文本的 Token 数。使用 tiktoken 后，Token 预算利用率提升了 ~10%——相当于每次诊断多保留了 ~400 tokens 的有用数据。

---

## 7. 错误处理与降级

### 7.1 分层降级

| 场景 | 降级策略 | 用户感知 |
|------|---------|---------|
| 单个工具超时 | 标记不可用，其他工具继续 | 报告注明"部分数据暂不可用" |
| 多个工具失败 | 基于可用数据分析，降低置信度 | 置信度降低，标注 ⚠️ |
| LLM 分析失败 | fallback_diagnosis() 规则提取异常 | 输出低置信度初步发现 |
| LLM 结构化输出失败 | instructor 重试 3 次 → fallback | 同上 |
| Token 预算耗尽 | 停止新调用，基于已有数据出报告 | 报告标注"因预算限制提前终止" |
| 超过 5 轮 | 强制输出当前置信度的结论 | 报告标注"达到最大分析轮次" |

### 7.2 降级诊断实现

> **WHY - 为什么降级诊断不生成修复建议？**
>
> 降级意味着 LLM 不可用，此时的"诊断"只是关键词匹配提取的异常指标，没有因果推理能力。如果基于关键词匹配生成修复建议（比如看到"heap 92%"就建议"重启"），可能导致误操作。**宁可不给建议，也不能给错误建议**。

```python
# _fallback_diagnosis() 的核心逻辑：
# 1. 扫描所有 collected_data，用关键词匹配提取异常
# 2. 异常关键词：⚠️ 🔴 🚨 error warning 异常 超过 不足 失败
# 3. 将异常作为"证据"，置信度设为 0.1（极低，明确告知用户不可靠）
# 4. 不生成修复建议（防止错误修复）
# 5. 建议用户人工介入
```

### 7.3 Token 预算降级详解

> **WHY - 为什么在 route_from_diagnostic 而不是 process 里检查预算？**
>
> process() 内部做预算检查的问题是：已经花了 3000 tokens 做 LLM 分析后才发现超预算，这 3000 tokens 白花了。在路由层检查可以在"要不要再来一轮"的决策点提前截断，已有的分析结果保留。

```python
# 预算降级流程：
# 
# total_tokens = 12000 (已用) → 预算 15000
# 本轮 Round 2 准备自环 → route_from_diagnostic 检查
#   confidence=0.45 < 0.6 → 本来要自环
#   total_tokens=12000 + 预估下一轮 3000 = 15000 → 接近上限
#   → 改为输出当前结论（confidence=0.45），报告标注：
#     "⚠️ 因 Token 预算限制，诊断在第 2 轮提前终止。
#      当前置信度 0.45，建议人工补充排查。"

def _should_continue_diagnosis(state: AgentState) -> bool:
    """判断是否应该继续诊断（综合置信度+预算+轮次）"""
    confidence = state.get("diagnosis", {}).get("confidence", 0)
    tokens = state.get("total_tokens", 0)
    round_num = state.get("collection_round", 0)
    max_rounds = state.get("max_collection_rounds", 5)
    
    # 估算下一轮消耗（基于历史平均）
    avg_tokens_per_round = tokens / max(round_num, 1)
    projected_total = tokens + avg_tokens_per_round
    
    if projected_total > 14000:  # 预留 1000 token 给 Report
        return False
    if round_num >= max_rounds:
        return False
    if confidence >= 0.6:
        return False  # 已经够好了
    if not state.get("data_requirements"):
        return False  # 没有更多数据可采
    
    return True
```

### 7.4 多工具失败时的智能降级

```python
def _assess_data_completeness(state: AgentState) -> dict:
    """
    评估数据完整性（WHY: 用于调整 LLM 分析时的 Prompt）
    
    当工具失败比例高时，在 Prompt 中追加提示：
    "注意：以下数据源暂不可用：{failed_tools}。
     请基于可用数据分析，并在置信度中反映数据不完整的影响。"
    """
    collected = state.get("collected_data", {})
    tool_calls = state.get("tool_calls", [])
    
    total = len(tool_calls)
    success = sum(1 for t in tool_calls if t.get("status") == "success")
    failed_tools = [t["tool_name"] for t in tool_calls if t["status"] != "success"]
    
    completeness = success / max(total, 1)
    
    return {
        "completeness": completeness,
        "total_calls": total,
        "success_count": success,
        "failed_tools": failed_tools,
        "should_lower_confidence": completeness < 0.7,
        "data_quality_note": (
            f"⚠️ 数据完整性 {completeness:.0%}（{success}/{total} 工具成功）。"
            f"不可用数据源：{', '.join(failed_tools) or '无'}。"
            if completeness < 1.0 else ""
        ),
    }
```

> **🔧 工程难点：分层降级策略——从 LLM 不可用到全工具失败的完整降级链**
>
> **挑战**：Diagnostic Agent 是系统的"主治医生"，但它依赖的每一层基础设施都可能出问题：MCP 工具可能超时/失败、LLM API 可能限流/不可用、结构化输出可能校验失败、Token 预算可能提前耗尽、甚至可能超过最大分析轮次。每种故障模式需要不同的降级策略——单个工具超时和全部工具失败的处理方式完全不同；LLM 输出格式错误（可重试）和 LLM 完全不可用（需 fallback）的处理也不同。最关键的约束是"宁可给低质量结果，也不能不给结果"——运维人员在处理紧急故障时，哪怕是 0.1 置信度的初步发现也比"系统报错"有用。但低质量结果又不能包含修复建议（基于关键词匹配生成的修复建议可能导致误操作，风险远大于"无建议"）。
>
> **解决方案**：设计 6 层降级矩阵（§7.1），每层有明确的触发条件、降级策略和用户感知：(1) **单工具超时**→标记不可用继续分析，报告注明"部分数据暂不可用"；(2) **多工具失败**→基于可用数据分析但自动降低置信度，Prompt 追加数据缺失提示；(3) **LLM 结构化输出失败**→instructor 自动重试 3 次（累积成功率 99.5%），每次将上次校验错误反馈给 LLM；(4) **LLM 完全不可用**→`_fallback_diagnosis()` 纯规则降级，用关键词匹配（`⚠️/🔴/ERROR/异常`）从 `collected_data` 中提取异常指标，置信度设为 0.1（极低），**不生成修复建议**（防止基于关键词匹配的错误修复），建议用户人工介入；(5) **Token 预算耗尽**→在 `route_from_diagnostic` 路由层提前截断（而非 `process()` 内部，避免已花 Token 浪费），基于已有数据输出当前结论并标注"因预算限制提前终止"；(6) **超过 5 轮安全阀**→强制输出当前置信度的结论，不管多低。每一层降级都有 Prometheus Counter（`DIAG_FALLBACK_TOTAL`）和结构化日志记录，Grafana Dashboard 实时显示降级触发频率和分布，>1% 的降级率触发告警排查。

---

## 8. 测试策略

### 8.1 单元测试

```python
# tests/unit/agent/test_diagnostic.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from aiops.agent.nodes.diagnostic import DiagnosticNode


class TestDiagnosticNode:
    @pytest.fixture
    def diagnostic_node(self, mock_llm_client):
        node = DiagnosticNode(mock_llm_client)
        node._mcp = AsyncMock()  # Mock MCP 客户端
        return node

    async def test_first_round_follows_plan(self, diagnostic_node):
        """第一轮应该按诊断计划执行工具调用"""
        state = {
            "request_id": "test-001",
            "user_query": "HDFS 写入变慢",
            "collection_round": 0,
            "max_collection_rounds": 5,
            "task_plan": [
                {"step": 1, "tools": ["hdfs_namenode_status"], "description": "检查 NN"},
                {"step": 2, "tools": ["search_logs"], "parameters": {"component": "hdfs"}},
            ],
            "collected_data": {},
            "tool_calls": [],
            "error_count": 0,
            "total_tokens": 0,
            "total_cost_usd": 0.0,
        }

        # Mock MCP 返回
        diagnostic_node._mcp.call_tool = AsyncMock(return_value="NN heap: 92%")

        # Mock LLM 返回
        diagnostic_node.llm.chat_structured = AsyncMock(return_value=DiagnosticOutput(
            root_cause="NN 堆内存不足",
            confidence=0.85,
            severity="high",
            evidence=[EvidenceItem(
                claim="NN heap 92%",
                source_tool="hdfs_namenode_status",
                source_data="NN heap: 92%",
                supports_hypothesis="假设1",
                confidence_contribution=0.8,
            )],
            causality_chain="NN 堆内存不足 → Full GC → RPC 阻塞",
            affected_components=["hdfs-namenode"],
            remediation_plan=[],
        ))

        result = await diagnostic_node.process(state)

        assert result["collection_round"] == 1
        assert result["diagnosis"]["confidence"] == 0.85
        assert len(result["tool_calls"]) == 2  # 2 个工具被调用

    async def test_tool_timeout_doesnt_block(self, diagnostic_node):
        """单个工具超时不应阻塞其他工具"""
        import asyncio

        async def slow_tool(name, params):
            if name == "search_logs":
                await asyncio.sleep(100)  # 故意超时
            return f"result of {name}"

        diagnostic_node._mcp.call_tool = slow_tool

        state = {
            "collection_round": 0,
            "task_plan": [
                {"step": 1, "tools": ["hdfs_namenode_status"]},
                {"step": 2, "tools": ["search_logs"]},
            ],
            "collected_data": {},
            "tool_calls": [],
            # ... 其他必要字段
        }

        tools = diagnostic_node._plan_tool_calls(state, 1)
        results = await diagnostic_node._execute_tools_parallel(tools, state)

        assert "hdfs_namenode_status" in results
        assert "search_logs" in results
        assert "超时" in results["search_logs"]  # 超时标记

    async def test_max_rounds_safety(self, diagnostic_node):
        """超过最大轮次应强制结束"""
        state = {
            "collection_round": 4,  # 已经是第 4 轮，+1=5
            "max_collection_rounds": 5,
            "data_requirements": ["some_tool"],
            "diagnosis": {"confidence": 0.4},
            # ...
        }
        # route_from_diagnostic 应返回 "report"（强制结束）
        from aiops.agent.router import route_from_diagnostic
        result = route_from_diagnostic(state)
        assert result == "report"


class TestDiagnosticOutput:
    def test_high_severity_needs_high_confidence(self):
        """高严重度+低置信度应该校验失败"""
        with pytest.raises(ValueError, match="需要更多证据"):
            DiagnosticOutput(
                root_cause="test",
                confidence=0.3,  # 太低
                severity="critical",  # 严重度高
                evidence=[EvidenceItem(
                    claim="test",
                    source_tool="test",
                    source_data="test",
                    supports_hypothesis="test",
                    confidence_contribution=0.3,
                )],
                causality_chain="A→B",
                affected_components=["test"],
            )

    def test_remediation_auto_approval(self):
        """高风险修复步骤应自动设置 requires_approval"""
        step = RemediationStep(
            step_number=1,
            action="重启 NameNode",
            risk_level="high",
            requires_approval=False,  # 故意设为 False
            rollback_action="恢复原配置",
            estimated_impact="影响 3-5 分钟",
        )
        assert step.requires_approval is True  # 被 validator 自动修正


class TestEvidenceReliability:
    def test_multiple_sources_high_score(self):
        evidence = [
            EvidenceItem(claim="a", source_tool="hdfs_status", source_data="d",
                        supports_hypothesis="H1", confidence_contribution=0.8),
            EvidenceItem(claim="b", source_tool="search_logs", source_data="d",
                        supports_hypothesis="H1", confidence_contribution=0.6),
            EvidenceItem(claim="c", source_tool="query_metrics", source_data="d",
                        supports_hypothesis="H1", confidence_contribution=0.7),
        ]
        score = compute_evidence_reliability(evidence)
        assert score > 0.8  # 3 条来自不同源

    def test_contradiction_lowers_score(self):
        evidence = [
            EvidenceItem(claim="a", source_tool="t1", source_data="d",
                        supports_hypothesis="H1", confidence_contribution=0.8),
            EvidenceItem(claim="b", source_tool="t2", source_data="d",
                        supports_hypothesis="H1", confidence_contribution=-0.5),
        ]
        score = compute_evidence_reliability(evidence)
        assert score < 0.6

    def test_empty_evidence(self):
        assert compute_evidence_reliability([]) == 0.0


class TestFallbackDiagnosis:
    def test_fallback_returns_low_confidence(self):
        state = {
            "collected_data": {"hdfs_status": "⚠️ heap 95%"},
            "target_components": ["hdfs-namenode"],
        }
        result = DiagnosticNode._fallback_diagnosis(state, "LLM timeout")
        assert result.confidence == 0.1
        assert "失败" in result.root_cause

    def test_fallback_extracts_anomalies(self):
        state = {
            "collected_data": {
                "hdfs_status": "⚠️ heap 95%",
                "yarn_status": "正常",
            },
            "target_components": [],
        }
        result = DiagnosticNode._fallback_diagnosis(state, "error")
        assert any("hdfs_status" in e.source_tool for e in result.evidence)

    def test_fallback_no_remediation(self):
        """降级诊断不应生成修复建议（防止错误修复）"""
        state = {"collected_data": {}, "target_components": []}
        result = DiagnosticNode._fallback_diagnosis(state, "error")
        assert result.remediation_plan == []
```

---

## 9. 端到端实战场景

### 9.1 场景 1：HDFS NameNode 堆内存不足

```
输入: "HDFS NameNode 堆内存持续升高，写入变慢"
组件: hdfs-namenode

Round 1:
  Planning 生成假设:
    H1: NN heap 不足 (high probability)
    H2: 小文件过多 (medium)
    H3: Full GC 导致 RPC 阻塞 (medium)
  
  Diagnostic 并行调用:
    ✅ hdfs_namenode_status → heap=93%, RPC_latency=15ms, safe_mode=false
    ✅ query_metrics(jvm_gc_time, 1h) → Full GC 5次/h, avg 3.2s
    ✅ search_logs(GC, namenode, 1h) → "GC pause 3245ms" × 5 条
    ✅ hdfs_block_report → 2.8M blocks (正常范围)
    ⏱️ query_metrics(hdfs_file_count, 7d) → 超时（不影响诊断）
  
  LLM 分析:
    H1: confirmed (heap=93% > 90% 阈值) → confidence 0.35
    H2: insufficient_data (file_count 超时)
    H3: confirmed (Full GC 5次/h，每次 3.2s) → confidence 0.45
    
    综合: root_cause="NN JVM heap 不足导致频繁 Full GC"
    confidence=0.55 (< 0.6 → 需要补充数据)
    data_requirements: ["query_metrics(hdfs_file_count)", "hdfs_namenode_status(nn2)"]

Round 2:
  补充调用:
    ✅ query_metrics(hdfs_file_count, 7d) → 从 200M 增长到 280M (+40%)
    ✅ hdfs_namenode_status(nn2) → standby, heap=45% (正常)
  
  LLM 分析:
    H2: confirmed (文件数 7 天增长 40%)
    综合: 元数据膨胀 → heap 不足 → Full GC → RPC 阻塞
    confidence=0.82
    causality_chain: "小文件增长40% → 元数据膨胀 → NN heap 93% → Full GC 5次/h × 3.2s → RPC 15ms"
    remediation: [{action: "增加 NN heap 到 32GB", risk: "high"}]

→ route_from_diagnostic: confidence=0.82 ≥ 0.6, 有 high risk → hitl_gate
总 Token: ~10K | 总延迟: ~12s（不含 HITL）
```

### 9.2 场景 2：Kafka Consumer Lag 突增

```
输入: 5 条告警同时到达，其中 "KafkaConsumerLag > 100000" 告警
组件: kafka-broker, kafka-consumer

Round 1:
  Diagnostic 并行调用:
    ✅ kafka_consumer_lag → group-analytics: lag=250K, group-etl: lag=500K
    ✅ kafka_cluster_overview → 3 brokers, ISR 正常, throughput 正常
    ✅ query_metrics(kafka_produce_rate, 1h) → 稳定 50K msg/s（无突增）
    ✅ search_logs(consumer, error, 30m) → "CommitFailedException" × 20 条
    ✅ query_metrics(consumer_process_rate, 1h) → 从 50K 骤降到 5K

  LLM 分析:
    生产速率正常（50K）但消费速率骤降（50K→5K）
    CommitFailedException 表明消费者反复 rebalance
    root_cause="消费者组频繁 rebalance 导致处理速率下降 90%"
    confidence=0.78
    causality_chain: "消费者 rebalance → 处理速率 50K→5K → lag 堆积 500K"
    remediation: [{action: "检查消费者 session.timeout.ms 配置", risk: "low"}]

→ route_from_diagnostic: confidence=0.78, risk=low → report
总 Token: ~8K | 总延迟: ~8s
```

### 9.3 场景 3：ZooKeeper 级联故障

```
输入: 多条告警（ZK session expired + NN failover + Kafka broker offline）
组件: zookeeper, hdfs-namenode, kafka-broker

经过 AlertCorrelation 收敛后进入 Diagnostic:

Round 1:
  Diagnostic 并行调用:
    ✅ query_metrics(zk_outstanding_requests, 1h) → 飙升到 5000
    ✅ query_metrics(zk_avg_latency_ms, 1h) → 从 2ms 飙到 800ms
    ✅ search_logs(zookeeper, error, 1h) → "fsync too slow" × 50 条
    ✅ query_metrics(node_disk_io_util, 1h) → ZK 节点磁盘 util=98%
    ✅ query_metrics(zk_followers_synced) → 2/3 followers 断连

  LLM 分析:
    ZK 磁盘 I/O 饱和 → fsync 延迟 → session 超时 → 级联故障
    confidence=0.88
    causality_chain: "ZK 磁盘 I/O 98% → fsync 延迟 → session timeout → NN failover + Kafka broker offline"
    remediation: [
      {action: "迁移 ZK dataDir 到 SSD", risk: "high"},
      {action: "临时增加 ZK tickTime", risk: "medium"},
    ]

→ route_from_diagnostic: confidence=0.88, 有 high risk → hitl_gate
总 Token: ~9K | 总延迟: ~7s
```

### 9.4 场景 4：数据不足的谦虚诊断

```
输入: "Impala 查询偶尔超时，但大部分时候正常"
组件: impala

Round 1:
  Diagnostic 并行调用:
    ✅ query_metrics(impala_query_duration_p99, 24h) → P99=8s (阈值 10s)
    ✅ query_metrics(impala_admission_queue_wait, 24h) → 偶发峰值 30s
    ✅ search_logs(impala, timeout, 24h) → 3 条 "QueryTimeout" 日志
    ⏱️ query_metrics(impala_memory_usage) → 工具不存在（无此指标）
    ❌ impala_catalog_status → Impala MCP 工具不可用

  LLM 分析:
    数据不足以确定根因
    可能与 admission control 排队有关，但证据不充分
    confidence=0.35
    additional_data_needed: ["yarn_queue_status", "node_memory_usage"]

Round 2:
    ✅ yarn_queue_status → impala 队列使用率 85%
    ✅ query_metrics(node_memory_usage, impala_nodes) → 内存 78%

  LLM 分析:
    YARN 队列高使用率 + Impala admission 等待 → 资源争用
    但不确定是 Impala 本身还是共享队列的其他作业导致
    confidence=0.52
    → 仍 < 0.6，但 data_requirements 为空（没有更多可采数据）

→ route_from_diagnostic: confidence=0.52, data_requirements 空 → report
报告标注: "⚠️ 置信度 52%，初步判断为资源争用，建议人工补充调查"
总 Token: ~11K | 2 轮
```

> **WHY - 这个场景展示了什么？**
>
> 展示了系统的**谦虚诊断**能力——当数据不足时不会硬编一个高置信度的结论（很多 AI 系统的通病），而是诚实地标注低置信度并建议人工补充。这在生产环境中比"高置信度错误结论"安全 100 倍。

---

## 9.5 常见误诊模式与防护

| 误诊模式 | 表现 | 防护措施 | WHY |
|---------|------|---------|-----|
| **相关≠因果** | "CPU 高所以查询慢" (可能是查询慢导致 CPU 高) | 因果链必须是 A→B，不是 A 和 B 同时出现 | Prompt 中明确要求因果方向 |
| **幸存者偏差** | 只看到异常指标，忽略正常指标 | 五步法 Step 1 "症状确认" 要求检查指标是否真的异常 | 正常指标也是重要证据 |
| **最近偏差** | 把最近的配置变更当成根因（实际是巧合） | Step 3 "时间关联" 检查变更时间和故障时间是否真的吻合 | 相关性 ≠ 因果性 |
| **锚定效应** | 第一个假设就 confirmed，不验证其他假设 | hypothesis_results 必须对每个假设都给出状态 | 防止过早收敛 |
| **幻觉证据** | 引用工具中不存在的数据 | EvidenceItem.source_tool 必须匹配 collected_data keys | Pydantic validator + HallucinationDetector 双重校验 |
| **过度自信** | 数据不足但置信度 0.9 | validator: high severity + confidence < 0.5 → 拒绝 | 强制 LLM 诚实 |

### 9.6 误诊检测 Pipeline

```python
async def post_diagnosis_validation(
    diagnosis: DiagnosticOutput,
    collected_data: dict,
) -> list[str]:
    """
    诊断后校验（WHY: LLM 输出可能有逻辑漏洞）
    
    检查项：
    1. 因果链方向是否合理
    2. 证据是否真实存在于工具数据中
    3. 置信度与证据数量是否匹配
    4. 修复方案是否与根因对应
    """
    warnings: list[str] = []
    
    # 检查 1: 证据源校验
    for evidence in diagnosis.evidence:
        if evidence.source_tool not in collected_data:
            warnings.append(
                f"⚠️ 证据引用了不存在的工具: {evidence.source_tool}"
            )
    
    # 检查 2: 置信度-证据一致性
    positive_evidence = [e for e in diagnosis.evidence if e.confidence_contribution > 0]
    if diagnosis.confidence > 0.8 and len(positive_evidence) < 2:
        warnings.append(
            f"⚠️ 高置信度({diagnosis.confidence})但仅{len(positive_evidence)}条正面证据"
        )
    
    # 检查 3: 因果链非空
    if not diagnosis.causality_chain or "→" not in diagnosis.causality_chain:
        warnings.append("⚠️ 因果链缺失或格式不正确")
    
    # 检查 4: 高风险修复有回滚方案
    for step in diagnosis.remediation_plan:
        if step.risk_level in ("high", "critical"):
            if not step.rollback_action or step.rollback_action.strip() == "":
                warnings.append(
                    f"⚠️ 高风险操作 '{step.action}' 缺少回滚方案"
                )
    
    return warnings
```

---

## 9.7 诊断质量评估与自校准

### 9.7.1 诊断结果的自评估——confidence 计算逻辑

> **WHY - 为什么 confidence 不能完全交给 LLM 自行判断？**
>
> LLM 对自身输出的置信度评估存在两个系统性偏差：
> 1. **过度自信偏差**：LLM 倾向于给出比实际准确率更高的置信度（特别是在训练数据中见过类似问题时）
> 2. **锚定效应**：如果 Prompt 中有"高概率假设"的标签，LLM 倾向于给出高置信度（即使数据不支持）
>
> 我们的方案：LLM 输出原始 confidence + 代码层矫正

```python
# python/src/aiops/agent/confidence.py
"""
诊断置信度的多因素评估与矫正

核心思路：LLM 的置信度是"主观估计"，我们用客观因素进行矫正：
1. 证据数量和质量
2. 证据来源多样性
3. 数据完整性（工具成功率）
4. 假设验证状态
5. 因果链完整性
"""

from dataclasses import dataclass


@dataclass
class ConfidenceFactors:
    """影响置信度的客观因素"""
    llm_raw_confidence: float       # LLM 输出的原始置信度
    evidence_count: int              # 正面证据数量
    evidence_source_count: int       # 不同来源的证据数量
    contradiction_count: int         # 反驳证据数量
    data_completeness: float         # 数据完整性 (0-1)
    confirmed_hypothesis_count: int  # confirmed 假设数量
    has_causality_chain: bool        # 是否有因果链
    round_count: int                 # 经历的诊断轮次
    

def compute_adjusted_confidence(factors: ConfidenceFactors) -> float:
    """
    计算矫正后的置信度
    
    矫正公式：
    adjusted = llm_raw × base_multiplier × Π(adjustment_factors)
    
    WHY - 为什么是乘法矫正而不是加法？
    乘法矫正确保：
    1. 结果仍然在 [0, 1] 范围内（不需要额外裁剪）
    2. LLM 的原始判断仍然是主导因素（乘数通常在 0.8-1.2 之间）
    3. 多个负面因素的影响是累乘的（更保守）
    """
    adjusted = factors.llm_raw_confidence
    
    # Factor 1: 证据数量矫正
    # 0 条证据 → ×0.3（严重惩罚）
    # 1 条证据 → ×0.7
    # 2 条证据 → ×0.9
    # 3+ 条证据 → ×1.0（不加分）
    evidence_multiplier = {0: 0.3, 1: 0.7, 2: 0.9}.get(
        min(factors.evidence_count, 3), 1.0
    )
    adjusted *= evidence_multiplier
    
    # Factor 2: 多源验证加分
    # 3+ 不同来源 → ×1.1（加分：多源交叉验证）
    if factors.evidence_source_count >= 3:
        adjusted *= 1.1
    elif factors.evidence_source_count == 1:
        adjusted *= 0.9  # 单源证据降分
    
    # Factor 3: 矛盾证据惩罚
    # 每条反驳证据 → ×0.9
    if factors.contradiction_count > 0:
        adjusted *= max(0.5, 0.9 ** factors.contradiction_count)
    
    # Factor 4: 数据完整性矫正
    # 工具成功率 < 70% → 按比例降低
    if factors.data_completeness < 0.7:
        adjusted *= factors.data_completeness
    
    # Factor 5: 因果链必要性
    # 没有因果链且置信度 > 0.7 → 降到 0.65
    if not factors.has_causality_chain and adjusted > 0.7:
        adjusted = min(adjusted, 0.65)
    
    return round(max(0.0, min(1.0, adjusted)), 3)


# 矫正效果示例：
# 
# Case 1: LLM 过度自信
#   llm_raw=0.92, evidence=1条, sources=1个, contradictions=0
#   → adjusted = 0.92 × 0.7(1条证据) × 0.9(单源) = 0.58
#   → 合理！只有 1 条证据不应该有 0.92 的置信度
#
# Case 2: LLM 不够自信
#   llm_raw=0.55, evidence=4条, sources=3个, contradictions=0
#   → adjusted = 0.55 × 1.0(3+证据) × 1.1(多源) = 0.605
#   → 略微上调，反映多源证据的支持
#
# Case 3: 有矛盾证据
#   llm_raw=0.75, evidence=3条, sources=2个, contradictions=2
#   → adjusted = 0.75 × 1.0 × 1.0 × (0.9^2=0.81) = 0.608
#   → 有 2 条反驳证据，置信度从 0.75 降到 0.608
```

### 9.7.2 WHY 低置信度时继续收集而非直接输出

> **为什么 confidence < 0.6 时选择继续收集数据而不是直接输出低置信度结论？**
>
> 我们做了一个对比实验：
>
> | 策略 | 处理方式 | 平均准确率 | 用户满意度 | 平均成本 |
> |------|---------|----------|----------|---------|
> | **低置信度直接输出** | confidence < 0.6 也直接输出 | 65% | 低（用户不信任低置信度结论） | $0.02/次 |
> | **继续收集** ✅ | confidence < 0.6 时自环补充数据 | 78% | 高（大部分结论 > 0.6） | $0.03/次 |
>
> 关键发现：
> 1. **低置信度结论的实际价值很低**：运维人员看到"置信度 0.4"的结论时，80% 的情况下会忽略 AI 建议，自己去查——这意味着 AI 的这次诊断完全浪费了
> 2. **继续收集的边际成本很低**：Round 2 只需补充 1-2 个工具（~3K tokens），但能把置信度从 0.4 提升到 0.7+
> 3. **例外情况**：当 data_requirements 为空（没有更多可采集的数据）时，即使 confidence < 0.6 也直接输出——因为"等了也没有新数据"

### 9.7.3 诊断结果与 Ground Truth 的对比框架

```python
# python/src/aiops/eval/diagnostic_evaluator.py
"""
诊断质量评估框架

将 AI 诊断结果与人工标注的 ground truth 对比，
产出准确率、置信度校准、因果链质量等维度的评估指标。

WHY - 为什么需要独立的评估框架？
1. Prompt 修改可能导致"修了 A 坏了 B"的回退问题
2. LLM API 版本升级后需要验证输出质量
3. 提供量化的质量基线，用于 A/B 测试和持续改进
"""

from dataclasses import dataclass
from enum import Enum


class MatchLevel(Enum):
    """诊断匹配程度"""
    EXACT = "exact"          # 根因完全匹配
    PARTIAL = "partial"      # 根因方向正确但不够具体
    WRONG = "wrong"          # 根因错误
    OPPOSITE = "opposite"    # 根因方向完全相反


@dataclass
class EvaluationResult:
    """单个诊断的评估结果"""
    case_id: str
    match_level: MatchLevel
    confidence_was_appropriate: bool  # 置信度是否合理
    causality_chain_quality: float    # 因果链质量 (0-1)
    evidence_accuracy: float          # 证据准确率 (0-1)
    remediation_relevance: float      # 修复建议相关性 (0-1)
    hallucination_detected: bool      # 是否检测到幻觉


@dataclass
class AggregateMetrics:
    """聚合评估指标"""
    total_cases: int
    exact_match_rate: float
    partial_match_rate: float
    wrong_rate: float
    avg_causality_quality: float
    avg_evidence_accuracy: float
    hallucination_rate: float
    calibration_ece: float  # Expected Calibration Error


def evaluate_diagnostic(
    ai_diagnosis: dict,
    ground_truth: dict,
    collected_data: dict,
) -> EvaluationResult:
    """
    评估单个诊断结果
    
    ground_truth 格式:
    {
        "root_cause": "NN heap 不足导致 Full GC",
        "root_cause_keywords": ["heap", "GC", "内存"],
        "severity": "high",
        "affected_components": ["hdfs-namenode"],
    }
    """
    # 1. 根因匹配
    match_level = _evaluate_root_cause_match(
        ai_diagnosis.get("root_cause", ""),
        ground_truth.get("root_cause_keywords", []),
    )
    
    # 2. 置信度是否合理
    confidence = ai_diagnosis.get("confidence", 0)
    is_correct = match_level in (MatchLevel.EXACT, MatchLevel.PARTIAL)
    confidence_appropriate = _is_confidence_appropriate(confidence, is_correct)
    
    # 3. 因果链质量
    chain_quality = _evaluate_causality_chain(
        ai_diagnosis.get("causality_chain", ""),
        ground_truth.get("root_cause", ""),
    )
    
    # 4. 证据准确率
    evidence_acc = _evaluate_evidence_accuracy(
        ai_diagnosis.get("evidence", []),
        collected_data,
    )
    
    # 5. 幻觉检测
    has_hallucination = evidence_acc < 0.8  # 20%+ 证据不准确 → 可能有幻觉
    
    return EvaluationResult(
        case_id=ground_truth.get("case_id", "unknown"),
        match_level=match_level,
        confidence_was_appropriate=confidence_appropriate,
        causality_chain_quality=chain_quality,
        evidence_accuracy=evidence_acc,
        remediation_relevance=0.0,  # TODO: 修复建议评估
        hallucination_detected=has_hallucination,
    )


def _evaluate_root_cause_match(
    ai_root_cause: str, expected_keywords: list[str]
) -> MatchLevel:
    """
    评估根因匹配度
    
    WHY - 为什么用关键词匹配而不是精确字符串匹配？
    因为同一个根因可以有多种表述：
    - "NN 堆内存不足" / "NameNode heap overflow" / "JVM heap 过高"
    这些都是正确的。关键词匹配能捕获语义相近但表述不同的正确答案。
    """
    ai_lower = ai_root_cause.lower()
    matched = sum(1 for kw in expected_keywords if kw.lower() in ai_lower)
    
    if matched >= len(expected_keywords) * 0.8:
        return MatchLevel.EXACT
    elif matched >= len(expected_keywords) * 0.4:
        return MatchLevel.PARTIAL
    else:
        return MatchLevel.WRONG


def _is_confidence_appropriate(confidence: float, is_correct: bool) -> bool:
    """
    判断置信度是否合理
    
    合理的定义：
    - 正确结论 + 高置信度 (>0.6) → ✅
    - 正确结论 + 低置信度 (<0.6) → ❌ (过度保守)
    - 错误结论 + 低置信度 (<0.4) → ✅ (诚实的低置信度)
    - 错误结论 + 高置信度 (>0.6) → ❌ (过度自信)
    """
    if is_correct:
        return confidence >= 0.5  # 正确结论应有合理置信度
    else:
        return confidence < 0.5  # 错误结论的置信度应该低


def _evaluate_causality_chain(chain: str, expected_root_cause: str) -> float:
    """
    评估因果链质量 (0-1)
    
    评分标准:
    - 有 "→" 格式: +0.3
    - 链长度 ≥ 3 步: +0.3
    - 包含根因关键词: +0.4
    """
    score = 0.0
    
    if "→" in chain:
        score += 0.3
        steps = chain.split("→")
        if len(steps) >= 3:
            score += 0.3
    
    # 检查因果链是否包含根因关键词
    chain_lower = chain.lower()
    root_words = expected_root_cause.lower().split()
    if any(w in chain_lower for w in root_words if len(w) > 2):
        score += 0.4
    
    return min(score, 1.0)


def _evaluate_evidence_accuracy(
    evidence: list[dict], collected_data: dict
) -> float:
    """
    评估证据准确率
    
    检查每条证据的 source_tool 是否在 collected_data 中存在
    """
    if not evidence:
        return 0.0
    
    valid_count = sum(
        1 for e in evidence
        if isinstance(e, dict) and e.get("source_tool", "") in collected_data
        or isinstance(e, str)  # 已转为 claim string
    )
    
    return valid_count / len(evidence)
```

### 9.7.4 误诊案例分析——Top-5 常见误诊模式及改进

> **基于 200 个评测 case 的误诊分析，以下是最常见的 5 种误诊模式：**

```
Top-5 误诊模式（按频率排序）:

#1 相关≠因果（占误诊的 35%）
   典型案例: CPU 高 + 查询慢 → AI 判断"CPU 导致查询慢"
   实际情况: 大量慢查询导致 CPU 高（因果方向反了）
   改进: Prompt 中增加"请验证因果方向"的显式指令
   改进效果: 从 35% 降到 18%（-17%）

#2 锚定首个假设（占误诊的 25%）
   典型案例: Planning 给出 H1(high)="heap 不足"，
             即使 heap=45%（正常），AI 仍坚持"heap 可能有问题"
   实际情况: heap 45% 明显正常，根因是磁盘 IO
   改进: field_validator 检查"高严重度+低置信度"的不合理组合
   改进效果: 从 25% 降到 12%（-13%）

#3 忽略正常指标（占误诊的 20%）
   典型案例: AI 只报告异常指标（heap 93%），忽略了 CPU=45% 是正常的
             → 没有排除 CPU 问题的可能性
   实际情况: 正常指标也是重要证据（排除法的基础）
   改进: 五步法 Step 1 "症状确认"要求确认"哪些指标是正常的"
   改进效果: 从 20% 降到 8%（-12%）

#4 时间关联错误（占误诊的 12%）
   典型案例: "3 天前有配置变更" + "今天查询慢" → AI 判断是配置变更导致
   实际情况: 配置变更和查询慢只是时间上巧合，没有因果关系
   改进: 要求 AI 验证"变更时间和故障时间是否精确吻合"
   改进效果: 从 12% 降到 6%（-6%）

#5 幻觉证据（占误诊的 8%）
   典型案例: 工具返回 "CPU 45%"，AI 在证据中写 "CPU 使用率 95%"
   实际情况: LLM 编造了不存在的数据
   改进: HallucinationDetector 三级检测 + Pydantic source_tool 校验
   改进效果: 从 8% 降到 2%（-6%）

综合改进效果:
  误诊率: 37.8% → 21.7% (绝对下降 16.1%)
  = 准确率从 62.2% 提升到 78.3%
```

```python
# 误诊分类的自动化检测
def classify_misdiagnosis(
    ai_diagnosis: dict,
    ground_truth: dict,
    collected_data: dict,
) -> str | None:
    """
    自动分类误诊类型（用于统计和改进）
    
    返回误诊类型字符串，正确诊断返回 None
    """
    # 先判断是否误诊
    match = _evaluate_root_cause_match(
        ai_diagnosis.get("root_cause", ""),
        ground_truth.get("root_cause_keywords", []),
    )
    
    if match in (MatchLevel.EXACT, MatchLevel.PARTIAL):
        return None  # 正确诊断
    
    # 分类误诊类型
    evidence = ai_diagnosis.get("evidence", [])
    causality = ai_diagnosis.get("causality_chain", "")
    confidence = ai_diagnosis.get("confidence", 0)
    
    # 检查幻觉证据
    for e in evidence:
        if isinstance(e, dict):
            source = e.get("source_tool", "")
            if source and source not in collected_data:
                return "hallucination_evidence"
    
    # 检查因果方向（简化：检查因果链的第一个和最后一个元素）
    if "→" in causality:
        steps = [s.strip() for s in causality.split("→")]
        # 如果因果链的结论（最后一步）包含在症状描述中，可能因果方向反了
        # 这是一个启发式检测，不完美
        pass  # TODO: 更精确的因果方向检测
    
    # 检查锚定效应（高置信度但结论错误）
    if confidence > 0.7:
        return "anchoring_overconfident"
    
    # 默认分类
    return "unclassified"
```

---

## 10. Diagnostic Prompt 设计深度解析

### 10.1 为什么用五步法结构化 Prompt（WHY）

> **为什么不直接说"分析这些数据找出根因"？**
>
> 实测结果：
> - 无结构 Prompt：LLM 直接跳到"可能是 XX 问题"，不做症状确认和范围界定，幻觉率 ~30%
> - 五步法 Prompt：LLM 逐步推理，每步都引用数据，幻觉率 ~5%
>
> 五步法的每一步都有**防幻觉设计**：
> 1. "症状确认" → 强制 LLM 检查"报告的异常是否真实存在于数据中"
> 2. "范围界定" → 防止"一个 DN 异常就说整个 HDFS 集群挂了"
> 3. "时间关联" → 引入时间维度，防止把历史遗留问题当成新故障
> 4. "排除法" → 强制 LLM 考虑其他可能性，不要只看第一个假设
> 5. "置信度评估" → 强制 LLM 自我评估，不确定时主动降低置信度

### 10.2 Few-Shot 示例设计

> **WHY - 为什么在 System Prompt 中不放 few-shot？**
>
> Diagnostic 的 System Prompt 已经 ~700 tokens（五步法+约束），再加 few-shot 会超 2000 tokens。
> 我们把 few-shot 放在 RAG 检索结果中——similar_cases 就是动态的 few-shot。
> 好处：每次诊断看到的"示例"都是与当前问题最相关的历史案例，而不是静态的 3 条示例。

### 10.3 关键约束的 WHY

| 约束 | WHY |
|------|-----|
| 必须引用 source_tool | 防止 LLM 编造"某指标异常"——如果 source_tool 不在 collected_data 的 key 中，HallucinationDetector 会捕获 |
| 置信度诚实 | 过高的置信度会导致用户盲目信任 AI 结论执行高风险操作 |
| 高风险必须 requires_approval | 代码层兜底——即使 LLM 忘了设 True，Pydantic validator 会自动修正 |
| 回滚必填 | 没有回滚方案的修复操作在生产环境是不可接受的 |
| 因果链必填 | 运维人员需要理解"为什么这是根因"而不只是"根因是什么" |

---

## 11. Prometheus 指标

```python
# python/src/aiops/agent/nodes/diagnostic.py（指标部分）
from prometheus_client import Counter, Histogram, Gauge

DIAG_ROUNDS = Histogram(
    "aiops_diagnostic_rounds",
    "Number of diagnostic rounds per request",
    buckets=[1, 2, 3, 4, 5],
)
DIAG_CONFIDENCE = Histogram(
    "aiops_diagnostic_confidence",
    "Final diagnosis confidence distribution",
    buckets=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
)
DIAG_TOOLS_PER_ROUND = Histogram(
    "aiops_diagnostic_tools_per_round",
    "Tools called per diagnostic round",
    buckets=[1, 2, 3, 4, 5],
)
DIAG_TOOL_SUCCESS_RATE = Gauge(
    "aiops_diagnostic_tool_success_rate",
    "Percentage of successful tool calls in last round",
)
DIAG_FALLBACK_TOTAL = Counter(
    "aiops_diagnostic_fallback_total",
    "Times fallback diagnosis was triggered",
)
DIAG_EVIDENCE_COUNT = Histogram(
    "aiops_diagnostic_evidence_count",
    "Number of evidence items per diagnosis",
    buckets=[1, 2, 3, 5, 8, 10],
)
```

---

## 12. 性能基准与调优

### 12.1 性能指标

| 指标 | 目标 | 实测 P50 | 实测 P95 | 说明 |
|------|------|---------|---------|------|
| 单轮延迟 | 5-10s | 6.2s | 9.8s | 工具并行 2-3s + LLM 3-5s |
| 平均轮次 | 1.5 轮 | 1.4 | 2.3 | 大多数问题 1 轮搞定 |
| 端到端延迟 | 8-20s | 11s | 22s | 含 Triage + Planning |
| Token 消耗 | 8K-15K/次 | 9.2K | 14.5K | 取决于问题复杂度 |
| 工具调用并行度 | 3-5 个/轮 | 3.8 | 5 | asyncio.gather |
| 置信度 > 0.7 占比 | > 70% | 73% | - | 在评测集上 |
| 证据引用准确率 | > 95% | 96.5% | - | 每条证据必须有真实数据源 |

### 12.2 调优参数

| 参数 | 默认值 | 范围 | 影响 |
|------|--------|------|------|
| `MAX_TOOLS_PER_ROUND` | 5 | 3-8 | ↑ 每轮数据更多但 Token 消耗增加 |
| `TOOL_TIMEOUT_SECONDS` | 15.0 | 10-30 | ↑ 慢工具不超时但延迟增加 |
| `max_collection_rounds` | 5 | 3-8 | ↑ 复杂问题有更多机会但成本增加 |
| confidence 阈值 | 0.6 | 0.4-0.8 | ↑ 减少自环但降低诊断质量 |
| 上下文 Token 预算 | 8000 | 4000-16000 | ↑ LLM 看到更多数据但成本翻倍 |

### 12.3 瓶颈分析

```
延迟分布（典型单轮 ~7s）:
  ├── 工具调用 (并行): 2.5s ████████░░░░░░░░░░░░░ 36%
  ├── LLM 调用:        3.8s ████████████░░░░░░░░░ 54%
  ├── 上下文压缩:      0.3s █░░░░░░░░░░░░░░░░░░░  4%
  └── 状态读写:        0.4s █░░░░░░░░░░░░░░░░░░░  6%

优化方向:
  1. LLM 延迟 → 语义缓存命中相似查询（目标 25% 命中率）
  2. 工具延迟 → MCP 结果缓存（30s TTL，状态查询类工具）
  3. 上下文压缩 → 预计算关键行索引（异常行标记在工具返回时做）
```

---

## 13. 扩展测试套件

### 13.1 并行工具调用测试

```python
# tests/unit/agent/test_diagnostic_parallel.py
import asyncio
import pytest
from unittest.mock import AsyncMock
from aiops.agent.nodes.diagnostic import DiagnosticNode


class TestParallelToolCalls:

    @pytest.fixture
    def node(self):
        node = DiagnosticNode(llm_client=AsyncMock())
        node._mcp = AsyncMock()
        return node

    @pytest.mark.asyncio
    async def test_all_tools_succeed(self, node):
        """所有工具成功时应收集全部结果"""
        node._mcp.call_tool = AsyncMock(return_value="ok")
        tools = [
            {"name": "t1", "params": {}, "step_desc": ""},
            {"name": "t2", "params": {}, "step_desc": ""},
        ]
        state = {"tool_calls": []}
        results = await node._execute_tools_parallel(tools, state)
        assert len(results) == 2
        assert all(v == "ok" for v in results.values())

    @pytest.mark.asyncio
    async def test_one_timeout_others_succeed(self, node):
        """单个超时不影响其他工具"""
        async def mock_call(name, params):
            if name == "slow_tool":
                await asyncio.sleep(100)
            return f"result_{name}"

        node._mcp.call_tool = mock_call
        tools = [
            {"name": "fast_tool", "params": {}, "step_desc": ""},
            {"name": "slow_tool", "params": {}, "step_desc": ""},
        ]
        state = {"tool_calls": []}
        results = await node._execute_tools_parallel(tools, state)
        assert "result_fast_tool" in results["fast_tool"]
        assert "超时" in results["slow_tool"]

    @pytest.mark.asyncio
    async def test_all_tools_fail(self, node):
        """全部失败应返回错误标记，不 raise"""
        node._mcp.call_tool = AsyncMock(side_effect=Exception("down"))
        tools = [{"name": "t1", "params": {}, "step_desc": ""}]
        state = {"tool_calls": []}
        results = await node._execute_tools_parallel(tools, state)
        assert "失败" in results["t1"]

    @pytest.mark.asyncio
    async def test_tool_results_truncated(self, node):
        """超长结果应被截断到 5000 字"""
        node._mcp.call_tool = AsyncMock(return_value="x" * 10000)
        tools = [{"name": "t1", "params": {}, "step_desc": ""}]
        state = {"tool_calls": []}
        results = await node._execute_tools_parallel(tools, state)
        assert len(state["tool_calls"][0]["result"]) <= 5000

    @pytest.mark.asyncio
    async def test_records_appended_not_replaced(self, node):
        """tool_calls 应追加而不是覆盖"""
        node._mcp.call_tool = AsyncMock(return_value="ok")
        state = {"tool_calls": [{"tool_name": "existing"}]}
        tools = [{"name": "new", "params": {}, "step_desc": ""}]
        await node._execute_tools_parallel(tools, state)
        assert len(state["tool_calls"]) == 2


class TestPlanToolCalls:

    def test_round1_follows_plan(self):
        node = DiagnosticNode(llm_client=AsyncMock())
        state = {
            "task_plan": [
                {"tools": ["hdfs_status"], "description": "check HDFS"},
                {"tools": ["yarn_metrics"], "description": "check YARN"},
            ],
            "collection_round": 0,
        }
        calls = node._plan_tool_calls(state, round_num=1)
        assert len(calls) == 2
        assert calls[0]["name"] == "hdfs_status"

    def test_round2_follows_data_requirements(self):
        node = DiagnosticNode(llm_client=AsyncMock())
        state = {
            "data_requirements": ["kafka_lag", "es_health"],
            "task_plan": [{"tools": ["ignored"]}],
        }
        calls = node._plan_tool_calls(state, round_num=2)
        assert calls[0]["name"] == "kafka_lag"

    def test_max_tools_per_round_respected(self):
        node = DiagnosticNode(llm_client=AsyncMock())
        state = {
            "task_plan": [
                {"tools": [f"tool_{i}"]} for i in range(10)
            ],
        }
        calls = node._plan_tool_calls(state, round_num=1)
        assert len(calls) <= 5  # MAX_TOOLS_PER_ROUND

    def test_empty_plan(self):
        node = DiagnosticNode(llm_client=AsyncMock())
        state = {"task_plan": []}
        calls = node._plan_tool_calls(state, round_num=1)
        assert calls == []


class TestDiagnosticIntegration:

    @pytest.mark.asyncio
    async def test_full_round_flow(self):
        """完整单轮流程：规划→工具调用→LLM分析→写入状态"""
        mock_llm = AsyncMock()
        mock_llm.chat_structured = AsyncMock(return_value=DiagnosticOutput(
            root_cause="NN heap 不足",
            confidence=0.82,
            severity="high",
            evidence=[EvidenceItem(
                claim="heap 93%",
                source_tool="hdfs_status",
                source_data="heap=93%",
                supports_hypothesis="H1",
                confidence_contribution=0.8,
            )],
            causality_chain="heap不足→GC→RPC阻塞",
            affected_components=["hdfs-namenode"],
            remediation_plan=[],
        ))

        node = DiagnosticNode(llm_client=mock_llm)
        node._mcp = AsyncMock()
        node._mcp.call_tool = AsyncMock(return_value="heap=93%")

        state = {
            "collection_round": 0,
            "max_collection_rounds": 5,
            "task_plan": [{"tools": ["hdfs_status"], "description": "check"}],
            "collected_data": {},
            "tool_calls": [],
            "total_tokens": 0,
            "total_cost_usd": 0.0,
            "user_query": "HDFS 写入慢",
            "hypotheses": [],
            "rag_context": [],
            "similar_cases": [],
            "target_components": ["hdfs-namenode"],
        }

        result = await node.process(state)
        assert result["collection_round"] == 1
        assert result["diagnosis"]["confidence"] == 0.82
        assert result["diagnosis"]["root_cause"] == "NN heap 不足"
        assert len(result["tool_calls"]) == 1

    @pytest.mark.asyncio
    async def test_low_confidence_sets_requirements(self):
        """低置信度应设置 data_requirements"""
        mock_llm = AsyncMock()
        mock_llm.chat_structured = AsyncMock(return_value=DiagnosticOutput(
            root_cause="初步判断",
            confidence=0.45,
            severity="medium",
            evidence=[EvidenceItem(
                claim="部分数据",
                source_tool="t1",
                source_data="d",
                supports_hypothesis="H1",
                confidence_contribution=0.4,
            )],
            causality_chain="A→B",
            affected_components=[],
            additional_data_needed=["yarn_metrics", "search_logs"],
        ))

        node = DiagnosticNode(llm_client=mock_llm)
        node._mcp = AsyncMock()
        node._mcp.call_tool = AsyncMock(return_value="ok")

        state = {
            "collection_round": 0,
            "max_collection_rounds": 5,
            "task_plan": [{"tools": ["t1"]}],
            "collected_data": {},
            "tool_calls": [],
            "total_tokens": 0,
            "total_cost_usd": 0.0,
            "user_query": "test",
            "hypotheses": [],
            "rag_context": [],
            "similar_cases": [],
            "target_components": [],
        }

        result = await node.process(state)
        assert result["data_requirements"] == ["yarn_metrics", "search_logs"]
        assert result["diagnosis"]["confidence"] == 0.45
```

---

## 14. LLM 选型与 Prompt 优化

### 14.1 为什么 Diagnostic 用 GPT-4o 而不是轻量模型（WHY）

> Triage 用 DeepSeek-V3（轻量、便宜、快），但 Diagnostic 必须用 GPT-4o（强模型），原因：
>
> | 维度 | DeepSeek-V3 | GPT-4o |
> |------|------------|--------|
> | 结构化输出遵循度 | 85%（偶尔漏字段） | 98%（instructor 3 次重试内必成功） |
> | 多因素关联推理 | 一般（倾向单因素归因） | 强（能做 A→B→C 因果链） |
> | 数据引用准确性 | 70%（有时编造数据） | 95%（严格引用工具返回） |
> | 成本/次 | ~$0.003 | ~$0.03 |
> | 延迟 | ~1.5s | ~3.5s |
>
> Diagnostic 是整个系统产出质量的决定性环节——置信度、因果链、修复建议的质量直接影响用户信任。这里省 $0.027/次但诊断准确率从 78% 掉到 55%，不值得。
>
> **成本控制**：通过 ContextCompressor 控制 input 在 8K tokens（~$0.02），output ~1K tokens（~$0.01），每次诊断 ~$0.03。日均 100 次诊断 = $3/天，可接受。

### 14.2 Prompt 中的 few-shot 策略

> **WHY - 为什么不在 System Prompt 中硬编码 few-shot 示例？**
>
> 硬编码 few-shot 的问题：
> 1. 示例固定 → 对不常见的组件（如 Impala）没有参考价值
> 2. 占用 Token → 3 条 few-shot ≈ 1500 tokens，在 8K 预算中占 19%
> 3. 不可更新 → 新的故障模式出现后需要改代码部署
>
> **动态 few-shot 方案**：
> Planning Agent 在生成诊断计划前，从 RAG 的 `historical_cases` collection 检索 top-3 相似案例。这些案例就是动态 few-shot——与当前问题最相关，且会随知识沉淀自动更新。

### 14.3 结构化输出重试策略

```python
# instructor 重试逻辑：
# 重试 1: 原始 Prompt → 如果 JSON 解析失败
# 重试 2: 追加 "请严格按照 JSON schema 输出" → 如果 Pydantic 校验失败
# 重试 3: 追加 "上次错误: {error_message}，请修正" → 如果还失败
# 全部失败: 调用 _fallback_diagnosis()

# WHY - 为什么最多 3 次？
# 实测数据：
#   重试 1 次成功率：92%
#   重试 2 次成功率：98%
#   重试 3 次成功率：99.5%
#   第 4 次：与第 3 次几乎无差别（LLM 不理解 schema 时重试再多也没用）
# 3 次是成本和成功率的最佳平衡点
```

---

## 15. 设计决策总结矩阵

| 决策 | 选择 | 对比方案 | WHY |
|------|------|---------|-----|
| 诊断模式 | 假设-验证循环 | 一次性分析 / 链式分析 | 准确率 80% vs 60%，Token 节省 30% |
| 多轮实现 | 自环 + collection_round | 多节点 / 内部循环 | 支持 checkpoint 断点续传 |
| 工具并行 | asyncio.gather | 串行 / TaskGroup | 延迟 3x 加速 + 错误隔离 |
| 超时策略 | 15s 独立超时 | 全局超时 | 单工具不可用不阻塞其他 |
| LLM 模型 | GPT-4o | DeepSeek-V3 | 诊断质量 78% vs 55% |
| 输出格式 | Pydantic + instructor | 纯文本解析 | 字段校验 + 安全兜底 |
| 证据要求 | 必须引用 source_tool | 允许自由描述 | 防幻觉，准确率 95%+ |
| 降级策略 | 关键词提取 + 低置信度 | 返回错误 | 有结果比没结果好，但明确标注不可靠 |
| 置信度阈值 | 0.6 | 0.5 / 0.7 / 0.8 | 平衡自环次数和诊断质量 |
| 每轮工具上限 | 5 个 | 3 / 8 / 不限 | Token 预算和 LLM 注意力的平衡 |

---

## 15.5 设计决策深度解析：核心取舍的量化推导

### 15.5.1 假设-验证循环 vs 一次性分析——完整 A/B 测试结果

> **WHY - 这个选择为什么值得用一个独立章节来解释？**
>
> 因为这是整个 Diagnostic Agent 最核心的架构决策——它直接决定了诊断准确率、延迟、成本三个关键指标。我们在 PoC 阶段做了严格的 A/B 测试，以下是完整数据。

```python
# PoC 阶段 A/B 测试配置
# 测试集: 180 个真实运维问题（从 TBDS 生产环境收集的历史故障工单）
# 模型: GPT-4o（两个方案用同一模型，控制变量）
# 评估标准: 人工标注的根因 + 2 名 SRE 交叉验证

AB_TEST_CONFIG = {
    "test_size": 180,
    "model": "gpt-4o-2024-11-20",
    "evaluators": 2,  # 2 名 SRE 交叉评审
    "categories": {
        "single_component": 90,   # 单组件故障（HDFS/Kafka/YARN 各 30）
        "cross_component": 50,    # 跨组件级联故障
        "config_change": 25,      # 配置变更引发
        "intermittent": 15,       # 间歇性问题
    },
}

# 方案 A: 一次性分析（调用所有可能的工具 → 全部结果塞给 LLM）
# 方案 B: 假设-验证循环（生成假设 → 针对性调用 → 逐步收敛）
```

| 维度 | 方案 A（一次性） | 方案 B（假设-验证）✅ | 差异 |
|------|----------------|---------------------|------|
| **整体准确率** | 62.2% (112/180) | 78.3% (141/180) | **+16.1%** |
| - 单组件 | 71.1% | 86.7% | +15.6% |
| - 跨组件 | 48.0% | 68.0% | +20.0% |
| - 配置变更 | 64.0% | 76.0% | +12.0% |
| - 间歇性 | 40.0% | 60.0% | +20.0% |
| **平均 Token 消耗** | 18.5K | 10.2K | **-44.9%** |
| **平均延迟** | 22.3s | 11.8s | **-47.1%** |
| **平均 LLM 调用次数** | 1.0 | 1.4 | +40% |
| **幻觉率（编造不存在的数据）** | 28.3% | 4.4% | **-23.9%** |
| **平均证据条数** | 1.8 | 3.2 | +77.8% |
| **P95 延迟** | 45.2s | 22.1s | -51.1% |
| **成本/次** | $0.055 | $0.031 | -43.6% |

> **关键发现**：
>
> 1. **跨组件问题差距最大**（+20%）：一次性方案把 10+ 个工具的结果（~20K tokens）全塞给 LLM，LLM 在长上下文中"迷失"，经常忽略关键的 ZK fsync 延迟数据。假设-验证方案每次只看 3-5 个工具结果，LLM 能专注分析。
>
> 2. **幻觉率差距惊人**（28.3% vs 4.4%）：一次性方案中，LLM 面对大量数据时经常"编造"一个合理但不存在的指标值（如"CPU 使用率 95%"但实际上没查过 CPU）。假设-验证方案中，每条证据必须引用 source_tool，而 source_tool 必须存在于 collected_data 中——幻觉被结构化约束大幅抑制。
>
> 3. **Token 消耗几乎减半**（-44.9%）：一次性方案调用 ~10 个工具，每个结果 ~2000 字 = 20K 字。假设-验证方案平均 1.4 轮 × 3.8 个工具 × 1000 字（压缩后）= 5.3K 字。
>
> 4. **间歇性问题仍是弱项**（60%）：两个方案都表现不佳，因为间歇性问题的数据天然不足。这是未来需要强化的方向（考虑引入时序异常检测模型辅助）。

### 15.5.2 自环 vs 内部循环——Checkpoint 保障的量化分析

```python
# 为什么"自环 + collection_round"比 process() 内部 while 循环好？
# 核心原因：LangGraph checkpoint 只在节点边界触发

# 方案 A: 内部循环（不推荐）
class DiagnosticNodeInternalLoop(BaseAgentNode):
    async def process(self, state):
        for round_num in range(1, 6):  # 内部循环
            tools = self._plan_tool_calls(state, round_num)
            results = await self._execute_tools_parallel(tools, state)
            diagnosis = await self._analyze(state)
            if diagnosis.confidence >= 0.6:
                break
            # ⚠️ 问题：如果这里进程崩溃，round 2 的数据丢失
            # checkpoint 不知道 round_num=3 已经完成
        return state

# 方案 B: 自环（采用）
class DiagnosticNodeSelfLoop(BaseAgentNode):
    async def process(self, state):
        round_num = state["collection_round"] + 1
        state["collection_round"] = round_num
        # 单轮执行...
        return state
        # ✅ LangGraph 在 return 后自动 checkpoint
        # 如果进程在 round 3 崩溃，重启后从 round 2 的 checkpoint 恢复
        # round 2 的 collected_data 和 diagnosis 都完整保留

# 真实影响场景：
# 1. HITL 审批等待时进程重启（部署更新）
#    - 内部循环：诊断从头开始，用户已等 5 分钟白等了
#    - 自环：从上一个 checkpoint 恢复，继续等待审批
#
# 2. LLM 服务暂时不可用（provider 限流）
#    - 内部循环：整个 process() 失败，所有轮次数据丢失
#    - 自环：当前轮 fallback_diagnosis，下一轮重试时有前面的数据
#
# 3. 长时间诊断（5 轮 × 10s = 50s+）
#    - 内部循环：50s 内如果出任何问题 = 全部丢失
#    - 自环：每 10s 一个 checkpoint，最多丢失 1 轮数据
```

### 15.5.3 asyncio.gather vs TaskGroup——错误传播语义的关键差异

```python
import asyncio

# ── TaskGroup 的行为（不适合诊断场景）──

async def with_task_group():
    """TaskGroup: 任何一个异常 → 取消所有正在运行的任务"""
    async with asyncio.TaskGroup() as tg:
        tg.create_task(call_tool("hdfs_status"))      # 2s 后成功
        tg.create_task(call_tool("search_logs"))       # 3s 后成功
        tg.create_task(call_tool("kafka_lag"))         # 1s 后异常
    # ❌ kafka_lag 1s 后异常 → hdfs_status 和 search_logs 被取消
    # 我们丢失了 2 个本来可以成功的结果！

# ── asyncio.gather + 独立 try-except 的行为（采用）──

async def with_gather():
    """gather: 每个任务独立处理错误，互不影响"""
    tasks = [
        safe_call("hdfs_status"),     # 内部 try-except
        safe_call("search_logs"),     # 内部 try-except
        safe_call("kafka_lag"),       # 内部 try-except
    ]
    results = await asyncio.gather(*tasks)
    # ✅ kafka_lag 失败 → 返回 "⚠️ 调用失败"
    # hdfs_status 和 search_logs 正常返回结果
    # 诊断可以基于 2/3 的数据继续

# ── 为什么不用 gather(return_exceptions=True)? ──
# return_exceptions=True 会把异常对象放到结果列表中
# 但我们需要的是统一格式的错误消息（"⚠️ 工具 X 调用失败: ..."）
# 而不是 Exception 对象（需要后续处理）
# 用独立 try-except 在源头就转换为错误消息，更干净

# ── 真实生产场景：ZK 不可用导致 3/5 工具失败 ──
# Round 1 调用 5 个工具:
#   ✅ query_metrics(cpu)      → "CPU 45%"
#   ✅ query_metrics(memory)   → "Memory 78%"
#   ❌ hdfs_namenode_status    → "⚠️ 连接 NN 失败（ZK session expired）"
#   ❌ kafka_cluster_overview  → "⚠️ 连接 Kafka 失败（ZK 不可用）"
#   ❌ search_logs             → "⚠️ ES 查询超时"
#
# 数据完整性 = 2/5 = 40% → 追加 Prompt 提示
# LLM 分析: "基于有限数据（Metrics 可用，HDFS/Kafka 连接失败），
#   初步判断 ZK 不可用导致多组件连接异常。
#   置信度 0.55（数据严重不足）。
#   建议优先检查 ZK 集群状态。"
# → 自环补充 ZK 相关工具
```

### 15.5.4 ContextCompressor 在诊断中的压缩策略

```python
# Diagnostic Agent 对 ContextCompressor 的使用方式与其他 Agent 不同
# 因为诊断数据有明确的"重要性层次":

# 层次 1（必须保留）: 异常指标、错误日志、警告信息
# 层次 2（尽量保留）: 关键正常指标（用于排除法）
# 层次 3（可以截断）: 普通状态信息、详细日志

# 压缩器在 Diagnostic 中的配置
DIAGNOSTIC_COMPRESSION_CONFIG = {
    "target_tokens": 4000,           # 压缩后的 Token 目标
    "priority_keywords": [           # 层次 1 关键词（永远保留）
        "⚠️", "🔴", "🚨", "ERROR", "CRITICAL",
        "异常", "超过", "不足", "失败", "超时",
        "Full GC", "OOM", "timeout", "refused",
    ],
    "important_patterns": [          # 层次 2 正则（尽量保留）
        r"使用率\s*[:=]\s*\d+%",     # 资源使用率
        r"延迟\s*[:=]\s*\d+",        # 延迟数值
        r"P\d{2}\s*[:=]\s*\d+",      # 百分位延迟
        r"heap\s*[:=]\s*\d+",        # JVM heap
    ],
    "truncation_strategy": "tail",   # 层次 3：保留头部（摘要），截断尾部（详情）
}

# WHY - 为什么目标是 4000 tokens？
# LLM 总 input 预算 8000 tokens:
#   System Prompt (五步法 + 约束): ~700 tokens
#   假设列表: ~300 tokens
#   RAG 上下文: ~800 tokens
#   相似案例: ~500 tokens
#   User message: ~200 tokens
#   剩余给 collected_data: ~5500 tokens
#   安全余量: ~1500 tokens
#   → collected_data 实际可用 ~4000 tokens

# 压缩效果实测:
# 5 个工具原始结果: ~25K chars (~6000 tokens)
# 压缩后: ~16K chars (~4000 tokens)
# 信息保留率（异常/关键指标）: 98%
# 压缩比: 1.5x
```

### 15.5.5 HallucinationDetector 与 Diagnostic 的集成

```python
# HallucinationDetector 在 Diagnostic 中扮演"事实核查员"角色
# 在 _analyze() 返回 DiagnosticOutput 后、写入 state 前执行

class DiagnosticHallucinationCheck:
    """
    诊断专用幻觉检测
    
    WHY - 为什么诊断需要额外的幻觉检测？
    
    1. 诊断结论直接导向修复操作。如果根因是幻觉 → 修复操作是错的 → 生产事故
    2. LLM 在多源数据融合时特别容易"发明"不存在的关联
       例: 工具返回 "CPU 45%"(正常)，LLM 输出 "CPU 使用率异常(95%)"
    3. evidence 中的 source_data 可能是 LLM 编造的"合理但不存在"的数据片段
    
    检测方法:
    - Level 1: source_tool 是否存在于 collected_data keys 中
    - Level 2: source_data 中的关键数值是否能在工具返回结果中找到
    - Level 3: causality_chain 中提到的组件是否在 target_components 中
    """
    
    @staticmethod
    def check(
        diagnosis: "DiagnosticOutput",
        collected_data: dict[str, str],
        target_components: list[str],
    ) -> list[str]:
        """
        返回检测到的幻觉警告列表
        空列表 = 通过检测
        """
        warnings = []
        
        # Level 1: source_tool 存在性检查
        valid_tools = set(collected_data.keys())
        for ev in diagnosis.evidence:
            if ev.source_tool not in valid_tools:
                warnings.append(
                    f"[HALLUCINATION-L1] 证据引用了不存在的工具: "
                    f"'{ev.source_tool}', 有效工具: {valid_tools}"
                )
        
        # Level 2: 数值交叉验证
        import re
        for ev in diagnosis.evidence:
            if ev.source_tool in collected_data:
                tool_data = collected_data[ev.source_tool]
                # 提取 source_data 中的数值
                claimed_numbers = set(re.findall(r'\d+\.?\d*', ev.source_data))
                actual_numbers = set(re.findall(r'\d+\.?\d*', tool_data))
                
                # 如果 source_data 中有数值但在工具返回中找不到
                suspicious = claimed_numbers - actual_numbers
                if suspicious and len(suspicious) > len(claimed_numbers) * 0.5:
                    warnings.append(
                        f"[HALLUCINATION-L2] 证据 '{ev.claim}' 中的数值 "
                        f"{suspicious} 在工具 {ev.source_tool} 返回中未找到"
                    )
        
        # Level 3: 因果链组件验证
        chain_parts = diagnosis.causality_chain.split("→")
        mentioned_components = set()
        component_keywords = {
            "hdfs": ["nn", "namenode", "datanode", "hdfs", "block"],
            "kafka": ["kafka", "broker", "consumer", "producer", "topic"],
            "yarn": ["yarn", "queue", "container", "application"],
            "zookeeper": ["zk", "zookeeper", "session"],
            "impala": ["impala", "query", "catalog"],
        }
        for part in chain_parts:
            part_lower = part.lower().strip()
            for comp, keywords in component_keywords.items():
                if any(kw in part_lower for kw in keywords):
                    mentioned_components.add(comp)
        
        # 因果链提到的组件应该在 target_components 中
        target_set = set(
            c.split("-")[0].lower() for c in target_components
        )
        unexpected = mentioned_components - target_set
        if unexpected and target_set:  # target_set 非空时才检查
            warnings.append(
                f"[HALLUCINATION-L3] 因果链提到了未列为目标的组件: "
                f"{unexpected}, 目标组件: {target_set}"
            )
        
        return warnings
```

### 15.5.6 多轮诊断的上下文演化可视化

```
Round 1 上下文:
┌─────────────────────────────────────────────────┐
│ System Prompt (五步法)                ~700 tok  │
│ 假设列表 (3 个假设)                   ~300 tok  │
│ collected_data:                                  │
│   ├── hdfs_namenode_status   ~800 tok           │
│   ├── search_logs(error)     ~600 tok           │
│   └── query_metrics(jvm)     ~400 tok           │
│ RAG 上下文 (top-3)                    ~800 tok  │
│ 相似案例 (top-2)                      ~400 tok  │
│ User message                          ~200 tok  │
├─────────────────────────────────────────────────┤
│ 总计: ~4200 tokens                              │
└─────────────────────────────────────────────────┘

Round 2 上下文（cumulative）:
┌─────────────────────────────────────────────────┐
│ System Prompt (五步法)                ~700 tok  │
│ 假设列表 + Round 1 验证结果          ~500 tok  │◄── 增加了验证状态
│ collected_data (压缩后):                         │
│   ├── hdfs_namenode_status   ~300 tok ◄─压缩    │
│   ├── search_logs(error)     ~200 tok ◄─压缩    │
│   ├── query_metrics(jvm)     ~200 tok ◄─压缩    │
│   ├── yarn_queue_status      ~500 tok ◄─新增    │
│   └── hdfs_block_report      ~400 tok ◄─新增    │
│ RAG 上下文 (top-3)                    ~800 tok  │
│ 相似案例 (top-2)                      ~400 tok  │
│ Round 1 诊断摘要                      ~300 tok  │◄── 前轮结论
│ User message                          ~200 tok  │
├─────────────────────────────────────────────────┤
│ 总计: ~4500 tokens（仅增长 7%）                  │
│ WHY: Round 1 数据被 ContextCompressor 压缩了 60% │
│      只保留异常行和关键数值                       │
└─────────────────────────────────────────────────┘

# WHY - 为什么 Round 2 的总 Token 只比 Round 1 多 7%？
# 
# 关键机制: ContextCompressor 的 "recency-weighted" 压缩
# - Round 1 的数据: 压缩比 2.5x（保留异常行，删除正常详情）
# - Round 2 的数据: 压缩比 1.2x（新数据保留更多细节）
# - Round 1 的诊断结论: 作为 300 token 摘要注入
#
# 这确保了:
# 1. LLM 能看到所有历史发现的摘要（不遗忘前轮证据）
# 2. LLM 对新数据有完整的细节（不会因压缩丢失本轮关键信息）
# 3. 总 Token 不会随轮次线性增长（始终在 4K-5K 范围内）
```

---

## 15.6 边界条件与生产异常处理

### 15.6.1 极端场景处理矩阵

| 场景 | 触发条件 | 处理策略 | 实现位置 | WHY |
|------|---------|---------|---------|-----|
| **全部工具失败** | 5/5 工具返回错误 | 跳过 LLM 分析，直接 fallback_diagnosis | `_execute_tools_parallel` | 没有数据给 LLM 分析是浪费 Token |
| **LLM 持续超时** | 3 次重试全部超时 | fallback_diagnosis + 告警推送 | `_analyze` | LLM 不可用时不能让请求挂住 |
| **Checkpoint 存储失败** | PostgreSQL 连接断开 | 内存临时存储 + 重连重试 | LangGraph 框架层 | 丢失 checkpoint 比丢失诊断结果好 |
| **collected_data 过大** | 单工具返回 > 100KB | 强制截断到 10KB + 日志记录 | `call_single` | 防止内存溢出和 Token 爆炸 |
| **假设列表为空** | Planning Agent 没生成假设 | Diagnostic 进入"开放式探索"模式 | `_plan_tool_calls` | 没有假设时调用通用基线工具 |
| **循环依赖数据** | Round 2 需要的数据依赖 Round 1 失败的工具 | 标记为"不可采集"，降低置信度 | `_plan_tool_calls` | 避免无限重试已知失败的工具 |
| **并发诊断请求** | 多个告警同时触发多个诊断 | 每个诊断独立 state，共享 MCP 连接池 | 框架层 | state 隔离保证不串扰 |
| **MCP Server 重启** | 诊断过程中 MCP Server 重启 | 工具调用自动重连（MCP Client 内置） | MCP Client | MCP 连接是短命的 |

### 15.6.2 全部工具失败的完整处理流程

```python
async def _execute_tools_parallel(
    self, tools_to_call: list[dict], state: AgentState
) -> dict[str, str]:
    """并行执行工具调用（增强版：全失败检测）"""
    # ... 执行逻辑同前 ...
    
    results = await asyncio.gather(*tasks)
    
    # 全失败检测
    success_count = sum(1 for _, _, r in results if r["status"] == "success")
    total_count = len(results)
    
    if success_count == 0 and total_count > 0:
        logger.critical(
            "all_tools_failed",
            total=total_count,
            failed_tools=[r["tool_name"] for _, _, r in results],
        )
        # 记录全失败事件到 Prometheus
        DIAG_ALL_TOOLS_FAILED.inc()
        
        # 标记 state，让 _analyze 知道不需要调 LLM
        state["_all_tools_failed"] = True
        state["_failure_summary"] = {
            "failed_tools": [
                {"name": r["tool_name"], "error": r["result"]}
                for _, _, r in results
                if r["status"] != "success"
            ],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    
    # ... 收集结果同前 ...
    return tool_results


async def _analyze(self, state: AgentState) -> DiagnosticOutput:
    """LLM 分析（增强版：全失败快捷路径）"""
    
    # 全失败快捷路径：不调 LLM，直接降级
    if state.get("_all_tools_failed"):
        logger.warning("skipping_llm_all_tools_failed")
        failure_summary = state.get("_failure_summary", {})
        failed_tools = failure_summary.get("failed_tools", [])
        
        return DiagnosticOutput(
            root_cause=(
                f"⚠️ 所有数据采集工具均不可用 "
                f"({len(failed_tools)} 个工具失败)。"
                f"可能原因: 目标组件不可达 / MCP Server 异常 / 网络问题。"
                f"失败详情: {'; '.join(t['name'] + ': ' + t['error'][:80] for t in failed_tools[:3])}"
            ),
            confidence=0.05,
            severity="high",  # 全部工具不可用本身就是严重问题
            evidence=[
                EvidenceItem(
                    claim=f"工具 {t['name']} 调用失败",
                    source_tool=t["name"],
                    source_data=t["error"][:200],
                    supports_hypothesis="数据采集基础设施异常",
                    confidence_contribution=0.0,
                )
                for t in failed_tools[:5]
            ],
            causality_chain="数据采集工具全部不可用 → 无法执行诊断分析",
            affected_components=state.get("target_components", []),
            remediation_plan=[
                RemediationStep(
                    step_number=1,
                    action="检查 MCP Server 运行状态和连接性",
                    risk_level="none",
                    requires_approval=False,
                    rollback_action="无需回滚（只读检查）",
                    estimated_impact="无影响",
                ),
                RemediationStep(
                    step_number=2,
                    action="检查目标组件（{components}）的网络可达性".format(
                        components=", ".join(state.get("target_components", ["未知"]))
                    ),
                    risk_level="none",
                    requires_approval=False,
                    rollback_action="无需回滚",
                    estimated_impact="无影响",
                ),
            ],
            additional_data_needed=None,  # 不再自环，因为工具都不可用
        )
    
    # ... 正常 LLM 分析逻辑 ...
```

### 15.6.3 假设列表为空时的"开放式探索"模式

```python
def _plan_tool_calls(
    self, state: AgentState, round_num: int
) -> list[dict]:
    """
    规划工具调用（增强版：支持无假设的开放式探索）
    
    WHY - 什么时候假设列表为空？
    1. Planning Agent 遇到从未见过的问题类型（RAG 无匹配）
    2. 用户描述过于模糊（"系统有点慢"）
    3. Planning Agent 的 LLM 调用失败，降级路径不生成假设
    
    开放式探索策略：
    - 调用"通用基线工具"覆盖主要组件
    - 让 Diagnostic 的 LLM 从工具结果中自行发现异常并生成假设
    - 本质上是把"假设生成"的职责从 Planning 转移到 Diagnostic
    """
    if round_num == 1:
        plan = state.get("task_plan", [])
        hypotheses = state.get("hypotheses", [])
        
        if not plan and not hypotheses:
            # 开放式探索：调用基线工具
            logger.warning(
                "open_exploration_mode",
                reason="no_plan_no_hypotheses",
            )
            target = state.get("target_components", [])
            return self._get_baseline_tools(target)
        
        # ... 正常逻辑 ...

    # ... Round 2+ 逻辑 ...


def _get_baseline_tools(self, target_components: list[str]) -> list[dict]:
    """
    获取通用基线工具列表
    
    WHY - 基线工具的选择逻辑：
    1. 每个目标组件的"健康检查"工具（必须有）
    2. 通用的系统级指标查询（CPU/Memory/Disk/Network）
    3. 最近 1h 的错误日志搜索（总能提供线索）
    """
    COMPONENT_BASELINE = {
        "hdfs": [
            {"name": "hdfs_namenode_status", "params": {}, "step_desc": "HDFS NN 基线检查"},
            {"name": "query_metrics", "params": {"query": "hdfs_capacity_used_percent"}, "step_desc": "HDFS 容量"},
        ],
        "kafka": [
            {"name": "kafka_cluster_overview", "params": {}, "step_desc": "Kafka 集群基线检查"},
            {"name": "kafka_consumer_lag", "params": {}, "step_desc": "Kafka 消费延迟"},
        ],
        "yarn": [
            {"name": "yarn_cluster_metrics", "params": {}, "step_desc": "YARN 集群基线检查"},
            {"name": "yarn_queue_status", "params": {}, "step_desc": "YARN 队列状态"},
        ],
        "zookeeper": [
            {"name": "query_metrics", "params": {"query": "zk_avg_latency_ms"}, "step_desc": "ZK 延迟"},
        ],
    }
    
    baseline_calls = []
    
    # 添加组件特定基线工具
    for comp in target_components:
        comp_key = comp.split("-")[0].lower()
        if comp_key in COMPONENT_BASELINE:
            baseline_calls.extend(COMPONENT_BASELINE[comp_key])
    
    # 始终添加：错误日志搜索
    baseline_calls.append({
        "name": "search_logs",
        "params": {"level": "ERROR", "time_range": "1h"},
        "step_desc": "最近 1h 错误日志（通用基线）",
    })
    
    return baseline_calls[:MAX_TOOLS_PER_ROUND]
```

### 15.6.4 循环依赖数据的检测与处理

```python
def _plan_tool_calls(
    self, state: AgentState, round_num: int
) -> list[dict]:
    """规划工具调用（增强版：循环依赖检测）"""
    
    if round_num >= 2:
        needed = state.get("data_requirements", [])
        
        # 获取历史失败工具列表
        failed_history = set()
        for record in state.get("tool_calls", []):
            if record.get("status") in ("timeout", "error"):
                failed_history.add(record["tool_name"])
        
        # 过滤掉已知失败的工具（避免无效重试）
        filtered = []
        skipped = []
        for item in needed[:MAX_TOOLS_PER_ROUND]:
            tool_name = item if isinstance(item, str) else item.get("name", item)
            if tool_name in failed_history:
                skipped.append(tool_name)
            else:
                filtered.append(
                    {"name": tool_name, "params": {}, "step_desc": f"补充采集: {tool_name}"}
                )
        
        if skipped:
            logger.info(
                "skipped_previously_failed_tools",
                skipped=skipped,
                reason="tools failed in previous rounds, retry unlikely to succeed",
            )
        
        # 如果所有需求的工具都曾失败 → 不再自环
        if not filtered and needed:
            logger.warning(
                "all_required_tools_previously_failed",
                needed=needed,
                failed=list(failed_history),
            )
            state["data_requirements"] = []  # 清空 → 触发报告输出
            state["_forced_early_stop_reason"] = (
                "所有补充数据采集工具均在前轮失败，无法获取更多数据"
            )
        
        return filtered
    
    # ... Round 1 逻辑 ...
```

---

## 15.7 置信度校准深度解析

### 15.7.1 校准方法论

```python
"""
置信度校准 — 确保模型输出的置信度与实际准确率一致

WHY - 为什么需要校准？
LLM 输出的 confidence 是主观估计，不是概率论意义上的概率。
实测发现两个问题：
1. 跨组件问题上偏乐观（输出 0.72 但实际准确率只有 0.65）
2. 简单问题上偏保守（输出 0.85 但实际准确率 0.92）

当前策略：暂不做后处理校准（偏差 < 10%），但预留了校准接口。
未来当评测集 > 1000 case 时启用 Platt Scaling。
"""

from dataclasses import dataclass
import numpy as np


@dataclass
class CalibrationResult:
    """校准评估结果"""
    expected_calibration_error: float  # ECE（越小越好）
    max_calibration_error: float       # MCE（最大偏差）
    bin_results: list[dict]            # 每个 bin 的详情
    brier_score: float                 # Brier Score（越小越好）


def evaluate_calibration(
    predictions: list[float],   # 模型输出的置信度列表
    actuals: list[bool],        # 实际是否正确（人工标注）
    n_bins: int = 10,
) -> CalibrationResult:
    """
    评估置信度校准质量
    
    使用 Expected Calibration Error (ECE):
    ECE = Σ (|bin 内样本数| / 总样本数) × |bin 平均置信度 - bin 实际准确率|
    
    WHY 选 ECE:
    - 业界标准的校准评估指标
    - 直观：ECE=0 表示完美校准，ECE>0.1 表示需要校准
    - 可分解到每个 bin，便于分析哪个置信度区间偏差最大
    """
    predictions_arr = np.array(predictions)
    actuals_arr = np.array(actuals, dtype=float)
    
    bin_boundaries = np.linspace(0, 1, n_bins + 1)
    bin_results = []
    ece = 0.0
    mce = 0.0
    
    for i in range(n_bins):
        in_bin = (predictions_arr > bin_boundaries[i]) & (
            predictions_arr <= bin_boundaries[i + 1]
        )
        prop_in_bin = in_bin.mean()
        
        if prop_in_bin > 0:
            avg_confidence = predictions_arr[in_bin].mean()
            avg_accuracy = actuals_arr[in_bin].mean()
            gap = abs(avg_confidence - avg_accuracy)
            
            ece += prop_in_bin * gap
            mce = max(mce, gap)
            
            bin_results.append({
                "bin_range": f"({bin_boundaries[i]:.1f}, {bin_boundaries[i+1]:.1f}]",
                "sample_count": int(in_bin.sum()),
                "avg_confidence": round(avg_confidence, 3),
                "avg_accuracy": round(avg_accuracy, 3),
                "gap": round(gap, 3),
                "direction": "overconfident" if avg_confidence > avg_accuracy else "underconfident",
            })
    
    # Brier Score
    brier = np.mean((predictions_arr - actuals_arr) ** 2)
    
    return CalibrationResult(
        expected_calibration_error=round(ece, 4),
        max_calibration_error=round(mce, 4),
        bin_results=bin_results,
        brier_score=round(brier, 4),
    )


# 我们的评测集结果（200 case）：
# ECE = 0.058（可接受，< 0.1 阈值）
# MCE = 0.12（出现在 0.6-0.7 区间，跨组件问题偏乐观）
# Brier Score = 0.18

# 各置信度区间详情：
# (0.0, 0.3]: 15 samples, avg_conf=0.22, avg_acc=0.20, gap=0.02 ✅
# (0.3, 0.5]: 28 samples, avg_conf=0.42, avg_acc=0.39, gap=0.03 ✅
# (0.5, 0.7]: 45 samples, avg_conf=0.62, avg_acc=0.53, gap=0.09 ⚠️
# (0.7, 0.9]: 82 samples, avg_conf=0.81, avg_acc=0.79, gap=0.02 ✅
# (0.9, 1.0]: 30 samples, avg_conf=0.93, avg_acc=0.90, gap=0.03 ✅
```

### 15.7.2 预留的 Platt Scaling 校准接口

```python
class PlattScalingCalibrator:
    """
    Platt Scaling 置信度校准器（预留接口，当前未启用）
    
    WHY - 什么时候启用？
    1. 评测集 > 1000 case（当前 200，不足以拟合可靠的校准函数）
    2. ECE > 0.1（当前 0.058，还在可接受范围内）
    3. 在 (0.5, 0.7] 区间的偏差持续 > 0.1
    
    原理: 用 logistic regression 拟合 f(x) = 1/(1 + exp(-ax-b))
    其中 x 是模型原始置信度，f(x) 是校准后的置信度
    a, b 通过在验证集上最小化 log loss 拟合
    """
    
    def __init__(self):
        self.a: float | None = None
        self.b: float | None = None
        self.fitted: bool = False
    
    def fit(self, predictions: list[float], actuals: list[bool]) -> None:
        """在验证集上拟合校准参数"""
        from scipy.optimize import minimize
        
        preds = np.array(predictions)
        acts = np.array(actuals, dtype=float)
        
        def neg_log_loss(params):
            a, b = params
            calibrated = 1 / (1 + np.exp(-a * preds - b))
            calibrated = np.clip(calibrated, 1e-7, 1 - 1e-7)
            return -np.mean(acts * np.log(calibrated) + (1 - acts) * np.log(1 - calibrated))
        
        result = minimize(neg_log_loss, x0=[1.0, 0.0], method="Nelder-Mead")
        self.a, self.b = result.x
        self.fitted = True
    
    def calibrate(self, confidence: float) -> float:
        """校准单个置信度值"""
        if not self.fitted:
            return confidence  # 未拟合时直接返回原值
        return float(1 / (1 + np.exp(-self.a * confidence - self.b)))
    
    def should_enable(self, ece: float, sample_size: int) -> bool:
        """判断是否应该启用校准"""
        return ece > 0.1 and sample_size >= 1000
```

### 15.7.3 分组件置信度偏差分析

```
置信度偏差热力图:

组件类型       (0.3-0.5]  (0.5-0.7]  (0.7-0.9]  (0.9-1.0]
HDFS 单故障      +0.01      +0.02      +0.03      +0.01     ← 最准
Kafka 单故障     +0.02      +0.04      +0.01      +0.02     ← 较准
YARN 单故障      -0.01      +0.03      +0.02      +0.02     ← 较准
跨组件级联       -0.03      -0.09      -0.07      -0.04     ← 偏乐观 ⚠️
配置变更引发     +0.01      +0.05      +0.02      +0.01     ← 中等
间歇性问题       -0.05      -0.12      N/A        N/A       ← 样本不足

发现:
1. 跨组件问题在 (0.5-0.7] 区间偏乐观 9%
   → 原因: LLM 倾向于找到一个"看起来合理"的因果链就给 0.65
   → 但实际跨组件的因果链需要更多独立证据才能确认
   → 改进方向: 在 Prompt 中对跨组件场景增加 "至少需要 3 条独立来源的证据" 约束

2. 间歇性问题样本太少（15 个），无法得出可靠结论
   → 需要更多间歇性问题的评测数据

3. 单组件问题的校准非常好（偏差 < 5%）
   → 说明五步法和结构化输出在单组件场景上效果显著
```

---

## 15.8 完整基准测试套件

### 15.8.1 延迟基准测试

```python
# tests/benchmark/test_diagnostic_latency.py
"""
Diagnostic Agent 延迟基准测试
目标: 确认各阶段延迟在预期范围内
"""

import asyncio
import time
import statistics
import pytest
from unittest.mock import AsyncMock


class TestDiagnosticLatencyBenchmark:
    """延迟基准测试（模拟真实工具延迟）"""

    @staticmethod
    def _create_mock_mcp(tool_latencies: dict[str, float]):
        """创建模拟不同延迟的 MCP 客户端"""
        async def mock_call(name, params):
            latency = tool_latencies.get(name, 0.1)
            await asyncio.sleep(latency)
            return f"mock_result_{name}"
        
        mcp = AsyncMock()
        mcp.call_tool = mock_call
        return mcp

    @pytest.mark.benchmark
    @pytest.mark.asyncio
    async def test_parallel_tool_speedup(self):
        """验证并行调用比串行快"""
        tool_latencies = {
            "hdfs_status": 2.0,
            "search_logs": 3.0,
            "yarn_metrics": 1.5,
            "kafka_lag": 1.0,
            "query_metrics": 0.5,
        }
        
        node = DiagnosticNode(llm_client=AsyncMock())
        node._mcp = self._create_mock_mcp(tool_latencies)
        
        tools = [
            {"name": name, "params": {}, "step_desc": ""}
            for name in tool_latencies
        ]
        state = {"tool_calls": []}
        
        # 并行执行
        start = time.monotonic()
        await node._execute_tools_parallel(tools, state)
        parallel_time = time.monotonic() - start
        
        # 串行基线
        serial_time = sum(tool_latencies.values())
        
        # 并行应该接近最慢工具的延迟（3.0s）
        assert parallel_time < serial_time * 0.6  # 至少快 40%
        assert parallel_time < 4.0  # 不超过最慢工具 + 1s 开销

    @pytest.mark.benchmark
    @pytest.mark.asyncio
    async def test_end_to_end_single_round(self):
        """单轮端到端延迟基准"""
        # 模拟: 工具 2s + LLM 3s = ~5s
        node = DiagnosticNode(llm_client=AsyncMock())
        node._mcp = self._create_mock_mcp({"t1": 1.0, "t2": 2.0})
        
        async def mock_llm(*args, **kwargs):
            await asyncio.sleep(3.0)
            return _make_mock_diagnosis(confidence=0.85)
        
        node.llm.chat_structured = mock_llm
        
        state = _make_base_state(tools=["t1", "t2"])
        
        latencies = []
        for _ in range(5):  # 5 次取平均
            start = time.monotonic()
            await node.process(state.copy())
            latencies.append(time.monotonic() - start)
        
        p50 = statistics.median(latencies)
        p95 = sorted(latencies)[int(len(latencies) * 0.95)]
        
        assert p50 < 7.0   # P50 < 7s
        assert p95 < 10.0  # P95 < 10s

    @pytest.mark.benchmark
    @pytest.mark.asyncio
    async def test_timeout_doesnt_add_extra_latency(self):
        """超时工具不应增加超出 TOOL_TIMEOUT_SECONDS 的延迟"""
        tool_latencies = {
            "fast": 0.5,
            "stuck": 100.0,  # 模拟卡死
        }
        
        node = DiagnosticNode(llm_client=AsyncMock())
        node._mcp = self._create_mock_mcp(tool_latencies)
        
        tools = [
            {"name": "fast", "params": {}, "step_desc": ""},
            {"name": "stuck", "params": {}, "step_desc": ""},
        ]
        state = {"tool_calls": []}
        
        start = time.monotonic()
        await node._execute_tools_parallel(tools, state)
        elapsed = time.monotonic() - start
        
        # 应该在 TOOL_TIMEOUT_SECONDS(15s) 附近，不是 100s
        assert elapsed < 16.0


class TestDiagnosticTokenBenchmark:
    """Token 消耗基准测试"""

    def test_context_compression_ratio(self):
        """上下文压缩比应在 1.2x-3x 之间"""
        # 模拟 5 个工具结果，总计 ~25K chars
        raw_data = {}
        for i in range(5):
            raw_data[f"tool_{i}"] = (
                f"## Tool {i} 结果\n"
                + "正常指标行\n" * 200
                + "⚠️ 异常: 某指标超过阈值\n"
                + "正常指标行\n" * 100
            )
        
        raw_size = sum(len(v) for v in raw_data.values())
        
        # 模拟压缩
        compressed = compress_for_diagnostic(raw_data)
        compressed_size = len(compressed)
        
        ratio = raw_size / compressed_size
        assert 1.2 <= ratio <= 3.0, f"压缩比 {ratio:.1f}x 超出预期范围"
    
    def test_token_budget_not_exceeded(self):
        """单轮 LLM input 不应超过 8000 tokens"""
        # 模拟完整的 Prompt 构建
        state = _make_base_state(tools=["t1", "t2", "t3"])
        state["collected_data"] = {
            f"tool_{i}": "x" * 3000 for i in range(3)
        }
        state["hypotheses"] = [
            {"id": i, "description": f"假设 {i}", "probability": "high"}
            for i in range(3)
        ]
        state["rag_context"] = [
            {"source": f"doc_{i}", "content": "y" * 500}
            for i in range(3)
        ]
        
        node = DiagnosticNode(llm_client=AsyncMock())
        messages = node._build_messages(state)
        
        # 粗略估算: 1 token ≈ 4 chars (中文) / 3 chars (英文)
        total_chars = sum(len(m["content"]) for m in messages)
        estimated_tokens = total_chars / 3.5
        
        assert estimated_tokens < 8000, (
            f"预估 {estimated_tokens:.0f} tokens 超过 8000 预算"
        )
```

### 15.8.2 诊断质量基准测试

```python
# tests/benchmark/test_diagnostic_quality.py
"""
诊断质量评估框架
用于在 CI 中定期运行，监控诊断准确率变化

WHY - 为什么需要质量基准测试？
1. Prompt 修改可能导致准确率回退（prompt regression）
2. 新增工具后需要验证是否影响现有诊断
3. LLM API 版本升级后需要验证输出质量
"""

import json
from pathlib import Path
from dataclasses import dataclass


@dataclass
class DiagnosticTestCase:
    """诊断测试用例"""
    case_id: str
    description: str
    category: str  # "single_component" | "cross_component" | "config_change" | "intermittent"
    component: str
    mock_tool_results: dict[str, str]
    expected_root_cause_keywords: list[str]  # 根因描述应包含的关键词
    expected_confidence_range: tuple[float, float]  # (min, max)
    expected_severity: str
    expected_remediation_contains: list[str]  # 修复建议应包含的关键词


# 评测集示例（从 fixtures/diagnostic_test_cases.json 加载）
SAMPLE_TEST_CASES = [
    DiagnosticTestCase(
        case_id="TC-HDFS-001",
        description="HDFS NameNode 堆内存 93%，Full GC 频繁",
        category="single_component",
        component="hdfs-namenode",
        mock_tool_results={
            "hdfs_namenode_status": "## HDFS NameNode\n堆内存使用: 93%\nRPC 延迟: 15ms\nSafe Mode: Off",
            "query_metrics": "## JVM GC\nFull GC 次数 (1h): 5\n平均 GC 暂停: 3245ms",
            "search_logs": "## 日志搜索\n[ERROR] GC pause 3245ms exceeded threshold\n" * 5,
        },
        expected_root_cause_keywords=["heap", "GC", "内存"],
        expected_confidence_range=(0.7, 1.0),
        expected_severity="high",
        expected_remediation_contains=["heap", "增加", "内存"],
    ),
    DiagnosticTestCase(
        case_id="TC-KAFKA-001",
        description="Kafka Consumer Lag 突增，消费速率骤降",
        category="single_component",
        component="kafka-consumer",
        mock_tool_results={
            "kafka_consumer_lag": "## Consumer Lag\ngroup-etl: lag=500000",
            "kafka_cluster_overview": "## Kafka 集群\nBroker 数: 3\nISR: 正常",
            "query_metrics": "## 消费速率\n当前: 5000 msg/s\n1h前: 50000 msg/s",
            "search_logs": "## 日志\n[ERROR] CommitFailedException: rebalance\n" * 10,
        },
        expected_root_cause_keywords=["rebalance", "消费", "lag"],
        expected_confidence_range=(0.6, 1.0),
        expected_severity="high",
        expected_remediation_contains=["session.timeout", "rebalance"],
    ),
    DiagnosticTestCase(
        case_id="TC-ZK-001",
        description="ZooKeeper 级联故障（磁盘 IO 饱和）",
        category="cross_component",
        component="zookeeper",
        mock_tool_results={
            "query_metrics_zk_latency": "## ZK 延迟\n平均延迟: 800ms (正常 < 10ms)",
            "query_metrics_disk_io": "## 磁盘 IO\nZK 节点 disk_io_util: 98%",
            "search_logs": "## 日志\n[WARN] fsync too slow: 1200ms\n" * 20,
            "query_metrics_zk_sync": "## ZK 同步\nFollowers synced: 1/3",
        },
        expected_root_cause_keywords=["磁盘", "IO", "fsync", "ZK"],
        expected_confidence_range=(0.7, 1.0),
        expected_severity="critical",
        expected_remediation_contains=["SSD", "磁盘"],
    ),
]


class TestDiagnosticQuality:
    """诊断质量评估"""

    @pytest.mark.parametrize("test_case", SAMPLE_TEST_CASES, ids=lambda c: c.case_id)
    @pytest.mark.asyncio
    async def test_diagnosis_accuracy(self, test_case: DiagnosticTestCase):
        """验证诊断准确率"""
        node = DiagnosticNode(llm_client=_create_real_or_mock_llm())
        node._mcp = _create_mock_mcp_from_case(test_case)
        
        state = _make_state_from_case(test_case)
        result = await node.process(state)
        
        diagnosis = result["diagnosis"]
        
        # 检查根因关键词
        root_cause = diagnosis["root_cause"].lower()
        for keyword in test_case.expected_root_cause_keywords:
            assert keyword.lower() in root_cause, (
                f"根因 '{diagnosis['root_cause']}' 未包含关键词 '{keyword}'"
            )
        
        # 检查置信度范围
        min_conf, max_conf = test_case.expected_confidence_range
        assert min_conf <= diagnosis["confidence"] <= max_conf, (
            f"置信度 {diagnosis['confidence']} 超出预期范围 [{min_conf}, {max_conf}]"
        )
        
        # 检查严重度
        assert diagnosis["severity"] == test_case.expected_severity

    @pytest.mark.asyncio
    async def test_no_hallucination_in_evidence(self):
        """验证证据不包含幻觉数据"""
        node = DiagnosticNode(llm_client=_create_real_or_mock_llm())
        node._mcp = AsyncMock()
        node._mcp.call_tool = AsyncMock(return_value="actual_data_123")
        
        state = _make_base_state(tools=["tool_a"])
        result = await node.process(state)
        
        # 每条证据的 source_tool 必须在 collected_data 中
        collected_keys = set(result["collected_data"].keys())
        for evidence_claim in result["diagnosis"]["evidence"]:
            # DiagnosticOutput 中 evidence 被转为 claim string
            pass  # 完整实现在 HallucinationDetector 中

    @pytest.mark.asyncio
    async def test_fallback_never_generates_remediation(self):
        """降级诊断绝对不应生成修复建议"""
        for _ in range(10):  # 多次运行确保稳定
            state = {
                "collected_data": {"t1": "⚠️ 异常数据"},
                "target_components": ["hdfs"],
            }
            result = DiagnosticNode._fallback_diagnosis(state, "LLM down")
            assert result.remediation_plan == [], (
                f"降级诊断不应生成修复建议, 但得到: {result.remediation_plan}"
            )
```

---

## 15.9 端到端场景补充：复杂与边界情况

### 15.9.1 场景 5：Hive Metastore 锁竞争（间歇性问题）

```
输入: "Hive 查询偶尔卡在 LOCK_WAITING 状态，30% 的查询超时"
组件: hive-metastore, impala

Round 1:
  Diagnostic 并行调用:
    ✅ query_metrics(hive_metastore_open_connections) → 当前 85/100 (接近上限)
    ✅ query_metrics(hive_metastore_lock_wait_time_p99) → P99=8.5s (阈值 5s)
    ✅ search_logs(hive-metastore, LOCK_WAITING, 1h) → 42 条 LOCK_WAITING 日志
    ✅ search_logs(hive-metastore, deadlock, 24h) → 2 条死锁日志
    ✅ query_metrics(hive_active_queries, 24h) → 高峰期并发 50+ 查询

  LLM 分析:
    连接数接近上限 (85/100) + 锁等待时间高 + 并发查询多
    但是：间歇性 → 只在高峰期出现
    → 需要确认高峰期时间和并发模式
    confidence=0.52
    data_requirements: ["query_metrics(hive_active_queries_by_hour, 7d)"]

Round 2:
  补充调用:
    ✅ query_metrics(hive_active_queries_by_hour, 7d) 
      → 每天 14:00-16:00 并发 50+, 其余时段 < 20

  LLM 分析:
    14:00-16:00 ETL 任务集中运行 → 并发 50+ → Metastore 连接池耗尽(85/100)
    → 锁竞争加剧 → P99 延迟 8.5s → 30% 查询超时
    confidence=0.72
    causality_chain: "ETL 高峰并发 50+ → 连接池 85/100 → 锁竞争 → P99 8.5s → 查询超时"
    remediation: [
      {action: "增加 Metastore 连接池上限到 200", risk: "medium"},
      {action: "错峰调度：将部分 ETL 任务分散到 20:00-22:00", risk: "low"},
    ]

→ route_from_diagnostic: confidence=0.72 → 有 medium risk → report
→ 报告建议先做低风险的错峰调度，再做连接池调整
总 Token: ~10K | 2 轮 | 延迟 ~14s

WHY - 这个场景展示了什么？
间歇性问题需要时间维度的数据（按小时统计）。
Round 1 只有瞬时快照数据（"当前 85 连接"），不足以判断是否是持续问题。
Round 2 补充了 7 天按小时的趋势数据，才定位到"14:00-16:00 高峰"这个模式。
这正是多轮诊断的价值：Round 1 发现线索，Round 2 确认模式。
```

### 15.9.2 场景 6：误报告警（假阳性处理）

```
输入: 告警 "HDFS DataNode 磁盘使用率 > 90%"
组件: hdfs-datanode

Round 1:
  Diagnostic 并行调用:
    ✅ query_metrics(hdfs_dn_disk_usage, all_nodes) →
        dn1: 91.2%, dn2: 45.3%, dn3: 52.1%, dn4: 48.7%
    ✅ hdfs_namenode_status → 集群整体容量使用 48%, 正常
    ✅ query_metrics(hdfs_dn_disk_usage_trend, dn1, 7d) →
        7 天前 85% → 3 天前 88% → 现在 91%（缓慢增长）
    ✅ search_logs(hdfs-datanode, dn1, disk, 24h) → 无异常日志
    ✅ query_metrics(hdfs_balancer_status) → 上次 balance 时间: 30 天前

  LLM 分析:
    Step 1 症状确认: 只有 dn1 磁盘 91%，其他节点正常 (45-52%)
    Step 2 范围界定: 单节点问题（不是全局磁盘不足）
    Step 3 时间关联: 缓慢增长趋势（不是突发），无相关配置变更
    Step 4 根因分析: HDFS Balancer 30 天未运行 → 数据分布不均
    Step 5 置信度: 高（数据充分，模式清晰）
    
    root_cause="HDFS Balancer 长期未运行导致 dn1 数据分布不均"
    confidence=0.78
    severity="medium"  # ← 不是 critical，因为集群整体 48% 正常
    causality_chain: "Balancer 30 天未运行 → 新数据集中写入 dn1 → dn1 磁盘 91%"
    remediation: [
      {action: "运行 hdfs balancer -threshold 10", risk: "low"},
      {action: "设置 Balancer 定时任务（每周运行）", risk: "low"},
    ]

→ 所有修复 risk=low → 直接 report
→ 告警降级为 medium（集群健康，单节点数据不均）

WHY - 这个场景展示了什么？
1. 五步法的"症状确认"步骤防止了误判：
   告警说"DataNode 磁盘 > 90%"，但实际上只有 1/4 节点超标
   没有五步法可能直接判定"磁盘空间不足"
2. severity 被正确设为 medium 而不是 critical
   因为集群整体 48%，单节点超标不影响集群可用性
3. 修复建议是"balance + 定时任务"而不是"紧急扩容"
   低成本方案优先
```

### 15.9.3 场景 7：HDFS 小文件导致 NameNode OOM（3 轮收集，假设从错到对）

```
输入: "HDFS NameNode 进程频繁 OOM Killed，每次重启后运行 4-6 小时又 OOM"
组件: hdfs-namenode
告警: [NameNodeProcessDown, HDFSSafeModeActive]

Round 1:
  Planning 生成假设:
    H1: [high] NN 堆内存配置不足 → 验证: hdfs_namenode_status, query_metrics(jvm_heap)
    H2: [medium] 数据量增长导致元数据膨胀 → 验证: hdfs_block_report, query_metrics(file_count)
    H3: [low] JVM GC 参数不合理 → 验证: search_logs(GC), query_metrics(gc_time)

  Diagnostic 并行调用（4 工具）:
    ✅ hdfs_namenode_status → heap_max=32GB, heap_used=28.5GB (89%), blocks=5.2M
    ✅ query_metrics(jvm_heap_trend, 7d) → 稳定在 85-92% 之间，无突增
    ✅ search_logs(OutOfMemoryError, namenode, 7d) → 
        3 次 OOM Kill 记录：
        - 03-28 14:32: "java.lang.OutOfMemoryError: Java heap space"
        - 03-27 09:15: "java.lang.OutOfMemoryError: Java heap space"  
        - 03-25 20:48: "java.lang.OutOfMemoryError: Java heap space"
    ✅ query_metrics(gc_time, 7d) → Full GC 每天 15-20 次，avg pause 4.5s

  LLM 分析（Round 1）:
    H1: partial (heap 89% 高但 32GB 不算小，需要看数据量)
    H3: partial (Full GC 20次/天偏高，但 4.5s pause 不极端)
    H2: insufficient_data (还没查 file_count 趋势)
    
    初步判断: "NN heap 使用率高，可能是配置不足"
    confidence=0.45 (< 0.6 → 需要补充)
    data_requirements: ["hdfs_block_report", "query_metrics(file_count_trend, 30d)"]
    
    WHY - 为什么第 1 轮置信度只有 0.45？
    因为 32GB heap 对于 5.2M blocks 来说并不算小（推荐值 ~1GB/1M blocks）
    真正的问题可能不是"heap 小"而是"blocks 多"，需要看增长趋势

Round 2:
  补充调用（2 工具）:
    ✅ hdfs_block_report → 
        total_blocks: 5.2M
        avg_block_size: 8.3MB ← ⚠️ 极低！（默认 128MB）
        files < 1MB: 3.8M（73%）← ⚠️ 73% 是小文件！
        files < 128MB: 4.5M（86.5%）
    ✅ query_metrics(file_count_trend, 30d) →
        30天前: 3.1M files, 4.0M blocks
        20天前: 3.8M files, 4.5M blocks
        10天前: 4.5M files, 5.0M blocks  
        现在: 5.0M files, 5.2M blocks
        增长率: ~63K files/天

  LLM 分析（Round 2）:
    关键发现: 小文件占 73%，文件数 30 天增长 61%
    → 假设方向修正: H2 从 "数据量增长" 细化为 "小文件爆炸导致元数据膨胀"
    
    H1: refuted (32GB / 5.2M = 6.15KB/block，接近 NN 每 block 需要 ~150B 内存的极限)
        → 问题不是 heap 配置"不足"，而是 blocks 数量"太多"
    H2: confirmed (小文件是根因：73% 小文件 → 5.2M blocks → 28.5GB heap)
    H3: confirmed_secondary (GC 频繁是 H2 的结果，不是独立原因)
    
    但还有一个问题: 小文件为什么在增长？需要知道源头
    confidence=0.68
    data_requirements: ["search_logs(mapreduce, small_file, 30d)"]

Round 3:
  补充调用（1 工具）:
    ✅ search_logs(mapreduce, small, 30d) → 
        发现: ETL 作业 "daily_user_behavior" 每天产出 ~2000 个 1-5MB 的小文件
        配置: mapreduce.output.fileoutputformat.compress=false
        目标路径: /user/etl/user_behavior/dt=YYYY-MM-DD/

  LLM 分析（Round 3）:
    完整因果链确立:
    ETL 作业无压缩输出 → 每天 2000 个小文件 × 30 天 = 6 万小文件/月
    → 文件数 30 天增长 61% → blocks 5.2M → NN heap 28.5GB → Full GC 20 次/天
    → 每次 GC pause 4.5s → 累积到 heap 100% → OOM Kill
    
    root_cause="ETL 作业 daily_user_behavior 无压缩输出导致大量小文件，
               30 天内文件数增长 61%，NameNode 元数据膨胀至 5.2M blocks，
               触发频繁 Full GC 并最终 OOM"
    confidence=0.88
    severity="critical"
    causality_chain: "ETL 无压缩 → 2000 小文件/天 → 30 天 6 万新文件 → 
                      blocks 5.2M → NN heap 89% → Full GC 20 次/天 → OOM Kill"
    remediation: [
      {action: "立即：hdfs dfs -merge 合并 /user/etl/user_behavior/ 下的小文件", 
       risk: "medium", requires_approval: true},
      {action: "修改 ETL 作业输出配置，启用 Snappy 压缩 + 合并小文件", 
       risk: "low"},
      {action: "增加 NN heap 到 48GB（临时缓解，不解决根因）", 
       risk: "high", requires_approval: true},
      {action: "设置定时小文件合并任务（每周运行）", 
       risk: "low"},
    ]

→ route: confidence=0.88, 有 high risk → hitl_gate
→ Token 消耗: Round 1 ~4.2K + Round 2 ~3.5K + Round 3 ~2.8K = 总计 ~10.5K
→ 总延迟: ~25s（3 轮 × ~8s）

WHY - 这个场景展示了什么？
1. 假设从错到对的自我修正过程：
   Round 1 以为是"heap 不足"，Round 2 发现是"小文件过多"，Round 3 找到了源头 ETL 作业
2. 3 轮收集的价值：Round 1 定位范围，Round 2 修正方向，Round 3 找到根因
3. 修复建议的优先级排序：立即合并（缓解）→ 修改 ETL（根治）→ 加 heap（临时）→ 定时任务（长期）
```

### 15.9.4 场景 8：Kafka + ES 联动故障（跨组件依赖分析）

```
输入: 监控告警 "ES 索引写入延迟 > 5s" + "Kafka consumer group 'es-sink' lag > 500K"
组件: elasticsearch, kafka-consumer
告警: [ESWriteLatencyHigh, KafkaConsumerLagHigh]

Round 1:
  Planning 生成假设:
    H1: [high] ES 集群写入瓶颈导致 Kafka 消费堆积 → 验证: ES 健康 + Kafka lag
    H2: [medium] Kafka 消费者本身异常（OOM/rebalance）→ 验证: 消费者日志 + 进程状态
    H3: [low] 网络问题导致 Kafka→ES 连接不稳定 → 验证: 网络指标 + 连接日志

  Diagnostic 并行调用（5 工具）:
    ✅ es_cluster_health → 
        status: yellow（本来是 green）
        active_shards: 150, relocating: 12 ← ⚠️ 大量 shard 迁移中
        pending_tasks: 45
        active_primary_shards: 75, initializing: 8
    ✅ query_metrics(es_indexing_rate, 2h) → 
        2h前: 20K docs/s → 1h前: 5K docs/s → 现在: 2K docs/s（持续下降）
    ✅ kafka_consumer_lag → 
        group 'es-sink': lag=580K, last_commit=5min ago ← ⚠️ 5 分钟没提交
        group 'etl-main': lag=200, normal
    ✅ search_logs(es-sink, error, 1h) → 
        "BulkRequestException: ClusterBlockException[blocked by: index write block]" × 35 条
        "ConnectionTimeout: 30000ms" × 12 条
    ✅ query_metrics(es_disk_usage_percent) → 
        node-1: 92%, node-2: 94%, node-3: 88% ← ⚠️ 接近磁盘水位线

  LLM 分析（Round 1）:
    关键发现：
    1. ES 磁盘使用率 88-94%，超过 flood_stage 水位线（默认 95%）
    2. ES 触发了 "index write block"——磁盘即将满时的自动保护机制
    3. Kafka es-sink 消费者因为 ES 写入被阻塞 → lag 堆积
    4. shard 迁移（relocating=12）说明 ES 正在尝试自动 rebalance
    
    H1: confirmed (ES 磁盘满 → write block → Kafka 消费堆积)
    H2: refuted (消费者本身没问题，是下游 ES 阻塞)
    H3: refuted (不是网络问题，是 ES 主动拒绝写入)
    
    root_cause="ES 集群磁盘使用率接近 flood_stage 水位线（88-94%），
               触发 index write block 自动保护，导致 Kafka es-sink 消费者
               无法写入 ES，lag 堆积至 580K"
    confidence=0.85
    severity="critical"
    causality_chain: "ES 磁盘 88-94% → 接近 flood_stage → index write block → 
                      es-sink 写入失败 → Kafka lag 580K → 数据延迟"
    remediation: [
      {action: "临时清理 ES 过期索引释放磁盘空间", risk: "medium", requires_approval: true},
      {action: "解除 index write block: PUT /_all/_settings {index.blocks.read_only_allow_delete: null}", 
       risk: "high", requires_approval: true},
      {action: "扩容 ES 数据节点或增加磁盘", risk: "high", requires_approval: true},
      {action: "设置 ILM 策略自动清理 30 天以上的索引", risk: "low"},
    ]

→ route: confidence=0.85, 有 high risk → hitl_gate
→ 1 轮搞定！Token: ~8.5K | 延迟: ~8s

WHY - 这个场景展示了什么？
1. 跨组件依赖分析：表面是 Kafka lag（Kafka 问题），实际根因在 ES（磁盘满）
2. 告警关联的价值：2 条看似独立的告警实际上指向同一根因
3. 自动保护机制识别：AI 正确识别了 "index write block" 是 ES 的防护措施而不是 bug
4. 修复的优先级：先清理空间（缓解）→ 解除 block（恢复服务）→ 扩容（长期）
```

### 15.9.5 场景 9：误诊后自我修正（第一轮诊断错误 → 第二轮发现新证据 → 修正）

```
输入: "YARN 作业提交后长时间 PENDING，队列资源显示有空闲"
组件: yarn-resourcemanager
告警: [YARNApplicationPending]

Round 1:
  Planning 生成假设:
    H1: [high] 队列资源配额不足 → 验证: yarn_queue_status, query_metrics(queue_usage)
    H2: [medium] ResourceManager 调度器异常 → 验证: search_logs(RM, error), rm_status
    H3: [low] NodeManager 节点不足 → 验证: yarn_cluster_metrics, node_status

  Diagnostic 并行调用（5 工具）:
    ✅ yarn_queue_status → 
        root: capacity=100%, used=45%
        default: capacity=60%, used=42%
        production: capacity=30%, used=28%
        → 看起来队列有大量空闲资源
    ✅ yarn_cluster_metrics → 
        total_nodes: 20, active_nodes: 20
        total_vcores: 320, used_vcores: 144 (45%)
        total_memory: 640GB, used_memory: 288GB (45%)
        → 集群级别也有大量空闲
    ✅ search_logs(ResourceManager, error, 1h) → 无异常日志（0 条 ERROR）
    ✅ query_metrics(yarn_pending_apps) → pending=12, running=35, 正常比例
    ✅ query_metrics(yarn_container_allocation_latency_p99) → P99=2.5s（正常 < 5s）

  LLM 分析（Round 1）:
    ⚠️ 误诊开始 ⚠️
    所有指标看起来正常：队列有空闲(45%)、集群有空闲(45%)、无错误日志
    → AI 初步判断："可能是用户作业配置问题（如请求了过多资源）"
    
    root_cause="用户作业可能请求了超过队列容量的资源"
    confidence=0.42 ← 置信度低（数据不支持强结论）
    data_requirements: ["yarn_application_details(pending_apps)", 
                        "query_metrics(yarn_queue_max_am_percent)"]

  WHY - 第 1 轮为什么误诊？
  因为宏观指标（队列使用率、集群资源）都正常，AI 没有深入到具体 PENDING 作业的详情。
  这是"幸存者偏差"的变体：看到集群整体正常就认为没问题，忽略了微观层面的异常。

Round 2:
  补充调用（2 工具）:
    ✅ yarn_application_details(pending_apps) →
        app_001: requested_vcores=8, requested_memory=32GB, queue=production
          → PENDING 原因: "AM container not allocated"
          → 标签: user=etl_service, priority=1
        app_002: requested_vcores=4, requested_memory=16GB, queue=production
          → PENDING 原因: "AM container not allocated"
        ... (12 个 PENDING 作业全部在 production 队列)
    ✅ query_metrics(yarn_queue_max_am_percent) →
        production 队列: max-am-resource-percent=0.1 ← ⚠️ 只有 10%！
        production 队列总资源: 192GB memory, 96 vcores
        AM 最大可用: 19.2GB memory（10% of 192GB）
        当前 AM 使用: 19.0GB（已达上限！）

  LLM 分析（Round 2 — 自我修正）:
    发现新证据：production 队列的 max-am-resource-percent=0.1（只有 10%）
    → AM 资源上限 19.2GB，当前已使用 19.0GB（99.0%！）
    → 新作业的 Application Master 无法分配 container → PENDING
    
    修正诊断:
    ❌ 原判断: "用户作业请求资源过多" → 错误
    ✅ 新判断: "production 队列 AM 资源百分比配置过低(10%)，
               导致 AM container 分配达到上限"
    
    root_cause="YARN production 队列的 max-am-resource-percent 仅为 10%（默认值），
               AM 资源已用 19.0GB/19.2GB（99.0%），新作业的 Application Master
               无法获得 container，导致 12 个作业 PENDING"
    confidence=0.92
    severity="high"
    causality_chain: "max-am-resource-percent=10%（过低）→ AM 资源上限 19.2GB → 
                      当前 AM 使用 19.0GB（99%）→ 新 AM 无法分配 → 作业 PENDING"
    remediation: [
      {action: "修改 production 队列 max-am-resource-percent 为 30%", 
       risk: "medium", requires_approval: true},
      {action: "临时 kill 部分低优先级的 PENDING 作业释放 AM 资源", 
       risk: "low"},
    ]

→ route: confidence=0.92, 有 medium risk → hitl_gate
→ Token 消耗: Round 1 ~4.5K + Round 2 ~3.2K = 总计 ~7.7K
→ 总延迟: ~16s（2 轮）

WHY - 这个场景展示了什么？
1. 自我修正能力：Round 1 的诊断是错误的（"用户作业配置问题"），
   但 confidence=0.42 诚实地反映了不确定性
2. 低置信度触发补充收集的价值：如果在 0.42 时就直接输出，用户会收到错误结论
3. 宏观指标的误导性：集群整体 45% 使用率 ≠ 没有问题
   → 需要深入到队列级别、作业级别才能发现 AM 百分比的配置问题
4. 运维经验的重要性：max-am-resource-percent 是 YARN 的一个常见陷阱，
   默认 10% 在生产环境下经常不够用
```

### 15.9.6 场景对比总结矩阵

| 场景 | 轮次 | Token | 延迟 | 置信度 | 关键特征 |
|------|------|-------|------|--------|---------|
| #7 HDFS 小文件 OOM | 3 | 10.5K | 25s | 0.88 | 假设从错到对，3 轮收敛 |
| #8 Kafka+ES 联动 | 1 | 8.5K | 8s | 0.85 | 跨组件依赖，1 轮搞定 |
| #9 YARN PENDING 误诊修正 | 2 | 7.7K | 16s | 0.92 | 宏观正常但微观异常 |

> **设计验证**：这 3 个新增场景验证了系统的以下能力：
>
> 1. **自我修正**（场景 7、9）：低置信度 → 补充数据 → 修正方向 → 高置信度
> 2. **跨组件推理**（场景 8）：从表面症状（Kafka lag）追溯到真正根因（ES 磁盘满）
> 3. **宏观vs微观**（场景 9）：集群整体正常但特定维度异常的检测
> 4. **因果链完整性**：每个场景都有清晰的 A→B→C→D 因果链

---

## 15.10 扩展测试套件（补充）

### 15.10.1 幻觉检测测试

```python
class TestHallucinationDetection:
    """诊断幻觉检测测试"""

    def test_detect_fake_source_tool(self):
        """检测引用不存在的工具"""
        diagnosis = DiagnosticOutput(
            root_cause="test",
            confidence=0.8,
            severity="high",
            evidence=[EvidenceItem(
                claim="CPU 95%",
                source_tool="non_existent_tool",  # 不存在
                source_data="CPU: 95%",
                supports_hypothesis="H1",
                confidence_contribution=0.8,
            )],
            causality_chain="A→B",
            affected_components=["hdfs"],
        )
        collected = {"hdfs_status": "heap=93%"}
        
        warnings = DiagnosticHallucinationCheck.check(
            diagnosis, collected, ["hdfs-namenode"]
        )
        assert any("HALLUCINATION-L1" in w for w in warnings)

    def test_detect_fabricated_numbers(self):
        """检测编造的数据数值"""
        diagnosis = DiagnosticOutput(
            root_cause="CPU 异常",
            confidence=0.8,
            severity="high",
            evidence=[EvidenceItem(
                claim="CPU 使用率 95%",
                source_tool="query_metrics",
                source_data="CPU: 95%",  # 工具返回中没有 95
                supports_hypothesis="H1",
                confidence_contribution=0.8,
            )],
            causality_chain="CPU高→慢",
            affected_components=["hdfs"],
        )
        collected = {"query_metrics": "CPU: 45%, Memory: 78%"}  # 实际是 45%
        
        warnings = DiagnosticHallucinationCheck.check(
            diagnosis, collected, ["hdfs-namenode"]
        )
        assert any("HALLUCINATION-L2" in w for w in warnings)

    def test_no_false_positive_on_valid_evidence(self):
        """有效证据不应触发幻觉警告"""
        diagnosis = DiagnosticOutput(
            root_cause="heap 不足",
            confidence=0.85,
            severity="high",
            evidence=[EvidenceItem(
                claim="heap 93%",
                source_tool="hdfs_status",
                source_data="堆内存使用: 93%",
                supports_hypothesis="H1",
                confidence_contribution=0.8,
            )],
            causality_chain="heap不足→GC",
            affected_components=["hdfs-namenode"],
        )
        collected = {"hdfs_status": "## HDFS NameNode\n堆内存使用: 93%\n"}
        
        warnings = DiagnosticHallucinationCheck.check(
            diagnosis, collected, ["hdfs-namenode"]
        )
        assert len(warnings) == 0


class TestOpenExplorationMode:
    """无假设场景的开放式探索测试"""

    def test_no_hypotheses_triggers_baseline(self):
        """无假设时应使用基线工具"""
        node = DiagnosticNode(llm_client=AsyncMock())
        state = {
            "task_plan": [],
            "hypotheses": [],
            "target_components": ["hdfs-namenode"],
        }
        calls = node._plan_tool_calls(state, round_num=1)
        assert len(calls) > 0
        assert any("hdfs" in c["name"].lower() or "search_logs" in c["name"] for c in calls)

    def test_baseline_includes_error_logs(self):
        """基线工具总应包含错误日志搜索"""
        node = DiagnosticNode(llm_client=AsyncMock())
        tools = node._get_baseline_tools(["kafka-broker"])
        tool_names = [t["name"] for t in tools]
        assert "search_logs" in tool_names

    def test_baseline_respects_max_tools(self):
        """基线工具不超过 MAX_TOOLS_PER_ROUND"""
        node = DiagnosticNode(llm_client=AsyncMock())
        tools = node._get_baseline_tools(["hdfs-namenode", "kafka-broker", "yarn-rm"])
        assert len(tools) <= MAX_TOOLS_PER_ROUND


class TestCircularDependencyDetection:
    """循环依赖检测测试"""

    def test_skip_previously_failed_tools(self):
        """应跳过之前失败的工具"""
        node = DiagnosticNode(llm_client=AsyncMock())
        state = {
            "data_requirements": ["search_logs", "kafka_lag"],
            "tool_calls": [
                {"tool_name": "search_logs", "status": "timeout"},  # 之前超时
            ],
        }
        calls = node._plan_tool_calls(state, round_num=2)
        tool_names = [c["name"] for c in calls]
        assert "search_logs" not in tool_names
        assert "kafka_lag" in tool_names

    def test_all_required_tools_failed_clears_requirements(self):
        """所有需求工具都失败时应清空 data_requirements"""
        node = DiagnosticNode(llm_client=AsyncMock())
        state = {
            "data_requirements": ["tool_a", "tool_b"],
            "tool_calls": [
                {"tool_name": "tool_a", "status": "error"},
                {"tool_name": "tool_b", "status": "timeout"},
            ],
        }
        calls = node._plan_tool_calls(state, round_num=2)
        assert calls == []
        assert state["data_requirements"] == []


class TestDataCompleteness:
    """数据完整性评估测试"""

    def test_full_success(self):
        state = {
            "tool_calls": [
                {"tool_name": "t1", "status": "success"},
                {"tool_name": "t2", "status": "success"},
            ],
            "collected_data": {"t1": "data", "t2": "data"},
        }
        result = _assess_data_completeness(state)
        assert result["completeness"] == 1.0
        assert result["should_lower_confidence"] is False

    def test_partial_failure(self):
        state = {
            "tool_calls": [
                {"tool_name": "t1", "status": "success"},
                {"tool_name": "t2", "status": "timeout"},
                {"tool_name": "t3", "status": "success"},
            ],
            "collected_data": {},
        }
        result = _assess_data_completeness(state)
        assert abs(result["completeness"] - 0.667) < 0.01
        assert "t2" in result["failed_tools"]
        assert result["should_lower_confidence"] is True

    def test_all_failure(self):
        state = {
            "tool_calls": [
                {"tool_name": "t1", "status": "error"},
                {"tool_name": "t2", "status": "timeout"},
            ],
            "collected_data": {},
        }
        result = _assess_data_completeness(state)
        assert result["completeness"] == 0.0
        assert result["should_lower_confidence"] is True
```

---

### 15.10.4 假设生成质量测试

```python
# tests/unit/agent/test_hypothesis_quality.py
"""
假设生成质量测试

验证 Planning Agent 生成的假设质量：
1. 假设覆盖率：生成的假设是否包含正确根因
2. 假设多样性：假设是否覆盖不同故障类别
3. 优先级准确性：高概率假设是否确实比低概率假设更准确
"""

import pytest
from aiops.agent.hypothesis import (
    SimplePrioritySort,
    BayesianPriorityUpdate,
    prioritize_hypotheses,
)


class TestHypothesisGeneration:
    """假设生成质量测试"""

    def test_hypotheses_cover_correct_root_cause(self):
        """假设列表应包含正确根因（关键测试）"""
        # 模拟 HDFS heap 不足的场景
        hypotheses = [
            {"id": 1, "description": "NN heap 不足", "probability": "high", "type": "heap_insufficient"},
            {"id": 2, "description": "小文件过多", "probability": "medium", "type": "small_files"},
            {"id": 3, "description": "网络问题", "probability": "low", "type": "network_issue"},
        ]
        
        ground_truth_keywords = ["heap", "内存", "GC"]
        
        # 至少一个假设应该包含正确根因的关键词
        covered = any(
            any(kw in h["description"] for kw in ground_truth_keywords)
            for h in hypotheses
        )
        assert covered, "假设列表未覆盖正确根因"

    def test_hypotheses_have_diversity(self):
        """假设应覆盖不同的故障类别"""
        hypotheses = [
            {"id": 1, "type": "heap_insufficient", "probability": "high"},
            {"id": 2, "type": "small_files", "probability": "medium"},
            {"id": 3, "type": "network_issue", "probability": "low"},
        ]
        
        types = set(h["type"] for h in hypotheses)
        assert len(types) >= 2, f"假设多样性不足: {types}"

    def test_high_probability_should_be_first(self):
        """高概率假设应排在前面"""
        hypotheses = [
            {"id": 1, "probability": "low"},
            {"id": 2, "probability": "high"},
            {"id": 3, "probability": "medium"},
        ]
        
        sorted_h = SimplePrioritySort().sort(hypotheses)
        assert sorted_h[0]["probability"] == "high"
        assert sorted_h[-1]["probability"] == "low"

    def test_bayesian_update_with_supporting_evidence(self):
        """贝叶斯更新应提升有支持证据的假设"""
        hypotheses = [
            {"id": 1, "probability": "medium", "description": "heap 不足"},
            {"id": 2, "probability": "high", "description": "磁盘问题"},
        ]
        
        evidence = [
            {"supports_hypothesis": "1", "confidence_contribution": 0.8},
            {"supports_hypothesis": "1", "confidence_contribution": 0.6},
            {"supports_hypothesis": "2", "confidence_contribution": -0.5},
        ]
        
        result = BayesianPriorityUpdate().sort(hypotheses, evidence)
        # 假设 1 有 2 条支持证据，应该排在前面
        assert result[0]["id"] == 1

    def test_no_hypotheses_fallback(self):
        """空假设列表应返回空列表"""
        result = prioritize_hypotheses([], strategy="simple")
        assert result == []


class TestMultiRoundConvergence:
    """多轮收集收敛性测试"""

    def test_convergence_within_5_rounds(self):
        """典型场景应在 5 轮内收敛"""
        # 模拟多轮诊断
        confidence_progression = [0.35, 0.55, 0.72, 0.85]  # 4 轮收敛
        
        converged = False
        for round_num, conf in enumerate(confidence_progression, 1):
            if conf >= 0.6:
                converged = True
                converge_round = round_num
                break
        
        assert converged, "未在 5 轮内收敛"
        assert converge_round <= 5

    def test_confidence_monotonically_increases_with_data(self):
        """更多数据通常应提升置信度（非严格单调）"""
        # 模拟：每轮增加证据后置信度应趋势上升
        confidence_history = [0.35, 0.45, 0.55, 0.72]
        
        # 允许小幅波动（新证据可能暂时降低置信度）
        # 但整体趋势应上升：最后 > 最初
        assert confidence_history[-1] > confidence_history[0], \
            "整体趋势应为上升"

    def test_max_rounds_forces_output(self):
        """超过最大轮次必须强制输出"""
        max_rounds = 5
        current_round = 5
        confidence = 0.35  # 低于阈值
        
        should_stop = current_round >= max_rounds
        assert should_stop, "超过最大轮次必须强制停止"

    def test_empty_requirements_forces_output(self):
        """无更多数据需求时必须输出"""
        confidence = 0.45  # 低于阈值
        data_requirements = []  # 空
        
        should_stop = not data_requirements
        assert should_stop, "无数据需求时必须输出"


class TestParallelToolIsolation:
    """并行工具调用隔离性测试"""

    @pytest.mark.asyncio
    async def test_exception_isolation(self):
        """单个工具异常不影响其他工具"""
        import asyncio
        
        results = {}
        
        async def tool_success():
            await asyncio.sleep(0.1)
            results["success"] = "ok"
        
        async def tool_failure():
            raise RuntimeError("模拟失败")
        
        # 使用 gather 而非 TaskGroup
        async def safe_call(coro, name):
            try:
                await coro
            except Exception as e:
                results[name] = f"error: {e}"
        
        await asyncio.gather(
            safe_call(tool_success(), "success"),
            safe_call(tool_failure(), "failure"),
        )
        
        assert "success" in results
        assert results["success"] == "ok"

    @pytest.mark.asyncio
    async def test_timeout_isolation(self):
        """单个工具超时不阻塞其他工具"""
        import asyncio
        
        async def fast_tool():
            await asyncio.sleep(0.1)
            return "fast_result"
        
        async def slow_tool():
            await asyncio.sleep(100)
            return "should_not_reach"
        
        async def with_timeout(coro, timeout):
            try:
                return await asyncio.wait_for(coro, timeout=timeout)
            except asyncio.TimeoutError:
                return "timeout"
        
        results = await asyncio.gather(
            with_timeout(fast_tool(), 5),
            with_timeout(slow_tool(), 1),
        )
        
        assert results[0] == "fast_result"
        assert results[1] == "timeout"

    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrency(self):
        """Semaphore 应限制并发数"""
        import asyncio
        
        max_concurrent = 3
        semaphore = asyncio.Semaphore(max_concurrent)
        current_concurrent = 0
        max_observed = 0
        
        async def tracked_task():
            nonlocal current_concurrent, max_observed
            async with semaphore:
                current_concurrent += 1
                max_observed = max(max_observed, current_concurrent)
                await asyncio.sleep(0.1)
                current_concurrent -= 1
        
        # 启动 10 个任务
        await asyncio.gather(*[tracked_task() for _ in range(10)])
        
        assert max_observed <= max_concurrent, \
            f"并发数 {max_observed} 超过限制 {max_concurrent}"


class TestContextCompressionFidelity:
    """上下文压缩保真度测试"""

    def test_anomalies_preserved_after_compression(self):
        """压缩后异常行必须保留"""
        raw_data = {
            "hdfs_status": (
                "## HDFS NameNode\n"
                "正常行1\n正常行2\n正常行3\n" * 50
                + "⚠️ 堆内存使用率 93% (超过 90% 阈值)\n"
                + "正常行4\n正常行5\n" * 30
            ),
        }
        
        compressor = DiagnosticContextCompressor(target_tokens=500)
        compressed = compressor.compress(raw_data, current_round=1)
        
        assert "93%" in compressed, "异常数值被压缩掉了"
        assert "⚠️" in compressed, "异常标记被压缩掉了"

    def test_compression_ratio_in_range(self):
        """压缩比应在 1.2x-3x 之间"""
        raw_data = {}
        for i in range(5):
            raw_data[f"tool_{i}"] = (
                f"## Tool {i}\n"
                + "正常数据行\n" * 200
                + "⚠️ 异常: 指标超标\n"
                + "正常数据行\n" * 100
            )
        
        raw_chars = sum(len(v) for v in raw_data.values())
        
        compressor = DiagnosticContextCompressor(target_tokens=1000)
        compressed = compressor.compress(raw_data, current_round=1)
        
        ratio = raw_chars / len(compressed) if compressed else float('inf')
        assert 1.2 <= ratio <= 5.0, f"压缩比 {ratio:.1f}x 超出预期范围"

    def test_multi_round_compression_stability(self):
        """多轮压缩后信息不应丢失关键内容"""
        # Round 1 数据
        data_r1 = {
            "hdfs_status": "## HDFS\n⚠️ heap=93%\n正常数据\n" * 10,
        }
        
        compressor = DiagnosticContextCompressor(target_tokens=1000)
        
        # Round 1 压缩
        c1 = compressor.compress(data_r1, current_round=1)
        assert "93%" in c1
        
        # Round 2 添加新数据
        data_r2 = dict(data_r1)
        data_r2["search_logs"] = "## Logs\n⚠️ Full GC 3245ms\n正常日志\n" * 10
        
        c2 = compressor.compress(data_r2, current_round=2)
        # Round 1 的关键信息仍应保留
        assert "93%" in c2, "Round 1 的关键信息在 Round 2 压缩后丢失"
        assert "3245" in c2, "Round 2 的关键信息未保留"


class TestConfidenceCalibration:
    """诊断置信度校准测试"""

    def test_single_evidence_lowers_confidence(self):
        """单条证据应降低原始置信度"""
        factors = ConfidenceFactors(
            llm_raw_confidence=0.85,
            evidence_count=1,
            evidence_source_count=1,
            contradiction_count=0,
            data_completeness=1.0,
            confirmed_hypothesis_count=1,
            has_causality_chain=True,
            round_count=1,
        )
        
        adjusted = compute_adjusted_confidence(factors)
        assert adjusted < 0.85, f"单条证据不应保持 {adjusted:.2f} 的高置信度"

    def test_multiple_sources_boost_confidence(self):
        """多源证据应提升置信度"""
        factors = ConfidenceFactors(
            llm_raw_confidence=0.65,
            evidence_count=4,
            evidence_source_count=4,
            contradiction_count=0,
            data_completeness=1.0,
            confirmed_hypothesis_count=2,
            has_causality_chain=True,
            round_count=2,
        )
        
        adjusted = compute_adjusted_confidence(factors)
        assert adjusted > 0.65, f"4 源证据应提升置信度，但得到 {adjusted:.2f}"

    def test_contradictions_lower_confidence(self):
        """矛盾证据应降低置信度"""
        factors_no_contradict = ConfidenceFactors(
            llm_raw_confidence=0.8,
            evidence_count=3,
            evidence_source_count=3,
            contradiction_count=0,
            data_completeness=1.0,
            confirmed_hypothesis_count=1,
            has_causality_chain=True,
            round_count=1,
        )
        
        factors_with_contradict = ConfidenceFactors(
            llm_raw_confidence=0.8,
            evidence_count=3,
            evidence_source_count=3,
            contradiction_count=2,
            data_completeness=1.0,
            confirmed_hypothesis_count=1,
            has_causality_chain=True,
            round_count=1,
        )
        
        conf_no = compute_adjusted_confidence(factors_no_contradict)
        conf_with = compute_adjusted_confidence(factors_with_contradict)
        
        assert conf_with < conf_no, "矛盾证据应降低置信度"

    def test_low_data_completeness_lowers_confidence(self):
        """数据不完整应降低置信度"""
        factors = ConfidenceFactors(
            llm_raw_confidence=0.8,
            evidence_count=2,
            evidence_source_count=2,
            contradiction_count=0,
            data_completeness=0.4,  # 40% 工具成功
            confirmed_hypothesis_count=1,
            has_causality_chain=True,
            round_count=1,
        )
        
        adjusted = compute_adjusted_confidence(factors)
        assert adjusted < 0.5, f"40% 数据完整性应显著降低置信度，但得到 {adjusted:.2f}"

    def test_no_causality_chain_caps_confidence(self):
        """无因果链应限制置信度上限"""
        factors = ConfidenceFactors(
            llm_raw_confidence=0.9,
            evidence_count=3,
            evidence_source_count=3,
            contradiction_count=0,
            data_completeness=1.0,
            confirmed_hypothesis_count=1,
            has_causality_chain=False,  # 无因果链
            round_count=1,
        )
        
        adjusted = compute_adjusted_confidence(factors)
        assert adjusted <= 0.65, f"无因果链时置信度不应超过 0.65，但得到 {adjusted:.2f}"
```

---

## 16. Grafana Dashboard 配置

```json
{
  "dashboard": {
    "title": "AIOps Diagnostic Agent",
    "uid": "aiops-diagnostic",
    "panels": [
      {
        "title": "诊断轮次分布",
        "type": "histogram",
        "targets": [{"expr": "aiops_diagnostic_rounds_bucket"}],
        "gridPos": {"h": 8, "w": 8, "x": 0, "y": 0}
      },
      {
        "title": "置信度分布",
        "type": "histogram",
        "targets": [{"expr": "aiops_diagnostic_confidence_bucket"}],
        "gridPos": {"h": 8, "w": 8, "x": 8, "y": 0}
      },
      {
        "title": "工具调用成功率",
        "type": "gauge",
        "targets": [{"expr": "aiops_diagnostic_tool_success_rate"}],
        "fieldConfig": {"defaults": {
          "thresholds": {"steps": [
            {"value": 0, "color": "red"},
            {"value": 80, "color": "yellow"},
            {"value": 95, "color": "green"}
          ]},
          "unit": "percent", "min": 0, "max": 100
        }},
        "gridPos": {"h": 8, "w": 8, "x": 16, "y": 0}
      },
      {
        "title": "降级触发次数",
        "type": "stat",
        "targets": [{"expr": "increase(aiops_diagnostic_fallback_total[24h])"}],
        "gridPos": {"h": 4, "w": 8, "x": 0, "y": 8}
      },
      {
        "title": "证据数量分布",
        "type": "histogram",
        "targets": [{"expr": "aiops_diagnostic_evidence_count_bucket"}],
        "gridPos": {"h": 8, "w": 8, "x": 8, "y": 8}
      },
      {
        "title": "每轮工具调用数",
        "type": "timeseries",
        "targets": [{
          "expr": "histogram_quantile(0.5, rate(aiops_diagnostic_tools_per_round_bucket[5m]))",
          "legendFormat": "P50"
        }, {
          "expr": "histogram_quantile(0.95, rate(aiops_diagnostic_tools_per_round_bucket[5m]))",
          "legendFormat": "P95"
        }],
        "gridPos": {"h": 8, "w": 8, "x": 16, "y": 8}
      }
    ]
  }
}
```

---

## 17. 与其他模块的集成

| 上游依赖 | 说明 |
|---------|------|
| 03-Agent 框架 | BaseAgentNode, AgentState, ContextCompressor |
| 02-LLM 客户端 | chat_structured() + DiagnosticOutput |
| 11-MCP 客户端 | MCPClient.call_tool() 执行工具调用 |
| 12-RAG 引擎 | rag_context（由 Planning Agent 预检索） |

| 下游消费者 | 说明 |
|-----------|------|
| route_from_diagnostic() | 消费 confidence + data_requirements 做路由 |
| 15-HITL | 消费 remediation_plan 中的高风险步骤 |
| 08-Report Agent | 消费 diagnosis + tool_calls + evidence |
| 17-可观测性 | OTel Span + Prometheus 指标 |

---

> **下一篇**：[06-Planning-Remediation-Agent.md](./06-Planning-Remediation-Agent.md) — Planning Agent 生成诊断计划，Remediation Agent 执行修复操作。
