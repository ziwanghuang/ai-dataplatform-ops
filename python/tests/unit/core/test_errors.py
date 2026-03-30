"""错误码体系测试."""

from __future__ import annotations

from aiops.core.errors import (
    AIOpsError,
    ErrorCode,
    LLMError,
    RAGError,
    ToolError,
)


class TestAIOpsError:
    """AIOpsError 基类测试."""

    def test_error_message_format(self) -> None:
        """错误消息应包含错误码."""
        err = AIOpsError(
            code=ErrorCode.LLM_API_ERROR,
            message="LLM 调用失败",
        )
        assert "AIOPS-L0601" in str(err)
        assert "LLM 调用失败" in str(err)

    def test_http_status_mapping(self) -> None:
        """错误码应正确映射 HTTP 状态码."""
        assert AIOpsError(code=ErrorCode.UNAUTHORIZED, message="").http_status == 401
        assert AIOpsError(code=ErrorCode.LLM_RATE_LIMITED, message="").http_status == 429
        assert AIOpsError(code=ErrorCode.TOOL_TIMEOUT, message="").http_status == 504
        assert AIOpsError(code=ErrorCode.SERVICE_UNAVAILABLE, message="").http_status == 503
        # 未映射的错误码默认 500
        assert AIOpsError(code=ErrorCode.INTERNAL_ERROR, message="").http_status == 500

    def test_error_response_serialization(self) -> None:
        """to_response() 应生成可序列化的响应."""
        err = AIOpsError(
            code=ErrorCode.TOOL_CALL_FAILED,
            message="HDFS 工具调用失败",
            detail="Connection refused",
        )
        resp = err.to_response(trace_id="trace-abc")
        assert resp.code == "AIOPS-T0601"
        assert resp.trace_id == "trace-abc"
        assert resp.message == "HDFS 工具调用失败"
        assert resp.detail == "Connection refused"
        # 可 JSON 序列化
        data = resp.model_dump()
        assert isinstance(data, dict)
        assert data["code"] == "AIOPS-T0601"

    def test_error_cause_chain(self) -> None:
        """cause 应保留原始异常链."""
        original = ConnectionError("HDFS unreachable")
        err = ToolError(
            code=ErrorCode.TOOL_CALL_FAILED,
            message="工具调用失败",
            cause=original,
        )
        assert err.cause is original
        assert isinstance(err, AIOpsError)

    def test_error_context(self) -> None:
        """context 应保留额外上下文."""
        err = AIOpsError(
            code=ErrorCode.LLM_TIMEOUT,
            message="超时",
            context={"model": "gpt-4o", "retry_count": 2},
        )
        assert err.context["model"] == "gpt-4o"
        assert err.context["retry_count"] == 2


class TestErrorSubclasses:
    """错误子类测试."""

    def test_llm_error_isinstance(self) -> None:
        err = LLMError(code=ErrorCode.LLM_TIMEOUT, message="超时")
        assert isinstance(err, AIOpsError)
        assert isinstance(err, LLMError)

    def test_tool_error_isinstance(self) -> None:
        err = ToolError(code=ErrorCode.TOOL_CALL_FAILED, message="失败")
        assert isinstance(err, AIOpsError)
        assert isinstance(err, ToolError)

    def test_rag_error_isinstance(self) -> None:
        err = RAGError(code=ErrorCode.RAG_NO_RESULTS, message="无结果")
        assert isinstance(err, AIOpsError)
        assert isinstance(err, RAGError)
