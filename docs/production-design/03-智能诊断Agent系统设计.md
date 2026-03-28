# 03 - 智能诊断 Agent 系统设计

> **本文档定义**：AIOps 系统的核心智能层——多 Agent 协作编排、LLM 集成、MCP 工具层、HITL 机制的生产级设计。
> **核心挑战**：如何让 LLM "不确定性"的输出，在"确定性"的工程框架中可靠运行。

---

## 一、Agent 编排总体设计

### 1.1 LangGraph 编排图

```
                         用户请求 / 告警输入
                               │
                               ▼
                    ┌────────────────────┐
                    │   Triage Agent     │
                    │   (分诊路由)        │
                    └─────────┬──────────┘
                              │
                 ┌────────────┼────────────┐
                 │            │            │
          simple_query   diagnosis    alert_batch
                 │            │            │
                 ▼            ▼            ▼
          ┌──────────┐ ┌──────────┐ ┌──────────────┐
          │ 直接工具  │ │ Planning │ │ Alert        │
          │ 调用返回  │ │ Agent    │ │ Correlation  │
          └──────────┘ └────┬─────┘ │ Agent        │
                            │       └──────┬───────┘
                            │              │
                            ▼              │
                     ┌──────────┐          │
                     │Diagnostic│ ◄────────┘
                     │ Agent    │
                     └────┬─────┘
                          │
                    ┌─────┴──────┐
                    │            │
                 need_more   diagnosis_done
                 _data          │
                    │           ▼
                    │    ┌────────────────┐
                    │    │ HITL Gate      │ (高风险建议需审批)
                    └──→ │ (人工审批节点)  │
                         └───────┬────────┘
                                 │
                          ┌──────┴──────┐
                       approved      rejected
                          │              │
                          ▼              ▼
                   ┌──────────┐  ┌──────────┐
                   │Remediate │  │ Report   │ (跳过修复，仅报告)
                   │ Agent    │  │ Agent    │
                   └────┬─────┘  └────┬─────┘
                        │              │
                        ▼              │
                   ┌──────────┐        │
                   │ Report   │ ◄──────┘
                   │ Agent    │
                   └────┬─────┘
                        │
                        ▼
                   ┌──────────┐
                   │ 知识沉淀  │ (自动入库本次诊断经验)
                   └──────────┘
```

### 1.2 AgentState 设计（LangGraph 共享状态）

```python
from typing import TypedDict, Optional, Literal
from datetime import datetime

class ToolCallRecord(TypedDict):
    tool_name: str
    parameters: dict
    result: str
    duration_ms: int
    risk_level: str
    timestamp: str

class DiagnosisResult(TypedDict):
    root_cause: str
    confidence: float  # 0.0-1.0
    evidence: list[str]
    severity: Literal["critical", "high", "medium", "low", "info"]
    affected_components: list[str]
    related_alerts: list[str]

class RemediationStep(TypedDict):
    step_number: int
    action: str
    risk_level: Literal["none", "low", "medium", "high", "critical"]
    requires_approval: bool
    rollback_action: Optional[str]
    estimated_impact: str

class AgentState(TypedDict):
    # === 输入 ===
    request_id: str                    # 请求唯一 ID (用于 Tracing)
    request_type: str                  # "user_query" | "alert" | "patrol"
    user_query: str                    # 用户原始输入 / 告警描述
    user_id: str                       # 操作者 ID (RBAC 权限控制)
    cluster_id: str                    # 目标集群 ID
    alerts: list[dict]                 # 关联告警列表

    # === 分诊结果 ===
    intent: str                        # 识别的意图
    complexity: str                    # "simple" | "moderate" | "complex"
    route: str                         # "direct_tool" | "diagnosis" | "alert_correlation"

    # === 规划 ===
    task_plan: list[dict]              # 诊断步骤计划
    data_requirements: list[str]       # 需要采集的数据列表

    # === 数据采集 ===
    tool_calls: list[ToolCallRecord]   # 所有工具调用记录
    collected_data: dict               # 采集到的数据 (key=工具名, value=结果)
    collection_round: int              # 当前采集轮次 (防无限循环)
    max_collection_rounds: int         # 最大采集轮次 (默认 5)

    # === RAG 上下文 ===
    rag_context: list[dict]            # 检索到的知识库文档片段
    similar_cases: list[dict]          # 相似历史案例

    # === 诊断结果 ===
    diagnosis: DiagnosisResult         # 诊断结论
    remediation_plan: list[RemediationStep]  # 修复方案

    # === HITL ===
    hitl_required: bool                # 是否需要人工审批
    hitl_status: str                   # "pending" | "approved" | "rejected"
    hitl_comment: str                  # 审批意见

    # === 输出 ===
    final_report: str                  # 最终报告 (Markdown)
    knowledge_entry: dict              # 待沉淀的知识条目

    # === 元信息 ===
    messages: list                     # LangGraph 消息历史
    current_agent: str                 # 当前活跃 Agent
    error_count: int                   # 累计错误次数
    start_time: str                    # 开始时间
    total_tokens: int                  # 累计 Token 消耗
    total_cost_usd: float              # 累计 LLM 成本
```

### 1.3 路由逻辑

```python
def route_from_triage(state: AgentState) -> str:
    """Triage Agent 的路由决策"""
    route = state.get("route", "diagnosis")

    if route == "direct_tool":
        # 简单查询：直接调工具返回
        return "direct_tool_call"
    elif route == "alert_correlation":
        # 批量告警：先关联收敛再诊断
        return "alert_correlation"
    else:
        # 复杂问题：走完整诊断流程
        return "planning"


def route_from_diagnostic(state: AgentState) -> str:
    """Diagnostic Agent 的路由决策"""
    diagnosis = state.get("diagnosis", {})
    collection_round = state.get("collection_round", 0)
    max_rounds = state.get("max_collection_rounds", 5)

    # 防止无限循环：超过最大轮次强制结束
    if collection_round >= max_rounds:
        return "report"

    # 如果诊断结果置信度不够且还有数据可采集
    if diagnosis.get("confidence", 0) < 0.6 and state.get("data_requirements"):
        return "need_more_data"

    # 如果有高风险修复建议 → 需要人工审批
    remediation = state.get("remediation_plan", [])
    high_risk_steps = [s for s in remediation if s.get("risk_level") in ("high", "critical")]
    if high_risk_steps:
        return "hitl_gate"

    return "report"


def route_from_hitl(state: AgentState) -> str:
    """HITL 审批后的路由"""
    status = state.get("hitl_status", "pending")

    if status == "approved":
        return "remediation"
    elif status == "rejected":
        return "report"  # 跳过修复，直接出报告
    else:
        return "wait_for_approval"  # 继续等待
```

