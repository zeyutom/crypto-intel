"""Swarm Decision Layer — 多 Agent 投票/辩论式决策。

灵感:
  - TradingAgents (TauricResearch): 7 角色 Agent 协作框架
  - Vibe-Trading (HKUDS): Swarm preset + observable decision trace
  - OctoBot-AI: Analyst → Strategy → Trading 三层 Agent

架构:
  4 个 Specialist Agent 各自对 Top-N 代币打分 + 给出 rationale:
    1. FundamentalAgent — TVL 增长、开发活跃度、协议收入
    2. MomentumAgent   — 价格动量、成交量趋势、ATH 距离
    3. SentimentAgent  — NLP 情绪、Trending 热度、叙事评分
    4. RiskAgent       — 波动率、回撤、板块集中度、资金费率极端

  Ensemble 方法:
    A) 加权投票 (默认): 各 Agent 权重可配、可被元学习动态调整
    B) LLM 辩论 (可选): 把 4 Agent 分析喂给 Claude, 做最终 Jury

  输出:
    - swarm_consensus: dict[symbol → {score, agents_agree, rationale}]
    - decision_trace: 完整决策路径 (每个 Agent 的打分 + 原因)

设计原则:
  - 纯 Python, 不依赖 LangGraph (避免重依赖)
  - 每个 Agent 是一个函数, 输入 scored_coins, 输出 agent_scores
  - LLM 辩论是可选增强层, 没有时退化到纯权重投票
  - decision_trace 全程可追溯 (调试 + 信任)
"""
from __future__ import annotations
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from ..utils import setup_logger

log = setup_logger("swarm_decision", "INFO")

# Agent 默认权重 (可被元学习覆盖)
DEFAULT_AGENT_WEIGHTS = {
    "fundamental": 0.30,
    "momentum": 0.25,
    "sentiment": 0.20,
    "risk": 0.25,
}

META_DIR = Path(__file__).resolve().parents[2] / "data" / "meta"


# ====================================================================
#  Agent 1: Fundamental (TVL / Dev / Protocol Revenue)
# ====================================================================

def fundamental_agent(coins: list[dict]) -> dict[str, dict]:
    """基本面 Agent: 关注 TVL 增长、开发活跃度、市值合理性。

    输出: {symbol: {score: 0-1, signals: [str], conviction: str}}
    """
    results = {}
    for coin in coins:
        sym = coin.get("symbol", "")
        signals = []
        score = 0.5  # 中性起点

        # TVL/MCap ratio
        tvl_mcap = coin.get("f_tvl_mcap", 0) or 0
        if tvl_mcap > 0.7:
            score += 0.15
            signals.append(f"TVL/MCap 优秀 ({tvl_mcap:.2f})")
        elif tvl_mcap < 0.2:
            score -= 0.1
            signals.append(f"TVL/MCap 偏低 ({tvl_mcap:.2f})")

        # 开发活跃度
        dev = coin.get("f_dev_activity", 0) or 0
        if dev > 0.7:
            score += 0.15
            signals.append(f"开发活跃 ({dev:.2f})")
        elif dev < 0.2:
            score -= 0.1
            signals.append("开发不活跃")

        # 市值合理性
        mcap_size = coin.get("f_market_cap_size", 0) or 0
        if 0.3 < mcap_size < 0.7:
            score += 0.05
            signals.append("中等市值 (成长空间)")

        # 链上活跃
        onchain = coin.get("f_onchain_activity", 0) or 0
        if onchain > 0.6:
            score += 0.1
            signals.append(f"链上活跃 ({onchain:.2f})")

        conviction = "high" if score > 0.7 else "medium" if score > 0.5 else "low"
        results[sym] = {
            "score": round(max(0, min(1, score)), 3),
            "signals": signals,
            "conviction": conviction,
        }
    return results


# ====================================================================
#  Agent 2: Momentum (价格趋势 + 成交量)
# ====================================================================

