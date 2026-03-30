"""ModelRouter — 按 (task_type, complexity, sensitivity) 三维路由到最优模型."""

from __future__ import annotations

from aiops.core.config import settings
from aiops.core.logging import get_logger
from aiops.llm.types import (
    Complexity,
    LLMCallContext,
    ModelConfig,
    Sensitivity,
    TaskType,
)

logger = get_logger(__name__)


class ModelRouter:
    """多模型智能路由."""

    ROUTING_TABLE: list[tuple[str, str, str, ModelConfig]] = [
        # Triage: DeepSeek-V3 (低成本高准确率)
        ("triage", "any", "any", ModelConfig(
            provider="deepseek", model="deepseek-chat", temperature=0.0, max_tokens=1024,
        )),
        # Diagnostic: GPT-4o (强推理)
        ("diagnostic", "any", "low", ModelConfig(
            provider="openai", model="gpt-4o", temperature=0.0, max_tokens=4096,
        )),
        ("diagnostic", "any", "high", ModelConfig(
            provider="local", model="qwen2.5-72b-instruct", temperature=0.0, max_tokens=4096,
        )),
        # Planning
        ("planning", "simple", "low", ModelConfig(
            provider="openai", model="gpt-4o-mini", temperature=0.1, max_tokens=2048,
        )),
        ("planning", "complex", "low", ModelConfig(
            provider="openai", model="gpt-4o", temperature=0.1, max_tokens=4096,
        )),
        ("planning", "any", "high", ModelConfig(
            provider="local", model="qwen2.5-72b-instruct", temperature=0.1, max_tokens=4096,
        )),
        # Report: 轻量
        ("report", "any", "any", ModelConfig(
            provider="openai", model="gpt-4o-mini", temperature=0.3, max_tokens=4096,
        )),
        # Alert Correlation: 强推理
        ("alert_correlation", "any", "any", ModelConfig(
            provider="openai", model="gpt-4o", temperature=0.0, max_tokens=4096,
        )),
        # Patrol: 轻量
        ("patrol", "any", "any", ModelConfig(
            provider="openai", model="gpt-4o-mini", temperature=0.0, max_tokens=2048,
        )),
        # Remediation
        ("remediation", "any", "any", ModelConfig(
            provider="openai", model="gpt-4o", temperature=0.0, max_tokens=4096,
        )),
    ]

    def route(self, context: LLMCallContext) -> ModelConfig:
        """根据上下文选择最优模型."""
        if context.force_provider:
            return self._get_forced_config(context)

        for task, comp, sens, config in self.ROUTING_TABLE:
            if (self._match(task, context.task_type.value)
                    and self._match(comp, context.complexity.value)
                    and self._match(sens, context.sensitivity.value)):
                logger.debug(
                    "model_routed",
                    task_type=context.task_type.value,
                    model=config.model,
                )
                return config

        # 默认兜底
        return ModelConfig(provider="openai", model="gpt-4o-mini", temperature=0.0)

    @staticmethod
    def _match(pattern: str, value: str) -> bool:
        return pattern == "any" or pattern == value

    @staticmethod
    def _get_forced_config(context: LLMCallContext) -> ModelConfig:
        provider = context.force_provider or "openai"
        model_map = {
            "openai": "gpt-4o",
            "anthropic": "claude-3-5-sonnet-20241022",
            "deepseek": "deepseek-chat",
            "local": "qwen2.5-72b-instruct",
        }
        return ModelConfig(
            provider=provider,
            model=model_map.get(provider, "gpt-4o"),
        )
