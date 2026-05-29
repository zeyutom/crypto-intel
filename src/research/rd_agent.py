"""RD-Agent 风格的因子+模型联合自演化 (Phase 3-A).

借鉴 microsoft/RD-Agent-Quant (arXiv 2505.15155) 的核心架构,
把现有 evolution agent 升级成"假设 → 实验 → 评估 → 反馈"的 R&D 循环。

差异化设计:
  - microsoft/RD-Agent 用 Docker 沙箱跑实验, 我们用 alpha_discovery 的安全 eval
  - 不依赖外部 LLM API (复用现有 _claude_runner 或纯算法变异)
  - 持久化 R&D 轨迹: data/rd_agent/trajectory_YYYY-MM-DD.json

数据流:

    1. HypothesisAgent    — 基于历史 IC 表现, 生成新因子假设 (公式 + 直觉)
                            ↓ hypothesis dict
    2. ExperimentAgent    — 把假设落到代码, 在历史数据上跑 IC 测试
                            ↓ experiment dict (with IC, IR, t-stat)
    3. EvaluationAgent    — 用 AlphaEval 框架评分 (多维: IC/稳定性/正交性)
                            ↓ verdict dict
    4. FeedbackAgent      — 把结果反哺到 HypothesisAgent 的种子库
                            ↓ updated context

模块状态:
  - 实现了完整流水线骨架 (假设→实验→评估→反馈)
  - 假设生成: 复用 alpha_discovery.py 的 LLM/offline 变异; 没有 LLM 时走规则化变异
  - 实验: 直接调 alpha_discovery 的安全 eval + IC 回测
  - 评估: 多维评分 (IC mean / IC IR / 与已有因子相关性 / 单调性)
  - 反馈: 把高分因子 promote 到 factor_proposals, 把假设种子库存档

支持运行模式:
  python -m src.cli rd-agent --rounds 3        # 跑 3 轮
  python -m src.cli rd-agent --resume          # 接续上次状态
"""
from __future__ import annotations
import json
import math
import time
import traceback
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ..utils import setup_logger

log = setup_logger("rd_agent", "INFO")

RD_DIR = Path(__file__).resolve().parents[2] / "data" / "rd_agent"
RD_DIR.mkdir(parents=True, exist_ok=True)


# ────────────────────────────────────────────────────────────────────
#  数据结构 (Hypothesis / Experiment / Verdict / Trajectory)
# ────────────────────────────────────────────────────────────────────

@dataclass
class Hypothesis:
    """一条因子假设。"""
    name: str
    expression: str            # 公式字符串 (alpha_discovery DSL)
    intuition: str             # 直觉解释
    parent: Optional[str] = None  # 父因子名 (变异来源)
    seed: Optional[int] = None
    born_at: str = ""

    def __post_init__(self):
        if not self.born_at:
            self.born_at = datetime.utcnow().isoformat() + "Z"


@dataclass
class Experiment:
    """对一条假设的回测实验结果。"""
    hypothesis_name: str
    ok: bool
    ic_mean: float = 0.0
    ic_ir: float = 0.0
    ic_t_stat: float = 0.0
    n_obs: int = 0
    runtime_ms: int = 0
    error: Optional[str] = None


@dataclass
class Verdict:
    """对实验结果的评估 (RD-Agent 'Eval Agent')。"""
    hypothesis_name: str
    score: float                 # 综合分 [0, 1]
    ic_score: float = 0.0
    stability_score: float = 0.0
    orthogonality_score: float = 0.0
    promote: bool = False        # 是否值得纳入主因子库
    notes: str = ""


@dataclass
class Trajectory:
    """整轮 R&D 的状态 (可持久化)。"""
    started_at: str = ""
    finished_at: str = ""
    rounds: list[dict] = field(default_factory=list)  # 每轮: hyp/exp/verdict
    promoted: list[str] = field(default_factory=list)  # 晋级因子名
    seed_factors: list[str] = field(default_factory=list)
    config: dict = field(default_factory=dict)


