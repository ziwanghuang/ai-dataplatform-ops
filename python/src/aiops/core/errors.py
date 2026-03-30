"""
分层错误码体系

所有业务异常都继承自 AIOpsError，
包含错误码、HTTP 状态码、用户友好消息和内部技术详情。
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel


class ErrorCode(str, Enum):
    """错误码枚举."""

    # === System (S) ===
    CONFIG_LOAD_FAILED = "AIOPS-S0101"
    SERVICE_UNAVAILABLE = "AIOPS-S0501"
    INTERNAL_ERROR = "AIOPS-S0701"

    # === Business (B) ===
    UNAUTHORIZED = "AIOPS-B0201"
    FORBIDDEN = "AIOPS-B0202"
    INVALID_INPUT = "AIOPS-B0301"
    SESSION_NOT_FOUND = "AIOPS-B0701"
    APPROVAL_TIMEOUT = "AIOPS-B0401"
    APPROVAL_REJECTED = "AIOPS-B0702"

    # === LLM (L) ===
    LLM_API_ERROR = "AIOPS-L0601"
    LLM_RATE_LIMITED = "AIOPS-L0602"
    LLM_TIMEOUT = "AIOPS-L0401"
    LLM_PARSE_ERROR = "AIOPS-L0701"
    LLM_ALL_PROVIDERS_DOWN = "AIOPS-L0603"
    TOKEN_BUDGET_EXCEEDED = "AIOPS-L0501"

    # === Tool (T) ===
    TOOL_NOT_FOUND = "AIOPS-T0301"
    TOOL_CALL_FAILED = "AIOPS-T0601"
    TOOL_TIMEOUT = "AIOPS-T0402"
    TOOL_PARAM_INVALID = "AIOPS-T0302"
    TOOL_CIRCUIT_OPEN = "AIOPS-T0602"
    TOOL_RATE_LIMITED = "AIOPS-T0603"

    # === RAG (R) ===
    RAG_INDEX_ERROR = "AIOPS-R0601"
    RAG_NO_RESULTS = "AIOPS-R0701"
    RAG_RERANK_FAILED = "AIOPS-R0602"


# 错误码 → HTTP 状态码映射
_ERROR_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.INVALID_INPUT: 400,
    ErrorCode.TOOL_NOT_FOUND: 404,
    ErrorCode.SESSION_NOT_FOUND: 404,
    ErrorCode.LLM_RATE_LIMITED: 429,
    ErrorCode.TOOL_RATE_LIMITED: 429,
    ErrorCode.TOKEN_BUDGET_EXCEEDED: 429,
    ErrorCode.LLM_TIMEOUT: 504,
    ErrorCode.TOOL_TIMEOUT: 504,
    ErrorCode.SERVICE_UNAVAILABLE: 503,
    ErrorCode.LLM_ALL_PROVIDERS_DOWN: 503,
}


class ErrorDetail(BaseModel):
    """API 错误响应体."""

    code: str
    message: str
    detail: str | None = None
    trace_id: str | None = None


class AIOpsError(Exception):
    """AIOps 业务异常基类."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        detail: str | None = None,
        cause: Exception | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.detail = detail
        self.cause = cause
        self.context = context or {}
        super().__init__(f"[{code.value}] {message}")

    @property
    def http_status(self) -> int:
        return _ERROR_HTTP_STATUS.get(self.code, 500)

    def to_response(self, trace_id: str | None = None) -> ErrorDetail:
        return ErrorDetail(
            code=self.code.value,
            message=self.message,
            detail=self.detail,
            trace_id=trace_id,
        )


class LLMError(AIOpsError):
    """LLM 相关异常."""


class ToolError(AIOpsError):
    """工具调用异常."""


class RAGError(AIOpsError):
    """RAG 检索异常."""


class SecurityError(AIOpsError):
    """安全相关异常."""
