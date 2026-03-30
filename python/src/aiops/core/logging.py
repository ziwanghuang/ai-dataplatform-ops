"""
结构化日志配置

使用 structlog 实现：
- JSON 格式输出（生产环境）
- 彩色控制台输出（开发环境）
- 自动注入 trace_id, session_id
- 敏感字段自动脱敏
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog
from structlog.types import EventDict

from aiops.core.config import Environment, settings

# 需要脱敏的字段模式
_SENSITIVE_PATTERNS = re.compile(
    r"(api[_-]?key|password|secret|token|authorization|credential)",
    re.IGNORECASE,
)


def _mask_sensitive_values(
    logger: Any, method: str, event_dict: EventDict
) -> EventDict:
    """自动脱敏敏感字段."""
    for key, value in list(event_dict.items()):
        if isinstance(key, str) and _SENSITIVE_PATTERNS.search(key):
            if isinstance(value, str) and len(value) > 8:
                event_dict[key] = value[:4] + "****" + value[-4:]
            else:
                event_dict[key] = "****"
    return event_dict


def _add_service_info(
    logger: Any, method: str, event_dict: EventDict
) -> EventDict:
    """注入服务元信息."""
    event_dict.setdefault("service", settings.app_name)
    event_dict.setdefault("version", settings.version)
    event_dict.setdefault("env", settings.env.value)
    return event_dict


def setup_logging() -> None:
    """初始化日志系统，应在应用启动时调用一次."""
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,  # 自动注入 trace_id 等
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        _add_service_info,
        _mask_sensitive_values,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]

    if settings.env == Environment.DEV:
        # 开发环境：彩色控制台
        renderer: structlog.types.Processor = structlog.dev.ConsoleRenderer(colors=True)
    else:
        # 生产环境：JSON
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # 同时配置标准库 logging（第三方库用的）
    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[*shared_processors, renderer],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.DEBUG if settings.debug else logging.INFO)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """获取 logger 实例."""
    return structlog.get_logger(name)


def bind_request_context(
    trace_id: str,
    session_id: str | None = None,
    user_id: str | None = None,
) -> None:
    """绑定请求级别的上下文变量到 structlog contextvars."""
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        trace_id=trace_id,
        session_id=session_id or "",
        user_id=user_id or "",
    )