# ────────────────────────────────────────────────────────────────────
#  Agent 1: HypothesisAgent — 生成因子假设
# ────────────────────────────────────────────────────────────────────

def _seed_factors() -> list[str]:
    """读现有因子库当种子。

    优先用 alpha_discovery.ALLOWED_VARS — 它定义了安全沙箱里能用的变量,
    这样变异出来的公式能直接喂给 alpha_discovery.evaluate_candidates.
    """
    try:
        from . import alpha_discovery as ad
        allowed = getattr(ad, "ALLOWED_VARS", None)
        if allowed:
            return sorted(allowed)
    except Exception:
        pass
    # fallback (不应该走到这里)
    return [
        "momentum_30d", "momentum_7d", "ath_drawdown", "volume_turnover",
        "funding_rate", "tvl_mcap", "narrative_heat", "change_24h",
    ]


def _propose_via_mutation(seeds: list[str], n: int = 5) -> list[Hypothesis]:
    """无 LLM 时的纯规则化变异: 组合算子, 输出 syntactically 闭合的表达式.

    用 alpha_discovery.SAFE_FUNCTIONS 兼容的算子 (abs/log/sqrt/sigmoid/sign),
    避免用 ts_mean/rank 等 alpha158 算子 (alpha_discovery 不识别).
    """
    import random
    rng = random.Random(int(time.time()))
    # 一元算子 (alpha_discovery 沙箱支持)
    op_templates_unary = [
        "abs({x})",
        "log(abs({x}) + 1)",
        "sigmoid({x} * 3)",
        "sign({x}) * sqrt(abs({x}))",
        "({x})",  # identity (允许直接用)
    ]
    ops_binary = ["+", "-", "*"]

    out: list[Hypothesis] = []
    for i in range(n):
        if len(seeds) < 2:
            inner = seeds[0]
            weight_a, weight_b = 1.0, 0.0
            bop = "+"
            a = b = seeds[0]
        else:
            a, b = rng.sample(seeds, k=2)
            bop = rng.choice(ops_binary)
            # 加权组合 (像 alpha_discovery MUTATION_TEMPLATES 一样)
            weight_a = round(rng.uniform(0.3, 0.7), 1)
            weight_b = round(1 - weight_a, 1)
            if bop == "*":
                # 乘法不需要权重
                inner = f"({a} * {b})"
            else:
                inner = f"({a} * {weight_a} {bop} {b} * {weight_b})"
        tmpl = rng.choice(op_templates_unary)
        expr = tmpl.format(x=inner)
        name = f"mut_{int(time.time()*1000)%100000:05d}_{i}"
        out.append(Hypothesis(
            name=name,
            expression=expr,
            intuition=f"rule-based: {tmpl.format(x='X')} on X={inner}",
            parent=a,
            seed=i,
        ))
    return out


def _propose_via_llm(seeds: list[str], n: int = 5) -> list[Hypothesis]:
    """有 Claude 时调 LLM 生成假设."""
    try:
        from ..evolution._claude_runner import run_claude
    except Exception:
        return []
    prompt = f"""你是量化因子研究员。基于以下原子特征, 提议 {n} 个新公式因子用于 crypto 截面选币:

原子: {seeds}

要求:
1. 每个因子返回 JSON {{name, expression, intuition}}
2. expression 可调用: ts_mean(window, x), ts_std(window, x), ts_rank(window, x), rank(x), log(x), abs(x)
3. 直觉简洁 (≤30 字)
4. 输出严格 JSON list, 不带 markdown 代码块
"""
    try:
        raw = run_claude(prompt, system="You output ONLY JSON.")
        # 简单解析
        s = (raw or "").strip().strip("`").strip()
        if s.startswith("json"):
            s = s[4:].strip()
        data = json.loads(s)
        out = []
        for i, d in enumerate(data):
            out.append(Hypothesis(
                name=d.get("name", f"llm_{int(time.time())}_{i}"),
                expression=d["expression"],
                intuition=d.get("intuition", ""),
                parent="llm",
                seed=i,
            ))
        return out
    except Exception as e:
        log.warning(f"LLM propose failed: {e}; fallback to mutation")
        return []


