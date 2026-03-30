"""
RBAC 权限控制 — 基于角色的访问控制

WHY RBAC 而不是 ABAC/ACL：
- 运维团队角色分层清晰（viewer/operator/engineer/admin/super）
- 角色 → 工具权限映射直观易审计
- 实现复杂度低，能快速落地

角色权限模型（参考 16-安全防护.md §2.1）：
  viewer:   只读查询（status/list/query）
  operator: 只读 + 低风险操作（clear_cache/refresh）
  engineer: 上述 + 中风险操作（update_config/trigger_gc）
  admin:    上述 + 高风险操作（restart/scale）
  super:    所有操作（包括 decommission/delete_data）
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from aiops.core.logging import get_logger

logger = get_logger(__name__)


class Role(IntEnum):
    """用户角色（权限从低到高）."""
    VIEWER = 0
    OPERATOR = 1
    ENGINEER = 2
    ADMIN = 3
    SUPER = 4


@dataclass
class Permission:
    """单个权限定义."""
    resource: str      # 资源标识（如 "tools:hdfs", "agent:diagnose"）
    action: str        # 操作（read/write/execute/admin）
    min_role: Role     # 最低要求角色


@dataclass
class User:
    """用户信息."""
    id: str
    name: str
    role: Role
    components: list[str] = field(default_factory=list)  # 授权组件
    created_at: float = field(default_factory=time.time)
    is_active: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AccessDecision:
    """访问控制决策."""
    allowed: bool
    reason: str
    user_role: str
    required_role: str
    resource: str
    action: str


class RBACManager:
    """
    RBAC 权限管理器.

    职责：
    1. 权限检查（用户角色 vs 操作要求）
    2. 工具级权限过滤
    3. 组件级权限过滤
    4. 审计日志

    存储策略（双模式）：
    - 内存 dict（默认）：启动时从默认配置加载，单实例足够
    - Redis hash（可选）：多实例部署时用户状态共享
    - 可通过 load_users_from_config() 从 YAML/dict 批量加载

    WHY 内存为主而不是全走 Redis：
    - 用户数量少（运维团队 <50 人）
    - 角色变更频率极低（每月可能 1-2 次）
    - 权限检查是热路径（每次工具调用），内存 <0.01ms vs Redis ~1ms
    - Redis 只做持久化备份，启动时加载一次即可
    """

    # 权限定义（resource:action → 最低角色）
    PERMISSION_TABLE: list[Permission] = [
        # Agent 操作
        Permission("agent:diagnose", "execute", Role.OPERATOR),
        Permission("agent:remediate", "execute", Role.ENGINEER),
        Permission("agent:patrol", "execute", Role.OPERATOR),
        Permission("agent:config", "write", Role.ADMIN),

        # 工具调用
        Permission("tools:read", "execute", Role.VIEWER),      # 只读工具
        Permission("tools:low_risk", "execute", Role.OPERATOR),  # 低风险操作
        Permission("tools:medium_risk", "execute", Role.ENGINEER),  # 中风险
        Permission("tools:high_risk", "execute", Role.ADMIN),   # 高风险
        Permission("tools:critical", "execute", Role.SUPER),    # 致命操作

        # 审批操作
        Permission("approval:view", "read", Role.VIEWER),
        Permission("approval:approve", "write", Role.ENGINEER),
        Permission("approval:approve_critical", "write", Role.ADMIN),

        # 管理操作
        Permission("admin:users", "read", Role.ADMIN),
        Permission("admin:users", "write", Role.SUPER),
        Permission("admin:config", "write", Role.SUPER),
    ]

    # 工具风险等级 → 权限资源
    RISK_TO_PERMISSION: dict[str, str] = {
        "none": "tools:read",
        "low": "tools:low_risk",
        "medium": "tools:medium_risk",
        "high": "tools:high_risk",
        "critical": "tools:critical",
    }

    def __init__(self, redis_url: str = "") -> None:
        self._users: dict[str, User] = {}
        self._permission_index: dict[tuple[str, str], Permission] = {
            (p.resource, p.action): p for p in self.PERMISSION_TABLE
        }
        self._audit_log: list[dict[str, Any]] = []
        self._redis: Any = None
        self._use_redis = False

        # 可选 Redis 持久化
        if redis_url:
            self._init_redis(redis_url)

        # 注册默认用户（开发模式）
        self._register_default_users()

        # 从 Redis 恢复已注册用户
        if self._use_redis:
            self._restore_users_from_redis()

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
            logger.info("rbac_redis_connected")
        except Exception as e:
            logger.warning("rbac_redis_connect_failed", error=str(e))
            self._redis = None

    def check_permission(
        self,
        user_id: str,
        resource: str,
        action: str,
    ) -> AccessDecision:
        """
        检查用户是否有权限执行操作.

        Returns:
            AccessDecision 包含允许/拒绝和原因
        """
        user = self._users.get(user_id)
        if not user:
            decision = AccessDecision(
                allowed=False,
                reason=f"User not found: {user_id}",
                user_role="unknown",
                required_role="unknown",
                resource=resource,
                action=action,
            )
            self._log_access(user_id, resource, action, decision)
            return decision

        if not user.is_active:
            decision = AccessDecision(
                allowed=False,
                reason="User is inactive",
                user_role=user.role.name.lower(),
                required_role="active",
                resource=resource,
                action=action,
            )
            self._log_access(user_id, resource, action, decision)
            return decision

        # 查找权限要求
        permission = self._permission_index.get((resource, action))
        if not permission:
            # 未定义的权限默认拒绝
            decision = AccessDecision(
                allowed=False,
                reason=f"Unknown permission: {resource}:{action}",
                user_role=user.role.name.lower(),
                required_role="undefined",
                resource=resource,
                action=action,
            )
            self._log_access(user_id, resource, action, decision)
            return decision

        # 角色检查
        allowed = user.role >= permission.min_role
        decision = AccessDecision(
            allowed=allowed,
            reason="Permission granted" if allowed else (
                f"Insufficient role: {user.role.name.lower()} < {permission.min_role.name.lower()}"
            ),
            user_role=user.role.name.lower(),
            required_role=permission.min_role.name.lower(),
            resource=resource,
            action=action,
        )

        self._log_access(user_id, resource, action, decision)
        return decision

    def check_tool_permission(
        self,
        user_id: str,
        tool_name: str,
        tool_risk: str,
    ) -> AccessDecision:
        """
        检查用户是否有权限调用指定工具.

        根据工具的风险等级映射到对应的权限资源。
        """
        resource = self.RISK_TO_PERMISSION.get(tool_risk, "tools:read")
        decision = self.check_permission(user_id, resource, "execute")

        if decision.allowed:
            # 额外检查组件级权限
            user = self._users.get(user_id)
            if user and user.components:
                # 从工具名推断组件
                component = self._infer_component(tool_name)
                if component and component not in user.components:
                    # 通用工具（metrics/log/alert）对所有人开放
                    always_allowed = {"metrics", "log", "alert", "event", "topology"}
                    if component not in always_allowed:
                        return AccessDecision(
                            allowed=False,
                            reason=f"No access to component: {component}",
                            user_role=user.role.name.lower(),
                            required_role=decision.required_role,
                            resource=resource,
                            action="execute",
                        )

        return decision

    def register_user(self, user: User) -> None:
        """注册用户（内存 + 可选 Redis 持久化）."""
        self._users[user.id] = user
        self._persist_user_to_redis(user)
        logger.info(
            "user_registered",
            user_id=user.id,
            role=user.role.name.lower(),
            components=user.components,
        )

    def get_user(self, user_id: str) -> User | None:
        return self._users.get(user_id)

    def list_users(self) -> list[User]:
        return list(self._users.values())

    def get_audit_log(self, limit: int = 100) -> list[dict[str, Any]]:
        """获取最近的访问日志."""
        return self._audit_log[-limit:]

    def _register_default_users(self) -> None:
        """注册开发模式默认用户."""
        defaults = [
            User(id="dev-admin", name="Dev Admin", role=Role.SUPER,
                 components=["hdfs", "yarn", "kafka", "es", "zk"]),
            User(id="dev-viewer", name="Dev Viewer", role=Role.VIEWER),
            User(id="agent", name="Agent System", role=Role.ENGINEER,
                 components=["hdfs", "yarn", "kafka", "es", "zk"]),
        ]
        for u in defaults:
            self._users[u.id] = u

    def _log_access(
        self,
        user_id: str,
        resource: str,
        action: str,
        decision: AccessDecision,
    ) -> None:
        """记录访问日志."""
        entry = {
            "timestamp": time.time(),
            "user_id": user_id,
            "resource": resource,
            "action": action,
            "allowed": decision.allowed,
            "reason": decision.reason,
        }
        self._audit_log.append(entry)

        # 保持日志大小
        if len(self._audit_log) > 10000:
            self._audit_log = self._audit_log[-5000:]

        if not decision.allowed:
            logger.warning(
                "access_denied",
                user_id=user_id,
                resource=resource,
                action=action,
                reason=decision.reason,
            )

    @staticmethod
    def _infer_component(tool_name: str) -> str | None:
        """从工具名推断组件."""
        prefix_map = {
            "hdfs_": "hdfs", "yarn_": "yarn", "kafka_": "kafka",
            "es_": "es", "zk_": "zk", "ops_": "ops",
        }
        for prefix, component in prefix_map.items():
            if tool_name.startswith(prefix):
                return component
        return None

    # ──────────────────────────────────────────────
    # 用户批量加载
    # ──────────────────────────────────────────────

    def load_users_from_config(self, users_config: list[dict[str, Any]]) -> int:
        """
        从配置批量加载用户.

        支持 YAML/dict 格式：
        ```yaml
        users:
          - id: zhangsan
            name: 张三
            role: engineer
            components: [hdfs, yarn]
          - id: lisi
            name: 李四
            role: admin
            components: [hdfs, yarn, kafka, es, zk]
        ```

        WHY 配置加载而不是 LDAP：
        - 运维团队 <50 人，配置文件足够管理
        - LDAP 集成是 Phase 2 的事（需要对接公司 SSO）
        - 配置加载更适合 showcase 演示

        Returns:
            成功加载的用户数
        """
        loaded = 0
        role_map = {r.name.lower(): r for r in Role}

        for cfg in users_config:
            user_id = cfg.get("id", "")
            if not user_id:
                continue

            role_str = cfg.get("role", "viewer").lower()
            role = role_map.get(role_str, Role.VIEWER)

            user = User(
                id=user_id,
                name=cfg.get("name", user_id),
                role=role,
                components=cfg.get("components", []),
                is_active=cfg.get("is_active", True),
                metadata=cfg.get("metadata", {}),
            )
            self.register_user(user)
            loaded += 1

        logger.info("users_loaded_from_config", count=loaded)
        return loaded

    # ──────────────────────────────────────────────
    # Redis 持久化（可选）
    # ──────────────────────────────────────────────

    REDIS_USER_PREFIX = "rbac:user:"

    def _persist_user_to_redis(self, user: User) -> None:
        """将用户信息持久化到 Redis."""
        if not self._use_redis or not self._redis:
            return
        try:
            import json
            key = f"{self.REDIS_USER_PREFIX}{user.id}"
            data = {
                "id": user.id,
                "name": user.name,
                "role": user.role.name.lower(),
                "components": json.dumps(user.components),
                "is_active": "1" if user.is_active else "0",
            }
            self._redis.hset(key, mapping=data)
        except Exception as e:
            logger.warning("rbac_redis_persist_failed", user_id=user.id, error=str(e))

    def _restore_users_from_redis(self) -> None:
        """启动时从 Redis 恢复已注册用户."""
        if not self._use_redis or not self._redis:
            return
        try:
            import json
            keys = self._redis.keys(f"{self.REDIS_USER_PREFIX}*")
            role_map = {r.name.lower(): r for r in Role}
            restored = 0

            for key in keys:
                data = self._redis.hgetall(key)
                if not data:
                    continue
                user_id = data.get("id", "")
                if user_id and user_id not in self._users:
                    role_str = data.get("role", "viewer")
                    user = User(
                        id=user_id,
                        name=data.get("name", user_id),
                        role=role_map.get(role_str, Role.VIEWER),
                        components=json.loads(data.get("components", "[]")),
                        is_active=data.get("is_active") == "1",
                    )
                    self._users[user_id] = user
                    restored += 1

            if restored:
                logger.info("rbac_users_restored_from_redis", count=restored)
        except Exception as e:
            logger.warning("rbac_redis_restore_failed", error=str(e))
