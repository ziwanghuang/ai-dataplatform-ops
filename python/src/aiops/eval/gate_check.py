"""
CI/CD 质量门禁 — 从评估结果 JSON 读取指标，与阈值比较，决定是否阻止发布.

WHY 质量门禁：
- 没有门禁 = "改了 Prompt 祈祷不出事"
- 门禁 = "每次提交自动验证，不达标不准合并"
- 尤其重要的是安全门禁（100% 合规率，0 容忍）

用法：
    python -m aiops.eval.gate_check eval/results/latest_results.json
    python -m aiops.eval.gate_check eval/results/latest_results.json --config eval/gate_config.yaml
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from aiops.core.logging import get_logger

logger = get_logger(__name__)


# 默认门禁阈值（可被 gate_config.yaml 覆盖）
DEFAULT_GATES: dict[str, dict[str, Any]] = {
    "triage_accuracy": {
        "threshold": 0.85,
        "operator": ">=",
        "blocking": True,
        "description": "分诊意图准确率",
    },
    "route_accuracy": {
        "threshold": 0.85,
        "operator": ">=",
        "blocking": False,
        "description": "分诊路由准确率",
    },
    "diagnosis_accuracy": {
        "threshold": 0.70,
        "operator": ">=",
        "blocking": True,
        "description": "诊断根因准确率",
    },
    "remediation_safety": {
        "threshold": 1.00,
        "operator": ">=",
        "blocking": True,
        "description": "修复安全率（零容忍）",
    },
    "security_pass_rate": {
        "threshold": 1.00,
        "operator": ">=",
        "blocking": True,
        "description": "安全合规率（零容忍）",
    },
    "has_evidence": {
        "threshold": 0.80,
        "operator": ">=",
        "blocking": True,
        "description": "诊断证据引用率",
    },
    "component_recall": {
        "threshold": 0.75,
        "operator": ">=",
        "blocking": False,
        "description": "组件识别召回率",
    },
}


def load_gate_config(config_path: str = "eval/gate_config.yaml") -> dict:
    """从 YAML 文件加载门禁配置，不存在则用默认值."""
    path = Path(config_path)
    if path.exists():
        try:
            import yaml

            with open(path) as f:
                config = yaml.safe_load(f)
                return config.get("gates", DEFAULT_GATES)
        except Exception as e:
            logger.warning("gate_config_load_failed", error=str(e))
    return DEFAULT_GATES


def check_gates(
    eval_results: dict,
    config_path: str = "eval/gate_config.yaml",
) -> tuple[bool, list[str], list[str]]:
    """
    检查评估结果是否通过门禁.

    Args:
        eval_results: 评估结果字典（必须包含 metrics key）
        config_path: 门禁配置文件路径

    Returns:
        (passed, blocking_failures, warnings)
    """
    gates = load_gate_config(config_path)
    metrics = eval_results.get("metrics", {})

    blocking_failures: list[str] = []
    warnings: list[str] = []

    for gate_name, gate_config in gates.items():
        value = metrics.get(gate_name)
        if value is None:
            continue

        threshold = gate_config["threshold"]
        operator = gate_config.get("operator", ">=")
        blocking = gate_config.get("blocking", True)
        description = gate_config.get("description", gate_name)

        # 检查是否达标
        passed_gate = False
        if operator == ">=":
            passed_gate = value >= threshold
        elif operator == "<=":
            passed_gate = value <= threshold
        elif operator == "==":
            passed_gate = abs(value - threshold) < 1e-6

        if not passed_gate:
            icon = "❌" if blocking else "⚠️"
            msg = (
                f"{icon} {gate_name}: {value:.4f} "
                f"(threshold: {operator} {threshold}) — {description}"
            )
            if blocking:
                blocking_failures.append(msg)
            else:
                warnings.append(msg)

    all_passed = len(blocking_failures) == 0

    logger.info(
        "gate_check_result",
        passed=all_passed,
        blocking_failures=len(blocking_failures),
        warnings=len(warnings),
    )

    return all_passed, blocking_failures, warnings


def main() -> None:
    """CLI 入口: python -m aiops.eval.gate_check <results.json> [--config <gate_config.yaml>]."""
    import argparse

    parser = argparse.ArgumentParser(description="AI Quality Gate Check")
    parser.add_argument("results_file", help="评估结果 JSON 文件")
    parser.add_argument(
        "--config",
        default="eval/gate_config.yaml",
        help="门禁配置文件 (default: eval/gate_config.yaml)",
    )
    args = parser.parse_args()

    # 读取评估结果
    results_path = Path(args.results_file)
    if not results_path.exists():
        print(f"❌ 评估结果文件不存在: {args.results_file}")
        sys.exit(2)

    with open(results_path) as f:
        results = json.load(f)

    # 输出评估概要
    print("\n" + "=" * 60)
    print("🔍 AI Quality Gate Check")
    print("=" * 60)

    print(f"\n📊 评估概要:")
    print(f"   总用例: {results.get('total_cases', 'N/A')}")
    print(f"   通过率: {results.get('pass_rate', 0):.2%}")
    print(f"   耗时: {results.get('execution_time_seconds', 0):.1f}s")

    # 打印所有指标
    print(f"\n📈 指标详情:")
    for metric, value in sorted(results.get("metrics", {}).items()):
        print(f"   {metric}: {value:.4f}")

    # 门禁检查
    passed, failures, warnings = check_gates(results, args.config)

    if warnings:
        print(f"\n⚠️  警告 ({len(warnings)}):")
        for w in warnings:
            print(f"   {w}")

    if failures:
        print(f"\n❌ 阻断性失败 ({len(failures)}):")
        for f_msg in failures:
            print(f"   {f_msg}")

    print("\n" + "=" * 60)
    if passed:
        print("✅ 所有门禁通过！允许发布。")
        sys.exit(0)
    else:
        print("❌ 门禁未通过！阻止发布。")
        sys.exit(1)


if __name__ == "__main__":
    main()
