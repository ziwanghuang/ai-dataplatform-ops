"""LLMClient — 统一 LLM 调用入口."""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel

from aiops.core.config import settings
from aiops.core.errors import ErrorCode, LLMError
from aiops.core.logging import get_logger
from aiops.llm.router import ModelRouter
from aiops.llm.types import (
    LLMCallContext,
    LLMResponse,
    TokenUsage,
)

logger = get_logger(__name__)


class LLMClient:
    """统一 LLM 客户端，封装路由/重试/超时/容灾."""

    def __init__(self) -> None:
        self._router = ModelRouter()
        self._call_count = 0

    async def chat(
        self,
        messages: list[dict[str, Any]],
        context: LLMCallContext,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """调用 LLM 获取文本响应."""
        config = self._router.route(context)
        self._call_count += 1
        start = time.monotonic()

        try:
            import litellm  # type: ignore[import-untyped]

            response = await litellm.acompletion(
                model=config.model,
                messages=messages,
                temperature=config.temperature,
                max_tokens=config.max_tokens,
                timeout=config.timeout_seconds,
                tools=tools,
                **kwargs,
            )

            latency_ms = int((time.monotonic() - start) * 1000)
            content = response.choices[0].message.content or ""
            usage = TokenUsage(
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                total_tokens=response.usage.total_tokens,
            )

            logger.info(
                "llm_call_success",
                model=config.model,
                latency_ms=latency_ms,
                tokens=usage.total_tokens,
                task=context.task_type.value,
            )

            return LLMResponse(
                content=content,
                model=config.model,
                provider=config.provider,
                usage=usage,
                latency_ms=latency_ms,
                finish_reason=response.choices[0].finish_reason or "stop",
            )

        except Exception as e:
            latency_ms = int((time.monotonic() - start) * 1000)
            logger.error(
                "llm_call_failed",
                model=config.model,
                error=str(e),
                latency_ms=latency_ms,
            )
            raise LLMError(
                code=ErrorCode.LLM_API_ERROR,
                message=f"LLM call failed: {e}",
                detail=f"model={config.model}, provider={config.provider}",
                cause=e,
            ) from e

    async def chat_structured(
        self,
        messages: list[dict[str, Any]],
        response_model: type[BaseModel],
        context: LLMCallContext,
        max_retries: int = 3,
        **kwargs: Any,
    ) -> BaseModel:
        """调用 LLM 获取结构化输出（instructor）."""
        config = self._router.route(context)

        try:
            import instructor  # type: ignore[import-untyped]
            import litellm  # type: ignore[import-untyped]

            client = instructor.from_litellm(litellm.acompletion)
            result = await client.chat.completions.create(
                model=config.model,
                messages=messages,
                response_model=response_model,
                max_retries=max_retries,
                temperature=config.temperature,
                **kwargs,
            )

            logger.info(
                "llm_structured_success",
                model=config.model,
                response_type=response_model.__name__,
            )
            return result

        except Exception as e:
            raise LLMError(
                code=ErrorCode.LLM_PARSE_ERROR,
                message=f"Structured output failed: {e}",
                detail=f"model={config.model}, target={response_model.__name__}",
                cause=e,
            ) from e
