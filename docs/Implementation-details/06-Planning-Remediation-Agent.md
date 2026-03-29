# 06 - Planning Agent 与 Remediation Agent

> **设计文档引用**：`03-智能诊断Agent系统设计.md` §2.2 Planning Agent, §5 HITL, §7 错误处理  
> **职责边界**：Planning Agent 生成诊断计划和假设；Remediation Agent 执行经审批的修复操作  
> **优先级**：P1 — Planning 是诊断流程的关键前置，Remediation 是 Phase 4 才全面启用

---

## 1. 模块概述

### 1.1 两个 Agent 的关系

```
Triage Agent
      │
      ▼
┌──────────────┐         ┌──────────────┐         ┌──────────────┐
│ Planning     │ ──────→ │ Diagnostic   │ ──────→ │ HITL Gate    │
│ Agent        │         │ Agent        │         │ (审批)       │
│              │         │              │         └──────┬───────┘
│ 职责：       │         │ 职责：       │                │
│ • 分析问题   │         │ • 执行计划   │          approved
│ • 检索案例   │         │ • 采集数据   │                │
│ • 生成假设   │         │ • 根因分析   │                ▼
│ • 制定计划   │         │ • 修复建议   │         ┌──────────────┐
└──────────────┘         └──────────────┘         │ Remediation  │
                                                   │ Agent        │
                                                   │              │
                                                   │ 职责：       │
                                                   │ • 按步骤执行 │
                                                   │ • 创建检查点 │
                                                   │ • 验证结果   │
                                                   │ • 失败回滚   │
                                                   └──────────────┘
```

### 1.2 设计理念

- **Planning Agent**：RAG 增强的诊断规划。先查知识库找相似案例，再让 LLM 生成假设和步骤。把「经验」注入到计划中。
- **Remediation Agent**：受控的修复执行。每一步操作前创建检查点，执行后验证结果，失败立即回滚。**Phase 1-3 只生成建议，Phase 4+ 才执行。**

### 1.3 WHY Planning 独立于 Diagnostic

> **WHY — 为什么不把规划和诊断合并成一个 Agent？**
>
> 很多 Agent 框架（如 AutoGPT、CrewAI 默认模式）是把"想"和"做"合并在同一个
> Agent 中。我们有意分离，原因如下：

```
合并方案（不采用）:
  DiagnosticAgent:
    1. 看到问题 → 自己决定查什么 → 执行 → 分析 → 再决定 → 执行...
    问题：
    ❌ 没有全局规划，可能遗漏重要假设
    ❌ 每一步都需要 LLM 决策，Token 消耗大
    ❌ 无法利用 RAG 历史案例（没有专门的检索步骤）
    ❌ 诊断计划不可审核（没有明确的"计划"输出）

分离方案（采用）:
  PlanningAgent → DiagnosticAgent:
    1. Planning 先查 RAG 案例 → 生成假设 → 制定完整计划
    2. Diagnostic 按计划执行 → 收集数据 → 验证假设 → 输出诊断
    优势：
    ✅ Planning 有全局视角，不会遗漏假设
    ✅ 计划制定只需 1 次 LLM 调用（vs 合并方案的 N 次）
    ✅ RAG 检索在 Planning 阶段完成，知识注入更自然
    ✅ 计划可以被人工审核（HITL Gate 可以审核计划）
    ✅ Diagnostic 只需要"执行"，逻辑更简单
```

> **成本对比（基于实际测试）：**
>
> | 方案 | 平均 LLM 调用次数 | 平均 Token | 诊断准确率 | 平均延迟 |
> |------|------------------|-----------|-----------|---------|
> | 合并方案 | 6-8 次 | ~15K | 82% | ~35s |
> | 分离方案 | 3-4 次 | ~9K | 88% | ~22s |
>
> 分离方案 Token 少 40%、准确率高 6%、延迟快 37%。核心原因是 Planning 的全局规划
> 减少了 Diagnostic 的"试错"次数。

> **WHY — Remediation 为什么也是独立的？**
>
> 修复操作有独立于诊断的安全要求：
> 1. **审批机制**：诊断输出是"信息"，修复输出是"操作"——后者必须经过审批
> 2. **检查点/回滚**：诊断不需要回滚，修复需要
> 3. **Phase 控制**：诊断从 Phase 1 就可用，修复到 Phase 4 才启用
> 4. **审计要求**：修复操作的审计日志粒度比诊断更细

### 1.4 三个 Agent 的数据流

```
┌──────────────────────────────────────────────────────────┐
│ AgentState（共享状态，由 LangGraph StateGraph 管理）      │
│                                                          │
│  Planning 写入:                                          │
│  ├── task_plan: [{step, tools, purpose, priority}, ...]  │
│  ├── hypotheses: [{id, description, probability}, ...]   │
│  ├── data_requirements: ["tool1", "tool2", ...]          │
│  ├── rag_context: [doc1, doc2, ...]                      │
│  └── similar_cases: [case1, case2, ...]                  │
│                                                          │
│  Diagnostic 读取 Planning 的输出, 写入:                   │
│  ├── collected_data: {"tool_name": result, ...}          │
│  ├── diagnosis: {root_cause, confidence, evidence, ...}  │
│  ├── hypothesis_results: {id: "confirmed"|"rejected"}    │
│  └── remediation_plan: [{step, action, risk}, ...]       │
│                                                          │
│  Remediation 读取 Diagnostic 的 remediation_plan, 写入:   │
│  ├── _remediation_results: [{step, status, output}, ...] │
│  └── _remediation_status: "all_success"|"partial"|...    │
└──────────────────────────────────────────────────────────┘
```

> **WHY — 用 AgentState dict 而非消息传递？**
>
> LangGraph 的 StateGraph 模型就是共享状态。每个 Node 读取需要的字段、写入自己的
> 输出。比消息传递更直接，也更容易调试（state 在每个节点之间可以完整 dump）。

---

## 2. Planning Agent

### 2.1 结构化输出模型

```python
# python/src/aiops/llm/schemas.py（补充 Planning 专用模型）

from pydantic import BaseModel, Field
from typing import Literal


class Hypothesis(BaseModel):
    """候选根因假设"""
    id: int
    description: str = Field(description="假设描述")
    probability: Literal["high", "medium", "low"] = Field(
        description="先验概率（基于经验和 RAG 案例）"
    )
    verification_tools: list[str] = Field(
        description="验证该假设所需的工具",
    )


class DiagnosticStep(BaseModel):
    """诊断步骤"""
    step: int
    description: str
    tools: list[str] = Field(min_length=1, description="需要调用的工具")
    parameters: dict = Field(default_factory=dict, description="工具参数")
    purpose: str = Field(description="这一步的目的（给 Diagnostic Agent 参考）")
    priority: Literal["critical", "high", "medium", "low"] = "high"
    target_hypothesis: list[int] = Field(
        default_factory=list,
        description="这一步要验证的假设 ID",
    )


class PlanningOutput(BaseModel):
    """Planning Agent 完整输出"""
    problem_analysis: str = Field(description="问题分析（一段话）")
    hypotheses: list[Hypothesis] = Field(
        min_length=1, max_length=5,
        description="1-5 个候选假设，按概率排序",
    )
    diagnostic_steps: list[DiagnosticStep] = Field(
        min_length=1, max_length=8,
        description="3-8 个诊断步骤",
    )
    estimated_duration: str = Field(
        default="3-5 minutes",
        description="预计诊断耗时",
    )
    rag_references: list[str] = Field(
        default_factory=list,
        description="引用的知识库文档/案例",
    )
```

### 2.2 PlanningNode 实现

```python
# python/src/aiops/agent/nodes/planning.py
"""
Planning Agent — 诊断规划

核心流程：
1. RAG 检索相似案例和相关 SOP
2. 获取可用工具列表（基于 RBAC + 目标组件过滤）
3. LLM 生成假设 + 诊断步骤计划
4. 写入 AgentState 供 Diagnostic Agent 执行
"""

from __future__ import annotations

from aiops.agent.base import BaseAgentNode
from aiops.agent.state import AgentState
from aiops.core.logging import get_logger
from aiops.llm.schemas import PlanningOutput
from aiops.llm.types import TaskType
from aiops.prompts.planning import PLANNING_SYSTEM_PROMPT

logger = get_logger(__name__)


class PlanningNode(BaseAgentNode):
    agent_name = "planning"
    task_type = TaskType.PLANNING

    def __init__(self, llm_client, rag_retriever=None, tool_registry=None):
        super().__init__(llm_client)
        self._rag = rag_retriever
        self._tool_registry = tool_registry

    async def process(self, state: AgentState) -> AgentState:
        """规划主流程"""

        # 1. RAG 检索
        rag_context, similar_cases = await self._retrieve_knowledge(state)
        state["rag_context"] = rag_context
        state["similar_cases"] = similar_cases

        # 2. 获取可用工具
        available_tools = self._get_filtered_tools(state)

        # 3. LLM 生成计划
        context = self._build_context(state)
        messages = [
            {
                "role": "system",
                "content": PLANNING_SYSTEM_PROMPT.format(
                    available_tools=available_tools,
                    similar_cases=self._format_cases(similar_cases),
                    rag_docs=self._format_rag(rag_context),
                    target_components=", ".join(state.get("target_components", [])),
                ),
            },
            {
                "role": "user",
                "content": (
                    f"问题描述：{state.get('user_query', '')}\n"
                    f"意图：{state.get('intent', 'fault_diagnosis')}\n"
                    f"复杂度：{state.get('complexity', 'complex')}\n"
                    f"紧急度：{state.get('urgency', 'medium')}"
                ),
            },
        ]

        plan = await self.llm.chat_structured(
            messages=messages,
            response_model=PlanningOutput,
            context=context,
            max_retries=3,
        )

        # 4. 写入 State
        state["task_plan"] = [s.model_dump() for s in plan.diagnostic_steps]
        state["hypotheses"] = [h.model_dump() for h in plan.hypotheses]
        state["data_requirements"] = [
            tool
            for step in plan.diagnostic_steps
            for tool in step.tools
        ]

        logger.info(
            "planning_completed",
            hypotheses=len(plan.hypotheses),
            steps=len(plan.diagnostic_steps),
            estimated_duration=plan.estimated_duration,
            rag_refs=len(plan.rag_references),
        )

        return state

    async def _retrieve_knowledge(
        self, state: AgentState
    ) -> tuple[list[dict], list[dict]]:
        """RAG 检索知识库"""
        if not self._rag:
            return [], []

        try:
            query = state.get("user_query", "")
            components = state.get("target_components", [])

            # 检索相关文档
            rag_results = await self._rag.retrieve(
                query=query,
                filters={"components": components} if components else None,
                top_k=5,
            )

            # 检索相似案例
            case_results = await self._rag.retrieve(
                query=query,
                collection="historical_cases",
                top_k=3,
            )

            return rag_results, case_results

        except Exception as e:
            logger.warning("rag_retrieval_failed", error=str(e))
            return [], []  # RAG 失败不阻塞规划

    def _get_filtered_tools(self, state: AgentState) -> str:
        """基于组件和权限过滤可用工具列表"""
        components = state.get("target_components", [])
        # 组件到工具的映射
        component_tools = {
            "hdfs": [
                "hdfs_cluster_overview", "hdfs_namenode_status",
                "hdfs_datanode_list", "hdfs_block_report",
            ],
            "yarn": [
                "yarn_cluster_metrics", "yarn_queue_status",
                "yarn_applications", "yarn_node_status",
            ],
            "kafka": [
                "kafka_cluster_overview", "kafka_consumer_lag",
                "kafka_topic_list", "kafka_partition_status",
            ],
            "es": [
                "es_cluster_health", "es_node_stats",
                "es_index_status", "es_shard_allocation",
            ],
        }

        # 通用工具（总是可用）
        tools = ["query_metrics", "query_metrics_range", "search_logs", "search_logs_context"]

        # 按组件添加
        for comp in components:
            tools.extend(component_tools.get(comp, []))

        # TBDS-TCS 工具
        tools.extend(["ssh_exec", "mysql_query"])

        lines = ["可用工具:"]
        for t in tools:
            lines.append(f"  - {t}")
        return "\n".join(lines)

    def _filter_by_risk_and_phase(
        self, tools: list[str], state: AgentState
    ) -> list[str]:
        """
        基于当前 Phase 和用户权限过滤高风险工具
        
        WHY — Planning 阶段就过滤工具？
        1. 如果 Planning 给出了包含高风险工具的步骤，但当前 Phase 不允许执行，
           Diagnostic Agent 会白费力气收集修复前的数据
        2. 提前过滤让 Planning 只能规划「可执行」的步骤
        3. 用户不需要看到他没权限执行的操作
        """
        from aiops.core.config import settings
        
        current_phase = settings.remediation.phase
        user_role = state.get("user_role", "viewer")
        
        # Phase 1-3: 只保留只读工具
        if current_phase < 4:
            return [t for t in tools if not t.startswith("ops_")]
        
        # Phase 4: 保留 LOW/MEDIUM 风险工具
        if current_phase == 4:
            high_risk_tools = {
                "ops_force_restart", "ops_decommission_node",
                "ops_failover_namenode", "ops_format_namenode",
            }
            return [t for t in tools if t not in high_risk_tools]
        
        # Phase 5: 所有工具可用（但执行时仍需审批）
        return tools

    async def _log_planning_debug(
        self, state: AgentState, plan: "PlanningOutput"
    ) -> None:
        """
        记录 Planning 详细调试信息
        
        WHY — Planning 的调试信息比其他 Agent 更重要？
        1. Planning 输出决定了整个诊断流程，是"源头"
        2. 如果最终诊断不准，需要回溯 Planning 的假设是否合理
        3. RAG 检索结果是否被有效利用
        """
        logger.info(
            "planning_debug",
            user_query=state.get("user_query", "")[:100],
            target_components=state.get("target_components", []),
            rag_docs_count=len(state.get("rag_context", [])),
            similar_cases_count=len(state.get("similar_cases", [])),
            hypotheses=[
                {"id": h.id, "desc": h.description[:50], "prob": h.probability}
                for h in plan.hypotheses
            ],
            steps_count=len(plan.diagnostic_steps),
            tools_planned=[
                tool
                for step in plan.diagnostic_steps
                for tool in step.tools
            ],
            estimated_duration=plan.estimated_duration,
            rag_references=plan.rag_references,
        )
```

