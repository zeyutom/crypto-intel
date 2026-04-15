"""月度新数据源发现 Agent。

让 Claude 搜: 2026 年 crypto 世界有什么新出现的免费/高价值 API?
它评估 + 提议接入, 写成 `data/proposals/source_{name}_{date}.md` 和 adapter 代码草稿。
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from ..db import insert_evolution_log
from ..config import ROOT
from ..utils import setup_logger
from ._claude_runner import run_claude

log = setup_logger("source_discoverer", "INFO")

PROPOSAL_DIR = ROOT / "data" / "proposals"
PROPOSAL_DIR.mkdir(parents=True, exist_ok=True)


SYSTEM = """你是一名 crypto 投研数据专家。

任务: 通过 web_search 扫一扫 2025/2026 年新出现或被低估的 crypto 数据源,
找 1-3 个值得接入的 **免费或低成本** API, 并写出评估 + 接入建议。

评估标准:
1. **信号含量**: 提供的数据与现有 12 个源不重复, 且有明显 alpha 潜力
2. **成本**: 优先免费; 付费需 < $50/月且有免费额度
3. **稳定性**: API 文档清晰, 更新及时, 开发者评价正面
4. **实现难度**: 1-2 小时能接入 (标准 REST JSON, 最好)

现有 12 源参考 (不要重复):
coingecko / binance / okx / coinbase / defillama / feargreed /
farside_etf / cg_global / cg_trending / coinglass / yfinance_macro / defillama_extra

重点覆盖这些缺口:
- 链上深度数据 (尤其不是 ETH / 更 BTC-native 的)
- 社交/KOL (我们目前只有 F&G)
- 事件/新闻流 (CryptoPanic 还没接)
- 期权 (Laevitas / Deribit greeks)
- DeFi 收益率/利率 (新 LRT/restaking)

输出 Markdown, 每个候选一节:
## 🔍 候选源: {name}
- URL + 免费额度
- 提供的独特数据 (不与现有源重复)
- 预期信号 / 可衍生因子
- 接入代码草稿 (Python adapter, 参考 src/adapters/cg_global.py 结构)"""


def run_source_discovery() -> dict:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    user_prompt = ("请通过 web_search 找 2025-2026 年新出现或我们可能错过的免费 crypto API, "
                   "给 1-3 个候选的接入评估和代码草稿。")

    log.info("Running source discovery...")
    result = run_claude(user_prompt, system=SYSTEM, timeout=900)
    if not result["ok"]:
        return result

    md = result["markdown"]
    out_path = PROPOSAL_DIR / f"source_discovery_{today}.md"
    out_path.write_text(md, encoding="utf-8")
    log.info(f"Source discovery saved: {out_path}")

    insert_evolution_log(
        kind="source_discovery",
        title=f"新数据源发现 {today}",
        content=md,
        action="pending",
        meta=json.dumps({"file": str(out_path)}),
    )
    return {"ok": True, "file": str(out_path), "chars": len(md)}
