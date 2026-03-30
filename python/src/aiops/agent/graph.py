"""
OpsGraph — LangGraph 状态图定义

这是整个 Agent 系统的中枢——定义所有节点和条件边。

图结构（参考 03-Agent核心框架与状态机.md §4）：

    请求 → Triage → ┬→ DirectTool → END（快速路径，~40%请求）
                     ├→ AlertCorrelation → Planning → Diagnostic ─┐
                     └→ Planning → Diagnostic ─────────────────────┤
                                                                    │
                     ┌──── need_more_data（自环，≤5轮）──────────────┘
                     │
                     Diagnostic → ┬→ HITL Gate → ┬→ Remediation → Report → KnowledgeSink → END
                                  │              └→ Report → KnowledgeSink → END
                                  └→ Report → KnowledgeSink → END

WHY 入口固定是 Triage：
- 所有请求（用户查询/告警/巡检）都需要"安检"和路由
- Triage 决定快速路径 vs 完整诊断，是 Token 节省的关键

WHY DirectTool → END 而不是 DirectTool → Report：
- 快速路径的设计目标是"跳过一切不必要步骤"
- DirectTool 自己会格式化结果，不需要 Report Agent 再润色
"""

from __future__ import annotations

from typing import Any

from aiops.agent.state import AgentState
from aiops.agent.router import (
    route_from_diagnostic,
    route_from_hitl,
    route_from_triage,
)
from aiops.core.logging import get_logger

logger = get_logger(__name__)


def build_ops_graph(llm_client: Any | None = None, mcp_client: Any | None = None) -> Any:
    """
    构建 AIOps 编排图.

    Returns:
        编译后的 LangGraph，可直接 .invoke() 或 .ainvoke()

    WHY 延迟导入所有 Node 类：
    避免循环依赖——Agent 节点 → AgentState → base → router → ...
    如果在模块顶部导入所有节点，很容易产生循环。
    """
    # 延迟导入 LangGraph（可能未安装）
    try:
        from langgraph.graph import END, StateGraph
        from langgraph.checkpoint.memory import MemorySaver
    except ImportError:
        logger.warning("langgraph_not_installed, using mock graph")
        return _build_mock_graph(llm_client, mcp_client)

    # 延迟导入所有节点
    from aiops.agent.nodes.triage import TriageNode
    from aiops.agent.nodes.planning import PlanningNode
    from aiops.agent.nodes.diagnostic import DiagnosticNode
    from aiops.agent.nodes.remediation import RemediationNode
    from aiops.agent.nodes.report import ReportNode
    from aiops.agent.nodes.alert_correlation import AlertCorrelationNode
    from aiops.agent.nodes.direct_tool import DirectToolNode
    from aiops.agent.nodes.hitl_gate import HITLGateNode
    from aiops.agent.nodes.knowledge_sink import KnowledgeSinkNode

    # 实例化节点
    triage = TriageNode(llm_client)
    planning = PlanningNode(llm_client)
    diagnostic = DiagnosticNode(llm_client, mcp_client)
    remediation = RemediationNode(llm_client)
    report = ReportNode(llm_client)
    alert_correlation = AlertCorrelationNode(llm_client)
    direct_tool = DirectToolNode(llm_client, mcp_client)
    hitl_gate = HITLGateNode(llm_client)
    knowledge_sink = KnowledgeSinkNode(llm_client)

    # ── 构建图 ──
    graph = StateGraph(AgentState)

    # 注册所有节点
    graph.add_node("triage", triage)
    graph.add_node("planning", planning)
    graph.add_node("diagnostic", diagnostic)
    graph.add_node("remediation", remediation)
    graph.add_node("report", report)
    graph.add_node("alert_correlation", alert_correlation)
    graph.add_node("direct_tool", direct_tool)
    graph.add_node("hitl_gate", hitl_gate)
    graph.add_node("knowledge_sink", knowledge_sink)

    # 入口：所有请求从 Triage 开始
    graph.set_entry_point("triage")

    # ── 条件边 ──

    # Triage → 三路分发
    graph.add_conditional_edges("triage", route_from_triage, {
        "direct_tool": "direct_tool",      # 快速路径（~40%请求）
        "planning": "planning",            # 完整诊断
        "alert_correlation": "alert_correlation",  # 多告警聚合
    })

    # 快速路径直接结束
    graph.add_edge("direct_tool", END)

    # AlertCorrelation → Planning（聚合后走诊断流程）
    graph.add_edge("alert_correlation", "planning")

    # Planning → Diagnostic
    graph.add_edge("planning", "diagnostic")

    # Diagnostic → 三路判断（自环/审批/报告）
    graph.add_conditional_edges("diagnostic", route_from_diagnostic, {
        "need_more_data": "diagnostic",    # 自环：置信度<0.6，继续采集
        "hitl_gate": "hitl_gate",          # 高风险修复→人工审批
        "report": "report",                # 诊断完成→出报告
    })

    # HITL Gate → 审批结果路由
    graph.add_conditional_edges("hitl_gate", route_from_hitl, {
        "remediation": "remediation",      # 批准→执行修复
        "report": "report",                # 拒绝→跳过修复
    })

    # Remediation → Report
    graph.add_edge("remediation", "report")

    # Report → 知识沉淀 → 结束
    graph.add_edge("report", "knowledge_sink")
    graph.add_edge("knowledge_sink", END)

    # ── 编译 ──
    # 开发环境用 MemorySaver（进程内存，重启丢失）
    # 生产环境必须换 AsyncPostgresSaver（HITL 审批可能跨小时）
    compiled = graph.compile(checkpointer=MemorySaver())

    logger.info("ops_graph_built", nodes=9, edges=12)
    return compiled


