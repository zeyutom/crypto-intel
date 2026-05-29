"""LangGraph Evolution DAG (Phase 2-B).

把 evolution/ 的 5 个 script 编排成可观测、可重试、可 checkpoint 的有向图:

    [START]
       │
       ▼
    source_discoverer  ─── 新数据源候选, 写 data/source_proposals/
       │
       ▼
    factor_proposer  ─── 基于 IC/IR 表现提新因子, 写 data/proposals/
       │
       ▼
    narrative_tracker  ─── 抓新叙事 (从 KOL/Reddit/Trending)
       │
       ▼
    prompt_evolver  ─── 根据反馈微调 system prompts
       │
       ▼
    weekly_review  ─── 综合本周表现 + 写 review markdown
       │
       ▼
     [END]

设计:
  - 优先用 langgraph (LangGraph 框架, 真正 DAG, 支持 checkpoint/streaming)
  - 没装 langgraph → 降级为顺序调用 (sequential fallback)
  - 每个 node 是 idempotent: 失败可重试; 状态写到 data/evolution_state.json
  - 输出聚合到 EvolutionState (TypedDict)
"""
from __future__ import annotations
import json
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, TypedDict

from ..utils import setup_logger

log = setup_logger("evolution_graph", "INFO")

STATE_FILE = Path(__file__).resolve().parents[2] / "data" / "evolution_state.json"
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)


def _langgraph_available() -> bool:
    try:
        import langgraph  # noqa
        return True
    except ImportError:
        return False


def is_available() -> bool:
    return _langgraph_available()


# ── 共享状态 (TypedDict 同时被 LangGraph 和 fallback 用) ─────────────

class EvolutionState(TypedDict, total=False):
    """跨 node 传递的累积状态。"""
    run_id: str
    started_at: str
    finished_at: str
    sources_proposed: list[dict]
    factors_proposed: list[dict]
    narratives_tracked: list[dict]
    prompts_evolved: list[dict]
    weekly_review_path: Optional[str]
    errors: list[dict]
    nodes_ok: list[str]
    nodes_failed: list[str]


def _new_state() -> EvolutionState:
    return EvolutionState(
        run_id=datetime.utcnow().strftime("%Y%m%d_%H%M%S"),
        started_at=datetime.utcnow().isoformat() + "Z",
        finished_at="",
        sources_proposed=[],
        factors_proposed=[],
        narratives_tracked=[],
        prompts_evolved=[],
        weekly_review_path=None,
        errors=[],
        nodes_ok=[],
        nodes_failed=[],
    )


# ── 每个 node 都是一个无副作用函数 (state → state) ──────────────────

def _safe_call(node_name: str, fn, state: EvolutionState) -> EvolutionState:
    """包一层 try/except, 任何 node 失败不阻塞 graph。"""
    t0 = time.time()
    log.info(f"  ▶ {node_name} ...")
    try:
        fn()
        state["nodes_ok"].append(node_name)
        log.info(f"    ✓ {node_name} ({time.time()-t0:.1f}s)")
    except Exception as e:
        log.warning(f"    ✗ {node_name}: {e}")
        state["nodes_failed"].append(node_name)
        state["errors"].append({
            "node": node_name,
            "error": str(e),
            "traceback": traceback.format_exc(),
        })
    return state


def node_source_discover(state: EvolutionState) -> EvolutionState:
    def _do():
        from . import source_discoverer
        # source_discoverer.main() 没有标准签名, 走个轻量探测
        if hasattr(source_discoverer, "discover_sources"):
            out = source_discoverer.discover_sources()  # type: ignore[attr-defined]
            if isinstance(out, list):
                state["sources_proposed"] = out[:50]
        elif hasattr(source_discoverer, "main"):
            source_discoverer.main()  # type: ignore[attr-defined]
    return _safe_call("source_discover", _do, state)


def node_factor_propose(state: EvolutionState) -> EvolutionState:
    def _do():
        from . import factor_proposer
        if hasattr(factor_proposer, "propose_factors"):
            out = factor_proposer.propose_factors()  # type: ignore[attr-defined]
            if isinstance(out, list):
                state["factors_proposed"] = out[:20]
        elif hasattr(factor_proposer, "main"):
            factor_proposer.main()  # type: ignore[attr-defined]
    return _safe_call("factor_propose", _do, state)


