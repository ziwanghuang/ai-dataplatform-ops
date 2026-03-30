"""配置模块单元测试."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from aiops.core.config import Environment, LLMConfig, Settings


class TestSettings:
    """Settings 主配置类测试."""

    def test_default_values(self) -> None:
        """默认值应为 dev 环境."""
        settings = Settings()
        assert settings.env == Environment.DEV
        assert settings.debug is False
        assert settings.app_name == "aiops-agent"
        assert settings.version == "0.1.0"

    def test_env_override(self) -> None:
        """环境变量应覆盖默认值."""
        with patch.dict(os.environ, {"ENV": "production", "DEBUG": "false"}):
            settings = Settings()
            assert settings.env == Environment.PRODUCTION
            assert settings.is_production is True

    def test_env_case_insensitive(self) -> None:
        """环境名应大小写不敏感."""
        with patch.dict(os.environ, {"ENV": "Production"}):
            settings = Settings()
            assert settings.env == Environment.PRODUCTION

    def test_secret_str_masking(self) -> None:
        """SecretStr 应该在 str() 时脱敏."""
        from pydantic import SecretStr

        config = LLMConfig(openai_api_key=SecretStr("sk-real-key-12345"))
        # str() 不泄露真实值
        assert "sk-real-key-12345" not in str(config.openai_api_key)
        # get_secret_value() 获取真实值
        assert config.openai_api_key.get_secret_value() == "sk-real-key-12345"

    def test_invalid_env_rejected(self) -> None:
        """无效的环境名应抛出 ValidationError."""
        with patch.dict(os.environ, {"ENV": "invalid_env"}):
            with pytest.raises(Exception):
                Settings()

    def test_sub_config_defaults(self) -> None:
        """子配置应有合理默认值."""
        settings = Settings()
        assert settings.llm.primary_model == "gpt-4o-2024-11-20"
        assert settings.llm.temperature == 0.0
        assert settings.rag.top_k_final == 5
        assert settings.mcp.gateway_url == "http://localhost:3000"
        assert settings.hitl.enabled is True
        assert settings.token_budget.daily_limit == 500000
