"""
成本追踪器 — LLM 调用成本实时计量

WHY 独立的成本追踪模块：
- DeepSeek-V3 分诊 vs GPT-4o 诊断，成本差 20 倍
- 路由策略的 ROI 需要用数据验证（文档声称成本优化 77%，需要实际数据支撑）
- 成本数据联动 Prometheus 指标，Grafana Dashboard 可视化

定价数据（参考 02-LLM客户端与多模型路由.md §2.2 表格）：
- DeepSeek-V3: $0.27/M input, $1.10/M output（缓存命中 $0.07/M input）
- GPT-4o: $2.50/M input, $10.00/M output
- GPT-4o-mini: $0.15/M input, $0.60/M output
- Claude-3.5-Sonnet: $3.00/M input, $15.00/M output
- Qwen2.5-72B (local): $0.00（自建推理）
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from aiops.core.logging import get_logger
from aiops.llm.types import TaskType, TokenUsage

logger = get_logger(__name__)


@dataclass
class ModelPricing:
    """单个模型的定价（单位: $/M Token）."""
    model: str
    input_price: float       # $/M input tokens
    output_price: float      # $/M output tokens
    cached_input_price: float = 0.0  # 缓存命中时的 input 价格

    def calculate_cost(self, usage: TokenUsage) -> float:
        """计算单次调用的成本（美元）."""
        input_tokens = usage.prompt_tokens - usage.cached_tokens
        cached_tokens = usage.cached_tokens

        input_cost = (input_tokens / 1_000_000) * self.input_price
        cached_cost = (cached_tokens / 1_000_000) * self.cached_input_price
        output_cost = (usage.completion_tokens / 1_000_000) * self.output_price

        return input_cost + cached_cost + output_cost


@dataclass
class CostRecord:
    """单次调用的成本记录."""
    timestamp: float
    model: str
    provider: str
    task_type: str
    session_id: str
    usage: TokenUsage
    cost_usd: float
    cached: bool = False


@dataclass
class CostSummary:
    """成本汇总."""
    total_cost_usd: float = 0.0
    total_calls: int = 0
    total_tokens: int = 0
    total_cached_tokens: int = 0
    by_model: dict[str, float] = field(default_factory=dict)
    by_task: dict[str, float] = field(default_factory=dict)
    by_provider: dict[str, float] = field(default_factory=dict)
    saved_by_cache: float = 0.0
    period_start: float = field(default_factory=time.monotonic)


class CostTracker:
    """
    LLM 调用成本追踪器.

    职责：
    1. 实时计量每次 LLM 调用的成本
    2. 按维度（model/task/provider/session）聚合
    3. 计算缓存带来的成本节省
    4. 联动 Prometheus 指标（通过回调）

    WHY 不直接用 litellm 的 cost tracking：
    - litellm 的成本追踪是全局的，不支持按 session/task 维度
    - 我们需要自定义定价（本地部署模型成本为 0）
    - 需要计算"如果没有路由优化，成本会是多少"做 ROI 对比
    """

    # 模型定价表
    PRICING: dict[str, ModelPricing] = {
        "deepseek-chat": ModelPricing(
            model="deepseek-chat",
            input_price=0.27,
            output_price=1.10,
            cached_input_price=0.07,
        ),
        "deepseek-reasoner": ModelPricing(
            model="deepseek-reasoner",
            input_price=0.55,
            output_price=2.19,
            cached_input_price=0.14,
        ),
        "gpt-4o": ModelPricing(
            model="gpt-4o",
            input_price=2.50,
            output_price=10.00,
            cached_input_price=1.25,
        ),
        "gpt-4o-mini": ModelPricing(
            model="gpt-4o-mini",
            input_price=0.15,
            output_price=0.60,
            cached_input_price=0.075,
        ),
        "claude-3-5-sonnet-20241022": ModelPricing(
            model="claude-3-5-sonnet-20241022",
            input_price=3.00,
            output_price=15.00,
            cached_input_price=1.50,
        ),
        "qwen2.5-72b-instruct": ModelPricing(
            model="qwen2.5-72b-instruct",
            input_price=0.0,  # 本地部署
            output_price=0.0,
            cached_input_price=0.0,
        ),
    }

    # 用于 ROI 对比的"基准模型"（如果所有请求都用这个模型）
    BASELINE_MODEL: str = "gpt-4o"

    def __init__(self) -> None:
        self._records: list[CostRecord] = []
        self._summary = CostSummary()
        self._session_costs: dict[str, float] = {}

    def record(
        self,
        model: str,
        provider: str,
        task_type: TaskType,
        session_id: str,
        usage: TokenUsage,
        cached: bool = False,
    ) -> CostRecord:
        """
        记录一次 LLM 调用的成本.

        Returns:
            CostRecord 包含计算后的成本
        """
        pricing = self.PRICING.get(model)
        if not pricing:
            # 未知模型按 gpt-4o-mini 估算
            pricing = self.PRICING["gpt-4o-mini"]
            logger.warning("unknown_model_pricing", model=model, fallback="gpt-4o-mini")

        cost = pricing.calculate_cost(usage)

        record = CostRecord(
            timestamp=time.monotonic(),
            model=model,
            provider=provider,
            task_type=task_type.value,
            session_id=session_id,
            usage=usage,
            cost_usd=cost,
            cached=cached,
        )
        self._records.append(record)

        # 更新汇总
        self._summary.total_cost_usd += cost
        self._summary.total_calls += 1
        self._summary.total_tokens += usage.total_tokens
        self._summary.total_cached_tokens += usage.cached_tokens
        self._summary.by_model[model] = self._summary.by_model.get(model, 0.0) + cost
        self._summary.by_task[task_type.value] = (
            self._summary.by_task.get(task_type.value, 0.0) + cost
        )
        self._summary.by_provider[provider] = (
            self._summary.by_provider.get(provider, 0.0) + cost
        )

        # Session 级成本
        self._session_costs[session_id] = (
            self._session_costs.get(session_id, 0.0) + cost
        )

        # 计算缓存节省
        if cached and usage.cached_tokens > 0:
            full_cost = (usage.cached_tokens / 1_000_000) * pricing.input_price
            cached_cost = (usage.cached_tokens / 1_000_000) * pricing.cached_input_price
            self._summary.saved_by_cache += (full_cost - cached_cost)

        logger.info(
            "cost_recorded",
            model=model,
            cost_usd=f"${cost:.6f}",
            tokens=usage.total_tokens,
            task=task_type.value,
            session_total=f"${self._session_costs[session_id]:.6f}",
        )

        return record

    def get_summary(self) -> dict[str, Any]:
        """获取成本汇总报告."""
        # 计算 ROI：对比"全部用 gpt-4o"的假设成本
        baseline_pricing = self.PRICING[self.BASELINE_MODEL]
        baseline_cost = sum(
            baseline_pricing.calculate_cost(r.usage) for r in self._records
        )

        optimization_rate = (
            (1 - self._summary.total_cost_usd / baseline_cost) * 100
            if baseline_cost > 0
            else 0.0
        )

        return {
            "total_cost_usd": f"${self._summary.total_cost_usd:.4f}",
            "total_calls": self._summary.total_calls,
            "total_tokens": self._summary.total_tokens,
            "avg_cost_per_call": (
                f"${self._summary.total_cost_usd / self._summary.total_calls:.6f}"
                if self._summary.total_calls > 0 else "$0"
            ),
            "by_model": {
                k: f"${v:.4f}" for k, v in self._summary.by_model.items()
            },
            "by_task": {
                k: f"${v:.4f}" for k, v in self._summary.by_task.items()
            },
            "by_provider": {
                k: f"${v:.4f}" for k, v in self._summary.by_provider.items()
            },
            "cache_savings": {
                "saved_usd": f"${self._summary.saved_by_cache:.4f}",
                "cached_tokens": self._summary.total_cached_tokens,
            },
            "roi_analysis": {
                "actual_cost": f"${self._summary.total_cost_usd:.4f}",
                "baseline_cost_gpt4o": f"${baseline_cost:.4f}",
                "optimization_rate": f"{optimization_rate:.1f}%",
            },
        }

    def get_session_cost(self, session_id: str) -> float:
        """获取指定 Session 的总成本."""
        return self._session_costs.get(session_id, 0.0)

    def get_recent_records(self, limit: int = 50) -> list[dict[str, Any]]:
        """获取最近的成本记录."""
        return [
            {
                "model": r.model,
                "provider": r.provider,
                "task_type": r.task_type,
                "cost_usd": f"${r.cost_usd:.6f}",
                "tokens": r.usage.total_tokens,
                "cached": r.cached,
            }
            for r in self._records[-limit:]
        ]