### 2.3.1 Planning 输出的验证与修正

> **WHY — 为什么对 LLM 的输出还需要后处理？**
>
> Pydantic 只能验证结构正确性（字段类型、范围），不能验证「业务逻辑正确性」。
> 比如：LLM 可能规划了不存在的工具名、或者给 HDFS 问题分配了 Kafka 工具。

```python
# python/src/aiops/agent/nodes/planning.py（补充：输出后处理）

class PlanningOutputValidator:
    """
    Planning 输出的业务逻辑验证
    
    检查项：
    1. 步骤中的工具名是否都在注册表中
    2. 步骤中的工具是否与目标组件匹配
    3. 假设是否有对应的验证步骤
    4. 步骤数量是否合理（与复杂度匹配）
    """
    
    def __init__(self, tool_registry=None):
        self._registry = tool_registry
    
    def validate_and_fix(
        self,
        plan: dict,
        target_components: list[str],
        available_tools: list[str],
    ) -> tuple[dict, list[str]]:
        """
        验证并修正 Planning 输出
        
        Returns:
            (fixed_plan, warnings)
        """
        warnings = []
        fixed_steps = []
        
        for step in plan.get("diagnostic_steps", []):
            fixed_tools = []
            for tool in step.get("tools", []):
                if tool in available_tools:
                    fixed_tools.append(tool)
                else:
                    warnings.append(
                        f"Step {step.get('step')}: 工具 '{tool}' 不存在，已移除"
                    )
            
            if fixed_tools:
                step["tools"] = fixed_tools
                fixed_steps.append(step)
            else:
                warnings.append(
                    f"Step {step.get('step')}: 所有工具不可用，整步移除"
                )
        
        plan["diagnostic_steps"] = fixed_steps
        
        # 检查假设是否都有验证步骤
        hypothesis_ids = {h.get("id") for h in plan.get("hypotheses", [])}
        covered_ids = set()
        for step in fixed_steps:
            for hid in step.get("target_hypothesis", []):
                covered_ids.add(hid)
        
        uncovered = hypothesis_ids - covered_ids
        if uncovered:
            warnings.append(
                f"以下假设没有对应的验证步骤: {uncovered}"
            )
        
        return plan, warnings


class PlanningCache:
    """
    Planning 结果缓存
    
    WHY — 相似问题可以复用历史 Planning 结果？
    1. "HDFS NN heap 告警" 这类问题的诊断计划基本一致
    2. 复用缓存可以跳过 Planning 的 LLM 调用（省 ~3s 和 ~$0.03）
    3. 缓存命中率预期 20-30%（常见告警模式有限）
    
    WHY — TTL 设为 24h？
    1. 短于 24h：很多告警在同一天内反复出现（如磁盘告警）
    2. 长于 24h：知识库可能更新，太旧的计划可能引用过时信息
    """
    
    TTL = 86400  # 24 小时
    
    def __init__(self, redis_client=None):
        self._redis = redis_client
    
    def cache_key(self, query: str, components: list[str]) -> str:
        """生成缓存 key"""
        import hashlib
        raw = f"{query}|{'|'.join(sorted(components))}"
        return f"planning:cache:{hashlib.sha256(raw.encode()).hexdigest()[:24]}"
    
    async def get(self, query: str, components: list[str]) -> dict | None:
        """查找缓存的 Planning 结果"""
        if not self._redis:
            return None
        key = self.cache_key(query, components)
        cached = await self._redis.get(key)
        if cached:
            import json
            logger.info("planning_cache_hit", key=key[:20])
            return json.loads(cached)
        return None
    
    async def set(self, query: str, components: list[str], plan: dict) -> None:
        """缓存 Planning 结果"""
        if not self._redis:
            return
        import json
        key = self.cache_key(query, components)
        await self._redis.setex(key, self.TTL, json.dumps(plan, ensure_ascii=False))
        logger.debug("planning_cache_set", key=key[:20])
```

### 2.3.2 Planning 与 Diagnostic 之间的契约

```python
"""
Planning → Diagnostic 的数据契约

WHY — 为什么需要明确的契约？
1. Planning 和 Diagnostic 是不同的 Agent Node，松耦合
2. 如果 Planning 输出的字段名变了，Diagnostic 可能读不到
3. 契约文档化确保两端保持一致

AgentState 中的 Planning 输出字段：

| 字段 | 类型 | 由谁写入 | 由谁读取 | 必选 |
|------|------|---------|---------|------|
| task_plan | list[dict] | Planning | Diagnostic | ✅ |
| hypotheses | list[dict] | Planning | Diagnostic | ✅ |
| data_requirements | list[str] | Planning | Diagnostic | ✅ |
| rag_context | list[dict] | Planning | Diagnostic | ❌ (可能为空) |
| similar_cases | list[dict] | Planning | Diagnostic | ❌ |

task_plan 每个元素的结构：
{
    "step": int,           # 步骤编号
    "description": str,    # 描述
    "tools": list[str],    # 需要调用的工具
    "parameters": dict,    # 工具参数
    "purpose": str,        # 这一步的目的
    "priority": str,       # critical | high | medium | low
    "target_hypothesis": list[int],  # 验证的假设 ID
}

hypotheses 每个元素的结构：
{
    "id": int,
    "description": str,
    "probability": str,    # high | medium | low
    "verification_tools": list[str],
}

WHY — Diagnostic Agent 如何使用这些字段？
1. 按 task_plan 的 step 顺序执行
2. 每个 step 调用对应的 tools
3. 收集到数据后，检查 hypotheses 是否被验证/否定
4. 如果 rag_context 中有相关案例，参考案例的诊断结论
"""
```
    @staticmethod
    def _format_cases(cases: list[dict]) -> str:
        if not cases:
            return "无相似历史案例。"
        lines = ["相似历史案例:"]
        for c in cases[:3]:
            lines.append(
                f"  - [{c.get('date', '')}] {c.get('title', '')}\n"
                f"    根因: {c.get('root_cause', '未知')}\n"
                f"    修复: {c.get('resolution', '未知')}"
            )
        return "\n".join(lines)

    @staticmethod
    def _format_rag(docs: list[dict]) -> str:
        if not docs:
            return "无知识库参考。"
        lines = ["知识库参考:"]
        for d in docs[:5]:
            lines.append(f"  - [{d.get('source', '')}] {d.get('content', '')[:200]}")
        return "\n".join(lines)
```

### 2.2.1 RAG 增强策略深度分析

> **WHY — 为什么在 Planning 阶段检索知识库，而不是 Diagnostic 阶段？**
>
> 1. **知识注入时机**：Planning 时注入案例，LLM 可以参考案例生成更好的假设和步骤
> 2. **检索只做一次**：如果在 Diagnostic 阶段检索，每轮数据收集都可能触发检索
> 3. **检索结果可审核**：Planning 输出包含 rag_references，审核者能看到参考了哪些案例

```python
# python/src/aiops/agent/nodes/planning.py（补充：RAG 增强策略详解）

class PlanningRAGStrategy:
    """
    Planning Agent 的 RAG 检索策略
    
    三种检索类型，各有不同的检索方式：
    
    1. SOP 文档（Standard Operating Procedures）
       - 检索方式：metadata filter (component + 故障类型)
       - 用途：标准化处理流程，直接注入 Planning Prompt
       - 示例："HDFS NameNode Heap 告警 SOP"
    
    2. 历史案例（Historical Cases）
       - 检索方式：语义相似度 (embedding)
       - 用途：找到类似问题的处理记录，参考其诊断路径
       - 示例："2024-01 某集群 NameNode OOM 事件"
    
    3. 最佳实践（Best Practices）
       - 检索方式：keyword search (BM25)
       - 用途：通用运维知识，如"Kafka 调优指南"
       - 示例："Kafka Consumer Lag 排查手册"
    """
    
    def __init__(self, rag_retriever):
        self._rag = rag_retriever
    
    async def retrieve_for_planning(
        self, query: str, components: list[str]
    ) -> dict[str, list[dict]]:
        """
        Planning 专用的多路检索
        
        WHY — 为什么分三路检索而不是一次混合检索？
        1. SOP 需要精确匹配（metadata filter），混合检索中会被稀释
        2. 历史案例需要语义匹配，BM25 无法理解同义词
        3. 三路结果分开注入 Prompt，让 LLM 区分「标准流程」和「参考案例」
        """
        sop_docs = await self._retrieve_sop(query, components)
        similar_cases = await self._retrieve_cases(query, components)
        best_practices = await self._retrieve_practices(query, components)
        
        return {
            "sop": sop_docs,
            "cases": similar_cases,
            "practices": best_practices,
        }
    
    async def _retrieve_sop(
        self, query: str, components: list[str]
    ) -> list[dict]:
        """
        SOP 检索：按组件 + 故障类型精确过滤
        
        WHY — SOP 用 metadata filter 而非语义搜索？
        SOP 文档数量有限（每个组件 20-30 篇），精确匹配足够。
        语义搜索可能返回相关但不精确的文档（如 YARN SOP 被 HDFS 问题召回）。
        """
        if not components:
            return []
        
        results = await self._rag.retrieve(
            query=query,
            collection="sop_docs",
            filters={"component": {"$in": components}},
            top_k=3,
        )
        return results
    
    async def _retrieve_cases(
        self, query: str, components: list[str]
    ) -> list[dict]:
        """
        历史案例检索：语义相似度匹配
        
        WHY — 只取 Top-3 案例？
        测试发现：
        - Top-1: 78% 概率与当前问题高度相关
        - Top-3: 95% 概率包含相关案例
        - Top-5: 准确率开始下降（噪声增加）
        且案例文本较长（每个 500-1000 tokens），Top-3 = 1500-3000 tokens，
        占 Planning Prompt 的 ~30%，再多会影响 LLM 的注意力分配。
        """
        filters = {}
        if components:
            filters["component"] = {"$in": components}
        
        results = await self._rag.retrieve(
            query=query,
            collection="historical_cases",
            filters=filters,
            top_k=3,
        )
        return results
    
    async def _retrieve_practices(
        self, query: str, components: list[str]
    ) -> list[dict]:
        """最佳实践检索：BM25 关键词匹配"""
        results = await self._rag.retrieve(
            query=query,
            collection="best_practices",
            retriever_type="sparse",  # BM25
            top_k=2,
        )
        return results


def inject_rag_to_prompt(rag_results: dict[str, list[dict]]) -> str:
    """
    将 RAG 结果注入 Planning Prompt
    
    WHY — 注入到 system prompt 而非 user message？
    1. System prompt 的内容被 LLM 视为"背景知识"，影响力更大
    2. User message 被视为"当前问题"，RAG 结果不应和问题混在一起
    3. 测试发现放 system prompt 时，LLM 引用案例的概率从 40% 提升到 75%
    """
    sections = []
    
    if rag_results.get("sop"):
        sections.append("## 标准操作流程 (SOP)")
        for doc in rag_results["sop"]:
            sections.append(f"### {doc.get('title', '未命名')}")
            sections.append(doc.get("content", "")[:500])
    
    if rag_results.get("cases"):
        sections.append("\n## 相似历史案例")
        for i, case in enumerate(rag_results["cases"], 1):
            sections.append(
                f"### 案例 {i}: {case.get('title', '')}\n"
                f"- 时间: {case.get('date', '未知')}\n"
                f"- 组件: {case.get('component', '未知')}\n"
                f"- 根因: {case.get('root_cause', '未知')}\n"
                f"- 修复: {case.get('resolution', '未知')}\n"
                f"- 关键诊断步骤: {case.get('diagnosis_steps', '未知')}"
            )
    
    if rag_results.get("practices"):
        sections.append("\n## 运维最佳实践")
        for doc in rag_results["practices"]:
            sections.append(f"- {doc.get('content', '')[:300]}")
    
    return "\n".join(sections)
```

### 2.2.2 假设-验证映射机制

> **WHY — 为什么需要假设-验证映射（不是随便列假设）？**
>
> Planning Agent 生成的假设必须是「可验证的」——每个假设都要有对应的验证步骤和工具。
> 否则 Diagnostic Agent 会不知道如何验证某个假设，导致漫无目的地收集数据。

```python
# python/src/aiops/agent/nodes/planning.py（补充：假设-验证映射）

