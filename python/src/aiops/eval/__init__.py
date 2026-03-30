"""
评估体系 — 离线评估 / 质量门禁 / LLM-as-Judge / A/B 测试 / 持续评估

模块清单:
- runner: OfflineEvaluator 离线评估运行器
- gate_check: 质量门禁 (CI 集成)
- llm_judge: LLM-as-Judge 4 维评分
- ragas_eval: RAGAS RAG 质量评估
- auto_discovery: BadCase 自动发现
- deepeval_eval: DeepEval GEval 集成
- ab_test: A/B 测试框架
- continuous_eval: 持续评估流水线
"""

from aiops.eval.runner import OfflineEvaluator
from aiops.eval.gate_check import check_gates, load_gate_config
from aiops.eval.llm_judge import LLMJudge, JudgeScore
from aiops.eval.ragas_eval import RAGASEvaluator, RAGEvalSample, RAGEvalReport
from aiops.eval.auto_discovery import BadCaseAutoDiscovery, DetectionSignal
from aiops.eval.deepeval_eval import DeepEvalRunner
from aiops.eval.ab_test import ABTestRunner, ABTestConfig, ABTestReport
from aiops.eval.continuous_eval import ContinuousEvaluator

__all__ = [
    # runner
    "OfflineEvaluator",
    # gate_check
    "check_gates",
    "load_gate_config",
    # llm_judge
    "LLMJudge",
    "JudgeScore",
    # ragas_eval
    "RAGASEvaluator",
    "RAGEvalSample",
    "RAGEvalReport",
    # auto_discovery
    "BadCaseAutoDiscovery",
    "DetectionSignal",
    # deepeval
    "DeepEvalRunner",
    # ab_test
    "ABTestRunner",
    "ABTestConfig",
    "ABTestReport",
    # continuous_eval
    "ContinuousEvaluator",
]
