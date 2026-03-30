"""LLM Router 单元测试."""

from __future__ import annotations

from aiops.llm.router import ModelRouter
from aiops.llm.types import Complexity, LLMCallContext, Sensitivity, TaskType


class TestModelRouter:
    def setup_method(self) -> None:
        self.router = ModelRouter()

    def test_triage_routes_to_deepseek(self) -> None:
        ctx = LLMCallContext(task_type=TaskType.TRIAGE)
        config = self.router.route(ctx)
        assert config.provider == "deepseek"
        assert "deepseek" in config.model

    def test_diagnostic_routes_to_gpt4o(self) -> None:
        ctx = LLMCallContext(task_type=TaskType.DIAGNOSTIC)
        config = self.router.route(ctx)
        assert config.provider == "openai"
        assert "gpt-4o" in config.model

    def test_high_sensitivity_routes_to_local(self) -> None:
        ctx = LLMCallContext(task_type=TaskType.DIAGNOSTIC, sensitivity=Sensitivity.HIGH)
        config = self.router.route(ctx)
        assert config.provider == "local"
        assert "qwen" in config.model

    def test_report_routes_to_mini(self) -> None:
        ctx = LLMCallContext(task_type=TaskType.REPORT)
        config = self.router.route(ctx)
        assert "mini" in config.model

    def test_force_provider(self) -> None:
        ctx = LLMCallContext(task_type=TaskType.TRIAGE, force_provider="anthropic")
        config = self.router.route(ctx)
        assert config.provider == "anthropic"

    def test_patrol_routes_to_lightweight(self) -> None:
        ctx = LLMCallContext(task_type=TaskType.PATROL)
        config = self.router.route(ctx)
        assert "mini" in config.model
