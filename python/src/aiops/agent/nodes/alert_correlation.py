"""
AlertCorrelation Agent — 告警聚合与关联分析

当多个告警同时到达时（如 NameNode OOM + DataNode 心跳超时 + Block 复制失败），
需要先关联分析这些告警之间的因果关系，再进入诊断流程。

WHY 告警关联：
- 同一根因可能触发 5-10 个不同告警（告警风暴）
- 如果每个告警独立诊断：5 个告警 × 15K Token = 75K Token，但根因可能只有一个
- 先关联再诊断：合并为 1 次诊断，~20K Token，且诊断质量更高（信息更全）
"""

from __future__ import annotations

from typing import Any

from aiops.agent.base import BaseAgentNode
from aiops.agent.state import AgentState
from aiops.core.logging import get_logger
from aiops.llm.types import TaskType

logger = get_logger(__name__)

# 告警组件推断规则：从告警标签中提取组件
COMPONENT_PATTERNS = {
    "hdfs": ["hdfs", "namenode", "datanode", "dfs", "block"],
    "yarn": ["yarn", "resourcemanager", "nodemanager", "queue", "application"],
    "kafka": ["kafka", "broker", "consumer", "topic", "partition", "lag"],
    "es": ["elasticsearch", "es_cluster", "shard", "index"],
    "zk": ["zookeeper", "zk"],
}


class AlertCorrelationNode(BaseAgentNode):
    """
    告警关联节点 — 分析多个告警之间的因果关系.

    输入：state["alerts"]（多个告警）
    输出：修改 state["user_query"]（聚合后的描述）+ target_components
    """

    agent_name = "alert_correlation"
    task_type = TaskType.ALERT_CORRELATION

    async def process(self, state: AgentState) -> AgentState:
        """
        告警关联分析主流程.

        Step 1: 提取所有告警涉及的组件
        Step 2: 按组件分组聚合
        Step 3: 生成聚合后的问题描述（替换 user_query）
        Step 4: 设置 target_components 缩小诊断范围
        """
        alerts = state.get("alerts", [])

        if not alerts:
            logger.info("alert_correlation_skip", reason="no alerts")
            return state

        # Step 1: 提取组件
        components: set[str] = set()
        for alert in alerts:
            comp = self._extract_component(alert)
            if comp:
                components.add(comp)

        # Step 2: 按组件分组
        grouped: dict[str, list[dict[str, Any]]] = {}
        for alert in alerts:
            comp = self._extract_component(alert) or "unknown"
            grouped.setdefault(comp, []).append(alert)

        # Step 3: 生成聚合描述
        descriptions = []
        for comp, comp_alerts in grouped.items():
            alert_msgs = [a.get("message", str(a))[:100] for a in comp_alerts[:3]]
            descriptions.append(f"[{comp}] {len(comp_alerts)} 个告警: {'; '.join(alert_msgs)}")

        aggregated_query = (
            f"告警关联分析 — 共 {len(alerts)} 个告警涉及 {len(components)} 个组件:\n"
            + "\n".join(descriptions)
        )

        # 覆盖 user_query（这是唯一允许修改 user_query 的场景）
        state["user_query"] = aggregated_query
        state["target_components"] = sorted(components)
        state["complexity"] = "complex"  # 多告警默认复杂
        state["urgency"] = self._determine_urgency(alerts)

        logger.info(
            "alert_correlation_completed",
            total_alerts=len(alerts),
            components=sorted(components),
            urgency=state["urgency"],
        )

        return state

    @staticmethod
    def _extract_component(alert: dict[str, Any]) -> str | None:
        """
        从告警中推断涉及的组件.

        优先从 labels.component 读取（标准化告警），
        其次从 message 中用关键词匹配（非标准告警）。
        """
        # 优先级 1: 显式标签
        component = alert.get("component") or alert.get("labels", {}).get("component")
        if component:
            return component.lower()

        # 优先级 2: 从 message 中推断
        message = (alert.get("message", "") + " " + str(alert.get("labels", {}))).lower()
        for comp, keywords in COMPONENT_PATTERNS.items():
            if any(kw in message for kw in keywords):
                return comp

        return None

    @staticmethod
    def _determine_urgency(alerts: list[dict[str, Any]]) -> str:
        """
        根据告警严重度确定整体紧急度.

        取所有告警中最高的严重度作为整体紧急度。
        """
        severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
        min_order = 4  # 默认最低紧急度

        for alert in alerts:
            severity = alert.get("severity", "medium").lower()
            order = severity_order.get(severity, 3)
            min_order = min(min_order, order)

        # 反向映射
        urgency_map = {0: "critical", 1: "high", 2: "medium", 3: "low", 4: "low"}
        return urgency_map.get(min_order, "medium")
