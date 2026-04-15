"""月度因子提议 Agent。

职责:
  1. 读 factor_performance 最新 IC/IR + 已有因子列表
  2. 让 Claude 提 2-3 个新因子 (公式 + 代码)
  3. 代码写到 data/proposals/factor_{name}_{date}.py
  4. 日志入 evolution_log, kind=factor_proposal, action=pending
  5. 飞书通知 PM review

PM 若觉得可用, 手工把代码从 data/proposals/ 移到 src/factors/ + 注册到 __init__.py。
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from ..db import query_df, insert_evolution_log
from ..config import ROOT
from ..utils import setup_logger
from ._claude_runner import run_claude

log = setup_logger("factor_proposer", "INFO")

PROPOSAL_DIR = ROOT / "data" / "proposals"
PROPOSAL_DIR.mkdir(parents=True, exist_ok=True)

SYSTEM = """你是一名量化因子开发专家。

目标: 基于当前因子表现 (有些 IR 高, 有些失效), 为这个 crypto intel 系统提议 2-3 个**新因子**。

要求:
1. 每个提议有: 因子名 (snake_case) / 类别 / 一句话定义 / 数学公式 / 可得数据源
2. 必须只用系统**已有的数据源** (不让我去加新数据源):
   coingecko, binance, okx, coinbase, defillama, feargreed, farside_etf,
   cg_global, cg_trending, coinglass, yfinance_macro, defillama_extra
3. 与现有 15 个因子**不重复** (不要再来一个恐慌指数)
4. 必须可以被量化: 有明确的阈值规则决定 signal (+1/-1/0)
5. 每个因子给出 Python 代码 (完整 compute() 函数), 格式参考:

```python
import json
from ..db import query_df
from ..utils import now_iso

FACTOR = "my_new_factor"

def compute() -> list[dict]:
    df = query_df(\"\"\"...\"\"\")
    if df.empty: return []
    # 计算 raw_value, signal
    return [{"ts": now_iso(), "asset_id": "market", "factor": FACTOR,
             "raw_value": v, "zscore": None, "signal": s, "confidence": c,
             "meta": json.dumps({...})}]
```

输出 Markdown, 每个因子一节,section 含: 定义 / 公式 / 代码 / 预期 IR。"""


def _gather_current_state() -> str:
    # 最新因子表现
    perf = query_df(
        """SELECT factor, MAX(ir) AS best_ir, MAX(n_obs) AS obs
           FROM factor_performance
           WHERE date = (SELECT MAX(date) FROM factor_performance)
           GROUP BY factor ORDER BY best_ir DESC"""
    )
    # 现有因子
    all_factors = query_df("SELECT DISTINCT factor FROM factors")

    parts = ["## 现有因子表现 (最近一次 backtest)\n"]
    if not perf.empty:
        for _, r in perf.iterrows():
            parts.append(f"- {r['factor']}: IR = {r['best_ir']:.3f} (n={int(r['obs'])})")
    else:
        parts.append("(无 backtest 历史, 仅列已有因子)")

    parts.append(f"\n## 已有因子列表 ({len(all_factors)} 个)\n")
    parts.extend(f"- {f}" for f in all_factors["factor"].tolist())
    return "\n".join(parts)


def run_factor_proposal() -> dict:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    user_prompt = (_gather_current_state() + "\n\n---\n\n"
                   "请基于以上现状, 提议 2-3 个新因子 "
                   "(如果某现有因子的 IR 很高, 可以提议一个相关的组合/衍生因子)。")

    log.info("Running factor proposal...")
    result = run_claude(user_prompt, system=SYSTEM, timeout=600)
    if not result["ok"]:
        return result

    md = result["markdown"]
    out_path = PROPOSAL_DIR / f"factor_proposal_{today}.md"
    out_path.write_text(md, encoding="utf-8")
    log.info(f"Proposal saved: {out_path}")

    insert_evolution_log(
        kind="factor_proposal",
        title=f"因子提议 {today}",
        content=md,
        action="pending",
        meta=json.dumps({"file": str(out_path)}),
    )
    return {"ok": True, "file": str(out_path), "chars": len(md)}
