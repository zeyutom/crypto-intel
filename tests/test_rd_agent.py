"""Unit tests for src.research.rd_agent — 假设/实验/评估/反馈 流水线."""
import sys
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.research import rd_agent as rd


# ────────────────────────────────────────────────────────────────────
#  Hypothesis 生成
# ────────────────────────────────────────────────────────────────────

def test_seed_factors_from_alpha_discovery():
    seeds = rd._seed_factors()
    assert len(seeds) >= 10
    # 应包含已知 alpha_discovery 变量
    assert "momentum_30d" in seeds or "funding_rate" in seeds


def test_mutation_expressions_are_balanced():
    """变异生成的表达式必须括号闭合."""
    hyps = rd._propose_via_mutation(["momentum_30d", "funding_rate", "tvl_mcap"], n=10)
    assert len(hyps) == 10
    for h in hyps:
        assert h.expression.count("(") == h.expression.count(")"), \
            f"unbalanced: {h.expression}"


def test_mutation_uses_alpha_discovery_compatible_ops():
    """变异公式应只用 alpha_discovery.SAFE_FUNCTIONS 兼容的算子."""
    hyps = rd._propose_via_mutation(["momentum_30d", "funding_rate"], n=20)
    allowed = {"abs", "log", "sqrt", "sigmoid", "sign", "min", "max", "pow", "clip"}
    for h in hyps:
        # 提取所有函数名
        import re
        funcs = set(re.findall(r"([a-z_]+)\(", h.expression))
        unknown = funcs - allowed
        assert not unknown, f"unknown ops in {h.expression}: {unknown}"


# ────────────────────────────────────────────────────────────────────
#  Evaluation agent: Bonferroni 校正
# ────────────────────────────────────────────────────────────────────

def test_evaluation_bonferroni_filters_weak_at_high_n():
    """50 个候选, IC=0.03 太弱 → 全 0 promoted."""
    exps = [
        rd.Experiment(hypothesis_name=f"h{i}", ok=True,
                      ic_mean=0.03, ic_ir=0.4, n_obs=60)
        for i in range(50)
    ]
    verdicts = rd.evaluation_agent(exps)
    promoted = sum(1 for v in verdicts if v.promote)
    assert promoted == 0


def test_evaluation_promotes_strong_signal():
    """单候选 + 强 IC: N=1 Bonferroni z=1.96, IC@n=60 ≈ 0.253 阈值;
    需要 IC ≥ 0.30 才稳过."""
    exps = [rd.Experiment(hypothesis_name="h0", ok=True,
                          ic_mean=0.30, ic_ir=0.8, n_obs=60)]
    verdicts = rd.evaluation_agent(exps)
    assert verdicts[0].promote is True, (
        f"strong IC=0.30 should pass Bonferroni N=1, got {verdicts[0]}"
    )


def test_evaluation_rejects_negative_ir():
    """IC 看似强但 IR 为负 → 不 promote (方向不一致)."""
    exps = [rd.Experiment(hypothesis_name="h0", ok=True,
                          ic_mean=0.20, ic_ir=-0.1, n_obs=60)]
    verdicts = rd.evaluation_agent(exps)
    assert verdicts[0].promote is False


def test_evaluation_rejects_small_sample():
    """n<30 → 不 promote."""
    exps = [rd.Experiment(hypothesis_name="h0", ok=True,
                          ic_mean=0.20, ic_ir=0.5, n_obs=20)]
    verdicts = rd.evaluation_agent(exps)
    assert verdicts[0].promote is False


def test_evaluation_failed_experiment_not_promoted():
    """ok=False 的实验直接跳过."""
    exps = [rd.Experiment(hypothesis_name="h0", ok=False, error="x")]
    verdicts = rd.evaluation_agent(exps)
    assert verdicts[0].promote is False
    assert verdicts[0].score == 0.0


def test_evaluation_notes_contain_bonferroni_info():
    exps = [rd.Experiment(hypothesis_name="h0", ok=True,
                          ic_mean=0.1, ic_ir=0.5, n_obs=60)]
    verdicts = rd.evaluation_agent(exps)
    assert "Bonferroni" in verdicts[0].notes
    assert "N=1" in verdicts[0].notes   # 单个候选


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
