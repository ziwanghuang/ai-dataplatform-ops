#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════
AI DataPlatform Ops Agent — 面试演示脚本

3 个场景完整演示 Agent 核心能力:

  场景 1: HDFS 容量查询（快速路径）
    → 展示 DirectTool 零 LLM Token 快速响应
    → 预期耗时: <1s

  场景 2: Kafka Consumer Lag 诊断（完整链路）
    → 展示 Triage → Diagnostic → Planning → Report 全链路
    → 展示假设-验证循环 + 并行工具调用 + RAG 增强
    → 预期耗时: 10-30s

  场景 3: NameNode 重启审批（HITL 流程）
    → 展示高风险操作触发 5 级风控 → 人机审批门控
    → 展示审批后受控执行 → 报告沉淀
    → 预期耗时: 依赖人工审批

运行方式:
  python scripts/demo.py                    # 运行全部场景
  python scripts/demo.py --scenario 1       # 运行指定场景
  python scripts/demo.py --base-url http://localhost:8080

═══════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Any

import httpx

# ── 配置 ──────────────────────────────────────

DEFAULT_BASE_URL = "http://localhost:8080"
TIMEOUT = 120.0

# ANSI 颜色
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def banner(text: str) -> None:
    width = 60
    print(f"\n{CYAN}{'═' * width}")
    print(f"  {BOLD}{text}{RESET}{CYAN}")
    print(f"{'═' * width}{RESET}\n")


def step(num: int, text: str) -> None:
    print(f"  {YELLOW}[Step {num}]{RESET} {text}")


def success(text: str) -> None:
    print(f"  {GREEN}✅ {text}{RESET}")


def error(text: str) -> None:
    print(f"  {RED}❌ {text}{RESET}")


def info(text: str) -> None:
    print(f"  {CYAN}ℹ️  {text}{RESET}")


def print_json(data: Any) -> None:
    """Pretty print JSON 响应."""
    formatted = json.dumps(data, indent=2, ensure_ascii=False)
    # 限制输出长度
    lines = formatted.split("\n")
    if len(lines) > 30:
        print("\n".join(lines[:25]))
        print(f"    ... ({len(lines) - 25} more lines)")
    else:
        print(formatted)


# ── 场景 1: HDFS 容量查询 ─────────────────────

async def scenario_1_hdfs_query(client: httpx.AsyncClient) -> bool:
    """场景 1: HDFS 容量查询 — 快速路径."""
    banner("场景 1: HDFS 容量查询（快速路径 / DirectTool）")

    info("演示目标: 简单查询走 DirectTool，零 LLM Token，<1s 响应")
    print()

    step(1, "发送 HDFS 状态查询请求...")
    start = time.monotonic()
    try:
        resp = await client.post(
            "/api/v1/agent/diagnose",
            json={
                "query": "查看 HDFS NameNode 状态",
                "request_type": "simple_query",
            },
        )
        elapsed = time.monotonic() - start

        step(2, f"收到响应 [{resp.status_code}], 耗时 {elapsed:.2f}s")

        if resp.status_code == 200:
            data = resp.json()
            print_json(data)
            print()

            step(3, "验证快速路径特征...")
            route = data.get("route", "unknown")
            info(f"路由路径: {route}")
            info(f"响应耗时: {elapsed:.2f}s")

            if elapsed < 5.0:
                success(f"快速路径验证通过 — {elapsed:.2f}s < 5s 阈值")
            else:
                info(f"响应耗时 {elapsed:.2f}s，可能走了 LLM 路径")

            return True
        else:
            error(f"请求失败: {resp.status_code}")
            return False
    except Exception as e:
        error(f"请求异常: {e}")
        return False


# ── 场景 2: Kafka Lag 诊断 ─────────────────────

async def scenario_2_kafka_diagnosis(client: httpx.AsyncClient) -> bool:
    """场景 2: Kafka Consumer Lag 诊断 — 完整链路."""
    banner("场景 2: Kafka Consumer Lag 诊断（完整链路）")

    info("演示目标: Triage → Diagnostic（假设-验证循环）→ Planning → Report")
    info("展示: 并行工具调用 / RAG 知识增强 / 结构化根因分析")
    print()

    step(1, "发送 Kafka Lag 诊断请求...")
    start = time.monotonic()
    try:
        resp = await client.post(
            "/api/v1/agent/diagnose",
            json={
                "query": (
                    "payment-consumer 消费组在 production 集群的 lag "
                    "持续增长，当前堆积超过 100 万条消息，已持续 30 分钟。"
                    "消费者实例数正常(3个)，无重启记录。"
                ),
                "request_type": "diagnosis",
                "context": {
                    "component": "kafka",
                    "cluster": "production",
                    "consumer_group": "payment-consumer",
                    "current_lag": 1_000_000,
                    "duration_minutes": 30,
                    "urgency": "high",
                },
            },
        )
        elapsed = time.monotonic() - start

        step(2, f"收到响应 [{resp.status_code}], 耗时 {elapsed:.1f}s")

        if resp.status_code == 200:
            data = resp.json()

            step(3, "分析诊断结果...")
            print_json(data)
            print()

            # 提取关键信息
            if "report" in data:
                info("📋 生成了诊断报告")
            if "root_cause" in data:
                info(f"🔍 根因: {data['root_cause']}")
            if "remediation" in data:
                info(f"💊 修复建议: {data['remediation'][:100]}...")
            if "confidence" in data:
                info(f"📊 置信度: {data['confidence']}")
            if "tools_called" in data:
                info(f"🔧 调用工具数: {len(data['tools_called'])}")

            success(f"完整诊断链路完成 — 耗时 {elapsed:.1f}s")
            return True
        else:
            error(f"请求失败: {resp.status_code}")
            print(resp.text[:500])
            return False
    except Exception as e:
        error(f"请求异常: {e}")
        return False