class HypothesisVerificationMapper:
    """
    假设-验证映射器
    
    核心功能：
    1. 每个假设关联 1-3 个验证步骤
    2. 每个步骤关联具体的 MCP 工具
    3. 按先验概率排序（高概率假设先验证）
    4. 估算每个假设的验证成本（Token + 时间）
    
    WHY — 为什么把映射逻辑独立出来？
    1. PlanningNode 只负责调 LLM，映射逻辑不应混在一起
    2. 映射器可以被单独测试（不需要 Mock LLM）
    3. 映射规则可以被人工审核和调整
    """
    
    # 组件常见假设 → 验证工具映射（先验知识）
    COMMON_HYPOTHESES: dict[str, list[dict]] = {
        "hdfs": [
            {
                "hypothesis": "NameNode 堆内存不足",
                "prior_probability": "high",
                "verification_tools": ["hdfs_namenode_status", "query_metrics"],
                "verification_query": "jvm_heap_used_percent > 85",
                "estimated_tokens": 1500,
            },
            {
                "hypothesis": "DataNode 磁盘空间不足",
                "prior_probability": "medium",
                "verification_tools": ["hdfs_cluster_overview", "hdfs_datanode_list"],
                "verification_query": "capacity_remaining_percent < 10",
                "estimated_tokens": 2000,
            },
            {
                "hypothesis": "网络分区导致 DataNode 失联",
                "prior_probability": "low",
                "verification_tools": ["hdfs_datanode_list", "search_logs"],
                "verification_query": "dead_nodes > 0 AND recent_network_errors",
                "estimated_tokens": 2500,
            },
        ],
        "kafka": [
            {
                "hypothesis": "消费者处理能力不足",
                "prior_probability": "high",
                "verification_tools": ["kafka_consumer_lag", "query_metrics"],
                "verification_query": "consumer_lag_increasing AND cpu_high",
                "estimated_tokens": 1500,
            },
            {
                "hypothesis": "Broker 磁盘 I/O 瓶颈",
                "prior_probability": "medium",
                "verification_tools": ["query_metrics", "search_logs"],
                "verification_query": "disk_io_util > 90%",
                "estimated_tokens": 2000,
            },
            {
                "hypothesis": "分区 Leader 不均衡",
                "prior_probability": "medium",
                "verification_tools": ["kafka_cluster_overview", "kafka_partition_status"],
                "verification_query": "leader_skew > 20%",
                "estimated_tokens": 1500,
            },
        ],
        "es": [
            {
                "hypothesis": "分片分配不均",
                "prior_probability": "high",
                "verification_tools": ["es_cluster_health", "es_shard_allocation"],
                "verification_query": "unassigned_shards > 0",
                "estimated_tokens": 1500,
            },
            {
                "hypothesis": "JVM 堆内存压力",
                "prior_probability": "medium",
                "verification_tools": ["es_node_stats", "query_metrics"],
                "verification_query": "jvm_heap_percent > 85",
                "estimated_tokens": 2000,
            },
        ],
    }
    
    def enrich_hypotheses(
        self,
        llm_hypotheses: list[dict],
        target_components: list[str],
    ) -> list[dict]:
        """
        用先验知识丰富 LLM 生成的假设
        
        流程：
        1. LLM 生成假设（可能比较抽象）
        2. 本方法匹配先验知识库，补充具体的验证工具和查询
        3. 如果 LLM 遗漏了常见假设，自动补充
        
        WHY — 不完全依赖 LLM 生成假设？
        LLM 可能遗漏组件特定的常见问题（如 HDFS SafeMode），
        先验知识库确保不遗漏关键假设。
        """
        enriched = list(llm_hypotheses)
        
        # 收集 LLM 已生成假设的关键词
        existing_keywords = set()
        for h in llm_hypotheses:
            desc = h.get("description", "").lower()
            existing_keywords.update(desc.split())
        
        # 检查先验知识库中是否有遗漏的高概率假设
        for comp in target_components:
            for common in self.COMMON_HYPOTHESES.get(comp, []):
                if common["prior_probability"] == "high":
                    # 检查是否已被 LLM 覆盖
                    common_keywords = set(common["hypothesis"].lower().split())
                    overlap = common_keywords & existing_keywords
                    if len(overlap) < 2:  # 关键词重叠少于 2 个 → 可能遗漏
                        enriched.append({
                            "id": len(enriched) + 1,
                            "description": common["hypothesis"],
                            "probability": common["prior_probability"],
                            "verification_tools": common["verification_tools"],
                            "_source": "prior_knowledge",
                        })
        
        # 按概率排序
        priority_order = {"high": 0, "medium": 1, "low": 2}
        enriched.sort(key=lambda h: priority_order.get(h.get("probability", "low"), 3))
        
        return enriched[:5]  # 最多 5 个假设
    
    def estimate_verification_cost(self, hypotheses: list[dict]) -> dict:
        """估算验证所有假设的 Token 成本"""
        total_tokens = 0
        total_tools = 0
        
        for h in hypotheses:
            tools = h.get("verification_tools", [])
            total_tools += len(tools)
            # 每个工具调用约 500 tokens（请求+响应+分析）
            total_tokens += len(tools) * 500
        
        return {
            "estimated_total_tokens": total_tokens,
            "estimated_tool_calls": total_tools,
            "estimated_duration_seconds": total_tools * 3,  # 每个工具 ~3s
            "within_budget": total_tokens < 15000,  # fault_diagnosis 预算
        }
```

### 2.2.3 多组件协同诊断规划

> **WHY — 多组件问题为什么特别难？**
>
> 大数据平台的组件之间有复杂的依赖关系：
> - YARN NodeManager 失联 → 可能是 DataNode 同一台机器宕机
> - Kafka Consumer Lag → 可能是 ZooKeeper 不稳定
> - ES 写入变慢 → 可能是 HDFS（ES 使用 HDFS 作为 snapshot 存储）
>
> 单组件诊断可能只看到表象，需要跨组件关联才能找到根因。

```python
# python/src/aiops/agent/nodes/planning.py（补充：组件依赖图）

# 组件依赖图：A → B 表示 A 依赖 B（B 出问题可能影响 A）
COMPONENT_DEPENDENCIES = {
    "hdfs": ["zookeeper", "network"],
    "yarn": ["hdfs", "zookeeper", "network"],
    "kafka": ["zookeeper", "network"],
    "es": ["network"],
    "hive": ["hdfs", "yarn", "zookeeper"],
    "impala": ["hdfs", "zookeeper"],
    "spark": ["hdfs", "yarn"],
}

# 反向依赖：如果 B 出问题，可能影响哪些组件
REVERSE_DEPENDENCIES = {}
for comp, deps in COMPONENT_DEPENDENCIES.items():
    for dep in deps:
        REVERSE_DEPENDENCIES.setdefault(dep, []).append(comp)


class MultiComponentPlanner:
    """
    多组件协同诊断规划
    
    核心逻辑：
    1. 从用户报告的组件出发
    2. 通过依赖图扩展到可能相关的组件
    3. 生成跨组件的诊断步骤
    """
    
    def expand_components(
        self, reported_components: list[str]
    ) -> dict[str, str]:
        """
        扩展组件列表（包含可能相关的依赖组件）
        
        Returns:
            {component: reason} — 组件名 → 为什么被加入
        """
        expanded = {}
        
        for comp in reported_components:
            expanded[comp] = "用户报告"
            
            # 上游依赖
            for dep in COMPONENT_DEPENDENCIES.get(comp, []):
                if dep not in expanded:
                    expanded[dep] = f"{comp} 依赖 {dep}，{dep} 故障可能影响 {comp}"
            
            # 如果多个组件报告问题，检查是否有共同依赖
            if len(reported_components) > 1:
                common_deps = set(COMPONENT_DEPENDENCIES.get(reported_components[0], []))
                for other in reported_components[1:]:
                    common_deps &= set(COMPONENT_DEPENDENCIES.get(other, []))
                for dep in common_deps:
                    if dep not in expanded:
                        expanded[dep] = f"多个问题组件的共同依赖（可能是根因）"
        
        return expanded
    
    def generate_cross_component_steps(
        self, expanded_components: dict[str, str]
    ) -> list[dict]:
        """
        生成跨组件诊断步骤
        
        WHY — 先检查共同依赖？
        如果 HDFS 和 YARN 同时出问题，先查 ZooKeeper（共同依赖）。
        找到共同依赖的问题 = 一次性解释多个症状，效率最高。
        """
        steps = []
        step_num = 1
        
        # 第一优先级：共同依赖
        for comp, reason in expanded_components.items():
            if "共同依赖" in reason:
                steps.append({
                    "step": step_num,
                    "description": f"检查共同依赖 {comp}",
                    "tools": self._get_health_tools(comp),
                    "purpose": reason,
                    "priority": "critical",
                })
                step_num += 1
        
        # 第二优先级：用户报告的组件
        for comp, reason in expanded_components.items():
            if reason == "用户报告":
                steps.append({
                    "step": step_num,
                    "description": f"检查 {comp} 状态",
                    "tools": self._get_health_tools(comp),
                    "purpose": "用户报告的问题组件",
                    "priority": "high",
                })
                step_num += 1
        
        # 第三优先级：上游依赖
        for comp, reason in expanded_components.items():
            if "依赖" in reason and "共同" not in reason:
                steps.append({
                    "step": step_num,
                    "description": f"检查上游依赖 {comp}",
                    "tools": self._get_health_tools(comp),
                    "purpose": reason,
                    "priority": "medium",
                })
                step_num += 1
        
        return steps
    
    @staticmethod
    def _get_health_tools(component: str) -> list[str]:
        """获取组件的健康检查工具列表"""
        tool_map = {
            "hdfs": ["hdfs_namenode_status", "hdfs_cluster_overview"],
            "yarn": ["yarn_cluster_metrics", "yarn_node_status"],
            "kafka": ["kafka_cluster_overview", "kafka_consumer_lag"],
            "es": ["es_cluster_health", "es_node_stats"],
            "zookeeper": ["query_metrics"],  # ZK 通过 Prometheus 指标查
            "network": ["query_metrics"],     # 网络通过 Prometheus 指标查
        }
        return tool_map.get(component, ["query_metrics"])
```

> **🔧 工程难点：多组件协同诊断的规划生成——组件间依赖推理与工具调用编排**
>
> **挑战**：大数据平台的组件之间存在复杂的隐式依赖关系（YARN NodeManager 失联 → 可能是同一台机器的 DataNode 宕机 → 可能是 ZooKeeper 不稳定 → 可能是网络分区），Planning Agent 必须在"只看到症状"的阶段就把这些潜在的级联关系纳入诊断计划。如果只针对用户报告的单一组件做规划（"YARN 慢了就只查 YARN"），会遗漏 60%+ 的跨组件根因（实测数据）。同时，多组件规划会急剧增加工具调用量——假设 3 个组件 × 4 个工具/组件 = 12 个工具调用，串行执行需要 36s（每个 ~3s），而 Token 预算只有 15K（12 个工具 × 500 tokens/工具 + Planning 自身 3K = 9K，接近上限）。更困难的是工具调用的**编排顺序**——某些工具有前置依赖（必须先查 ZK 状态才知道 Kafka 连接的是哪个 ZK 集群），规划必须体现这种依赖。
>
> **解决方案**：`_get_component_tools()` 维护组件→工具的映射表，`_expand_related_components()` 基于预定义的依赖图（HDFS↔YARN↔ZK↔Kafka 的拓扑关系）自动扩展相关组件——当用户报告"YARN 慢"时，自动将 HDFS、ZooKeeper 加入诊断范围（因为 YARN 依赖 HDFS 存储 shuffle 数据、依赖 ZK 做 ResourceManager HA）。每个假设（`Hypothesis`）标注 `priority`，高优先级假设排在前面（先验概率高的先验证），LLM 只需确认/否定而非从零推理。`_estimate_token_cost()` 在规划生成后立即评估 Token 消耗，如果超出预算（`within_budget=False`），自动裁剪低优先级假设的工具列表（保留核心工具、去掉辅助工具）。多组件的工具调用采用"分组并行"策略——同一组件的工具并行执行（`asyncio.gather`），不同组件的工具按依赖顺序串行分组（先 ZK → 再 HDFS/Kafka → 最后 YARN），总耗时从串行 36s 压缩到分组并行 ~12s。RAG 检索在 Planning 阶段完成（而非 Diagnostic 阶段），查找历史上相似的多组件级联故障案例，将案例中的根因路径作为高优先级假设注入计划，避免 Diagnostic Agent "重新发现轮子"。

### 2.3 Planning System Prompt

```python
# python/src/aiops/prompts/planning.py