def momentum_agent(coins: list[dict]) -> dict[str, dict]:
    """动量 Agent: 关注价格趋势、突破信号、成交量配合。"""
    results = {}
    for coin in coins:
        sym = coin.get("symbol", "")
        signals = []
        score = 0.5

        # 30d 动量
        mom_30d = coin.get("f_momentum_30d", 0) or 0
        if mom_30d > 0.7:
            score += 0.2
            signals.append(f"30d 强势 ({mom_30d:.2f})")
        elif mom_30d < 0.3:
            score -= 0.15
            signals.append("30d 弱势")

        # 7d 动量 (短期加速)
        mom_7d = coin.get("f_momentum_7d", 0) or 0
        if mom_7d > 0.7 and mom_30d > 0.5:
            score += 0.1
            signals.append("短期加速 ✓")
        elif mom_7d < 0.3 and mom_30d > 0.5:
            signals.append("⚠️ 短期回调, 但中期趋势完好")

        # ATH 回撤
        ath = coin.get("f_ath_drawdown", 0) or 0
        if ath > 0.8:
            score += 0.1
            signals.append("接近 ATH (强势)")
        elif ath < 0.2:
            score += 0.05
            signals.append("深度回撤 (可能超跌反弹)")

        # 成交量
        vol = coin.get("f_volume_turnover", 0) or 0
        if vol > 0.7:
            score += 0.1
            signals.append(f"放量 ({vol:.2f})")

        conviction = "high" if score > 0.7 else "medium" if score > 0.5 else "low"
        results[sym] = {
            "score": round(max(0, min(1, score)), 3),
            "signals": signals,
            "conviction": conviction,
        }
    return results


# ====================================================================
#  Agent 3: Sentiment (NLP 情绪 + 叙事热度)
# ====================================================================

def sentiment_agent(coins: list[dict],
                     sentiment_data: dict = None) -> dict[str, dict]:
    """情绪 Agent: 关注市场情绪、叙事热度、Trending 状态。"""
    results = {}
    for coin in coins:
        sym = coin.get("symbol", "")
        signals = []
        score = 0.5

        # 叙事热度 (screener factor)
        narrative = coin.get("f_narrative_heat", 0) or 0
        if narrative > 0.7:
            score += 0.15
            signals.append(f"叙事火热 ({narrative:.2f})")
        elif narrative < 0.2:
            score -= 0.05
            signals.append("叙事冷淡")

        # NLP 情绪数据 (如果有)
        if sentiment_data and sym in sentiment_data:
            sent = sentiment_data[sym]
            sent_score = sent.get("sentiment_score", 0)
            if sent_score > 0.3:
                score += 0.15
                signals.append(f"情绪积极 ({sent_score:+.2f})")
            elif sent_score < -0.3:
                score -= 0.15
                signals.append(f"情绪消极 ({sent_score:+.2f})")

            hype = sent.get("hype_score", 0)
            if hype > 0.5:
                signals.append(f"高关注度 (hype={hype:.2f})")

        # 资金费率 (情绪代理) — f_funding_rate 是归一化分数:
        #   高分 = 负费率 = 空头拥挤/超卖 = 看多; 低分 = 正费率 = 多方过度拥挤 = 看空
        funding = coin.get("f_funding_rate", 0) or 0
        if funding > 0.7:
            score += 0.05
            signals.append("资金费率健康 (空头拥挤/超卖, 反转倾向)")
        elif funding < 0.3:
            score -= 0.1
            signals.append("⚠️ 资金费率偏高 (多方过度拥挤)")

        conviction = "high" if score > 0.7 else "medium" if score > 0.5 else "low"
        results[sym] = {
            "score": round(max(0, min(1, score)), 3),
            "signals": signals,
            "conviction": conviction,
        }
    return results


# ====================================================================
#  Agent 4: Risk (波动率 / 回撤 / 集中度)
# ====================================================================

