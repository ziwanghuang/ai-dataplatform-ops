"""
敏感数据处理 — 脱敏 + 分级 + 路由约束

WHY 需要独立的敏感数据处理模块：
- 运维场景的日志和配置中经常包含密码、IP、连接串
- LLM 调用前必须脱敏（外部 API 不应看到内部密码）
- 敏感数据必须走本地模型（data residency 合规）

脱敏策略（参考 16-安全防护.md §3.2）：
- 密码/密钥: 完全遮蔽（***）
- IP 地址: 保留网段，隐藏主机位（10.0.x.x）
- 数据库连接串: 隐藏密码部分
- 用户名/邮箱: 部分遮蔽
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from aiops.core.logging import get_logger

logger = get_logger(__name__)


class SensitivityLevel(IntEnum):
    """数据敏感等级."""
    PUBLIC = 0         # 公开（可发送到任何 LLM）
    INTERNAL = 1       # 内部（可发送到受信任 LLM）
    CONFIDENTIAL = 2   # 机密（仅本地模型处理）
    RESTRICTED = 3     # 受限（不能发送到任何 LLM）


@dataclass
class SanitizeResult:
    """脱敏结果."""
    original_length: int
    sanitized_text: str
    sensitivity_level: SensitivityLevel
    redacted_count: int
    redacted_types: list[str]


class SensitiveDataHandler:
    """
    敏感数据检测与脱敏.

    职责：
    1. 检测文本中的敏感信息
    2. 按策略脱敏
    3. 评估整体敏感度等级
    4. 提供路由建议（本地 vs 远程 LLM）

    WHY 正则检测而不是 NER 模型：
    - 运维场景的敏感数据模式固定（密码、IP、连接串）
    - 正则零延迟、100% 确定性
    - NER 模型有误判，安全场景不允许漏检
    """

    # 敏感数据检测规则
    # 格式: (pattern, replacement_fn, type_name, sensitivity_level)
    PATTERNS: list[tuple[re.Pattern[str], str, str, SensitivityLevel]] = [
        # 密码和密钥（最高优先级）
        (
            re.compile(
                r'(?i)(password|passwd|pwd|secret|token|api[_-]?key|'
                r'access[_-]?key|private[_-]?key)\s*[=:]\s*["\']?(\S+)["\']?',
            ),
            r"\1=***REDACTED***",
            "password/secret",
            SensitivityLevel.RESTRICTED,
        ),
        # AWS/云密钥
        (
            re.compile(r'(?:AKIA|ASIA)[A-Z0-9]{16}'),
            "***AWS_KEY_REDACTED***",
            "aws_key",
            SensitivityLevel.RESTRICTED,
        ),
        # Bearer Token
        (
            re.compile(r'Bearer\s+[A-Za-z0-9\-_\.]+'),
            "Bearer ***TOKEN_REDACTED***",
            "bearer_token",
            SensitivityLevel.RESTRICTED,
        ),
        # JDBC 连接串中的密码
        (
            re.compile(
                r'(jdbc:\w+://[^?]+\?.*?password=)[^&\s]+',
                re.IGNORECASE,
            ),
            r"\1***REDACTED***",
            "jdbc_password",
            SensitivityLevel.CONFIDENTIAL,
        ),
        # Redis/MongoDB 连接串
        (
            re.compile(r'(redis://|mongodb://|mongodb\+srv://)([^:]+):([^@]+)@'),
            r"\1\2:***@",
            "connection_password",
            SensitivityLevel.CONFIDENTIAL,
        ),
        # IP 地址（保留网段）
        (
            re.compile(r'\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\b'),
            r"\1.\2.x.x",
            "ip_address",
            SensitivityLevel.INTERNAL,
        ),
        # 邮箱
        (
            re.compile(r'\b([a-zA-Z0-9._%+-]+)@([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})\b'),
            r"***@\2",
            "email",
            SensitivityLevel.INTERNAL,
        ),
        # 手机号（中国）
        (
            re.compile(r'\b1[3-9]\d{9}\b'),
            "1****PHONE****",
            "phone",
            SensitivityLevel.CONFIDENTIAL,
        ),
    ]

    def sanitize(self, text: str) -> SanitizeResult:
        """
        脱敏文本.

        按优先级顺序应用脱敏规则，返回脱敏后的文本和元信息。
        """
        sanitized = text
        max_sensitivity = SensitivityLevel.PUBLIC
        redacted_count = 0
        redacted_types: list[str] = []

        for pattern, replacement, type_name, level in self.PATTERNS:
            matches = pattern.findall(sanitized)
            if matches:
                match_count = len(matches)
                sanitized = pattern.sub(replacement, sanitized)
                redacted_count += match_count
                if type_name not in redacted_types:
                    redacted_types.append(type_name)
                max_sensitivity = max(max_sensitivity, level)

        if redacted_count > 0:
            logger.info(
                "data_sanitized",
                redacted_count=redacted_count,
                types=redacted_types,
                sensitivity=max_sensitivity.name,
            )

        return SanitizeResult(
            original_length=len(text),
            sanitized_text=sanitized,
            sensitivity_level=max_sensitivity,
            redacted_count=redacted_count,
            redacted_types=redacted_types,
        )

    def detect_sensitivity(self, text: str) -> SensitivityLevel:
        """只检测不脱敏——用于路由决策."""
        max_level = SensitivityLevel.PUBLIC
        for pattern, _, _, level in self.PATTERNS:
            if pattern.search(text):
                max_level = max(max_level, level)
                if max_level == SensitivityLevel.RESTRICTED:
                    break  # 已经是最高级，不用继续
        return max_level

    def should_use_local_model(self, text: str) -> bool:
        """
        判断是否应该使用本地模型处理.

        CONFIDENTIAL 及以上必须走本地模型。
        """
        level = self.detect_sensitivity(text)
        return level >= SensitivityLevel.CONFIDENTIAL

    def sanitize_for_llm(
        self,
        messages: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], SensitivityLevel]:
        """
        对 LLM 消息列表进行脱敏.

        Returns:
            (脱敏后的消息列表, 整体敏感度)
        """
        sanitized_messages: list[dict[str, Any]] = []
        max_sensitivity = SensitivityLevel.PUBLIC

        for msg in messages:
            new_msg = dict(msg)
            content = msg.get("content", "")
            if isinstance(content, str) and content:
                result = self.sanitize(content)
                new_msg["content"] = result.sanitized_text
                max_sensitivity = max(max_sensitivity, result.sensitivity_level)
            sanitized_messages.append(new_msg)

        return sanitized_messages, max_sensitivity

    def sanitize_tool_output(self, output: str) -> str:
        """
        对工具输出进行脱敏.

        工具返回的数据（如日志、配置）经常包含敏感信息。
        在注入 LLM 上下文之前必须脱敏。
        """
        result = self.sanitize(output)
        return result.sanitized_text