# ── 场景 3: NameNode 重启审批 ──────────────────

async def scenario_3_hitl_approval(client: httpx.AsyncClient) -> bool:
    """场景 3: NameNode 重启 — HITL 审批流程."""
    banner("场景 3: NameNode 重启审批（HITL 人机协作）")

    info("演示目标: 高风险操作 → 5 级风控拦截 → 人工审批 → 受控执行")
    info("展示: RiskClassifier / HITLGate / ApprovalWorkflow")
    print()

    step(1, "发送高风险修复请求...")
    start = time.monotonic()
    try:
        resp = await client.post(
            "/api/v1/agent/diagnose",
            json={
                "query": (
                    "NameNode 进程 Full GC 频繁（每分钟 3 次），heap 使用率 95%，"
                    "RPC 队列堆积 2000+，建议重启 NameNode。"
                ),
                "request_type": "diagnosis",
                "context": {
                    "component": "hdfs",
                    "subcomponent": "namenode",
                    "urgency": "critical",
                    "remediation_hint": "restart_namenode",
                },
            },
        )
        elapsed = time.monotonic() - start

        step(2, f"收到响应 [{resp.status_code}], 耗时 {elapsed:.1f}s")

        if resp.status_code == 200:
            data = resp.json()
            print_json(data)
            print()

            # 检查是否触发了 HITL
            risk_level = data.get("risk_level", "unknown")
            needs_approval = data.get("needs_approval", False)
            approval_id = data.get("approval_id")

            step(3, "检查风控结果...")
            info(f"风险等级: {risk_level}")
            info(f"需要审批: {needs_approval}")

            if approval_id:
                info(f"审批单 ID: {approval_id}")

                step(4, "模拟审批操作...")
                # 尝试批准
                approval_resp = await client.post(
                    f"/api/v1/approval/{approval_id}/approve",
                    json={
                        "approver": "demo-admin",
                        "comment": "演示审批通过",
                    },
                )
                if approval_resp.status_code == 200:
                    success("审批通过 ✅")
                else:
                    info(f"审批响应: {approval_resp.status_code}")
            else:
                info("未生成审批单（可能 DEV 环境自动批准）")

            success(f"HITL 审批流程演示完成 — 耗时 {elapsed:.1f}s")
            return True
        else:
            error(f"请求失败: {resp.status_code}")
            return False
    except Exception as e:
        error(f"请求异常: {e}")
        return False


# ── 主入口 ────────────────────────────────────

async def run_demo(base_url: str, scenarios: list[int]) -> None:
    """运行指定的演示场景."""
    print(f"""
{BOLD}╔════════════════════════════════════════════════════════╗
║  AI DataPlatform Ops Agent — 面试演示                  ║
║                                                        ║
║  大数据平台智能运维 Agent 系统                          ║
║  核心能力: 告警分诊 → 根因诊断 → 方案规划 → 人机审批   ║
╚════════════════════════════════════════════════════════╝{RESET}
""")

    info(f"Agent URL: {base_url}")

    async with httpx.AsyncClient(base_url=base_url, timeout=TIMEOUT) as client:
        # 健康检查
        step(0, "检查 Agent 服务状态...")
        try:
            health = await client.get("/healthz")
            if health.status_code == 200:
                success("Agent 服务运行中")
            else:
                error(f"Agent 服务异常: {health.status_code}")
                return
        except httpx.ConnectError:
            error(f"无法连接 Agent 服务 ({base_url})")
            info("请确保 Agent 服务已启动: uvicorn aiops.web.app:app --port 8080")
            return

        print()
        results: dict[int, bool] = {}

        scenario_map = {
            1: ("HDFS 容量查询", scenario_1_hdfs_query),
            2: ("Kafka Lag 诊断", scenario_2_kafka_diagnosis),
            3: ("NameNode 重启审批", scenario_3_hitl_approval),
        }

        for num in scenarios:
            if num in scenario_map:
                name, func = scenario_map[num]
                results[num] = await func(client)
                print()
            else:
                error(f"未知场景: {num}")

        # 汇总
        banner("演示结果汇总")
        for num in sorted(results):
            name = scenario_map[num][0]
            status = f"{GREEN}PASS{RESET}" if results[num] else f"{RED}FAIL{RESET}"
            print(f"  场景 {num}: {name} — {status}")

        total = len(results)
        passed = sum(1 for v in results.values() if v)
        print(f"\n  总计: {passed}/{total} 通过")

        if passed == total:
            print(f"\n  {GREEN}{BOLD}🎉 全部场景通过！{RESET}")
        else:
            print(f"\n  {YELLOW}⚠️  部分场景未通过，请检查日志{RESET}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AI DataPlatform Ops Agent — 面试演示脚本"
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Agent 服务地址 (默认: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--scenario",
        type=int,
        nargs="*",
        default=[1, 2, 3],
        help="运行的场景编号 (默认: 1 2 3)",
    )
    args = parser.parse_args()

    asyncio.run(run_demo(args.base_url, args.scenario))


if __name__ == "__main__":
    main()
