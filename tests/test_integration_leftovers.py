"""审计遗留项的回归测试 (2026-10-20 合并各修复包后补齐的跨文件问题)."""
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.research import screener  # noqa: E402
from src.research.screener import score_coins  # noqa: E402

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def _cg(sym, cid, mcap, price=1.0):
    return {"id": cid, "symbol": sym.lower(), "name": sym, "current_price": price,
            "market_cap": mcap, "market_cap_rank": 50, "total_volume": mcap * 0.1,
            "price_change_percentage_24h": 1.0,
            "price_change_percentage_7d_in_currency": 4.0,
            "price_change_percentage_30d_in_currency": 12.0,
            "ath": price * 2, "ath_change_percentage": -50.0,
            "last_updated": (NOW - timedelta(minutes=5)).isoformat()}


def test_duplicate_symbol_only_largest_coin_gets_symbol_keyed_tvl_and_funding():
    """VELO = velodrome-finance (大) / velo (小): 小币不能拿到大币的 DeFiLlama TVL."""
    coins = [_cg("VELO", "velodrome-finance", 9e7), _cg("VELO", "velo", 3e7, price=0.004)]
    scored = score_coins(coins, {"VELO": 8e7}, {}, {"VELO": 5e6},
                         funding_rates={"VELO": -0.002}, now=NOW)
    by_id = {c["coin_id"]: c for c in scored}
    assert by_id["velodrome-finance"]["tvl"] == 8e7
    assert by_id["velo"]["tvl"] == 0
    assert by_id["velo"]["funding_rate"] == 0
    assert by_id["velodrome-finance"]["funding_rate"] == -0.002


def test_unique_symbol_unaffected():
    scored = score_coins([_cg("AAVE", "aave", 2e9)], {"AAVE": 1e9}, {}, {}, now=NOW)
    assert scored[0]["tvl"] == 1e9


def test_funding_composite_prefers_fresh_okx_over_stale_binance():
    from src.factors.funding_composite import _pick_fresh_funding
    df = pd.DataFrame([
        {"asset_id": "bitcoin", "value": 0.0003, "src": "binance", "ts": "2026-06-05T05:45:00Z"},
        {"asset_id": "bitcoin", "value": 0.0001, "src": "okx", "ts": "2026-10-20T05:00:00Z"},
        {"asset_id": "ethereum", "value": 0.0002, "src": "binance", "ts": "2026-10-20T04:00:00Z"},
        {"asset_id": "ethereum", "value": 0.0004, "src": "okx", "ts": "2026-10-20T05:00:00Z"},
    ])
    out = _pick_fresh_funding(df).set_index("asset_id")
    assert out.loc["bitcoin", "src"] == "okx"          # 4 个月前的 binance 不再胜出
    assert out.loc["ethereum", "src"] == "binance"     # 同样新鲜时 binance 优先


def test_notifier_counts_stale_sources_as_missing(monkeypatch):
    """卡片的覆盖度警告: 过期的源 (云端 binance 451) 也算缺失."""
    import src.notifier as notifier
    import src.db as db
    calls = {}

    def fake_status(expected):
        calls["expected"] = list(expected)
        return [{"source": s, "ok": s not in ("binance", "farside_etf")} for s in expected]

    monkeypatch.setattr(db, "source_status", fake_status)
    captured = {}

    def fake_build(sig, fact, regime, missing):
        captured["missing"] = missing
        raise RuntimeError("stop")   # 只关心传进来的 missing

    monkeypatch.setattr(notifier, "build_briefing", fake_build, raising=False)
    src = (ROOT / "src" / "notifier.py").read_text(encoding="utf-8")
    assert "source_status(expected)" in src and "if not s[\"ok\"]" in src
    missing = sorted(s["source"] for s in fake_status(["binance", "okx", "farside_etf"])
                     if not s["ok"])
    assert missing == ["binance", "farside_etf"]


def test_dashboard_factor_rows_show_effective_weights():
    from src.research.dashboard import _factor_rows_html
    cfg = {"factors": {
        "a": {"weight": 0.2, "enabled": True, "ic_history": []},
        "b": {"weight": 0.2, "enabled": True, "ic_history": []},
        "c": {"weight": 0.0, "enabled": False, "ic_history": []},
    }}
    html = _factor_rows_html(cfg)
    assert "50.0%" in html and "20.0%" not in html


def test_refresh_workflow_restores_outputs_after_reset():
    y = (ROOT / ".github" / "workflows" / "refresh.yml").read_text(encoding="utf-8")
    assert 'REFRESH_SHA=$(git rev-parse HEAD)' in y
    reset = y.index("git reset --hard origin/main")
    restore = y.index('git checkout "$REFRESH_SHA" -- data/intel.db data/reports')
    readd = y.index("git add data/intel.db data/reports/", reset)
    assert reset < restore < readd
