"""LLM 模块公共类型定义."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class TaskType(str, Enum):
    """Agent 任务类型."""
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
    """Token 用量."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0


class LLMResponse(BaseModel):
    """LLM 响应封装."""
    content: str
    model: str
    provider: str
    usage: TokenUsage = Field(default_factory=TokenUsage)
    finish_reason: str = "stop"
    latency_ms: int = 0
    cached: bool = False
    structured_output: Any = None


@dataclass
class ModelConfig:
    """模型配置."""
    provider: str
    model: str
    temperature: float = 0.0
    max_tokens: int = 4096
    timeout_seconds: float = 60.0


@dataclass
class LLMCallContext:
    """LLM 调用上下文."""
    task_type: TaskType
    complexity: Complexity = Complexity.MODERATE
    sensitivity: Sensitivity = Sensitivity.LOW
    request_id: str = ""
    session_id: str = ""
    user_id: str = ""
    force_provider: str | None = None
