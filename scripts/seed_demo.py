"""向 raw_metrics 注入一组合理的演示数据,用于在没有网络时看到完整报表效果。
数据只作示例,不代表真实市场状态。
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import random
from datetime import datetime, timedelta, timezone
from src.db import init_db, upsert_raw
from src.config import CFG

random.seed(42)
now = datetime.now(timezone.utc)

def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

# 基础价格 (用接近 2026-04 真实水位的虚构数字, 用户应理解这是 DEMO 不是真实行情)
base_prices = {"bitcoin": 74_500, "ethereum": 2_380, "solana": 86.2, "bnb": 618.5}
change_24h = {"bitcoin": 5.32, "ethereum": 9.08, "solana": 5.39, "bnb": 3.52}
# 不同源给不同的细微偏差,用于价格交叉验证
src_offsets = {"coingecko": 0.0, "binance": 0.0008, "coinbase": -0.0012}

rows = []
ts = iso(now)
for asset_id, price in base_prices.items():
    for src, off in src_offsets.items():
        # coinbase 不上 BNB
        if src == "coinbase" and asset_id == "bnb":
            continue
        rows.append({"ts": ts, "source": src, "asset_id": asset_id,
                     "metric": "price_usd", "value": round(price * (1 + off), 4),
                     "value_text": None})
    # 24h change + volume (binance 唯一源)
    rows.append({"ts": ts, "source": "binance", "asset_id": asset_id,
                 "metric": "change_24h_pct", "value": change_24h[asset_id], "value_text": None})
    rows.append({"ts": ts, "source": "binance", "asset_id": asset_id,
                 "metric": "volume_24h_usd",
                 "value": random.uniform(1e9, 3e10), "value_text": None})

# Funding rates (8h) — 当前偏温和 (近似真实)
fundings = {"bitcoin": 0.00007, "ethereum": 0.00006, "solana": 0.00009, "bnb": 0.00004}
for asset_id, f in fundings.items():
    rows.append({"ts": ts, "source": "binance", "asset_id": asset_id,
                 "metric": "funding_rate_8h", "value": f, "value_text": None})

# Stablecoin 流通 30 日序列 (USDT+USDC, 趋势向上)
start_total = 205_000_000_000  # 约 $205B
for i in range(30, 0, -1):
    day = now - timedelta(days=i)
    # 每日 +0.15% 的 mint(近一周略有波动)
    growth = 0.0015 * (30 - i) + random.uniform(-0.0005, 0.001)
    rows.append({"ts": iso(day.replace(hour=0, minute=0, second=0)),
                 "source": "defillama", "asset_id": "stablecoins",
                 "metric": "total_circulating_usd",
                 "value": start_total * (1 + growth), "value_text": None})
# 最新一条
rows.append({"ts": ts, "source": "defillama", "asset_id": "stablecoins",
             "metric": "total_circulating_usd", "value": 214_300_000_000, "value_text": None})

# Fear & Greed — 当前 21 极度恐慌 (匹配近期真实状态), 历史从中性滑入恐慌
fg_series = [55, 52, 48, 45, 42, 40, 38, 42, 45, 48, 45, 42, 38, 35, 32, 30, 28, 32, 35, 30, 28, 25, 22, 20, 18, 22, 25, 23, 20, 21]
classes = ["Neutral", "Neutral", "Fear", "Fear", "Fear", "Fear", "Fear", "Fear", "Neutral",
           "Neutral", "Neutral", "Fear", "Fear", "Fear", "Fear", "Fear", "Fear", "Fear", "Fear",
           "Fear", "Fear", "Extreme Fear", "Extreme Fear", "Extreme Fear", "Extreme Fear",
           "Extreme Fear", "Fear", "Extreme Fear", "Extreme Fear", "Extreme Fear"]
for i, (v, cls) in enumerate(zip(fg_series, classes)):
    day = now - timedelta(days=29 - i)
    rows.append({"ts": iso(day.replace(hour=0, minute=0, second=0)),
                 "source": "feargreed", "asset_id": "market",
                 "metric": "fear_greed_index", "value": v, "value_text": cls})

# BTC ETF Net Flow (近 10 日, 单位 $M) - 恐慌区间机构混合行为, 末段开始抄底
etf_flows = [-180, -240, -95, -310, -55, 85, -120, 145, 220, 380]
for i, v in enumerate(etf_flows):
    day = now - timedelta(days=9 - i)
    rows.append({"ts": iso(day.replace(hour=23, minute=0, second=0)),
                 "source": "farside_etf", "asset_id": "bitcoin",
                 "metric": "etf_net_flow_musd", "value": v, "value_text": None})

init_db()
n = upsert_raw(rows)
# 显式标记: DEMO 模式 (报表用此判断, 比启发式更可靠)
upsert_raw([{"ts": ts, "source": "_meta", "asset_id": "_demo_marker",
             "metric": "is_demo", "value": 1.0, "value_text": "seeded by seed_demo.py"}])
print(f"Seeded {n} demo rows.")
