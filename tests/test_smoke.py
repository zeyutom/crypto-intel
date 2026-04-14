"""冒烟测试: 不联网的情况下确保 import 不挂、schema 能建。"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from src.config import CFG, load_config
from src.db import init_db, get_conn
from src.adapters import ALL_ADAPTERS
from src.factors import ALL_FACTORS


def test_config():
    assert "universe" in CFG
    assert len(CFG["universe"]) >= 1


def test_init_db():
    init_db()
    with get_conn() as c:
        tables = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )]
    for t in ("raw_metrics", "factors", "signals", "reviews", "events"):
        assert t in tables


def test_adapters_loaded():
    assert len(ALL_ADAPTERS) == 6


def test_factors_loaded():
    assert len(ALL_FACTORS) == 5


if __name__ == "__main__":
    test_config(); test_init_db(); test_adapters_loaded(); test_factors_loaded()
    print("All smoke tests passed.")