---

## 二、各 Agent 详细设计

### 2.1 Triage Agent（分诊 Agent）

**职责**：接收请求 → 意图识别 → 复杂度评估 → 路由分发

```python
# Triage Agent System Prompt
TRIAGE_SYSTEM_PROMPT = """
你是大数据平台智能运维系统的分诊 Agent。

你的任务是分析用户请求或告警信息，做出以下判断：

1. **意图识别**：判断请求属于哪个类别
   - status_query: 查询某个组件的当前状态（简单查询）
   - health_check: 整体健康检查（中等复杂度）
   - fault_diagnosis: 故障诊断（复杂）
   - capacity_planning: 容量规划（中等复杂度）
   - alert_handling: 告警处理（复杂度取决于告警数量）

2. **复杂度评估**：
   - simple: 单工具查询即可回答
   - moderate: 需要 2-3 个工具 + 简单分析
   - complex: 需要多步诊断 + 跨组件关联

3. **路由决策**：
   - simple → direct_tool (直接调工具返回)
   - moderate/complex → diagnosis (走完整诊断流程)
   - 批量告警 → alert_correlation (先关联收敛)

4. **提取关键信息**：
   - 涉及的组件 (hdfs/kafka/es/yarn/...)
   - 涉及的集群
   - 时间范围
   - 紧急程度

输出 JSON 格式：
{
  "intent": "fault_diagnosis",
  "complexity": "complex",
  "route": "diagnosis",
  "components": ["hdfs", "yarn"],
  "cluster": "prod-bigdata-01",
  "urgency": "high",
  "summary": "HDFS NameNode 堆内存持续升高，疑似内存泄露"
}
"""
```

**关键设计决策**：
- Triage 使用**轻量模型**（DeepSeek-V3 / Qwen2.5-7B），降低成本
- 简单查询（~40-50% 请求）直接返回，节省 70%+ Token
- 分诊结果做为后续 Agent 的输入上下文

### 2.2 Planning Agent（规划 Agent）

**职责**：分析问题 → 检索相似案例 → 制定诊断计划

```python
PLANNING_SYSTEM_PROMPT = """
你是大数据平台智能运维系统的规划 Agent。

你将收到一个运维问题的描述和分诊信息。你的任务是制定一个系统化的诊断计划。

## 工作流程

1. **问题分析**：理解问题的本质和可能的原因范围
2. **检索知识库**：查看相似的历史案例和相关 SOP
3. **制定诊断步骤**：按优先级列出需要检查的项目

## 可用数据源

以下是你可以让 Diagnostic Agent 调用的工具：
{available_tools}

## 历史相似案例（RAG 检索结果）
{similar_cases}

## 输出格式

```json
{
  "problem_analysis": "问题分析描述",
  "hypothesis": [
    {"id": 1, "description": "假设1：NN 堆内存不足", "probability": "high"},
    {"id": 2, "description": "假设2：RPC 请求积压", "probability": "medium"}
  ],
  "diagnostic_steps": [
    {
      "step": 1,
      "description": "检查 HDFS NameNode 状态",
      "tools": ["hdfs_namenode_status"],
      "purpose": "确认 NN 堆内存使用率和 SafeMode 状态",
      "priority": "critical"
    },
    {
      "step": 2,
      "description": "查看 NN 最近的错误日志",
      "tools": ["search_logs"],
      "parameters": {"component": "hdfs-namenode", "level": "ERROR", "time_range": "1h"},
      "purpose": "查找异常堆栈和错误模式",
      "priority": "high"
    }
  ],
  "estimated_duration": "3-5 minutes"
}
```

## 注意事项
- 优先检查最可能的原因（先验概率高的假设先验证）
- 每个步骤说明目的，确保 Diagnostic Agent 理解为什么要做
- 考虑组件间的依赖关系（如 Kafka 问题可能源于 ZK）
- 总步骤数控制在 3-8 步，避免过度收集
"""
```

**关键设计**：
- **RAG 增强**：每次规划前检索知识库中的相似案例，为 LLM 提供参考
- **假设驱动**：先列出可能原因的假设，再为每个假设设计验证步骤
- **步骤约束**：限制 3-8 步，避免 LLM 产生过多冗余步骤

### 2.3 Diagnostic Agent（诊断 Agent）

**职责**：执行诊断计划 → 调用 MCP 工具 → 分析数据 → 定位根因