def _build_mock_graph(llm_client: Any, mcp_client: Any) -> Any:
    """
    Mock 图——LangGraph 未安装时的降级方案.

    WHY 不直接报错：
    - LangGraph 是可选依赖，showcase 演示时可能未安装
    - Mock 图覆盖核心链路（Triage → Planning → Diagnostic → Report）
    - 不支持条件路由和自环，但基本诊断流程可用

    增强版 Mock 图（vs 之前只有 Triage → Report）：
    - 支持 4 个核心节点的线性执行
    - 异常兜底：任一节点失败不阻塞后续
    - 结果与真实图格式一致
    """
    from aiops.agent.nodes.triage import TriageNode
    from aiops.agent.nodes.planning import PlanningNode
    from aiops.agent.nodes.diagnostic import DiagnosticNode
    from aiops.agent.nodes.report import ReportNode
    from aiops.agent.nodes.knowledge_sink import KnowledgeSinkNode

    class MockGraph:
        """
        增强版线性执行图.

        节点顺序：Triage → Planning → Diagnostic → Report → KnowledgeSink
        不支持：条件路由（DirectTool 快速路径）、自环、HITL 审批
        """

        def __init__(self) -> None:
            self._nodes = [
                ("triage", TriageNode(llm_client)),
                ("planning", PlanningNode(llm_client)),
                ("diagnostic", DiagnosticNode(llm_client, mcp_client)),
                ("report", ReportNode(llm_client)),
                ("knowledge_sink", KnowledgeSinkNode(llm_client)),
            ]

        async def ainvoke(self, state: AgentState, config: dict | None = None) -> AgentState:
            """线性执行所有节点，异常不中断."""
            for name, node in self._nodes:
                try:
                    state = await node(state)
                except Exception as e:
                    logger.warning(
                        "mock_graph_node_failed",
                        node=name,
                        error=str(e),
                    )
                    state["errors"] = state.get("errors", []) + [
                        f"{name}: {e}"
                    ]
            return state

        def invoke(self, state: AgentState, config: dict | None = None) -> AgentState:
            import asyncio
            return asyncio.run(self.ainvoke(state, config))

    logger.warning("using_mock_graph", reason="langgraph_not_installed", nodes=5)
    return MockGraph()
