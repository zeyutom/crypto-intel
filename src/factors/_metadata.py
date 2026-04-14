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

    # ============ v0.4 新增 ============

    # --- 衍生品/杠杆 ---
    "open_interest_change": FactorMeta(
        cn_name="未平仓合约变化 (Open Interest Δ)",
        category="衍生品杠杆",
        one_line="全交易所衍生品未平仓合约 24h 变化, 反映杠杆资金流入流出方向。",
        why_matters="价涨 + OI 涨 = 真趋势 (新多头入场); 价涨 + OI 跌 = 空头回补 (脆弱反弹)。OI 是判断行情真假的关键。",
        how_to_read={
             1: "OI 24h 上升 > 5%, 杠杆资金流入, 趋势可能延续。",
            -1: "OI 24h 下降 > 5%, 杠杆资金离场, 警惕变盘。",
             0: "OI 变动温和, 无明显方向。",
        },
        pm_action={
             1: "若价格同向上涨, 趋势真实, 可顺势; 若价格反向, 警惕新空头。",
            -1: "已有多头注意止盈, 反弹常昙花一现。",
             0: "等待 OI 突破再行动。",
        },
        fmt_unit="百分比 (24h)",
    ),

    "liquidation_heat": FactorMeta(
        cn_name="清算热度 (Liquidation Heat)",
        category="衍生品风险",
        one_line="过去 24h 全网衍生品清算总额, 数额越大代表市场越脆弱/波动越大。",
        why_matters="单日清算 > $5亿 通常对应剧烈行情或短期底/顶。多空清算比可指示哪边更脆弱。",
        how_to_read={
             1: "空头清算占主导 (短期顶可能临近, 反向看空)。注: 此因子反向解读。",
            -1: "多头清算占主导 (短期底可能临近, 反向看多)。",
             0: "清算正常, 无极端单边。",
        },
        pm_action={
             1: "空头清算激增后常见短期回调, 减仓或对冲。",
            -1: "多头清算激增 = 市场清洗结束, 可考虑分批入场。",
             0: "正常市场, 无需操作。",
        },
        fmt_unit="单位:百万美元 (M$)",
    ),

    "long_short_ratio": FactorMeta(
        cn_name="多空账户比 (Long/Short Ratio)",
        category="衍生品情绪",
        one_line="主流交易所多头账户数 / 空头账户数, 散户情绪温度计。",
        why_matters="散户多空比 > 1 = 散户偏多; > 2.5 = 极度看多, 反向信号。",
        how_to_read={
             1: "比率 < 0.7, 散户极度看空, 反向看多机会。",
            -1: "比率 > 2.5, 散户极度看多, 反向看空机会。",
             0: "比率 0.7–2.5, 散户情绪正常。",
        },
        pm_action={
             1: "可考虑逆向建多, 但需要其他因子配合。",
            -1: "市场过热, 减仓避险。",
             0: "情绪中性, 看其他因子。",
        },
        fmt_unit="比率 (无单位)",
    ),

    # --- 全市场宏观 ---
    "btc_dominance_trend": FactorMeta(
        cn_name="BTC 占比趋势 (BTC Dominance Trend)",
        category="全市场宏观",
        one_line="BTC 市值占全加密市场比例, 占比上升 = 资金流向 BTC, 山寨季冷却。",
        why_matters="BTC 占比是配置 BTC vs 山寨的核心信号。占比 < 50% 历史上对应山寨季, > 60% BTC 独大。",
        how_to_read={
             1: "BTC 占比上升 (流入主流币), 减仓山寨增 BTC。",
            -1: "BTC 占比下降 (山寨季信号), 可考虑配置 ETH/SOL/MEME。",
             0: "占比稳定, 维持现有配置。",
        },
        pm_action={
             1: "降低山寨敞口, 转入 BTC; 山寨可能继续跑输。",
            -1: "山寨季可能开启, 适度增配 ETH/SOL 等龙头。",
             0: "保持现状。",
        },
        fmt_unit="百分比 (BTC mcap / total)",
    ),

    "total_mcap_momentum": FactorMeta(
        cn_name="全市场市值动量 (Total Mcap Momentum)",
        category="全市场宏观",
        one_line="加密总市值 7d 变化率, 衡量整体市场扩张/收缩。",
        why_matters="总市值上升代表新资金流入; 下降代表资金撤离。是 risk-on / risk-off 的最直接指标。",
        how_to_read={
             1: "总市值 7d > +5%, 整体市场扩张, risk-on。",
            -1: "总市值 7d < -5%, 整体收缩, risk-off。",
             0: "市值变动温和。",
        },
        pm_action={
             1: "可适度提升仓位, 顺势。",
            -1: "降低仓位, 防御。",
             0: "维持现有仓位。",
        },
        fmt_unit="百分比 (7d)",
    ),

    # --- DeFi 基本面 ---
    "defi_tvl_momentum": FactorMeta(
        cn_name="DeFi TVL 动量 (DeFi TVL Momentum)",
        category="DeFi 基本面",
        one_line="DeFi 全市场 TVL (锁仓量) 7d/30d 变化, 衡量 DeFi 生态扩张。",
        why_matters="TVL 上升 = 用户在存钱进 DeFi 协议, 是 DeFi 龙头币 (UNI/AAVE/CRV/PENDLE) 的基本面利好。",
        how_to_read={
             1: "TVL 7d > +5%, DeFi 扩张, DeFi 板块顺风。",
            -1: "TVL 7d < -5%, 资金撤离 DeFi, 警惕。",
             0: "TVL 持平。",
        },
        pm_action={
             1: "可考虑加配 DeFi 龙头币种。",
            -1: "减仓 DeFi 板块。",
             0: "维持现状。",
        },
        fmt_unit="百分比 (7d 变化)",
    ),

    # --- 叙事/热点 ---
    "trending_score": FactorMeta(
        cn_name="热门叙事强度 (Trending Score)",
        category="叙事热点",
        one_line="CoinGecko 全球搜索热度 Top 7 币种聚合, 反映散户/社区当下关注的代币。",
        why_matters="新出现的热门代币往往代表正在形成的叙事 (AI / RWA / MEME 等), 是早期布局信号。",
        how_to_read={
             1: "新代币进入 Top 7 / 同一类别 (AI / Meme) 占多数, 叙事在发酵。",
            -1: "Top 7 全是老币, 市场缺乏新故事, 关注度疲软。",
             0: "Trending 列表稳定, 无突变。",
        },
        pm_action={
             1: "研究热门叙事的标的, 判断是否值得早期布局 (小仓位试探)。",
            -1: "无新故事时, 资金集中流向老币 (BTC/ETH), 优先持有龙头。",
             0: "持续观察。",
        },
        fmt_unit="代币数 (Top 7)",
    ),

    # --- 宏观联动 ---
    "btc_nasdaq_corr": FactorMeta(
        cn_name="BTC 与纳指相关性 (BTC-Nasdaq Correlation)",
        category="宏观联动",
        one_line="BTC 与纳指 60 日滚动相关系数, 衡量 BTC 是否被作为风险资产交易。",
        why_matters="高相关 = BTC 跟随美股波动, 风险事件时容易共振下跌; 低相关 = BTC 独立行情, 受加密内部因素主导。",
        how_to_read={
             1: "相关性下降, BTC 独立行情, 加密内部因素 (ETF / 链上) 主导。",
            -1: "相关性升高 > 0.6, 风险事件下 BTC 易跟随美股下跌。",
             0: "相关性中等, 无极端共振。",
        },
        pm_action={
             1: "BTC 可作为分散仓位的有效工具。",
            -1: "美股大跌时同步对冲 BTC, 减少敞口。",
             0: "正常配置。",
        },
        fmt_unit="相关系数 (-1 至 +1)",
    ),

    "dxy_inverse": FactorMeta(
        cn_name="美元指数反向信号 (DXY Inverse)",
        category="宏观联动",
        one_line="DXY (美元指数) 7d 变化率, BTC 通常与 DXY 反向。",
        why_matters="DXY 走弱 = 美元贬值 = 资金流入风险资产 (含 BTC); DXY 走强 = 反之。这是宏观流动性的一个核心代理。",
        how_to_read={
             1: "DXY 7d 下跌 > 1%, 美元走弱, BTC 顺风。",
            -1: "DXY 7d 上涨 > 1%, 美元走强, BTC 承压。",
             0: "DXY 横盘。",
        },
        pm_action={
             1: "宏观顺风, 可适度加仓主流币。",
            -1: "宏观逆风, 控制仓位。",
             0: "维持现有仓位。",
        },
        fmt_unit="百分比 (DXY 7d 变化)",
    ),

    "btc_gold_corr": FactorMeta(
        cn_name="BTC 与黄金相关性 (BTC-Gold Correlation)",
        category="宏观联动",
        one_line="BTC 与黄金 60 日滚动相关系数, 高相关时 BTC 体现「数字黄金」属性。",
        why_matters="当避险资金同时流入 BTC + 黄金时, BTC 在宏观定位上更接近避险资产, 通常对应熊转牛或恐慌期。",
        how_to_read={
             1: "相关性 > 0.4, BTC 显现「数字黄金」属性, 避险资金同步流入。",
            -1: "相关性 < -0.3, BTC 与黄金背离, BTC 被作为纯风险资产。",
             0: "相关性中等, 无明显宏观叙事。",
        },
        pm_action={
             1: "BTC 在宏观风险事件中可能独立坚挺, 可保持配置。",
            -1: "BTC 风险属性明显, 跟随美股操作。",
             0: "无特殊操作。",
        },
        fmt_unit="相关系数 (-1 至 +1)",
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