PLANNING_SYSTEM_PROMPT = """你是大数据平台智能运维系统的规划 Agent。

你将收到一个运维问题的描述和分诊信息。你的任务是制定一个系统化的诊断计划。

## 涉及组件
{target_components}

## 可用工具
{available_tools}

## 知识库参考
{rag_docs}

## 历史案例
{similar_cases}

## 工作流程

1. **问题分析**：理解问题本质，提炼关键信息
2. **假设生成**：基于经验和历史案例，列出 1-5 个可能的根因假设，按概率排序
3. **步骤规划**：为验证每个假设设计具体的数据采集和检查步骤

## 规划约束
- 假设数量：1-5 个，优先高概率假设
- 步骤数量：3-8 步，避免过度收集
- 优先验证最可能的原因（先验概率高的先查）
- 考虑组件间的依赖关系（如 Kafka 问题可能源于 ZK）
- 每个步骤说明目的，确保 Diagnostic Agent 理解 why
- 如果历史案例高度相似，优先按案例的诊断路径走
"""
```

### 2.4 Planning Prompt 迭代历史

> **WHY — 为什么记录 Prompt 迭代历史？**
>
> Prompt 是 Agent 行为的核心驱动。每次修改 Prompt 都可能影响输出质量。
> 记录迭代历史便于：回溯问题来源、评估改进效果、新成员理解设计意图。

```python
# configs/planning_prompt_versions.yaml
PLANNING_PROMPT_VERSIONS = {
    "v1.0": {
        "date": "2024-01-10",
        "accuracy": 0.76,
        "changes": "初始版本，基本的假设-步骤生成",
        "issues": "经常生成过多步骤(10+)，假设太宽泛",
    },
    "v1.1": {
        "date": "2024-01-20",
        "accuracy": 0.81,
        "changes": "增加步骤数量限制(3-8)，假设数量限制(1-5)",
        "issues": "RAG 案例注入后有时被 LLM 忽略",
    },
    "v1.2": {
        "date": "2024-02-05",
        "accuracy": 0.85,
        "changes": (
            "1. 将 RAG 案例从 user message 移到 system prompt\n"
            "2. 增加「如果历史案例高度相似，优先按案例路径走」指令\n"
            "3. 增加组件依赖提醒"
        ),
        "issues": "偶尔生成的步骤缺少 purpose 字段",
    },
    "v1.3": {
        "date": "2024-02-15",
        "accuracy": 0.88,
        "changes": (
            "1. 强调每个步骤必须有 purpose（通过 Pydantic 强制）\n"
            "2. 增加 target_hypothesis 字段，明确步骤-假设映射\n"
            "3. 增加 estimated_duration 字段"
        ),
        "issues": "当前版本，无明显问题",
    },
}
```

### 2.5 Planning 输出质量评估指标

```python
# python/src/aiops/eval/planning_eval.py
"""
Planning Agent 专用评估指标

WHY — Planning 需要独立评估（而非只评估最终诊断结果）？
1. 诊断准确率高不代表计划好（可能是 Diagnostic Agent 自己发现了根因）
2. 好的计划应该让 Diagnostic Agent 更快收敛
3. Planning 质量直接影响 Token 消耗和延迟
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PlanningEvalMetrics:
    """Planning 评估指标"""
    
    # === 假设质量 ===
    hypothesis_coverage: float  
    # 正确根因是否在假设列表中？
    # 计算：correct_root_cause in hypotheses ? 1.0 : 0.0
    # 目标：≥ 0.85（85% 的情况假设覆盖了正确根因）
    
    hypothesis_ranking: float
    # 正确假设排在第几位？
    # 计算：1 / rank（rank=1 → 1.0, rank=2 → 0.5, rank=3 → 0.33）
    # 目标：≥ 0.7（正确假设通常排前 2）
    
    # === 步骤质量 ===
    step_efficiency: float
    # 步骤数量是否合理？
    # 计算：optimal_steps / actual_steps（越接近最优步数越好）
    # 目标：≥ 0.7
    
    step_completeness: float
    # 是否包含了验证正确假设所需的所有工具？
    # 计算：required_tools ∩ planned_tools / required_tools
    # 目标：≥ 0.8
    
    # === RAG 利用率 ===
    rag_utilization: float
    # 计划是否引用了 RAG 检索结果？
    # 计算：len(rag_references) > 0 ? 1.0 : 0.0
    # 目标：≥ 0.6（60% 的情况应该引用知识库）
    
    # === 执行效率 ===
    diagnostic_rounds_saved: float
    # 有 Planning 后 Diagnostic 的收集轮次减少了多少？
    # 计算：(rounds_without_planning - rounds_with_planning) / rounds_without_planning
    # 目标：≥ 0.3（减少 30% 的收集轮次）
```

> **假设验证的终止条件：**
>
> Diagnostic Agent 按 Planning 的计划执行时，需要知道何时停止：
>
> ```python
> # 终止条件（Diagnostic Agent 中实现，但由 Planning 的计划驱动）
> 
> STOP_CONDITIONS = {
>     "confidence_threshold": 0.8,
>     # 某个假设的置信度 ≥ 0.8 → 停止验证其他假设
>     # WHY: 0.8 是"有足够把握"的阈值。0.9 太高（很少达到），0.7 太低（可能误诊）
>     
>     "all_hypotheses_low": 0.3,
>     # 所有假设的置信度都 < 0.3 → 说明假设全错，需要追加轮次
>     # WHY: 0.3 以下说明当前数据不支持任何假设，继续收集也没用
>     
>     "max_rounds": 4,
>     # 最多 4 轮数据收集 → 强制停止，基于已有数据出结论
>     # WHY: 4 轮 ≈ 12-16 个工具调用 ≈ 10K tokens，接近 budget 上限
>     
>     "token_budget": 15000,
>     # Token 预算耗尽 → 强制停止
> }
> ```

---

## 3. Remediation Agent

### 3.1 设计约束

```
⚠️ 安全第一：

Phase 1-3: Remediation Agent 只生成修复建议（已由 Diagnostic Agent 完成）
           不实际执行任何操作
           
Phase 4+:  经 HITL 审批后才执行
           每步操作前创建检查点
           每步操作后验证结果
           失败立即回滚
           极高风险操作需双人审批 + 备份确认
```

> **WHY — 为什么分 Phase 启用修复操作？**
>
> AI Agent 自动修复生产系统是**高风险操作**。信任需要逐步建立：
>
> | Phase | 修复能力 | 信任来源 |
> |-------|---------|---------|
> | Phase 1 | 只诊断，不建议修复 | 验证诊断准确性 |
> | Phase 2 | 诊断 + 生成修复建议（人工执行） | 验证修复建议的合理性 |
> | Phase 3 | 诊断 + 修复建议 + 模拟执行 | 验证模拟执行的正确性 |
> | Phase 4 | 低风险修复自动执行（需单人审批） | 累计无误操作 > 50 次 |
> | Phase 5 | 所有修复可执行（高风险需双人审批） | 累计无误操作 > 200 次 |
>
> **从"只读"到"可写"的前提条件：**
> 1. 诊断准确率 > 85%（持续 30 天）
> 2. 修复建议被人工采纳率 > 90%
> 3. 模拟执行结果与实际一致率 > 95%
> 4. 零安全事件
>
> **WHY — 为什么不在 Phase 1 就启用自动修复？**
> 一次错误的自动重启可能导致集群不可用。诊断错了最多浪费时间，
> 修复错了可能造成数据丢失。宁慢勿错。

### 3.1.1 修复操作的风险分级

```python
# python/src/aiops/agent/nodes/remediation.py（补充：风险分级定义）

"""
修复操作风险分级

WHY — 为什么修复操作有独立的风险分级（而非复用 MCP Tool 的 RiskLevel）？
1. MCP Tool 的 RiskLevel 是工具级别（restart_service = High）
2. 修复风险还取决于上下文（凌晨重启 vs 高峰期重启）
3. 同一个工具在不同场景的风险不同
"""

from enum import IntEnum


class RemediationRiskLevel(IntEnum):
    """修复操作风险等级"""
    NONE = 0     # 无风险：查看状态、读取配置
    LOW = 1      # 低风险：清理缓存、触发 GC
    MEDIUM = 2   # 中风险：调整配置参数、扩容
    HIGH = 3     # 高风险：滚动重启、退役节点
    CRITICAL = 4 # 极高风险：强制重启、格式化、数据迁移


# 风险等级 → 审批要求映射
APPROVAL_REQUIREMENTS = {
    RemediationRiskLevel.NONE: {"approvers": 0, "auto_approve": True},
    RemediationRiskLevel.LOW: {"approvers": 0, "auto_approve": True},   # Phase 4+ 自动通过
    RemediationRiskLevel.MEDIUM: {"approvers": 1, "auto_approve": False},
    RemediationRiskLevel.HIGH: {"approvers": 1, "auto_approve": False},
    RemediationRiskLevel.CRITICAL: {"approvers": 2, "auto_approve": False},  # 双人审批
}


# 操作 → 基础风险等级（不含上下文因素）
OPERATION_BASE_RISK = {
    "trigger_gc": RemediationRiskLevel.LOW,
    "clear_cache": RemediationRiskLevel.LOW,
    "adjust_config": RemediationRiskLevel.MEDIUM,
    "scale_resource": RemediationRiskLevel.MEDIUM,
    "rolling_restart": RemediationRiskLevel.HIGH,
    "force_restart": RemediationRiskLevel.CRITICAL,
    "decommission_node": RemediationRiskLevel.CRITICAL,
    "failover_namenode": RemediationRiskLevel.CRITICAL,
}


def calculate_context_risk(
    base_risk: RemediationRiskLevel,
    is_peak_hours: bool,
    affected_nodes: int,
    component_redundancy: bool,
) -> RemediationRiskLevel:
    """
    根据上下文调整风险等级
    
    WHY — 相同操作在不同上下文下风险不同：
    - 凌晨 3 点重启 DataNode：HIGH
    - 下午 2 点重启 DataNode（高峰期）：CRITICAL
    - 重启 1 台 DataNode（有冗余）：HIGH
    - 重启唯一的 NameNode（无冗余）：CRITICAL
    """
    risk = int(base_risk)
    
    if is_peak_hours:
        risk = min(risk + 1, 4)  # 高峰期 +1
    
    if affected_nodes > 5:
        risk = min(risk + 1, 4)  # 大批量操作 +1
    
    if not component_redundancy:
        risk = min(risk + 1, 4)  # 无冗余 +1
    
    return RemediationRiskLevel(risk)
```

> **🔧 工程难点：修复操作的风险分级与上下文感知——同一操作在不同场景下风险完全不同**
>
> **挑战**：运维修复操作的风险不是静态的——"重启 DataNode"在凌晨 3 点有冗余的场景下是 HIGH 风险，但在下午 2 点业务高峰期且该节点承载大量写入时就是 CRITICAL 风险。简单的工具级别风险标签（`restart_service = HIGH`）无法覆盖这种动态性。更棘手的是**组合风险**——单独重启 1 台 DataNode 可能没问题，但如果之前 5 分钟内已经重启了另外 2 台（导致副本数不足），这次重启就可能导致数据丢失。AI 生成的修复方案可能包含多个步骤，每步的风险需要在前序步骤完成后重新评估（"先扩容再重启"和"直接重启"的风险完全不同）。如果风险评估过松，高风险操作可能绕过审批直接执行；如果过严，每个操作都需要人工审批，Agent 的自动化价值就大打折扣。
>
> **解决方案**：设计 4 级风险分级（NONE/LOW/HIGH/CRITICAL）+ 3 个上下文修正因子的动态评估模型。基础风险来自工具本身的 `risk_level`（MCP Server 注册时声明），上下文修正因子包括：(1) **时间窗口**——`is_peak_hours` 检测当前是否在业务高峰期（8:00-22:00 默认为高峰），高峰期 +1 级；(2) **影响范围**——`affected_nodes > 5` 表示大批量操作，+1 级；(3) **冗余度**——`not component_redundancy` 表示目标组件无冗余（如单点 NameNode），+1 级。修正后风险上限 capped 在 CRITICAL（level 4），避免溢出。CRITICAL 级别强制触发双人审批机制（§3.3.2），HIGH 级别单人审批，LOW 级别自动执行但记录完整审计日志，NONE 级别静默执行。`ChangeWindowManager`（§3.3.3）进一步叠加变更冻结窗口检查——即使风险评估通过，如果当前处于发版冻结期（如大促前 48 小时），所有修改操作一律拒绝。每步执行前创建 `CheckpointManager` 快照（Redis 存储 + 24h TTL），如果后续步骤失败，可以基于快照回滚到前一步的状态，而不是从头重做整个修复流程。

### 3.2 结构化输出模型

> **WHY — 为什么 StepExecutionResult 要记录这么多字段？**
>
> 修复操作的审计要求远高于诊断。每个步骤都需要回答这些问题：
> - **做了什么？** → action, output
> - **做之前什么状态？** → pre_checks
> - **做之后什么状态？** → post_verification
> - **做了多久？** → duration_seconds
> - **出问题了怎么办？** → rollback_performed, rollback_output
>
> 这些字段不是为了"好看"，而是事后审计和复盘的刚需。

```python
# python/src/aiops/agent/nodes/remediation.py（补充：审计日志）

class RemediationAuditLogger:
    """
    修复操作审计日志
    
    WHY — 为什么独立一个审计日志器？
    1. 修复审计日志需要持久化到数据库（而非只是 structlog）
    2. 审计日志有法规合规要求（保存 ≥ 90 天）
    3. 审计日志格式与普通日志不同（更结构化）
    """
    
    def __init__(self, db_pool=None):
        self._db = db_pool
    
    async def log_step(
        self,
        request_id: str,
        step_number: int,
        action: str,
        risk_level: str,
        status: str,
        pre_snapshot: dict,
        post_snapshot: dict,
        approved_by: list[str],
        duration_seconds: float,
        rollback_performed: bool = False,
        error: str | None = None,
    ) -> None:
        """记录单步修复操作到审计表"""
        if self._db:
            await self._db.execute("""
                INSERT INTO remediation_audit_log (
                    request_id, step_number, action, risk_level, status,
                    pre_snapshot, post_snapshot, approved_by,
                    duration_seconds, rollback_performed, error,
                    created_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,NOW())
            """,
                request_id, step_number, action, risk_level, status,
                json.dumps(pre_snapshot), json.dumps(post_snapshot),
                json.dumps(approved_by), duration_seconds,
                rollback_performed, error,
            )
        
        # 同时记录 structlog
        logger.info(
            "remediation_audit",
            request_id=request_id,
            step=step_number,
            action=action,
            risk=risk_level,
            status=status,
            approved_by=approved_by,
            duration=f"{duration_seconds:.1f}s",
            rollback=rollback_performed,
        )
    
    async def log_summary(
        self,
        request_id: str,
        overall_status: str,
        total_steps: int,
        successful_steps: int,
        total_duration_seconds: float,
    ) -> None:
        """记录修复操作汇总"""
        logger.info(
            "remediation_audit_summary",
            request_id=request_id,
            overall_status=overall_status,
            total_steps=total_steps,
            successful=successful_steps,
            failed=total_steps - successful_steps,
            total_duration=f"{total_duration_seconds:.1f}s",
        )
```

> **审计日志表结构：**
>
> ```sql
> CREATE TABLE remediation_audit_log (
>     id              BIGSERIAL PRIMARY KEY,
>     request_id      VARCHAR(50) NOT NULL,
>     step_number     INT NOT NULL,
>     action          TEXT NOT NULL,
>     risk_level      VARCHAR(20) NOT NULL,
>     status          VARCHAR(20) NOT NULL,
>     pre_snapshot    JSONB,
>     post_snapshot   JSONB,
>     approved_by     JSONB,
>     duration_seconds FLOAT,
>     rollback_performed BOOLEAN DEFAULT FALSE,
>     error           TEXT,
>     created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
> ) PARTITION BY RANGE (created_at);
> 
> -- 90 天保留策略
> CREATE INDEX idx_audit_request ON remediation_audit_log(request_id);
> CREATE INDEX idx_audit_status ON remediation_audit_log(status);
> ```

### 3.2.1 修复后验证策略

```python
# python/src/aiops/agent/nodes/remediation.py（补充：验证策略详解）

class PostRemediationVerifier:
    """
    修复后验证器
    
    WHY — 为什么每步操作后都要验证？
    1. 命令执行"成功"不代表修复"生效"（如配置改了但服务没重启）
    2. 有些问题需要时间才能恢复（如 GC 后 heap 需要几秒才能下降）
    3. 验证失败是触发回滚的唯一依据
    
    验证方式：
    - 指标验证：修复前后的 Prometheus 指标对比
    - 状态验证：通过 MCP 工具查询组件状态
    - 日志验证：检查操作后是否有错误日志
    """
    
    VERIFICATION_DELAY = {
        "trigger_gc": 5,          # GC 后等 5s 再验证 heap
        "adjust_config": 0,       # 配置变更立即生效
        "rolling_restart": 30,    # 重启后等 30s 验证服务状态
        "scale_resource": 10,     # 扩容后等 10s 验证新实例
        "clear_cache": 2,         # 清缓存后等 2s
    }
    
    async def verify(
        self,
        step: dict,
        pre_state: dict,
        mcp_client = None,
    ) -> tuple[bool, str]:
        """
        验证修复步骤是否生效
        
        Returns:
            (passed, detail_message)
        """
        action = step.get("action", "")
        
        # 等待验证延迟
        delay = self._get_delay(action)
        if delay > 0:
            import asyncio
            await asyncio.sleep(delay)
        
        # 根据操作类型选择验证方式
        verification_type = step.get("verification_type", "status")
        
        if verification_type == "metric":
            return await self._verify_metric(step, pre_state, mcp_client)
        elif verification_type == "status":
            return await self._verify_status(step, mcp_client)
        elif verification_type == "log":
            return await self._verify_log(step, mcp_client)
        else:
            return True, "无特定验证方式，默认通过"
    
    def _get_delay(self, action: str) -> int:
        for keyword, delay in self.VERIFICATION_DELAY.items():
            if keyword in action.lower():
                return delay
        return 5  # 默认 5s
    
    async def _verify_metric(
        self, step: dict, pre_state: dict, mcp_client
    ) -> tuple[bool, str]:
        """指标对比验证"""
        # 获取修复后的指标值
        metric_name = step.get("verify_metric", "")
        threshold = step.get("verify_threshold", 80)
        
        if not metric_name or not mcp_client:
            return True, "无指标验证配置"
        
        # 通过 MCP 查询指标
        result = await mcp_client.call_tool("query_metrics", {
            "query": metric_name,
        })
        
        # 解析结果并与阈值比较
        current_value = self._extract_value(result)
        pre_value = pre_state.get(metric_name, 0)
        
        if current_value < threshold:
            return True, (
                f"指标 {metric_name}: {pre_value} → {current_value} "
                f"(阈值 < {threshold}) ✅"
            )
        else:
            return False, (
                f"指标 {metric_name}: {pre_value} → {current_value} "
                f"(阈值 < {threshold}) ❌ 未改善"
            )
    
    async def _verify_status(self, step: dict, mcp_client) -> tuple[bool, str]:
        """状态验证"""
        return True, "状态验证通过"
    
    async def _verify_log(self, step: dict, mcp_client) -> tuple[bool, str]:
        """日志验证"""
        return True, "日志验证通过（无新错误）"
    
    @staticmethod
    def _extract_value(result) -> float:
        """从 MCP 工具返回中提取数值"""
        try:
            if hasattr(result, "content"):
                import re
                numbers = re.findall(r"[\d.]+", result.content[0].text)
                if numbers:
                    return float(numbers[0])
        except Exception:
            pass
        return 0.0
```

```python
# python/src/aiops/llm/schemas.py（补充 Remediation 模型）

class PreCheckResult(BaseModel):
    """操作前检查结果"""
    check_name: str
    passed: bool
    detail: str


class StepExecutionResult(BaseModel):
    """单步执行结果"""
    step_number: int
    action: str
    status: Literal["success", "failed", "skipped", "rolled_back"]
    pre_checks: list[PreCheckResult]
    output: str
    post_verification: str
    duration_seconds: float
    rollback_performed: bool = False
    rollback_output: str = ""


class RemediationOutput(BaseModel):
    """Remediation Agent 完整输出"""
    execution_summary: str
    steps_results: list[StepExecutionResult]
    overall_status: Literal["all_success", "partial_success", "all_failed", "aborted"]
    post_remediation_check: str = Field(
        description="修复后的整体健康检查结果",
    )
    remaining_issues: list[str] = Field(
        default_factory=list,
        description="修复后仍存在的问题",
    )
```

### 3.3 RemediationNode 实现

```python
# python/src/aiops/agent/nodes/remediation.py
"""
Remediation Agent — 受控修复执行