```python
DIAGNOSTIC_SYSTEM_PROMPT = """
你是大数据平台智能运维系统的诊断 Agent。

你将收到一个诊断计划和已采集的数据，你的任务是：
1. 按照诊断计划调用工具采集数据
2. 分析采集到的数据
3. 基于数据和你的专业知识定位根因

## 诊断计划
{task_plan}

## 已采集的数据
{collected_data}

## 诊断方法论（五步法）

1. **症状确认**：确认报告的症状是否真实存在
2. **范围界定**：判断是单节点/单组件还是全局性问题
3. **时间关联**：确认问题开始的时间点，关联同期事件
4. **根因分析**：
   - 排除法：逐步排除不可能的原因
   - 因果链：建立 "原因→现象" 的因果链
   - 变更关联：检查问题发生前是否有配置/版本变更
5. **置信度评估**：对诊断结论的置信度打分

## 输出格式

```json
{
  "root_cause": "HDFS NameNode 堆内存不足，JVM 老年代占用 92%，频繁 Full GC 导致 RPC 处理延迟",
  "confidence": 0.85,
  "severity": "critical",
  "evidence": [
    "NN heap usage 92.3% (阈值 85%)",
    "最近 1 小时 Full GC 12 次，平均耗时 3.2s",
    "RPC 队列长度 230 (正常 <50)"
  ],
  "affected_components": ["hdfs-namenode", "hdfs-datanode", "yarn"],
  "causality_chain": "NN 堆内存不足 → Full GC 频繁 → RPC 处理阻塞 → DN 心跳超时 → Block 报告延迟",
  "remediation_plan": [
    {
      "step_number": 1,
      "action": "增加 NameNode JVM 堆内存至 48GB (-Xmx48g)",
      "risk_level": "high",
      "requires_approval": true,
      "rollback_action": "恢复原始 JVM 参数并重启 NN",
      "estimated_impact": "需要重启 NameNode，预计影响 3-5 分钟"
    },
    {
      "step_number": 2,
      "action": "检查并清理 HDFS 小文件（当前小文件占比需确认）",
      "risk_level": "medium",
      "requires_approval": true,
      "rollback_action": "N/A（只读操作）",
      "estimated_impact": "减少 NN 内存压力，无直接影响"
    }
  ],
  "additional_data_needed": null
}
```

## 关键约束
- 如果数据不足以得出结论（置信度 < 0.6），在 `additional_data_needed` 中说明还需要什么数据
- 修复步骤中 risk_level 为 "high" 或 "critical" 的必须设置 requires_approval = true
- 每个修复步骤必须有回滚方案
"""
```

**关键设计**：
- **多轮数据采集**：如果首次采集数据不够，可以请求更多数据（最多 5 轮）
- **置信度机制**：每次诊断结果附带置信度，低于阈值时触发额外采集或人工确认
- **因果链**：要求 LLM 输出因果推理链，增强可解释性
- **修复约束**：所有修复建议必须标注风险级别和回滚方案

### 2.4 Report Agent（报告 Agent）

**职责**：汇总全流程信息 → 生成结构化诊断报告 → 沉淀知识

```python
REPORT_SYSTEM_PROMPT = """
你是大数据平台智能运维系统的报告 Agent。

请根据以下诊断信息生成一份结构化的运维诊断报告。

## 报告模板

# 🔍 运维诊断报告

## 📋 基本信息
- **报告 ID**: {request_id}
- **诊断时间**: {start_time} ~ {end_time}
- **耗时**: {duration}
- **触发方式**: {request_type}
- **目标集群**: {cluster_id}

## 🎯 问题摘要
{一句话描述问题和根因}

## 📊 诊断过程

### 步骤 1: {step_description}
- **工具**: {tool_name}
- **关键发现**: {findings}

### 步骤 2: ...

## 🔬 根因分析
- **根因**: {root_cause}
- **置信度**: {confidence}%
- **严重程度**: {severity}
- **因果链**: {causality_chain}

## 📝 证据列表
{每条证据带数据支撑}

## 🛠️ 修复建议
{按优先级排列的修复步骤，标注风险级别}

## ⚠️ 影响评估
- **受影响组件**: {affected_components}
- **受影响服务**: {affected_services}
- **预估恢复时间**: {estimated_recovery}

## 📚 参考资料
- {相关知识库文档链接}
- {相似历史案例}

## 📈 资源消耗
- **LLM Token**: {total_tokens}
- **工具调用**: {tool_call_count} 次
- **预估成本**: ${total_cost}
"""
```

---

## 三、LLM 集成与管理

### 3.1 多模型路由策略

```python
class ModelRouter:
    """
    智能模型路由：根据任务类型、复杂度、数据敏感度选择最合适的模型
    """

    ROUTING_TABLE = {
        # (task_type, complexity, sensitivity) → (model, temperature)
        ("triage", "any", "any"):          ("deepseek-v3", 0.0),
        ("planning", "simple", "low"):     ("gpt-4o-mini", 0.1),
        ("planning", "complex", "low"):    ("gpt-4o", 0.1),
        ("planning", "any", "high"):       ("qwen2.5-72b-local", 0.1),  # 敏感数据不出域
        ("diagnostic", "any", "low"):      ("gpt-4o", 0.0),
        ("diagnostic", "any", "high"):     ("qwen2.5-72b-local", 0.0),
        ("report", "any", "any"):          ("gpt-4o-mini", 0.3),
        ("alert_correlation", "any", "any"): ("gpt-4o", 0.0),
    }

    def select_model(self, task_type: str, complexity: str, sensitivity: str) -> tuple:
        """返回 (model_name, temperature)"""
        # 精确匹配
        key = (task_type, complexity, sensitivity)
        if key in self.ROUTING_TABLE:
            return self.ROUTING_TABLE[key]

        # 通配匹配
        for pattern, model_config in self.ROUTING_TABLE.items():
            if (pattern[0] == task_type and
                (pattern[1] == "any" or pattern[1] == complexity) and
                (pattern[2] == "any" or pattern[2] == sensitivity)):
                return model_config

        # 默认
        return ("gpt-4o", 0.0)
```

### 3.2 LLM 调用容灾

