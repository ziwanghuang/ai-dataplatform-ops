"""ToolRegistry — 工具元数据注册中心."""

from __future__ import annotations

from enum import Enum
from typing import Any

from aiops.core.logging import get_logger

logger = get_logger(__name__)


class RiskLevel(str, Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def order(self) -> int:
        return {"none": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}[self.value]


# 内部 MCP 工具元数据 (42 个)
INTERNAL_TOOLS: list[dict[str, Any]] = [
    # HDFS (8)
    {"name": "hdfs_cluster_overview", "component": "hdfs", "risk": "none", "source": "internal", "description": "获取 HDFS 集群概览"},
    {"name": "hdfs_namenode_status", "component": "hdfs", "risk": "none", "source": "internal", "description": "获取 NameNode 状态"},
    {"name": "hdfs_datanode_list", "component": "hdfs", "risk": "none", "source": "internal", "description": "列出所有 DataNode 状态"},
    {"name": "hdfs_block_report", "component": "hdfs", "risk": "none", "source": "internal", "description": "HDFS 块健康报告"},
    {"name": "hdfs_fsck_status", "component": "hdfs", "risk": "none", "source": "internal", "description": "HDFS 文件系统健康检查"},
    {"name": "hdfs_snapshot_list", "component": "hdfs", "risk": "none", "source": "internal", "description": "列出 HDFS 快照"},
    {"name": "hdfs_safe_mode_status", "component": "hdfs", "risk": "none", "source": "internal", "description": "检查安全模式状态"},
    {"name": "hdfs_decommission_status", "component": "hdfs", "risk": "none", "source": "internal", "description": "DataNode 退役进度"},
    # YARN (7)
    {"name": "yarn_cluster_metrics", "component": "yarn", "risk": "none", "source": "internal", "description": "YARN 集群资源指标"},
    {"name": "yarn_queue_status", "component": "yarn", "risk": "none", "source": "internal", "description": "YARN 队列状态"},
    {"name": "yarn_applications", "component": "yarn", "risk": "none", "source": "internal", "description": "YARN 应用列表"},
    {"name": "yarn_node_list", "component": "yarn", "risk": "none", "source": "internal", "description": "YARN NodeManager 列表"},
    {"name": "yarn_app_attempt_logs", "component": "yarn", "risk": "none", "source": "internal", "description": "YARN 应用 attempt 日志"},
    {"name": "yarn_scheduler_info", "component": "yarn", "risk": "none", "source": "internal", "description": "YARN 调度器配置"},
    {"name": "yarn_node_resource_usage", "component": "yarn", "risk": "none", "source": "internal", "description": "NodeManager 资源使用"},
    # Kafka (7)
    {"name": "kafka_cluster_overview", "component": "kafka", "risk": "none", "source": "internal", "description": "Kafka 集群概览"},
    {"name": "kafka_consumer_lag", "component": "kafka", "risk": "none", "source": "internal", "description": "消费者组 Lag"},
    {"name": "kafka_topic_list", "component": "kafka", "risk": "none", "source": "internal", "description": "Topic 列表"},
    {"name": "kafka_topic_detail", "component": "kafka", "risk": "none", "source": "internal", "description": "Topic 详情"},
    {"name": "kafka_broker_configs", "component": "kafka", "risk": "none", "source": "internal", "description": "Broker 配置"},
    {"name": "kafka_under_replicated", "component": "kafka", "risk": "none", "source": "internal", "description": "ISR 不同步分区"},
    {"name": "kafka_consumer_groups", "component": "kafka", "risk": "none", "source": "internal", "description": "消费者组列表"},
    # ES (6)
    {"name": "es_cluster_health", "component": "es", "risk": "none", "source": "internal", "description": "ES 集群健康"},
    {"name": "es_node_stats", "component": "es", "risk": "none", "source": "internal", "description": "ES 节点统计"},
    {"name": "es_index_stats", "component": "es", "risk": "none", "source": "internal", "description": "ES 索引统计"},
    {"name": "es_pending_tasks", "component": "es", "risk": "none", "source": "internal", "description": "ES 挂起任务"},
    {"name": "es_shard_allocation", "component": "es", "risk": "none", "source": "internal", "description": "ES 分片分配"},
    {"name": "es_hot_threads", "component": "es", "risk": "none", "source": "internal", "description": "ES 热点线程"},
    # 通用查询 (5)
    {"name": "query_metrics", "component": "metrics", "risk": "none", "source": "internal", "description": "Prometheus 指标查询"},
    {"name": "search_logs", "component": "log", "risk": "none", "source": "internal", "description": "日志搜索"},
    {"name": "query_alerts", "component": "alert", "risk": "none", "source": "internal", "description": "查询活跃告警"},
    {"name": "query_events", "component": "event", "risk": "none", "source": "internal", "description": "查询变更事件"},
    {"name": "query_topology", "component": "topology", "risk": "none", "source": "internal", "description": "查询组件拓扑"},
    # ZooKeeper (3)
    {"name": "zk_status", "component": "zk", "risk": "none", "source": "internal", "description": "ZK 集群状态"},
    {"name": "zk_node_list", "component": "zk", "risk": "none", "source": "internal", "description": "ZK 节点列表"},
    {"name": "zk_session_list", "component": "zk", "risk": "none", "source": "internal", "description": "ZK 活跃会话"},
    # 运维操作 (6)
    {"name": "ops_restart_service", "component": "ops", "risk": "high", "source": "internal", "description": "重启服务进程（需 HITL 审批）"},
    {"name": "ops_scale_resource", "component": "ops", "risk": "high", "source": "internal", "description": "扩缩容资源（需 HITL 审批）"},
    {"name": "ops_update_config", "component": "ops", "risk": "medium", "source": "internal", "description": "更新组件配置参数"},
    {"name": "ops_clear_cache", "component": "ops", "risk": "low", "source": "internal", "description": "清除组件缓存"},
    {"name": "ops_trigger_gc", "component": "ops", "risk": "medium", "source": "internal", "description": "触发 JVM Full GC"},
    {"name": "ops_decommission_node", "component": "ops", "risk": "critical", "source": "internal", "description": "退役节点（需双人审批）"},
]


class ToolRegistry:
    """工具元数据注册中心."""

    def __init__(self) -> None:
        self._tools: dict[str, dict[str, Any]] = {}
        self._component_index: dict[str, list[str]] = {}
        self._source_index: dict[str, list[str]] = {}

        for t in INTERNAL_TOOLS:
            self._register(t)

        logger.info("tool_registry_initialized", total=len(self._tools))

    def _register(self, tool: dict[str, Any]) -> None:
        name = tool["name"]
        self._tools[name] = tool
        comp = tool.get("component", "unknown")
        self._component_index.setdefault(comp, []).append(name)
        source = tool.get("source", "internal")
        self._source_index.setdefault(source, []).append(name)

    def get(self, name: str) -> dict[str, Any] | None:
        return self._tools.get(name)

    def list_all(self) -> list[dict[str, Any]]:
        return list(self._tools.values())

    def list_by_component(self, component: str) -> list[dict[str, Any]]:
        names = self._component_index.get(component, [])
        return [self._tools[n] for n in names]

    def list_by_risk(self, max_risk: str) -> list[dict[str, Any]]:
        max_level = RiskLevel(max_risk).order
        return [t for t in self._tools.values() if RiskLevel(t["risk"]).order <= max_level]

    def list_components(self) -> list[str]:
        return sorted(self._component_index.keys())

    def filter_for_user(
        self,
        user_role: str,
        components: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """RBAC 动态过滤."""
        role_max_risk = {
            "viewer": "none", "operator": "none",
            "engineer": "medium", "admin": "high", "super": "critical",
        }
        max_risk = role_max_risk.get(user_role, "none")
        tools = self.list_by_risk(max_risk)
        if components:
            always_available = {"metrics", "log", "alert", "event", "topology"}
            allowed = set(components) | always_available
            tools = [t for t in tools if t["component"] in allowed]
        return tools

    def format_for_prompt(self, tools: list[dict[str, Any]] | None = None) -> str:
        """格式化工具列表，用于注入 LLM Prompt."""
        tools = tools or self.list_all()
        lines = []
        for t in sorted(tools, key=lambda x: (x["component"], x["name"])):
            lines.append(f"- {t['name']} [{t['component']}, risk:{t['risk']}]: {t.get('description', '')}")
        return "\n".join(lines)

    def update_from_discovery(self, discovered_tools: list[dict[str, Any]]) -> int:
        updated = 0
        for tool in discovered_tools:
            name = tool.get("name")
            if name and name not in self._tools:
                self._register(tool)
                updated += 1
        return updated
