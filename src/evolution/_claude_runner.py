"""统一调用 Claude CLI 的 runner,供各类 evolution agent 复用。

优先从 .env 读 ANTHROPIC_API_KEY 走 API 认证 (最稳定),
也兼容 Max 订阅 OAuth 登录。
"""
from __future__ import annotations
import subprocess
import os
from pathlib import Path
from ..llm_brief_local import find_claude_cli
from ..utils import setup_logger

log = setup_logger("claude_runner", "INFO")

# 项目根目录 (.env 在这里)
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


_ENV_KEYS = {
    "ANTHROPIC_API_KEY", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY",
    "https_proxy", "http_proxy", "all_proxy",
}


def _load_env():
    """从 .env 加载关键环境变量 (API Key + 代理)。"""
    env_file = _PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    loaded = []
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip("'\"")
        if key in _ENV_KEYS and val:
            os.environ.setdefault(key, val)
            # 代理变量同时设大小写版本
            if key.upper() in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
                os.environ.setdefault(key.lower(), val)
                os.environ.setdefault(key.upper(), val)
            loaded.append(key)
    if loaded:
        log.info(f"已从 .env 加载: {', '.join(loaded)}")


def run_claude(prompt: str, system: str = "", timeout: int = 900,
               allowed_tools: str = "WebSearch,WebFetch") -> dict:
    """发 prompt 给 Claude CLI 返回文本.

    v0.9 W3-M2: 加上 token 预算 + 熔断器
      - 调用前检查 daily/monthly cap (env MAX_TOKENS_PER_DAY / _MONTH)
      - 连续 5 次失败 → 冷却 60min (env MAX_FAILS_BEFORE_BREAK / LLM_COOLDOWN_MINUTES)
      - 所有调用记录到 data/llm_ledger.json (滚动 90 天)
      - 调用 src.cli llm-budget 查用量

    认证优先级: 环境变量 ANTHROPIC_API_KEY > .env 文件 > OAuth 登录
    """
    # W3-M2: 预算检查 (env CRYPTO_INTEL_DISABLE_BUDGET=1 可跳过)
    if os.environ.get("CRYPTO_INTEL_DISABLE_BUDGET") != "1":
        try:
            from ..llm_budget import budget
            full = prompt if not system else f"{system}\n\n---\n\n{prompt}"
            est_in = budget.chars_to_tokens(len(full))
            allowed, reason = budget.allow(est_input_tokens=est_in)
            if not allowed:
                log.warning(f"⛔ LLM 调用被拒 (budget/circuit): {reason}")
                return {"ok": False, "error": f"budget/circuit: {reason}"}
        except Exception as e:
            log.warning(f"budget check skipped: {e}")

    # 确保 .env 中的 API Key 被加载
    _load_env()

    cli = find_claude_cli()
    if not cli:
        return {"ok": False, "error": "Claude CLI not found — 请先运行: curl -fsSL https://claude.ai/install.sh | bash"}

    has_api_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if has_api_key:
        log.info("使用 ANTHROPIC_API_KEY 认证")
    else:
        log.info("未找到 API Key, 将使用 OAuth 认证 (需要已登录)")

    full = prompt if not system else f"{system}\n\n---\n\n{prompt}"

    # 检测 Claude CLI 版本
    try:
        ver = subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=10)
        version_str = ver.stdout.strip() if ver.returncode == 0 else "unknown"
        log.info(f"Claude CLI version: {version_str}")
    except Exception:
        version_str = "unknown"

    # 构建 env (如果有 API Key 会自动传递)
    run_env = {**os.environ, "CLAUDE_NO_INTERACTIVE": "1"}

    # 尝试多种参数组合 (不同版本 CLI 参数不同)
    attempts = [
        # 格式 1: 最新版 (2026) — stdin + allowedTools
        None,  # 特殊处理: 用 stdin (最稳定)
        # 格式 2: -p 传参
        [cli, "--print", "-p", full,
         "--allowedTools", allowed_tools],
        # 格式 3: -p + --permission-mode
        [cli, "--print", "-p", full,
         "--allowedTools", allowed_tools,
         "--permission-mode", "bypassPermissions"],
    ]

    for i, cmd in enumerate(attempts):
        try:
            if cmd is None:
                # stdin 模式 (最稳定, 优先尝试)
                log.info(f"  尝试 #{i+1}: stdin 模式")
                r = subprocess.run(
                    [cli, "--print",
                     "--allowedTools", allowed_tools],
                    input=full,
                    capture_output=True, text=True, timeout=timeout,
                    env=run_env,
                )
            else:
                log.info(f"  尝试 #{i+1}: {' '.join(cmd[:5])}...")
                r = subprocess.run(
                    cmd,
                    capture_output=True, text=True, timeout=timeout,
                    env=run_env,
                )

            if r.returncode == 0:
                out = r.stdout.strip()
                if len(out) < 50:
                    log.warning(f"  尝试 #{i+1}: 输出太短 ({len(out)} chars)")
                    continue
                log.info(f"  ✓ 成功 (格式 #{i+1}, {len(out)} chars)")
                _record_budget(prompt, system, out, success=True)
                return {"ok": True, "markdown": out}
            else:
                stderr = r.stderr.strip() if r.stderr else ""
                stdout = r.stdout.strip() if r.stdout else ""
                # 合并 stdout + stderr 一起检查 (CLI 可能把错误写到 stdout)
                all_output = f"{stdout} {stderr}".lower()
                log.warning(f"  尝试 #{i+1} 失败: rc={r.returncode}")
                if stderr:
                    log.warning(f"    stderr: {stderr[:300]}")
                if stdout and len(stdout) < 500:
                    log.warning(f"    stdout: {stdout[:300]}")
                # 如果是认证/登录问题, 直接返回错误 (不再尝试其他格式)
                if any(kw in all_output for kw in [
                    "login", "auth", "sign in", "forbidden",
                    "failed to authenticate", "403", "unauthorized"
                ]):
                    msg = stderr or stdout
                    return {"ok": False, "error": f"Claude CLI 认证失败, 请在终端运行 'claude' 重新登录。\n原始报错: {msg[:300]}"}
                # 如果是 "unknown flag" 类错误, 换下一种格式
                if "unknown" in all_output or "invalid" in all_output or "flag" in all_output:
                    continue
                # 其他错误也记录但继续尝试
                last_error = f"rc={r.returncode}: {(stderr or stdout)[:300]}"

        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"Timeout ({timeout}s)"}
        except Exception as e:
            log.warning(f"  尝试 #{i+1} 异常: {e}")
            last_error = str(e)

    # W3-M2: 所有路径失败时记录
    err_msg = f"所有调用格式都失败 — {last_error if 'last_error' in dir() else 'unknown'}"
    _record_budget(prompt, system, "", success=False)
    return {"ok": False, "error": err_msg}


def _record_budget(prompt: str, system: str, response: str, success: bool):
    """记录调用到 budget ledger (静默失败不影响主流程)."""
    if os.environ.get("CRYPTO_INTEL_DISABLE_BUDGET") == "1":
        return
    try:
        from ..llm_budget import budget
        full = prompt if not system else f"{system}\n\n---\n\n{prompt}"
        budget.record_call(
            prompt_chars=len(full),
            response_chars=len(response),
            success=success,
        )
    except Exception:
        pass
