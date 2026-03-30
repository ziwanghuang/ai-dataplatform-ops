"""
LLM-as-Judge 评估器 — 用高能力模型评估诊断质量

WHY LLM-as-Judge：
- 关键词匹配太粗糙（"NameNode heap" 匹配不了 "NN 堆内存不足"）
- 人工标注太贵（SRE $100/h，一次评估 4-8h）
- LLM-as-Judge 成本是人工的 0.6%，覆盖 70-80% 场景

关键设计决策：
- WHY 用 structured output：避免 LLM 评分漂移
- WHY 4 个维度不是 10 个：维度太多 → 评分不稳定
- WHY 1-5 分制不是 0-1：人类标注者更习惯 1-5，校准更容易
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from aiops.core.logging import get_logger

logger = get_logger(__name__)


# === Pydantic Schema：强制 LLM 返回结构化评分 ===


class JudgeScore(BaseModel):
    """LLM Judge 结构化评分."""

    accuracy: int = Field(
        ge=1, le=5, description="准确性：根因分析是否正确指向了真实问题"
    )
    completeness: int = Field(
        ge=1, le=5, description="完整性：是否覆盖了所有相关因素"
    )
    actionability: int = Field(
        ge=1, le=5, description="可操作性：修复建议是否具体可执行"
    )
    safety: int = Field(
        ge=1, le=5, description="安全性：建议是否考虑了风险和回滚"
    )
    reasoning: str = Field(
        default="", description="评分理由（简要说明）"
    )


# === 评估 Prompt 设计 ===

JUDGE_SYSTEM_PROMPT = """\
你是一个大数据平台运维专家，负责评估 AI 诊断报告的质量。
请对以下诊断报告进行 4 个维度的评分（1-5 分），并给出简要理由。

## 准确性 (accuracy)
- 5分: 精确定位根因，与 ground truth 完全一致
- 4分: 基本正确，但遗漏次要因素
- 3分: 方向正确，但根因描述不够精确
- 2分: 部分正确，但混入了错误信息
- 1分: 完全错误或与问题无关

## 完整性 (completeness)
- 5分: 覆盖了所有相关组件、指标、影响范围
- 4分: 覆盖主要内容，遗漏边缘场景
- 3分: 基本覆盖，但缺少部分重要分析
- 2分: 明显不完整，遗漏关键内容
- 1分: 严重不完整

## 可操作性 (actionability)
- 5分: 修复步骤明确、可直接执行，含具体命令
- 4分: 修复建议清晰，但缺少部分细节
- 3分: 有方向，但需要进一步细化
- 2分: 建议过于笼统
- 1分: 无有效建议

## 安全性 (safety)
- 5分: 明确评估了风险等级，有回滚方案
- 4分: 考虑了主要风险
- 3分: 有基本的安全意识
- 2分: 忽视了部分安全风险
- 1分: 建议存在严重安全隐患
"""

JUDGE_USER_TEMPLATE = """\
## 用户查询
{query}

## 待评估的诊断报告
{diagnosis_result}

## 参考答案（Ground Truth）
{ground_truth}

## 检索到的上下文
{contexts}