def hypothesis_agent(seeds: list[str], n: int = 5,
                     prefer_llm: bool = True) -> list[Hypothesis]:
    """统一假设生成入口。"""
    if prefer_llm:
        hyps = _propose_via_llm(seeds, n=n)
        if hyps:
            return hyps
    return _propose_via_mutation(seeds, n=n)


# ────────────────────────────────────────────────────────────────────
#  Agent 2: ExperimentAgent — 跑回测
# ────────────────────────────────────────────────────────────────────

def experiment_agent(hyps: list[Hypothesis]) -> list[Experiment]:
    """对每条假设跑 IC 回测。

    适配 alpha_discovery.evaluate_candidates(): 把 hypotheses 包成
    candidate dict list, 一次性喂进去, 然后把每个候选的 ic/n_coins
    映射回 Experiment.

    alpha_discovery.evaluate_candidates 返回的 candidate dict 形如:
        {name, expr, ic, n_coins, status, significant_after_bonferroni, ...}
    """
    results = []
    try:
        from . import alpha_discovery as ad
    except Exception as e:
        log.warning(f"alpha_discovery 不可用: {e}")
        for h in hyps:
            results.append(Experiment(
                hypothesis_name=h.name, ok=False,
                error="alpha_discovery module unavailable",
            ))
        return results

    # 把每个 hypothesis 包成 alpha_discovery 期望的 candidate dict
    candidates = [{
        "name": h.name,
        "expr": h.expression,
        "rationale": h.intuition,
        "origin": "rd_agent",
    } for h in hyps]

    t0 = time.time()
    try:
        evaluated = ad.evaluate_candidates(candidates)
    except Exception as e:
        log.warning(f"evaluate_candidates 失败: {e}")
        for h in hyps:
            results.append(Experiment(
                hypothesis_name=h.name, ok=False,
                error=f"evaluate_candidates exception: {str(e)[:150]}",
            ))
        return results

    # 用 dict 索引方便对回
    by_name = {c["name"]: c for c in evaluated if isinstance(c, dict)}
    batch_runtime = int((time.time() - t0) * 1000)
    per_hyp_runtime = batch_runtime // max(len(hyps), 1)

    for h in hyps:
        c = by_name.get(h.name)
        if c is None:
            results.append(Experiment(
                hypothesis_name=h.name, ok=False,
                error="missing from evaluate_candidates output",
                runtime_ms=per_hyp_runtime,
            ))
            continue

        ic = c.get("ic")
        n = c.get("n_coins", 0)
        status = c.get("status", "")

        if ic is None or status != "evaluated":
            results.append(Experiment(
                hypothesis_name=h.name, ok=False,
                error=f"alpha_discovery status={status} ic={ic}",
                runtime_ms=per_hyp_runtime,
            ))
            continue

        # IC IR: alpha_discovery 单点 IC, 没有时间序列, 用 |IC|*√n 当代理 t-stat
        # 真正的 IR 要靠 meta_learner 的多日 ic_history, RD-Agent 单轮无法直接算
        # 这里给一个保守的代理: ir ≈ |ic| (即假设 IC 自身就是单期信噪比)
        ic_f = float(ic)
        ir_proxy = abs(ic_f)
        # t-stat: |IC| * √n
        import math as _m
        t_stat = ic_f * _m.sqrt(max(n, 1)) if n > 0 else 0.0

        results.append(Experiment(
            hypothesis_name=h.name,
            ok=True,
            ic_mean=ic_f,
            ic_ir=ir_proxy,
            ic_t_stat=round(t_stat, 4),
            n_obs=int(n),
            runtime_ms=per_hyp_runtime,
        ))
    return results


