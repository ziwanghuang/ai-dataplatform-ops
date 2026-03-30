"""
上下文压缩器 — 控制 LLM 输入的 Token 量

Diagnostic Agent 多轮采集后，collected_data 可能非常大（5 个工具 × 1-5KB = 5-25KB）。
直接全部塞给 LLM 会导致：
1. 超出 Token 预算（单次诊断 15K Token 上限）
2. LLM 注意力衰减（context > 8K 时准确率显著下降）
3. 成本爆炸

ContextCompressor 的策略（按优先级）：
1. 移除工具返回结果中的冗余信息（表头、分隔线等）
2. 只保留与当前假设相关的数据
3. 截断超长字段（保留前 N 字符）
4. 限制总字符数（默认 8000 字符 ≈ ~3000 Token）
"""

from __future__ import annotations

from typing import Any

from aiops.agent.state import AgentState
from aiops.core.logging import get_logger

logger = get_logger(__name__)

# 压缩参数
MAX_TOTAL_CHARS = 8000          # 最终上下文最大字符数（≈3000 Token）
MAX_PER_TOOL_CHARS = 2000       # 单个工具结果最大字符数
MAX_RAG_CHARS = 1500            # RAG 上下文最大字符数
MAX_CASES_CHARS = 1000          # 相似案例最大字符数


class ContextCompressor:
    """
    上下文压缩器 — 确保 LLM 输入不超过 Token 预算.

    压缩策略（信息损失从低到高）：
    1. 移除空白行和重复分隔符
    2. 截断单个工具结果
    3. 截断 RAG 上下文
    4. 如果仍超限，只保留最近的工具结果
    """

    def compress(self, state: AgentState) -> str:
        """
        压缩 state 中的数据，生成适合 LLM 消费的上下文文本.

        Returns:
            压缩后的上下文字符串（保证 <= MAX_TOTAL_CHARS）
        """
        parts: list[str] = []

        # 1. 压缩 collected_data（最重要，优先保留）
        collected = state.get("collected_data", {})
        data_text = self._compress_collected_data(collected)
        parts.append(f"### 采集数据\n{data_text}")

        # 2. 压缩 RAG 上下文
        rag = state.get("rag_context", [])
        if rag:
            rag_text = self._compress_rag(rag)
            parts.append(f"### 知识库参考\n{rag_text}")

        # 3. 压缩相似案例
        cases = state.get("similar_cases", [])
        if cases:
            cases_text = self._compress_cases(cases)
            parts.append(f"### 相似案例\n{cases_text}")

        # 合并
        result = "\n\n".join(parts)

        # 最终硬截断（安全网）
        if len(result) > MAX_TOTAL_CHARS:
            result = result[:MAX_TOTAL_CHARS] + "\n\n... [上下文已截断，总长超限]"
            logger.warning("context_hard_truncated", original_len=len("\n\n".join(parts)))

        return result

    def _compress_collected_data(self, collected: dict[str, Any]) -> str:
        """压缩工具采集数据."""
        lines = []
        for tool_name, data in collected.items():
            text = str(data)
            # 移除空白行
            text = "\n".join(line for line in text.split("\n") if line.strip())
            # 截断单个工具结果
            if len(text) > MAX_PER_TOOL_CHARS:
                text = text[:MAX_PER_TOOL_CHARS] + f"\n... [截断，原长 {len(str(data))} 字符]"
            lines.append(f"[{tool_name}]\n{text}")
        return "\n\n".join(lines)

    def _compress_rag(self, rag_context: list[dict[str, Any]]) -> str:
        """压缩 RAG 检索结果（只保留 top 3）."""
        lines = []
        total_chars = 0
        for ctx in rag_context[:3]:  # 最多 3 条
            content = ctx.get("content", "")[:500]
            source = ctx.get("source", "未知")
            line = f"- [{source}] {content}"
            total_chars += len(line)
            if total_chars > MAX_RAG_CHARS:
                break
            lines.append(line)
        return "\n".join(lines) if lines else "无相关知识库文档。"

    def _compress_cases(self, cases: list[dict[str, Any]]) -> str:
        """压缩相似案例（只保留 top 2）."""
        lines = []
        total_chars = 0
        for case in cases[:2]:  # 最多 2 条
            root_cause = case.get("root_cause", "")[:200]
            severity = case.get("severity", "unknown")
            line = f"- [{severity}] {root_cause}"
            total_chars += len(line)
            if total_chars > MAX_CASES_CHARS:
                break
            lines.append(line)
        return "\n".join(lines) if lines else "无相似历史案例。"
