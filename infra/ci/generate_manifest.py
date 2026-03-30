"""
Release Manifest 自动生成器

从 Git、评测结果、配置等数据源收集版本信息，
组装成一份完整的 Release Manifest。

在 CI/CD Stage 4 自动执行。
文档参考: 20-部署CICD与运维.md §2.2
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


def get_git_info() -> dict[str, str]:
    """获取 Git 提交信息."""
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True
    ).strip()
    branch = subprocess.check_output(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], text=True
    ).strip()
    # 尝试获取 tag
    try:
        tag = subprocess.check_output(
            ["git", "describe", "--tags", "--always"], text=True
        ).strip()
    except subprocess.CalledProcessError:
        tag = "dev"
    return {"commit": commit, "branch": branch, "tag": tag}


def get_eval_results(eval_dir: str) -> dict[str, Any]:
    """从最近一次评测结果中读取指标."""
    results_file = Path(eval_dir) / "latest_results.json"
    if results_file.exists():
        with open(results_file) as f:
            return json.load(f)
    return {}


def generate_manifest(version: str, registry: str) -> dict[str, Any]:
    """生成 Release Manifest."""
    git = get_git_info()
    eval_results = get_eval_results("eval/results/")

    manifest: dict[str, Any] = {
        "release": {
            "version": version,
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "author": "ci-bot",
            "git_commit": git["commit"],
            "git_branch": git["branch"],
            "git_tag": git["tag"],
        },
        "components": {
            "agent_service": {
                "image": f"{registry}/agent-service:{version}",
                "git_commit": git["commit"],
            },
            "mcp_server": {
                "image": f"{registry}/mcp-server:{version}",
                "git_commit": git["commit"],
            },
        },
        "models": {
            "primary_llm": {"provider": "deepseek", "model": "deepseek-chat"},
            "fallback_llm": {"provider": "openai", "model": "gpt-4o"},
            "embedding": {"model": "BAAI/bge-m3", "dimension": 1024},
            "reranker": {"model": "BAAI/bge-reranker-large"},
        },
        "evaluation": eval_results,
        "prompts": {"version": "v2.3"},
        "config": {"version": version},
    }

    return manifest


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Generate Release Manifest")
    parser.add_argument("--version", required=True, help="Release version")
    parser.add_argument(
        "--registry", default="registry.internal/aiops", help="Docker registry"
    )
    parser.add_argument(
        "--output", default="release-manifest.yaml", help="Output file path"
    )
    args = parser.parse_args()

    manifest = generate_manifest(args.version, args.registry)

    with open(args.output, "w") as f:
        yaml.dump(manifest, f, default_flow_style=False, allow_unicode=True)

    print(f"✅ Release manifest generated: {args.output}")


if __name__ == "__main__":
    main()
