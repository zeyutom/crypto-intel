"""nightly.yml 失败告警步骤: 依赖装不上时也要能推到飞书.

回归 (2026-10-20 运维审计复核):
- "Alert on job failure or cancel" 只 import src.notifier (要 httpx/pandas/src.db…);
  告警文案自己列了 "依赖安装失败", 可恰恰这种情况下 import 失败, 只在 Actions 控制台
  echo 一句 "飞书告警发送失败", 飞书收不到任何东西。
  → 主路径仍走 push_alert; import 失败退回纯标准库 urllib 直推 FEISHU_GROUP_1_URL (签名同 notifier._sign)。
"""
import io
import json
import pathlib
import sys
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.notifier as notifier  # noqa: E402

_TOKEN = "f00dbabe-1234-5678-9abc-def012345678"
_HOOK = f"https://open.feishu.cn/open-apis/bot/v2/hook/{_TOKEN}"


def _alert_step():
    import yaml
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "nightly.yml").read_text(encoding="utf-8"))
    return next(s for s in wf["jobs"]["nightly"]["steps"] if s.get("name") == "Alert on job failure or cancel")


def _alert_script() -> str:
    """取出 run 里 heredoc (<<'PY' … PY) 的 Python 源码。"""
    lines = _alert_step()["run"].splitlines()
    i0 = next(i for i, ln in enumerate(lines) if "<<'PY'" in ln)
    i1 = next(i for i, ln in enumerate(lines) if i > i0 and ln == "PY")   # 终止符必须顶格
    return "\n".join(lines[i0 + 1:i1])


@pytest.fixture
def env(monkeypatch):
    for k in [k for k in list(notifier.os.environ) if k.startswith("FEISHU_")]:
        monkeypatch.delenv(k)
    monkeypatch.setenv("FEISHU_GROUP_1_URL", _HOOK)
    monkeypatch.setenv("RUN_ID", "123")
    monkeypatch.setenv("RUN_NO", "45")
    monkeypatch.setenv("RUN_URL", "https://github.com/o/r/actions/runs/123")
    monkeypatch.setattr(sys, "path", list(sys.path))      # 脚本会 sys.path.insert(0, '.')
    st = SimpleNamespace(sent=[], reply={"code": 0, "msg": "success"})

    class _Resp(io.BytesIO):
        status = 200

    def fake_urlopen(req, timeout=None):
        st.sent.append(req)
        return _Resp(json.dumps(st.reply).encode("utf-8"))

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return st


def _run(code: str):
    exec(compile(code, "nightly-alert", "exec"), {"__name__": "__main__"})


def test_alert_script_has_no_inline_expressions():
    step = _alert_step()
    assert step["if"].replace(" ", "") == "failure()||cancelled()"
    # ${{ }} 只放 env, 不拼进 Python 源码 (引号/换行都会把脚本弄坏)
    assert "${{" not in _alert_script()
    assert "github.run_id" in step["env"]["RUN_URL"]


def test_primary_path_uses_push_alert(env, monkeypatch):
    calls = []
    monkeypatch.setattr(notifier, "push_alert",
                        lambda title, lines, color="red": calls.append((title, lines)) or {"ok": True, "pushed": 1})
    _run(_alert_script())
    assert len(calls) == 1 and "nightly workflow 失败" in calls[0][0]
    assert any("run 123" in ln for ln in calls[0][1])
    assert env.sent == []                                   # 没走 stdlib 兜底


def test_falls_back_to_stdlib_when_notifier_import_fails(env, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "src.notifier", None)  # 模拟依赖没装上: import 抛 ImportError
    monkeypatch.setenv("FEISHU_GROUP_1_SECRET", "s3cr3t")
    _run(_alert_script())
    assert len(env.sent) == 1
    req = env.sent[0]
    assert req.full_url == _HOOK
    body = json.loads(req.data.decode("utf-8"))
    assert body["msg_type"] == "text"
    text = body["content"]["text"]
    assert "nightly workflow 失败" in text and "依赖安装失败" in text
    assert "https://github.com/o/r/actions/runs/123" in text
    # 签名与 notifier._sign 一致
    assert body["sign"] == notifier._sign(body["timestamp"], "s3cr3t")
    assert _TOKEN not in capsys.readouterr().out


def test_fallback_without_secret_is_unsigned(env, monkeypatch):
    monkeypatch.setitem(sys.modules, "src.notifier", None)
    _run(_alert_script())
    body = json.loads(env.sent[0].data.decode("utf-8"))
    assert "sign" not in body and "timestamp" not in body


def test_fallback_failure_exits_nonzero_without_leaking_webhook(env, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "src.notifier", None)
    import urllib.request

    def boom(req, timeout=None):
        raise OSError(f"HTTP Error 502 for {req.full_url}")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(SystemExit) as ei:
        _run(_alert_script())
    assert _TOKEN not in str(ei.value.code)
    assert _TOKEN not in capsys.readouterr().out


def test_fallback_feishu_error_code_exits_nonzero(env, monkeypatch):
    monkeypatch.setitem(sys.modules, "src.notifier", None)
    env.reply = {"code": 19021, "msg": "sign match fail"}
    with pytest.raises(SystemExit) as ei:
        _run(_alert_script())
    assert ei.value.code not in (0, None)


def test_fallback_without_webhook_exits_nonzero(env, monkeypatch):
    monkeypatch.setitem(sys.modules, "src.notifier", None)
    monkeypatch.delenv("FEISHU_GROUP_1_URL")
    with pytest.raises(SystemExit) as ei:
        _run(_alert_script())
    assert ei.value.code not in (0, None) and env.sent == []
