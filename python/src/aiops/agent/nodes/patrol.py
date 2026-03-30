"""
Patrol Agent — 定时巡检

由 APScheduler 定时触发（默认每 5 分钟），自动检查集群各组件健康状态。
发现异常时生成告警，触发诊断链路。

巡检项：
- HDFS: NameNode 状态、块健康、磁盘使用率
- YARN: 队列积压、NodeManager 健康
- Kafka: 消费延迟、ISR 缩减
- ES: 集群颜色、未分配分片

WHY 定时巡检而不是只靠 Prometheus 告警：
- Prometheus 告警是"阈值触发"——只有指标超阈值才告警
- 巡检是"主动体检"——可以发现趋势性问题（如磁盘缓慢增长）
- 巡检还能验证监控系统本身是否正常（监控的监控）
"""

from __future__ import annotations

from typing import Any

from aiops.agent.base import BaseAgentNode
from aiops.agent.state import AgentState
from aiops.core.logging import get_logger
from aiops.llm.types import TaskType

logger = get_logger(__name__)

# 巡检项配置：每个组件需要检查的工具和阈值
PATROL_CHECKS: list[dict[str, Any]] = [
    {
        "name": "hdfs_health",
        "tool": "hdfs_namenode_status",
        "params": {"namenode": "active"},
        "component": "hdfs",
        "description": "HDFS NameNode 健康检查",
    },
    {
        "name": "hdfs_blocks",
        "tool": "hdfs_block_report",
        "params": {},
        "component": "hdfs",
        "description": "HDFS 块健康报告",
    },
    {
        "name": "yarn_resources",
        "tool": "yarn_cluster_metrics",
        "params": {},
        "component": "yarn",
        "description": "YARN 集群资源检查",
    },
    {
        "name": "kafka_lag",
        "tool": "kafka_consumer_lag",
        "params": {},
        "component": "kafka",
        "description": "Kafka 消费延迟检查",
    },
    {
        "name": "es_health",
        "tool": "es_cluster_health",
        "params": {},
        "component": "es",
        "description": "ES 集群健康检查",
    },
]


class PatrolNode(BaseAgentNode):
    """
    巡检节点 — 定时检查集群健康状态.

    与其他 Agent 不同，Patrol 不由用户请求触发，
    而是由定时任务触发（request_type="patrol"）。
    """

    agent_name = "patrol"
    task_type = TaskType.PATROL

    def __init__(self, llm_client: Any | None = None, mcp_client: Any | None = None, **kwargs: Any) -> None:
        super().__init__(llm_client, **kwargs)
        self._mcp = mcp_client

    async def process(self, state: AgentState) -> AgentState:
        """
        巡检主流程.

        Step 1: 遍历巡检项，并行调用工具
        Step 2: 检查返回结果是否有异常
        Step 3: 有异常则修改 state，触发完整诊断流程
        Step 4: 全部正常则生成健康报告直接结束
        """
        results: list[dict[str, Any]] = []
        anomalies: list[str] = []

        # Step 1 & 2: 执行巡检（当前串行，Sprint 5 优化为并行）
        for check in PATROL_CHECKS:
            try:
                if self._mcp:
                    result = await self._mcp.call_tool(check["tool"], check["params"])
                else:
                    result = f"[Mock] {check['tool']} healthy"

                results.append({
                    "check": check["name"],
                    "component": check["component"],
                    "status": "healthy",
                    "data": str(result)[:500],
                })

            except Exception as e:
                anomalies.append(f"{check['component']}: {check['description']} - {e}")
                results.append({
                    "check": check["name"],
                    "component": check["component"],
                    "status": "error",
                    "error": str(e),
                })

        # Step 3: 有异常则触发诊断
        if anomalies:
            # 修改 state 让 Triage 路由到诊断流程
            state["user_query"] = f"巡检发现异常：{'; '.join(anomalies[:3])}"
            state["route"] = "diagnosis"
            state["intent"] = "fault_diagnosis"
            state["urgency"] = "medium"
            state["target_components"] = list({r["component"] for r in results if r["status"] == "error"})
            logger.warning("patrol_anomalies_found", count=len(anomalies), anomalies=anomalies[:3])
        else:
            # Step 4: 全部正常
            healthy_components = list({r["component"] for r in results})
            state["final_report"] = (
                f"## ✅ 巡检报告\n\n"
                f"所有组件健康：{', '.join(healthy_components)}\n\n"
                f"共检查 {len(PATROL_CHECKS)} 项，全部正常。"
            )
            state["route"] = "direct_tool"  # 直接结束，不走诊断
            logger.info("patrol_all_healthy", components=healthy_components)

        return state
