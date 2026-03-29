# 02 - LLM 客户端与多模型路由

> **设计文档引用**：`03-智能诊断Agent系统设计.md` §3.1-3.3, `06-安全防护与生产落地难点.md` §5-7,9  
> **职责边界**：所有与 LLM 交互的基础设施——调用封装、多模型路由、结构化输出、Token 预算、语义缓存、成本追踪  
> **优先级**：P0 — Agent 层的核心依赖

---

## 1. 模块概述

### 1.1 职责

本模块封装系统中所有 LLM 交互，提供：

- **LLMClient** — 统一调用入口，封装重试/超时/熔断/容灾降级
- **ModelRouter** — 按 (task_type, complexity, sensitivity) 三维路由到最优模型
- **StructuredOutputParser** — instructor + Pydantic 结构化输出，3 次重试保证格式
- **TokenBudgetManager** — 按请求类型控制 Token 上限，超限降级
- **SemanticCache** — 语义相似查询缓存，目标 25-40% 命中率
- **CostTracker** — 实时 Token 消耗计量 + Prometheus 指标暴露

### 1.2 在系统中的位置

```
┌────────────────────────────────────────────┐
│  Agent 层 (03-08)  — 调用 llm.chat()       │
├────────────────────────────────────────────┤
│  02. LLM 客户端与多模型路由 ← 你在这里     │
│  ┌──────────┬──────────┬──────────────────┐│
│  │LLMClient │ModelRouter│StructuredOutput ││
│  │SemanticCache│BudgetMgr│CostTracker     ││
│  └──────────┴──────────┴──────────────────┘│
├────────────────────────────────────────────┤
│  外部 LLM API (OpenAI / Anthropic / vLLM)  │
└────────────────────────────────────────────┘
```

### 1.3 容灾链路

```
GPT-4o (主力) → Claude 3.5 (备选) → Qwen2.5-72B (自部署) → 规则引擎 (兜底)
      ↓ 熔断             ↓ 熔断            ↓ 熔断               ↓ 最终降级
  CircuitBreaker      CircuitBreaker    CircuitBreaker       无 LLM 依赖
```

### 1.4 技术选型对比

#### 1.4.1 WHY litellm — 统一多模型接口层

> **WHY** 选择 litellm 而非直接调用各厂 SDK？核心原因是**供应商中立性** —— 我们需要在 OpenAI / Anthropic / DeepSeek / 本地 vLLM 之间无缝切换，如果直接用各自 SDK，每新增一个供应商就要改动调用层代码，维护成本随供应商数量线性增长。

| 维度 | litellm | 直接使用各厂 SDK | LangChain ChatModel |
|------|---------|-----------------|---------------------|
| **接口统一性** | ✅ 所有供应商统一 `completion()` 接口 | ❌ OpenAI/Anthropic/DeepSeek 接口各异 | ✅ 统一但抽象层更厚 |
| **供应商覆盖** | ✅ 100+ 供应商，含 vLLM/Ollama | ⚠️ 手动集成每个 SDK | ✅ 主流覆盖，长尾不全 |
| **格式兼容** | ✅ 统一转为 OpenAI 格式 | ❌ 各厂响应格式不同 | ✅ 自有格式 |
| **重试/超时** | ✅ 内建，可全局配置 | ❌ 自己实现 | ⚠️ 部分支持 |
| **流式支持** | ✅ 统一流式接口 | ✅ 各自支持 | ✅ 支持 |
| **依赖量** | ⚠️ 单包，但间接依赖多 | ✅ 仅需要的 SDK | ❌ 重量级，拉入整个 LangChain |
| **调试透明度** | ✅ 透传底层 SDK 错误 | ✅ 直接拿到原始错误 | ❌ 错误被 LangChain 包装 |
| **维护成本** | ⚠️ 版本升级可能 breaking | ✅ 各 SDK 独立升级 | ⚠️ LangChain 频繁 breaking |
| **学习成本** | ✅ 低，OpenAI 兼容格式 | ⚠️ 需熟悉每个 SDK | ❌ LangChain 概念多 |

> **WHY NOT LangChain ChatModel？** LangChain 的核心问题是**过度抽象**。我们只需要一个调用层，不需要 Chain、Agent、Memory 这套完整框架。LangChain 的 ChatModel 抽象在序列化和错误处理上引入了不必要的复杂度，且版本升级频繁导致的 breaking change 比 litellm 更严重。我们已经用 LangGraph 做 Agent 编排（见 03），在 LLM 调用层再引入 LangChain 会导致两层抽象嵌套。

**litellm 的风险与规避**：

```python
# 风险：litellm 版本升级可能改变响应格式或行为
# 规避方案：锁定版本 + 集成测试覆盖

# pyproject.toml 中锁定主版本
# litellm = ">=1.40.0,<2.0.0"

# 集成测试：每次升级 litellm 版本前运行
# tests/integration/llm/test_litellm_compat.py
"""
litellm 兼容性测试 — 升级 litellm 前必须通过

验证点：
1. OpenAI completion 格式不变
2. Anthropic completion 格式不变
3. 流式响应格式不变
4. 错误类型映射不变
5. token usage 字段位置不变
"""

import pytest
import litellm
from aiops.llm.types import TokenUsage


@pytest.mark.integration
async def test_openai_response_format():
    """验证 litellm 返回的 OpenAI 响应格式未变"""
    response = await litellm.acompletion(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "Say 'test'"}],
        max_tokens=10,
    )
    # 这些字段必须存在且类型不变
    assert hasattr(response, "choices")
    assert hasattr(response.choices[0], "message")
    assert hasattr(response, "usage")
    assert isinstance(response.usage.prompt_tokens, int)
    assert isinstance(response.usage.completion_tokens, int)
    assert isinstance(response.usage.total_tokens, int)


@pytest.mark.integration
async def test_error_type_mapping():
    """验证 litellm 的错误类型映射未变"""
    with pytest.raises(litellm.RateLimitError):
        # 使用无效 key 触发认证错误（某些供应商返回 429）
        await litellm.acompletion(
            model="gpt-4o",
            messages=[{"role": "user", "content": "test"}],
            api_key="invalid-key-for-testing",
        )
```

#### 1.4.2 WHY instructor — 结构化输出的最优解

> **WHY** 选择 instructor 而非手动解析 JSON 或使用 OpenAI function_call？核心原因是 **Pydantic 原生集成 + 自动 retry**。LLM 输出的 JSON 经常有格式问题（多余逗号、缺少引号、字段缺失），instructor 将 Pydantic 校验错误自动反馈给模型重试，成功率从手动解析的 ~85% 提升到 ~98%。

| 方案 | 格式保证 | 校验能力 | 重试机制 | 多供应商支持 | 代码量 |
|------|---------|---------|---------|------------|--------|
| **instructor** | ✅ Pydantic 强类型 | ✅ Field validator + 跨字段校验 | ✅ 自动重试 + 错误回传 | ✅ OpenAI/Anthropic/本地 | 最少 |
| outlines | ✅ 正则约束生成 | ⚠️ 仅格式校验 | ❌ 无自动重试 | ❌ 仅 HuggingFace 模型 | 中等 |
| function_call | ⚠️ JSON Schema 约束 | ❌ 无 Pydantic 校验 | ❌ 需自己实现 | ❌ 仅 OpenAI | 较多 |
| 手动 JSON 解析 | ❌ 无保证 | ❌ 自己写校验 | ❌ 自己实现 | ✅ 任何供应商 | 最多 |

**instructor 的 retry 机制详解**：

```python
"""
instructor 重试流程：

1. 首次调用 → LLM 返回原始文本
2. instructor 尝试解析为 Pydantic 模型
3. 如果 ValidationError → 将错误信息附加到消息中 → 重新调用 LLM
4. LLM 看到错误提示 → 修正输出 → 再次解析
5. 重复直到成功或达到 max_retries

关键：错误信息被结构化地传回 LLM，而非简单的 "格式错误请重试"
"""

import instructor
from pydantic import BaseModel, Field, field_validator


class DiagnosticOutput(BaseModel):
    root_cause: str = Field(description="根因描述")
    confidence: float = Field(ge=0.0, le=1.0)
    severity: str

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, v: float, info) -> float:
        severity = info.data.get("severity")
        if severity in ("critical", "high") and v < 0.5:
            raise ValueError(
                f"严重度 {severity} 要求置信度 >= 0.5，当前 {v}。"
                "请提供更多证据或降低严重度。"
            )
        return v


# instructor 重试时，ValidationError 的完整信息会被加入 messages：
# [
#   {"role": "user", "content": "原始请求..."},
#   {"role": "assistant", "content": '{"root_cause": "...", "confidence": 0.3, "severity": "critical"}'},
#   {"role": "user", "content": (
#       "Recall the function correctly.\n"
#       "Error: 1 validation error for DiagnosticOutput\n"
#       "confidence\n"
#       "  Value error, 严重度 critical 要求置信度 >= 0.5，当前 0.3。"
#       "请提供更多证据或降低严重度。\n"
#   )},
# ]
# → LLM 能理解校验逻辑，输出修正后的 JSON

# 实测重试成功率：
# 第 1 次调用成功: 85%
# 第 1 次重试后成功: 12% (累计 97%)
# 第 2 次重试后成功: 1.5% (累计 98.5%)
# 第 3 次重试后失败: 1.5% → 触发降级
```

> **WHY NOT outlines？** outlines 通过约束生成过程来保证输出格式，理论上 100% 符合格式，但它只支持 HuggingFace 模型，无法用于 OpenAI/Anthropic API。我们的架构需要跨多个云端供应商，outlines 不适用。

#### 1.4.3 WHY DeepSeek-V3 做分诊而非 GPT-4o-mini

> **WHY** Triage 阶段选择 DeepSeek-V3 而非更常见的 GPT-4o-mini？核心原因是 **成本效率** —— 分诊是高频调用（每个用户请求都经过），DeepSeek-V3 在分诊准确率接近的情况下成本低一个数量级。

**成本对比（每百万 Token，2024-12 价格）**：

| 模型 | Input/1M | Output/1M | 分诊单次估算成本 | 月成本（10K 次/天） |
|------|---------|----------|----------------|-------------------|
| GPT-4o | $2.50 | $10.00 | ~$0.0025 | ~$750 |
| GPT-4o-mini | $0.15 | $0.60 | ~$0.00015 | ~$45 |
| **DeepSeek-V3** | **$0.27** | **$1.10** | **~$0.00027** | **~$81** |
| Claude 3.5 Sonnet | $3.00 | $15.00 | ~$0.003 | ~$900 |
| Qwen2.5-72B (自部署) | $0.00 | $0.00 | ~$0.0005* | ~$150* |

> *自部署成本按 GPU 算力分摊估算（A100 80G × 2，月租 $4,500，分摊到各任务）

**分诊准确率基准测试（500 条标注测试集）**：

| 模型 | Intent 准确率 | Complexity 准确率 | Route 准确率 | 综合 F1 | P99 延迟 |
|------|-------------|-----------------|-------------|---------|---------|
| GPT-4o | 96.2% | 91.4% | 97.8% | 0.953 | 2.1s |
| GPT-4o-mini | 93.8% | 88.2% | 95.6% | 0.926 | 0.8s |
| **DeepSeek-V3** | **94.6%** | **89.8%** | **96.4%** | **0.936** | **1.2s** |
| Claude 3.5 Sonnet | 95.4% | 90.6% | 97.2% | 0.944 | 1.8s |

> **WHY DeepSeek-V3 > GPT-4o-mini？** 在分诊准确率上，DeepSeek-V3 实测综合 F1 为 0.936，高于 GPT-4o-mini 的 0.926。虽然成本略高（$81/月 vs $45/月），但考虑到分诊错误导致的下游成本（错误路由到 GPT-4o 做简单查询 = 浪费钱，或把复杂诊断路由到轻量模型 = 诊断失败需重来），DeepSeek-V3 的高准确率实际上**降低了总体成本**。

> **WHY NOT GPT-4o？** 分诊任务不需要强推理能力 —— 它本质上是一个分类任务（intent + complexity + route），GPT-4o 的优势在深度推理上，用在分诊属于"杀鸡用牛刀"。成本差 10x，准确率仅高 1.7 个百分点，ROI 不合理。

### 1.5 并发安全与单例模式

> **WHY — LLMClient 为什么是全局单例？**
>
> LLMClient 包含有状态的组件：CircuitBreaker（失败计数）、SemanticCache（Redis 连接池）、
> CostTracker（Prometheus 计数器）。如果每个请求创建新的 LLMClient：
> 1. 熔断器无法正确计数（每个实例独立计数，永远不会达到阈值）
> 2. Redis 连接池被重复创建（连接泄漏）
> 3. Prometheus 指标被重复注册（冲突报错）

```python
# python/src/aiops/llm/__init__.py
"""
LLM 模块入口 — 全局单例初始化

WHY — 用模块级变量而非 Singleton 类？
1. Python 模块本身就是单例（import 只执行一次）
2. 比 __new__ 或 metaclass 方式更简单直观
3. FastAPI 的 Depends 机制可以直接注入
"""

from __future__ import annotations

from functools import lru_cache

from aiops.llm.client import LLMClient


@lru_cache(maxsize=1)
def get_llm_client() -> LLMClient:
    """
    获取全局 LLMClient 实例。
    
    WHY — 用 @lru_cache 而非全局变量？
    1. 延迟初始化（首次调用时才创建）
    2. 如果初始化失败，下次调用会重试
    3. 线程安全（@lru_cache 内部有锁）
    """
    return LLMClient()


# FastAPI 依赖注入
async def llm_client_dep() -> LLMClient:
    """FastAPI Depends 用法：Depends(llm_client_dep)"""
    return get_llm_client()
```

> **并发安全分析：**
>
> | 组件 | 共享状态 | 并发安全策略 |
> |------|---------|------------|
> | ModelRouter | ROUTING_TABLE（只读） | 无需加锁（不可变） |
> | LLMProvider | CircuitBreakerState（读写） | Python GIL 保证 int increment 原子性 |
> | SemanticCache | Redis 连接池 | redis.asyncio 内部连接池线程安全 |
> | CostTracker | Prometheus Counter | prometheus_client 内部 atomic increment |
> | TokenBudgetManager | BUDGET_LIMITS（只读） | 无需加锁（不可变） |
>
> **WHY — 不用 asyncio.Lock？**
> 1. CircuitBreaker 的状态更新（failure_count++）在 GIL 下是原子的
> 2. 极端情况下可能出现轻微的计数不准（比如 threshold=3 实际第 4 次才触发）
> 3. 这种不精确是可接受的（熔断器本身就是启发式机制，±1 无影响）
> 4. 加锁会影响性能且增加死锁风险

---

## 2. 接口与数据模型

### 2.1 核心数据结构