```python
class LLMClient:
    """
    带容灾的 LLM 调用客户端

    容灾链路：GPT-4o → Claude 3.5 → 自部署 Qwen → 降级（规则引擎）
    """

    def __init__(self):
        self.providers = [
            LLMProvider("openai", model="gpt-4o", priority=1,
                       circuit_breaker=CircuitBreaker(failure_threshold=3, reset_timeout=60)),
            LLMProvider("anthropic", model="claude-3.5-sonnet", priority=2,
                       circuit_breaker=CircuitBreaker(failure_threshold=3, reset_timeout=60)),
            LLMProvider("local", model="qwen2.5-72b", priority=3,
                       circuit_breaker=CircuitBreaker(failure_threshold=5, reset_timeout=120)),
        ]

    async def call(self, messages: list, tools: list = None,
                   timeout: float = 30.0, **kwargs) -> LLMResponse:
        errors = []

        for provider in sorted(self.providers, key=lambda p: p.priority):
            if provider.circuit_breaker.is_open:
                continue

            try:
                response = await asyncio.wait_for(
                    provider.chat(messages, tools=tools, **kwargs),
                    timeout=timeout
                )
                provider.circuit_breaker.record_success()

                # 记录调用指标
                metrics.llm_call_total.labels(
                    provider=provider.name,
                    model=provider.model,
                    status="success"
                ).inc()

                return response

            except (TimeoutError, RateLimitError, ServiceError) as e:
                provider.circuit_breaker.record_failure()
                errors.append(f"{provider.name}: {e}")

                metrics.llm_call_total.labels(
                    provider=provider.name,
                    model=provider.model,
                    status="error"
                ).inc()

                logger.warning(f"LLM provider {provider.name} failed: {e}")
                continue

        # 所有供应商失败 → 降级到规则引擎
        logger.error(f"All LLM providers failed: {errors}")
        return self._fallback_rule_engine(messages)

    def _fallback_rule_engine(self, messages: list) -> LLMResponse:
        """
        降级方案：基于规则的简单响应
        不依赖 LLM，确保系统可用性
        """
        return LLMResponse(
            content="⚠️ 当前 AI 服务暂时不可用，已降级为规则引擎模式。\n"
                    "请直接使用命令行工具查看组件状态，或联系运维值班人员。",
            model="rule-engine-fallback",
            usage=TokenUsage(prompt_tokens=0, completion_tokens=0)
        )
```

### 3.3 结构化输出与重试

```python
from pydantic import BaseModel, Field, validator
from typing import Optional
import instructor

class DiagnosticOutput(BaseModel):
    """诊断结果的强类型定义"""
    root_cause: str = Field(description="根因描述")
    confidence: float = Field(ge=0.0, le=1.0, description="置信度 0-1")
    severity: Literal["critical", "high", "medium", "low", "info"]
    evidence: list[str] = Field(min_length=1, description="至少 1 条证据")
    affected_components: list[str]
    remediation_plan: list[RemediationStep]

    @validator('confidence')
    def validate_confidence(cls, v, values):
        """高严重度必须有较高置信度"""
        severity = values.get('severity')
        if severity in ('critical', 'high') and v < 0.5:
            raise ValueError(f"严重度为 {severity} 但置信度仅 {v}，需要更多证据")
        return v


async def call_diagnostic_llm(state: AgentState) -> DiagnosticOutput:
    """
    带结构化输出和重试的诊断 LLM 调用

    使用 instructor 库确保输出符合 Pydantic Schema
    最多重试 3 次
    """
    client = instructor.from_openai(openai_client)

    try:
        result = await client.chat.completions.create(
            model="gpt-4o",
            response_model=DiagnosticOutput,
            max_retries=3,
            messages=[
                {"role": "system", "content": DIAGNOSTIC_SYSTEM_PROMPT.format(...)},
                {"role": "user", "content": format_diagnostic_input(state)}
            ],
            temperature=0.0,
        )
        return result

    except instructor.MaxRetriesExceeded:
        # 3 次重试都无法获得合规输出 → 降级
        logger.error("Diagnostic LLM output validation failed after 3 retries")
        return DiagnosticOutput(
            root_cause="⚠️ 自动诊断输出格式异常，请人工介入",
            confidence=0.0,
            severity="medium",
            evidence=["LLM 输出格式校验失败"],
            affected_components=[],
            remediation_plan=[]
        )
```

---

## 四、MCP 工具层设计

### 4.1 MCP Server 架构

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    MCP Server 生产级架构（双源架构）                       │
│                                                                          │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │                     MCP Gateway (Go)                              │   │
│  │  ┌────────────┐  ┌─────────────┐  ┌────────────────────────────┐│   │
│  │  │ 认证/鉴权   │→ │ 速率限制     │→ │ 智能路由                    ││   │
│  │  │ (JWT Token) │  │ (令牌桶)     │  │ (按 component + 可用性路由) ││   │
│  │  └────────────┘  └─────────────┘  └────────────────────────────┘│   │
│  └──────────────────────────────────────────────────────────────────┘   │
│                              │                                           │
│         ┌────────────────────┼──────────────────────────┐               │
│         │                    │                          │                │
│         ▼                    ▼                          ▼                │
│  ══════════════════  ══════════════════  ══════════════════════════════ │
│  ║  内部 MCP       ║  ║  内部 MCP       ║  ║  TBDS-TCS 外部 MCP      ║ │
│  ║  Server 集群    ║  ║  Server 集群    ║  ║  (Streamable HTTP)      ║ │
│  ══════════════════  ══════════════════  ══════════════════════════════ │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────────────┐ │
│  │ hdfs-mcp     │  │ kafka-mcp    │  │ 真实集群数据采集              │ │
│  │ server       │  │ server       │  │                              │ │
│  │              │  │              │  │ ┌──────────────────────────┐ │ │
│  │ ┌──────────┐│  │ ┌──────────┐│  │ │ SSH 远程执行 (12节点)     │ │ │
│  │ │Middleware ││  │ │Middleware ││  │ │ • grep/tail 服务日志      │ │ │
│  │ │Chain     ││  │ │Chain     ││  │ │ • cat 配置文件            │ │ │
│  │ │• Audit   ││  │ │• Audit   ││  │ │ • df/free 系统资源        │ │ │
│  │ │• Risk    ││  │ │• Risk    ││  │ └──────────────────────────┘ │ │
│  │ │• Rate    ││  │ │• Rate    ││  │ ┌──────────────────────────┐ │ │
│  │ │• Timeout ││  │ │• Timeout ││  │ │ K8s 集群管理 (47 NS)      │ │ │
│  │ │• Cache   ││  │ │• Cache   ││  │ │ • Pod/Node/Event 查询    │ │ │
│  │ └──────────┘│  │ └──────────┘│  │ │ • 容器日志/命令执行       │ │ │
│  │ ┌──────────┐│  │ ┌──────────┐│  │ └──────────────────────────┘ │ │
│  │ │ Tools    ││  │ │ Tools    ││  │ ┌──────────────────────────┐ │ │
│  │ │ 8 tools  ││  │ │ 6 tools  ││  │ │ MySQL 只读 (3库)         │ │ │
│  │ └──────────┘│  │ └──────────┘│  │ │ • galileo/Hive/StarRocks │ │ │
│  └──────────────┘  └──────────────┘  │ └──────────────────────────┘ │ │
│                                       │ ┌──────────────────────────┐ │ │
│                                       │ │ Redis 只读 (全数据类型)   │ │ │
│                                       │ └──────────────────────────┘ │ │
│                                       │                              │ │
│                                       │ 特性：天然只读安全            │ │
│                                       │ 无需内部中间件链              │ │
│                                       └──────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────┘