def risk_agent(coins: list[dict]) -> dict[str, dict]:
    """风控 Agent: 标记风险因素, 低风险得高分。"""
    results = {}
    for coin in coins:
        sym = coin.get("symbol", "")
        signals = []
        score = 0.7  # 默认中高 (无风险假设)

        # 波动率 (用 30d 变幅代理)
        change_30d = abs(coin.get("change_30d", 0) or 0)
        if change_30d > 50:
            score -= 0.2
            signals.append(f"⚠️ 高波动 (30d |{change_30d:.0f}%|)")
        elif change_30d > 30:
            score -= 0.1
            signals.append(f"中等波动 (30d |{change_30d:.0f}%|)")

        # 市值过小
        mcap = coin.get("market_cap", 0) or 0
        if mcap < 1e8:
            score -= 0.15
            signals.append(f"⚠️ 微市值 (${mcap/1e6:.0f}M)")
        elif mcap < 5e8:
            score -= 0.05
            signals.append(f"小市值 (${mcap/1e6:.0f}M)")

        # 资金费率极端
        funding = coin.get("f_funding_rate", 0) or 0
        if funding > 0.85 or funding < 0.15:
            score -= 0.15
            signals.append("⚠️ 资金费率极端")

        # ATH 回撤过深 (可能是基本面恶化)
        ath = coin.get("f_ath_drawdown", 0) or 0
        if ath < 0.15:
            score -= 0.1
            signals.append("⚠️ 距 ATH 跌幅 >85%")

        # 正面: 大市值稳定
        if mcap > 1e10 and change_30d < 20:
            score += 0.1
            signals.append("大盘稳健")

        conviction = "high" if score > 0.7 else "medium" if score > 0.5 else "low"
        results[sym] = {
            "score": round(max(0, min(1, score)), 3),
            "signals": signals,
            "conviction": conviction,
        }
    return results


# ====================================================================
#  Ensemble: 加权投票
# ====================================================================

def ensemble_vote(
    agent_results: dict[str, dict[str, dict]],
    weights: dict[str, float] = None,
) -> list[dict]:
    """加权投票 Ensemble。

    agent_results: {"fundamental": {sym: {score, signals}}, "momentum": {...}, ...}
    weights: {"fundamental": 0.3, ...}

    Returns: [{symbol, swarm_score, agents_agree, agent_breakdown, top_signals}]
    """
    w = weights or DEFAULT_AGENT_WEIGHTS
    total_w = sum(w.values())

    # 收集所有 symbols
    all_syms = set()
    for agent_name, scores in agent_results.items():
        all_syms.update(scores.keys())

    results = []
    for sym in all_syms:
        weighted_score = 0
        agent_breakdown = {}
        all_signals = []
        agree_count = 0
        total_agents = 0

        for agent_name, scores in agent_results.items():
            agent_w = w.get(agent_name, 0.25) / total_w
            agent_score = scores.get(sym, {}).get("score", 0.5)
            conviction = scores.get(sym, {}).get("conviction", "medium")
            signals = scores.get(sym, {}).get("signals", [])

            weighted_score += agent_score * agent_w
            agent_breakdown[agent_name] = {
                "score": agent_score,
                "conviction": conviction,
                "weight": round(agent_w, 2),
            }
            all_signals.extend(signals[:2])  # 每个 Agent 最多 2 条
            total_agents += 1
            if agent_score > 0.55:
                agree_count += 1

        results.append({
            "symbol": sym,
            "swarm_score": round(weighted_score, 4),
            "agents_agree": agree_count,
            "total_agents": total_agents,
            "consensus": "strong" if agree_count >= 3 else "moderate" if agree_count >= 2 else "weak",
            "agent_breakdown": agent_breakdown,
            "top_signals": all_signals[:6],
        })

    results.sort(key=lambda x: x["swarm_score"], reverse=True)
    return results


# ====================================================================
#  LLM Jury (可选增强)
# ====================================================================