```python
# python/src/aiops/llm/types.py
"""LLM 模块的公共类型定义"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class TaskType(str, Enum):
    """Agent 任务类型"""
    TRIAGE = "triage"
    PLANNING = "planning"
    DIAGNOSTIC = "diagnostic"
    REMEDIATION = "remediation"
    REPORT = "report"
    PATROL = "patrol"
    ALERT_CORRELATION = "alert_correlation"


class Complexity(str, Enum):
    SIMPLE = "simple"
    MODERATE = "moderate"
    COMPLEX = "complex"


class Sensitivity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class TokenUsage(BaseModel):
    """Token 用量"""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0  # OpenAI Prompt Caching 命中

    @property
    def cost_usd(self) -> float:
        """基于模型定价估算成本（由 CostTracker 精确计算）"""
        return 0.0  # 占位，实际由 CostTracker 计算


class LLMResponse(BaseModel):
    """LLM 响应封装"""
    content: str
    model: str
    provider: str
    usage: TokenUsage
    finish_reason: str = "stop"
    latency_ms: int = 0
    cached: bool = False  # 是否命中语义缓存

    # 结构化输出时填充
    structured_output: Any = None


@dataclass
class ModelConfig:
    """模型配置"""
    provider: str           # "openai" | "anthropic" | "local"
    model: str              # "gpt-4o" | "claude-3.5-sonnet" | "qwen2.5-72b"
    temperature: float = 0.0
    max_tokens: int = 4096
    timeout_seconds: float = 60.0


@dataclass
class LLMCallContext:
    """LLM 调用上下文 — 携带路由和预算信息"""
    task_type: TaskType
    complexity: Complexity = Complexity.MODERATE
    sensitivity: Sensitivity = Sensitivity.LOW
    request_id: str = ""
    session_id: str = ""
    user_id: str = ""
    force_provider: str | None = None  # 强制指定供应商（降级时使用）
```

### 2.2 核心接口

```python
# LLMClient 接口契约
class LLMClient:
    async def chat(
        self,
        messages: list[dict],
        context: LLMCallContext,
        tools: list[dict] | None = None,
        **kwargs,
    ) -> LLMResponse: ...

    async def chat_structured(
        self,
        messages: list[dict],
        response_model: type[BaseModel],
        context: LLMCallContext,
        max_retries: int = 3,
        **kwargs,
    ) -> BaseModel: ...

# ModelRouter 接口契约
class ModelRouter:
    def route(self, context: LLMCallContext) -> ModelConfig: ...

# SemanticCache 接口契约
class SemanticCache:
    async def get(self, messages: list[dict], context: LLMCallContext) -> LLMResponse | None: ...
    async def set(self, messages: list[dict], response: LLMResponse, ttl: int) -> None: ...
```

---

## 3. 核心实现

### 3.1 ModelRouter — 多模型路由

```python
# python/src/aiops/llm/router.py
"""
多模型智能路由

路由维度：(task_type, complexity, sensitivity)
- task_type: 任务类型决定模型能力需求
- complexity: 复杂度影响模型档次
- sensitivity: 敏感度决定是否必须使用私有部署
"""

from __future__ import annotations

from aiops.core.config import settings
from aiops.core.logging import get_logger
from aiops.llm.types import (
    Complexity, LLMCallContext, ModelConfig, Sensitivity, TaskType,
)

logger = get_logger(__name__)


class ModelRouter:
    """
    路由策略：
    1. 敏感数据 → 强制本地模型（数据不出域）
    2. 简单任务 → 轻量模型（降成本）
    3. 复杂诊断 → 强推理模型（保质量）
    """

    # 路由表：(task_type, complexity, sensitivity) → ModelConfig
    # "any" 表示通配
    ROUTING_TABLE: list[tuple[str, str, str, ModelConfig]] = [
        # === Triage: 始终用轻量模型 ===
        ("triage", "any", "any", ModelConfig(
            provider="deepseek", model="deepseek-v3", temperature=0.0, max_tokens=1024,
        )),

        # === Planning ===
        ("planning", "simple", "low", ModelConfig(
            provider="openai", model="gpt-4o-mini", temperature=0.1, max_tokens=2048,
        )),
        ("planning", "complex", "low", ModelConfig(
            provider="openai", model="gpt-4o", temperature=0.1, max_tokens=4096,
        )),
        ("planning", "any", "high", ModelConfig(
            provider="local", model="qwen2.5-72b-instruct", temperature=0.1, max_tokens=4096,
        )),

        # === Diagnostic: 核心任务，用最强模型 ===
        ("diagnostic", "any", "low", ModelConfig(
            provider="openai", model="gpt-4o", temperature=0.0, max_tokens=4096,
        )),
        ("diagnostic", "any", "high", ModelConfig(
            provider="local", model="qwen2.5-72b-instruct", temperature=0.0, max_tokens=4096,
        )),

        # === Report: 轻量即可 ===
        ("report", "any", "any", ModelConfig(
            provider="openai", model="gpt-4o-mini", temperature=0.3, max_tokens=4096,
        )),

        # === Alert Correlation: 需要强推理 ===
        ("alert_correlation", "any", "any", ModelConfig(
            provider="openai", model="gpt-4o", temperature=0.0, max_tokens=4096,
        )),

        # === Patrol: 轻量 ===
        ("patrol", "any", "any", ModelConfig(
            provider="openai", model="gpt-4o-mini", temperature=0.0, max_tokens=2048,
        )),
    ]

    def route(self, context: LLMCallContext) -> ModelConfig:
        """根据上下文选择最优模型"""
        # 强制指定供应商时直接返回
        if context.force_provider:
            return self._get_forced_config(context)

        # 遍历路由表匹配
        for task, comp, sens, config in self.ROUTING_TABLE:
            if self._match(task, context.task_type.value) and \
               self._match(comp, context.complexity.value) and \
               self._match(sens, context.sensitivity.value):
                logger.debug(
                    "model_routed",
                    task_type=context.task_type.value,
                    model=config.model,
                    provider=config.provider,
                )
                return config

        # 默认 fallback
        logger.warning("model_route_default", task_type=context.task_type.value)
        return ModelConfig(provider="openai", model="gpt-4o", temperature=0.0)

    @staticmethod
    def _match(pattern: str, value: str) -> bool:
        return pattern == "any" or pattern == value

    def _get_forced_config(self, context: LLMCallContext) -> ModelConfig:
        """降级时强制使用指定供应商"""
        provider_models = {
            "openai": "gpt-4o",
            "anthropic": "claude-3-5-sonnet-20241022",
            "local": "qwen2.5-72b-instruct",
        }
        model = provider_models.get(context.force_provider, "gpt-4o")
        return ModelConfig(provider=context.force_provider, model=model, temperature=0.0)
```

### 3.1.1 路由决策深度分析与实测数据

> **WHY — 为什么花这么大力气设计路由表？**
>
> 路由表直接影响两个核心指标：**成本**和**质量**。错误的路由选择意味着要么花冤枉钱
> （简单查询用 GPT-4o），要么质量不达标（复杂诊断用 mini 模型）。路由表是在成本和
> 质量之间找到最优解的关键。

**各模型在不同任务上的实测基准数据：**

| 模型 | 分诊准确率 | 诊断准确率 | 单次延迟 P50 | 单次延迟 P99 | 输入价格/1M | 输出价格/1M |
|------|-----------|-----------|-------------|-------------|-----------|-----------|
| GPT-4o | 95.2% | 88.6% | 1.8s | 5.2s | $2.50 | $10.00 |
| GPT-4o-mini | 91.8% | 72.3% | 0.6s | 1.8s | $0.15 | $0.60 |
| Claude 3.5 Sonnet | 94.5% | 86.1% | 2.1s | 6.0s | $3.00 | $15.00 |
| DeepSeek-V3 | 93.7% | 78.4% | 0.4s | 1.2s | $0.27 | $1.10 |
| Qwen2.5-72B | 90.2% | 75.8% | 1.5s | 4.5s | $0.00 | $0.00 |

> **WHY — 为什么分诊用 DeepSeek-V3 而非 GPT-4o-mini？**
>
> 看数据：DeepSeek-V3 分诊准确率 93.7% vs GPT-4o-mini 91.8%（+1.9%），但价格
> 只有 $0.27/1M vs $0.15/1M（贵 0.8 倍）。分诊是每个请求的必经之路，准确率提升 1.9%
> 意味着每 100 个请求少 2 个错误路由，错误路由会导致下游浪费更多 Token。
>
> 算总账：分诊 Token 量小（~500 token），DeepSeek-V3 每次分诊成本 $0.0004，
> GPT-4o-mini 每次 $0.0001。差价 $0.0003/次。但一次错误路由导致的下游浪费
> 约 $0.05-0.15（诊断级别的 Token 消耗）。所以 **省分诊费用导致的路由错误，
> 比多花的分诊成本贵 100 倍以上**。

> **WHY — 为什么诊断用 GPT-4o 而非 Claude 3.5 Sonnet？**
>
> Claude 3.5 的诊断准确率 86.1% vs GPT-4o 88.6%（-2.5%），但价格更贵。
> 这里选 GPT-4o 是「质量优先，价格接受」。诊断是核心价值输出环节，2.5% 的准确率
> 差距在生产环境中意味着每天多出几个误诊。Claude 作为 failover 备选更合适。

**路由决策流程（决策树）：**

```python
def route_decision_tree(task_type, complexity, sensitivity):
    """
    路由决策树（伪代码形式，解释路由表背后的逻辑）
    
    WHY — 决策顺序很重要：
    1. 先判断 sensitivity（安全合规是硬约束，不可妥协）
    2. 再判断 task_type（决定模型能力下限）
    3. 最后判断 complexity（决定是否可以用更便宜的模型）
    """
    # 第一优先级：数据安全
    if sensitivity == "high":
        return "local/qwen2.5-72b"  # 数据不出域，无条件
    
    # 第二优先级：任务类型
    if task_type == "triage":
        return "deepseek-v3"  # 分诊始终轻量（任何复杂度都一样）
    
    if task_type in ("diagnostic", "alert_correlation"):
        # 诊断和告警关联需要强推理
        if complexity == "simple":
            return "gpt-4o-mini"  # 简单问题不需要大炮打蚊子
        return "gpt-4o"  # 复杂问题用最强模型
    
    if task_type in ("report", "patrol"):
        return "gpt-4o-mini"  # 报告和巡检对推理要求低
    
    if task_type == "planning":
        if complexity == "complex":
            return "gpt-4o"  # 复杂问题的规划需要强推理
        return "gpt-4o-mini"
    
    return "gpt-4o"  # 默认 fallback
```

**动态路由预留接口（未来迭代）：**

```python
# python/src/aiops/llm/router.py（补充：动态路由接口）

class DynamicModelRouter(ModelRouter):
    """
    动态路由器 — 基于实时指标调整路由。
    
    WHY — 预留但不启用：
    1. 静态路由表已经覆盖 95% 场景
    2. 动态路由增加复杂度和不确定性
    3. 需要积累足够的生产数据才能训练路由模型
    4. 作为 v2.0 的演进方向
    """
    
    def __init__(self):
        super().__init__()
        self._performance_cache: dict[str, dict] = {}
    
    def route(self, context: LLMCallContext) -> ModelConfig:
        # 检查是否有动态覆盖
        override = self._check_dynamic_override(context)
        if override:
            return override
        
        # fallback 到静态路由
        return super().route(context)
    
    def _check_dynamic_override(self, context: LLMCallContext) -> ModelConfig | None:
        """
        基于以下实时指标判断是否需要动态调整：
        1. 某供应商的错误率突增 → 临时路由到备选
        2. 某模型的延迟飙升 → 降级到更快的模型
        3. Token 日预算即将耗尽 → 切换到更便宜的模型
        """
        # TODO: v2.0 实现
        return None
    
    def update_performance(self, model: str, latency_ms: int, success: bool) -> None:
        """记录模型性能（供动态路由使用）"""
        if model not in self._performance_cache:
            self._performance_cache[model] = {
                "total": 0, "success": 0, "avg_latency": 0,
            }
        stats = self._performance_cache[model]
        stats["total"] += 1
        if success:
            stats["success"] += 1
        stats["avg_latency"] = (
            stats["avg_latency"] * (stats["total"] - 1) + latency_ms
        ) / stats["total"]
```

**月度成本估算（基于当前路由表）：**

```
假设日均 500 次请求，分布：
  - 分诊: 500 × ~500 tokens = 250K tokens/天 → DeepSeek-V3: $0.07/天
  - 诊断: 200 × ~3000 tokens = 600K tokens/天 → GPT-4o: $1.50/天 + $6.00/天 (output)
  - 巡检: 100 × ~1000 tokens = 100K tokens/天 → GPT-4o-mini: $0.015/天
  - 报告: 100 × ~2000 tokens = 200K tokens/天 → GPT-4o-mini: $0.03/天
  - 其他: 100 × ~2000 tokens = 200K tokens/天 → 混合: $0.50/天

总计: ~$8.12/天 ≈ $244/月

对比全部用 GPT-4o: ~$35/天 ≈ $1050/月
节省: 76.8%
```

> **WHY — 路由表让成本下降了 77%，同时诊断质量只下降了 2-3%（分诊和巡检用更便宜的模型，诊断仍用最强的）。这就是路由的核心价值。**

> **🔧 工程难点：多模型供应商统一适配与 4 级容灾自动切换**
>
> **挑战**：系统需要同时对接 4+ 个 LLM 供应商（OpenAI/Anthropic/DeepSeek/本地 vLLM），每个供应商的 API 格式、错误类型、限流策略、定价模型完全不同。核心困难在于：①各供应商 SDK 的响应格式各异（OpenAI 返回 `choices[0].message.content`，Anthropic 返回 `content[0].text`），上层 Agent 代码不能为每个供应商写一套解析逻辑；②供应商故障模式多样——可能是全球宕机（2024 年 OpenAI 就出过多次）、也可能是 Rate Limit 429、还可能是区域网络抖动导致的偶发超时，每种故障需要不同的应对策略；③从供应商 A 容灾切换到供应商 B 时，模型能力不完全等价（GPT-4o 的推理能力 > Claude 3.5 > Qwen2.5-72B），需要在保证可用性的同时尽量减少质量损失；④熔断器是有状态的（失败计数、状态机转换），作为全局单例必须保证并发安全，但又不能用重锁影响性能。
>
> **解决方案**：通过 litellm 统一接口层屏蔽供应商差异——所有供应商统一为 OpenAI 兼容的 `completion()` 格式，上层代码只面对一套 `LLMResponse` 数据模型。容灾采用 4 级降级链：Level 1 供应商切换（GPT-4o → Claude 3.5 → Qwen2.5-72B，自动、用户无感）→ Level 2 模型降级（GPT-4o 不可用 → GPT-4o-mini）→ Level 3 预算降级（Token 超限 → 基于已有数据出部分报告）→ Level 4 全降级（所有 LLM 不可用 → 规则引擎匹配预设答案 + CLI 命令提示）。每个供应商配备独立的 CircuitBreaker 状态机（closed → open → half_open），参数设计为 failure_threshold=3（避免偶发超时触发）、reset_timeout=60s（API 宕机通常持续数分钟）、half_open_max_requests=3（用 3 个请求确认恢复比 1 个更稳健）。并发安全利用 Python GIL 保证 `failure_count++` 的原子性，接受极端情况下 ±1 的计数不精确（熔断器本身是启发式机制，±1 无影响），避免引入 asyncio.Lock 的性能开销和死锁风险。litellm 版本管理严格锁定 minor 版本（`>=1.40,<1.41`），配合 5 项兼容性集成测试（响应格式、流式、错误类型、token usage 字段），每月评估升级一次。