路由策略：
• 通用数据采集（日志/配置/状态）→ 优先 TBDS-TCS（真实数据）
• 专用诊断逻辑（复合分析/异常检测）→ 内部 MCP Server（自研工具）
• 写操作（重启/扩缩容/配置变更）→ 仅内部 ops-mcp-server + HITL 审批
• 降级策略：TBDS-TCS 不可用时 → 回退到内部 MCP Mock 数据
```

### 4.2 中间件链设计

```go
// 中间件接口定义
type Middleware func(ToolHandler) ToolHandler
type ToolHandler func(ctx context.Context, params map[string]interface{}) (*ToolResult, error)

// 生产级中间件链
func BuildMiddlewareChain(handler ToolHandler, tool Tool) ToolHandler {
    // 洋葱模型：从外到内
    chain := handler

    // 1. 超时控制 (最外层)
    chain = TimeoutMiddleware(30 * time.Second)(chain)

    // 2. 熔断器
    chain = CircuitBreakerMiddleware(tool.Name())(chain)

    // 3. 速率限制
    chain = RateLimitMiddleware(10, time.Second)(chain)  // 10 req/s

    // 4. 风险检查 + HITL
    chain = RiskAssessmentMiddleware(tool.RiskLevel())(chain)

    // 5. 参数校验
    chain = ParameterValidationMiddleware(tool.Schema())(chain)

    // 6. 结果缓存 (只读工具)
    if tool.RiskLevel() == RiskNone {
        chain = CacheMiddleware(5 * time.Minute)(chain)
    }

    // 7. 审计日志 (最内层，但最先执行)
    chain = AuditMiddleware()(chain)

    // 8. OpenTelemetry Tracing
    chain = TracingMiddleware(tool.Name())(chain)

    return chain
}


// 熔断器中间件
func CircuitBreakerMiddleware(toolName string) Middleware {
    cb := gobreaker.NewCircuitBreaker(gobreaker.Settings{
        Name:        toolName,
        MaxRequests: 3,                    // 半开状态允许的探测请求数
        Interval:    60 * time.Second,     // 统计窗口
        Timeout:     30 * time.Second,     // 从开到半开的等待时间
        ReadyToTrip: func(counts gobreaker.Counts) bool {
            failureRatio := float64(counts.TotalFailures) / float64(counts.Requests)
            return counts.Requests >= 5 && failureRatio >= 0.5  // 50% 失败率触发
        },
        OnStateChange: func(name string, from, to gobreaker.State) {
            logger.Warn("Circuit breaker state change",
                "tool", name, "from", from, "to", to)
            metrics.CircuitBreakerState.WithLabelValues(name).Set(float64(to))
        },
    })

    return func(next ToolHandler) ToolHandler {
        return func(ctx context.Context, params map[string]interface{}) (*ToolResult, error) {
            result, err := cb.Execute(func() (interface{}, error) {
                return next(ctx, params)
            })
            if err != nil {
                if errors.Is(err, gobreaker.ErrOpenState) {
                    return nil, fmt.Errorf("tool %s is temporarily unavailable (circuit breaker open)", toolName)
                }
                return nil, err
            }
            return result.(*ToolResult), nil
        }
    }
}
```

#### TBDS-TCS 外部 MCP 服务的中间件策略

TBDS-TCS 作为外部只读 MCP 服务，其中间件策略与内部 Server 有所不同：

```
内部 MCP Server 中间件链（8 层）:
  Tracing → Audit → Cache → ParamValidation → Risk+HITL → RateLimit → CircuitBreaker → Timeout
  → 完整安全管控，适用于包含写操作的自研工具

TBDS-TCS 外部 MCP 代理层中间件（4 层）:
  Tracing → Audit → CircuitBreaker → Timeout
  → 精简链路，原因：
    • 天然只读安全 → 无需 Risk+HITL
    • 服务端已做参数校验 → 无需 ParamValidation
    • 服务端已做速率限制 → 无需客户端 RateLimit
    • 结果可能实时变化 → Cache 需谨慎（仅 ssh_list_targets 等元数据可缓存）

例外：pods_exec / resources_create_or_update → 走完整 8 层中间件链
```

### 4.3 工具实现示例：HDFS NameNode 状态

```go
// 生产级 HDFS NameNode 状态工具
type HDFSNameNodeStatus struct {
    client     HDFSClient         // 真实 HDFS API 客户端
    cache      *ristretto.Cache   // 本地缓存
    metrics    *ToolMetrics       // 指标收集器
}

func (t *HDFSNameNodeStatus) Name() string { return "hdfs_namenode_status" }

func (t *HDFSNameNodeStatus) Description() string {
    return `获取 HDFS NameNode 的详细状态信息。
返回内容包括：
- NameNode HA 状态 (Active/Standby)
- JVM 堆内存使用率和详情
- SafeMode 状态和原因
- RPC 队列长度和延迟
- HDFS 总容量和使用率
- 副本不足/损坏块数

使用场景：
- 当 HDFS 出现写入失败、延迟升高时，首先检查 NN 状态
- 定时巡检时作为 HDFS 健康检查的第一步
- 告警中提到 NameNode 相关问题时`
}

