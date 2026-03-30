"""
请求上下文管理

使用 contextvars 实现异步安全的请求上下文传播，
确保 trace_id/session_id/user_id 在整个请求链路中可访问。
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

# Context variables — 异步安全
_request_context: ContextVar[RequestContext | None] = ContextVar(
    "_request_context", default=None
)


@dataclass(frozen=True)
class RequestContext:
    """请求级别上下文，在整个调用链路中传播."""

    trace_id: str
    session_id: str = ""
    user_id: str = ""
    agent_name: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def generate_trace_id() -> str:
        """生成唯一 trace_id."""
        return uuid.uuid4().hex[:16]


def set_request_context(ctx: RequestContext) -> None:
    """设置当前请求上下文."""
    _request_context.set(ctx)


def get_request_context() -> RequestContext | None:
    """获取当前请求上下文."""
    return _request_context.get()


def get_trace_id() -> str:
    """获取当前 trace_id，没有上下文时返回 'no-trace'."""
    ctx = _request_context.get()
    return ctx.trace_id if ctx else "no-trace"


def clear_request_context() -> None:
    """清除请求上下文."""
    _request_context.set(None)
