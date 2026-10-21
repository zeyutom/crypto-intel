"""screener.detect_anomalies 规则 5: 相对阈值 (2026-10-20 复核).

权重归一化方式 / 启用因子变化会让综合分整体平移; 固定 0.55 阈值在关掉两个
低覆盖因子后命中数从 0 跳到 41, 淹没了真正的异动。
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.research.screener import detect_anomalies  # noqa: E402


def _coins(scores, mcap=5e8):
    return [{"symbol": f"C{i}", "composite_score": s, "market_cap": mcap}
            for i, s in enumerate(scores)]


def _hits(signals):
    return [s["symbol"] for s in signals if s["type"] == "high_score_small_cap"]


def test_high_score_small_cap_is_top_decile_regardless_of_scale():
    scores = [0.30 + 0.004 * i for i in range(100)]          # 0.30 .. 0.696
    lo = _hits(detect_anomalies(sorted(_coins(scores), key=lambda c: -c["composite_score"]), {}))
    hi = _hits(detect_anomalies(sorted(_coins([s * 1.5 for s in scores]),
                                       key=lambda c: -c["composite_score"]), {}))
    assert lo == hi                     # 整体平移/缩放不改变命中
    assert 5 <= len(lo) <= 10


def test_large_caps_never_flagged():
    scores = [0.9] * 3 + [0.1] * 97
    sig = detect_anomalies(_coins(scores, mcap=5e10), {})
    assert _hits(sig) == []
