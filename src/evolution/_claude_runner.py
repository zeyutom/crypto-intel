"""统一调用 Claude CLI (订阅模式) 的 runner,供各类 evolution agent 复用。"""
from __future__ import annotations
import subprocess
from ..llm_brief_local import find_claude_cli
from ..utils import setup_logger

log = setup_logger("claude_runner", "INFO")


def run_claude(prompt: str, system: str = "", timeout: int = 900,
               allowed_tools: str = "WebSearch,WebFetch,Read") -> dict:
    """发 prompt 给 Claude CLI 返回文本。"""
    cli = find_claude_cli()
    if not cli:
        return {"ok": False, "error": "Claude CLI not found"}
    full = prompt if not system else f"{system}\n\n---\n\n{prompt}"
    try:
        r = subprocess.run(
            [cli, "--print",
             "--allowedTools", allowed_tools,
             "--permission-mode", "bypassPermissions"],
            input=full, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Timeout ({timeout}s)"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if r.returncode != 0:
        return {"ok": False, "error": f"rc={r.returncode}: {r.stderr[-500:]}"}
    out = r.stdout.strip()
    if len(out) < 50:
        return {"ok": False, "error": f"Empty output ({len(out)} chars)"}
    return {"ok": True, "markdown": out}