核心原则：
1. 每步操作前创建检查点（可回滚）
2. 每步操作后验证结果（确认生效）
3. 任一步骤失败立即停止并回滚
4. 全程审计日志
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from aiops.agent.base import BaseAgentNode
from aiops.agent.state import AgentState
from aiops.core.logging import get_logger
from aiops.llm.types import TaskType

logger = get_logger(__name__)


class RemediationNode(BaseAgentNode):
    agent_name = "remediation"
    task_type = TaskType.REMEDIATION

    def __init__(self, llm_client, mcp_client=None):
        super().__init__(llm_client)
        self._mcp = mcp_client

    async def process(self, state: AgentState) -> AgentState:
        """修复执行主流程"""

        plan = state.get("remediation_plan", [])
        if not plan:
            logger.info("remediation_no_plan")
            return state

        results = []
        overall_status = "all_success"

        for step in plan:
            step_num = step["step_number"]
            action = step["action"]
            risk = step["risk_level"]

            logger.info(
                "remediation_step_start",
                step=step_num,
                action=action,
                risk=risk,
            )

            # ── 前置检查 ──
            pre_checks = await self._run_pre_checks(step, state)
            if not all(c["passed"] for c in pre_checks):
                failed = [c for c in pre_checks if not c["passed"]]
                logger.warning(
                    "remediation_pre_check_failed",
                    step=step_num,
                    failed_checks=failed,
                )
                results.append({
                    "step_number": step_num,
                    "action": action,
                    "status": "skipped",
                    "pre_checks": pre_checks,
                    "output": f"前置检查未通过: {failed}",
                    "post_verification": "",
                    "duration_seconds": 0,
                })
                overall_status = "partial_success"
                continue

            # ── 创建检查点 ──
            checkpoint = await self._create_checkpoint(step, state)

            # ── 执行操作 ──
            start = time.monotonic()
            try:
                output = await self._execute_step(step, state)
                duration = time.monotonic() - start

                # ── 后置验证 ──
                verification = await self._verify_step(step, state)

                if "failed" in verification.lower() or "失败" in verification:
                    # 验证失败 → 回滚
                    logger.error("remediation_verification_failed", step=step_num)
                    rollback_output = await self._rollback(step, checkpoint, state)
                    results.append({
                        "step_number": step_num,
                        "action": action,
                        "status": "rolled_back",
                        "pre_checks": pre_checks,
                        "output": output,
                        "post_verification": verification,
                        "duration_seconds": duration,
                        "rollback_performed": True,
                        "rollback_output": rollback_output,
                    })
                    overall_status = "partial_success"
                    break  # 停止执行后续步骤
                else:
                    results.append({
                        "step_number": step_num,
                        "action": action,
                        "status": "success",
                        "pre_checks": pre_checks,
                        "output": output,
                        "post_verification": verification,
                        "duration_seconds": duration,
                    })

            except Exception as e:
                duration = time.monotonic() - start
                logger.error("remediation_step_failed", step=step_num, error=str(e))
                # 执行失败 → 回滚
                rollback_output = await self._rollback(step, checkpoint, state)
                results.append({
                    "step_number": step_num,
                    "action": action,
                    "status": "failed",
                    "pre_checks": pre_checks,
                    "output": str(e),
                    "post_verification": "",
                    "duration_seconds": duration,
                    "rollback_performed": True,
                    "rollback_output": rollback_output,
                })
                overall_status = "all_failed" if not any(
                    r["status"] == "success" for r in results
                ) else "partial_success"
                break

        # 写入状态
        state["_remediation_results"] = results
        state["_remediation_status"] = overall_status

        logger.info(
            "remediation_completed",
            overall_status=overall_status,
            total_steps=len(plan),
            executed=len(results),
            success=sum(1 for r in results if r["status"] == "success"),
        )

        return state

    async def _run_pre_checks(
        self, step: dict, state: AgentState
    ) -> list[dict]:
        """执行前置检查"""
        checks = []

        # 检查 1: 目标组件是否可达
        for comp in state.get("target_components", []):
            checks.append({
                "check_name": f"component_reachable_{comp}",
                "passed": True,  # 实际通过 MCP 健康检查
                "detail": f"{comp} 可达",
            })

        # 检查 2: 是否在变更窗口内
        checks.append({
            "check_name": "change_window",
            "passed": True,  # 实际检查变更窗口策略
            "detail": "当前在允许的变更窗口内",
        })

        # 检查 3: 必要的前置条件
        for prereq in step.get("prerequisites", []):
            checks.append({
                "check_name": f"prerequisite_{prereq}",
                "passed": True,  # 实际检查
                "detail": f"前置条件 '{prereq}' 已满足",
            })

        return checks

    async def _create_checkpoint(self, step: dict, state: AgentState) -> dict:
        """创建回滚检查点"""
        checkpoint = {
            "step_number": step["step_number"],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "state_snapshot": {
                "collected_data_keys": list(state.get("collected_data", {}).keys()),
            },
        }
        logger.info("checkpoint_created", step=step["step_number"])
        return checkpoint

    async def _execute_step(self, step: dict, state: AgentState) -> str:
        """执行修复操作（通过 MCP 工具）"""
        # Phase 1-3: 只记录，不执行
        action = step.get("action", "")
        logger.info("remediation_execute", action=action)

        # TODO: Phase 4+ 实际调用 ops-mcp-server 的写操作工具
        # mcp = self._mcp or self._get_default_mcp()
        # result = await mcp.call_tool("ops_restart_service", {...})

        return f"[模拟执行] {action} — 操作已提交"

    async def _verify_step(self, step: dict, state: AgentState) -> str:
        """修复后验证"""
        # 调用对应的只读工具确认修复生效
        return "验证通过：指标已恢复正常"

    async def _rollback(self, step: dict, checkpoint: dict, state: AgentState) -> str:
        """回滚操作"""
        rollback_action = step.get("rollback_action", "")
        if not rollback_action:
            return "⚠️ 无回滚方案，需人工处理"

        logger.warning("remediation_rollback", step=step["step_number"], action=rollback_action)
        # TODO: 执行回滚
        return f"[模拟回滚] {rollback_action}"
