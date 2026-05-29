"""各子 Agent 的 prompt + 解析逻辑。

每个 agent 是一个函数, 输入 project_name + 已知信息, 输出结构化 JSON。
所有 agent 共用 _claude_runner.run_claude() 调 Claude CLI。
"""
from __future__ import annotations
import json
from ..evolution._claude_runner import run_claude
from ..utils import setup_logger

log = setup_logger("research_agents", "INFO")


# ── Agent 1: GitHub 开发活跃度 Agent ────────────────────────────────
GITHUB_AGENT_SYSTEM = """你是 crypto 项目 GitHub 分析师。给定一个区块链项目名称,
使用 web_search 搜索该项目在 GitHub 上的所有重要仓库, 分析其开发活跃度。

输出**严格合法的 JSON** (只有 JSON, 无 markdown 围栏):
{
  "project": "项目名",
  "github_orgs": ["org1", "org2"],
  "main_repos": [
    {"repo": "org/repo-name", "stars": 12000, "forks": 3000,
     "last_commit_days_ago": 2, "contributors": 150,
     "language": "C++", "description": "..."}
  ],
  "notable_recent_prs": [
    {"title": "PR标题", "repo": "org/repo", "date": "2026-05-01", "significance": "..."}
  ],
  "dev_activity_score": 85,
  "dev_trend": "rising",
  "key_findings": ["发现1", "发现2", "发现3"],
  "risks": ["风险1"]
}

规则:
- dev_activity_score: 0-100, 100=极其活跃(daily commits, 多 contributor)
- dev_trend: "rising" / "stable" / "declining"
- 列出 top 5-10 重要仓库 (按 stars 排序)
- 关注最近 30 天的 PR/commit 活跃度
- 识别关键升级/里程碑
- JSON 中数字不带 + 号, 无尾随逗号"""


def run_github_agent(project: str, context: str = "") -> dict:
    """搜索分析项目 GitHub 开发活跃度。"""
    prompt = f"""请深度分析 **{project}** 区块链项目的 GitHub 开发状况。

补充信息: {context or '无'}

请用 web_search 搜索:
1. "{project} blockchain GitHub organization repositories"
2. "{project} GitHub commits pull requests 2026"
3. "{project} developer activity stats"

然后输出 JSON。"""
    log.info(f"[GitHub Agent] 开始分析 {project}")
    return run_claude(prompt, system=GITHUB_AGENT_SYSTEM, timeout=600)


# ── Agent 2: 链上数据 Agent ─────────────────────────────────────────
ONCHAIN_AGENT_SYSTEM = """你是链上数据分析师。给定一个区块链/代币名称,
使用 web_search 收集其最新链上指标和 DeFi 生态数据。

输出**严格合法的 JSON** (只有 JSON, 无 markdown 围栏):
{
  "project": "项目名",
  "token_symbol": "XXX",
  "price_usd": 3.25,
  "market_cap_billion": 8.1,
  "price_change_30d_pct": -12.5,
  "ath_usd": 8.25,
  "ath_drawdown_pct": -60.6,
  "daily_active_addresses": 150000,
  "daily_transactions": 2100000,
  "total_wallets_million": 52.1,
  "tvl_million": 59.15,
  "tvl_change_30d_pct": -8.2,
  "top_defi_protocols": [
    {"name": "STON.fi", "tvl_million": 25.0, "category": "DEX"},
    {"name": "DeDust", "tvl_million": 15.0, "category": "DEX"}
  ],
  "staking_yield_pct": 3.8,
  "inflation_rate_pct": 0.6,
  "supply_circulating_billion": 2.5,
  "supply_total_billion": 5.1,
  "onchain_health_score": 62,
  "key_findings": ["发现1", "发现2"],
  "risks": ["风险1"]
}

规则:
- onchain_health_score: 0-100
- 所有百分比保留1位小数
- 用 web_search 搜索 DefiLlama/CoinGecko/Tonscan 等来源
- 关注 TVL 趋势、活跃地址变化、鲸鱼动向
- JSON 中数字不带 + 号"""


