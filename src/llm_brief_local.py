"""本地 LLM 简报: 通过 subprocess 调用 Claude Code CLI (使用 Pro/Max 订阅, 不走 API 计费)。

前置条件:
  1. 已装 Claude Code CLI (claude.ai/install 或 brew install claude-code)
  2. 已运行过 `claude` 一次完成订阅登录
"""
from __future__ import annotations
import subprocess
import shutil
from datetime import datetime, timezone
from .llm_brief import _build_user_prompt, SYSTEM_PROMPT, save_brief
from .utils import setup_logger

log = setup_logger("llm_brief_local", "INFO")


def find_claude_cli() -> str | None:
    """搜索系统中的 claude CLI 可执行文件路径。"""
    # 常见安装位置 + PATH 搜索
    candidates = [
        shutil.which("claude"),
        "/usr/local/bin/claude",
        "/opt/homebrew/bin/claude",
        f"{__import__('os').path.expanduser('~/.claude/local/claude')}",
        f"{__import__('os').path.expanduser('~/.local/bin/claude')}",
    ]
    for c in candidates:
        if c and __import__('os').path.exists(c) and __import__('os').access(c, __import__('os').X_OK):
            return c
    return None


def generate_brief_via_cli(claude_path: str | None = None,
                            timeout_sec: int = 600,
                            allowed_tools: str = "WebSearch,WebFetch") -> dict:
    """通过 Claude Code CLI 生成简报 (订阅模式)。"""
    cli = claude_path or find_claude_cli()
    if not cli:
        return {"ok": False, "markdown": "", "model": "claude-cli-subscription",
                "error": "Claude Code CLI 未找到。请运行: curl -fsSL https://claude.ai/install.sh | bash"}

    user_prompt = _build_user_prompt()
    full_prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        f"=== 输入数据 ===\n\n{user_prompt}\n\n"
        f"=== 输出 ===\n\n请按系统指令的 Markdown 格式直接输出简报内容,不要任何前后缀寒暄。"
    )

    log.info(f"Calling Claude CLI ({cli}) with allowed tools: {allowed_tools}")
    try:
        result = subprocess.run(
            [cli, "--print",
             "--allowedTools", allowed_tools,
             "--permission-mode", "bypassPermissions"],
            input=full_prompt,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except FileNotFoundError:
        return {"ok": False, "markdown": "", "model": "claude-cli-subscription",
                "error": f"Claude CLI 路径无效: {cli}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "markdown": "", "model": "claude-cli-subscription",
                "error": f"Claude CLI 超时 ({timeout_sec}s)"}
    except Exception as e:
        return {"ok": False, "markdown": "", "model": "claude-cli-subscription",
                "error": f"Claude CLI 调用失败: {e}"}

    if result.returncode != 0:
        return {"ok": False, "markdown": "", "model": "claude-cli-subscription",
                "error": f"Claude CLI rc={result.returncode}: {result.stderr[-500:]}"}

    md = result.stdout.strip()
    if not md or len(md) < 100:
        return {"ok": False, "markdown": "", "model": "claude-cli-subscription",
                "error": f"Claude CLI 输出过短或为空 ({len(md)} chars)"}

    log.info(f"Claude CLI brief OK: {len(md)} chars")
    return {
        "ok": True,
        "markdown": md,
        "model": "claude-cli-subscription",
        "usage": {"output_chars": len(md), "ts": datetime.now(timezone.utc).isoformat()},
        "error": None,
    }


def run_local_brief() -> dict:
    """生成 + 入库 (供 CLI 调用)。"""
    result = generate_brief_via_cli()
    if result["ok"]:
        save_brief({
            "ok": True,
            "markdown": result["markdown"],
            "usage": {"output_tokens": len(result["markdown"]) // 4},  # 粗估
        })
    return result