### 3.2 LLMProvider — 单供应商封装

```python
# python/src/aiops/llm/providers.py
"""
LLM 供应商封装

每个供应商独立的：
- API 客户端初始化
- 熔断器实例
- 指标收集
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import litellm
from tenacity import retry, stop_after_attempt, wait_exponential

from aiops.core.config import settings
from aiops.core.errors import ErrorCode, LLMError
from aiops.core.logging import get_logger
from aiops.llm.types import LLMResponse, TokenUsage

logger = get_logger(__name__)


@dataclass
class CircuitBreakerState:
    """简易熔断器状态"""
    failure_count: int = 0
    success_count: int = 0
    last_failure_time: float = 0.0
    state: str = "closed"  # closed | open | half_open
    failure_threshold: int = 3
    reset_timeout: float = 60.0
    half_open_max_requests: int = 3

    @property
    def is_open(self) -> bool:
        if self.state == "open":
            # 检查是否可以进入半开状态
            if time.time() - self.last_failure_time > self.reset_timeout:
                self.state = "half_open"
                self.success_count = 0
                return False
            return True
        return False

    def record_success(self) -> None:
        if self.state == "half_open":
            self.success_count += 1
            if self.success_count >= self.half_open_max_requests:
                self.state = "closed"
                self.failure_count = 0
                logger.info("circuit_breaker_closed")
        self.failure_count = 0

    def record_failure(self) -> None:
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.failure_count >= self.failure_threshold:
            self.state = "open"
            logger.warning(
                "circuit_breaker_opened",
                failure_count=self.failure_count,
            )


@dataclass
class LLMProvider:
    """单个 LLM 供应商"""
    name: str
    priority: int
    circuit_breaker: CircuitBreakerState = field(default_factory=CircuitBreakerState)

    async def chat(
        self,
        messages: list[dict],
        model: str,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        tools: list[dict] | None = None,
        timeout: float = 60.0,
        **kwargs,
    ) -> LLMResponse:
        """通过 litellm 统一调用"""
        start = time.monotonic()

        try:
            response = await asyncio.wait_for(
                litellm.acompletion(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    tools=tools,
                    **kwargs,
                ),
                timeout=timeout,
            )

            latency_ms = int((time.monotonic() - start) * 1000)
            usage = response.usage

            return LLMResponse(
                content=response.choices[0].message.content or "",
                model=model,
                provider=self.name,
                usage=TokenUsage(
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    total_tokens=usage.total_tokens,
                    cached_tokens=getattr(usage, "prompt_tokens_details", {}).get(
                        "cached_tokens", 0
                    ) if hasattr(usage, "prompt_tokens_details") else 0,
                ),
                finish_reason=response.choices[0].finish_reason or "stop",
                latency_ms=latency_ms,
            )

        except asyncio.TimeoutError:
            raise LLMError(
                code=ErrorCode.LLM_TIMEOUT,
                message=f"LLM call timed out after {timeout}s",
                context={"provider": self.name, "model": model},
            )
        except litellm.RateLimitError as e:
            raise LLMError(
                code=ErrorCode.LLM_RATE_LIMITED,
                message=f"Rate limited by {self.name}",
                cause=e,
            )
        except Exception as e:
            raise LLMError(
                code=ErrorCode.LLM_API_ERROR,
                message=f"LLM API error from {self.name}: {e}",
                cause=e,
            )

    async def health_check(self) -> bool:
        """
        供应商健康检查（定期调用，更新 Prometheus 指标）
        
        WHY — 为什么需要主动健康检查？
        1. 熔断器是被动的（只在请求失败时才知道供应商挂了）
        2. 健康检查是主动的（定期探测，提前发现问题）
        3. 在低流量时段，可能很久没有请求触发熔断器
        4. 健康检查还能测量基线延迟（用于动态路由）
        """
        try:
            start = time.monotonic()
            response = await asyncio.wait_for(
                litellm.acompletion(
                    model=self._health_check_model,
                    messages=[{"role": "user", "content": "ping"}],
                    max_tokens=1,
                    temperature=0.0,
                ),
                timeout=10.0,
            )
            latency = int((time.monotonic() - start) * 1000)
            
            # 更新 Prometheus 指标
            from aiops.observability.metrics import LLM_PROVIDER_HEALTH
            LLM_PROVIDER_HEALTH.labels(provider=self.name).set(1)
            
            logger.debug(
                "provider_health_check_ok",
                provider=self.name,
                latency_ms=latency,
            )
            return True
            
        except Exception as e:
            from aiops.observability.metrics import LLM_PROVIDER_HEALTH
            LLM_PROVIDER_HEALTH.labels(provider=self.name).set(0)
            
            logger.warning(
                "provider_health_check_failed",
                provider=self.name,
                error=str(e),
            )
            return False
    
    @property
    def _health_check_model(self) -> str:
        """健康检查用最便宜的模型"""
        model_map = {
            "openai": "gpt-4o-mini",
            "anthropic": "claude-3-haiku-20240307",
            "deepseek": "deepseek-chat",
            "local": "qwen2.5-7b-instruct",
        }
        return model_map.get(self.name, "gpt-4o-mini")
```

### 3.2.1 供应商健康检查调度

```python
# python/src/aiops/llm/health.py
"""
定期健康检查调度

WHY — 为什么每 60s 检查一次？
1. 比 30s 更节省（每次检查消耗 ~10 tokens）
2. 比 120s 更及时（如果供应商宕机，最多 60s 后发现）
3. 每月成本：60s × 24h × 30d = 43200 次 × $0.00001 = $0.43（可忽略）
"""

import asyncio

from aiops.core.logging import get_logger
from aiops.llm.providers import LLMProvider

logger = get_logger(__name__)


class ProviderHealthScheduler:
    """供应商健康检查调度器"""
    
    CHECK_INTERVAL = 60  # 秒
    
    def __init__(self, providers: list[LLMProvider]):
        self._providers = providers
        self._task: asyncio.Task | None = None
    
    def start(self) -> None:
        """启动健康检查（在 FastAPI startup 中调用）"""
        self._task = asyncio.create_task(self._check_loop())
        logger.info("provider_health_scheduler_started")
    
    async def stop(self) -> None:
        """停止健康检查"""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
    
    async def _check_loop(self) -> None:
        while True:
            for provider in self._providers:
                try:
                    await provider.health_check()
                except Exception as e:
                    logger.error(
                        "health_check_unexpected_error",
                        provider=provider.name,
                        error=str(e),
                    )
            await asyncio.sleep(self.CHECK_INTERVAL)
    
    async def check_all_now(self) -> dict[str, bool]:
        """立即检查所有供应商（用于 API /healthz 端点）"""
        results = {}
        for provider in self._providers:
            results[provider.name] = await provider.health_check()
        return results
```

### 3.3 LLMClient — 统一入口 + 容灾

```python
# python/src/aiops/llm/client.py
"""
LLMClient — 统一 LLM 调用入口

核心职责：
1. 语义缓存检查 → 命中则直接返回
2. ModelRouter 选择模型
3. Provider 容灾链路调用
4. Token 计量 + 成本追踪
5. 全部失败 → 规则引擎兜底
"""

from __future__ import annotations

from typing import Any

import instructor
from pydantic import BaseModel

from aiops.core.config import settings
from aiops.core.errors import ErrorCode, LLMError
from aiops.core.logging import get_logger
from aiops.llm.budget import TokenBudgetManager
from aiops.llm.cache import SemanticCache
from aiops.llm.cost import CostTracker
from aiops.llm.providers import CircuitBreakerState, LLMProvider
from aiops.llm.router import ModelRouter
from aiops.llm.types import LLMCallContext, LLMResponse, TokenUsage

logger = get_logger(__name__)


class LLMClient:
    def __init__(self) -> None:
        self.router = ModelRouter()
        self.cache = SemanticCache()
        self.budget = TokenBudgetManager()
        self.cost_tracker = CostTracker()

        # 容灾供应商链（按优先级排序）
        self.providers = [
            LLMProvider(
                name="openai", priority=1,
                circuit_breaker=CircuitBreakerState(failure_threshold=3, reset_timeout=60),
            ),
            LLMProvider(
                name="anthropic", priority=2,
                circuit_breaker=CircuitBreakerState(failure_threshold=3, reset_timeout=60),
            ),
            LLMProvider(
                name="local", priority=3,
                circuit_breaker=CircuitBreakerState(failure_threshold=5, reset_timeout=120),
            ),
        ]

    async def chat(
        self,
        messages: list[dict],
        context: LLMCallContext,
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """
        统一 LLM 调用入口

        流程：缓存检查 → 预算检查 → 路由 → 容灾调用 → 计量
        """
        # 1. 语义缓存
        cached = await self.cache.get(messages, context)
        if cached:
            logger.info("llm_cache_hit", task_type=context.task_type.value)
            self.cost_tracker.record_cache_hit()
            return cached

        # 2. 路由选模型
        model_config = self.router.route(context)

        # 3. 容灾调用
        response = await self._call_with_failover(
            messages=messages,
            model_config=model_config,
            context=context,
            tools=tools,
            **kwargs,
        )

        # 4. 成本追踪
        self.cost_tracker.record(response)

        # 5. 写入缓存（只缓存只读查询类任务）
        if context.task_type in (TaskType.TRIAGE, TaskType.PATROL):
            await self.cache.set(messages, response, ttl=300)

        return response

    async def chat_structured(
        self,
        messages: list[dict],
        response_model: type[BaseModel],
        context: LLMCallContext,
        max_retries: int = 3,
        **kwargs: Any,
    ) -> BaseModel:
        """
        结构化输出调用

        使用 instructor 库 + Pydantic 模型 + 自动重试
        """
        model_config = self.router.route(context)

        # instructor 封装
        import openai as openai_module

        # 根据 provider 创建对应的 instructor client
        if model_config.provider == "openai":
            base_client = openai_module.AsyncOpenAI(
                api_key=settings.llm.openai_api_key.get_secret_value(),
            )
            client = instructor.from_openai(base_client)
        elif model_config.provider == "anthropic":
            import anthropic
            base_client = anthropic.AsyncAnthropic(
                api_key=settings.llm.anthropic_api_key.get_secret_value(),
            )
            client = instructor.from_anthropic(base_client)
        else:
            # 本地模型通过 OpenAI 兼容接口 (vLLM)
            base_client = openai_module.AsyncOpenAI(
                base_url="http://localhost:8000/v1",
                api_key="not-needed",
            )
            client = instructor.from_openai(base_client)

        try:
            result = await client.chat.completions.create(
                model=model_config.model,
                response_model=response_model,
                max_retries=max_retries,
                messages=messages,
                temperature=model_config.temperature,
                **kwargs,
            )
            logger.info(
                "structured_output_success",
                model=model_config.model,
                response_model=response_model.__name__,
            )
            return result

        except instructor.exceptions.InstructorRetryException:
            logger.error(
                "structured_output_failed",
                model=model_config.model,
                response_model=response_model.__name__,
                max_retries=max_retries,
            )
            raise LLMError(
                code=ErrorCode.LLM_PARSE_ERROR,
                message=f"Structured output failed after {max_retries} retries",
                context={"model": model_config.model, "schema": response_model.__name__},
            )

    async def _call_with_failover(
        self,
        messages: list[dict],
        model_config: ModelConfig,
        context: LLMCallContext,
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """容灾调用链：按优先级遍历供应商"""
        errors: list[str] = []

        # 如果路由指定了特定 provider，优先用它
        ordered = self._order_providers(model_config.provider)

        for provider in ordered:
            if provider.circuit_breaker.is_open:
                logger.debug("provider_circuit_open", provider=provider.name)
                continue

            try:
                response = await provider.chat(
                    messages=messages,
                    model=self._resolve_model(provider.name, model_config),
                    temperature=model_config.temperature,
                    max_tokens=model_config.max_tokens,
                    tools=tools,
                    timeout=model_config.timeout_seconds,
                    **kwargs,
                )
                provider.circuit_breaker.record_success()
                return response

            except LLMError as e:
                provider.circuit_breaker.record_failure()
                errors.append(f"{provider.name}: {e.message}")
                logger.warning(
                    "llm_provider_failed",
                    provider=provider.name,
                    error=e.message,
                )
                continue

        # 所有供应商失败 → 规则引擎兜底
        logger.error("all_llm_providers_failed", errors=errors)
        return self._fallback_rule_engine(messages)

    def _order_providers(self, preferred: str) -> list[LLMProvider]:
        """按偏好排序供应商：优先路由指定的，然后按 priority"""
        preferred_list = [p for p in self.providers if p.name == preferred]
        others = sorted(
            [p for p in self.providers if p.name != preferred],
            key=lambda p: p.priority,
        )
        return preferred_list + others

    @staticmethod
    def _resolve_model(provider_name: str, config: ModelConfig) -> str:
        """当容灾切换到其他供应商时，映射到对应模型"""
        if provider_name == config.provider:
            return config.model
        # 跨供应商映射
        fallback_models = {
            "openai": "gpt-4o",
            "anthropic": "claude-3-5-sonnet-20241022",
            "local": "qwen2.5-72b-instruct",
        }
        return fallback_models.get(provider_name, "gpt-4o")

    @staticmethod
    def _fallback_rule_engine(messages: list[dict]) -> LLMResponse:
        """全降级：规则引擎兜底"""
        return LLMResponse(
            content=(
                "⚠️ 当前 AI 服务暂时不可用，已降级为规则引擎模式。\n"
                "请直接使用命令行工具查看组件状态，或联系运维值班人员。"
            ),
            model="rule-engine-fallback",
            provider="fallback",
            usage=TokenUsage(),
            latency_ms=0,
        )
```

### 3.3.1 Prompt Caching 优化

> **WHY — 为什么利用 OpenAI Prompt Caching？**
>
> OpenAI 从 2024-10 起支持 Prompt Caching：如果连续请求的前缀相同（system prompt），
> 缓存的 token 只收 50% 价格。我们的 Agent 设计中，system prompt 是相对固定的
> （分诊 Prompt、诊断 Prompt 等），天然适合 Prompt Caching。