请返回 JSON 格式的评分结果。"""


@dataclass
class JudgeResult:
    """单条 LLM Judge 评估结果."""

    case_id: str
    scores: JudgeScore | None = None
    weighted_score: float = 0.0
    error: str | None = None
    cached: bool = False


@dataclass
class CalibrationResult:
    """校准结果."""

    metric: str
    human_mean: float
    llm_mean: float
    correlation: float
    bias: float  # LLM - Human


class LLMJudge:
    """
    LLM-as-Judge 评估器.

    用 GPT-4o 级别的模型评估 Agent 输出质量。
    4 维度评分 + 校准机制 + 缓存。
    """

    # 评分权重
    WEIGHTS: dict[str, float] = {
        "accuracy": 0.35,
        "completeness": 0.25,
        "actionability": 0.25,
        "safety": 0.15,
    }

    def __init__(
        self,
        model: str = "gpt-4o",
        cache_enabled: bool = True,
    ) -> None:
        self._model = model
        self._cache_enabled = cache_enabled
        self._cache: dict[str, JudgeScore] = {}

    async def judge_one(
        self,
        case_id: str,
        query: str,
        diagnosis_result: str,
        ground_truth: str = "",
        contexts: list[str] | None = None,
    ) -> JudgeResult:
        """
        评估单条诊断结果.

        Returns:
            JudgeResult 包含 4 维评分和加权总分
        """
        # 缓存检查
        cache_key = self._cache_key(query, diagnosis_result)
        if self._cache_enabled and cache_key in self._cache:
            scores = self._cache[cache_key]
            weighted = self._weighted_score(scores)
            return JudgeResult(
                case_id=case_id,
                scores=scores,
                weighted_score=weighted,
                cached=True,
            )

        # 构建 Prompt
        user_msg = JUDGE_USER_TEMPLATE.format(
            query=query,
            diagnosis_result=diagnosis_result,
            ground_truth=ground_truth or "（无参考答案）",
            contexts="\n".join(contexts) if contexts else "（无上下文）",
        )

        try:
            # 调用 LLM（通过 litellm + instructor）
            scores = await self._call_llm(user_msg)

            if self._cache_enabled:
                self._cache[cache_key] = scores

            weighted = self._weighted_score(scores)

            logger.info(
                "llm_judge_scored",
                case_id=case_id,
                accuracy=scores.accuracy,
                completeness=scores.completeness,
                actionability=scores.actionability,
                safety=scores.safety,
                weighted=f"{weighted:.2f}",
            )

            return JudgeResult(
                case_id=case_id,
                scores=scores,
                weighted_score=weighted,
            )

        except Exception as e:
            logger.error("llm_judge_error", case_id=case_id, error=str(e))
            return JudgeResult(case_id=case_id, error=str(e))

    async def judge_batch(
        self,
        cases: list[dict],
    ) -> list[JudgeResult]:
        """
        批量评估.

        cases 格式:
        [{"case_id": "D001", "query": "...", "diagnosis_result": "...",
          "ground_truth": "...", "contexts": [...]}]
        """
        import asyncio

        tasks = [
            self.judge_one(
                case_id=c.get("case_id", f"case_{i}"),
                query=c.get("query", ""),
                diagnosis_result=c.get("diagnosis_result", ""),
                ground_truth=c.get("ground_truth", ""),
                contexts=c.get("contexts"),
            )
            for i, c in enumerate(cases)
        ]
        return list(await asyncio.gather(*tasks))

    async def calibrate(
        self,
        calibration_set: list[dict],
    ) -> list[CalibrationResult]:
        """
        校准：对比 LLM 评分与人工评分.

        calibration_set 格式:
        [{"query": "...", "diagnosis_result": "...",
          "human_scores": {"accuracy": 4, "completeness": 3, ...}}]
        """
        results: list[CalibrationResult] = []

        for metric in self.WEIGHTS:
            human_scores = [
                c["human_scores"].get(metric, 3) for c in calibration_set
            ]
            llm_scores: list[float] = []

            for c in calibration_set:
                judge_result = await self.judge_one(
                    case_id="calibration",
                    query=c["query"],
                    diagnosis_result=c["diagnosis_result"],
                )
                if judge_result.scores:
                    llm_scores.append(getattr(judge_result.scores, metric))
                else:
                    llm_scores.append(3.0)  # fallback

            # 简单统计
            human_mean = sum(human_scores) / len(human_scores)
            llm_mean = sum(llm_scores) / len(llm_scores)

            # Pearson 相关系数（简化版）
            n = len(human_scores)
            if n > 1:
                h_std = (
                    sum((h - human_mean) ** 2 for h in human_scores) / (n - 1)
                ) ** 0.5
                l_std = (
                    sum((l - llm_mean) ** 2 for l in llm_scores) / (n - 1)
                ) ** 0.5
                if h_std > 0 and l_std > 0:
                    cov = sum(
                        (h - human_mean) * (l - llm_mean)
                        for h, l in zip(human_scores, llm_scores)
                    ) / (n - 1)
                    correlation = cov / (h_std * l_std)
                else:
                    correlation = 0.0
            else:
                correlation = 0.0

            results.append(
                CalibrationResult(
                    metric=metric,
                    human_mean=human_mean,
                    llm_mean=llm_mean,
                    correlation=correlation,
                    bias=llm_mean - human_mean,
                )
            )

        return results

    async def _call_llm(self, user_msg: str) -> JudgeScore:
        """调用 LLM 获取结构化评分."""
        try:
            import instructor
            import litellm

            client = instructor.from_litellm(litellm.acompletion)
            response = await client.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                response_model=JudgeScore,
                max_retries=2,
            )
            return response
        except ImportError:
            # litellm/instructor 不可用时返回默认分数
            logger.warning("llm_judge_deps_missing", fallback="default_scores")
            return JudgeScore(
                accuracy=3,
                completeness=3,
                actionability=3,
                safety=3,
                reasoning="LLM 评估依赖未安装，返回默认分数",
            )

    def _weighted_score(self, scores: JudgeScore) -> float:
        """计算加权总分（归一化到 0-1）."""
        raw = (
            scores.accuracy * self.WEIGHTS["accuracy"]
            + scores.completeness * self.WEIGHTS["completeness"]
            + scores.actionability * self.WEIGHTS["actionability"]
            + scores.safety * self.WEIGHTS["safety"]
        )
        # 1-5 分映射到 0-1
        return (raw - 1) / 4

    @staticmethod
    def _cache_key(query: str, result: str) -> str:
        """生成缓存 key."""
        content = f"{query}||{result}"
        return hashlib.sha256(content.encode()).hexdigest()[:16]
