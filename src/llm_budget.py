"""LLM 调用预算 + 熔断器 (W3-M2).

防止 evolution agent 不小心烧光 token 配额.

机制:
  - Token 累计: 按 input/output 字符数估算 (1 token ≈ 4 chars), 持久化 ledger
  - 日度预算: env MAX_TOKENS_PER_DAY (默认 200k)
  - 月度预算: env MAX_TOKENS_PER_MONTH (默认 5M)
  - 失败熔断: 连续 N 次失败 → 冷却 1h
  - Ledger: data/llm_ledger.json, 滚动保留 90 天

API:
    from src.llm_budget import budget
    if not budget.allow():
        return {"ok": False, "error": "budget exceeded"}
    res = run_claude(prompt)
    budget.record(prompt_chars=len(prompt), response_chars=len(res.get("markdown", "")))

CLI:
    python -m src.cli llm-budget  # 看用量
"""
from __future__ import annotations
import json
import os
import threading
from dataclasses import dataclass, asdict, field
from datetime import datetime, date, timedelta
from pathlib import Path

from .utils import setup_logger

log = setup_logger("llm_budget", "INFO")

LEDGER_FILE = Path(__file__).resolve().parent.parent / "data" / "llm_ledger.json"
LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)

# 默认预算 (env 可覆盖)
DEFAULT_MAX_TOKENS_PER_DAY = 200_000
DEFAULT_MAX_TOKENS_PER_MONTH = 5_000_000
DEFAULT_MAX_FAILS_BEFORE_BREAK = 5
DEFAULT_COOLDOWN_MINUTES = 60

# 1 token ≈ 4 字符 (Claude tokenizer 经验值)
CHARS_PER_TOKEN = 4


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default


@dataclass
class DailyEntry:
    date: str
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    failures: int = 0
    last_call: str = ""

    @property
    def total_tokens(self) -> int:
        return self.tokens_in + self.tokens_out


@dataclass
class CircuitState:
    consecutive_failures: int = 0
    open_until: str = ""    # ISO timestamp; 在此之前熔断打开

    def is_open(self) -> bool:
        if not self.open_until:
            return False
        try:
            return datetime.utcnow() < datetime.fromisoformat(self.open_until)
        except ValueError:
            return False


@dataclass
class LedgerData:
    days: dict[str, DailyEntry] = field(default_factory=dict)
    circuit: CircuitState = field(default_factory=CircuitState)