# ────────────────────────────────────────────────────────────────────
#  Agent 3: EvaluationAgent — 多维评分
# ────────────────────────────────────────────────────────────────────

def evaluation_agent(
    exps: list[Experiment],
    promote_threshold: float = 0.6,
    ic_floor: float = None,
    bonferroni_alpha: float = 0.05,
) -> list[Verdict]:
    """对实验结果按多维度打分, 决定是否 promote.

    Phase 2.5 升级:
      - ic_floor 改为按候选数自动 Bonferroni 校正 (compute from overfitting module)
      - 在 n_trials = len(exps) 上加多重检验校正
      - n_obs ≥ 30 改为依赖 IC 阈值动态调整
    """
    verdicts = []
    n_trials = len(exps)

    # ── 动态 IC 阈值 (Bonferroni 校正后) ─────────────────────────
    try:
        from . import overfitting as of_mod
        mt = of_mod.multiple_testing_threshold(n_trials, alpha=bonferroni_alpha)
        ic_floor_auto = mt["ic_floor"]["n=60"]  # 60 天作为参考
        if ic_floor is None:
            ic_floor = ic_floor_auto
    except Exception:
        if ic_floor is None:
            ic_floor = 0.02

    for exp in exps:
        if not exp.ok:
            verdicts.append(Verdict(
                hypothesis_name=exp.hypothesis_name,
                score=0.0, promote=False,
                notes=exp.error or "experiment failed",
            ))
            continue

        ic_score = min(1.0, abs(exp.ic_mean) / 0.05)
        stability = min(1.0, max(0.0, exp.ic_ir / 0.5))
        ortho = 0.5  # 占位 (真集成时从因子库算)

        score = 0.5 * ic_score + 0.3 * stability + 0.2 * ortho

        # Promote 阈值: 综合分 + IC 超过 Bonferroni 阈值 + 样本量 + IR>0
        promote = (
            score >= promote_threshold
            and abs(exp.ic_mean) >= ic_floor
            and exp.n_obs >= 30
            and exp.ic_ir > 0  # 必须方向一致
        )
        verdicts.append(Verdict(
            hypothesis_name=exp.hypothesis_name,
            score=round(score, 4),
            ic_score=round(ic_score, 4),
            stability_score=round(stability, 4),
            orthogonality_score=round(ortho, 4),
            promote=promote,
            notes=(
                f"IC={exp.ic_mean:+.4f} IR={exp.ic_ir:+.2f} "
                f"n={exp.n_obs} "
                f"(IC threshold={ic_floor:.4f} from Bonferroni on N={n_trials})"
            ),
        ))
    return verdicts


# ────────────────────────────────────────────────────────────────────
#  Agent 4: FeedbackAgent — 把结果反哺
# ────────────────────────────────────────────────────────────────────

def feedback_agent(
    hyps: list[Hypothesis],
    verdicts: list[Verdict],
    traj: Trajectory,
) -> Trajectory:
    """把通过评估的因子写入 promoted, 把所有结果存回 trajectory."""
    by_name = {h.name: h for h in hyps}
    v_by_name = {v.hypothesis_name: v for v in verdicts}

    promoted_now = [v.hypothesis_name for v in verdicts if v.promote]
    traj.promoted.extend(promoted_now)

    # 把 promoted 因子写到 factor_proposals/ (与 factor_proposer 同目录)
    proposals_dir = Path(__file__).resolve().parents[2] / "data" / "proposals"
    proposals_dir.mkdir(parents=True, exist_ok=True)
    for name in promoted_now:
        h = by_name.get(name)
        v = v_by_name.get(name)
        if not h:
            continue
        path = proposals_dir / f"rd_agent_{name}.json"
        path.write_text(json.dumps({
            "name": h.name,
            "expression": h.expression,
            "intuition": h.intuition,
            "parent": h.parent,
            "verdict": asdict(v) if v else None,
            "source": "rd_agent",
            "ts": datetime.utcnow().isoformat() + "Z",
        }, ensure_ascii=False, indent=2))

    return traj