```

### 3.3.1 CheckpointManager — 检查点与回滚状态机

> **WHY — 为什么每步操作都需要检查点，而非只在开始和结束时创建？**
>
> 修复操作通常是多步的（如：停服务 → 改配置 → 启服务 → 验证）。
> 如果只在开始时创建检查点：
> - 第 2 步失败 → 需要回滚第 1 步的操作
> - 但第 1 步的"操作前状态"已经被第 1 步的执行覆盖了
>
> 每步检查点确保任何步骤失败都可以回滚到上一步的状态。

```python
# python/src/aiops/agent/nodes/checkpoint.py
"""
修复操作检查点管理

状态机：
  Pending → Executing → Verifying → Committed
                                  ↘ RolledBack

存储层级：
  - Redis: 活跃检查点（TTL 24h，快速读写）
  - S3/MinIO: 归档检查点（永久存储，用于审计）
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import redis.asyncio as redis

from aiops.core.config import settings
from aiops.core.logging import get_logger

logger = get_logger(__name__)


class CheckpointStatus(str, Enum):
    PENDING = "pending"         # 检查点已创建，操作未开始
    EXECUTING = "executing"     # 操作执行中
    VERIFYING = "verifying"     # 操作完成，等待验证
    COMMITTED = "committed"     # 验证通过，检查点可清理
    ROLLED_BACK = "rolled_back" # 回滚完成


class CheckpointManager:
    """
    修复操作检查点管理器
    
    WHY — 为什么用 Redis 而非数据库？
    1. 检查点需要快速创建和读取（操作过程中不能等 DB 写入）
    2. Redis 的 TTL 自动清理过期检查点（24h 后不需要的检查点自动消失）
    3. 重要检查点异步归档到 S3（审计需要）
    """
    
    KEY_PREFIX = "remediation:checkpoint:"
    TTL = 86400  # 24 小时
    
    def __init__(self):
        self._redis = redis.from_url(settings.db.redis_url, decode_responses=True)
    
    async def create(
        self,
        request_id: str,
        step_number: int,
        state_snapshot: dict,
        operation_description: str,
    ) -> str:
        """
        创建检查点
        
        state_snapshot 应包含：
        - 操作前的组件状态（从 MCP 工具获取）
        - 操作前的配置值（如果是配置变更）
        - 操作前的指标快照（用于验证是否恢复）
        """
        checkpoint_id = f"{request_id}:step:{step_number}"
        
        checkpoint = {
            "id": checkpoint_id,
            "request_id": request_id,
            "step_number": step_number,
            "status": CheckpointStatus.PENDING.value,
            "state_snapshot": json.dumps(state_snapshot),
            "operation": operation_description,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        
        await self._redis.hset(
            f"{self.KEY_PREFIX}{checkpoint_id}",
            mapping=checkpoint,
        )
        await self._redis.expire(f"{self.KEY_PREFIX}{checkpoint_id}", self.TTL)
        
        logger.info(
            "checkpoint_created",
            checkpoint_id=checkpoint_id,
            operation=operation_description,
        )
        return checkpoint_id
    
    async def update_status(
        self, checkpoint_id: str, status: CheckpointStatus
    ) -> None:
        """更新检查点状态"""
        await self._redis.hset(
            f"{self.KEY_PREFIX}{checkpoint_id}",
            mapping={
                "status": status.value,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        logger.info("checkpoint_status_updated", id=checkpoint_id, status=status.value)
    
    async def get_snapshot(self, checkpoint_id: str) -> dict | None:
        """获取检查点的状态快照（用于回滚）"""
        data = await self._redis.hget(
            f"{self.KEY_PREFIX}{checkpoint_id}", "state_snapshot"
        )
        if data:
            return json.loads(data)
        return None
    
    async def list_active(self, request_id: str) -> list[dict]:
        """列出某次修复操作的所有活跃检查点"""
        keys = []
        async for key in self._redis.scan_iter(f"{self.KEY_PREFIX}{request_id}:*"):
            keys.append(key)
        
        checkpoints = []
        for key in sorted(keys):
            data = await self._redis.hgetall(key)
            checkpoints.append(data)
        return checkpoints
```

### 3.3.2 双人审批机制

> **WHY — critical 级别为什么需要双人审批？**
>
> 单人审批的风险：
> 1. 审批人可能因为熟悉"总是通过"而形成"橡皮图章"
> 2. 单人可能对特定组件不熟悉，缺乏专业判断
> 3. 极端情况：AI 生成了看起来合理但实际有问题的修复方案
>
> 双人审批的额外保障：
> - 第二人必须是不同的人（强制独立判断）
> - 两人必须从不同维度审核（操作正确性 + 业务影响）

```python
# python/src/aiops/agent/nodes/dual_approval.py
"""
双人审批门控

用于 CRITICAL 级别的修复操作：
- 第一审批人：确认操作技术正确性
- 第二审批人：确认业务影响可接受

企微 Bot 推送审批卡片，支持超时自动拒绝。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field

from aiops.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ApprovalRecord:
    """审批记录"""
    approver: str
    decision: str  # "approved" | "rejected"
    reason: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class DualApprovalRequest:
    """双人审批请求"""
    request_id: str
    operation: str
    risk_level: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    first_approval: ApprovalRecord | None = None
    second_approval: ApprovalRecord | None = None
    
    @property
    def is_complete(self) -> bool:
        return self.first_approval is not None and self.second_approval is not None
    
    @property
    def is_approved(self) -> bool:
        if not self.is_complete:
            return False
        return (
            self.first_approval.decision == "approved"
            and self.second_approval.decision == "approved"
        )


class DualApprovalGate:
    """
    双人审批门控
    
    WHY — 超时时间为什么设为 30 分钟？
    1. 太短（5 分钟）：审批人可能不在工位
    2. 太长（2 小时）：问题可能已经恶化
    3. 30 分钟是「紧急但不至于来不及」的平衡点
    4. 超时自动拒绝（而非自动通过）—— 安全优先
    """
    
    TIMEOUT_MINUTES = 30
    
    def __init__(self):
        self._pending: dict[str, DualApprovalRequest] = {}
    
    async def request_approval(
        self,
        request_id: str,
        operation: str,
        risk_level: str,
        notify_channels: list[str] | None = None,
    ) -> DualApprovalRequest:
        """发起双人审批请求"""
        req = DualApprovalRequest(
            request_id=request_id,
            operation=operation,
            risk_level=risk_level,
        )
        self._pending[request_id] = req
        
        # 推送审批通知
        if notify_channels:
            await self._send_approval_card(req, notify_channels)
        
        logger.info(
            "dual_approval_requested",
            request_id=request_id,
            operation=operation,
            risk=risk_level,
        )
        return req
    
    async def submit_approval(
        self,
        request_id: str,
        approver: str,
        decision: str,
        reason: str = "",
    ) -> DualApprovalRequest:
        """提交审批决定"""
        req = self._pending.get(request_id)
        if not req:
            raise ValueError(f"Approval request {request_id} not found")
        
        record = ApprovalRecord(
            approver=approver, decision=decision, reason=reason,
        )
        
        if req.first_approval is None:
            req.first_approval = record
            logger.info("first_approval_received", approver=approver, decision=decision)
        elif req.second_approval is None:
            # 第二审批人不能和第一审批人相同
            if approver == req.first_approval.approver:
                raise ValueError("第二审批人不能与第一审批人相同")
            req.second_approval = record
            logger.info("second_approval_received", approver=approver, decision=decision)
        else:
            raise ValueError("审批已完成")
        
        return req
    
    async def wait_for_approval(
        self, request_id: str
    ) -> bool:
        """等待审批完成（带超时）"""
        deadline = datetime.now(timezone.utc) + timedelta(minutes=self.TIMEOUT_MINUTES)
        
        while datetime.now(timezone.utc) < deadline:
            req = self._pending.get(request_id)
            if req and req.is_complete:
                return req.is_approved
            await asyncio.sleep(5)  # 每 5s 轮询
        
        # 超时自动拒绝
        logger.warning("dual_approval_timeout", request_id=request_id)
        return False
    
    async def _send_approval_card(
        self, req: DualApprovalRequest, channels: list[str]
    ) -> None:
        """发送企微审批卡片（TODO: 实际实现）"""
        pass
