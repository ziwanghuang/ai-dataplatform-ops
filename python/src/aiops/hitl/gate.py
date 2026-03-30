"""
HITL Gate — LangGraph interrupt 集成 + 审批门控

WHY 将 HITL Gate 独立于 HITLGateNode：
- HITLGateNode（在 agent/nodes/hitl_gate.py）是 LangGraph 节点，负责图编排
- HITLGate（这里）是审批逻辑的核心实现，可以被多个节点复用
- 分离关注点：图编排 vs 审批逻辑

审批触发条件（参考 15-HITL人机协作系统.md §2.1）：
1. RiskClassifier 评估为 HIGH/CRITICAL
2. 修复操作涉及 restart/scale/decommission
3. 影响范围超过单节点（多组件联动修复）
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from aiops.core.logging import get_logger

logger = get_logger(__name__)


class ApprovalStatus(str, Enum):
    """审批状态."""
    PENDING = "pending"           # 等待审批
    APPROVED = "approved"         # 已批准
    REJECTED = "rejected"         # 已拒绝
    TIMEOUT = "timeout"           # 审批超时
    AUTO_APPROVED = "auto_approved"  # 开发环境自动批准


class ApprovalType(str, Enum):
    """审批类型."""
    SINGLE = "single"             # 单人审批
    DUAL = "dual"                 # 双人审批（critical 操作）
    AUTO = "auto"                 # 自动审批（开发模式）


@dataclass
class ApprovalRequest:
    """审批请求."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    session_id: str = ""
    operation: str = ""           # 操作描述
    risk_level: str = "medium"
    approval_type: ApprovalType = ApprovalType.SINGLE
    status: ApprovalStatus = ApprovalStatus.PENDING
    requester: str = "agent"
    approvers: list[str] = field(default_factory=list)
    approved_by: list[str] = field(default_factory=list)
    rejected_by: str | None = None
    reject_reason: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    resolved_at: float | None = None
    timeout_seconds: float = 300.0  # 默认 5 分钟超时

    @property
    def is_resolved(self) -> bool:
        return self.status not in (ApprovalStatus.PENDING,)

    @property
    def is_timed_out(self) -> bool:
        if self.status != ApprovalStatus.PENDING:
            return False
        return (time.time() - self.created_at) > self.timeout_seconds