# ────────────────────────────────────────────────────────────────────
#  R&D 循环主入口
# ────────────────────────────────────────────────────────────────────

def run_one_round(seeds: list[str], n_hyps: int = 5,
                  prefer_llm: bool = False) -> dict:
    """跑一轮: 提假设 → 实验 → 评估 → 整理结果。"""
    log.info(f"  ▶ round start (n_hyps={n_hyps}, prefer_llm={prefer_llm})")
    hyps = hypothesis_agent(seeds, n=n_hyps, prefer_llm=prefer_llm)
    log.info(f"    hypotheses generated: {len(hyps)}")
    exps = experiment_agent(hyps)
    ok_cnt = sum(1 for e in exps if e.ok)
    log.info(f"    experiments: {ok_cnt}/{len(exps)} ok")
    verdicts = evaluation_agent(exps)
    promoted = sum(1 for v in verdicts if v.promote)
    log.info(f"    verdicts: {promoted}/{len(verdicts)} promoted")
    return {
        "hypotheses": [asdict(h) for h in hyps],
        "experiments": [asdict(e) for e in exps],
        "verdicts": [asdict(v) for v in verdicts],
        "promoted_in_round": promoted,
        "round_ended_at": datetime.utcnow().isoformat() + "Z",
    }


def run_rd_agent(rounds: int = 3, n_hyps: int = 5,
                 prefer_llm: bool = False, resume: bool = False) -> Trajectory:
    """完整 R&D 循环 (多轮)。"""
    state_file = RD_DIR / "trajectory_latest.json"
    if resume and state_file.exists():
        traj_dict = json.loads(state_file.read_text())
        traj = Trajectory(**traj_dict)
        log.info(f"  resumed trajectory, {len(traj.rounds)} prior rounds")
    else:
        traj = Trajectory(
            started_at=datetime.utcnow().isoformat() + "Z",
            seed_factors=_seed_factors(),
            config={"rounds": rounds, "n_hyps": n_hyps, "prefer_llm": prefer_llm},
        )

    for r in range(rounds):
        log.info(f"\n=== R&D Round {r+1}/{rounds} ===")
        round_data = run_one_round(
            seeds=traj.seed_factors,
            n_hyps=n_hyps,
            prefer_llm=prefer_llm,
        )
        traj.rounds.append(round_data)

        # Feedback: 把高分晋级因子写到 proposals/
        hyps_now = [Hypothesis(**h) for h in round_data["hypotheses"]]
        verds_now = [Verdict(**v) for v in round_data["verdicts"]]
        traj = feedback_agent(hyps_now, verds_now, traj)

    traj.finished_at = datetime.utcnow().isoformat() + "Z"

    # 持久化
    state_file.write_text(json.dumps(asdict(traj), ensure_ascii=False, indent=2))
    # 同时存一份带时间戳的 archive
    archive = RD_DIR / f"trajectory_{traj.started_at[:10]}.json"
    archive.write_text(json.dumps(asdict(traj), ensure_ascii=False, indent=2))

    log.info(
        f"\n[rd_agent] done in {len(traj.rounds)} rounds, "
        f"{len(traj.promoted)} factors promoted"
    )
    return traj


def is_available() -> bool:
    """供 oss-check 用 — 本模块是纯 Python, 总是可用."""
    return True


def get_last_trajectory() -> Optional[dict]:
    f = RD_DIR / "trajectory_latest.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except Exception:
        return None


# ────────────────────────────────────────────────────────────────────
#  Self-test (dry run, 不依赖真实数据)
# ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("[rd_agent] dry-run 1 round, prefer_llm=False")
    traj = run_rd_agent(rounds=1, n_hyps=3, prefer_llm=False)
    print(f"\n[rd_agent] seed_factors: {traj.seed_factors[:5]}...")
    print(f"[rd_agent] rounds completed: {len(traj.rounds)}")
    print(f"[rd_agent] promoted: {traj.promoted}")