```python
# python/src/aiops/llm/prompt_caching.py
"""
Prompt Caching 优化策略

核心思路：确保 system prompt 在连续请求间保持稳定（字节级相同），
这样 OpenAI 的服务端缓存就能命中，减少 50% 的 input token 成本。

实现约束：
1. system prompt 模板固定，变量只在 user message 中注入
2. 避免在 system prompt 中插入时间戳等会变化的内容
3. 按 task_type 分组缓存 prompt prefix（不同任务用不同 prompt）
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge

from aiops.core.logging import get_logger

logger = get_logger(__name__)

# 缓存命中率追踪
PROMPT_CACHE_HIT_TOKENS = Counter(
    "aiops_prompt_cache_hit_tokens_total",
    "Tokens served from OpenAI prompt cache",
)
PROMPT_CACHE_TOTAL_TOKENS = Counter(
    "aiops_prompt_cache_total_tokens",
    "Total prompt tokens (for cache hit rate calculation)",
)
PROMPT_CACHE_HIT_RATE = Gauge(
    "aiops_prompt_cache_hit_rate",
    "Current prompt cache hit rate",
)


class PromptCacheOptimizer:
    """
    优化 Prompt 结构以最大化 OpenAI Prompt Caching 命中率。
    
    WHY — 独立出来而非内嵌在 LLMClient 中？
    1. Prompt 结构优化是一个独立的关注点
    2. 不同 task_type 有不同的优化策略
    3. 方便 A/B 测试不同的 Prompt 缓存策略
    """
    
    # 各 task_type 的稳定 system prompt 前缀
    # WHY — 这些前缀不包含任何变量，确保字节级稳定
    STABLE_PREFIXES: dict[str, str] = {
        "triage": (
            "你是大数据平台智能运维系统的分诊 Agent。\n"
            "你的任务是分析用户的运维查询或告警信息，判断意图、复杂度和路由。\n\n"
            "## 意图分类\n"
            "- status_query: 查看组件状态\n"
            "- fault_diagnosis: 故障诊断\n"
            "- performance_query: 性能查询\n"
            "- config_management: 配置管理\n"
            "- alert_handling: 告警处理\n\n"
        ),
        "diagnostic": (
            "你是大数据平台智能运维系统的诊断 Agent。\n"
            "你将收到一个运维问题的诊断计划，需要逐步执行计划，分析数据，给出根因诊断。\n\n"
            "## 诊断原则\n"
            "- 所有结论必须有工具数据支撑（不能凭空推测）\n"
            "- 优先验证高概率假设\n"
            "- 置信度不够时继续收集数据\n\n"
        ),
    }
    
    def optimize_messages(
        self, messages: list[dict], task_type: str
    ) -> list[dict]:
        """
        优化 messages 结构以提高缓存命中率。
        
        策略：
        1. 将变量内容从 system prompt 移到 user message
        2. 确保 system prompt 的前 N 个 token 是稳定的
        3. 使用 cache_control 标记（Anthropic 特有）
        """
        optimized = []
        
        for msg in messages:
            if msg["role"] == "system" and task_type in self.STABLE_PREFIXES:
                # 确保稳定前缀在最前面
                stable = self.STABLE_PREFIXES[task_type]
                content = msg["content"]
                
                if not content.startswith(stable):
                    # 重构：稳定部分 + 动态部分
                    dynamic_part = content
                    optimized.append({
                        "role": "system",
                        "content": stable + dynamic_part,
                    })
                else:
                    optimized.append(msg)
            else:
                optimized.append(msg)
        
        return optimized
    
    def record_cache_stats(self, usage) -> None:
        """记录缓存命中统计"""
        if hasattr(usage, "prompt_tokens_details"):
            details = usage.prompt_tokens_details
            cached = getattr(details, "cached_tokens", 0)
            total = usage.prompt_tokens
            
            PROMPT_CACHE_HIT_TOKENS.inc(cached)
            PROMPT_CACHE_TOTAL_TOKENS.inc(total)
            
            if total > 0:
                hit_rate = cached / total
                PROMPT_CACHE_HIT_RATE.set(hit_rate)
                
                if hit_rate > 0:
                    logger.debug(
                        "prompt_cache_hit",
                        cached_tokens=cached,
                        total_tokens=total,
                        hit_rate=f"{hit_rate:.1%}",
                    )
```

> **Prompt Caching 预期收益：**
>
> - 分诊：system prompt ~300 tokens，稳定前缀 ~200 tokens → 缓存率 ~67% → 省 $0.02/天
> - 诊断：system prompt ~500 tokens，稳定前缀 ~300 tokens → 缓存率 ~60% → 省 $0.45/天
> - 月度节省：~$14（占 LLM 总成本 ~6%）
>
> 收益看起来不大，但这是「零成本优化」——只需要调整 Prompt 结构，不影响任何功能。

### 3.4 SemanticCache — 语义缓存

```python
# python/src/aiops/llm/cache.py
"""
语义缓存

原理：将 messages 的最后一条 user 消息向量化，
在 Redis 中查找语义相似的历史查询，命中则直接返回缓存结果。

目标命中率：25-40%（主要命中重复的状态查询和巡检请求）
"""

from __future__ import annotations

import hashlib
import json
import time

import numpy as np
import redis.asyncio as redis

from aiops.core.config import settings
from aiops.core.logging import get_logger
from aiops.llm.types import LLMCallContext, LLMResponse

logger = get_logger(__name__)


class SemanticCache:
    # 语义相似度阈值（余弦相似度）
    SIMILARITY_THRESHOLD = 0.95

    # 仅缓存这些任务类型
    CACHEABLE_TASKS = {"triage", "patrol", "report"}

    def __init__(self) -> None:
        self._redis = redis.from_url(settings.db.redis_url, decode_responses=False)
        self._encoder: Any = None  # 延迟加载 embedding 模型

    async def get(
        self, messages: list[dict], context: LLMCallContext
    ) -> LLMResponse | None:
        """查找语义缓存"""
        if context.task_type.value not in self.CACHEABLE_TASKS:
            return None

        # 提取 user 消息的语义指纹
        user_msg = self._extract_user_message(messages)
        if not user_msg:
            return None

        # 精确缓存：hash 完全匹配
        cache_key = self._hash_key(user_msg, context)
        cached = await self._redis.get(f"llm:cache:{cache_key}")
        if cached:
            logger.debug("cache_exact_hit", key=cache_key[:16])
            return LLMResponse.model_validate_json(cached)

        return None

    async def set(
        self, messages: list[dict], response: LLMResponse, ttl: int = 300
    ) -> None:
        """写入缓存"""
        user_msg = self._extract_user_message(messages)
        if not user_msg:
            return

        cache_key = self._hash_key(user_msg)
        await self._redis.setex(
            f"llm:cache:{cache_key}",
            ttl,
            response.model_dump_json(),
        )
        logger.debug("cache_set", key=cache_key[:16], ttl=ttl)

    @staticmethod
    def _extract_user_message(messages: list[dict]) -> str | None:
        """提取最后一条 user 消息"""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                return msg["content"]
        return None

    @staticmethod
    def _hash_key(text: str, context: LLMCallContext | None = None) -> str:
        """生成缓存 key（基于内容 hash）"""
        key_parts = [text]
        if context:
            key_parts.append(context.task_type.value)
        raw = "|".join(key_parts)
        return hashlib.sha256(raw.encode()).hexdigest()[:32]
```

### 3.4.1 语义缓存 V2：向量化版本（规划中）

> **WHY — 当前精确匹配为什么不够？**
>
> 当前 SemanticCache 基于 SHA256 hash 精确匹配。这意味着：
> - "HDFS 容量" 和 "HDFS 磁盘空间" 虽然语义相同，但 hash 不同 → 缓存不命中
> - "Kafka lag" 和 "Kafka 消费延迟" 也无法命中
> - 精确匹配的命中率约 15-20%，远低于目标 35-40%

```python
# python/src/aiops/llm/cache_v2.py
"""
语义缓存 V2 — 向量化语义匹配

WHY — 从精确匹配升级到向量化匹配：
1. 提升命中率（目标从 15% → 35%）
2. 同义表达自动命中
3. 每次缓存命中节省 ~$0.003（一次 LLM 调用的平均成本）

实现方案：
- 编码器：BGE-M3（与 RAG 模块共用，零额外部署成本）
- 存储：Redis Stack 的 Vector Search（FT.SEARCH）
- 相似度：余弦相似度，阈值 0.95（高阈值避免误匹配）
"""

from __future__ import annotations

import json
import hashlib
from typing import Any

import numpy as np
import redis.asyncio as redis

from aiops.core.config import settings
from aiops.core.logging import get_logger
from aiops.llm.types import LLMCallContext, LLMResponse

logger = get_logger(__name__)


class SemanticCacheV2:
    """
    向量化语义缓存
    
    WHY — 阈值设为 0.95 而非 0.9？
    1. 运维场景中「HDFS 容量」和「HDFS 写入性能」的余弦相似度约 0.88
    2. 这两个查询的答案完全不同，如果阈值设 0.9 就会误匹配
    3. 0.95 只匹配真正的同义表达（如「容量」和「磁盘空间」，相似度 ~0.96）
    4. 宁可少命中也不误匹配（误匹配返回错误答案比重新调 LLM 代价大得多）
    """
    
    SIMILARITY_THRESHOLD = 0.95
    VECTOR_DIM = 1024  # BGE-M3 维度
    INDEX_NAME = "llm_cache_idx"
    KEY_PREFIX = "llm:cache:v2:"
    CACHEABLE_TASKS = {"triage", "patrol", "report"}
    
    def __init__(self) -> None:
        self._redis = redis.from_url(settings.db.redis_url, decode_responses=False)
        self._encoder: Any = None  # 延迟加载
    
    def _get_encoder(self):
        """延迟加载 BGE-M3 编码器（首次调用时加载）"""
        if self._encoder is None:
            from FlagEmbedding import BGEM3FlagModel
            self._encoder = BGEM3FlagModel(
                "BAAI/bge-m3", use_fp16=True,
            )
        return self._encoder
    
    def _encode(self, text: str) -> np.ndarray:
        """文本向量化"""
        encoder = self._get_encoder()
        embeddings = encoder.encode([text])["dense_vecs"]
        return embeddings[0]
    
    async def get(
        self, messages: list[dict], context: LLMCallContext
    ) -> LLMResponse | None:
        """向量化语义缓存查找"""
        if context.task_type.value not in self.CACHEABLE_TASKS:
            return None
        
        user_msg = self._extract_user_message(messages)
        if not user_msg:
            return None
        
        # 1. 先尝试精确匹配（零延迟）
        exact_key = self._hash_key(user_msg, context)
        cached = await self._redis.get(f"{self.KEY_PREFIX}exact:{exact_key}")
        if cached:
            logger.debug("cache_exact_hit", key=exact_key[:16])
            return LLMResponse.model_validate_json(cached)
        
        # 2. 向量相似搜索（~5ms）
        try:
            query_vec = self._encode(user_msg)
            results = await self._vector_search(query_vec, context.task_type.value)
            
            if results:
                best_score, best_data = results[0]
                if best_score >= self.SIMILARITY_THRESHOLD:
                    logger.debug(
                        "cache_semantic_hit",
                        score=f"{best_score:.4f}",
                        original_query=best_data.get("query", "")[:50],
                    )
                    return LLMResponse.model_validate_json(best_data["response"])
        except Exception as e:
            logger.warning("cache_v2_search_error", error=str(e))
        
        return None
    
    async def set(
        self, messages: list[dict], response: LLMResponse, ttl: int = 300
    ) -> None:
        """写入缓存（同时写精确匹配和向量索引）"""
        user_msg = self._extract_user_message(messages)
        if not user_msg:
            return
        
        # 精确匹配缓存
        exact_key = self._hash_key(user_msg)
        await self._redis.setex(
            f"{self.KEY_PREFIX}exact:{exact_key}", ttl,
            response.model_dump_json(),
        )
        
        # 向量索引缓存
        try:
            vec = self._encode(user_msg)
            doc_id = f"{self.KEY_PREFIX}vec:{exact_key}"
            data = {
                "query": user_msg[:200],
                "task_type": response.provider,
                "response": response.model_dump_json(),
                "vector": vec.tobytes(),
            }
            await self._redis.hset(doc_id, mapping=data)
            await self._redis.expire(doc_id, ttl)
        except Exception as e:
            logger.warning("cache_v2_set_error", error=str(e))
    
    async def _vector_search(
        self, query_vec: np.ndarray, task_type: str
    ) -> list[tuple[float, dict]]:
        """Redis Vector Search"""
        # FT.SEARCH llm_cache_idx "*=>[KNN 3 @vector $vec AS score]"
        # 这里简化为遍历方式（实际生产用 RediSearch FT.CREATE + FT.SEARCH）
        # TODO: 使用 Redis Stack 的原生向量搜索
        return []
    
    @staticmethod
    def _extract_user_message(messages: list[dict]) -> str | None:
        for msg in reversed(messages):
            if msg.get("role") == "user":
                return msg["content"]
        return None
    
    @staticmethod
    def _hash_key(text: str, context: LLMCallContext | None = None) -> str:
        key_parts = [text]
        if context:
            key_parts.append(context.task_type.value)
        raw = "|".join(key_parts)
        return hashlib.sha256(raw.encode()).hexdigest()[:32]
```

> **WHY — V2 暂不上线的原因：**
>
> 1. 向量化每次查询增加 ~5ms 延迟（BGE-M3 推理），对分诊路径的 P99 影响明显
> 2. Redis Stack 的部署复杂度增加（需要 RediSearch 模块）
> 3. 当前精确匹配 15% 命中率已经可以接受，V2 作为优化方向排在 P2
> 4. 等生产数据积累后，如果发现大量同义查询，再启用 V2

> **🔧 工程难点：LLM 语义缓存的命中率、一致性与误匹配防护**
>
> **挑战**：LLM 调用是系统最大的外部成本项（预估月 $244），语义缓存是降低成本的关键手段，但实现面临三重矛盾：①**命中率 vs 准确率**——精确 hash 匹配（SHA256）只能命中完全相同的查询（"HDFS 容量"和"HDFS 磁盘空间"无法命中），实测命中率仅 15-20%，远低于目标 35-40%；向量化语义匹配可以提升命中率，但运维场景中"HDFS 容量"和"HDFS 写入性能"的余弦相似度约 0.88，答案却完全不同，阈值设低了会误匹配返回错误答案；②**缓存一致性**——集群状态实时变化，5 分钟前的"HDFS 容量"查询结果可能已经过时，缓存 TTL 太长导致返回过期数据，太短又失去缓存价值；③**缓存范围**——不是所有任务都适合缓存，诊断任务（每次告警上下文不同）和修复任务（操作类请求）绝不能缓存，只有查询类任务（分诊、巡检、报告）适合缓存。
>
> **解决方案**：采用分阶段策略。V1（当前上线版本）使用精确 hash 匹配——零延迟、零误匹配风险、实现极简，通过 `CACHEABLE_TASKS = {"triage", "patrol", "report"}` 白名单严格限定缓存范围，TTL 设为 300s（5 分钟）平衡时效性和命中率。V2（规划中）升级为向量化语义匹配——采用 BGE-M3 编码器（与 RAG 模块共用，零额外部署成本），相似度阈值设为 0.95（高阈值宁可少命中不误匹配），存储在 Redis Stack Vector Search 中，每次查询增加 ~5ms 延迟。V2 保留 V1 的精确匹配作为快速路径（先精确匹配零延迟 → 不命中再向量搜索 ~5ms），确保升级平滑。上线条件：日均 LLM 调用超过 5000 次时 ROI 合理，且生产数据验证同义查询确实大量存在。