func (t *HDFSNameNodeStatus) Schema() map[string]interface{} {
    return map[string]interface{}{
        "type": "object",
        "properties": map[string]interface{}{
            "namenode": map[string]interface{}{
                "type":        "string",
                "description": "NameNode 标识，如 'nn1' 或 'nn2'。不指定则返回 Active NN",
                "enum":        []string{"nn1", "nn2", "active"},
                "default":     "active",
            },
            "include_jvm_detail": map[string]interface{}{
                "type":        "boolean",
                "description": "是否包含详细的 JVM 内存分区信息（Eden/Old/Metaspace）",
                "default":     false,
            },
        },
    }
}

func (t *HDFSNameNodeStatus) RiskLevel() RiskLevel { return RiskNone }  // 只读操作

func (t *HDFSNameNodeStatus) Execute(ctx context.Context, params map[string]interface{}) (*ToolResult, error) {
    span := trace.SpanFromContext(ctx)
    span.SetAttributes(attribute.String("tool.name", t.Name()))

    // 1. 获取目标 NameNode
    targetNN := getStringParam(params, "namenode", "active")
    includeJVM := getBoolParam(params, "include_jvm_detail", false)

    // 2. 调用 HDFS API
    status, err := t.client.GetNameNodeStatus(ctx, targetNN)
    if err != nil {
        span.RecordError(err)
        return nil, fmt.Errorf("failed to get NameNode status: %w", err)
    }

    // 3. 构建结果
    var sb strings.Builder
    sb.WriteString(fmt.Sprintf("## HDFS NameNode 状态 (%s)\n\n", status.Hostname))

    // HA 状态
    haEmoji := "🟢"
    if status.HAState != "active" {
        haEmoji = "🟡"
    }
    sb.WriteString(fmt.Sprintf("**HA 状态**: %s %s\n", haEmoji, status.HAState))

    // 堆内存
    heapPercent := float64(status.HeapUsed) / float64(status.HeapMax) * 100
    heapEmoji := "🟢"
    if heapPercent > 90 {
        heapEmoji = "🔴"
    } else if heapPercent > 80 {
        heapEmoji = "🟡"
    }
    sb.WriteString(fmt.Sprintf("**堆内存**: %s %.1f%% (%s / %s)\n",
        heapEmoji, heapPercent,
        formatBytes(status.HeapUsed), formatBytes(status.HeapMax)))

    // 异常检测
    alerts := []string{}
    if heapPercent > 85 {
        alerts = append(alerts, fmt.Sprintf("⚠️ NN 堆内存使用率 %.1f%%，超过 85%% 警戒线", heapPercent))
    }
    if status.SafeMode {
        alerts = append(alerts, fmt.Sprintf("🚨 NameNode 处于 SafeMode: %s", status.SafeModeReason))
    }
    if status.UnderReplicatedBlocks > 0 {
        alerts = append(alerts, fmt.Sprintf("⚠️ 存在 %d 个副本不足的块", status.UnderReplicatedBlocks))
    }
    if status.CorruptBlocks > 0 {
        alerts = append(alerts, fmt.Sprintf("🚨 存在 %d 个损坏的块", status.CorruptBlocks))
    }

    if len(alerts) > 0 {
        sb.WriteString("\n### ⚠️ 异常检测\n")
        for _, alert := range alerts {
            sb.WriteString(fmt.Sprintf("- %s\n", alert))
        }
    }

    return &ToolResult{Content: sb.String()}, nil
}
```

---

## 五、Human-in-the-Loop (HITL) 设计

### 5.1 五级风险分类

```
┌────────────────────────────────────────────────────────────────────────┐
│                    操作风险分级体系                                      │
│                                                                         │
│  Level 0: NONE (无风险)                                                 │
│  ────────────────────                                                   │
│  只读查询：状态查看、指标查询、日志搜索                                  │
│  处理方式：自动执行，无需审批                                            │
│                                                                         │
│  Level 1: LOW (低风险)                                                  │
│  ────────────────────                                                   │
│  低影响操作：配置检查、权限验证                                          │
│  处理方式：自动执行，记录审计日志                                        │
│                                                                         │
│  Level 2: MEDIUM (中风险)                                               │
│  ────────────────────                                                   │
│  中等影响：非核心配置修改、测试环境操作                                  │
│  处理方式：需要用户确认，但不需要主管审批                                │
│                                                                         │
│  Level 3: HIGH (高风险)                                                 │
│  ────────────────────                                                   │
│  高影响操作：服务重启、主备切换、配置变更                                │
│  处理方式：需要运维主管审批 + 变更窗口检查                               │
│                                                                         │
│  Level 4: CRITICAL (极高风险)                                           │
│  ────────────────────                                                   │
│  破坏性操作：数据删除、节点退役、集群级操作                              │
│  处理方式：需要双人审批 + 备份确认 + 变更窗口                            │
│                                                                         │
└────────────────────────────────────────────────────────────────────────┘
```

**TBDS-TCS 外部 MCP 工具的风险映射**:

| TBDS-TCS 工具 | 风险等级 | HITL 策略 | 理由 |
|---------------|---------|-----------|------|
| `ssh_list_targets` | Level 0 (NONE) | 自动执行 | 纯元数据查询 |
| `ssh_exec` (grep/tail/cat/df/free) | Level 0 (NONE) | 自动执行 | 服务端白名单限制，天然只读 |
| `pods_list/get/log` | Level 0 (NONE) | 自动执行 | K8s 只读查询 |
| `events_list/namespaces_list` | Level 0 (NONE) | 自动执行 | K8s 只读查询 |
| `resources_get/list` | Level 0 (NONE) | 自动执行 | K8s 只读查询 |
| `mysql_query` (SELECT/SHOW) | Level 0 (NONE) | 自动执行 | 只读事务，服务端禁止写操作 |
| `mysql_list_*/describe_table` | Level 0 (NONE) | 自动执行 | 纯元数据查询 |
| `redis_*_get/scan/members/range` | Level 0 (NONE) | 自动执行 | 只读操作 |
| `pods_exec` | Level 2 (MEDIUM) | 需用户确认 | 可在容器内执行命令，需审查命令内容 |
| `resources_create_or_update` | Level 3 (HIGH) | 需主管审批 | K8s 资源变更，可能影响运行中的服务 |

> **设计要点**：TBDS-TCS 的绝大多数工具（40+）映射到 Level 0，因为服务端已实施安全约束。仅 `pods_exec` 和 `resources_create_or_update` 需要 HITL 审批，这两个工具是 TBDS-TCS 中唯一可能产生副作用的操作。

### 5.2 HITL 审批流程

```python
class HumanApprovalGate:
    """
    生产级 HITL 审批门控

    支持多种审批通道：
    1. Web UI 审批
    2. 企业微信/钉钉机器人审批
    3. CLI 交互审批
    """

    async def request_approval(self, state: AgentState) -> ApprovalResult:
        remediation = state.get("remediation_plan", [])
        high_risk_steps = [s for s in remediation if s["risk_level"] in ("high", "critical")]

        if not high_risk_steps:
            return ApprovalResult(status="auto_approved", comment="无高风险操作")

        # 构建审批请求
        approval_request = ApprovalRequest(
            request_id=state["request_id"],
            requester=f"AIOps-Agent ({state['current_agent']})",
            diagnosis_summary=state["diagnosis"]["root_cause"],
            confidence=state["diagnosis"]["confidence"],
            operations=high_risk_steps,
            deadline=datetime.now() + timedelta(minutes=30),  # 30分钟超时
            cluster=state["cluster_id"],
        )

        # 发送到审批通道
        await self._send_to_approval_channels(approval_request)

        # 等待审批结果（异步，支持超时）
        result = await self._wait_for_approval(
            approval_request.request_id,
            timeout=timedelta(minutes=30)
        )

        # 审计记录
        await self._audit_log(approval_request, result)

        return result

    async def _send_to_approval_channels(self, request: ApprovalRequest):
        """多通道发送审批请求"""
        # 1. Web UI 推送
        await websocket_manager.broadcast(
            channel=f"approval:{request.cluster}",
            data=request.to_dict()
        )

        # 2. 企业微信通知
        await wecom_bot.send_interactive_card(
            receiver=self._get_approvers(request.cluster),
            card=self._build_approval_card(request)
        )

        # 3. 写入审批队列
        await approval_queue.put(request)

    def _build_approval_card(self, request: ApprovalRequest) -> dict:
        """构建企业微信交互式审批卡片"""
        return {
            "card_type": "interactive",
            "header": {"title": f"🔔 AIOps 运维操作审批 [{request.cluster}]"},
            "elements": [
                {"tag": "div", "text": f"**诊断结论**: {request.diagnosis_summary}"},
                {"tag": "div", "text": f"**置信度**: {request.confidence:.0%}"},
                {"tag": "div", "text": "**待审批操作**:"},
                *[{"tag": "div", "text": f"  {i+1}. [{op['risk_level']}] {op['action']}"}
                  for i, op in enumerate(request.operations)],
                {"tag": "action", "actions": [
                    {"tag": "button", "text": "✅ 批准", "value": "approved", "type": "primary"},
                    {"tag": "button", "text": "❌ 拒绝", "value": "rejected", "type": "danger"},
                    {"tag": "button", "text": "📝 部分批准", "value": "partial", "type": "default"},
                ]},
            ],
            "expire_time": int(request.deadline.timestamp()),
        }