class LLMBudget:
    """线程安全的 token 预算 + 熔断器."""

    def __init__(self):
        self._lock = threading.Lock()
        self._ledger = self._load()

    # ── 持久化 ───────────────────────────────────────────────────────

    def _load(self) -> LedgerData:
        if not LEDGER_FILE.exists():
            return LedgerData()
        try:
            raw = json.loads(LEDGER_FILE.read_text())
            days = {k: DailyEntry(**v) for k, v in raw.get("days", {}).items()}
            cs = CircuitState(**raw.get("circuit", {}))
            ld = LedgerData(days=days, circuit=cs)
            # 清理 > 90 天的旧记录
            cutoff = (date.today() - timedelta(days=90)).isoformat()
            ld.days = {d: e for d, e in ld.days.items() if d >= cutoff}
            return ld
        except Exception as e:
            log.warning(f"ledger load failed: {e}; start fresh")
            return LedgerData()

    def _save(self):
        try:
            LEDGER_FILE.write_text(json.dumps({
                "days": {k: asdict(v) for k, v in self._ledger.days.items()},
                "circuit": asdict(self._ledger.circuit),
            }, indent=2, default=str))
        except Exception as e:
            log.warning(f"ledger save failed: {e}")

    def _today(self) -> DailyEntry:
        today = date.today().isoformat()
        if today not in self._ledger.days:
            self._ledger.days[today] = DailyEntry(date=today)
        return self._ledger.days[today]

    # ── 预算判定 ─────────────────────────────────────────────────────

    @staticmethod
    def chars_to_tokens(chars: int) -> int:
        return max(1, chars // CHARS_PER_TOKEN)

    def daily_used(self) -> int:
        with self._lock:
            return self._today().total_tokens

    def monthly_used(self) -> int:
        with self._lock:
            month_prefix = date.today().strftime("%Y-%m")
            return sum(
                e.total_tokens for d, e in self._ledger.days.items()
                if d.startswith(month_prefix)
            )

    def allow(self, est_input_tokens: int = 0) -> tuple[bool, str]:
        """检查是否能继续调用.

        Returns: (allowed, reason)
        """
        with self._lock:
            # 1. 熔断器
            if self._ledger.circuit.is_open():
                return False, f"circuit-breaker OPEN until {self._ledger.circuit.open_until}"

            # 2. 日预算
            cap_day = _env_int("MAX_TOKENS_PER_DAY", DEFAULT_MAX_TOKENS_PER_DAY)
            today = self._today()
            if today.total_tokens + est_input_tokens > cap_day:
                return False, (
                    f"daily budget exceeded: used {today.total_tokens} + "
                    f"est {est_input_tokens} > cap {cap_day}"
                )

            # 3. 月预算
            cap_month = _env_int("MAX_TOKENS_PER_MONTH", DEFAULT_MAX_TOKENS_PER_MONTH)
            month_prefix = date.today().strftime("%Y-%m")
            month_used = sum(
                e.total_tokens for d, e in self._ledger.days.items()
                if d.startswith(month_prefix)
            )
            if month_used + est_input_tokens > cap_month:
                return False, (
                    f"monthly budget exceeded: used {month_used} > cap {cap_month}"
                )

            return True, "ok"

    # ── 记录 ─────────────────────────────────────────────────────────

    def record_call(self, prompt_chars: int, response_chars: int, success: bool):
        with self._lock:
            today = self._today()
            today.calls += 1
            today.tokens_in += self.chars_to_tokens(prompt_chars)
            today.tokens_out += self.chars_to_tokens(response_chars)
            today.last_call = datetime.utcnow().isoformat()

            if success:
                # 成功调用 → 重置失败计数 + 关熔断
                self._ledger.circuit.consecutive_failures = 0
                self._ledger.circuit.open_until = ""
            else:
                today.failures += 1
                self._ledger.circuit.consecutive_failures += 1
                cap_fails = _env_int(
                    "MAX_FAILS_BEFORE_BREAK", DEFAULT_MAX_FAILS_BEFORE_BREAK
                )
                if self._ledger.circuit.consecutive_failures >= cap_fails:
                    cooldown_min = _env_int(
                        "LLM_COOLDOWN_MINUTES", DEFAULT_COOLDOWN_MINUTES
                    )
                    open_until = datetime.utcnow() + timedelta(minutes=cooldown_min)
                    self._ledger.circuit.open_until = open_until.isoformat()
                    log.warning(
                        f"⛔ Circuit breaker OPEN — 连续 {cap_fails} 次失败, "
                        f"冷却到 {open_until.isoformat()}"
                    )

            self._save()

    # ── 报告 ─────────────────────────────────────────────────────────

    def report(self) -> dict:
        with self._lock:
            cap_day = _env_int("MAX_TOKENS_PER_DAY", DEFAULT_MAX_TOKENS_PER_DAY)
            cap_month = _env_int("MAX_TOKENS_PER_MONTH", DEFAULT_MAX_TOKENS_PER_MONTH)
            today = self._today()
            month_prefix = date.today().strftime("%Y-%m")
            month_used = sum(
                e.total_tokens for d, e in self._ledger.days.items()
                if d.startswith(month_prefix)
            )
            recent_7d = sorted(
                self._ledger.days.items(), reverse=True
            )[:7]
            return {
                "today": {
                    "date": today.date,
                    "calls": today.calls,
                    "tokens_in": today.tokens_in,
                    "tokens_out": today.tokens_out,
                    "total": today.total_tokens,
                    "failures": today.failures,
                    "budget_pct": round(
                        100 * today.total_tokens / max(cap_day, 1), 2
                    ),
                },
                "budgets": {
                    "daily_cap": cap_day,
                    "monthly_cap": cap_month,
                    "monthly_used": month_used,
                    "monthly_pct": round(100 * month_used / max(cap_month, 1), 2),
                },
                "circuit": {
                    "is_open": self._ledger.circuit.is_open(),
                    "consecutive_failures": self._ledger.circuit.consecutive_failures,
                    "open_until": self._ledger.circuit.open_until or None,
                },
                "recent_7d": [
                    {"date": d, "tokens": e.total_tokens, "calls": e.calls,
                     "failures": e.failures}
                    for d, e in recent_7d
                ],
            }

    def reset_circuit(self):
        """强制重置熔断器 (CLI 命令调用)."""
        with self._lock:
            self._ledger.circuit.consecutive_failures = 0
            self._ledger.circuit.open_until = ""
            self._save()
            log.info("✓ circuit-breaker reset")


# Singleton
budget = LLMBudget()


# ────────────────────────────────────────────────────────────────────
#  装饰器: 包装 run_claude
# ────────────────────────────────────────────────────────────────────

def guarded(run_claude_fn):
    """装饰 run_claude: 调用前检查预算, 调用后记录.

    用法:
        from src.evolution._claude_runner import run_claude
        from src.llm_budget import guarded
        safe_run = guarded(run_claude)
    """
    def wrapper(prompt: str, system: str = "", **kwargs):
        full = prompt if not system else f"{system}\n\n---\n\n{prompt}"
        est_in = budget.chars_to_tokens(len(full))

        allowed, reason = budget.allow(est_input_tokens=est_in)
        if not allowed:
            log.warning(f"⛔ LLM 调用被拒: {reason}")
            return {"ok": False, "error": f"budget/circuit: {reason}"}

        result = run_claude_fn(prompt, system=system, **kwargs)

        # 记录
        ok = result.get("ok", False)
        resp = result.get("markdown", "") if ok else ""
        budget.record_call(
            prompt_chars=len(full),
            response_chars=len(resp),
            success=ok,
        )
        return result
    return wrapper


def is_available() -> bool:
    return True


# ────────────────────────────────────────────────────────────────────
#  Self-test
# ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== current budget report ===")
    print(json.dumps(budget.report(), indent=2, ensure_ascii=False))
