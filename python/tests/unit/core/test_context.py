"""请求上下文模块测试."""

from __future__ import annotations

from aiops.core.context import (
    RequestContext,
    clear_request_context,
    get_request_context,
    get_trace_id,
    set_request_context,
)


class TestRequestContext:
    """RequestContext 测试."""

    def test_set_and_get_context(self) -> None:
        """设置后应能获取上下文."""
        ctx = RequestContext(
            trace_id="test-trace-123",
            session_id="sess-456",
            user_id="user-001",
        )
        set_request_context(ctx)

        result = get_request_context()
        assert result is not None
        assert result.trace_id == "test-trace-123"
        assert result.session_id == "sess-456"
        assert result.user_id == "user-001"

        # 清理
        clear_request_context()

    def test_get_trace_id_with_context(self) -> None:
        """有上下文时应返回 trace_id."""
        ctx = RequestContext(trace_id="abc-123")
        set_request_context(ctx)

        assert get_trace_id() == "abc-123"

        clear_request_context()

    def test_get_trace_id_without_context(self) -> None:
        """没有上下文时应返回 'no-trace'."""
        clear_request_context()
        assert get_trace_id() == "no-trace"

    def test_generate_trace_id(self) -> None:
        """生成的 trace_id 应为 16 字符的 hex 字符串."""
        trace_id = RequestContext.generate_trace_id()
        assert len(trace_id) == 16
        assert all(c in "0123456789abcdef" for c in trace_id)

    def test_context_immutable(self) -> None:
        """RequestContext 应为不可变."""
        ctx = RequestContext(trace_id="immutable-test")
        try:
            ctx.trace_id = "mutated"  # type: ignore[misc]
            assert False, "Should raise FrozenInstanceError"
        except AttributeError:
            pass  # 预期行为

    def test_clear_context(self) -> None:
        """清除后应返回 None."""
        set_request_context(RequestContext(trace_id="temp"))
        clear_request_context()
        assert get_request_context() is None
