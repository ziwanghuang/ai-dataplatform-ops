"""
LangFuse 集成 — LLM 可观测性

WHY LangFuse 而不是自己实现 LLM 可观测：
- LangFuse 开源，提供 trace/generation/score 三维视图
- 自带 Prompt 版本管理和 A/B 测试
- Web UI 开箱即用，比自建 Grafana Dashboard 省力

集成点（参考 17-可观测性与全链路追踪.md §4）：
1. LLMClient 每次调用自动上报 trace
2. Agent 节点切换自动记录 generation
3. 评估分数（准确率/延迟/成本）自动关联到 trace
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Generator

from aiops.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class LangFuseConfig:
    """LangFuse 配置."""
    public_key: str = ""
    secret_key: str = ""
    host: str = "https://cloud.langfuse.com"
    enabled: bool = False
    flush_interval: float = 5.0  # 秒
    sample_rate: float = 1.0     # 采样率


@dataclass
class TraceContext:
    """追踪上下文."""
    trace_id: str = ""
    session_id: str = ""
    user_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)


class LangFuseIntegration:
    """
    LangFuse 集成层.

    职责：
    1. 初始化 LangFuse 客户端
    2. 提供 trace/generation/score 上报接口
    3. 管理追踪上下文
    4. 依赖不存在时降级为 noop

    WHY 封装一层而不是直接用 langfuse SDK：
    - langfuse 是可选依赖（开发时可以不装）
    - 统一 API 方便后续切换到其他 LLM 可观测方案
    - 添加 AIOps 特定的上下文（组件、告警 ID 等）
    """

    def __init__(self, config: LangFuseConfig | None = None) -> None:
        self._config = config or LangFuseConfig()
        self._client: Any = None
        self._enabled = False
        self._trace_count = 0
        self._generation_count = 0

        if self._config.enabled:
            self._init_client()

    def _init_client(self) -> None:
        """初始化 LangFuse 客户端."""
        try:
            from langfuse import Langfuse

            self._client = Langfuse(
                public_key=self._config.public_key,
                secret_key=self._config.secret_key,
                host=self._config.host,
                flush_interval=self._config.flush_interval,
            )
            self._enabled = True
            logger.info(
                "langfuse_initialized",
                host=self._config.host,
            )
        except ImportError:
            logger.warning(
                "langfuse_skipped",
                reason="langfuse package not installed",
            )
        except Exception as e:
            logger.error("langfuse_init_failed", error=str(e))

    def create_trace(
        self,
        name: str,
        session_id: str = "",
        user_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        """
        创建追踪.

        Returns:
            LangFuse trace 对象，或 NoopTrace
        """
        self._trace_count += 1

        if not self._enabled or not self._client:
            return _NoopTrace()

        try:
            trace = self._client.trace(
                name=name,
                session_id=session_id or None,
                user_id=user_id or None,
                metadata=metadata or {},
            )
            return trace
        except Exception as e:
            logger.error("langfuse_trace_failed", error=str(e))
            return _NoopTrace()

    def log_generation(
        self,
        trace: Any,
        name: str,
        model: str,
        input_messages: list[dict[str, Any]],
        output: str,
        usage: dict[str, int] | None = None,
        metadata: dict[str, Any] | None = None,
        latency_ms: int = 0,
    ) -> Any:
        """
        记录一次 LLM 调用（generation）.

        关联到 trace 下，形成 trace → generation 的父子关系。
        """
        self._generation_count += 1

        if not self._enabled:
            return None

        try:
            generation = trace.generation(
                name=name,
                model=model,
                input=input_messages,
                output=output,
                usage=usage or {},
                metadata=metadata or {},
            )
            return generation
        except Exception as e:
            logger.error("langfuse_generation_failed", error=str(e))
            return None

    def log_score(
        self,
        trace: Any,
        name: str,
        value: float,
        comment: str = "",
    ) -> None:
        """
        记录评估分数.

        评估分数类型:
        - triage_accuracy: 分诊准确率 (0-1)
        - diagnostic_accuracy: 诊断准确率 (0-1)
        - user_feedback: 用户满意度 (1-5)
        - latency_score: 延迟评分 (0-1, 越高越好)
        """
        if not self._enabled:
            return

        try:
            trace.score(
                name=name,
                value=value,
                comment=comment,
            )
        except Exception as e:
            logger.error("langfuse_score_failed", error=str(e))

    @contextmanager
    def trace_context(
        self,
        name: str,
        session_id: str = "",
        **kwargs: Any,
    ) -> Generator[Any, None, None]:
        """
        上下文管理器版本.

        用法:
            with langfuse.trace_context("diagnose", session_id="xxx") as trace:
                ...  # 在此范围内的所有 generation 自动关联
        """
        trace = self.create_trace(name, session_id, **kwargs)
        try:
            yield trace
        finally:
            if self._enabled and self._client:
                try:
                    self._client.flush()
                except Exception:
                    pass

    def flush(self) -> None:
        """刷新缓冲区."""
        if self._client:
            try:
                self._client.flush()
            except Exception as e:
                logger.error("langfuse_flush_failed", error=str(e))

    def shutdown(self) -> None:
        """关闭客户端."""
        if self._client:
            try:
                self._client.shutdown()
            except Exception:
                pass

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self._enabled,
            "trace_count": self._trace_count,
            "generation_count": self._generation_count,
        }


class _NoopTrace:
    """LangFuse 未启用时的空实现."""

    def generation(self, **kwargs: Any) -> None:
        return None

    def score(self, **kwargs: Any) -> None:
        pass

    def span(self, **kwargs: Any) -> _NoopTrace:
        return self

    def update(self, **kwargs: Any) -> None:
        pass

    def end(self) -> None:
        pass
