"""云端漏跑/空跑哨兵 + 工作流失败告警 (只用标准库, 不装依赖, 几秒跑完)。

为什么需要 (2026-10 盘点):
- GitHub 定时任务会延迟甚至丢弃: nightly 中位晚 3.4h、两次滑过 UTC 午夜;
  每 2h 的 watchdog 约 2/3 的触发被丢掉。
- 09-29 那晚 nightly job 是绿的却什么都没提交 (nightly_run.sh 总是 exit 0)。
- 所有告警都在 run 内部 —— GitHub 根本没触发 run 时, 没有任何人知道。

`check` (sentinel.yml 每天 3 个时段调用):
  对 nightly / refresh: 找到 "最近一次已过宽限期的 cron 时刻" since,
  看 main 上 since 之后有没有对应的产物 commit:
    有 → ok; 有 run 在排队/运行 → 等;
    没有 run (cron 被丢) 或 run 跑完却没产物 (空跑) → 自动 workflow_dispatch 补跑一次并推飞书;
    已补跑过仍无产物 → 推飞书严重告警, 退出码 1 (不再补跑, 防止循环)。
  对 watchdog: 距上一次 run 超过 4h 就静默补一次 (它只推告警、不提交, 补跑无副作用)。
`alert` (refresh.yml / watchdog.yml 失败时调用): 推一条飞书文本告警。

日志是公开的 (PUBLIC 仓库): 永远不打印 webhook / token / 异常文本 (可能带 URL)。
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

API = "https://api.github.com"
ACTIVE = {"queued", "in_progress", "waiting", "requested", "pending"}
WATCHDOG_MAX_GAP_H = 4.0


@dataclass(frozen=True)
class Watch:
    workflow: str        # .github/workflows/ 下的文件名
    name: str
    cron_utc: tuple      # (时, 分), 与该 workflow 的 cron 一致
    grace_h: float       # cron 之后多久还没有产物算漏跑 (GitHub 延迟最大观测 6h + 运行时长)
    commit_prefix: str   # 该 workflow 回推 main 的 commit 信息前缀


WATCHES = (
    Watch("nightly.yml", "nightly", (16, 17), 7.0, "🌙 nightly"),
    Watch("refresh.yml", "refresh", (0, 30), 9.0, "🔄 Daily data refresh"),
)


def expected_since(now: dt.datetime, cron_utc: tuple, grace_h: float) -> dt.datetime:
    """最近一次 "已经过了宽限期" 的 cron 时刻: 这一轮的产物此刻应该已经在 main 上了。"""
    t = now.replace(hour=cron_utc[0], minute=cron_utc[1], second=0, microsecond=0)
    while now - t < dt.timedelta(hours=grace_h):
        t -= dt.timedelta(days=1)
    return t


def parse_ts(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def decide(watch: Watch, since: dt.datetime, runs: list[dict], commit_msgs: list[str]) -> tuple[str, str]:
    """返回 (动作, 说明), 动作 ∈ ok / wait / dispatch / alert。

    runs: 该 workflow 的 run (GitHub API 原样字段), 只看 since 之后创建的;
    commit_msgs: main 上 since 之后的 commit 信息。
    """
    if any(m.startswith(watch.commit_prefix) for m in commit_msgs):
        return "ok", "产物已提交"
    recent = [r for r in runs if parse_ts(r["created_at"]) >= since]
    if any(r.get("status") in ACTIVE for r in recent):
        return "wait", "有 run 在排队或运行中"
    dispatched = [r for r in recent if r.get("event") == "workflow_dispatch"]
    if dispatched:
        last = max(dispatched, key=lambda r: r["created_at"])
        return "alert", f"已补跑过 (结果 {last.get('conclusion')}) 仍没有产物提交, 需要人工排查: {last.get('html_url', '')}"
    if recent:
        last = max(recent, key=lambda r: r["created_at"])
        return "dispatch", f"空跑: run 已结束 (结果 {last.get('conclusion')}) 但没有产物提交: {last.get('html_url', '')}"
    return "dispatch", "GitHub 没有触发这次定时任务 (cron 被丢弃)"


def watchdog_needs_run(runs: list[dict], now: dt.datetime, max_gap_h: float = WATCHDOG_MAX_GAP_H) -> bool:
    if any(r.get("status") in ACTIVE for r in runs):
        return False
    if not runs:
        return True
    last = max(parse_ts(r["created_at"]) for r in runs)
    return now - last > dt.timedelta(hours=max_gap_h)


# ── GitHub API ────────────────────────────────────────────────────────────────

def _gh(method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(API + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else None


def list_runs(repo: str, workflow: str, per_page: int = 20) -> list[dict]:
    return _gh("GET", f"/repos/{repo}/actions/workflows/{workflow}/runs?per_page={per_page}")["workflow_runs"]


def commit_messages_since(repo: str, since: dt.datetime, branch: str = "main") -> list[str]:
    iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    commits = _gh("GET", f"/repos/{repo}/commits?sha={branch}&since={iso}&per_page=100")
    return [c["commit"]["message"] for c in commits]


def dispatch(repo: str, workflow: str, ref: str = "main") -> None:
    # GITHUB_TOKEN 触发的 workflow_dispatch 会产生新 run (这是 GitHub 防递归规则的例外)
    _gh("POST", f"/repos/{repo}/actions/workflows/{workflow}/dispatches", {"ref": ref})


# ── 飞书 ──────────────────────────────────────────────────────────────────────

def feishu_text(title: str, lines: list[str]) -> bool:
    """飞书群机器人文本消息, 签名同 src.notifier._sign。未配置 webhook 时只打印, 返回 False。"""
    url = os.environ.get("FEISHU_GROUP_1_URL", "").strip()
    text = title + "\n" + "\n".join(x for x in lines if x)
    if not url:
        print("未配置 FEISHU_GROUP_1_URL, 只打印:\n" + text)
        return False
    msg = {"msg_type": "text", "content": {"text": text}}
    secret = os.environ.get("FEISHU_GROUP_1_SECRET", "").strip()
    if secret:
        ts = str(int(time.time()))
        digest = hmac.new(f"{ts}\n{secret}".encode("utf-8"), digestmod=hashlib.sha256).digest()
        msg["timestamp"] = ts
        msg["sign"] = base64.b64encode(digest).decode("utf-8")
    req = urllib.request.Request(url, data=json.dumps(msg).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except Exception as e:  # 只打异常类型: 异常文本可能带 webhook URL
        print(f"飞书推送失败: {type(e).__name__}")
        return False
    ok = data.get("code") in (0, None)   # 飞书出错也回 HTTP 200, 看 code
    print("飞书推送:", "OK" if ok else f"失败 code={data.get('code')} msg={data.get('msg')}")
    return ok


# ── 子命令 ────────────────────────────────────────────────────────────────────

def cmd_check(repo: str, now: dt.datetime, dry_run: bool) -> int:
    actions_url = f"https://github.com/{repo}/actions"
    problems = 0
    for w in WATCHES:
        since = expected_since(now, w.cron_utc, w.grace_h)
        action, why = decide(w, since, list_runs(repo, w.workflow), commit_messages_since(repo, since))
        print(f"[{w.name}] since {since:%Y-%m-%d %H:%M}Z → {action}: {why}")
        if action == "dispatch":
            if dry_run:
                continue
            try:
                dispatch(repo, w.workflow)
                feishu_text(f"⚠️ Crypto Intel 哨兵: {w.name} 漏跑, 已自动补跑",
                            [why, f"应在 {since:%m-%d %H:%M} UTC 之后 + {w.grace_h:g}h 内提交产物", actions_url])
            except urllib.error.HTTPError as e:
                problems += 1
                print(f"  补跑失败: HTTP {e.code}")
                feishu_text(f"🚨 Crypto Intel 哨兵: {w.name} 漏跑, 自动补跑失败 (HTTP {e.code})",
                            [why, "请手动在 Actions 里 Run workflow", actions_url])
        elif action == "alert":
            problems += 1
            if not dry_run:
                feishu_text(f"🚨 Crypto Intel 哨兵: {w.name} 补跑后仍无产物", [why, actions_url])

    if watchdog_needs_run(list_runs(repo, "watchdog.yml", per_page=5), now):
        print(f"[watchdog] 超过 {WATCHDOG_MAX_GAP_H:g}h 没跑 → 补跑一次")
        if not dry_run:
            try:
                dispatch(repo, "watchdog.yml")
            except urllib.error.HTTPError as e:
                print(f"  补跑失败: HTTP {e.code}")
    return 1 if problems else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="检查 nightly/refresh 是否漏跑/空跑, 必要时补跑")
    c.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "zeyutom/crypto-intel"))
    c.add_argument("--dry-run", action="store_true", help="只打印判断, 不补跑不推送")
    a = sub.add_parser("alert", help="推一条飞书文本告警")
    a.add_argument("title")
    a.add_argument("lines", nargs="*")
    args = ap.parse_args(argv)
    if args.cmd == "alert":
        feishu_text(args.title, args.lines)
        return 0
    return cmd_check(args.repo, dt.datetime.now(dt.timezone.utc), args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