def node_narrative_track(state: EvolutionState) -> EvolutionState:
    def _do():
        from . import narrative_tracker
        if hasattr(narrative_tracker, "track_narratives"):
            out = narrative_tracker.track_narratives()  # type: ignore[attr-defined]
            if isinstance(out, list):
                state["narratives_tracked"] = out[:30]
        elif hasattr(narrative_tracker, "main"):
            narrative_tracker.main()  # type: ignore[attr-defined]
    return _safe_call("narrative_track", _do, state)


def node_prompt_evolve(state: EvolutionState) -> EvolutionState:
    def _do():
        from . import prompt_evolver
        if hasattr(prompt_evolver, "evolve_prompts"):
            out = prompt_evolver.evolve_prompts()  # type: ignore[attr-defined]
            if isinstance(out, list):
                state["prompts_evolved"] = out[:10]
        elif hasattr(prompt_evolver, "main"):
            prompt_evolver.main()  # type: ignore[attr-defined]
    return _safe_call("prompt_evolve", _do, state)


def node_weekly_review(state: EvolutionState) -> EvolutionState:
    def _do():
        from . import weekly_review
        if hasattr(weekly_review, "run_weekly_review"):
            path = weekly_review.run_weekly_review()  # type: ignore[attr-defined]
            if path:
                state["weekly_review_path"] = str(path)
        elif hasattr(weekly_review, "main"):
            weekly_review.main()  # type: ignore[attr-defined]
    return _safe_call("weekly_review", _do, state)


# ── 顺序 fallback (没装 langgraph 时走这条) ─────────────────────────

def _run_sequential(state: EvolutionState) -> EvolutionState:
    log.info("[evolution_graph] running sequential fallback (no langgraph)")
    state = node_source_discover(state)
    state = node_factor_propose(state)
    state = node_narrative_track(state)
    state = node_prompt_evolve(state)
    state = node_weekly_review(state)
    return state


# ── LangGraph 主路径 ───────────────────────────────────────────────

def _build_langgraph():
    """构建 StateGraph (仅在 langgraph 已装时调用)."""
    from langgraph.graph import StateGraph, START, END

    g = StateGraph(EvolutionState)
    g.add_node("source_discover", node_source_discover)
    g.add_node("factor_propose", node_factor_propose)
    g.add_node("narrative_track", node_narrative_track)
    g.add_node("prompt_evolve", node_prompt_evolve)
    g.add_node("weekly_review", node_weekly_review)

    g.add_edge(START, "source_discover")
    g.add_edge("source_discover", "factor_propose")
    g.add_edge("factor_propose", "narrative_track")
    g.add_edge("narrative_track", "prompt_evolve")
    g.add_edge("prompt_evolve", "weekly_review")
    g.add_edge("weekly_review", END)

    return g.compile()


def _run_langgraph(state: EvolutionState) -> EvolutionState:
    log.info("[evolution_graph] running LangGraph DAG")
    app = _build_langgraph()
    final = app.invoke(state)
    return final


# ── 对外入口 ────────────────────────────────────────────────────────

def run_evolution(
    use_langgraph: Optional[bool] = None,
    persist: bool = True,
) -> EvolutionState:
    """跑一轮完整的 evolution。

    Args:
        use_langgraph: True/False/None (None = 自动检测)
        persist: 是否写 data/evolution_state.json

    Returns: 最终 EvolutionState dict
    """
    if use_langgraph is None:
        use_langgraph = _langgraph_available()

    state = _new_state()

    if use_langgraph and _langgraph_available():
        state = _run_langgraph(state)
    else:
        state = _run_sequential(state)

    state["finished_at"] = datetime.utcnow().isoformat() + "Z"

    if persist:
        try:
            STATE_FILE.write_text(
                json.dumps(state, ensure_ascii=False, indent=2, default=str)
            )
            log.info(f"  state persisted to {STATE_FILE}")
        except Exception as e:
            log.warning(f"  persist failed: {e}")

    return state


def get_last_state() -> Optional[dict]:
    """读取最近一次 evolution 状态 (供 dashboard / CLI 展示)."""
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return None


# ── Self-test ──────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"[evolution_graph] langgraph available: {_langgraph_available()}")
    print("--- dry run sequential fallback (会真实调每个 evolution module) ---")
    # 注意: 真实跑会触发 Claude CLI; 这里仅做架构 dry run
    # 仅打印 graph 拓扑
    print("Graph topology:")
    print("  START → source_discover → factor_propose → narrative_track "
          "→ prompt_evolve → weekly_review → END")
    print("\nLast persisted state:")
    print(json.dumps(get_last_state() or {}, indent=2, default=str)[:500])