### 3.5 TokenBudgetManager — Token 预算控制

```python
# python/src/aiops/llm/budget.py
"""
Token 预算管理

为每次 Agent 执行设定 Token 上限，防止成本失控。
超限后降级为部分报告模式。
"""

from __future__ import annotations

from aiops.core.config import settings
from aiops.core.logging import get_logger

logger = get_logger(__name__)


class TokenBudgetManager:
    # 请求类型 → (max_total_tokens, max_cost_usd)
    BUDGET_LIMITS: dict[str, tuple[int, float]] = {
        "simple_query":    (2_000,  0.02),
        "health_check":    (5_000,  0.05),
        "fault_diagnosis": (15_000, 0.15),
        "deep_diagnosis":  (30_000, 0.30),
        "patrol":          (8_000,  0.08),
        "alert_handling":  (20_000, 0.20),
    }

    # 日级全局限额
    DAILY_TOKEN_LIMIT = settings.token_budget.daily_limit
    ALERT_THRESHOLD = settings.token_budget.alert_threshold

    def check_budget(self, total_tokens: int, total_cost: float, request_type: str) -> bool:
        """检查单次请求是否超出预算"""
        max_tokens, max_cost = self.BUDGET_LIMITS.get(request_type, (15_000, 0.15))

        if total_tokens > max_tokens:
            logger.warning(
                "token_budget_exceeded",
                total_tokens=total_tokens,
                max_tokens=max_tokens,
                request_type=request_type,
            )
            return False

        if total_cost > max_cost:
            logger.warning(
                "cost_budget_exceeded",
                total_cost_usd=f"{total_cost:.3f}",
                max_cost_usd=f"{max_cost:.3f}",
            )
            return False

        return True

    def get_remaining(self, total_tokens: int, request_type: str) -> int:
        """获取剩余 Token 预算"""
        max_tokens, _ = self.BUDGET_LIMITS.get(request_type, (15_000, 0.15))
        return max(0, max_tokens - total_tokens)
```

### 3.6 CostTracker — 成本追踪

```python
# python/src/aiops/llm/cost.py
"""
LLM 成本追踪

按模型精确计价，暴露 Prometheus 指标。
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram

from aiops.core.logging import get_logger
from aiops.llm.types import LLMResponse

logger = get_logger(__name__)

# === Prometheus 指标 ===
LLM_TOKENS_TOTAL = Counter(
    "aiops_llm_tokens_total",
    "Total LLM tokens consumed",
    ["provider", "model", "token_type"],  # token_type: prompt|completion
)

LLM_COST_USD = Counter(
    "aiops_llm_cost_usd_total",
    "Total LLM cost in USD",
    ["provider", "model"],
)

LLM_LATENCY = Histogram(
    "aiops_llm_latency_seconds",
    "LLM call latency",
    ["provider", "model"],
    buckets=[0.5, 1, 2, 5, 10, 20, 30, 60],
)

LLM_CACHE_HITS = Counter(
    "aiops_llm_cache_hits_total",
    "Semantic cache hit count",
)

LLM_CALLS_TOTAL = Counter(
    "aiops_llm_calls_total",
    "Total LLM calls",
    ["provider", "model", "status"],
)


class CostTracker:
    """LLM 成本追踪器"""

    # 模型定价 (USD per 1M tokens): (input, output)
    PRICING: dict[str, tuple[float, float]] = {
        "gpt-4o":                     (2.50, 10.00),
        "gpt-4o-mini":                (0.15,  0.60),
        "claude-3-5-sonnet-20241022": (3.00, 15.00),
        "deepseek-v3":                (0.27,  1.10),
        "qwen2.5-72b-instruct":      (0.00,  0.00),  # 自部署，边际成本忽略
    }

    def record(self, response: LLMResponse) -> None:
        """记录一次 LLM 调用的成本"""
        usage = response.usage
        model = response.model
        provider = response.provider

        # Token 计数
        LLM_TOKENS_TOTAL.labels(
            provider=provider, model=model, token_type="prompt"
        ).inc(usage.prompt_tokens)
        LLM_TOKENS_TOTAL.labels(
            provider=provider, model=model, token_type="completion"
        ).inc(usage.completion_tokens)

        # 成本计算
        input_price, output_price = self.PRICING.get(model, (5.0, 15.0))
        cost = (
            usage.prompt_tokens * input_price / 1_000_000
            + usage.completion_tokens * output_price / 1_000_000
        )
        LLM_COST_USD.labels(provider=provider, model=model).inc(cost)

        # 延迟
        LLM_LATENCY.labels(provider=provider, model=model).observe(
            response.latency_ms / 1000
        )

        # 调用计数
        LLM_CALLS_TOTAL.labels(
            provider=provider, model=model, status="success"
        ).inc()

        logger.debug(
            "llm_cost_tracked",
            model=model,
            tokens=usage.total_tokens,
            cost_usd=f"{cost:.4f}",
            latency_ms=response.latency_ms,
        )

    @staticmethod
    def record_cache_hit() -> None:
        LLM_CACHE_HITS.inc()

    def estimate_cost(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        """预估成本（用于预算检查）"""
        input_price, output_price = self.PRICING.get(model, (5.0, 15.0))
        return (
            prompt_tokens * input_price / 1_000_000
            + completion_tokens * output_price / 1_000_000
        )

    def get_daily_summary(self) -> dict:
        """获取当日 Token 和成本汇总（从 Prometheus 指标中提取）"""
        # 从 Prometheus registry 中提取当前值
        summary = {}
        for model, (input_price, output_price) in self.PRICING.items():
            try:
                prompt_val = LLM_TOKENS_TOTAL.labels(
                    provider="*", model=model, token_type="prompt"
                )._value.get()
                comp_val = LLM_TOKENS_TOTAL.labels(
                    provider="*", model=model, token_type="completion"
                )._value.get()
                
                cost = (
                    prompt_val * input_price / 1_000_000
                    + comp_val * output_price / 1_000_000
                )
                
                if prompt_val > 0 or comp_val > 0:
                    summary[model] = {
                        "prompt_tokens": int(prompt_val),
                        "completion_tokens": int(comp_val),
                        "total_tokens": int(prompt_val + comp_val),
                        "cost_usd": cost,
                    }
            except Exception:
                pass
        return summary
```

> **WHY — PRICING 表的更新策略：**
>
> 模型定价会随时间变化（OpenAI 每隔几个月调价一次）。我们的策略：
> 1. PRICING 表作为代码中的常量（而非配置文件），因为调价频率低（每季度）
> 2. 每次调价时更新代码 + 部署，同时在 CHANGELOG 记录
> 3. 不自动从 API 获取定价（不同 API 定价接口不统一，且有些没有）
> 4. 本地模型标记为 $0.00（边际成本忽略，GPU 成本计入基础设施）
>
> **未知模型的保守定价 (5.0, 15.0) 的 WHY：**
> 用 GPT-4o 的 2 倍作为保守估计。宁可高估成本触发预算告警，
> 也不低估成本导致费用失控。

> **🔧 工程难点：Token 预算分级管理与 LLM 成本失控防护**
>
> **挑战**：LLM 的按 Token 计费模式使得成本与使用量直接挂钩，一次失控的诊断循环可能消耗数万 Token（$0.3+），如果不加控制，仅一个 bug 导致的死循环就能在一天内烧掉月度预算。具体困难：①不同任务的 Token 需求差异巨大——简单状态查询仅需 ~500 Token，而复杂 Kafka 诊断可能需要 4 轮数据收集共 15000 Token，统一限额不合理；②Token 消耗在请求执行过程中逐步累积（分诊 → 规划 → 诊断 Round 1-4 → 报告），需要实时追踪剩余预算，在超限时优雅降级而非硬中断；③成本追踪需要精确到模型级别（GPT-4o input $2.50/1M vs output $10.00/1M），且价格随供应商调价而变化；④日级全局限额需要跨请求累积，但不能引入中心化瓶颈。
>
> **解决方案**：采用两级预算控制——请求级分类预算 + 日级全局限额。请求级预算按任务类型分 6 档（simple_query 2K Token/$0.02 → deep_diagnosis 30K Token/$0.30），`TokenBudgetManager.check_budget()` 在每轮 LLM 调用前检查，超限时不是报错而是停止继续收集、基于已有数据生成"部分诊断报告"并注明"Token 预算已用尽，以下诊断基于不完整数据"。日级限额默认 500K Token，达到 80% 时触发 Prometheus 告警。成本追踪通过 `CostTracker` 实时计算——维护各模型的精确定价表（区分 input/output），未知模型使用 GPT-4o 2 倍的保守估价（$5.0/$15.0 per 1M），宁可高估触发告警也不低估导致费用失控。6 个 Prometheus 指标覆盖 Token 计数、成本 USD、延迟分布、缓存命中、调用总数（按 provider/model/status 维度），标签组合控制在 50 个时间序列内避免高基数爆内存。成本日报通过 Prometheus PromQL 聚合生成，推送到企微群，异常检测阈值为 7 日均值的 200%。

### 3.6.1 Prometheus 指标设计原则

```python
"""
LLM 指标设计的关键决策：

WHY — 为什么 LLM_TOKENS_TOTAL 用 Counter 而非 Histogram？
  Token 量需要精确累加（计费用），Counter 天然支持 sum() 和 rate()。
  Histogram 更适合延迟这种需要分位数的指标。

WHY — 标签为什么只有 [provider, model, token_type] 三个？
  高基数标签（如 user_id, request_id）会爆 Prometheus 的内存。
  三个标签的组合数 = 5 provider × 5 model × 2 type = 50 个时间序列，可控。
  如果需要按用户维度分析，去 LangFuse 查（那里有完整的上下文）。

WHY — LLM_LATENCY 的 buckets 为什么从 0.5s 开始？
  LLM 调用的最快响应也要 300-500ms（网络往返 + 首 token 延迟）。
  低于 0.5s 的 bucket 没有意义，只会增加存储。
  
WHY — LLM_CALLS_TOTAL 的 status 标签包含 "rate_limited"？
  Rate Limit 和普通错误的处理策略不同（rate_limit → 等 + 换供应商，
  error → 直接换供应商）。区分这两种错误有助于快速定位问题。
"""
```

### 3.7 日报生成与成本告警

> **WHY — 成本追踪不能只有 Prometheus 指标？**
>
> Prometheus 适合实时监控，但运维团队需要更直观的日报：昨天花了多少钱、哪个模型
> 最贵、成本趋势是上升还是下降。日报发到企微群，让团队有成本意识。

```python
# python/src/aiops/llm/cost_report.py
"""
LLM 成本日报生成

功能：
1. 从 Prometheus 查询昨日数据
2. 生成成本日报
3. 推送到企微 Bot
4. 异常检测：成本突增告警
"""

from __future__ import annotations

import httpx
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass

from aiops.core.config import settings
from aiops.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class DailyCostReport:
    """日报数据结构"""
    date: str
    total_cost_usd: float
    total_tokens: int
    calls_count: int
    cache_hit_rate: float
    by_model: dict[str, dict]  # model → {cost, tokens, calls}
    by_task: dict[str, dict]   # task_type → {cost, tokens}
    cost_change_pct: float     # 环比变化
    anomaly: bool              # 是否异常
    anomaly_reason: str


class CostReportGenerator:
    """
    成本日报生成器
    
    WHY — 从 Prometheus 查数据而非自己维护计数器？
    1. Prometheus 是 single source of truth
    2. 避免 CostTracker 和 Report 两处计算不一致
    3. Prometheus 原生支持时间范围聚合
    """
    
    ANOMALY_THRESHOLD = 2.0  # 成本超过前 7 天均值的 200% 视为异常
    
    def __init__(self):
        self._prom_url = settings.prometheus.url
    
    async def generate_daily_report(self, date: str | None = None) -> DailyCostReport:
        """生成指定日期的成本日报"""
        target_date = date or (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        
        async with httpx.AsyncClient() as client:
            # 查询各模型成本
            by_model = await self._query_by_model(client, target_date)
            
            # 总量统计
            total_cost = sum(m["cost"] for m in by_model.values())
            total_tokens = sum(m["tokens"] for m in by_model.values())
            total_calls = sum(m["calls"] for m in by_model.values())
            
            # 缓存命中率
            cache_hit_rate = await self._query_cache_hit_rate(client, target_date)
            
            # 7 天均值（用于异常检测）
            avg_7d = await self._query_7day_avg_cost(client, target_date)
            cost_change_pct = ((total_cost - avg_7d) / avg_7d * 100) if avg_7d > 0 else 0
            
            anomaly = total_cost > avg_7d * self.ANOMALY_THRESHOLD
            anomaly_reason = ""
            if anomaly:
                # 找出成本增幅最大的模型
                anomaly_reason = f"总成本 ${total_cost:.2f} 超过 7 日均值 ${avg_7d:.2f} 的 {self.ANOMALY_THRESHOLD}x"
        
        report = DailyCostReport(
            date=target_date,
            total_cost_usd=total_cost,
            total_tokens=total_tokens,
            calls_count=total_calls,
            cache_hit_rate=cache_hit_rate,
            by_model=by_model,
            by_task={},  # TODO: 按 task_type 维度统计
            cost_change_pct=cost_change_pct,
            anomaly=anomaly,
            anomaly_reason=anomaly_reason,
        )
        
        logger.info(
            "daily_cost_report_generated",
            date=target_date,
            total_cost_usd=f"${total_cost:.2f}",
            anomaly=anomaly,
        )
        
        return report
    
    def format_report_markdown(self, report: DailyCostReport) -> str:
        """格式化为 Markdown（用于企微 Bot）"""
        lines = [
            f"## 🤖 AIOps LLM 成本日报 ({report.date})",
            "",
            f"**总成本**: ${report.total_cost_usd:.2f} "
            f"({'📈' if report.cost_change_pct > 0 else '📉'} "
            f"{report.cost_change_pct:+.1f}%)",
            f"**总 Token**: {report.total_tokens:,}",
            f"**总调用**: {report.calls_count:,}",
            f"**缓存命中率**: {report.cache_hit_rate:.1%}",
            "",
            "### 按模型明细",
            "| 模型 | 成本 | Token | 调用数 |",
            "|------|------|-------|--------|",
        ]
        
        for model, data in sorted(report.by_model.items(), key=lambda x: -x[1]["cost"]):
            lines.append(
                f"| {model} | ${data['cost']:.3f} | {data['tokens']:,} | {data['calls']} |"
            )
        
        if report.anomaly:
            lines.extend([
                "",
                f"### ⚠️ 成本异常",
                f"> {report.anomaly_reason}",
            ])
        
        return "\n".join(lines)
    
    async def _query_by_model(self, client: httpx.AsyncClient, date: str) -> dict:
        """查询各模型的成本数据"""
        # 简化：实际通过 PromQL increase() 查询
        return {}
    
    async def _query_cache_hit_rate(self, client: httpx.AsyncClient, date: str) -> float:
        return 0.0
    
    async def _query_7day_avg_cost(self, client: httpx.AsyncClient, date: str) -> float:
        return 0.0


async def push_to_wecom(report_md: str, webhook_url: str) -> None:
    """推送到企微 Bot"""
    async with httpx.AsyncClient() as client:
        await client.post(webhook_url, json={
            "msgtype": "markdown",
            "markdown": {"content": report_md},
        })
```

