"""
Prometheus 中间件 — HTTP 请求指标自动采集

WHY 中间件而不是手动埋点：
- 所有 HTTP 请求自动采集（method/status/path/latency）
- 不需要在每个路由函数里写 metrics.observe()
- 标准 RED 指标（Rate/Error/Duration）开箱即用
"""

from __future__ import annotations

import time
from typing import Any, Callable

from aiops.core.logging import get_logger

logger = get_logger(__name__)


class PrometheusMiddleware:
    """
    FastAPI 中间件 — Prometheus 指标采集.

    采集指标：
    - http_requests_total{method, path, status}
    - http_request_duration_seconds{method, path}
    - http_requests_in_progress{method, path}

    WHY 自定义中间件而不是用 prometheus-fastapi-instrumentator：
    - 自定义中间件可以精确控制 path 标签粒度
    - 避免高基数（如 /sessions/{id}）导致的指标爆炸
    - 可以添加 AIOps 特定标签（如 component, cluster）
    """

    # 路径规范化映射（避免高基数）
    PATH_NORMALIZATION: dict[str, str] = {
        "/api/v1/agent/sessions/": "/api/v1/agent/sessions/{id}",
        "/api/v1/approval/": "/api/v1/approval/{id}",
    }

    def __init__(self, app: Any) -> None:
        self.app = app
        self._request_count = 0
        self._error_count = 0
        self._in_progress = 0

        # 直方图桶（秒）
        self._latency_buckets = [0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0]

        # 尝试注册 Prometheus 指标
        self._metrics_registered = False
        self._register_metrics()

    def _register_metrics(self) -> None:
        """注册 Prometheus 指标."""
        try:
            from prometheus_client import Counter, Histogram, Gauge

            self._http_requests_total = Counter(
                "http_requests_total",
                "Total HTTP requests",
                ["method", "path", "status"],
            )
            self._http_request_duration = Histogram(
                "http_request_duration_seconds",
                "HTTP request duration in seconds",
                ["method", "path"],
                buckets=self._latency_buckets,
            )
            self._http_in_progress = Gauge(
                "http_requests_in_progress",
                "HTTP requests currently in progress",
                ["method", "path"],
            )
            self._metrics_registered = True
            logger.info("prometheus_middleware_registered")

        except ImportError:
            logger.warning("prometheus_client_not_installed")

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        """ASGI 中间件入口."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        path = self._normalize_path(scope.get("path", "/"))
        start = time.monotonic()
        status_code = 500

        self._request_count += 1
        self._in_progress += 1

        if self._metrics_registered:
            self._http_in_progress.labels(method=method, path=path).inc()

        # 捕获响应状态码
        async def send_wrapper(message: Any) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            status_code = 500
            self._error_count += 1
            raise
        finally:
            duration = time.monotonic() - start
            self._in_progress -= 1

            if self._metrics_registered:
                self._http_requests_total.labels(
                    method=method, path=path, status=str(status_code)
                ).inc()
                self._http_request_duration.labels(
                    method=method, path=path
                ).observe(duration)
                self._http_in_progress.labels(method=method, path=path).dec()

            # 慢请求告警
            if duration > 5.0:
                logger.warning(
                    "slow_request",
                    method=method,
                    path=path,
                    duration_ms=int(duration * 1000),
                    status=status_code,
                )

    def _normalize_path(self, path: str) -> str:
        """路径规范化——避免指标高基数."""
        for prefix, normalized in self.PATH_NORMALIZATION.items():
            if path.startswith(prefix):
                return normalized
        return path
