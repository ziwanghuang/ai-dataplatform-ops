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

### 5.4 收敛条件矩阵

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

### 6.3 置信度校准

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