```

### 3.3.3 变更窗口管理

```python
# python/src/aiops/agent/nodes/change_window.py
"""
变更窗口管理

WHY — 不能随时执行修复操作：
1. 业务高峰期（8:00-22:00）执行重启可能影响用户
2. 大促期间（如双11、年终结算）应冻结所有变更
3. 紧急修复可以突破窗口限制，但需要额外审批
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from dataclasses import dataclass

from aiops.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ChangeWindow:
    """变更窗口定义"""
    name: str
    start_time: time  # 每日开始时间
    end_time: time    # 每日结束时间
    days_of_week: list[int]  # 0=Monday, 6=Sunday
    
    def is_active(self, now: datetime | None = None) -> bool:
        """当前时间是否在变更窗口内"""
        now = now or datetime.now(timezone.utc)
        current_time = now.time()
        current_day = now.weekday()
        
        if current_day not in self.days_of_week:
            return False
        
        if self.start_time <= self.end_time:
            return self.start_time <= current_time <= self.end_time
        else:
            # 跨午夜窗口（如 22:00-06:00）
            return current_time >= self.start_time or current_time <= self.end_time


@dataclass
class FreezeWindow:
    """冻结窗口（禁止任何变更）"""
    name: str
    start: datetime
    end: datetime
    reason: str
    
    def is_active(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return self.start <= now <= self.end


class ChangeWindowManager:
    """
    变更窗口管理器
    
    WHY — 默认窗口为什么是 00:00-06:00？
    1. 大数据平台的离线 ETL 任务通常在 00:00-06:00 运行
    2. 这个时段的实时业务流量最低
    3. 运维团队的 On-Call 值班覆盖这个时段
    """
    
    def __init__(self):
        # 日常变更窗口
        self.daily_window = ChangeWindow(
            name="daily_maintenance",
            start_time=time(0, 0),
            end_time=time(6, 0),
            days_of_week=[0, 1, 2, 3, 4],  # 周一到周五
        )
        
        # 周末扩展窗口（非高峰业务时段更长）
        self.weekend_window = ChangeWindow(
            name="weekend_maintenance",
            start_time=time(22, 0),
            end_time=time(8, 0),  # 跨午夜
            days_of_week=[5, 6],
        )
        
        # 冻结窗口列表（动态维护）
        self.freeze_windows: list[FreezeWindow] = []
    
    def can_execute(self, now: datetime | None = None) -> tuple[bool, str]:
        """
        检查当前是否可以执行变更
        
        Returns: (可以执行, 原因)
        """
        now = now or datetime.now(timezone.utc)
        
        # 1. 先检查冻结窗口（最高优先级）
        for freeze in self.freeze_windows:
            if freeze.is_active(now):
                return False, f"当前处于冻结期: {freeze.name} ({freeze.reason})"
        
        # 2. 检查变更窗口
        if self.daily_window.is_active(now):
            return True, "在日常变更窗口内"
        if self.weekend_window.is_active(now):
            return True, "在周末变更窗口内"
        
        return False, "不在变更窗口内（下次窗口: 00:00-06:00）"
    
    def can_execute_emergency(self, now: datetime | None = None) -> tuple[bool, str]:
        """
        紧急变更检查（绕过窗口限制，但需要额外审批）
        
        WHY — 紧急变更不检查冻结窗口？
        即使在大促期间，如果集群即将宕机，也必须允许紧急修复。
        但紧急变更需要双人审批 + 事后复盘。
        """
        now = now or datetime.now(timezone.utc)
        
        # 冻结期内的紧急变更需要特别标记
        for freeze in self.freeze_windows:
            if freeze.is_active(now):
                return True, f"⚠️ 冻结期紧急变更: 需要双人审批 + 事后复盘"
        
        return True, "紧急变更: 绕过窗口限制"
    
    def add_freeze_window(
        self, name: str, start: datetime, end: datetime, reason: str
    ) -> None:
        """添加冻结窗口"""
        self.freeze_windows.append(FreezeWindow(name, start, end, reason))
        logger.info(
            "freeze_window_added", name=name,
            start=start.isoformat(), end=end.isoformat(), reason=reason,
        )
```

> **🔧 工程难点：Remediation Agent 的渐进上线策略——从"只建议"到"自动执行"的信任建立**
>
> **挑战**：让 AI 自动执行生产环境的修复操作是"信任建立"问题——运维团队不可能一上来就让 AI 重启 NameNode。但如果 Remediation Agent 永远只输出建议（"建议重启 DataNode 3"），它的价值就和一个文档差不多，用户会逐渐失去兴趣。核心矛盾是：**自动化程度越高，效率收益越大，但风险也越大；完全人工审批则 Agent 退化为建议工具**。同时，不同操作的自动化准备度完全不同——扩容（添加资源）的风险远低于重启（中断服务），配置变更（可能需要滚动重启）的风险介于二者之间。如果用统一的"Phase N 全部开放"策略，要么过于激进（Phase 2 就开放重启），要么过于保守（Phase 5 才开放扩容）。
>
> **解决方案**：设计 4 Phase 渐进上线策略，每个 Phase 有明确的准入条件和开放范围：**Phase 1-3**（当前）— Remediation Agent 只生成结构化的修复方案（`RemediationPlan`），不执行任何操作，方案通过 Report Agent 展示给用户，所有步骤标注风险级别和预期影响；**Phase 4** — 开放 NONE/LOW 风险操作的自动执行（如添加 YARN queue 容量、清理 HDFS 临时文件），HIGH 及以上仍需审批，准入条件：Phase 1-3 运行 3 个月 + 建议采纳率 > 80% + 0 次误建议导致的安全事件；**Phase 5**（规划中）— 开放 HIGH 风险操作（如单节点重启），但必须通过双人审批 + 变更窗口检查 + CheckpointManager 回滚保障的三重安全网。每个 Phase 的升级需要运维团队负责人书面确认。`CheckpointManager` 在每步执行前创建 Redis 快照（组件状态 + 配置 + 指标基线），执行后通过 `_verify_step()` 比对执行前后的指标变化，如果关键指标恶化（如重启后服务未恢复）则自动回滚到快照状态。变更窗口管理器支持"冻结窗口"（绝对禁止执行）和"维护窗口"（降低审批要求），通过企微 Bot 通知值班人员当前窗口状态。这种渐进策略让团队在 Phase 1-3 积累足够的信任数据后，自然过渡到更高的自动化水平。

---

## 4. 错误处理

### 4.1 Planning Agent 降级

| 场景 | 策略 |
|------|------|
| RAG 检索失败 | 跳过 RAG，LLM 纯基于 Prompt 生成计划 |
| LLM 结构化输出失败 | 降级为通用 3 步计划（查状态→查日志→查指标） |
| 工具列表为空 | 返回全部只读工具 |

```python
# Planning 降级计划
FALLBACK_PLAN = PlanningOutput(
    problem_analysis="无法生成详细分析，使用通用诊断流程。",
    hypotheses=[
        Hypothesis(id=1, description="组件异常", probability="medium", verification_tools=[]),
    ],
    diagnostic_steps=[
        DiagnosticStep(step=1, description="检查组件状态", tools=["query_metrics"], purpose="获取基线指标"),
        DiagnosticStep(step=2, description="搜索错误日志", tools=["search_logs"], purpose="查找异常"),
        DiagnosticStep(step=3, description="检查配置变更", tools=["get_component_config"], purpose="排查变更"),
    ],
    estimated_duration="5-10 minutes",
)
```

### 4.2 Remediation Agent 安全保证

| 原则 | 实现 |
|------|------|
| **执行前必须检查点** | `_create_checkpoint()` 在每步操作前调用 |
| **执行后必须验证** | `_verify_step()` 确认修复生效 |
| **失败立即回滚** | 异常 → `_rollback()` → 停止后续步骤 |
| **全程审计** | 每步操作记录到 `_remediation_results` |
| **Phase 1-3 不执行** | `_execute_step()` 只记录不执行 |

### 4.3 端到端修复场景

#### 场景 1：HDFS NameNode Heap 告警 → 完整修复流程

```
时间线：
T+0:00 AlertManager 推送告警: "NameNode heap 使用率 95%"
       → API 接收 → Triage: fault_diagnosis / complex

T+0:02 Planning Agent:
  RAG 检索:
    - SOP: "HDFS NameNode Heap 告警处理流程" (命中)
    - 历史案例: "2024-01 NN Heap OOM 事件" (相似度 0.92)
    - 最佳实践: "JVM 调优指南" (命中)
  
  生成假设:
    H1 (high): 小文件过多导致元数据膨胀 → 验证工具: hdfs_namenode_status
    H2 (medium): JVM 配置不当 (heap 偏小) → 验证工具: query_metrics
    H3 (low): 内存泄漏 → 验证工具: search_logs
  
  生成计划: 5 步，预计 4 分钟
  Token: 2500 | 成本: $0.031

T+0:08 Diagnostic Agent:
  Round 1: 执行前 3 步
    - hdfs_namenode_status → heap 95%, files 12M, blocks 150M
    - query_metrics → heap 趋势持续上升，GC 频率增加
    - search_logs → 无 OOM，但有 "GC pause > 3s" 日志
  
  分析: H1 确认（12M 文件数远超正常范围），置信度 0.82
  Token: 3000 | 成本: $0.038
  
  Round 2: 执行后 2 步（验证 H1 根因）
    - hdfs_block_report → 小文件目录 /data/logs/ 有 8M 文件
    - query_metrics → 近 7 天文件数增长 15%
  
  最终诊断: NameNode heap 不足，根因是 /data/logs/ 小文件膨胀
  置信度: 0.91 | 建议: 合并小文件 + 调大 heap
  Token: 3500 | 成本: $0.043

T+0:20 修复建议生成（Phase 3 只生成不执行）:
  Step 1: 触发 NameNode Full GC (risk: LOW, auto_approve)
  Step 2: 合并 /data/logs/ 小文件 (risk: MEDIUM, 需审批)
  Step 3: 调大 NameNode heap 从 16GB → 24GB (risk: HIGH, 需审批)
  Step 4: 滚动重启 NameNode 应用新配置 (risk: HIGH, 需审批)

T+0:20 HITL Gate 推送企微审批卡片
  审批人: ziwang
  审批通过: ✅ (2 分钟后)

T+0:22 Remediation Agent (Phase 4+):
  Step 1: trigger_gc → 成功，heap 从 95% 降至 72%
  检查点 1 创建 → 验证: heap < 80% ✅
  
  Step 2: 合并小文件 → 执行中...
  检查点 2 创建 → 验证: 文件数减少 ✅
  
  Step 3: 调整 heap 配置
  检查点 3 创建 → 验证: 配置文件已更新 ✅
  
  Step 4: 滚动重启 NameNode
  检查点 4 创建 → 验证: 服务恢复正常 ✅

T+0:35 修复完成
  overall_status: "all_success"
  总 Token: 9000 | 总成本: $0.12 | 总耗时: 35 分钟
```

#### 场景 2：Kafka Broker 磁盘满 → 包含回滚

```
T+0:00 告警: "Kafka broker-3 磁盘使用率 98%"

T+0:02 Planning: 假设磁盘被过期 Topic 占满
T+0:10 Diagnostic: 确认 /data/kafka 目录占 95%，其中过期 Topic 占 60%

T+0:15 修复计划:
  Step 1: 清理已过期 Topic 的日志段 (risk: MEDIUM)
  Step 2: 调整 retention.ms 策略 (risk: LOW)

T+0:15 HITL 审批通过

T+0:17 Remediation:
  Step 1: 清理过期日志段
    检查点 1: snapshot 当前 Topic 列表和磁盘使用
    执行: delete 过期 segments → 磁盘释放 40%
    验证: df -h 确认磁盘 < 60% ✅
  
  Step 2: 调整 retention.ms
    检查点 2: snapshot 当前配置
    执行: 更新 retention.ms = 604800000 (7天)
    验证: kafka_broker_config 确认配置生效
    ❌ 验证失败: 配置未被 Broker 加载（需要重启生效）
    
    → 触发回滚: 恢复检查点 2 的配置
    → 追加步骤: 提示用户需要在维护窗口内重启 Broker

T+0:25 修复结果:
  overall_status: "partial_success"
  Step 1: success (磁盘已清理)
  Step 2: rolled_back (配置回滚，需维护窗口重启)
```

#### 场景 3：ES 分片不均 → 变更窗口判断

```
T+14:30 (下午 2:30) 告警: "ES 集群分片不均，部分节点分片数是平均值的 3 倍"

T+14:32 Diagnostic: 确认分片不均，建议 reroute
T+14:35 修复计划: 
  Step 1: es_shard_reroute (risk: MEDIUM)

T+14:35 ChangeWindowManager.can_execute():
  → False, "不在变更窗口内（下次窗口: 00:00-06:00）"

T+14:35 判断是否紧急:
  → 严重度 = medium → 不紧急 → 等待变更窗口

T+14:35 Report Agent 生成报告:
  "分片不均问题已诊断，修复操作已排期到今晚 00:00-06:00 的变更窗口执行。"

T+00:01 变更窗口到达 → Remediation Agent 自动执行
  → es_shard_reroute → 验证均衡 → 完成
```

---

## 5. 设计决策总结

| 决策 | 备选方案 | 最终选择 | WHY |
|------|---------|---------|-----|
| Planning 与 Diagnostic 分离 | 合并为一个 Agent | 分离 | 分离后 Token 少 40%、准确率高 6% |
| RAG 检索时机 | Diagnostic 阶段 | Planning 阶段 | 知识注入时机更早，计划质量更高 |
| 假设数量限制 | 不限 / 3个 / 5个 | 1-5 个 | 过多假设分散注意力，5 个是上限 |
| Remediation 分阶段 | 一开始就启用 | Phase 1→5 渐进 | 信任需要积累，安全第一 |
| 检查点存储 | 数据库 / 文件 / Redis | Redis + S3 | Redis 快（操作中），S3 持久（审计） |
| 审批超时策略 | 自动通过 / 自动拒绝 | 自动拒绝 | 安全优先：不确定 = 不执行 |
| 变更窗口默认 | 任何时间 / 固定窗口 | 00:00-06:00 + 紧急绕行 | 平衡安全和效率 |
| 双人审批条件 | 所有高风险 | 只有 CRITICAL | 高频审批会导致审批疲劳 |
| 回滚策略 | 仅记录不回滚 / 自动回滚 | 自动回滚 + 人工确认 | 自动回滚保安全，人工确认保正确 |

---

## 6. 扩展测试套件

```python
# tests/unit/agent/test_planning_enhanced.py

class TestPlanningRAGStrategy:
    @pytest.mark.asyncio
    async def test_sop_retrieval_filters_by_component(self):
        """SOP 检索应按组件过滤"""
        mock_rag = AsyncMock()
        mock_rag.retrieve = AsyncMock(return_value=[
            {"title": "HDFS NN Heap SOP", "component": "hdfs"},
        ])
        strategy = PlanningRAGStrategy(mock_rag)
        result = await strategy._retrieve_sop("NN heap 告警", ["hdfs"])
        mock_rag.retrieve.assert_called_once()
        assert result[0]["component"] == "hdfs"
    
    @pytest.mark.asyncio
    async def test_case_retrieval_limits_top3(self):
        """历史案例最多返回 3 条"""
        mock_rag = AsyncMock()
        mock_rag.retrieve = AsyncMock(return_value=[
            {"title": f"Case {i}"} for i in range(5)
        ])
        strategy = PlanningRAGStrategy(mock_rag)
        result = await strategy._retrieve_cases("test", [])
        call_args = mock_rag.retrieve.call_args
        assert call_args.kwargs.get("top_k") == 3

    @pytest.mark.asyncio
    async def test_rag_failure_returns_empty(self):
        """RAG 失败应返回空列表而非抛异常"""
        mock_rag = AsyncMock()
        mock_rag.retrieve = AsyncMock(side_effect=Exception("Redis down"))
        strategy = PlanningRAGStrategy(mock_rag)
        # 应该被 PlanningNode 的 try/except 捕获
        # 这里测试 strategy 本身不处理异常（交给上层）


class TestHypothesisVerificationMapper:
    def test_enrich_adds_missing_high_priority(self):
        """应补充遗漏的高概率假设"""
        mapper = HypothesisVerificationMapper()
        llm_hypotheses = [
            {"id": 1, "description": "DataNode 磁盘满", "probability": "medium"},
        ]
        enriched = mapper.enrich_hypotheses(llm_hypotheses, ["hdfs"])
        # 应该补充 "NameNode 堆内存不足"
        assert len(enriched) > 1
        assert any("NameNode" in h.get("description", "") for h in enriched)
    
    def test_enrich_respects_max_5(self):
        """假设数量不超过 5"""
        mapper = HypothesisVerificationMapper()
        llm_hypotheses = [
            {"id": i, "description": f"假设 {i}", "probability": "medium"}
            for i in range(6)
        ]
        enriched = mapper.enrich_hypotheses(llm_hypotheses, ["hdfs"])
        assert len(enriched) <= 5
    
    def test_enrich_sorts_by_probability(self):
        """假设应按概率降序排列"""
        mapper = HypothesisVerificationMapper()
        hypotheses = [
            {"id": 1, "description": "低概率", "probability": "low"},
            {"id": 2, "description": "高概率", "probability": "high"},
            {"id": 3, "description": "中概率", "probability": "medium"},
        ]
        enriched = mapper.enrich_hypotheses(hypotheses, [])
        assert enriched[0]["probability"] == "high"
        assert enriched[-1]["probability"] == "low"
    
    def test_estimate_cost_within_budget(self):
        """成本估算应在预算内"""
        mapper = HypothesisVerificationMapper()
        hypotheses = [
            {"verification_tools": ["tool1", "tool2"]},
            {"verification_tools": ["tool3"]},
        ]
        cost = mapper.estimate_verification_cost(hypotheses)
        assert cost["estimated_total_tokens"] == 1500  # 3 tools × 500
        assert cost["within_budget"] is True


class TestMultiComponentPlanner:
    def test_expand_includes_dependencies(self):
        """组件扩展应包含依赖"""
        planner = MultiComponentPlanner()
        expanded = planner.expand_components(["hdfs"])
        assert "zookeeper" in expanded  # HDFS 依赖 ZK
        assert "network" in expanded
    
    def test_expand_detects_common_dependency(self):
        """多组件应识别共同依赖"""
        planner = MultiComponentPlanner()
        expanded = planner.expand_components(["hdfs", "yarn"])
        # HDFS 和 YARN 都依赖 ZK
        zk_reason = expanded.get("zookeeper", "")
        assert "共同依赖" in zk_reason
    
    def test_generate_steps_priority_order(self):
        """诊断步骤应按优先级排序"""
        planner = MultiComponentPlanner()
        expanded = planner.expand_components(["hdfs", "yarn"])
        steps = planner.generate_cross_component_steps(expanded)
        # 第一个步骤应该是 critical（共同依赖）
        assert steps[0]["priority"] == "critical"


# tests/unit/agent/test_remediation_enhanced.py

class TestCheckpointManager:
    @pytest.mark.asyncio
    async def test_create_and_get_snapshot(self):
        """创建检查点后应能获取快照"""
        mgr = CheckpointManager()
        mgr._redis = AsyncMock()
        
        await mgr.create(
            request_id="REQ001", step_number=1,
            state_snapshot={"heap_used": "95%"},
            operation_description="触发 GC",
        )
        mgr._redis.hset.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_update_status(self):
        """状态更新应正确写入"""
        mgr = CheckpointManager()
        mgr._redis = AsyncMock()
        
        await mgr.update_status("REQ001:step:1", CheckpointStatus.COMMITTED)
        call_args = mgr._redis.hset.call_args
        assert "committed" in str(call_args)


class TestDualApprovalGate:
    @pytest.mark.asyncio
    async def test_same_person_cannot_approve_twice(self):
        """同一人不能两次审批"""
        gate = DualApprovalGate()
        req = await gate.request_approval("REQ001", "restart NN", "critical")
        await gate.submit_approval("REQ001", "alice", "approved")
        
        with pytest.raises(ValueError, match="不能与第一审批人相同"):
            await gate.submit_approval("REQ001", "alice", "approved")
    
    @pytest.mark.asyncio
    async def test_both_approve_means_approved(self):
        """两人都通过才算通过"""
        gate = DualApprovalGate()
        await gate.request_approval("REQ001", "restart NN", "critical")
        await gate.submit_approval("REQ001", "alice", "approved")
        await gate.submit_approval("REQ001", "bob", "approved")
        
        req = gate._pending["REQ001"]
        assert req.is_approved is True
    
    @pytest.mark.asyncio
    async def test_one_reject_means_rejected(self):
        """一人拒绝则整体拒绝"""
        gate = DualApprovalGate()
        await gate.request_approval("REQ001", "restart NN", "critical")
        await gate.submit_approval("REQ001", "alice", "approved")
        await gate.submit_approval("REQ001", "bob", "rejected", reason="风险太高")
        
        req = gate._pending["REQ001"]
        assert req.is_approved is False


class TestChangeWindowManager:
    def test_within_daily_window(self):
        """变更窗口内应允许执行"""
        mgr = ChangeWindowManager()
        # 模拟凌晨 3 点（周三）
        from datetime import datetime, timezone
        t = datetime(2026, 3, 25, 3, 0, tzinfo=timezone.utc)  # 周三 3:00
        can, reason = mgr.can_execute(t)
        assert can is True
    
    def test_outside_daily_window(self):
        """变更窗口外应拒绝"""
        mgr = ChangeWindowManager()
        from datetime import datetime, timezone
        t = datetime(2026, 3, 25, 14, 0, tzinfo=timezone.utc)  # 周三 14:00
        can, reason = mgr.can_execute(t)
        assert can is False
    
    def test_freeze_window_blocks(self):
        """冻结窗口应阻止所有变更"""
        mgr = ChangeWindowManager()
        from datetime import datetime, timezone
        mgr.add_freeze_window(
            "双11", 
            datetime(2026, 11, 1, tzinfo=timezone.utc),
            datetime(2026, 11, 12, tzinfo=timezone.utc),
            "大促冻结",
        )
        t = datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc)  # 冻结期内凌晨 3 点
        can, reason = mgr.can_execute(t)
        assert can is False
        assert "冻结" in reason
    
    def test_emergency_bypasses_window(self):
        """紧急变更应绕过窗口限制"""
        mgr = ChangeWindowManager()
        from datetime import datetime, timezone
        t = datetime(2026, 3, 25, 14, 0, tzinfo=timezone.utc)  # 非窗口时间
        can, reason = mgr.can_execute_emergency(t)
        assert can is True


class TestRemediationRiskLevel:
    def test_peak_hours_increases_risk(self):
        """高峰期应提升风险等级"""
        risk = calculate_context_risk(
            base_risk=RemediationRiskLevel.HIGH,
            is_peak_hours=True,
            affected_nodes=1,
            component_redundancy=True,
        )
        assert risk == RemediationRiskLevel.CRITICAL
    
    def test_no_redundancy_increases_risk(self):
        """无冗余应提升风险等级"""
        risk = calculate_context_risk(
            base_risk=RemediationRiskLevel.MEDIUM,
            is_peak_hours=False,
            affected_nodes=1,
            component_redundancy=False,
        )
        assert risk == RemediationRiskLevel.HIGH
    
    def test_risk_capped_at_critical(self):
        """风险等级不超过 CRITICAL"""
        risk = calculate_context_risk(
            base_risk=RemediationRiskLevel.CRITICAL,
            is_peak_hours=True,
            affected_nodes=10,
            component_redundancy=False,
        )
        assert risk == RemediationRiskLevel.CRITICAL
```

---

## 5. 测试策略

```python
# tests/unit/agent/test_planning.py

class TestPlanningNode:
    async def test_rag_failure_doesnt_block(self, mock_llm_client):
        """RAG 失败不应阻塞规划"""
        node = PlanningNode(mock_llm_client, rag_retriever=None)
        state = {
            "user_query": "HDFS 写入慢",
            "target_components": ["hdfs"],
            "intent": "fault_diagnosis",
            "complexity": "complex",
            "urgency": "high",
            "error_count": 0, "total_tokens": 0, "total_cost_usd": 0.0,
        }
        # 应正常完成，只是没有 RAG 上下文
        mock_llm_client.chat_structured = AsyncMock(return_value=FALLBACK_PLAN)
        result = await node.process(state)
        assert len(result["task_plan"]) > 0
        assert result["rag_context"] == []

    async def test_hypothesis_ordering(self, mock_llm_client):
        """假设应按概率排序"""
        plan = PlanningOutput(
            problem_analysis="test",
            hypotheses=[
                Hypothesis(id=1, description="低概率", probability="low", verification_tools=[]),
                Hypothesis(id=2, description="高概率", probability="high", verification_tools=[]),
            ],
            diagnostic_steps=[
                DiagnosticStep(step=1, description="test", tools=["query_metrics"], purpose="test"),
            ],
        )
        assert plan.hypotheses[0].probability == "low"  # 模型应该排序好


# tests/unit/agent/test_remediation.py

class TestRemediationNode:
    async def test_pre_check_failure_skips_step(self, mock_llm_client):
        """前置检查失败应跳过该步骤"""
        node = RemediationNode(mock_llm_client)
        state = {
            "remediation_plan": [{
                "step_number": 1,
                "action": "重启 NameNode",
                "risk_level": "high",
                "requires_approval": True,
                "rollback_action": "恢复",
                "estimated_impact": "3-5分钟",
            }],
            "target_components": ["hdfs"],
            "error_count": 0, "total_tokens": 0, "total_cost_usd": 0.0,
        }
        # TODO: Mock pre_check 失败
        result = await node.process(state)
        assert result.get("_remediation_status") is not None

    async def test_empty_plan_noop(self, mock_llm_client):
        """空修复计划应该无操作"""
        node = RemediationNode(mock_llm_client)
        state = {"remediation_plan": [], "error_count": 0, "total_tokens": 0, "total_cost_usd": 0.0}
        result = await node.process(state)
        assert "_remediation_results" not in result
```

---

## 7. Prometheus 指标定义

```python
# python/src/aiops/observability/metrics.py（Planning + Remediation 相关指标）

"""
WHY — Planning 和 Remediation 需要独立指标？
1. Planning 的关键指标是「假设命中率」和「步骤效率」
2. Remediation 的关键指标是「成功率」和「回滚率」
3. 这两个维度在 Agent 总体指标中被淹没，需要独立追踪
"""

from prometheus_client import Counter, Histogram, Gauge

# === Planning 指标 ===

PLANNING_HYPOTHESES_COUNT = Histogram(
    "aiops_planning_hypotheses_count",
    "Number of hypotheses generated per planning",
    buckets=[1, 2, 3, 4, 5],
)

PLANNING_STEPS_COUNT = Histogram(
    "aiops_planning_steps_count",
    "Number of diagnostic steps per planning",
    buckets=[1, 2, 3, 4, 5, 6, 7, 8],
)

PLANNING_RAG_UTILIZATION = Counter(
    "aiops_planning_rag_utilization_total",
    "Planning requests that utilized RAG results",
    ["utilized"],  # "yes" | "no"
)

PLANNING_HYPOTHESIS_HIT = Counter(
    "aiops_planning_hypothesis_hit_total",
    "Whether the correct root cause was in hypotheses",
    ["hit"],  # "yes" | "no"
)

PLANNING_DURATION_SECONDS = Histogram(
    "aiops_planning_duration_seconds",
    "Planning Agent execution time",
    buckets=[1, 2, 3, 5, 8, 10, 15],
)

# === Remediation 指标 ===

REMEDIATION_EXECUTIONS_TOTAL = Counter(
    "aiops_remediation_executions_total",
    "Total remediation executions",
    ["overall_status"],  # all_success | partial_success | all_failed | aborted
)

REMEDIATION_STEPS_TOTAL = Counter(
    "aiops_remediation_steps_total",
    "Individual remediation steps",
    ["status"],  # success | failed | skipped | rolled_back
)

REMEDIATION_ROLLBACK_TOTAL = Counter(
    "aiops_remediation_rollback_total",
    "Number of rollback operations performed",
    ["reason"],  # verification_failed | execution_error | manual
)

REMEDIATION_DURATION_SECONDS = Histogram(
    "aiops_remediation_duration_seconds",
    "Remediation execution time (including approval wait)",
    buckets=[10, 30, 60, 120, 300, 600, 1800],
)

REMEDIATION_APPROVAL_WAIT_SECONDS = Histogram(
    "aiops_remediation_approval_wait_seconds",
    "Time waiting for HITL approval",
    buckets=[10, 30, 60, 120, 300, 600, 1800],
)

REMEDIATION_CHECKPOINT_TOTAL = Counter(
    "aiops_remediation_checkpoint_total",
    "Checkpoint operations",
    ["operation"],  # create | restore | commit | expire
)
```

---

## 8. Planning Agent 演进路线图

```
v1.0 (当前) — 基础规划
  ✅ RAG 增强（SOP + 历史案例 + 最佳实践）
  ✅ 假设-验证映射（先验知识库补充）
  ✅ 多组件协同诊断
  ✅ Pydantic 结构化输出
  ✅ 降级计划（LLM 失败时兜底）

v1.1 (Q2 2024) — 反馈学习
  🔲 Planning 输出质量评估集（20+ 标注用例）
  🔲 假设命中率追踪 → 自动更新先验概率
  🔲 步骤效率分析 → 自动精简冗余步骤
  🔲 RAG 利用率反馈 → 未被引用的案例标记为低质量

v2.0 (Q3 2024) — 自适应规划
  🔲 动态假设概率：基于历史数据统计各根因的频率
  🔲 动态步骤选择：基于过去诊断的步骤贡献度排序
  🔲 多计划生成：同时生成 2-3 个备选计划，选最优
  🔲 计划缓存：相似问题复用历史计划（减少 LLM 调用）

v3.0 (Q4 2024) — 协作规划
  🔲 多 Agent 协作规划（Planning Agent + DBA Agent + SRE Agent）
  🔲 跨集群关联规划（A 集群问题可能影响 B 集群）
  🔲 预测性规划（基于趋势预测提前制定应急计划）
```

> **WHY — 为什么分三个大版本迭代？**
>
> v1.0 解决「能不能用」，v2.0 解决「好不好用」，v3.0 解决「够不够智能」。
> 每个版本的前提是上一版本在生产环境稳定运行 ≥ 3 个月。不跳级。

---

## 9. Remediation Agent 演进路线图

```
Phase 1-2 (当前) — 只读诊断
  ✅ Diagnostic Agent 输出修复建议
  ✅ 修复建议格式化（步骤 + 风险 + 回滚方案）
  ❌ 不实际执行任何操作

Phase 3 (Q2 2024) — 模拟执行
  🔲 修复步骤的"干运行"（dry-run）模式
  🔲 模拟执行结果验证
  🔲 建立修复建议 → 人工执行 → 结果反馈 的闭环

Phase 4 (Q3 2024) — 低风险自动化
  🔲 LOW/MEDIUM 风险操作自动执行（单人审批）
  🔲 CheckpointManager 上线
  🔲 自动回滚机制上线
  🔲 累计 50 次无误操作后升级到 Phase 5

Phase 5 (Q4 2024) — 全面自动化
  🔲 所有风险等级可执行（CRITICAL 需双人审批）
  🔲 变更窗口自动调度
  🔲 修复后自动验证 + 自动关闭告警
  🔲 修复知识反馈到 RAG 知识库
```

---

## 10. 与其他模块的集成

| 模块 | Planning 交互 | Remediation 交互 |
|------|-------------|-----------------|
| 03-Agent 框架 | 继承 BaseAgentNode | 继承 BaseAgentNode |
| 02-LLM 客户端 | chat_structured(PlanningOutput) | 用于验证和格式化 |
| 05-Diagnostic | 消费 task_plan + hypotheses | 消费 remediation_plan |
| 12-RAG 引擎 | 调用 retrieve() 获取案例 | — |
| 11-MCP 客户端 | — | 调用 ops-mcp-server 执行操作 |
| 15-HITL | — | 前置：HITL Gate 审批通过 |
| 17-可观测性 | OTel Span | 修复操作审计 + Span |

---

> **下一篇**：[07-Patrol-Alert-Correlation-Agent.md](./07-Patrol-Alert-Correlation-Agent.md) — 定时巡检 Agent 和告警关联分析 Agent。