```

---

## 六、Prompt 工程管理

### 6.1 Prompt 版本管理

```
prompt-templates/
├── v2.3/                        # 版本目录
│   ├── triage.jinja2            # 分诊 Prompt
│   ├── planning.jinja2          # 规划 Prompt
│   ├── diagnostic.jinja2        # 诊断 Prompt
│   ├── report.jinja2            # 报告 Prompt
│   ├── alert_correlation.jinja2 # 告警关联 Prompt
│   └── config.yaml              # 版本配置
├── eval/                        # 评测集
│   ├── triage_eval.yaml         # 分诊评测用例
│   ├── diagnostic_eval.yaml     # 诊断评测用例
│   └── report_eval.yaml         # 报告评测用例
└── CHANGELOG.md                 # 变更日志
```

```yaml
# config.yaml - Prompt 版本配置
version: "2.3"
release_date: "2026-03-25"
models:
  triage: "deepseek-v3"
  planning: "gpt-4o"
  diagnostic: "gpt-4o"
  report: "gpt-4o-mini"
changes:
  - "planning: 增加了容量预测场景的 Few-shot 示例"
  - "diagnostic: 优化了因果链输出格式要求"
eval_results:
  triage_accuracy: 0.93
  diagnostic_faithfulness: 0.87
  report_completeness: 0.91
  regression_count: 0
```

### 6.2 Prompt 回归评测

```python
# CI/CD 中的 Prompt 回归评测
class PromptRegressionTest:
    """
    每次 Prompt 变更后自动运行回归评测
    """

    async def run_regression(self, prompt_version: str) -> RegressionResult:
        eval_cases = load_eval_cases(f"prompt-templates/eval/")
        baseline = load_baseline_results(f"prompt-templates/v{self.last_version}/")

        results = []
        for case in eval_cases:
            result = await self.evaluate_single_case(case, prompt_version)
            results.append(result)

        # 计算指标
        metrics = {
            "triage_accuracy": self._calc_accuracy(results, "triage"),
            "diagnostic_faithfulness": self._calc_faithfulness(results, "diagnostic"),
            "tool_selection_accuracy": self._calc_tool_accuracy(results),
            "format_compliance": self._calc_format_compliance(results),
        }

        # 与基线对比
        regressions = []
        for key, value in metrics.items():
            baseline_value = baseline.get(key, 0)
            if value < baseline_value - 0.05:  # 允许 5% 波动
                regressions.append(f"{key}: {baseline_value:.2f} → {value:.2f} (↓{baseline_value-value:.2f})")

        return RegressionResult(
            metrics=metrics,
            regressions=regressions,
            passed=len(regressions) == 0,
            total_cases=len(eval_cases),
        )
