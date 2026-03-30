"""
通知服务 — 审批通知推送

支持通道（参考 15-HITL人机协作系统.md §5）：
1. 企业微信 Bot（生产主通道）
2. WebSocket（前端实时推送）
3. 控制台日志（开发模式）

WHY 多通道而不是只用企微：
- 开发/测试时不想每次都发企微消息
- WebSocket 给前端 Dashboard 实时状态更新
- 控制台日志是兜底（任何通道都可能挂）
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from aiops.core.logging import get_logger
from aiops.hitl.gate import ApprovalRequest

logger = get_logger(__name__)


class NotificationChannel(str, Enum):
    """通知通道."""
    WECOM = "wecom"           # 企业微信 Bot
    WEBSOCKET = "websocket"   # WebSocket 实时推送
    CONSOLE = "console"       # 控制台日志
    WEBHOOK = "webhook"       # 通用 Webhook


class NotificationPriority(str, Enum):
    """通知优先级."""
    LOW = "low"          # 信息性通知
    NORMAL = "normal"    # 常规通知
    HIGH = "high"        # 需要关注
    URGENT = "urgent"    # 需要立即处理


@dataclass
class Notification:
    """通知消息."""
    id: str = ""
    channel: NotificationChannel = NotificationChannel.CONSOLE
    priority: NotificationPriority = NotificationPriority.NORMAL
    title: str = ""
    body: str = ""
    recipients: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    sent: bool = False
    error: str | None = None


class NotificationSender(ABC):
    """通知发送器基类."""

    @abstractmethod
    async def send(self, notification: Notification) -> bool:
        """发送通知，返回是否成功."""
        ...


class ConsoleNotificationSender(NotificationSender):
    """
    控制台通知发送器（开发模式默认）.

    WHY 保留 Console 通道：
    - 开发时不需要配置企微/WebSocket
    - structlog JSON 输出，可以被日志系统采集
    - 测试时验证通知逻辑
    """

    async def send(self, notification: Notification) -> bool:
        priority_emoji = {
            NotificationPriority.LOW: "ℹ️",
            NotificationPriority.NORMAL: "📋",
            NotificationPriority.HIGH: "⚠️",
            NotificationPriority.URGENT: "🚨",
        }
        emoji = priority_emoji.get(notification.priority, "📋")

        logger.info(
            "notification_console",
            emoji=emoji,
            title=notification.title,
            body=notification.body,
            priority=notification.priority.value,
            recipients=notification.recipients,
        )
        return True


class WeComBotSender(NotificationSender):
    """
    企业微信 Bot 通知发送器.

    通过 Webhook URL 推送 Markdown 消息。
    生产部署时配置 WECOM_BOT_WEBHOOK_URL 环境变量。
    """

    def __init__(self, webhook_url: str = "") -> None:
        self._webhook_url = webhook_url

    async def send(self, notification: Notification) -> bool:
        if not self._webhook_url:
            logger.warning("wecom_webhook_not_configured")
            return False

        try:
            import httpx

            # 企微 Bot Markdown 消息格式
            markdown_content = (
                f"## {notification.title}\n\n"
                f"{notification.body}\n\n"
                f"> 优先级: {notification.priority.value}\n"
                f"> 接收人: {', '.join(notification.recipients)}"
            )

            payload = {
                "msgtype": "markdown",
                "markdown": {"content": markdown_content},
            }

            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    self._webhook_url,
                    json=payload,
                    timeout=10.0,
                )
                resp.raise_for_status()
                data = resp.json()
                success = data.get("errcode", -1) == 0

                if not success:
                    logger.warning(
                        "wecom_send_failed",
                        errcode=data.get("errcode"),
                        errmsg=data.get("errmsg"),
                    )
                return success

        except Exception as e:
            logger.error("wecom_send_error", error=str(e))
            return False


class WebSocketNotificationSender(NotificationSender):
    """
    WebSocket 通知发送器.

    负责将通知推送到已连接的 WebSocket 客户端。
    实际的 WebSocket 连接管理在 web 层（FastAPI WebSocket endpoint）。
    这里通过注入的回调函数发送消息。
    """

    def __init__(
        self,
        broadcast_fn: Any | None = None,
    ) -> None:
        self._broadcast_fn = broadcast_fn

    async def send(self, notification: Notification) -> bool:
        if not self._broadcast_fn:
            logger.debug("websocket_broadcast_not_configured")
            return False

        try:
            message = {
                "type": "approval_notification",
                "data": {
                    "title": notification.title,
                    "body": notification.body,
                    "priority": notification.priority.value,
                    "metadata": notification.metadata,
                },
            }
            await self._broadcast_fn(json.dumps(message))
            return True
        except Exception as e:
            logger.error("websocket_send_error", error=str(e))
            return False


class NotificationService:
    """
    通知服务 — 统一管理所有通知通道.

    策略：
    - 开发模式：只用 Console
    - 生产模式：Console + WeCom + WebSocket
    - 通知发送失败不阻塞主流程（异步尽力发送）
    """

    def __init__(self) -> None:
        self._senders: dict[NotificationChannel, NotificationSender] = {
            NotificationChannel.CONSOLE: ConsoleNotificationSender(),
        }
        self._notification_count = 0

    def register_sender(
        self,
        channel: NotificationChannel,
        sender: NotificationSender,
    ) -> None:
        """注册通知发送器."""
        self._senders[channel] = sender
        logger.info("notification_sender_registered", channel=channel.value)

    async def notify_approval_request(
        self,
        request: ApprovalRequest,
        channels: list[NotificationChannel] | None = None,
    ) -> None:
        """
        发送审批请求通知.

        根据风险等级自动设置优先级：
        - critical → URGENT
        - high → HIGH
        - medium → NORMAL
        - low/none → LOW
        """
        priority_map = {
            "critical": NotificationPriority.URGENT,
            "high": NotificationPriority.HIGH,
            "medium": NotificationPriority.NORMAL,
        }
        priority = priority_map.get(
            request.risk_level, NotificationPriority.LOW
        )

        notification = Notification(
            id=f"approval-{request.id}",
            priority=priority,
            title=f"[{request.risk_level.upper()}] 运维操作审批请求",
            body=(
                f"**操作**: {request.operation}\n"
                f"**风险等级**: {request.risk_level}\n"
                f"**审批类型**: {request.approval_type.value}\n"
                f"**超时时间**: {request.timeout_seconds}s\n"
                f"**会话 ID**: {request.session_id}"
            ),
            recipients=request.approvers or ["ops-team"],
            metadata={
                "request_id": request.id,
                "session_id": request.session_id,
                "risk_level": request.risk_level,
            },
        )

        channels = channels or list(self._senders.keys())
        await self._send_to_channels(notification, channels)

    async def notify_approval_result(
        self,
        request: ApprovalRequest,
        channels: list[NotificationChannel] | None = None,
    ) -> None:
        """发送审批结果通知."""
        status_emoji = {
            "approved": "✅",
            "auto_approved": "🤖✅",
            "rejected": "❌",
            "timeout": "⏰",
        }
        emoji = status_emoji.get(request.status.value, "📋")

        notification = Notification(
            id=f"result-{request.id}",
            priority=NotificationPriority.NORMAL,
            title=f"{emoji} 审批结果: {request.status.value}",
            body=(
                f"**操作**: {request.operation}\n"
                f"**状态**: {request.status.value}\n"
                f"**审批人**: {', '.join(request.approved_by) if request.approved_by else '无'}\n"
                f"**拒绝原因**: {request.reject_reason or '无'}"
            ),
            recipients=["ops-team"],
            metadata={"request_id": request.id},
        )

        channels = channels or list(self._senders.keys())
        await self._send_to_channels(notification, channels)

    async def _send_to_channels(
        self,
        notification: Notification,
        channels: list[NotificationChannel],
    ) -> None:
        """向多个通道发送通知."""
        self._notification_count += 1

        for channel in channels:
            sender = self._senders.get(channel)
            if not sender:
                continue

            notification.channel = channel
            try:
                success = await sender.send(notification)
                notification.sent = notification.sent or success
            except Exception as e:
                notification.error = str(e)
                logger.error(
                    "notification_send_failed",
                    channel=channel.value,
                    error=str(e),
                )

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "total_notifications": self._notification_count,
            "registered_channels": list(self._senders.keys()),
        }
