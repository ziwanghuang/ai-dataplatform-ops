"""
Prometheus 指标定义 — 可观测性基础

所有自定义 Prometheus 指标集中在此模块定义。
Agent 节点、RAG 引擎、MCP 客户端等模块引用这里的指标。

指标命名约定：
- 前缀: aiops_
- 类型后缀: _total (Counter), _seconds (Histogram), _info (Gauge)
- 标签: 用于区分子类型（如 agent_name, retriever_type）
"""

from __future__ import annotations

# 尝试导入 prometheus_client，不可用时使用 Mock（开发阶段不强制依赖）
try:
    from prometheus_client import Counter, Histogram, Gauge, Summary
except ImportError:
    # Mock implementation for development without prometheus_client
    class _MockMetric:
        """Mock metric that accepts any call without error."""
        def __init__(self, *args, **kwargs):
            pass
        def labels(self, **kwargs):
            return self
        def inc(self, amount=1):
            pass
        def dec(self, amount=1):
            pass
        def set(self, value):
            pass
        def observe(self, value):
            pass

    Counter = Histogram = Gauge = Summary = _MockMetric  # type: ignore[misc, assignment]


# ──────────────────────────────────────────────────
# Agent 层指标
# ──────────────────────────────────────────────────

AGENT_INVOCATIONS = Counter(
    "aiops_agent_invocations_total",
    "Total agent node invocations",
    ["agent", "status"],  # agent=triage/diagnostic/..., status=success/error
)

AGENT_DURATION = Histogram(
    "aiops_agent_duration_seconds",
    "Agent node execution duration",
    ["agent"],
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0],
)

AGENT_TOKENS = Counter(
    "aiops_agent_tokens_total",
    "Total tokens consumed by agent",
    ["agent", "model"],
)

# ──────────────────────────────────────────────────
# RAG 检索指标
# ──────────────────────────────────────────────────

RAG_RETRIEVAL_DURATION_SECONDS = Histogram(
    "aiops_rag_retrieval_duration_seconds",
    "RAG retrieval duration",
    ["retriever_type"],  # dense/sparse/hybrid/hybrid_cached
    buckets=[0.01, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0],
)

RAG_RESULTS_COUNT = Histogram(
    "aiops_rag_results_count",
    "Number of retrieval results returned",
    ["retriever_type"],
    buckets=[0, 1, 3, 5, 10, 20],
)

RAG_RERANKER_DURATION_SECONDS = Histogram(
    "aiops_rag_reranker_duration_seconds",
    "Reranker processing duration",
    buckets=[0.05, 0.1, 0.2, 0.3, 0.5, 1.0],
)

RAG_CACHE_HIT_TOTAL = Counter(
    "aiops_rag_cache_hit_total",
    "RAG cache hits",
)

RAG_CACHE_MISS_TOTAL = Counter(
    "aiops_rag_cache_miss_total",
    "RAG cache misses",
)

# ──────────────────────────────────────────────────
# LLM 调用指标
# ──────────────────────────────────────────────────

LLM_CALL_DURATION = Histogram(
    "aiops_llm_call_duration_seconds",
    "LLM API call duration",
    ["model", "task_type"],
    buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 30.0],
)

LLM_CALL_TOTAL = Counter(
    "aiops_llm_call_total",
    "Total LLM API calls",
    ["model", "status"],  # status=success/error/timeout
)

LLM_TOKEN_USAGE = Counter(
    "aiops_llm_token_usage_total",
    "Total tokens consumed",
    ["model", "token_type"],  # token_type=prompt/completion
)

# ──────────────────────────────────────────────────
# MCP 工具调用指标
# ──────────────────────────────────────────────────

MCP_TOOL_CALL_DURATION = Histogram(
    "aiops_mcp_tool_call_duration_seconds",
    "MCP tool call duration",
    ["tool_name", "status"],
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0],
)

MCP_TOOL_CALL_TOTAL = Counter(
    "aiops_mcp_tool_call_total",
    "Total MCP tool calls",
    ["tool_name", "status"],
)

# ──────────────────────────────────────────────────
# Triage 专用指标（高频入口，需要更细粒度）
# ──────────────────────────────────────────────────

TRIAGE_ROUTE_TOTAL = Counter(
    "aiops_triage_route_total",
    "Triage routing decisions",
    ["method", "route"],  # method=rule_engine/llm/fallback, route=direct_tool/diagnosis/alert_correlation
)

TRIAGE_RULE_ENGINE_HIT = Counter(
    "aiops_triage_rule_engine_hit_total",
    "Rule engine fast path hits",
)
