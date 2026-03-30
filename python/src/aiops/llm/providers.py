"""
LLM Provider 管理 — 多供应商适配 + 熔断状态机

WHY 独立 Provider 层而不是把供应商逻辑塞到 LLMClient：
- 每个供应商的 API 特征不同（速率限制、错误码、重试策略）
- 熔断器需要按供应商实例级别隔离
- 容灾切换需要知道每个供应商的健康状态

熔断状态机（参考 02-LLM客户端与多模型路由.md §3.3）：
  CLOSED ──(连续 5 次失败)──> OPEN
  OPEN ──(60s 冷却)──> HALF_OPEN
  HALF_OPEN ──(1 次成功)──> CLOSED
  HALF_OPEN ──(1 次失败)──> OPEN
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from aiops.core.logging import get_logger

logger = get_logger(__name__)


# ──────────────────────────────────────────────────
# 熔断器状态机
# ──────────────────────────────────────────────────

class CircuitState(str, Enum):
    """熔断器三态."""
    CLOSED = "closed"          # 正常
    OPEN = "open"              # 熔断中（拒绝所有请求）
    HALF_OPEN = "half_open"    # 试探中（允许 1 个请求通过）


@dataclass
class CircuitBreakerConfig:
    """熔断器配置."""
    failure_threshold: int = 5       # 连续失败次数触发熔断
    recovery_timeout: float = 60.0   # 熔断后冷却秒数
    half_open_max_calls: int = 1     # 半开状态允许的最大请求数
    success_threshold: int = 1       # 半开状态恢复所需的连续成功次数


@dataclass
class CircuitBreakerState:
    """
    单个 Provider 的熔断器状态.

    WHY 用 dataclass 而不是简单 dict：
    - 状态转移逻辑需要明确的类型约束
    - dataclass 提供 __repr__ 便于日志输出
    """
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    last_failure_time: float = 0.0
    last_state_change: float = field(default_factory=time.monotonic)
    total_requests: int = 0
    total_failures: int = 0

    def record_success(self) -> None:
        """记录一次成功调用."""
        self.total_requests += 1
        self.failure_count = 0

        if self.state == CircuitState.HALF_OPEN:
            self.success_count += 1
            if self.success_count >= 1:
                self._transition_to(CircuitState.CLOSED)
                self.success_count = 0

    def record_failure(self) -> None:
        """记录一次失败调用."""
        self.total_requests += 1
        self.total_failures += 1
        self.failure_count += 1
        self.last_failure_time = time.monotonic()

        if self.state == CircuitState.HALF_OPEN:
            self._transition_to(CircuitState.OPEN)
        elif self.state == CircuitState.CLOSED:
            if self.failure_count >= 5:  # failure_threshold
                self._transition_to(CircuitState.OPEN)

    def is_available(self, recovery_timeout: float = 60.0) -> bool:
        """判断当前 Provider 是否可用."""
        if self.state == CircuitState.CLOSED:
            return True

        if self.state == CircuitState.OPEN:
            elapsed = time.monotonic() - self.last_failure_time
            if elapsed >= recovery_timeout:
                self._transition_to(CircuitState.HALF_OPEN)
                return True
            return False

        # HALF_OPEN: 允许探测请求通过
        return True

    def _transition_to(self, new_state: CircuitState) -> None:
        old = self.state
        self.state = new_state
        self.last_state_change = time.monotonic()
        logger.info(
            "circuit_breaker_transition",
            old_state=old.value,
            new_state=new_state.value,
            failure_count=self.failure_count,
        )


# ──────────────────────────────────────────────────
# Provider 定义
# ──────────────────────────────────────────────────

@dataclass
class ProviderConfig:
    """
    单个 LLM 供应商配置.

    WHY 单独抽 ProviderConfig 而不是直接用 ModelConfig：
    - 一个 Provider 可能有多个模型（如 OpenAI 有 gpt-4o、gpt-4o-mini）
    - Provider 级别的限流、API Key、base_url 是共享的
    """
    name: str
    api_key_env: str            # 环境变量名，不在代码中硬编码 key
    base_url: str | None = None
    max_rpm: int = 500          # 每分钟最大请求数
    max_tpm: int = 200_000      # 每分钟最大 Token 数
    priority: int = 0           # 容灾优先级（越小越优先）
    models: list[str] = field(default_factory=list)
    is_local: bool = False      # 是否本地部署（敏感数据可用）


class ProviderManager:
    """
    Provider 全生命周期管理.

    职责：
    1. Provider 注册与配置
    2. 熔断器状态管理
    3. 容灾切换策略
    4. 健康状态上报

    WHY 不直接用 litellm 的 Router：
    - litellm Router 是通用方案，缺少 AIOps 特定的容灾策略
    - 我们需要"敏感数据只走本地"的路由约束，litellm 不支持
    - 熔断器状态需要与 Prometheus 指标联动
    """

    # 内置 Provider 列表（参考 02-LLM客户端与多模型路由.md §2.2）
    DEFAULT_PROVIDERS: list[ProviderConfig] = [
        ProviderConfig(
            name="deepseek",
            api_key_env="DEEPSEEK_API_KEY",
            base_url="https://api.deepseek.com",
            max_rpm=1000,
            max_tpm=500_000,
            priority=0,  # 成本最低，分诊首选
            models=["deepseek-chat", "deepseek-reasoner"],
        ),
        ProviderConfig(
            name="openai",
            api_key_env="OPENAI_API_KEY",
            max_rpm=500,
            max_tpm=200_000,
            priority=1,  # 推理能力最强，诊断首选
            models=["gpt-4o", "gpt-4o-mini"],
        ),
        ProviderConfig(
            name="anthropic",
            api_key_env="ANTHROPIC_API_KEY",
            max_rpm=400,
            max_tpm=150_000,
            priority=2,  # 容灾备选
            models=["claude-3-5-sonnet-20241022"],
        ),
        ProviderConfig(
            name="local",
            api_key_env="LOCAL_LLM_API_KEY",
            base_url="http://localhost:8080/v1",
            max_rpm=100,
            max_tpm=50_000,
            priority=10,  # 仅处理敏感数据
            models=["qwen2.5-72b-instruct"],
            is_local=True,
        ),
    ]

    def __init__(self, providers: list[ProviderConfig] | None = None) -> None:
        self._providers: dict[str, ProviderConfig] = {}
        self._circuit_breakers: dict[str, CircuitBreakerState] = {}
        self._request_counts: dict[str, int] = {}  # 滑动窗口计数
        self._window_start: float = time.monotonic()

        # 注册 Provider
        for p in (providers or self.DEFAULT_PROVIDERS):
            self.register(p)

    def register(self, config: ProviderConfig) -> None:
        """注册一个 Provider."""
        self._providers[config.name] = config
        self._circuit_breakers[config.name] = CircuitBreakerState()
        self._request_counts[config.name] = 0
        logger.info(
            "provider_registered",
            provider=config.name,
            models=config.models,
            priority=config.priority,
        )

    def get_available_providers(
        self,
        required_model: str | None = None,
        sensitive: bool = False,
    ) -> list[ProviderConfig]:
        """
        获取当前可用的 Provider 列表（按优先级排序）.

        Args:
            required_model: 如果指定，只返回支持该模型的 Provider
            sensitive: 如果为 True，只返回本地 Provider（敏感数据不出境）

        Returns:
            按 priority 排序的可用 Provider 列表
        """
        available: list[ProviderConfig] = []

        for name, config in self._providers.items():
            cb = self._circuit_breakers[name]

            # 熔断中的跳过
            if not cb.is_available():
                logger.debug("provider_circuit_open", provider=name)
                continue

            # 敏感数据只走本地
            if sensitive and not config.is_local:
                continue

            # 模型过滤
            if required_model and required_model not in config.models:
                continue

            # 限流检查
            if self._is_rate_limited(name):
                logger.warning("provider_rate_limited", provider=name)
                continue

            available.append(config)

        # 按优先级排序
        available.sort(key=lambda p: p.priority)
        return available

    def get_fallback_chain(
        self,
        primary_provider: str,
        model: str,
    ) -> list[tuple[str, str]]:
        """
        获取容灾链——主 Provider 失败时的兜底顺序.

        返回: [(provider_name, model_name), ...]

        WHY 返回 provider+model 对而不是只返回 provider：
        - 不同 Provider 的模型能力不同
        - gpt-4o 挂了应该切到 claude-3-5-sonnet，而不是 gpt-4o-mini
        """
        # 能力等级映射（同等级可互相替代）
        model_tier: dict[str, list[tuple[str, str]]] = {
            "tier1_reasoning": [  # 强推理
                ("openai", "gpt-4o"),
                ("anthropic", "claude-3-5-sonnet-20241022"),
                ("local", "qwen2.5-72b-instruct"),
            ],
            "tier2_balanced": [  # 均衡
                ("openai", "gpt-4o-mini"),
                ("deepseek", "deepseek-chat"),
            ],
            "tier3_fast": [  # 快速
                ("deepseek", "deepseek-chat"),
                ("openai", "gpt-4o-mini"),
            ],
        }

        # 确定当前模型所在的 tier
        target_tier = "tier2_balanced"  # 默认
        for tier_name, tier_models in model_tier.items():
            for p, m in tier_models:
                if p == primary_provider and m == model:
                    target_tier = tier_name
                    break

        # 构建容灾链（排除主 Provider + 排除熔断中的）
        chain: list[tuple[str, str]] = []
        for p_name, m_name in model_tier.get(target_tier, []):
            if p_name == primary_provider:
                continue
            cb = self._circuit_breakers.get(p_name)
            if cb and cb.is_available():
                chain.append((p_name, m_name))

        return chain

    def record_success(self, provider_name: str) -> None:
        """记录调用成功."""
        if cb := self._circuit_breakers.get(provider_name):
            cb.record_success()

    def record_failure(self, provider_name: str, error: Exception | None = None) -> None:
        """记录调用失败."""
        if cb := self._circuit_breakers.get(provider_name):
            cb.record_failure()
            logger.warning(
                "provider_failure",
                provider=provider_name,
                error=str(error) if error else "unknown",
                state=cb.state.value,
                failure_count=cb.failure_count,
            )

    def get_health_report(self) -> dict[str, Any]:
        """获取所有 Provider 的健康报告."""
        report: dict[str, Any] = {}
        for name, cb in self._circuit_breakers.items():
            config = self._providers[name]
            report[name] = {
                "state": cb.state.value,
                "failure_count": cb.failure_count,
                "total_requests": cb.total_requests,
                "total_failures": cb.total_failures,
                "error_rate": (
                    cb.total_failures / cb.total_requests
                    if cb.total_requests > 0
                    else 0.0
                ),
                "priority": config.priority,
                "models": config.models,
                "is_local": config.is_local,
            }
        return report

    def _is_rate_limited(self, provider_name: str) -> bool:
        """检查 Provider 是否被限流."""
        # 滑动窗口：每分钟重置
        now = time.monotonic()
        if now - self._window_start > 60.0:
            self._request_counts = {k: 0 for k in self._request_counts}
            self._window_start = now

        config = self._providers.get(provider_name)
        if not config:
            return False

        count = self._request_counts.get(provider_name, 0)
        return count >= config.max_rpm
