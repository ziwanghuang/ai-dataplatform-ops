"""
DirectTool Node — 快速路径：直接调用工具返回结果

~40% 的请求是简单查询（"HDFS 容量多少"），不需要走完整诊断流程。
Triage 的规则引擎识别后设置 route=direct_tool，流程直接到这个节点：
1. 调用指定工具获取数据
2. 用轻量 LLM（或模板）格式化结果
3. 直接返回 → END（跳过 Planning/Diagnostic/Report 全部）

WHY 快速路径的价值：
- 延迟：2-3s（vs 完整链路 12-15s）
- Token：~2K（vs 完整链路 ~15K）
- 成本：省 70%+ Token
"""

from __future__ import annotations

from typing import Any

from aiops.agent.base import BaseAgentNode
from aiops.agent.state import AgentState
from aiops.core.logging import get_logger
from aiops.llm.types import TaskType

logger = get_logger(__name__)


class DirectToolNode(BaseAgentNode):
    """
    快速路径节点 — 直接调用工具并格式化结果.

    接收 Triage 设置的 _direct_tool_name 和 _direct_tool_params，
    调用 MCP 工具后格式化结果写入 final_report。
    """

    agent_name = "direct_tool"
    task_type = TaskType.TRIAGE  # 复用 triage 的轻量模型

    def __init__(self, llm_client: Any | None = None, mcp_client: Any | None = None, **kwargs: Any) -> None:
        super().__init__(llm_client, **kwargs)
        self._mcp = mcp_client

    async def process(self, state: AgentState) -> AgentState:
        """
        快速路径主流程.

        Step 1: 获取工具名和参数（由 Triage 设置）
        Step 2: 调用 MCP 工具
        Step 3: 格式化结果为用户可读文本
        Step 4: 写入 final_report（直接 → END，不经过 Report Agent）
        """
        tool_name = state.get("_direct_tool_name", "")
        tool_params = state.get("_direct_tool_params", {})

        if not tool_name:
            state["final_report"] = "❌ 未指定工具名，无法执行快速路径查询。"
            return state

        # 调用工具
        try:
            if self._mcp:
                result = await self._mcp.call_tool(tool_name, tool_params)
            else:
                result = f"[Mock] {tool_name} 工具结果（MCP 客户端未配置）"

            # 格式化结果（模板方式，零 Token）
            state["final_report"] = (
                f"## 🔍 查询结果\n\n"
                f"**工具**: `{tool_name}`\n"
                f"**查询**: {state.get('user_query', '')}\n\n"
                f"```\n{result}\n```"
            )

            logger.info("direct_tool_success", tool=tool_name)

        except Exception as e:
            state["final_report"] = (
                f"## ❌ 查询失败\n\n"
                f"**工具**: `{tool_name}`\n"
                f"**错误**: {e}\n\n"
                f"建议使用完整诊断模式获取更详细的分析。"
            )
            logger.error("direct_tool_failed", tool=tool_name, error=str(e))

        return state
