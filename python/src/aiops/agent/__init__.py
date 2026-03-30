"""Agent orchestration: LangGraph state machine, agent nodes, routing.

Public API:
- build_ops_graph: 构建完整的 AIOps LangGraph 状态图
- AgentState / create_initial_state: 状态定义和工厂
- BaseAgentNode: Agent 节点基类
- route_from_triage / route_from_diagnostic / route_from_hitl: 条件路由
- ContextCompressor: LLM 输入上下文压缩器
"""

from aiops.agent.state import AgentState, create_initial_state, snapshot_state
from aiops.agent.base import BaseAgentNode
from aiops.agent.graph import build_ops_graph
from aiops.agent.router import (
    route_from_diagnostic,
    route_from_hitl,
    route_from_triage,
)
from aiops.agent.compressor import ContextCompressor

__all__ = [
    "AgentState",
    "BaseAgentNode",
    "ContextCompressor",
    "build_ops_graph",
    "create_initial_state",
    "route_from_diagnostic",
    "route_from_hitl",
    "route_from_triage",
    "snapshot_state",
]
