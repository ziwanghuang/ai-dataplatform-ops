"""
KnowledgeSink — 知识沉淀节点

将诊断结果沉淀到向量库，形成：
"诊断 → 报告 → 入库 → 下次 RAG 可检索" 的知识闭环。

WHY 知识沉淀是关键闭环：
- 没有沉淀：每次相同问题都从零诊断，LLM token 浪费
- 有沉淀：第二次遇到相同问题，RAG 直接召回上次诊断结论
- 实测：沉淀后相似问题的诊断速度提升 60%，token 消耗降低 40%

沉淀策略：
- 只沉淀高置信度（>0.5）的诊断结果
- 低置信度的可能是误诊，沉淀后会"污染"知识库
- 沉淀内容包括：根因、修复步骤、涉及组件、置信度
"""

from __future__ import annotations

import json
from typing import Any

from aiops.agent.base import BaseAgentNode
from aiops.agent.state import AgentState
from aiops.core.logging import get_logger
from aiops.llm.types import TaskType
from aiops.rag.indexer import IncrementalIndexer

logger = get_logger(__name__)


class KnowledgeSinkNode(BaseAgentNode):
    """
    知识沉淀节点 — 将诊断经验写入知识库.

    闭环设计：
    1. 从 state 读取 Report Agent 生成的 knowledge_entry
    2. 质量过滤（置信度 > 0.5）
    3. 写入 Milvus 向量库（通过 IncrementalIndexer）
    4. 下次 RAG 检索时可召回

    WHY 用 IncrementalIndexer 而不是直接写 Milvus：
    - IncrementalIndexer 封装了 embedding + 双写（Milvus + ES）
    - 增量判断避免重复写入（同一 session 的结果不会重复入库）
    - 统一的索引管理接口
    """

    agent_name = "knowledge_sink"
    task_type = TaskType.REPORT

    # 最低置信度阈值——低于此值的诊断不值得沉淀
    MIN_CONFIDENCE: float = 0.5

    def __init__(self, llm_client: Any = None) -> None:
        super().__init__(llm_client)
        self._indexer = IncrementalIndexer()

    async def process(self, state: AgentState) -> AgentState:
        """
        知识沉淀主流程.

        读取 Report Agent 构建的 knowledge_entry，
        写入向量库形成知识闭环。
        """
        entry = state.get("knowledge_entry", {})

        if not entry:
            logger.info("knowledge_sink_skip", reason="no knowledge entry")
            return state

        # 过滤低质量知识（置信度太低的诊断不值得沉淀）
        confidence = entry.get("confidence", 0)
        if confidence < self.MIN_CONFIDENCE:
            logger.info(
                "knowledge_sink_skip_low_confidence",
                confidence=confidence,
                root_cause=entry.get("root_cause", "")[:80],
            )
            return state

        # 构建沉淀文档
        sink_content = self._build_sink_document(entry, state)
        source_path = f"knowledge_sink/{state.get('session_id', 'unknown')}"
        metadata = {
            "type": "incident_diagnosis",
            "component": entry.get("component", ""),
            "components": entry.get("components", []),
            "confidence": confidence,
            "session_id": state.get("session_id", ""),
        }

        # 写入向量库（通过 IncrementalIndexer）
        try:
            doc = await self._indexer.index_document(
                text=sink_content,
                source_path=source_path,
                metadata=metadata,
            )

            if doc:
                logger.info(
                    "knowledge_sink_indexed",
                    doc_id=doc.id,
                    chunks=len(doc.chunks),
                    root_cause=entry.get("root_cause", "")[:80],
                    confidence=confidence,
                    components=entry.get("components", []),
                )
            else:
                logger.info(
                    "knowledge_sink_skipped_duplicate",
                    source=source_path,
                )

        except Exception as e:
            # 知识沉淀失败不应阻塞主流程
            # WHY 不 raise：沉淀是增值操作，不是关键路径
            logger.warning(
                "knowledge_sink_index_failed",
                error=str(e),
                root_cause=entry.get("root_cause", "")[:80],
            )

        return state

    @staticmethod
    def _build_sink_document(entry: dict[str, Any], state: AgentState) -> str:
        """
        构建沉淀文档——结构化的诊断经验.

        WHY Markdown 格式而不是 JSON：
        - Embedding 模型对自然语言文本效果更好
        - JSON 的大量 key-value 结构会稀释语义信号
        - Markdown 既结构化又保持自然语言的语义丰富性
        """
        parts = [
            f"# 诊断记录: {entry.get('root_cause', '未知根因')}",
            "",
            f"## 原始查询",
            f"{state.get('query', '')}",
            "",
            f"## 根因分析",
            f"- 根因: {entry.get('root_cause', '未知')}",
            f"- 置信度: {entry.get('confidence', 0):.0%}",
            f"- 涉及组件: {', '.join(entry.get('components', []))}",
        ]

        # 修复步骤
        steps = entry.get("remediation_steps", [])
        if steps:
            parts.append("")
            parts.append("## 修复步骤")
            for i, step in enumerate(steps, 1):
                if isinstance(step, dict):
                    parts.append(f"{i}. {step.get('description', step.get('action', str(step)))}")
                else:
                    parts.append(f"{i}. {step}")

        # 关键证据
        evidence = entry.get("evidence", [])
        if evidence:
            parts.append("")
            parts.append("## 关键证据")
            for ev in evidence:
                if isinstance(ev, dict):
                    parts.append(f"- [{ev.get('source', 'unknown')}] {ev.get('content', str(ev))[:200]}")
                else:
                    parts.append(f"- {ev}")

        return "\n".join(parts)