def llm_jury(top_coins: list[dict], agent_results: dict) -> dict:
    """用 Claude 做最终 Jury — 综合各 Agent 分析给出最终判断。

    只对 Top-10 代币做精细分析 (节省 token)。
    """
    try:
        from ..evolution._claude_runner import run_claude
    except Exception:
        return {"ok": False, "error": "Claude CLI 不可用"}

    # 构建 Agent 分析摘要
    analyses = []
    for coin in top_coins[:10]:
        sym = coin["symbol"]
        lines = [f"\n### {sym} (Swarm Score: {coin['swarm_score']:.3f})"]
        for agent_name, data in coin.get("agent_breakdown", {}).items():
            lines.append(f"  {agent_name}: score={data['score']:.2f} ({data['conviction']})")
        sigs = coin.get("top_signals", [])
        if sigs:
            lines.append(f"  信号: {'; '.join(sigs[:4])}")
        analyses.append("\n".join(lines))

    prompt = f"""你是 Crypto 对冲基金的首席投资官。以下是 4 个分析师 Agent 的分析结果:

{chr(10).join(analyses)}

基于以上多维度分析, 请:
1. 对 Top-10 重新排序 (给出最终推荐排名)
2. 标出最有信心的 3 个标的和原因
3. 标出需要警惕的风险点
4. 给出当前市场环境下的仓位建议 (激进/中性/保守)

输出 JSON:
{{
  "final_ranking": ["SYM1", "SYM2", ...],
  "high_conviction": [{{"symbol": "...", "reason": "..."}}],
  "risk_alerts": ["..."],
  "position_stance": "aggressive/neutral/conservative",
  "rationale": "一段话总结"
}}
只输出 JSON。"""

    result = run_claude(prompt, system="你是量化对冲基金 CIO。只输出 JSON。", timeout=120)
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error", "LLM 调用失败")}

    import re
    md = result.get("markdown", "")
    json_match = re.search(r'\{[\s\S]*\}', md)
    if json_match:
        try:
            jury_verdict = json.loads(json_match.group())
            jury_verdict["ok"] = True
            return jury_verdict
        except json.JSONDecodeError:
            pass

    return {"ok": False, "error": "LLM 输出解析失败"}


# ====================================================================
#  顶层 API: 跑一轮 Swarm Decision
# ====================================================================

def run_swarm_decision(
    scored_coins: list[dict],
    sentiment_data: dict = None,
    agent_weights: dict = None,
    use_llm_jury: bool = False,
    top_n: int = 30,
) -> dict:
    """运行一轮 Swarm Decision。

    Returns: {
        ok, top: [{symbol, swarm_score, consensus, ...}],
        jury: {...} or None,
        decision_trace: {...},
    }
    """
    log.info(f"[Swarm] 启动 4-Agent 决策 ({len(scored_coins)} 代币)...")

    # 各 Agent 独立分析
    log.info("  Agent 1: Fundamental...")
    fund_results = fundamental_agent(scored_coins[:top_n * 2])

    log.info("  Agent 2: Momentum...")
    mom_results = momentum_agent(scored_coins[:top_n * 2])

    log.info("  Agent 3: Sentiment...")
    sent_results = sentiment_agent(scored_coins[:top_n * 2], sentiment_data)

    log.info("  Agent 4: Risk...")
    risk_results = risk_agent(scored_coins[:top_n * 2])

    agent_results = {
        "fundamental": fund_results,
        "momentum": mom_results,
        "sentiment": sent_results,
        "risk": risk_results,
    }

    # Ensemble 投票
    log.info("  Ensemble 加权投票...")
    ensemble = ensemble_vote(agent_results, agent_weights)
    top = ensemble[:top_n]

    # 统计共识度
    strong = sum(1 for t in top if t["consensus"] == "strong")
    moderate = sum(1 for t in top if t["consensus"] == "moderate")
    log.info(f"  ✓ Top-{len(top)}: {strong} strong, {moderate} moderate consensus")

    # 可选 LLM Jury
    jury = None
    if use_llm_jury:
        log.info("  LLM Jury 审议...")
        jury = llm_jury(top, agent_results)
        if jury.get("ok"):
            log.info(f"  ✓ Jury stance: {jury.get('position_stance', 'N/A')}")

    # Decision trace (可追溯)
    trace = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "n_coins_input": len(scored_coins),
        "n_coins_output": len(top),
        "agent_weights": agent_weights or DEFAULT_AGENT_WEIGHTS,
        "consensus_distribution": {
            "strong": strong,
            "moderate": moderate,
            "weak": len(top) - strong - moderate,
        },
        "jury_used": use_llm_jury,
    }

    # 保存 trace
    META_DIR.mkdir(parents=True, exist_ok=True)
    trace_path = META_DIR / f"swarm_trace_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    trace_path.write_text(json.dumps({
        "trace": trace, "top": top[:10],
        "jury": jury if jury and jury.get("ok") else None,
    }, ensure_ascii=False, indent=2))

    return {
        "ok": True,
        "top": top,
        "jury": jury,
        "decision_trace": trace,
    }
