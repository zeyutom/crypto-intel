"""`python -m src.cli dashboard` 统一仪表盘 HTML (src.research.dashboard) 单测。

回归 (C46):
  * regime 读快照顶层 "regime" — 两种 schema 都没有这个键 (schema 2 存在 _meta.regime),
    页面永远显示 "❓ 未知";
  * 因子列表不看 enabled / locked: dev_activity (停用) 显示 "IC +0.000",
    volume_turnover (人工锁定) 像正常因子一样列出;
  * "N 天快照" 用文件数 (319), 含 99 个合成日期和同日多文件 (真实只有 143 天)。
全部写 tmp_path (DATA_DIR / META_DIR / REPORT_DIR 都 monkeypatch), 不碰真实 data/。
"""
from __future__ import annotations
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.research import dashboard as dash


def _snapshot(date: str, n: int, sym_prefix: str = "C", **meta) -> dict:
    coins = [{"symbol": f"{sym_prefix}{i}", "coin_id": f"{sym_prefix.lower()}{i}",
              "price": 1.0 + i, "market_cap": 1e9, "composite_score": 1.0 - i * 0.001,
              "change_30d": 1.0, "f_momentum_7d": 0.1} for i in range(n)]
    return {"timestamp": f"{date}T22:00:00+00:00", "date": date,
            "factor_weights": {}, "_meta": meta, "coins": coins}


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    meta = tmp_path / "meta"
    meta.mkdir()
    monkeypatch.setattr(dash, "DATA_DIR", tmp_path)
    monkeypatch.setattr(dash, "META_DIR", meta)
    monkeypatch.setattr(dash, "REPORT_DIR", tmp_path / "reports")
    return tmp_path


def _write(meta: pathlib.Path, name: str, data: dict) -> None:
    (meta / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_dashboard_regime_factor_flags_and_real_day_count(data_dir):
    meta = data_dir / "meta"
    # 2 个真实日期 (10-13 有两个文件) + 合成快照, 文件名排序最后的是合成 _0000
    _write(meta, "snapshot_20261012_0000.json",
           _snapshot("2026-10-12", 29, "SYN", source="backfill", synthetic=True))
    _write(meta, "snapshot_20261013_0117.json",
           _snapshot("2026-10-13", 120, schema=2, regime="volatile"))
    _write(meta, "snapshot_20261013_2324.json",
           _snapshot("2026-10-13", 120, schema=2, regime="volatile"))
    _write(meta, "snapshot_20261014_2215.json",
           _snapshot("2026-10-14", 120, "REAL", schema=2, regime="sideways"))
    _write(meta, "snapshot_20261015_0000.json",
           _snapshot("2026-10-15", 29, "SYN", source="backfill", synthetic=True))
    # factor_config 不带 regime 键 (该键正在被移除)
    _write(meta, "factor_config.json", {"factors": {
        "momentum_7d": {"weight": 0.2, "enabled": True,
                        "ic_history": [{"date": "2026-10-19", "ic": 0.08}]},
        "dev_activity": {"weight": 0.0, "enabled": False, "ic_history": []},
        "volume_turnover": {"weight": 0.0, "enabled": False, "locked": True,
                            "lock_reason": "7 日 IC 稳健为负 <人工关闭>",
                            "ic_history": [{"date": "2026-10-19", "ic": -0.168}]},
    }})

    out = dash.generate_dashboard()
    assert out.parent == data_dir / "reports"
    page = out.read_text(encoding="utf-8")

    assert "➡️ 震荡" in page and "❓ 未知" not in page
    # Top-10 来自最新真实快照, 不是更晚的合成 _0000
    assert "REAL0" in page and "SYN0" not in page
    assert "2026-10-14 · 2 天真实快照" in page

    assert "IC +0.080" in page
    assert "IC n/a" in page                      # dev_activity 没有 IC 历史
    assert "IC +0.000" not in page
    assert page.count("停用") == 2               # dev_activity + volume_turnover
    assert "🔒 锁定" in page
    assert "7 日 IC 稳健为负 &lt;人工关闭&gt;" in page   # 原因展示且转义


def test_snapshot_regime_fallback_chain():
    assert dash._snapshot_regime({"_meta": {"regime": "bear"}, "regime": "bull"},
                                 {"regime": "volatile"}) == "bear"
    assert dash._snapshot_regime({"regime": "bull"}, {"regime": "volatile"}) == "bull"
    assert dash._snapshot_regime({"_meta": {}}, {"regime": "volatile"}) == "volatile"
    assert dash._snapshot_regime(None, {}) == "unknown"
    assert dash._snapshot_regime({}, None) == "unknown"


def test_dashboard_without_real_snapshots_falls_back(data_dir):
    meta = data_dir / "meta"
    _write(meta, "snapshot_20261012_0000.json",
           _snapshot("2026-10-12", 29, "SYN", source="backfill", synthetic=True))
    page = dash.generate_dashboard().read_text(encoding="utf-8")
    assert "0 天真实快照" in page
    assert "SYN0" in page                         # 没有真实快照时仍展示最新文件
    assert "❓ 未知" in page
