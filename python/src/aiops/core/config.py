"""
统一配置管理：Pydantic BaseSettings + .env + 环境变量

优先级（从高到低）：
1. 环境变量（K8s ConfigMap/Secret 注入）
2. .env 文件（本地开发）
3. 默认值
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(str, Enum):
    DEV = "dev"
    STAGING = "staging"
    PRODUCTION = "production"


class LLMConfig(BaseSettings):
    """LLM 相关配置."""

    model_config = SettingsConfigDict(env_prefix="LLM_")

    primary_model: str = "gpt-4o-2024-11-20"
    fallback_model: str = "claude-3-5-sonnet-20241022"
    local_model: str = "qwen2.5-72b-instruct"
    triage_model: str = "deepseek-chat"
    temperature: float = 0.0
    max_retries: int = 3
    timeout_seconds: int = 60
    openai_api_key: SecretStr = Field(default=SecretStr(""))
    anthropic_api_key: SecretStr = Field(default=SecretStr(""))
    deepseek_api_key: SecretStr = Field(default=SecretStr(""))


class RAGConfig(BaseSettings):
    """RAG 检索配置."""

    model_config = SettingsConfigDict(env_prefix="RAG_")

    milvus_uri: str = "http://localhost:19530"
    milvus_collection: str = "ops_documents"
    es_url: str = "http://localhost:9200"
    es_index: str = "ops_docs"
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: SecretStr = Field(default=SecretStr(""))
    embedding_model: str = "BAAI/bge-m3"
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    top_k_dense: int = 20
    top_k_sparse: int = 20
    top_k_final: int = 5
    rrf_k: int = 60


class MCPConfig(BaseSettings):
    """MCP 连接配置."""

    model_config = SettingsConfigDict(env_prefix="MCP_")

    gateway_url: str = "http://localhost:3000"
    tbds_url: str = ""
    tbds_api_key: SecretStr = Field(default=SecretStr(""))
    timeout_seconds: int = 30
    max_retries: int = 2


class HITLConfig(BaseSettings):
    """人机协作配置."""

    model_config = SettingsConfigDict(env_prefix="HITL_")

    enabled: bool = True
    timeout_seconds: int = 1800  # 30 分钟审批超时
    auto_approve_level: int = 0  # Level 0 (NONE) 自动通过
    wecom_webhook_url: str = ""
    notification_channels: list[str] = Field(default_factory=lambda: ["websocket"])


class ObservabilityConfig(BaseSettings):
    """可观测性配置."""

    model_config = SettingsConfigDict(env_prefix="OTEL_")

    enabled: bool = True
    exporter_otlp_endpoint: str = "http://localhost:4317"
    service_name: str = "aiops-agent"
    langfuse_host: str = "http://localhost:3001"
    langfuse_public_key: str = ""
    langfuse_secret_key: SecretStr = Field(default=SecretStr(""))


class TokenBudgetConfig(BaseSettings):
    """Token 预算配置."""

    model_config = SettingsConfigDict(env_prefix="TOKEN_")

    budget_triage: int = 2000
    budget_diagnosis: int = 15000
    budget_planning: int = 8000
    budget_report: int = 5000
    budget_patrol: int = 3000
    daily_limit: int = 500000
    alert_threshold: float = 0.8  # 80% 预算时告警


class DatabaseConfig(BaseSettings):
    """数据库配置."""

    model_config = SettingsConfigDict(env_prefix="DB_")

    postgres_url: SecretStr = Field(
        default=SecretStr("postgresql+asyncpg://aiops:aiops@localhost:5432/aiops")
    )
    redis_url: str = "redis://localhost:6379/0"
    redis_cache_ttl: int = 300  # 5 分钟


class Settings(BaseSettings):
    """主配置类 — 聚合所有子配置."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # 全局
    env: Environment = Environment.DEV
    debug: bool = False
    app_name: str = "aiops-agent"
    version: str = "0.1.0"
    prompt_version: str = "v2.3"

    # 子配置
    llm: LLMConfig = Field(default_factory=LLMConfig)
    rag: RAGConfig = Field(default_factory=RAGConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    hitl: HITLConfig = Field(default_factory=HITLConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    token_budget: TokenBudgetConfig = Field(default_factory=TokenBudgetConfig)
    db: DatabaseConfig = Field(default_factory=DatabaseConfig)

    @field_validator("env", mode="before")
    @classmethod
    def validate_env(cls, v: str) -> str:
        return v.lower()

    @property
    def is_production(self) -> bool:
        return self.env == Environment.PRODUCTION


@lru_cache
def get_settings() -> Settings:
    """单例获取配置，应用生命周期内只加载一次."""
    return Settings()


# 便捷引用
settings = get_settings()