def run_onchain_agent(project: str, token: str = "", context: str = "") -> dict:
    """分析项目链上数据和 DeFi 生态。"""
    prompt = f"""请分析 **{project}** (代币: {token or project}) 的最新链上数据。

补充信息: {context or '无'}

请用 web_search 搜索:
1. "{project} price market cap May 2026"
2. "{project} DeFi TVL DefiLlama 2026"
3. "{project} on-chain active addresses transactions 2026"
4. "{project} tokenomics supply staking"

然后输出 JSON。"""
    log.info(f"[Onchain Agent] 开始分析 {project}")
    return run_claude(prompt, system=ONCHAIN_AGENT_SYSTEM, timeout=600)


# ── Agent 3: 社区与叙事 Agent ───────────────────────────────────────
NARRATIVE_AGENT_SYSTEM = """你是 crypto 社区情绪与叙事分析师。给定一个项目名称,
分析其社区热度、叙事定位、KOL 观点和市场情绪。

输出**严格合法的 JSON** (只有 JSON, 无 markdown 围栏):
{
  "project": "项目名",
  "narratives": ["L1公链", "Telegram生态", "Mass Adoption"],
  "narrative_heat": 72,
  "community_metrics": {
    "twitter_followers": "1.2M",
    "telegram_members": "500K",
    "reddit_subscribers": "50K",
    "discord_members": "120K"
  },
  "sentiment": "neutral_to_bullish",
  "kol_opinions": [
    {"who": "KOL名", "opinion": "看法摘要", "stance": "bullish"}
  ],
  "recent_catalysts": [
    {"event": "事件描述", "date": "2026-05-01", "impact": "positive"}
  ],
  "upcoming_catalysts": [
    {"event": "事件描述", "expected_date": "2026-Q3", "potential_impact": "high"}
  ],
  "competitive_position": "描述在同类项目中的位置",
  "community_score": 70,
  "key_findings": ["发现1", "发现2"],
  "risks": ["风险1"]
}

规则:
- narrative_heat: 0-100
- community_score: 0-100
- sentiment: bearish / neutral_to_bearish / neutral / neutral_to_bullish / bullish
- 用 web_search 搜索 Twitter/Reddit/Telegram/Discord 最新讨论
- 关注 KOL 最近 7 天的发言
- JSON 中数字不带 + 号"""


def run_narrative_agent(project: str, context: str = "") -> dict:
    """分析项目社区情绪和叙事。"""
    prompt = f"""请分析 **{project}** 的社区情绪、叙事热度和 KOL 观点。

补充信息: {context or '无'}

请用 web_search 搜索:
1. "{project} crypto community sentiment 2026"
2. "{project} KOL opinion twitter May 2026"
3. "{project} upcoming catalysts roadmap 2026"
4. "{project} vs competitors comparison"

然后输出 JSON。"""
    log.info(f"[Narrative Agent] 开始分析 {project}")
    return run_claude(prompt, system=NARRATIVE_AGENT_SYSTEM, timeout=600)


# ── Agent 4: 投资风险 Agent ─────────────────────────────────────────
RISK_AGENT_SYSTEM = """你是 crypto 投资风险分析师。给定一个项目的多维数据,
做综合风险评估。

输出**严格合法的 JSON** (只有 JSON, 无 markdown 围栏):
{
  "project": "项目名",
  "risk_matrix": {
    "smart_contract_risk": {"level": "medium", "score": 45, "detail": "..."},
    "centralization_risk": {"level": "high", "score": 70, "detail": "..."},
    "regulatory_risk": {"level": "medium", "score": 55, "detail": "..."},
    "market_risk": {"level": "high", "score": 65, "detail": "..."},
    "competition_risk": {"level": "medium", "score": 50, "detail": "..."},
    "tokenomics_risk": {"level": "low", "score": 30, "detail": "..."},
    "team_risk": {"level": "low", "score": 25, "detail": "..."}
  },
  "overall_risk_score": 55,
  "risk_level": "medium",
  "whale_concentration": "描述鲸鱼持仓集中度",
  "key_risks": ["核心风险1", "核心风险2", "核心风险3"],
  "mitigants": ["缓解因素1", "缓解因素2"],
  "worst_case_scenario": "最坏情况描述",
  "black_swan_risks": ["黑天鹅风险1"]
}

规则:
- overall_risk_score: 0-100, 100 = 极高风险
- risk_level: low / medium / high / very_high
- 每个子风险 score 也是 0-100
- level: low (0-33) / medium (34-66) / high (67-100)
- JSON 中数字不带 + 号"""