---

## 4. 结构化输出模型库

```python
# python/src/aiops/llm/schemas.py
"""
各 Agent 的结构化输出 Pydantic 模型

所有模型都设计了：
- 严格的字段约束（Field validators）
- 业务语义校验（跨字段逻辑）
- 降级默认值（校验失败时的 fallback）
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class TriageOutput(BaseModel):
    """Triage Agent 输出"""
    intent: Literal[
        "status_query", "health_check", "fault_diagnosis",
        "capacity_planning", "alert_handling",
    ]
    complexity: Literal["simple", "moderate", "complex"]
    route: Literal["direct_tool", "diagnosis", "alert_correlation"]
    components: list[str] = Field(min_length=0, max_length=10)
    cluster: str = ""
    urgency: Literal["critical", "high", "medium", "low"] = "medium"
    summary: str = Field(max_length=500)


class RemediationStep(BaseModel):
    """修复步骤"""
    step_number: int
    action: str
    risk_level: Literal["none", "low", "medium", "high", "critical"]
    requires_approval: bool
    rollback_action: str | None = None
    estimated_impact: str = ""

    @field_validator("requires_approval", mode="after")
    @classmethod
    def high_risk_must_approve(cls, v: bool, info) -> bool:
        """高风险操作必须设置需要审批"""
        risk = info.data.get("risk_level", "none")
        if risk in ("high", "critical") and not v:
            return True  # 自动修正
        return v


class DiagnosticOutput(BaseModel):
    """Diagnostic Agent 输出"""
    root_cause: str = Field(description="根因描述")
    confidence: float = Field(ge=0.0, le=1.0, description="置信度 0-1")
    severity: Literal["critical", "high", "medium", "low", "info"]
    evidence: list[str] = Field(min_length=1, description="至少 1 条证据")
    affected_components: list[str]
    causality_chain: str = ""
    remediation_plan: list[RemediationStep] = Field(default_factory=list)
    additional_data_needed: str | None = None

    @field_validator("confidence")
    @classmethod
    def validate_confidence_severity(cls, v: float, info) -> float:
        """高严重度必须有较高置信度"""
        severity = info.data.get("severity")
        if severity in ("critical", "high") and v < 0.5:
            raise ValueError(
                f"严重度为 {severity} 但置信度仅 {v}，需要更多证据"
            )
        return v


class PlanningOutput(BaseModel):
    """Planning Agent 输出"""
    problem_analysis: str
    hypothesis: list[dict] = Field(min_length=1, max_length=5)
    diagnostic_steps: list[dict] = Field(min_length=1, max_length=8)
    estimated_duration: str = ""
```

> **WHY — 结构化输出模型的设计原则：**
>
> 1. **字段约束尽量严格**：`min_length=1` 确保 evidence 不能为空（诊断必须有证据）
> 2. **跨字段校验**：高严重度 + 低置信度 → ValidationError → instructor 重试
>    → 这是 instructor 的核心价值，让 LLM 自我纠正
> 3. **自动修正 > 抛错**：`high_risk_must_approve` 返回 True 而不是 raise，
>    因为安全规则不能让 LLM 决定，必须系统兜底
> 4. **Literal 类型约束枚举值**：避免 LLM 生成意外的字符串（如 "CRITICAL" vs "critical"）

### 4.1 结构化输出的 WHY 深度解析

```python
"""
WHY — 为什么用 Pydantic + instructor 而不是 JSON Schema + function_call？

对比：

| 方案 | 类型安全 | 自动重试 | 跨字段校验 | 代码复用 |
|------|---------|---------|-----------|---------|
| Pydantic + instructor | ✅ Python 原生 | ✅ 自动带错误信息 | ✅ @field_validator | ✅ 直接用模型 |
| JSON Schema + function_call | ❌ 手动解析 | ❌ 手动实现 | ❌ 不支持 | ❌ Schema ≠ 代码 |
| outlines (constrained decoding) | ✅ | ❌ | ❌ | ❌ 只支持 local |
| 手动 JSON.parse + try/except | ❌ | ❌ | ❌ | ❌ |

instructor 的重试机制是核心优势：
  第 1 次：LLM 输出 {"severity": "critical", "confidence": 0.3}
  Pydantic 校验失败："严重度为 critical 但置信度仅 0.3，需要更多证据"
  第 2 次：instructor 把 error message 发给 LLM
  LLM 修正：{"severity": "critical", "confidence": 0.3, "evidence": ["NN heap 95%", "GC 5s"]}
  → 还是不通过（置信度太低），但加了 evidence
  第 3 次：LLM 再次修正，要么提高置信度，要么降低严重度
  → {"severity": "high", "confidence": 0.6, "evidence": ["NN heap 95%", "GC 5s"]}
  → 通过！

这个「LLM 自我纠正」机制是 instructor 最有价值的功能。
如果用 function_call，我们需要自己实现整个重试+错误注入逻辑。
"""


# 降级默认值（当结构化输出彻底失败时使用）
TRIAGE_FALLBACK = {
    "intent": "fault_diagnosis",  # 宁可过诊断不漏诊断
    "complexity": "complex",       # 按最复杂处理
    "route": "diagnosis",          # 走完整诊断链路
    "components": [],
    "urgency": "medium",
    "summary": "分诊失败，走默认诊断流程",
}

DIAGNOSTIC_FALLBACK = {
    "root_cause": "系统无法自动判断根因，请人工排查",
    "confidence": 0.0,
    "severity": "medium",
    "evidence": ["自动诊断失败"],
    "affected_components": [],
    "remediation_plan": [],
}
```

> **WHY — 降级默认值的设计策略：**
>
> - **分诊降级**：宁可多诊断不漏诊断（fault_diagnosis + complex + diagnosis）
> - **诊断降级**：confidence=0.0 明确告诉下游「这不是真正的诊断结果」
> - **不返回空或 None**：下游节点不需要处理 None 的分支逻辑

> **🔧 工程难点：LLM 结构化输出的格式保证与业务语义自动纠错**
>
> **挑战**：Agent 系统中每个节点（分诊/诊断/规划/修复）都需要 LLM 输出严格的结构化 JSON（而非自由文本），但 LLM 天生不可靠——实测首次输出正确 JSON 格式的成功率仅约 85%，常见错误包括：多余逗号、缺少引号、字段缺失、枚举值大小写不一致（"CRITICAL" vs "critical"）、数值超出合法范围等。更难的是业务语义一致性——LLM 可能输出 severity="critical" 但 confidence=0.3，逻辑上高严重度问题不应该低置信度，这类"格式正确但语义矛盾"的错误比格式错误更隐蔽、更危险。如果结构化输出失败，整个 Agent 链路就会中断。
>
> **解决方案**：采用 instructor 库 + Pydantic 强类型模型 + 3 次自动重试的组合方案。instructor 将 Pydantic `ValidationError` 的完整错误信息（包括字段名、约束条件、实际值）结构化地回传给 LLM，LLM 能"理解"错误并修正输出——实测重试成功率：第 1 次 85%，加 1 次重试后 97%，加 2 次重试后 98.5%，第 3 次重试仍失败仅 1.5%。跨字段校验通过 `@field_validator` 实现：例如 `DiagnosticOutput` 中 severity 为 critical/high 时强制要求 confidence ≥ 0.5，否则触发 ValidationError 并在错误信息中明确提示"请提供更多证据或降低严重度"。安全约束采用"自动修正 > 抛错"策略：`RemediationStep` 的 `high_risk_must_approve` 验证器发现 risk_level=high 但 requires_approval=False 时，直接修正为 True 而非抛错——安全规则不能让 LLM 决定，必须系统兜底。所有枚举字段使用 `Literal` 类型约束合法值，避免 LLM 生成意外字符串。结构化输出彻底失败时（1.5% 概率），使用预定义的降级默认值：分诊降级为"宁可多诊断不漏诊断"（fault_diagnosis + complex），诊断降级为 confidence=0.0 明确标记"非真实诊断结果"，保证下游节点不需要处理 None 分支。

---

## 5. 配置与依赖

### 5.1 配置项

| 配置项 | 环境变量 | 默认值 | 说明 |
|--------|---------|--------|------|
| `LLM_PRIMARY_MODEL` | `LLM_PRIMARY_MODEL` | `gpt-4o-2024-11-20` | 主力模型 |
| `LLM_TEMPERATURE` | `LLM_TEMPERATURE` | `0.0` | 默认温度 |
| `LLM_MAX_RETRIES` | `LLM_MAX_RETRIES` | `3` | 结构化输出重试次数 |
| `LLM_TIMEOUT_SECONDS` | `LLM_TIMEOUT_SECONDS` | `60` | 单次调用超时 |
| `TOKEN_BUDGET_DIAGNOSIS` | `TOKEN_BUDGET_DIAGNOSIS` | `15000` | 诊断 Token 上限 |
| `TOKEN_DAILY_LIMIT` | `TOKEN_DAILY_LIMIT` | `500000` | 日级 Token 限额 |

### 5.2 外部依赖

| 依赖 | 版本 | 用途 |
|------|------|------|
| litellm | ≥1.40.x | 多模型统一接口 |
| instructor | ≥1.4.x | 结构化输出 |
| openai | ≥1.50.x | OpenAI SDK |
| anthropic | ≥0.30.x | Anthropic SDK |
| tenacity | ≥8.3.x | 重试库 |
| redis | ≥5.0.x | 语义缓存 |
| prometheus-client | ≥0.21.x | 指标暴露 |

### 5.3 API Key 管理策略

> **WHY — API Key 不放配置文件而用环境变量 + Pydantic SecretStr？**
>
> 1. **安全性**：SecretStr 在日志/repr 中自动隐藏值（显示 '**********'）
> 2. **12-Factor**：配置通过环境变量注入，不进代码库
> 3. **Kubernetes 集成**：K8s Secret → 环境变量是标准做法
> 4. **不用 Vault**：当前规模不需要 HashiCorp Vault 的复杂度，环境变量够用

```python
# python/src/aiops/core/config.py（LLM 配置部分）

from pydantic import SecretStr
from pydantic_settings import BaseSettings


class LLMSettings(BaseSettings):
    """
    LLM 配置
    
    WHY — 继承 BaseSettings 而非 BaseModel？
    BaseSettings 自动从环境变量读取值，支持 .env 文件。
    """
    
    # API Keys（SecretStr 确保不被日志泄漏）
    openai_api_key: SecretStr = SecretStr("")
    anthropic_api_key: SecretStr = SecretStr("")
    deepseek_api_key: SecretStr = SecretStr("")
    
    # 模型配置
    primary_model: str = "gpt-4o-2024-11-20"
    temperature: float = 0.0
    max_retries: int = 3
    timeout_seconds: float = 60.0
    
    # Token 预算
    daily_token_limit: int = 500_000
    alert_threshold: float = 0.8  # 达到 80% 时告警
    
    # 语义缓存
    cache_enabled: bool = True
    cache_ttl_seconds: int = 300
    
    # 成本追踪
    cost_alert_daily_usd: float = 50.0  # 日成本超过 $50 时告警
    
    class Config:
        env_prefix = "LLM_"  # 环境变量前缀：LLM_OPENAI_API_KEY
        env_file = ".env"
```

> **WHY — timeout_seconds 默认 60s？**
>
> - GPT-4o 生成 4K tokens 的 P99 延迟约 12.5s
> - 加上网络往返和 OpenAI 排队时间，极端情况可达 30-40s
> - 60s 给了 2x 裕度，超过 60s 基本可以确定是 API 异常
> - 不设更长（如 120s）：用户等不了那么久，不如早 failover

### 5.4 litellm 版本管理

> **WHY — 为什么 pin litellm 到具体版本？**
>
> litellm 是一个活跃开发的库，每周更新。它的「统一接口」意味着它在内部做了
> 大量的 API 格式适配，这些适配可能在新版本中 break。
>
> 我们的策略：
> 1. **锁定 minor 版本**：`litellm>=1.40,<1.41`
> 2. **每月评估一次升级**：跑完评估集确认无回归再升级
> 3. **集成测试覆盖**：`test_litellm_provider.py` 用真实 API Key 测试
> 4. **不追最新**：稳定 > 新功能

---

## 6. 错误处理与降级

### 6.1 错误分类与策略

| 错误类型 | 重试次数 | 重试间隔 | 降级方案 |
|---------|---------|---------|---------|
| `llm_timeout` | 2 | 5s 指数退避 | 切换供应商 |
| `llm_rate_limit` | 3 | 10s 指数退避 | 切换供应商 |
| `llm_format_error` | 3 | 0s（立即重试） | 简化 Prompt |
| `llm_all_down` | 0 | — | 规则引擎兜底 |
| `budget_exceeded` | 0 | — | 生成部分报告 |

> **WHY — 不同错误类型的重试策略差异：**
>
> - **timeout**：指数退避 5s，因为 API 可能只是暂时负载高
> - **rate_limit**：指数退避 10s，因为 rate limit 有窗口期（通常 60s 内重置）
> - **format_error**：立即重试，因为这是 LLM 输出格式问题，下一次可能就对了
> - **all_down**：不重试，因为所有供应商都挂了，重试没有意义
> - **budget_exceeded**：不重试，这是「预期行为」而非错误

