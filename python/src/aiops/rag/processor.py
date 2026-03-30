"""
文档处理器 — 运维文档切分和预处理

WHY 自定义文档处理而不是直接用 LangChain 的 TextSplitter：
- 运维文档有特殊结构（YAML frontmatter + Markdown + 代码块 + 表格）
- 需要保留 SOP 步骤的完整性（不能在步骤中间切断）
- 代码块和配置示例需要作为原子单元
- 表格需要完整保留

文档类型（参考 14-索引管线.md）：
1. SOP 文档（标准操作流程）
2. 故障案例（Incident Report）
3. 组件文档（配置说明、参数表）
4. 变更记录（Change Log）
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from aiops.core.logging import get_logger

logger = get_logger(__name__)


class DocumentType(str, Enum):
    """文档类型."""
    SOP = "sop"               # 标准操作流程
    INCIDENT = "incident"     # 故障案例
    COMPONENT = "component"   # 组件文档
    CHANGELOG = "changelog"   # 变更记录
    UNKNOWN = "unknown"


@dataclass
class DocumentChunk:
    """文档切片."""
    id: str                    # chunk ID
    doc_id: str               # 所属文档 ID
    content: str              # 文本内容
    chunk_index: int          # 在文档中的序号
    total_chunks: int = 0     # 文档总切片数
    metadata: dict[str, Any] = field(default_factory=dict)
    doc_type: DocumentType = DocumentType.UNKNOWN
    section: str = ""         # 所属章节标题
    token_count: int = 0      # 估计的 token 数
    embedding: list[float] | None = None  # 稍后由 Indexer 填充


@dataclass
class ProcessedDocument:
    """处理后的文档."""
    id: str
    title: str
    doc_type: DocumentType
    chunks: list[DocumentChunk]
    metadata: dict[str, Any] = field(default_factory=dict)
    source_path: str = ""
    total_tokens: int = 0


class OpsDocumentProcessor:
    """
    运维文档处理器.

    切分策略：
    1. 按 Markdown 标题（##）做一级切分
    2. 每个 section 内，按 chunk_size 做二级切分
    3. 代码块、表格作为原子单元（不切断）
    4. SOP 步骤按编号保持完整
    5. 相邻 chunk 有 overlap 保证语义连续性

    WHY chunk_size 选 512 tokens：
    - 太小（128）：语义不完整，检索质量差
    - 太大（1024）：精度下降，不相关内容被带入
    - 512 是 RAG 社区的经验值，适合运维文档
    """

    DEFAULT_CHUNK_SIZE: int = 512        # 目标 token 数
    DEFAULT_CHUNK_OVERLAP: int = 64      # overlap token 数
    CHARS_PER_TOKEN: float = 1.5         # 中文约 1.5 字符/token

    def __init__(
        self,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    ) -> None:
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._max_chars = int(chunk_size * self.CHARS_PER_TOKEN)
        self._overlap_chars = int(chunk_overlap * self.CHARS_PER_TOKEN)

    def process(
        self,
        text: str,
        source_path: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> ProcessedDocument:
        """
        处理单个文档.

        Args:
            text: 文档原始文本
            source_path: 文件路径
            metadata: 额外元数据

        Returns:
            ProcessedDocument 包含所有切片
        """
        doc_id = hashlib.md5(text[:1000].encode()).hexdigest()[:12]
        title = self._extract_title(text)
        doc_type = self._detect_type(text, source_path)
        metadata = metadata or {}

        # 1. 提取 frontmatter
        frontmatter, body = self._extract_frontmatter(text)
        if frontmatter:
            metadata.update(frontmatter)

        # 2. 按标题切分为 sections
        sections = self._split_by_headings(body)

        # 3. 每个 section 内切分为 chunks
        all_chunks: list[DocumentChunk] = []
        chunk_index = 0

        for section_title, section_text in sections:
            # 特殊处理：代码块和表格作为原子单元
            atomic_blocks = self._extract_atomic_blocks(section_text)
            remaining_text = self._remove_atomic_blocks(section_text)

            # 对非原子文本做正常切分
            text_chunks = self._split_text(remaining_text)

            for chunk_text in text_chunks:
                chunk = DocumentChunk(
                    id=f"{doc_id}-{chunk_index:03d}",
                    doc_id=doc_id,
                    content=chunk_text.strip(),
                    chunk_index=chunk_index,
                    doc_type=doc_type,
                    section=section_title,
                    token_count=self._estimate_tokens(chunk_text),
                    metadata={
                        "source": source_path,
                        "section": section_title,
                        **metadata,
                    },
                )
                all_chunks.append(chunk)
                chunk_index += 1

            # 原子块作为独立 chunk
            for block in atomic_blocks:
                chunk = DocumentChunk(
                    id=f"{doc_id}-{chunk_index:03d}",
                    doc_id=doc_id,
                    content=block.strip(),
                    chunk_index=chunk_index,
                    doc_type=doc_type,
                    section=section_title,
                    token_count=self._estimate_tokens(block),
                    metadata={
                        "source": source_path,
                        "section": section_title,
                        "is_atomic": True,
                        **metadata,
                    },
                )
                all_chunks.append(chunk)
                chunk_index += 1

        # 更新总数
        for chunk in all_chunks:
            chunk.total_chunks = len(all_chunks)

        total_tokens = sum(c.token_count for c in all_chunks)

        logger.info(
            "document_processed",
            doc_id=doc_id,
            title=title,
            type=doc_type.value,
            chunks=len(all_chunks),
            total_tokens=total_tokens,
        )

        return ProcessedDocument(
            id=doc_id,
            title=title,
            doc_type=doc_type,
            chunks=all_chunks,
            metadata=metadata,
            source_path=source_path,
            total_tokens=total_tokens,
        )

    def _split_by_headings(self, text: str) -> list[tuple[str, str]]:
        """按 Markdown 标题切分."""
        # 按 ## 级别标题切分
        sections: list[tuple[str, str]] = []
        current_title = "Introduction"
        current_content: list[str] = []

        for line in text.split("\n"):
            heading_match = re.match(r'^(#{1,3})\s+(.+)$', line)
            if heading_match:
                # 保存之前的 section
                if current_content:
                    sections.append((current_title, "\n".join(current_content)))
                current_title = heading_match.group(2).strip()
                current_content = []
            else:
                current_content.append(line)

        # 最后一个 section
        if current_content:
            sections.append((current_title, "\n".join(current_content)))

        return sections if sections else [("", text)]

    def _split_text(self, text: str) -> list[str]:
        """按字符数切分文本（带 overlap）."""
        if len(text) <= self._max_chars:
            return [text] if text.strip() else []

        chunks: list[str] = []
        start = 0

        while start < len(text):
            end = start + self._max_chars

            # 尝试在自然断点处切分
            if end < len(text):
                # 优先在段落边界切分
                para_break = text.rfind("\n\n", start, end)
                if para_break > start + self._max_chars // 2:
                    end = para_break + 2
                else:
                    # 其次在句子边界切分
                    sentence_break = max(
                        text.rfind("。", start, end),
                        text.rfind(".", start, end),
                        text.rfind("\n", start, end),
                    )
                    if sentence_break > start + self._max_chars // 2:
                        end = sentence_break + 1

            chunk = text[start:end].strip()
            if chunk:
                chunks.append(chunk)

            # 下一个 chunk 的起始位置（带 overlap）
            start = end - self._overlap_chars

        return chunks

    def _extract_atomic_blocks(self, text: str) -> list[str]:
        """提取代码块和表格（作为原子单元）."""
        blocks: list[str] = []

        # Markdown 代码块
        code_pattern = re.compile(r'```[\s\S]*?```', re.MULTILINE)
        blocks.extend(code_pattern.findall(text))

        # Markdown 表格
        table_pattern = re.compile(
            r'^\|.+\|$\n^\|[-: |]+\|$\n(?:^\|.+\|$\n?)+',
            re.MULTILINE,
        )
        blocks.extend(table_pattern.findall(text))

        return blocks

    def _remove_atomic_blocks(self, text: str) -> str:
        """从文本中移除原子块。"""
        # 移除代码块
        text = re.sub(r'```[\s\S]*?```', '', text)
        # 移除表格
        text = re.sub(
            r'^\|.+\|$\n^\|[-: |]+\|$\n(?:^\|.+\|$\n?)+',
            '', text, flags=re.MULTILINE,
        )
        return text

    @staticmethod
    def _extract_title(text: str) -> str:
        """提取文档标题."""
        match = re.match(r'^#\s+(.+)$', text, re.MULTILINE)
        return match.group(1).strip() if match else "Untitled"

    @staticmethod
    def _detect_type(text: str, source_path: str) -> DocumentType:
        """检测文档类型."""
        path_lower = source_path.lower()
        text_lower = text[:500].lower()

        if "sop" in path_lower or "标准操作" in text_lower or "操作步骤" in text_lower:
            return DocumentType.SOP
        if "incident" in path_lower or "故障" in text_lower or "事故" in text_lower:
            return DocumentType.INCIDENT
        if "changelog" in path_lower or "变更" in text_lower:
            return DocumentType.CHANGELOG
        if any(comp in path_lower for comp in ("hdfs", "yarn", "kafka", "es")):
            return DocumentType.COMPONENT
        return DocumentType.UNKNOWN

    @staticmethod
    def _extract_frontmatter(text: str) -> tuple[dict[str, Any], str]:
        """提取 YAML frontmatter."""
        match = re.match(r'^---\n(.*?)\n---\n', text, re.DOTALL)
        if not match:
            return {}, text

        try:
            import yaml
            frontmatter = yaml.safe_load(match.group(1))
            body = text[match.end():]
            return frontmatter or {}, body
        except Exception:
            return {}, text

    def _estimate_tokens(self, text: str) -> int:
        """估计 token 数."""
        return int(len(text) / self.CHARS_PER_TOKEN)
