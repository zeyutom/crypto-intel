"""因子元数据中心: 中文别名、通俗解释、信号方向解读、PM 行动建议规则。

核心理念: 报告里所有"看不懂"的因子都从这里取人话解释。
任何新因子上线必须先在这里登记元数据。
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable

# ----- 信号语义解释模板 -----
SIGNAL_EXPLAIN = {
    1: "看多",
    -1: "看空",
    0: "中性 / 信号尚不显著",
}

# ----- 因子卡片元数据 -----

@dataclass
class FactorMeta:
    cn_name: str           # 中文别名
    category: str          # 类别(用于分组)
    one_line: str          # 一句话定义
    why_matters: str       # 为什么重要
    how_to_read: dict      # signal → "...解读..."
    pm_action: dict        # signal → "...PM 建议..."
    fmt_unit: str          # raw 数值单位说明


META: dict[str, FactorMeta] = {
    "funding_composite": FactorMeta(
        cn_name="资金费率综合 (Funding Composite)",
        category="衍生品情绪",
        one_line="主流交易所永续合约的加权资金费率,反映多空力量是否过度倾斜。",
        why_matters="资金费率是杠杆资金成本。极端正费率说明多头拥挤(过热),极端负费率说明空头拥挤——两者都常预示反转。",
        how_to_read={
             1: "费率显著为负 → 空头过度拥挤,容易出现轧空 / 反弹。",
            -1: "费率显著为正 → 多头过度拥挤,警惕短期回调。",
             0: "费率温和,杠杆情绪正常,本身不构成方向信号。",
        },
        pm_action={
             1: "可考虑短线试探多头,或对已有空头头寸减仓。",
            -1: "已有多头建议部分止盈,新仓暂缓追高。",
             0: "无需行动,继续观察其他因子。",
        },
        fmt_unit="单期 (8h) 资金费率, 0.01% = 万一",
    ),

    "coinbase_premium": FactorMeta(
        cn_name="Coinbase 美区溢价 (Coinbase Premium)",
        category="机构资金",
        one_line="Coinbase 价格 vs Binance 价格的偏差,反映美国机构资金主导程度。",
        why_matters="美国合规通道(Coinbase + 现货 ETF)是 2024+ 周期最重要的边际买盘。溢价为正说明美区在抢筹,与 ETF 净流入共振时信号尤其强。",
        how_to_read={
             1: "美区显著买入,溢价高于 0.2%,机构需求强劲。",
            -1: "美区在抛售或撤离,溢价显著为负。",
             0: "两侧均衡,无明显资金主导信号。",
        },
        pm_action={
             1: "若同时 ETF 净流入也为正 → 高置信看多,可加大主流币(BTC/ETH)配置。",
            -1: "美区撤离常伴随短中期下跌,建议降低 BTC 杠杆或对冲。",
             0: "等待方向性信号出现。",
        },
        fmt_unit="百分比, 0.2% 视为有效信号",
    ),

    "stablecoin_mint_7d": FactorMeta(
        cn_name="稳定币 7 日净增发 (Stablecoin Net Mint)",
        category="宏观流动性",
        one_line="USDT + USDC 过去 7 天净流通量增减,代表新增/流出加密市场的购买力。",
        why_matters="稳定币是加密圈的「美元基础货币」。每净增发 10 亿美元意味着市场新增了 10 亿美元的潜在买盘——这是最干净的宏观流动性指标之一。",
        how_to_read={
             1: "7d 净增发 > +10 亿美元,新资金正在持续进场。",
            -1: "7d 净流出 > 10 亿美元,资金正在退出加密市场,警惕下行。",
             0: "增减温和,流动性持平。",
        },
        pm_action={
             1: "宏观顺风,可保持或提升风险敞口;尤其利好 BTC、ETH。",
            -1: "宏观逆风,降低杠杆,关注核心仓位的止损位。",
             0: "维持现有仓位,以个币因子为准。",
        },
        fmt_unit="单位:十亿美元 (B$)",
    ),

    "fear_greed_reversal": FactorMeta(
        cn_name="恐慌贪婪指数反向 (Fear & Greed Reversal)",
        category="市场情绪",
        one_line="Alternative.me 综合 7 类指标得出的 0–100 情绪分,极端值用于反向操作。",
        why_matters="历史上 BTC 在 F&G < 25(极度恐慌)买入,> 75(极度贪婪)卖出,中长期胜率显著高于 50%——典型的反人性赚钱信号。",
        how_to_read={
             1: "F&G ≤ 25,市场极度恐慌,经验上是抄底窗口。",
            -1: "F&G ≥ 75,市场极度贪婪,经验上是减仓窗口。",
             0: "情绪在中性区间(25–75),不构成反向信号。",
        },
        pm_action={
             1: "建议分批建仓 BTC/ETH;不要一次满仓,留 30%+ 子弹防进一步恐慌。",
            -1: "建议分批减仓主流币;落袋为安,保留少量底仓不出。",
             0: "情绪中性时,不基于 F&G 行动,看其他因子。",
        },
        fmt_unit="0–100 分",
    ),

    "etf_flow_5d": FactorMeta(
        cn_name="BTC 现货 ETF 5 日累计净流入 (ETF Net Flow)",
        category="机构资金",
        one_line="美国 BTC 现货 ETF(IBIT/FBTC 等)过去 5 个交易日的净申购总额。",
        why_matters="自 2024 年现货 ETF 上市后,机构通过 ETF 的净申购成为 BTC 价格的最强解释变量之一。连续多日正净流入往往伴随价格新高。",
        how_to_read={
             1: "5d 累计净流入 > +5 亿美元,机构持续买入。",
            -1: "5d 累计净流出 > 5 亿美元,机构在派发,警惕下跌。",
             0: "净流入温和或方向不明。",
        },
        pm_action={
             1: "BTC 主升浪信号之一,可加大配置;若同时 Coinbase Premium 也为正 → 强信号。",
            -1: "机构撤离,显著利空 BTC,降低敞口或对冲。",
             0: "维持现有 BTC 配置。",
        },
        fmt_unit="单位:百万美元 (M$)",
    ),
}


# ----- 资产中文名 -----
ASSET_CN = {
    "bitcoin": "比特币 (BTC)",
    "ethereum": "以太坊 (ETH)",
    "solana": "Solana (SOL)",
    "bnb": "币安币 (BNB)",
    "stablecoins": "稳定币聚合",
    "market": "全市场",
    None: "全市场",
    "nan": "全市场",
}


# ----- Regime 中文解释 -----
REGIME_CN = {
    "BULL":         ("牛市", "机构资金持续流入 + 情绪健康, 顺势做多为主"),
    "BEAR":         ("熊市", "资金撤离 + 流动性收缩, 控制仓位为主"),
    "CHOP":         ("震荡", "缺乏方向性信号, 建议低频操作 / 等待"),
    "CRISIS":       ("危机", "极度恐慌叠加资金外流, 防御为主"),
    "BEAR_BOUNCE":  ("熊市反弹", "极端恐慌但 ETF 已开始回流, 短线反弹机会"),
    "UNKNOWN":      ("数据不足", "因子数量太少, 暂不出 Regime 判断"),
}


def factor_meta(factor: str) -> FactorMeta | None:
    return META.get(factor)


def asset_cn(asset_id: str | None) -> str:
    if asset_id is None or (isinstance(asset_id, float)):
        return "全市场"
    return ASSET_CN.get(asset_id, asset_id)


def regime_cn(regime: str) -> tuple[str, str]:
    return REGIME_CN.get(regime, (regime, ""))


def signal_label(signal: int) -> str:
    return SIGNAL_EXPLAIN.get(int(signal) if signal is not None else 0, "—")
