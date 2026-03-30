"""
OTel 分布式追踪 — OpenTelemetry 集成

WHY OTel 而不是自己实现追踪：
- 行业标准，与 Jaeger/Zipkin/Grafana Tempo 无缝对接
- 自动上下文传播（Python asyncio + HTTP header）
- LangGraph 每个节点自动产生子 Span

追踪架构（参考 17-可观测性与全链路追踪.md §2）：
  Agent 请求 → Triage Span → Diagnostic Span → Tool Call Spans → LLM Call Spans
  所有 Span 共享同一个 trace_id，形成完整的调用树。
"""

from __future__ import annotations

import functools
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Callable, TypeVar

from aiops.core.logging import get_logger

logger = get_logger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


# ──────────────────────────────────────────────────
# OTel 初始化
# ──────────────────────────────────────────────────

class TracingConfig:
    """追踪配置."""
    service_name: str = "aiops-agent"
    jaeger_endpoint: str = "http://localhost:4317"  # gRPC OTLP
    sample_rate: float = 1.0        # 开发全采样，生产 0.1
    max_span_attributes: int = 128
    enabled: bool = True


def init_tracing(config: TracingConfig | None = None) -> Any:
    """
    初始化 OpenTelemetry 追踪.

    Returns:
        TracerProvider 实例（或 None 如果依赖不存在）

    WHY lazy import：
    - opentelemetry-sdk 是可选依赖
    - 开发时不安装也能跑（降级为 noop tracer）
    """
    config = config or TracingConfig()

    if not config.enabled:
        logger.info("tracing_disabled")
        return None

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create({
            "service.name": config.service_name,
            "service.version": "1.0.0",
            "deployment.environment": "development",
        })

        provider = TracerProvider(resource=resource)

        # OTLP gRPC Exporter → Jaeger
        exporter = OTLPSpanExporter(endpoint=config.jaeger_endpoint)
        processor = BatchSpanProcessor(exporter)
        provider.add_span_processor(processor)

        trace.set_tracer_provider(provider)

        logger.info(
            "tracing_initialized",
            service=config.service_name,
            endpoint=config.jaeger_endpoint,
            sample_rate=config.sample_rate,
        )
        return provider

    except ImportError:
        logger.warning(
            "tracing_init_skipped",
            reason="opentelemetry-sdk not installed",
        )
        return None


def get_tracer(name: str = "aiops") -> Any:
    """获取 Tracer 实例."""
    try:
        from opentelemetry import trace
        return trace.get_tracer(name)
    except ImportError:
        return _NoopTracer()


# ──────────────────────────────────────────────────
# 追踪装饰器
# ──────────────────────────────────────────────────

def trace_agent(
    span_name: str | None = None,
    attributes: dict[str, Any] | None = None,
) -> Callable[[F], F]:
    """
    Agent 节点追踪装饰器.

    用法:
        @trace_agent("triage_node")
        async def execute(self, state: AgentState) -> AgentState:
            ...

    每次调用自动创建子 Span，记录：
    - 执行时间
    - 节点名称
    - 输入/输出状态 key
    - 异常信息
    """
    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            name = span_name or func.__qualname__
            tracer = get_tracer()

            try:
                from opentelemetry import trace as otel_trace

                with tracer.start_as_current_span(
                    name,
                    kind=otel_trace.SpanKind.INTERNAL,
                ) as span:
                    # 设置自定义属性
                    if attributes:
                        for k, v in attributes.items():
                            span.set_attribute(k, str(v))

                    span.set_attribute("agent.node", name)

                    start = time.monotonic()
                    try:
                        result = await func(*args, **kwargs)
                        latency_ms = int((time.monotonic() - start) * 1000)
                        span.set_attribute("agent.latency_ms", latency_ms)
                        span.set_attribute("agent.status", "success")
                        return result
                    except Exception as e:
                        latency_ms = int((time.monotonic() - start) * 1000)
                        span.set_attribute("agent.latency_ms", latency_ms)
                        span.set_attribute("agent.status", "error")
                        span.set_attribute("agent.error", str(e))
                        span.record_exception(e)
                        span.set_status(
                            otel_trace.Status(otel_trace.StatusCode.ERROR, str(e))
                        )
                        raise

            except ImportError:
                # OTel 未安装，直接执行
                return await func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]
    return decorator


def trace_llm_call(
    model: str = "",
    task_type: str = "",
) -> Callable[[F], F]:
    """
    LLM 调用追踪装饰器.

    额外记录：
    - model 名称
    - token 用量
    - 任务类型
    - 是否缓存命中
    """
    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            tracer = get_tracer()

            try:
                from opentelemetry import trace as otel_trace

                with tracer.start_as_current_span(
                    f"llm_call.{task_type or 'unknown'}",
                    kind=otel_trace.SpanKind.CLIENT,
                ) as span:
                    span.set_attribute("llm.model", model)
                    span.set_attribute("llm.task_type", task_type)

                    start = time.monotonic()
                    result = await func(*args, **kwargs)
                    latency_ms = int((time.monotonic() - start) * 1000)

                    span.set_attribute("llm.latency_ms", latency_ms)

                    # 尝试从结果中提取 token 信息
                    if hasattr(result, "usage"):
                        usage = result.usage
                        span.set_attribute(
                            "llm.tokens.total",
                            getattr(usage, "total_tokens", 0),
                        )
                    if hasattr(result, "cached"):
                        span.set_attribute("llm.cached", result.cached)

                    return result

            except ImportError:
                return await func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]
    return decorator


def trace_tool_call(tool_name: str = "") -> Callable[[F], F]:
    """MCP 工具调用追踪装饰器."""
    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            tracer = get_tracer()

            try:
                from opentelemetry import trace as otel_trace

                with tracer.start_as_current_span(
                    f"tool_call.{tool_name or 'unknown'}",
                    kind=otel_trace.SpanKind.CLIENT,
                ) as span:
                    span.set_attribute("tool.name", tool_name)

                    start = time.monotonic()
                    try:
                        result = await func(*args, **kwargs)
                        latency_ms = int((time.monotonic() - start) * 1000)
                        span.set_attribute("tool.latency_ms", latency_ms)
                        span.set_attribute("tool.status", "success")
                        return result
                    except Exception as e:
                        span.set_attribute("tool.status", "error")
                        span.record_exception(e)
                        raise

            except ImportError:
                return await func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]
    return decorator


@asynccontextmanager
async def trace_span(
    name: str,
    attributes: dict[str, Any] | None = None,
) -> AsyncGenerator[Any, None]:
    """
    上下文管理器版本的 Span 创建.

    用法:
        async with trace_span("custom_operation", {"key": "value"}) as span:
            ...
    """
    tracer = get_tracer()

    try:
        from opentelemetry import trace as otel_trace

        with tracer.start_as_current_span(name) as span:
            if attributes:
                for k, v in attributes.items():
                    span.set_attribute(k, str(v))
            yield span

    except ImportError:
        yield None


# ──────────────────────────────────────────────────
# Noop Tracer（OTel 未安装时的兜底）
# ──────────────────────────────────────────────────

class _NoopSpan:
    """空 Span."""
    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def record_exception(self, exc: Exception) -> None:
        pass

    def set_status(self, status: Any) -> None:
        pass

    def __enter__(self) -> _NoopSpan:
        return self

    def __exit__(self, *args: Any) -> None:
        pass


class _NoopTracer:
    """空 Tracer."""
    def start_as_current_span(self, name: str, **kwargs: Any) -> _NoopSpan:
        return _NoopSpan()