def run_risk_agent(project: str, research_context: str) -> dict:
    """基于已收集的数据做风险评估。"""
    prompt = f"""请基于以下已收集的研究数据, 对 **{project}** 做全面投资风险评估。

== 已收集的研究数据 ==
{research_context}

请用 web_search 补充搜索:
1. "{project} security audit smart contract risk"
2. "{project} regulatory risk concerns"
3. "{project} whale holdings concentration"

然后输出 JSON。"""
    log.info(f"[Risk Agent] 开始分析 {project}")
    return run_claude(prompt, system=RISK_AGENT_SYSTEM, timeout=600)


# ── Agent 5: Alpha 信号综合 Agent ───────────────────────────────────
ALPHA_AGENT_SYSTEM = """你是面向投资经理的 crypto alpha 信号分析师。基于多维研究数据,
输出一份结构化的投资分析结论。

输出**严格合法的 JSON** (只有 JSON, 无 markdown 围栏):
{
  "project": "项目名",
  "token": "XXX",
  "verdict": "CAUTIOUS_BUY",
  "confidence": 65,
  "alpha_signals": [
    {"signal": "信号描述", "direction": "bullish", "strength": "strong",
     "timeframe": "3-6个月", "source": "来源agent"}
  ],
  "price_levels": {
    "current": 3.25,
    "support_1": 2.80,
    "support_2": 2.20,
    "resistance_1": 4.00,
    "resistance_2": 5.50,
    "target_6m": 4.50,
    "stop_loss": 2.50
  },
  "position_sizing": "建议仓位占 crypto portfolio 的 3-5%",
  "entry_strategy": "分批建仓策略描述",
  "exit_triggers": ["止盈/止损触发条件1", "条件2"],
  "dimension_scores": {
    "development": 85,
    "onchain": 62,
    "narrative": 72,
    "risk_adjusted": 58
  },
  "overall_score": 68,
  "executive_summary": "给 PM 的2-3句话核心结论",
  "bull_case": "牛市情景",
  "bear_case": "熊市情景",
  "catalysts_to_watch": ["关注催化剂1", "催化剂2"],
  "comparable_projects": [
    {"name": "SOL", "why_comparable": "原因", "relative_valuation": "TON更便宜/更贵"}
  ]
}

规则:
- verdict: STRONG_BUY / BUY / CAUTIOUS_BUY / HOLD / CAUTIOUS_SELL / SELL / STRONG_SELL
- confidence: 0-100
- overall_score: 0-100 (综合各维度)
- 每个 alpha signal 的 strength: weak / moderate / strong
- 必须给出具体价格水平 (support/resistance/target/stop_loss)
- 以面向投资经理的专业口吻
- JSON 中数字不带 + 号"""


def run_alpha_agent(project: str, all_research: str) -> dict:
    """综合所有研究数据输出 alpha 信号。"""
    prompt = f"""请基于以下多个 Agent 收集的全面研究数据, 输出 **{project}** 的 alpha 信号投资分析。

== 全维度研究数据 ==
{all_research}

请输出 JSON (面向投资经理的专业分析)。"""
    log.info(f"[Alpha Agent] 综合分析 {project}")
    return run_claude(prompt, system=ALPHA_AGENT_SYSTEM, timeout=900)
