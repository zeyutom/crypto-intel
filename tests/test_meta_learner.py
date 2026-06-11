"""Unit tests for src.research.meta_learner — IC 回测的鲁棒性.

回归: 回填的合成快照里缺失因子值是 null, 曾导致 update-weights
连续多日崩溃 (TypeError: '<' not supported between NoneType).
"""
import sys
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.research.meta_learner import _spearman_rank_corr


# ────────────────────────────────────────────────────────────────────
#  _spearman_rank_corr 基础正确性
# ────────────────────────────────────────────────────────────────────

def test_spearman_perfect_monotone():
    """完全单调 → ±1."""
    x = [1, 2, 3, 4, 5, 6]
    assert abs(_spearman_rank_corr(x, [10, 20, 30, 40, 50, 60]) - 1.0) < 1e-9
    assert abs(_spearman_rank_corr(x, [60, 50, 40, 30, 20, 10]) + 1.0) < 1e-9


def test_spearman_constant_column_is_zero():
    """常数列无信息 → 0."""
    assert _spearman_rank_corr([1, 1, 1, 1, 1], [1, 2, 3, 4, 5]) == 0.0


# ────────────────────────────────────────────────────────────────────
#  回归: None 因子值 (回填快照) 不再崩
# ────────────────────────────────────────────────────────────────────

def test_spearman_none_pairs_dropped_not_crash():
    """含 None 的配对成对剔除后照常计算 (曾抛 TypeError)."""
    x = [1, None, 2, 3, None, 4, 5, 6]
    y = [2, 9.0, 4, 6, 1.0, 8, 10, 12]
    ic = _spearman_rank_corr(x, y)
    assert abs(ic - 1.0) < 1e-9  # 剔除 None 后剩完全单调的 6 对


def test_spearman_all_none_returns_zero():
    """全 None / 剔除后不足 5 对 → 0.0, 不抛异常."""
    assert _spearman_rank_corr([None] * 8, list(range(8))) == 0.0
    assert _spearman_rank_corr([1, None, None, None, None, 2],
                               [1, 2, 3, 4, 5, 6]) == 0.0


def test_spearman_none_in_returns_side():
    """y 侧含 None 同样剔除."""
    x = [1, 2, 3, 4, 5, 6, 7]
    y = [1, None, 3, 4, 5, 6, 7]
    ic = _spearman_rank_corr(x, y)
    assert abs(ic - 1.0) < 1e-9