```

---

## 七、错误处理与降级策略

### 7.1 分层错误处理

```python
class AgentErrorHandler:
    """
    Agent 执行过程中的错误处理策略
    """

    ERROR_STRATEGIES = {
        # 错误类型 → (重试次数, 重试间隔, 降级方案)
        "llm_timeout":       (2, 5,  "switch_provider"),
        "llm_rate_limit":    (3, 10, "switch_provider"),
        "llm_format_error":  (3, 0,  "simplify_prompt"),
        "tool_timeout":      (2, 3,  "skip_tool"),
        "tool_error":        (1, 0,  "skip_tool"),
        "rag_timeout":       (1, 3,  "skip_rag"),
        "state_corruption":  (0, 0,  "restart_from_checkpoint"),
        "unknown":           (1, 0,  "report_and_fallback"),
    }

    async def handle_error(self, error: Exception, state: AgentState, step: str) -> AgentState:
        error_type = self._classify_error(error)
        strategy = self.ERROR_STRATEGIES.get(error_type, self.ERROR_STRATEGIES["unknown"])
        max_retries, retry_delay, fallback = strategy

        # 更新错误计数
        state["error_count"] = state.get("error_count", 0) + 1

        # 超过全局错误限制 → 强制结束
        if state["error_count"] > 10:
            logger.error(f"Too many errors ({state['error_count']}), force ending")
            return self._force_end(state, "错误次数过多，自动终止")

        # 重试
        for attempt in range(max_retries):
            if retry_delay > 0:
                await asyncio.sleep(retry_delay * (attempt + 1))
            try:
                return await self._retry_step(state, step)
            except Exception:
                continue

        # 重试耗尽 → 执行降级方案
        return await self._execute_fallback(state, step, fallback)

    async def _execute_fallback(self, state, step, fallback):
        if fallback == "switch_provider":
            # 切换到备用 LLM 供应商
            state["_force_provider"] = self._get_next_provider()
            return state
        elif fallback == "skip_tool":
            # 跳过当前工具，标记为不可用
            state["collected_data"][f"UNAVAILABLE_{step}"] = "工具暂时不可用"
            return state
        elif fallback == "skip_rag":
            # 跳过 RAG 检索，使用空上下文
            state["rag_context"] = []
            return state
        elif fallback == "restart_from_checkpoint":
            # 从最近的 checkpoint 恢复
            return await self._restore_checkpoint(state)
        else:
            return self._force_end(state, f"步骤 {step} 执行失败，降级处理")
```

---

## 八、性能优化

### 8.1 延迟优化

| 优化措施 | 预估节省 | 实现方式 |
|---------|---------|---------|
| **SSE 流式输出** | 感知延迟降低 60% | FastAPI StreamingResponse |
| **工具并行调用** | 数据采集快 3-5x | asyncio.gather 并行调用多个 MCP 工具 |
| **RAG 预检索** | Planning 阶段快 30% | Triage 阶段就开始 RAG 检索 |
| **语义缓存** | 相似请求快 90% | 相似 Query 直接返回缓存诊断 |
| **Triage 快路径** | 40% 请求快 80% | 简单查询不走完整 Agent 流程 |
| **LLM Prompt 缓存** | LLM 调用快 20-40% | OpenAI Prompt Caching |

### 8.2 并行工具调用

```python
async def parallel_tool_calls(mcp_client, tool_calls: list[dict]) -> dict:
    """
    并行执行多个 MCP 工具调用

    示例：同时查询 HDFS 状态 + Kafka 状态 + ES 状态
    原本串行 3 × 2s = 6s → 并行 max(2s) = 2s
    """
    async def call_single_tool(tool_call):
        try:
            result = await asyncio.wait_for(
                mcp_client.call_tool(tool_call["name"], tool_call["params"]),
                timeout=15.0
            )
            return (tool_call["name"], result)
        except asyncio.TimeoutError:
            return (tool_call["name"], f"⚠️ 工具 {tool_call['name']} 调用超时 (15s)")
        except Exception as e:
            return (tool_call["name"], f"❌ 工具 {tool_call['name']} 调用失败: {e}")

    # 并行执行所有工具调用
    tasks = [call_single_tool(tc) for tc in tool_calls]
    results = await asyncio.gather(*tasks)

    return dict(results)
```

---

## 九、生产落地核心难点

> 引用自《AI Agent 工程化生产落地难点深度分析》中的关键挑战

### 9.1 LLM 输出不确定性的治理

**本系统的应对策略矩阵**：

| 不确定性类型 | 表现 | 应对措施 |
|------------|------|---------|
| **格式不确定** | JSON 字段名不一致、返回 Markdown | instructor + Pydantic 强校验 + 3 次重试 |
| **内容不确定** | 诊断结论不同次调用不一致 | 温度设为 0 + Few-shot 锚定 + Self-Consistency |
| **行为不确定** | 有时调工具 A 有时调工具 B | LangGraph 状态机控制流程 + 显式步骤计划 |
| **幻觉** | 编造不存在的指标值或组件 | 证据必须引用工具返回数据 + 置信度评分 |

### 9.2 工具调用的六大风险应对

| 风险 | 应对措施 |
|------|---------|
| **参数幻觉** | JSON Schema 校验 + 枚举约束 + 外部 ID 存在性检查 |
| **越权调用** | 基于 RBAC 的动态工具列表 + 每次调用鉴权 |
| **幂等性缺失** | 幂等 Key (请求哈希) + 状态检查前置 |
| **调用链故障** | LangGraph Checkpoint 断点续传 + 补偿事务 |
| **工具描述质量** | 标准化描述模板 + 评测集测试工具选择准确率 |
| **工具数量膨胀** | 按组件分组 + Triage 阶段动态加载相关工具子集 |
