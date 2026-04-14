"""PM 早会简报生成: 把因子原值翻译成"今日发生了什么 + 应该怎么做"。

规则驱动 (无 LLM 依赖), 输出供 template 渲染。
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field
from typing import Any
from ..factors._metadata import META, regime_cn


@dataclass
class Briefing:
    headline: str                            # 一句话核心结论
    regime_cn: str                           # 中文 regime
    regime_explain: str                      # regime 解释
    bull_points: list[str] = field(default_factory=list)
    bear_points: list[str] = field(default_factory=list)
    risk_warnings: list[str] = field(default_factory=list)
    pm_actions: list[str] = field(default_factory=list)
    coverage_warning: str | None = None      # 数据覆盖度警告


def _factor_lookup(factors_df) -> dict[str, dict]:
    """因子名 → {raw_value, signal, confidence, meta}"""
    out = {}
    for _, row in factors_df.iterrows():
        fname = row["factor"]
        if fname in out and (row.get("asset_id") or "") != "bitcoin":
            # 同一因子多 asset 时, 优先 bitcoin
            continue
        try:
            meta = json.loads(row["meta"]) if row["meta"] else {}
        except Exception:
            meta = {}
        out[fname] = {
            "raw_value": row["raw_value"],
            "signal": int(row["signal"]) if row["signal"] is not None else 0,
            "confidence": float(row["confidence"] or 0),
            "meta": meta,
            "asset_id": row.get("asset_id"),
        }
    return out


def build_briefing(signals_df, factors_df, regime: str,
                   missing_sources: list[str] | None = None) -> Briefing:
    facs = _factor_lookup(factors_df)
    rgm_cn, rgm_exp = regime_cn(regime)

    bulls: list[str] = []
    bears: list[str] = []
    risks: list[str] = []
    actions: list[str] = []

    # ---- 1. ETF 净流入 ----
    if "etf_flow_5d" in facs:
        f = facs["etf_flow_5d"]
        v = f["raw_value"]
        if f["signal"] == 1:
            bulls.append(f"BTC 现货 ETF 近 5 日累计净流入 ${v:+.0f}M, 机构资金持续买入(2024+ 周期最强机构指标)")
            actions.append("机构资金顺风, 可保持或提升 BTC 配置")
        elif f["signal"] == -1:
            bears.append(f"BTC 现货 ETF 近 5 日累计净流出 ${v:+.0f}M, 机构在派发")
            risks.append("机构撤离常预示中短期下跌, 考虑降低 BTC 敞口或对冲")
        else:
            # 中性时也提一下
            if v is not None:
                bulls.append(f"ETF 净流入 ${v:+.0f}M (温和)") if v >= 0 else bears.append(f"ETF 净流出 ${v:+.0f}M (温和)")

    # ---- 2. 稳定币净增发 ----
    if "stablecoin_mint_7d" in facs:
        f = facs["stablecoin_mint_7d"]
        v = f["raw_value"] / 1e9 if f["raw_value"] is not None else 0
        if f["signal"] == 1:
            bulls.append(f"USDT+USDC 7 日净增发 +${v:.2f}B, 新增购买力进场, 宏观流动性顺风")
        elif f["signal"] == -1:
            bears.append(f"USDT+USDC 7 日净流出 ${v:.2f}B, 资金正在退出加密市场")
            risks.append("稳定币流出常领先 BTC 下跌 1–2 周, 警惕")

    # ---- 3. F&G ----
    if "fear_greed_reversal" in facs:
        f = facs["fear_greed_reversal"]
        v = f["raw_value"]
        cls = f["meta"].get("classification", "")
        if f["signal"] == 1:
            bulls.append(f"恐慌贪婪指数 {v:.0f}/100 ({cls}), 历史上极度恐慌区是高胜率抄底窗口")
            actions.append("可分批建仓 BTC/ETH (留 30%+ 子弹防进一步恐慌)")
        elif f["signal"] == -1:
            bears.append(f"恐慌贪婪指数 {v:.0f}/100 ({cls}), 市场过热, 经验上是减仓窗口")
            actions.append("分批减仓主流币, 保留底仓; 不追高")
        else:
            # 中性也展示
            bulls.append(f"恐慌贪婪指数 {v:.0f}/100 ({cls}), 情绪中性") if v >= 50 else \
                bears.append(f"恐慌贪婪指数 {v:.0f}/100 ({cls}), 情绪偏弱")

    # ---- 4. Funding ----
    if "funding_composite" in facs:
        f = facs["funding_composite"]
        v = f["raw_value"]
        if f["signal"] == -1:
            bears.append(f"资金费率综合 {v*100:.4f}%(8h), 多头杠杆过度拥挤, 警惕短期回调")
            risks.append("高费率叠加价格新高时, 常出现 5–10% 级别回撤")
        elif f["signal"] == 1:
            bulls.append(f"资金费率综合 {v*100:.4f}%(8h), 空头拥挤, 容易出现轧空 / 反弹")

    # ---- 5. Coinbase Premium ----
    if "coinbase_premium" in facs:
        f = facs["coinbase_premium"]
        v = f["raw_value"]
        if f["signal"] == 1:
            bulls.append(f"Coinbase 美区溢价 {v*100:+.3f}%, 美区机构在抢筹")
            if "etf_flow_5d" in facs and facs["etf_flow_5d"]["signal"] == 1:
                actions.append("⭐ Coinbase Premium 与 ETF 净流入双正共振, 高置信看多, 可加大主流币配置")
        elif f["signal"] == -1:
            bears.append(f"Coinbase 美区溢价 {v*100:+.3f}%, 美区在撤离")

    # ---- 综合 headline ----
    bull_n, bear_n = len(bulls), len(bears)
    if bull_n > bear_n + 1:
        headline = f"看多信号 {bull_n} 条 vs 看空 {bear_n} 条 → 整体偏多, Regime: {rgm_cn}"
    elif bear_n > bull_n + 1:
        headline = f"看空信号 {bear_n} 条 vs 看多 {bull_n} 条 → 整体偏空, Regime: {rgm_cn}"
    else:
        headline = f"多空信号大致平衡 ({bull_n} vs {bear_n}) → 震荡观望, Regime: {rgm_cn}"

    if not actions:
        actions.append("当前因子组合未给出明确高置信信号, 建议维持现有仓位, 重点关注后续因子变化")

    coverage = None
    if missing_sources:
        coverage = f"⚠ 本次有 {len(missing_sources)} 个数据源未采集成功: {', '.join(missing_sources)}; 相关因子可能缺失或置信度偏低。"

    return Briefing(
        headline=headline,
        regime_cn=rgm_cn,
        regime_explain=rgm_exp,
        bull_points=bulls,
        bear_points=bears,
        risk_warnings=risks,
        pm_actions=actions,
        coverage_warning=coverage,
    )
