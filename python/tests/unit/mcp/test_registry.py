"""MCP Client Registry 单元测试."""

from __future__ import annotations

from aiops.mcp_client.registry import ToolRegistry


class TestToolRegistry:
    def setup_method(self) -> None:
        self.registry = ToolRegistry()

    def test_total_tools_count(self) -> None:
        assert len(self.registry.list_all()) == 42

    def test_get_existing_tool(self) -> None:
        tool = self.registry.get("hdfs_namenode_status")
        assert tool is not None
        assert tool["component"] == "hdfs"
        assert tool["risk"] == "none"

    def test_get_nonexistent_tool(self) -> None:
        assert self.registry.get("nonexistent_tool") is None

    def test_list_by_component(self) -> None:
        hdfs = self.registry.list_by_component("hdfs")
        assert len(hdfs) == 8

    def test_list_by_risk(self) -> None:
        safe = self.registry.list_by_risk("none")
        assert all(t["risk"] == "none" for t in safe)

    def test_filter_viewer_role(self) -> None:
        tools = self.registry.filter_for_user("viewer")
        assert all(t["risk"] == "none" for t in tools)

    def test_filter_admin_role(self) -> None:
        tools = self.registry.filter_for_user("admin")
        risks = {t["risk"] for t in tools}
        assert "high" in risks
        assert "critical" not in risks

    def test_filter_super_role(self) -> None:
        tools = self.registry.filter_for_user("super")
        risks = {t["risk"] for t in tools}
        assert "critical" in risks

    def test_list_components(self) -> None:
        comps = self.registry.list_components()
        assert "hdfs" in comps
        assert "kafka" in comps

    def test_format_for_prompt(self) -> None:
        text = self.registry.format_for_prompt()
        assert "hdfs_namenode_status" in text

    def test_update_from_discovery(self) -> None:
        count = self.registry.update_from_discovery([
            {"name": "new_tool", "component": "test", "risk": "none", "source": "internal"},
        ])
        assert count == 1
        assert self.registry.get("new_tool") is not None