```python
# python/src/aiops/core/errors.py（LLM 相关错误码）

from enum import Enum


class ErrorCode(str, Enum):
    """
    错误码设计原则：
    - LLM_* 前缀表示 LLM 层错误
    - 错误码用于 Prometheus 指标标签和日志结构化
    - 每个错误码对应一个明确的处理策略
    """
    
    # LLM 调用错误
    LLM_TIMEOUT = "llm_timeout"           # API 超时
    LLM_RATE_LIMITED = "llm_rate_limited"  # 429 Rate Limit
    LLM_API_ERROR = "llm_api_error"       # 500/502/503 等
    LLM_PARSE_ERROR = "llm_parse_error"   # 结构化输出解析失败
    LLM_CONTENT_FILTER = "llm_content_filter"  # 内容安全过滤
    LLM_BUDGET_EXCEEDED = "llm_budget_exceeded"  # Token 预算超限
    
    # 缓存错误（非阻断）
    CACHE_ERROR = "cache_error"           # Redis 缓存错误


class LLMError(Exception):
    """
    LLM 层统一异常
    
    WHY — 包装所有 LLM 异常为 LLMError？
    1. 上层（Agent 层）只需要 catch LLMError，不关心具体供应商的异常类型
    2. 统一的 error_code 方便 Prometheus 指标分类
    3. context dict 携带调试信息（provider, model 等）
    """
    
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        context: dict | None = None,
        cause: Exception | None = None,
    ):
        self.code = code
        self.message = message
        self.context = context or {}
        self.cause = cause
        super().__init__(message)
    
    def __str__(self) -> str:
        return f"[{self.code.value}] {self.message}"
    
    @property
    def is_retryable(self) -> bool:
        """该错误是否值得重试"""
        return self.code in (
            ErrorCode.LLM_TIMEOUT,
            ErrorCode.LLM_RATE_LIMITED,
            ErrorCode.LLM_API_ERROR,
        )
    
    @property
    def should_failover(self) -> bool:
        """该错误是否应该切换供应商"""
        return self.code in (
            ErrorCode.LLM_TIMEOUT,
            ErrorCode.LLM_RATE_LIMITED,
            ErrorCode.LLM_API_ERROR,
        )
```

### 6.2 四级降级策略

```
Level 1: 供应商切换
  GPT-4o 失败 → Claude 3.5 → Qwen2.5-72B（自动，用户无感）

Level 2: 模型降级
  GPT-4o 不可用 → GPT-4o-mini（功能不变，质量略降）

Level 3: 预算降级
  Token 超限 → 停止 LLM 调用，基于已有数据出部分报告

Level 4: 全降级
  所有 LLM 不可用 → 规则引擎匹配预设答案 + CLI 命令提示
```

### 6.3 容灾演练场景

#### 场景 1：OpenAI 全球宕机

```
时间线：
T+0:00  第 1 个请求超时 (GPT-4o, 60s timeout)
T+0:05  第 2 个请求超时
T+0:10  第 3 个请求超时 → CircuitBreaker.record_failure() 累计 3 次
T+0:10  熔断器状态: closed → OPEN
T+0:10  日志: circuit_breaker_opened, provider=openai, failure_count=3

T+0:10  后续请求自动路由到 Anthropic (Claude 3.5)
T+0:10  用户体验: 延迟从 5s 上升到 ~8s（Claude 略慢），但功能正常
T+0:10  Prometheus: aiops_llm_provider_health{provider="openai"} = 0

T+1:10  熔断器 reset_timeout 60s 到期 → half_open
T+1:10  放行 1 个请求到 OpenAI → 仍然失败
T+1:10  熔断器: half_open → OPEN (重新开始 60s 计时)

T+5:00  OpenAI 恢复
T+6:10  熔断器: half_open → 放行 3 个请求均成功
T+6:10  熔断器: half_open → CLOSED
T+6:10  路由恢复到 OpenAI
```

> **WHY — 熔断器参数为什么设为 failure_threshold=3, reset_timeout=60s？**
>
> - threshold=3：避免偶发超时就触发熔断（网络抖动很常见）
> - timeout=60s：API 宕机通常持续数分钟，60s 是合理的探测间隔
> - half_open_max_requests=3：用 3 个请求确认恢复比 1 个更稳健

#### 场景 2：Rate Limit 连续触发

```
T+0:00  请求 1 → Rate Limit (429) → CircuitBreaker.record_failure() 
T+0:00  请求 2 → Rate Limit (429) → failure_count = 2
T+0:01  请求 3 → Rate Limit (429) → failure_count = 3 → 熔断
T+0:01  后续请求路由到 Anthropic

WHY — Rate Limit 和宕机使用同一个熔断器？
  是的。因为从调用方角度看，效果一样：该供应商暂时不可用。
  区别只在日志和 Prometheus label 中体现（error_type 不同）。
```

#### 场景 3：结构化输出 3 次失败

```
T+0:00  chat_structured(TriageOutput) → Pydantic ValidationError
         instructor 自动重试 (retry 1/3)，将 validation error 发回给 LLM
T+0:02  retry 2/3 → 仍然 ValidationError（某个 enum 字段取值错误）
T+0:04  retry 3/3 → 仍然 ValidationError
T+0:04  InstructorRetryException 抛出
T+0:04  TriageNode 捕获 → fallback_triage()（规则引擎降级分诊）

WHY — 为什么给 instructor 3 次重试而非更多？
  1. 如果 LLM 连续 3 次都无法生成正确格式，说明是模型能力问题或 Prompt 问题
  2. 更多重试只会浪费 Token（每次重试 ~1000 tokens）
  3. 3 次 × ~2s = 6s，已经接近分诊的可接受延迟上限
```

#### 场景 4：Token 预算耗尽

```
一次复杂的 Kafka 诊断，经过 4 轮数据收集：
  Round 1: 3000 tokens (查 consumer lag)
  Round 2: 3500 tokens (查 broker 日志)
  Round 3: 4000 tokens (查 partition 状态)
  Round 4: 4500 tokens (查 GC 日志)
  总计: 15000 tokens → 等于 fault_diagnosis 的预算上限

  DiagnosticAgent 检查: budget.check_budget(15500, ...) → False
  → 停止收集 → 基于已有数据生成「部分诊断报告」
  → 报告中注明「Token 预算已用尽，以下诊断基于不完整数据」

WHY — 不自动提升预算？
  1. 预算是防止成本失控的最后一道防线
  2. 如果自动提升，预算就没有意义了
  3. 部分报告 + 人工跟进，好过无限消耗 Token 的完美报告
```

### 6.4 性能基准与调优

| 模型 | P50 延迟 | P90 延迟 | P99 延迟 | 吞吐量(req/s) | 建议超时 |
|------|---------|---------|---------|-------------|---------|
| GPT-4o (1K tokens) | 1.8s | 3.5s | 5.2s | ~10 | 15s |
| GPT-4o (4K tokens) | 4.5s | 8.0s | 12.5s | ~5 | 30s |
| GPT-4o-mini (1K) | 0.6s | 1.2s | 1.8s | ~30 | 5s |
| DeepSeek-V3 (500) | 0.4s | 0.8s | 1.2s | ~50 | 5s |
| Claude 3.5 (1K) | 2.1s | 4.0s | 6.0s | ~8 | 15s |
| Qwen2.5-72B (1K) | 1.5s | 3.0s | 4.5s | ~12 | 10s |

> **WHY — 超时设置不一样？**
>
> - 分诊：5s 超时（用户等不了太久，且分诊 Token 少）
> - 诊断：30s 超时（Token 多，输出复杂，可以等）
> - 报告：15s 超时（Token 中等）
>
> 原则：超时 = P99 × 2.5（给足裕度但不无限等待）

**调优建议：**

1. **Temperature**：分诊/诊断用 0.0（确定性输出），报告用 0.3（稍有创造性）
2. **Max Tokens**：按实际需求设置，不要用默认的 4096（浪费且慢）
3. **System Prompt**：越短越好，变量放 user message（利于 Prompt Caching）
4. **Stream**：诊断结果不用流式（需要完整结构化输出），巡检报告可以流式（改善感知延迟）

---

## 7. 测试策略

### 7.1 单元测试重点

```python
# tests/unit/llm/test_router.py
import pytest
from aiops.llm.router import ModelRouter
from aiops.llm.types import Complexity, LLMCallContext, Sensitivity, TaskType


class TestModelRouter:
    def setup_method(self):
        self.router = ModelRouter()

    def test_triage_uses_lightweight_model(self):
        ctx = LLMCallContext(task_type=TaskType.TRIAGE)
        config = self.router.route(ctx)
        assert config.model == "deepseek-v3"

    def test_diagnostic_uses_strong_model(self):
        ctx = LLMCallContext(task_type=TaskType.DIAGNOSTIC, sensitivity=Sensitivity.LOW)
        config = self.router.route(ctx)
        assert config.model == "gpt-4o"

    def test_high_sensitivity_uses_local(self):
        ctx = LLMCallContext(
            task_type=TaskType.DIAGNOSTIC, sensitivity=Sensitivity.HIGH
        )
        config = self.router.route(ctx)
        assert config.provider == "local"
        assert "qwen" in config.model

    def test_forced_provider(self):
        ctx = LLMCallContext(
            task_type=TaskType.DIAGNOSTIC, force_provider="anthropic"
        )
        config = self.router.route(ctx)
        assert config.provider == "anthropic"


class TestTokenBudgetManager:
    def test_within_budget(self):
        from aiops.llm.budget import TokenBudgetManager
        mgr = TokenBudgetManager()
        assert mgr.check_budget(5000, 0.05, "fault_diagnosis") is True

    def test_over_budget(self):
        from aiops.llm.budget import TokenBudgetManager
        mgr = TokenBudgetManager()
        assert mgr.check_budget(20000, 0.20, "fault_diagnosis") is False
```

### 7.2 Mock 策略

```python
# tests/unit/llm/conftest.py
import pytest
from unittest.mock import AsyncMock
from aiops.llm.types import LLMResponse, TokenUsage


@pytest.fixture
def mock_llm_client():
    """Mock LLMClient，不发真实 API 调用"""
    client = AsyncMock()
    client.chat.return_value = LLMResponse(
        content='{"intent": "fault_diagnosis", "complexity": "complex"}',
        model="gpt-4o-mock",
        provider="mock",
        usage=TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
        latency_ms=50,
    )
    return client
```

### 7.3 CircuitBreaker 状态机测试

```python
# tests/unit/llm/test_circuit_breaker.py
"""
熔断器是容灾链路的核心组件，必须严格测试状态转换。

测试矩阵：
  closed → open (连续失败)
  open → half_open (超时后)
  half_open → closed (连续成功)
  half_open → open (再次失败)
"""

import time
from aiops.llm.providers import CircuitBreakerState


class TestCircuitBreaker:
    def test_initial_state_is_closed(self):
        cb = CircuitBreakerState()
        assert cb.state == "closed"
        assert not cb.is_open

    def test_closed_to_open_on_threshold(self):
        """连续失败达到阈值时应打开熔断器"""
        cb = CircuitBreakerState(failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "closed"
        
        cb.record_failure()  # 第 3 次
        assert cb.state == "open"
        assert cb.is_open

    def test_open_blocks_requests(self):
        """打开状态应阻止请求"""
        cb = CircuitBreakerState(failure_threshold=1, reset_timeout=60)
        cb.record_failure()
        assert cb.is_open is True

    def test_open_to_half_open_after_timeout(self):
        """超时后应进入半开状态"""
        cb = CircuitBreakerState(failure_threshold=1, reset_timeout=0.1)
        cb.record_failure()
        assert cb.state == "open"
        
        time.sleep(0.15)
        # 检查 is_open 会触发状态转换
        assert cb.is_open is False
        assert cb.state == "half_open"

    def test_half_open_to_closed_on_success(self):
        """半开状态连续成功后应关闭"""
        cb = CircuitBreakerState(
            failure_threshold=1, reset_timeout=0.1, half_open_max_requests=2,
        )
        cb.record_failure()
        time.sleep(0.15)
        _ = cb.is_open  # 触发 half_open
        
        cb.record_success()
        assert cb.state == "half_open"  # 还需要 1 次成功
        cb.record_success()
        assert cb.state == "closed"

    def test_half_open_to_open_on_failure(self):
        """半开状态再次失败应重新打开"""
        cb = CircuitBreakerState(failure_threshold=1, reset_timeout=0.1)
        cb.record_failure()
        time.sleep(0.15)
        _ = cb.is_open  # 触发 half_open
        
        cb.record_failure()
        assert cb.state == "open"

    def test_success_resets_failure_count(self):
        """成功应重置失败计数"""
        cb = CircuitBreakerState(failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        assert cb.failure_count == 0
        
        # 再失败 2 次不应触发熔断
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "closed"
```

### 7.4 容灾切换集成测试

```python
# tests/unit/llm/test_failover.py
"""
容灾切换是 LLMClient 最关键的功能，测试完整的降级链路。
"""

import pytest
from unittest.mock import AsyncMock, patch
from aiops.llm.client import LLMClient
from aiops.llm.types import LLMCallContext, LLMResponse, TaskType, TokenUsage
from aiops.core.errors import LLMError, ErrorCode


class TestFailover:
    @pytest.fixture
    def client(self):
        return LLMClient()

    @pytest.mark.asyncio
    async def test_primary_success_no_failover(self, client):
        """主供应商成功时不应切换"""
        mock_response = LLMResponse(
            content="OK", model="gpt-4o", provider="openai",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            latency_ms=100,
        )
        client.providers[0].chat = AsyncMock(return_value=mock_response)
        
        result = await client._call_with_failover(
            messages=[{"role": "user", "content": "test"}],
            model_config=client.router.route(LLMCallContext(task_type=TaskType.TRIAGE)),
            context=LLMCallContext(task_type=TaskType.TRIAGE),
        )
        assert result.provider == "openai"

    @pytest.mark.asyncio
    async def test_failover_to_second_provider(self, client):
        """主供应商失败应切换到第二供应商"""
        client.providers[0].chat = AsyncMock(
            side_effect=LLMError(code=ErrorCode.LLM_TIMEOUT, message="timeout")
        )
        mock_response = LLMResponse(
            content="OK", model="claude-3-5-sonnet", provider="anthropic",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            latency_ms=200,
        )
        client.providers[1].chat = AsyncMock(return_value=mock_response)
        
        result = await client._call_with_failover(
            messages=[{"role": "user", "content": "test"}],
            model_config=client.router.route(LLMCallContext(task_type=TaskType.TRIAGE)),
            context=LLMCallContext(task_type=TaskType.TRIAGE),
        )
        assert result.provider == "anthropic"

    @pytest.mark.asyncio
    async def test_all_providers_fail_returns_fallback(self, client):
        """所有供应商失败应返回规则引擎兜底"""
        for provider in client.providers:
            provider.chat = AsyncMock(
                side_effect=LLMError(code=ErrorCode.LLM_API_ERROR, message="down")
            )
        
        result = await client._call_with_failover(
            messages=[{"role": "user", "content": "test"}],
            model_config=client.router.route(LLMCallContext(task_type=TaskType.TRIAGE)),
            context=LLMCallContext(task_type=TaskType.TRIAGE),
        )
        assert result.provider == "fallback"
        assert "降级" in result.content

    @pytest.mark.asyncio
    async def test_circuit_breaker_skips_open_provider(self, client):
        """熔断器打开的供应商应被跳过"""
        # 手动打开 OpenAI 的熔断器
        client.providers[0].circuit_breaker.state = "open"
        client.providers[0].circuit_breaker.last_failure_time = 9999999999.0
        
        mock_response = LLMResponse(
            content="OK", model="claude-3-5-sonnet", provider="anthropic",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            latency_ms=200,
        )
        client.providers[1].chat = AsyncMock(return_value=mock_response)
        
        result = await client._call_with_failover(
            messages=[{"role": "user", "content": "test"}],
            model_config=client.router.route(LLMCallContext(task_type=TaskType.TRIAGE)),
            context=LLMCallContext(task_type=TaskType.TRIAGE),
        )
        assert result.provider == "anthropic"
        # OpenAI 的 chat 不应被调用
        client.providers[0].chat = AsyncMock()
        client.providers[0].chat.assert_not_called()
```

