"""
A/B 测试框架 — Prompt 变更和模型升级的在线验证

WHY A/B 测试：
- Prompt 变更是 AI 系统中最不确定的变更类型
- 离线评估集无法完全模拟生产数据分布
- A/B 测试是唯一能在"不影响全量用户"前提下验证变更效果的方法

关键设计决策：
- WHY 基于 request_id hash 而不是随机：确定性分流，可复现
- WHY 并行执行而不是只跑实验组：需要对照组数据做统计检验
- WHY Welch's t-test 而不是 chi-square：指标是连续值（0-1），不是分类
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from prometheus_client import Counter, Histogram

from aiops.core.logging import get_logger

logger = get_logger(__name__)

# Prometheus 指标
AB_REQUEST_COUNT = Counter(
    "aiops_ab_test_requests_total",
    "Total A/B test requests",
    ["test_id", "group"],
)
AB_LATENCY = Histogram(
    "aiops_ab_test_latency_seconds",
    "A/B test execution latency",
    ["test_id", "group"],
    buckets=[0.5, 1, 2, 5, 10, 30, 60],
)


@dataclass
class ABTestConfig:
    """A/B 测试配置."""

    test_id: str  # 唯一标识
    description: str  # 测试描述
    control_runner: Callable  # 对照组 Agent runner
    experiment_runner: Callable  # 实验组 Agent runner
    traffic_ratio: float = 0.1  # 实验组流量比例（默认 10%）
    min_samples: int = 100  # 最少样本数
    significance_level: float = 0.05  # p-value 阈值
    auto_rollback: bool = True  # 实验组显著差时自动回滚


@dataclass
class ABTestResult:
    """A/B 测试单次执行结果."""

    request_id: str
    group: str  # "control" | "experiment"
    control_output: dict
    experiment_output: dict | None = None
    control_latency_ms: float = 0.0
    experiment_latency_ms: float = 0.0
    control_scores: dict = field(default_factory=dict)
    experiment_scores: dict = field(default_factory=dict)


@dataclass
class ABTestReport:
    """A/B 测试统计报告."""

    test_id: str
    total_samples: int
    control_count: int
    experiment_count: int
    metric_comparisons: dict[str, dict]
    recommendation: str  # "deploy" | "rollback" | "continue_testing"
    details: str


class ABTestRunner:
    """
    A/B 测试运行器.

    基于 request_id hash 分流，对照组结果用于响应用户，
    实验组结果只用于收集数据。
    """

    def __init__(self, config: ABTestConfig) -> None:
        self._config = config
        self._results: list[ABTestResult] = []

    def assign_group(self, request_id: str) -> str:
        """
        基于 request_id 确定性分组.

        WHY hash 而不是随机：
        - 同一 request_id 始终分到同一组
        - 可复现：调试时能确定某个请求走的哪条路径
        - 无状态：不需要维护分组映射表
        """
        hash_hex = hashlib.md5(
            f"{self._config.test_id}:{request_id}".encode()
        ).hexdigest()
        hash_int = int(hash_hex[:8], 16)
        ratio = hash_int / 0xFFFFFFFF

        return "experiment" if ratio < self._config.traffic_ratio else "control"

    async def execute(
        self, request_id: str, query: str, **kwargs: Any
    ) -> dict:
        """
        执行 A/B 测试.

        WHY 两组都执行：
        - 对照组结果用于响应用户
        - 实验组结果只用于收集数据
        - 两组都有数据才能做统计检验
        """
        group = self.assign_group(request_id)
        AB_REQUEST_COUNT.labels(
            test_id=self._config.test_id, group=group
        ).inc()

        # 对照组始终执行
        start_ctrl = time.time()
        control_output = await self._config.control_runner(
            query=query, **kwargs
        )
        control_latency = (time.time() - start_ctrl) * 1000
        AB_LATENCY.labels(
            test_id=self._config.test_id, group="control"
        ).observe(control_latency / 1000)

        # 实验组只在分到实验组时执行
        experiment_output = None
        experiment_latency = 0.0
        if group == "experiment":
            start_exp = time.time()
            try:
                experiment_output = await self._config.experiment_runner(
                    query=query, **kwargs
                )
                experiment_latency = (time.time() - start_exp) * 1000
                AB_LATENCY.labels(
                    test_id=self._config.test_id, group="experiment"
                ).observe(experiment_latency / 1000)
            except Exception as e:
                logger.warning(
                    "ab_experiment_error",
                    test_id=self._config.test_id,
                    error=str(e),
                )

        result = ABTestResult(
            request_id=request_id,
            group=group,
            control_output=control_output,
            experiment_output=experiment_output,
            control_latency_ms=control_latency,
            experiment_latency_ms=experiment_latency,
        )
        self._results.append(result)

        # 始终返回对照组结果（安全第一）
        return control_output

    def generate_report(self) -> ABTestReport:
        """
        生成统计报告.

        WHY Welch's t-test：
        - 不要求两组方差相等（Agent 输出质量方差可能不同）
        - 对样本量不平衡更鲁棒（实验组只有 10% 流量）
        """
        control_results = [r for r in self._results if r.group == "control"]
        experiment_results = [
            r for r in self._results if r.experiment_output is not None
        ]

        total = len(self._results)
        ctrl_count = len(control_results)
        exp_count = len(experiment_results)

        comparisons: dict[str, dict] = {}

        if exp_count >= self._config.min_samples:
            # 比较延迟
            ctrl_latencies = [r.control_latency_ms for r in control_results]
            exp_latencies = [
                r.experiment_latency_ms for r in experiment_results
            ]

            if ctrl_latencies and exp_latencies:
                t_stat, p_value = self._welch_ttest(
                    ctrl_latencies, exp_latencies
                )
                comparisons["latency_ms"] = {
                    "control_mean": sum(ctrl_latencies) / len(ctrl_latencies),
                    "experiment_mean": sum(exp_latencies) / len(exp_latencies),
                    "t_statistic": t_stat,
                    "p_value": p_value,
                    "significant": p_value < self._config.significance_level,
                    "direction": (
                        "experiment_faster"
                        if t_stat > 0
                        else "experiment_slower"
                    ),
                }

            # 比较质量分数
            for metric in ("accuracy", "completeness", "weighted_score"):
                ctrl_scores = [
                    r.control_scores.get(metric, 0)
                    for r in control_results
                    if metric in r.control_scores
                ]
                exp_scores = [
                    r.experiment_scores.get(metric, 0)
                    for r in experiment_results
                    if metric in r.experiment_scores
                ]

                if len(ctrl_scores) >= 10 and len(exp_scores) >= 10:
                    t_stat, p_value = self._welch_ttest(ctrl_scores, exp_scores)
                    ctrl_mean = sum(ctrl_scores) / len(ctrl_scores)
                    exp_mean = sum(exp_scores) / len(exp_scores)

                    comparisons[metric] = {
                        "control_mean": ctrl_mean,
                        "experiment_mean": exp_mean,
                        "t_statistic": t_stat,
                        "p_value": p_value,
                        "significant": p_value
                        < self._config.significance_level,
                        "direction": (
                            "experiment_better"
                            if exp_mean > ctrl_mean
                            else "experiment_worse"
                        ),
                    }

        recommendation = self._decide_recommendation(comparisons, exp_count)
        details = self._format_report_details(
            comparisons, ctrl_count, exp_count
        )

        return ABTestReport(
            test_id=self._config.test_id,
            total_samples=total,
            control_count=ctrl_count,
            experiment_count=exp_count,
            metric_comparisons=comparisons,
            recommendation=recommendation,
            details=details,
        )

    def _decide_recommendation(
        self, comparisons: dict, exp_count: int
    ) -> str:
        """
        决策建议.

        保守策略：默认 continue_testing。
        """
        if exp_count < self._config.min_samples:
            return "continue_testing"

        has_significant_worse = any(
            v.get("significant") and v.get("direction", "").endswith("worse")
            for v in comparisons.values()
        )
        has_significant_better = any(
            v.get("significant") and v.get("direction", "").endswith("better")
            for v in comparisons.values()
        )

        if has_significant_worse:
            return "rollback"
        elif has_significant_better:
            return "deploy"
        else:
            return "continue_testing"

    @staticmethod
    def _welch_ttest(
        group1: list[float], group2: list[float]
    ) -> tuple[float, float]:
        """
        Welch's t-test（不依赖 scipy）.

        WHY 内置而不是依赖 scipy：
        - scipy 是大依赖（~30MB）
        - 只需要一个 t-test，不值得引入
        - 公式简单，手动实现更轻量
        """
        import math

        n1, n2 = len(group1), len(group2)
        if n1 < 2 or n2 < 2:
            return 0.0, 1.0

        mean1 = sum(group1) / n1
        mean2 = sum(group2) / n2
        var1 = sum((x - mean1) ** 2 for x in group1) / (n1 - 1)
        var2 = sum((x - mean2) ** 2 for x in group2) / (n2 - 1)

        se = math.sqrt(var1 / n1 + var2 / n2)
        if se < 1e-10:
            return 0.0, 1.0

        t_stat = (mean1 - mean2) / se

        # Welch-Satterthwaite 自由度
        num = (var1 / n1 + var2 / n2) ** 2
        denom = (var1 / n1) ** 2 / (n1 - 1) + (var2 / n2) ** 2 / (n2 - 1)
        df = num / denom if denom > 0 else 1

        # 近似 p-value（Student's t 分布 CDF 近似）
        # 使用简化的正态近似（df > 30 时足够准确）
        if df > 30:
            # 标准正态近似
            p_value = 2 * (1 - _normal_cdf(abs(t_stat)))
        else:
            # 对小 df 用更保守的估计
            p_value = 2 * (1 - _normal_cdf(abs(t_stat) * 0.9))

        return float(t_stat), max(0.0, min(1.0, p_value))

    @staticmethod
    def _format_report_details(
        comparisons: dict, ctrl_count: int, exp_count: int
    ) -> str:
        """格式化报告详情."""
        lines = [
            f"对照组样本数: {ctrl_count}",
            f"实验组样本数: {exp_count}",
            "",
        ]
        for metric, data in comparisons.items():
            sig = "✅ 显著" if data["significant"] else "❌ 不显著"
            lines.append(
                f"{metric}: 对照组={data['control_mean']:.4f}, "
                f"实验组={data['experiment_mean']:.4f}, "
                f"p={data['p_value']:.4f} ({sig})"
            )
        return "\n".join(lines)


def _normal_cdf(x: float) -> float:
    """标准正态分布 CDF 近似（Abramowitz & Stegun 公式 7.1.26）."""
    import math

    if x < 0:
        return 1 - _normal_cdf(-x)

    t = 1.0 / (1.0 + 0.2316419 * x)
    d = 0.3989422804014327  # 1/sqrt(2*pi)
    p = d * math.exp(-x * x / 2.0)
    c = (
        t
        * (
            0.319381530
            + t
            * (
                -0.356563782
                + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))
            )
        )
    )
    return 1.0 - p * c
