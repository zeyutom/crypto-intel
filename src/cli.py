"""Crypto Intel CLI。

用法:
    python -m src.cli init           初始化数据库
    python -m src.cli ingest         跑一轮数据采集
    python -m src.cli factors        计算因子 + 合成信号
    python -m src.cli review         跑复核检查
    python -m src.cli report         生成日报 HTML
    python -m src.cli all            一键跑全流程 (含 LLM 简报, 走 API key)
    python -m src.cli all-no-llm     全流程但不调 LLM (云端 GitHub Actions 用)
    python -m src.cli llm-local      用 Claude Code CLI 生成简报 (Max 订阅, 不走 API)
    python -m src.cli daily-local    一键跑: ingest+factor+review+llm-local+report+feishu-push (本地 cron 用)
    python -m src.cli push-feishu    把最新简报推送到飞书群 (需配 FEISHU_WEBHOOK_URL)
    python -m src.cli test-feishu    发一条测试消息到飞书群
    python -m src.cli serve          启动 APScheduler 常驻
"""
import sys
from rich.console import Console
from .db import init_db
from .pipeline import (
    run_ingest_all, run_factors_all, run_reviews_all, run_report, run_all_once,
)

console = Console()


def main() -> None:
    if len(sys.argv) < 2:
        console.print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "init":
        init_db()
        console.print("[green]DB initialized.[/]")
    elif cmd == "ingest":
        stats = run_ingest_all()
        console.print_json(data=stats)
    elif cmd == "factors":
        stats = run_factors_all()
        console.print_json(data=stats)
    elif cmd == "review":
        stats = run_reviews_all()
        console.print_json(data=stats)
    elif cmd == "report":
        path = run_report()
        console.print(f"[green]Report:[/] {path}")
    elif cmd == "all":
        result = run_all_once()
        console.print_json(data=result)
    elif cmd == "all-no-llm":
        result = run_all_once(skip_llm=True)
        console.print_json(data=result)
    elif cmd == "llm-local":
        from .llm_brief_local import run_local_brief
        result = run_local_brief()
        if result.get("ok"):
            console.print(f"[green]✓ Brief saved ({result['usage']['output_chars']} chars)[/]")
        else:
            console.print(f"[red]✗ {result.get('error')}[/]")
        console.print_json(data={k: v for k, v in result.items() if k != "markdown"})
    elif cmd == "daily-local":
        # 本地完整一日流: data + Claude CLI 简报 + 报告 + 飞书推送
        ing = run_ingest_all()
        fac = run_factors_all()
        rev = run_reviews_all()
        from .llm_brief_local import run_local_brief
        llm = run_local_brief()
        path = run_report()
        # 自动推飞书 (若已配 FEISHU_WEBHOOK_URL)
        from .notifier import push_to_feishu
        fs = push_to_feishu()
        console.print_json(data={
            "ingest": ing, "factor": fac, "review": rev,
            "llm_local": {"ok": llm.get("ok"), "error": llm.get("error"),
                          "chars": llm.get("usage", {}).get("output_chars")},
            "feishu": {"ok": fs.get("ok"), "error": fs.get("error")},
            "report_path": str(path),
        })
    elif cmd == "push-feishu":
        from .notifier import push_to_feishu
        result = push_to_feishu()
        console.print_json(data=result)
    elif cmd == "test-feishu":
        # 对所有配置的群发测试消息
        import os
        from .notifier import _load_groups, push_test_message
        groups = _load_groups()
        if not groups:
            console.print("[red]✗ 没有配置任何飞书群 (FEISHU_GROUP_N_URL)[/]")
            sys.exit(1)
        results = []
        for g in groups:
            r = push_test_message(g["url"], g["secret"])
            r["group_name"] = g["name"]
            results.append(r)
            tag = "[green]✓[/]" if r.get("ok") else "[red]✗[/]"
            console.print(f"{tag} {g['name']}: {r.get('error') or 'OK'}")
        console.print_json(data={"groups_tested": len(groups),
                                  "ok": sum(1 for r in results if r.get('ok'))})
    elif cmd == "list-feishu":
        from .notifier import _load_groups
        groups = _load_groups()
        if not groups:
            console.print("[yellow](没有配置任何飞书群)[/]")
        else:
            console.print(f"[cyan]已配置 {len(groups)} 个飞书群:[/]")
            for i, g in enumerate(groups, 1):
                has_sec = "✓签名" if g["secret"] else "无签名"
                console.print(f"  [{i}] {g['name']} ({has_sec}) - {g['url'][:60]}...")
    elif cmd == "snapshot":
        from .snapshot import take_daily_snapshot
        n = take_daily_snapshot()
        console.print(f"[green]Snapshot: {n} rows[/]")
    elif cmd == "backtest":
        from .review.backtest import run_backtest_all
        n = run_backtest_all()
        console.print(f"[green]Backtest: {n} performance rows[/]")
    elif cmd == "weekly-review":
        from .evolution.weekly_review import run_weekly_review
        r = run_weekly_review()
        console.print_json(data=r)
    elif cmd == "propose-factors":
        from .evolution.factor_proposer import run_factor_proposal
        r = run_factor_proposal()
        console.print_json(data=r)
    elif cmd == "discover-sources":
        from .evolution.source_discoverer import run_source_discovery
        r = run_source_discovery()
        console.print_json(data=r)
    elif cmd == "evolve-prompt":
        from .evolution.prompt_evolver import run_prompt_evolution
        r = run_prompt_evolution()
        console.print_json(data=r)
    elif cmd == "track-narratives":
        from .evolution.narrative_tracker import run_narrative_tracking
        r = run_narrative_tracking()
        console.print_json(data=r)
    elif cmd == "serve":
        from .scheduler.runner import start
        start()
    else:
        console.print(f"[red]Unknown command:[/] {cmd}")
        console.print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
