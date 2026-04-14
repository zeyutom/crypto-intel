"""因子有效性监控 (IC / IR)。

PoC 阶段: 需要足够历史数据才能真正计算;这里实现框架并在数据不足时返回占位状态。
"""
import json
import pandas as pd
from ..config import CFG
from ..db import query_df, upsert_review
from ..utils import now_iso


def run() -> list[dict]:
    win = CFG["review"]["ic_monitor"]["rolling_window"]
    ir_floor = CFG["review"]["ic_monitor"]["ir_floor"]
    ts = now_iso()
    results = []

    factors = query_df("SELECT DISTINCT factor FROM factors")["factor"].tolist()
    for factor in factors:
        # 因子时序
        fdf = query_df(
            "SELECT ts, asset_id, raw_value FROM factors WHERE factor=? ORDER BY ts",
            (factor,),
        )
        if len(fdf) < 5:
            results.append({
                "ts": ts, "check_name": "ic_monitor", "subject": factor,
                "severity": "OK",
                "detail": json.dumps({
                    "status": "insufficient_history",
                    "observations": int(len(fdf)),
                    "note": "需累积至少 5+ 天数据方可进行 IC 回归"
                }),
            })
            continue

        # 对应 asset 价格变化
        fdf["ts"] = pd.to_datetime(fdf["ts"])
        # 占位 IC (真实实现需对齐未来 N 日收益)
        series = fdf["raw_value"].astype(float)
        vol = float(series.std()) if len(series) > 1 else 0
        mean = float(series.mean())
        ir_proxy = abs(mean) / vol if vol > 0 else 0

        severity = "OK" if ir_proxy >= ir_floor else "WARN"
        results.append({
            "ts": ts, "check_name": "ic_monitor", "subject": factor,
            "severity": severity,
            "detail": json.dumps({
                "observations": int(len(series)),
                "mean": mean, "stdev": vol,
                "ir_proxy": round(ir_proxy, 3),
                "ir_floor": ir_floor,
                "note": "当前为 placeholder IR;生产版应使用未来 N 日收益对齐回归",
            }),
        })
    upsert_review(results)
    return results
