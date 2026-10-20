"""Streamlit 页面的静态检查 (只解析源码, 不 import 页面 — 页面 import 即渲染)。

回归:
  * C12: pages/0 控制中心漏调 require_password, 线上任何人拿到链接就能跑 nightly /
    pip install / 下载全部快照 (pages/9 早先也漏过一次)。这里要求每个页面都在
    set_page_config 之后、任何其它 st.* 调用之前调 require_password()。
  * C43: 控制中心 "最近 7 天 nightly" 用 glob("*.log"), 本地残留的 watchdog.log /
    launchd*.log 反向排序排在日期前面, 挤掉 3 天真实运行记录。
  * C13: 因子有效性页把被降权的因子标成 "默认", 并声称 IR < 0 的因子权重为 0。
  * C14: 历史复盘页混入合成快照、按 symbol 建键 — 逻辑已移到 src.research.dashboard
    (见 tests/test_dash_history.py), 这里只确认页面确实调用了它们。
"""
from __future__ import annotations
import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGES_DIR = ROOT / "pages"
PAGE_FILES = sorted(PAGES_DIR.glob("*.py"))
CONTROL_PAGE = next((p for p in PAGE_FILES if p.name.startswith("0_")), None)
FACTOR_IR_PAGE = next((p for p in PAGE_FILES if p.name.startswith("7_")), None)
HISTORY_PAGE = next((p for p in PAGE_FILES if p.name.startswith("9_")), None)


def _parse(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _root_name(node: ast.AST) -> str | None:
    """st.session_state.get → "st"; foo() → None。"""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _is_require_password_stmt(stmt: ast.stmt) -> bool:
    return (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
            and isinstance(stmt.value.func, ast.Name)
            and stmt.value.func.id == "require_password")


def test_pages_dir_found():
    assert len(PAGE_FILES) >= 8, PAGE_FILES
    assert CONTROL_PAGE is not None and FACTOR_IR_PAGE is not None and HISTORY_PAGE is not None


@pytest.mark.parametrize("path", PAGE_FILES + [ROOT / "streamlit_app.py"],
                         ids=lambda p: p.name)
def test_every_page_gates_with_require_password(path):
    tree = _parse(path)
    imported = any(
        isinstance(n, ast.ImportFrom) and n.module == "src.dashboard_utils"
        and any(a.name == "require_password" for a in n.names)
        for n in ast.walk(tree)
    )
    assert imported, f"{path.name} 没有 from src.dashboard_utils import require_password"

    gate_idx = next((i for i, s in enumerate(tree.body) if _is_require_password_stmt(s)), None)
    assert gate_idx is not None, f"{path.name} 顶层没有调用 require_password()"

    # 门之前不能有 set_page_config 以外的 st.* 调用 (否则 UI / 按钮在密码框之前就渲染了)
    for stmt in tree.body[:gate_idx]:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for node in ast.walk(stmt):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and _root_name(node.func) == "st"
                    and node.func.attr != "set_page_config"):
                pytest.fail(f"{path.name}:{node.lineno} 在 require_password() 之前调用了 "
                            f"st.{node.func.attr}")


def _nightly_log_glob_pattern() -> str:
    """控制中心里 log_dir.glob(<pattern>) 的 pattern 字面量。"""
    pats = [
        n.args[0].value for n in ast.walk(_parse(CONTROL_PAGE))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "glob" and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "log_dir" and n.args
        and isinstance(n.args[0], ast.Constant)
    ]
    assert len(pats) == 1, pats
    return pats[0]


def test_control_page_nightly_table_only_lists_dated_logs(tmp_path):
    for name in ["watchdog.log", "launchd.log", "launchd.err.log",
                 "2026-10-19.failures.json"]:
        (tmp_path / name).write_text("x")
    dated = [f"2026-10-{d:02d}.log" for d in range(11, 20)]
    for name in dated:
        (tmp_path / name).write_text("Nightly 完成")

    # 与页面同样的选取方式: 反向排序取 7 个
    picked = sorted(tmp_path.glob(_nightly_log_glob_pattern()), reverse=True)[:7]
    assert [p.name for p in picked] == sorted(dated, reverse=True)[:7]


def test_factor_ir_page_labels_by_multiplier_and_states_real_rule():
    src = FACTOR_IR_PAGE.read_text(encoding="utf-8")
    # 旧文案: 负 IR 因子"权重为 0 (淘汰)" — 实际最低 0.5× 默认, 从不归零
    assert "权重为 0" not in src and "自动淘汰" not in src
    # 旧逻辑: 只有 eff > default 才算 IR 动态, 被降权的因子全标成默认
    assert "if eff_w > default_w" not in src
    assert "ir_weight_source(" in src and "ir_weight_rule_text()" in src
    # 排行用信号合成同口径的 IR (get_latest_ir_weights 按因子平均), 而不是 max
    assert "get_latest_ir_weights" in src and "factor_mean_ir(" in src


def test_history_page_uses_tested_helpers():
    src = HISTORY_PAGE.read_text(encoding="utf-8")
    assert "load_real_snapshots(" in src and "forward_returns(" in src
    # 旧实现按 symbol 建 dict / 按 top_n 加权累计命中率
    assert 'c["symbol"]: c.get("price")' not in src
    assert "cumulative_total += top_n" not in src
    assert "全市场中位" not in src
    # 出场快照是 schema 2 全量宇宙时 (10-06 ~ 10-11 + 7 天) 没有 "仍在前 200" 的幸存者偏差:
    # 旧说明对所有截断入场日一概这么写; 现在按 truncated_baseline_counts 分开计数
    assert "且只含 N 天后仍在前 200 的币" not in src
    assert "truncated_baseline_counts(" in src and "exit_snapshot_dates(" in src