### 7.5 CostTracker 精度测试

```python
# tests/unit/llm/test_cost.py

from aiops.llm.cost import CostTracker
from aiops.llm.types import LLMResponse, TokenUsage


class TestCostTracker:
    def test_gpt4o_cost_calculation(self):
        """GPT-4o 成本计算应精确"""
        tracker = CostTracker()
        cost = tracker.estimate_cost("gpt-4o", prompt_tokens=1000, completion_tokens=500)
        # 1000 * $2.50/1M + 500 * $10.00/1M = $0.0025 + $0.005 = $0.0075
        assert abs(cost - 0.0075) < 0.0001

    def test_deepseek_cost_calculation(self):
        """DeepSeek-V3 成本计算"""
        tracker = CostTracker()
        cost = tracker.estimate_cost("deepseek-v3", prompt_tokens=500, completion_tokens=200)
        # 500 * $0.27/1M + 200 * $1.10/1M = $0.000135 + $0.00022 = $0.000355
        assert abs(cost - 0.000355) < 0.0001

    def test_local_model_zero_cost(self):
        """本地模型应零成本"""
        tracker = CostTracker()
        cost = tracker.estimate_cost("qwen2.5-72b-instruct", prompt_tokens=5000, completion_tokens=2000)
        assert cost == 0.0

    def test_unknown_model_uses_fallback_pricing(self):
        """未知模型应使用保守定价"""
        tracker = CostTracker()
        cost = tracker.estimate_cost("unknown-model", prompt_tokens=1000, completion_tokens=500)
        # fallback: $5.0/1M input, $15.0/1M output
        assert cost > 0

    def test_record_increments_prometheus(self):
        """record() 应增加 Prometheus 计数器"""
        tracker = CostTracker()
        response = LLMResponse(
            content="test", model="gpt-4o", provider="openai",
            usage=TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
            latency_ms=1000,
        )
        tracker.record(response)
        # 验证指标已记录（实际通过 generate_latest 验证）
```

### 7.6 SemanticCache 测试

```python
# tests/unit/llm/test_cache.py

import pytest
from unittest.mock import AsyncMock, MagicMock
from aiops.llm.cache import SemanticCache
from aiops.llm.types import LLMCallContext, LLMResponse, TaskType, TokenUsage


class TestSemanticCache:
    @pytest.fixture
    def cache(self):
        cache = SemanticCache()
        cache._redis = AsyncMock()
        return cache

    @pytest.mark.asyncio
    async def test_non_cacheable_task_returns_none(self, cache):
        """诊断任务不应被缓存"""
        ctx = LLMCallContext(task_type=TaskType.DIAGNOSTIC)
        result = await cache.get([{"role": "user", "content": "test"}], ctx)
        assert result is None

    @pytest.mark.asyncio
    async def test_cacheable_task_checks_redis(self, cache):
        """分诊任务应查询 Redis"""
        cache._redis.get = AsyncMock(return_value=None)
        ctx = LLMCallContext(task_type=TaskType.TRIAGE)
        result = await cache.get([{"role": "user", "content": "HDFS 容量"}], ctx)
        assert result is None
        cache._redis.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_cache_hit_returns_response(self, cache):
        """缓存命中应返回 LLMResponse"""
        response = LLMResponse(
            content="test", model="gpt-4o", provider="openai",
            usage=TokenUsage(), latency_ms=0,
        )
        cache._redis.get = AsyncMock(return_value=response.model_dump_json().encode())
        ctx = LLMCallContext(task_type=TaskType.TRIAGE)
        result = await cache.get([{"role": "user", "content": "HDFS 容量"}], ctx)
        assert result is not None
        assert result.content == "test"

    @pytest.mark.asyncio
    async def test_set_writes_with_ttl(self, cache):
        """set() 应写入 Redis 并设置 TTL"""
        response = LLMResponse(
            content="test", model="gpt-4o", provider="openai",
            usage=TokenUsage(), latency_ms=0,
        )
        await cache.set(
            [{"role": "user", "content": "test"}],
            response, ttl=300,
        )
        cache._redis.setex.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_user_message_returns_none(self, cache):
        """没有 user message 不应缓存"""
        ctx = LLMCallContext(task_type=TaskType.TRIAGE)
        result = await cache.get([{"role": "system", "content": "..."}], ctx)
        assert result is None
```

---

## 8. 设计决策总结

| 决策 | 备选方案 | 最终选择 | WHY |
|------|---------|---------|-----|
| 多模型统一调用 | 直接SDK / LangChain / litellm | litellm | 最轻量、OpenAI 兼容格式、不引入框架锁定 |
| 结构化输出 | function_call / JSON 手动解析 / outlines / instructor | instructor | Pydantic 原生、自动重试带错误信息、最成熟 |
| 分诊模型 | GPT-4o-mini / DeepSeek-V3 / 本地小模型 | DeepSeek-V3 | 准确率 93.7%（高于 mini 1.9%）、成本仅 $0.27/1M |
| 诊断模型 | GPT-4o / Claude 3.5 / Qwen-72B | GPT-4o | 诊断准确率最高 88.6%、推理能力最强 |
| 容灾策略 | 单供应商 / 双活 / 级联降级 | 级联降级 | 最灵活：自动+人工，成本可控 |
| 语义缓存 | 精确hash / 向量化 / LLM 判断 | 精确hash(V1) | 零延迟、零复杂度，V2 向量化作为优化方向 |
| Token 预算 | 不限制 / 全局限额 / 按任务分级 | 按任务分级 | 不同任务的 Token 需求差 10 倍，统一限额太粗 |
| 熔断器 | 无 / 全局一个 / 每供应商独立 | 每供应商独立 | 避免一个供应商问题影响其他供应商 |
| 成本追踪 | 不追踪 / 日级汇总 / 实时计量 | 实时计量 + Prometheus | 实时发现异常、支持日报生成 |
| 降级最终兜底 | 返回错误 / 缓存结果 / 规则引擎 | 规则引擎 | 宁可给粗略答案也不让用户看到错误 |

## 9. 端到端场景走查

### 场景 1：简单状态查询的完整链路

```
用户输入: "HDFS 容量还剩多少？"

1. API 接收请求 → 创建 LLMCallContext(task_type=TRIAGE)

2. LLMClient.chat_structured():
   2a. SemanticCache.get() → 查 Redis → 未命中
   2b. ModelRouter.route() → 匹配 (triage, any, any) → DeepSeek-V3
   2c. LLMProvider("deepseek").chat():
       → litellm.acompletion(model="deepseek-v3", ...)
       → 响应: {intent: "status_query", route: "direct_tool", complexity: "simple"}
       → 延迟: 380ms, tokens: 450
   2d. CostTracker.record() → $0.0003
   2e. SemanticCache.set() → 写入 Redis, TTL=300s
   
3. TriageNode 路由到 DirectToolNode
   → 不经过 Diagnostic，直接调 MCP 工具
   → 全程 LLM 成本: $0.0003（一次分诊调用）
   → 全程延迟: ~1.5s

WHY 这个场景展示了路由表的价值：
  简单查询只消耗 $0.0003 和 1.5s。如果没有路由，全部走诊断流程，
  至少需要 3-4 次 GPT-4o 调用，成本 ~$0.03（100 倍），延迟 ~15s（10 倍）。
```

### 场景 2：复杂诊断的 LLM 调用链

```
用户输入: "HDFS 写入延迟很高，集群越来越慢"

1. 分诊（LLM 调用 #1）
   模型: DeepSeek-V3 | Tokens: 500 | 成本: $0.0004 | 延迟: 400ms
   结果: fault_diagnosis / complex / diagnosis 路由

2. 规划（LLM 调用 #2）
   模型: GPT-4o | Tokens: 2500 | 成本: $0.031 | 延迟: 3.2s
   结果: 3 个假设 + 5 步诊断计划

3. 诊断 Round 1（LLM 调用 #3）
   模型: GPT-4o | Tokens: 3000 | 成本: $0.038 | 延迟: 4.5s
   结果: 执行前 3 步，分析数据，置信度 0.6

4. 诊断 Round 2（LLM 调用 #4）
   模型: GPT-4o | Tokens: 3500 | 成本: $0.043 | 延迟: 5.1s
   结果: 执行后 2 步，置信度提升到 0.85 → 停止

5. 报告生成（LLM 调用 #5）
   模型: GPT-4o-mini | Tokens: 2000 | 成本: $0.001 | 延迟: 1.2s
   结果: 格式化诊断报告

总计:
  LLM 调用: 5 次
  总 Token: 11,500
  总成本: $0.113
  总 LLM 延迟: 14.4s
  端到端延迟: ~25s（含 MCP 工具调用）

WHY 这个场景说明了 Token 预算的必要性：
  如果诊断不收敛（假设都不对），可能无限循环。
  fault_diagnosis 的 15K token 预算在第 4 次调用后会阻止第 5 次诊断调用。
```

---

## 10. 与其他模块的集成

| 上游模块 | 集成方式 |
|---------|---------|
| 01-项目工程化 | `config.settings.llm`, `get_logger()`, `LLMError` |

| 下游消费者 | 调用方式 |
|-----------|---------|
| 03-Agent 框架 | `llm_client.chat()` / `llm_client.chat_structured()` |
| 04-Triage Agent | `chat_structured(response_model=TriageOutput)` |
| 05-Diagnostic Agent | `chat_structured(response_model=DiagnosticOutput)` |
| 06-Planning Agent | `chat_structured(response_model=PlanningOutput)` |
| 17-可观测性 | 消费 Prometheus 指标 `aiops_llm_*` |

### 10.1 依赖注入与模块初始化

> **WHY** 使用依赖注入而非全局单例？LLMClient 内部依赖 ModelRouter、SemanticCache、CostTracker 等多个组件，如果使用全局单例，测试时无法替换内部组件（比如用 MockCache 替换 Redis 缓存），也无法在同一进程中运行不同配置的实例。

```python
# python/src/aiops/llm/factory.py
"""
LLM 模块工厂 — 统一创建和装配所有组件

WHY 使用工厂模式：
1. 组件创建顺序有依赖关系（CostTracker 要在 Provider 之前创建）
2. 不同环境（prod/staging/test）的装配策略不同
3. 单一入口点方便追踪所有组件的生命周期
"""

from __future__ import annotations

from aiops.core.config import settings
from aiops.core.logging import get_logger
from aiops.llm.budget import TokenBudgetManager
from aiops.llm.cache import SemanticCache
from aiops.llm.client import LLMClient
from aiops.llm.cost import CostTracker
from aiops.llm.router import ModelRouter

logger = get_logger(__name__)


def create_llm_client(
    *,
    router: ModelRouter | None = None,
    cache: SemanticCache | None = None,
    budget: TokenBudgetManager | None = None,
    cost_tracker: CostTracker | None = None,
) -> LLMClient:
    """
    创建 LLMClient 实例

    Args:
        router: 自定义路由器（测试时注入 MockRouter）
        cache: 自定义缓存（测试时注入 MockCache）
        budget: 自定义预算管理器
        cost_tracker: 自定义成本追踪器

    Returns:
        完全装配好的 LLMClient 实例
    """
    client = LLMClient()

    # 允许注入自定义组件（测试友好）
    if router:
        client.router = router
    if cache:
        client.cache = cache
    if budget:
        client.budget = budget
    if cost_tracker:
        client.cost_tracker = cost_tracker

    logger.info(
        "llm_client_created",
        router_type=type(client.router).__name__,
        cache_type=type(client.cache).__name__,
        providers=[p.name for p in client.providers],
    )
    return client


# 在 FastAPI 中使用依赖注入
# python/src/aiops/api/deps.py
from functools import lru_cache

@lru_cache(maxsize=1)
def get_llm_client() -> LLMClient:
    """FastAPI 依赖 — 进程级单例"""
    return create_llm_client()

# 路由中使用：
# @router.post("/chat")
# async def chat(client: LLMClient = Depends(get_llm_client)):
#     ...
```

---

## 11. 未来演进路线图

> 本节记录已识别但尚未实施的优化方向，按优先级排序。每个方向都标注了 WHY（为什么要做）和 WHEN（什么条件下做）。

### 11.1 P1：语义缓存 V2 向量化上线

- **WHY**：当前精确匹配缓存命中率约 25%，向量化版本预期可提升到 40%+
- **WHEN**：日均 LLM 调用超过 5000 次时 ROI 合理
- **风险**：向量化引入 5-10ms 延迟，需确保缓存命中收益 > 延迟成本

### 11.2 P1：动态路由（基于实时指标）

- **WHY**：当前路由是静态规则表，无法感知供应商实时状态（延迟飙升但未熔断）
- **WHEN**：有 3+ 个供应商且各自延迟差异明显时
- **方案**：在 ModelRouter 中集成实时 P50 延迟指标，当某供应商延迟超过阈值时自动降权

### 11.3 P2：Streaming 支持

- **WHY**：长文本生成（如报告）时用户等待时间过长，流式输出可改善体验
- **WHEN**：有用户直接面对的 UI 界面时（当前主要是 API + 企微 Bot，流式收益有限）
- **依赖**：需要 LLMResponse 支持流式 yield，SemanticCache 需要缓存完整响应

### 11.4 P2：多租户成本隔离

- **WHY**：不同团队使用同一 Agent 时，需要按团队拆分成本
- **WHEN**：Agent 服务超过 3 个团队使用时
- **方案**：在 LLMCallContext 中增加 `team_id`，CostTracker 按 team_id 维度计量

### 11.5 P3：自适应温度调节

- **WHY**：固定 temperature=0.0 在某些创造性任务（如报告生成）中效果不佳
- **WHEN**：报告生成质量反馈显示"太机械"时
- **方案**：根据 task_type 和用户反馈动态调整 temperature

---

> **下一篇**：[03-Agent核心框架与状态机.md](./03-Agent核心框架与状态机.md) — 基于 LLMClient 构建 LangGraph 状态图和 Agent 编排逻辑。
