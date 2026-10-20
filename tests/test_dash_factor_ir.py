"""pages/7 因子有效性的 IR 展示纯函数 (src.research.dashboard) 单测。

回归 (C13): composite 自 221c827 起 IR 调权可升可降 (default × clamp(1 + 0.25·IR, 0.5, 1.5)),
页面仍按 eff > default 才标 "IR 动态" → 9 个有 IR 的因子里 8 个被降权却显示 "默认";
说明文字写 "IR < 0 的因子权重为 0 (淘汰)", 实际最低保留 0.5× 默认;
排行用全部行的 max IR, 而调权用的是 clip 后的均值 (fear_greed_reversal max +0.31 vs 用的 -0.42)。
不碰真实 DB: monkeypatch get_latest_ir_weights。
"""
from __future__ import annotations
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.research import dashboard as dash
from src.review import backtest as bt
from src.signals import composite as comp


IR_W = {
    ("total_mcap_momentum", None): -2.0,
    ("coinbase_premium", "bitcoin"): -1.8,
    ("coinbase_premium", "ethereum"): -1.6,
    ("btc_gold_corr", None): 0.24,
    ("fear_greed_reversal", "bitcoin"): -0.1,
    ("fear_greed_reversal", "ethereum"): -0.74,
}


def test_factor_mean_ir_averages_per_factor_like_composite():
    m = dash.factor_mean_ir(IR_W)
    assert m["coinbase_premium"] == pytest.approx(-1.7)
    assert m["fear_greed_reversal"] == pytest.approx(-0.42)
    assert m["total_mcap_momentum"] == pytest.approx(-2.0)
    assert "funding_composite" not in m
    assert dash.factor_mean_ir({}) == {}


def test_labels_match_composite_effective_weights(monkeypatch):
    """页面标签必须和 composite 实际算出的权重一致: 降权 → ↓, 不归零; 无 IR → 默认。"""
    monkeypatch.setattr(bt, "get_latest_ir_weights", lambda: dict(IR_W))
    eff = comp._effective_weights()
    used = dash.factor_mean_ir(IR_W)

    def label(f):
        return dash.ir_weight_source(comp.DEFAULT_WEIGHTS[f], eff[f], f in used)

    mult, src = label("total_mcap_momentum")
    assert mult == pytest.approx(comp.IR_MULT_MIN)
    assert src == f"🎯 IR ↓ ×{comp.IR_MULT_MIN:.2f}"
    assert eff["total_mcap_momentum"] > 0                     # 失效因子降权, 不是 0

    mult, src = label("coinbase_premium")
    assert mult == pytest.approx(1 + comp.IR_SLOPE * -1.7, abs=1e-3)
    assert src.startswith("🎯 IR ↓ ×")

    mult, src = label("btc_gold_corr")
    assert mult == pytest.approx(1.06, abs=1e-3)
    assert src == "🎯 IR ↑ ×1.06"

    mult, src = label("funding_composite")                    # 无可用 IR
    assert mult == pytest.approx(1.0)
    assert src == "🔧 默认 (无可用 IR)"


def test_ir_weight_source_edges():
    assert dash.ir_weight_source(1.0, 1.0, True) == (1.0, "🎯 IR ×1.00")
    assert dash.ir_weight_source(0.8, 0.79995, True)[1] == "🎯 IR ×1.00"   # round 误差
    assert dash.ir_weight_source(0.0, 0.0, True) == (None, "🔧 默认 (无可用 IR)")
    assert dash.ir_weight_source(1.2, 0.6, True)[1] == "🎯 IR ↓ ×0.50"


def test_ir_verdict_uses_used_ir():
    assert dash.ir_verdict(None) == "⚪ 数据不足"
    assert dash.ir_verdict(1.2) == "🟢 优秀"
    assert dash.ir_verdict(0.5) == "🟢 可用"
    assert dash.ir_verdict(0.0) == "🟡 边缘"
    assert dash.ir_verdict(-0.42).startswith("🔴 失效")


def test_rule_text_states_real_bounds():
    text = dash.ir_weight_rule_text()
    assert f"clamp(1 + {comp.IR_SLOPE} × IR均值, {comp.IR_MULT_MIN}, {comp.IR_MULT_MAX})" in text
    assert f"最低保留 {comp.IR_MULT_MIN}× 默认权重" in text
    assert "不会归零" in text
    assert "权重为 0" not in text