class HITLGate:
    """
    HITL 审批门控核心逻辑.

    职责：
    1. 创建审批请求
    2. 管理审批状态（内存 + 可选 Redis 持久化）
    3. 检查超时
    4. 开发模式自动审批

    存储策略（双模式）：
    - 内存 dict（默认）：单实例开发/测试
    - Redis hash（生产）：多实例部署时审批状态共享
    - 切换方式：有 redis_url 配置则启用 Redis，否则用内存
    - Redis 不可用时自动降级到内存

    WHY Redis hash 而不是 Redis string：
    - 一个审批请求有多个字段（status/approvers/timestamps）
    - hash 比多个 string key 更紧凑，TTL 管理也更简单
    - 键格式：hitl:approval:{request_id}

    WHY DEV 模式自动审批：
    - 开发/演示时不可能每次都手动审批
    - 但生产环境绝不能自动审批
    - 通过 settings.environment 控制
    """

    REDIS_KEY_PREFIX = "hitl:approval:"
    REDIS_TTL_SECONDS = 7200  # 2 小时后自动清理审批记录

    def __init__(
        self,
        auto_approve_dev: bool = True,
        redis_url: str = "",
    ) -> None:
        self._requests: dict[str, ApprovalRequest] = {}
        self._auto_approve_dev = auto_approve_dev
        self._redis: Any = None
        self._use_redis = False

        # 尝试连接 Redis
        if redis_url:
            self._init_redis(redis_url)

    def _init_redis(self, redis_url: str) -> None:
        """尝试连接 Redis."""
        try:
            import redis
            self._redis = redis.Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=3,
            )
            self._redis.ping()
            self._use_redis = True
            logger.info("hitl_redis_connected", url=redis_url[:30])
        except Exception as e:
            logger.warning("hitl_redis_connect_failed_using_memory", error=str(e))
            self._redis = None
            self._use_redis = False

    def create_request(
        self,
        session_id: str,
        operation: str,
        risk_level: str,
        context: dict[str, Any] | None = None,
        timeout_seconds: float = 300.0,
    ) -> ApprovalRequest:
        """
        创建审批请求.

        Args:
            session_id: 关联的诊断会话
            operation: 操作描述（如 "restart hdfs-namenode-01"）
            risk_level: 风险等级 (none/low/medium/high/critical)
            context: 附加上下文（诊断结果、证据链等）
            timeout_seconds: 审批超时时间

        Returns:
            ApprovalRequest 对象
        """
        # 确定审批类型
        approval_type = self._determine_approval_type(risk_level)

        # 确定超时时间（高风险给更多时间）
        if risk_level in ("high", "critical"):
            timeout_seconds = max(timeout_seconds, 600.0)  # 至少 10 分钟

        request = ApprovalRequest(
            session_id=session_id,
            operation=operation,
            risk_level=risk_level,
            approval_type=approval_type,
            context=context or {},
            timeout_seconds=timeout_seconds,
        )

        # DEV 模式自动审批
        if self._auto_approve_dev:
            request.status = ApprovalStatus.AUTO_APPROVED
            request.resolved_at = time.time()
            request.approved_by = ["dev-auto"]
            logger.info(
                "hitl_auto_approved",
                request_id=request.id,
                operation=operation,
                risk_level=risk_level,
            )
        else:
            logger.info(
                "hitl_request_created",
                request_id=request.id,
                operation=operation,
                risk_level=risk_level,
                approval_type=approval_type.value,
                timeout=timeout_seconds,
            )

        self._requests[request.id] = request
        self._persist_to_redis(request)
        return request

    def approve(
        self,
        request_id: str,
        approver: str,
        comment: str | None = None,
    ) -> ApprovalRequest:
        """
        批准审批请求.

        对于 DUAL 审批类型，需要两个不同的人批准。
        """
        request = self._get_request(request_id)

        if request.is_resolved:
            raise ValueError(f"Request {request_id} already resolved: {request.status.value}")

        if request.is_timed_out:
            request.status = ApprovalStatus.TIMEOUT
            request.resolved_at = time.time()
            raise ValueError(f"Request {request_id} has timed out")

        request.approved_by.append(approver)

        # 检查是否满足审批条件
        if request.approval_type == ApprovalType.DUAL:
            # 双人审批：需要 2 个不同的审批人
            unique_approvers = set(request.approved_by)
            if len(unique_approvers) >= 2:
                request.status = ApprovalStatus.APPROVED
                request.resolved_at = time.time()
                logger.info(
                    "hitl_dual_approved",
                    request_id=request_id,
                    approvers=list(unique_approvers),
                )
            else:
                logger.info(
                    "hitl_awaiting_second_approval",
                    request_id=request_id,
                    first_approver=approver,
                )
        else:
            # 单人审批
            request.status = ApprovalStatus.APPROVED
            request.resolved_at = time.time()
            logger.info(
                "hitl_approved",
                request_id=request_id,
                approver=approver,
            )

        self._persist_to_redis(request)
        return request

    def reject(
        self,
        request_id: str,
        rejector: str,
        reason: str,
    ) -> ApprovalRequest:
        """拒绝审批请求."""
        request = self._get_request(request_id)

        if request.is_resolved:
            raise ValueError(f"Request {request_id} already resolved")

        request.status = ApprovalStatus.REJECTED
        request.rejected_by = rejector
        request.reject_reason = reason
        request.resolved_at = time.time()

        logger.info(
            "hitl_rejected",
            request_id=request_id,
            rejector=rejector,
            reason=reason,
        )
        self._persist_to_redis(request)
        return request

    def check_status(self, request_id: str) -> ApprovalRequest:
        """检查审批状态（含超时检测 + Redis 同步）."""
        # 优先从 Redis 恢复（支持多实例场景）
        self._restore_from_redis(request_id)

        request = self._get_request(request_id)

        # 超时检测
        if request.status == ApprovalStatus.PENDING and request.is_timed_out:
            request.status = ApprovalStatus.TIMEOUT
            request.resolved_at = time.time()
            self._persist_to_redis(request)
            logger.warning(
                "hitl_timeout",
                request_id=request_id,
                operation=request.operation,
                waited_seconds=time.time() - request.created_at,
            )

        return request

    def list_pending(self, session_id: str | None = None) -> list[ApprovalRequest]:
        """列出待审批请求."""
        pending = [
            r for r in self._requests.values()
            if r.status == ApprovalStatus.PENDING
        ]
        if session_id:
            pending = [r for r in pending if r.session_id == session_id]
        return pending

    def get_request(self, request_id: str) -> ApprovalRequest | None:
        """获取审批请求（外部接口，返回 None 而不是抛异常）."""
        return self._requests.get(request_id)

    def _get_request(self, request_id: str) -> ApprovalRequest:
        """内部获取审批请求（抛异常版本）."""
        request = self._requests.get(request_id)
        if not request:
            raise ValueError(f"Approval request not found: {request_id}")
        return request

    @staticmethod
    def _determine_approval_type(risk_level: str) -> ApprovalType:
        """
        根据风险等级确定审批类型.

        - critical: 双人审批（如 decommission_node）
        - high: 单人审批（如 restart_service）
        - medium 及以下: 单人审批
        """
        if risk_level == "critical":
            return ApprovalType.DUAL
        return ApprovalType.SINGLE

    @property
    def stats(self) -> dict[str, Any]:
        """审批统计."""
        statuses = {}
        for r in self._requests.values():
            status = r.status.value
            statuses[status] = statuses.get(status, 0) + 1

        return {
            "total_requests": len(self._requests),
            "by_status": statuses,
            "pending_count": len(self.list_pending()),
            "storage": "redis" if self._use_redis else "memory",
        }

    # ──────────────────────────────────────────────
    # Redis 持久化（可选）
    # ──────────────────────────────────────────────

    def _persist_to_redis(self, request: ApprovalRequest) -> None:
        """将审批请求持久化到 Redis."""
        if not self._use_redis or not self._redis:
            return

        try:
            import json
            key = f"{self.REDIS_KEY_PREFIX}{request.id}"
            data = {
                "id": request.id,
                "session_id": request.session_id,
                "operation": request.operation,
                "risk_level": request.risk_level,
                "approval_type": request.approval_type.value,
                "status": request.status.value,
                "requester": request.requester,
                "approved_by": json.dumps(request.approved_by),
                "rejected_by": request.rejected_by or "",
                "reject_reason": request.reject_reason or "",
                "created_at": str(request.created_at),
                "resolved_at": str(request.resolved_at) if request.resolved_at else "",
                "timeout_seconds": str(request.timeout_seconds),
            }
            self._redis.hset(key, mapping=data)
            self._redis.expire(key, self.REDIS_TTL_SECONDS)
        except Exception as e:
            logger.warning("hitl_redis_persist_failed", request_id=request.id, error=str(e))

    def _restore_from_redis(self, request_id: str) -> None:
        """
        从 Redis 恢复审批请求到内存.

        WHY 只在 check_status 时恢复：
        - 避免每次操作都读 Redis（减少 RTT）
        - check_status 是轮询操作，频率可控
        - 其他实例的审批结果会通过这里同步到本地
        """
        if not self._use_redis or not self._redis:
            return
        if request_id in self._requests:
            # 内存中已有，检查 Redis 是否有更新
            pass  # 下面的逻辑会覆盖

        try:
            import json
            key = f"{self.REDIS_KEY_PREFIX}{request_id}"
            data = self._redis.hgetall(key)
            if not data:
                return

            # 重建 ApprovalRequest
            request = ApprovalRequest(
                id=data.get("id", request_id),
                session_id=data.get("session_id", ""),
                operation=data.get("operation", ""),
                risk_level=data.get("risk_level", "medium"),
                approval_type=ApprovalType(data.get("approval_type", "single")),
                status=ApprovalStatus(data.get("status", "pending")),
                requester=data.get("requester", "agent"),
                approved_by=json.loads(data.get("approved_by", "[]")),
                rejected_by=data.get("rejected_by") or None,
                reject_reason=data.get("reject_reason") or None,
                created_at=float(data.get("created_at", 0)),
                resolved_at=float(data["resolved_at"]) if data.get("resolved_at") else None,
                timeout_seconds=float(data.get("timeout_seconds", 300)),
            )
            self._requests[request_id] = request

        except Exception as e:
            logger.warning("hitl_redis_restore_failed", request_id=request_id, error=str(e))
