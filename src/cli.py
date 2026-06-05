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
    python -m src.cli research TON   Multi-Agent 深度研究 (指定项目名)
    python -m src.cli screen         Top-500 多因子筛选 v2 (10因子+元学习+Regime)
    python -m src.cli daily-screen   每日自动筛选+快照+IC更新+飞书推送 (定时调度用)
    python -m src.cli verify-returns 收益追踪: 对比历史快照 vs 当前价格
    python -m src.cli meta-report    因子池健康报告 (权重/IC/状态)
    python -m src.cli ic-backtest    IC 回测 (计算各因子信息系数)
    python -m src.cli update-weights 根据 IC 回测自动更新因子权重
    python -m src.cli discover-alpha LLM 因子自动发现 (进化一轮)
    python -m src.cli alpha-report   Alpha 因子发现状态报告
    python -m src.cli backtest-wf    Walk-forward 组合回测 (Top-N 等权)
    python -m src.cli param-sweep    参数扫描 (不同 Top-N / 换仓周期)
    python -m src.cli sentiment      NLP 情绪分析 (Top-30 代币)
    python -m src.cli risk-report    风控报告 (因子正交化+仓位约束+回撤保护)
    python -m src.cli swarm          Swarm 多Agent 投票决策 (Top-30)
    python -m src.cli train          自适应再训练 (IC+Alpha+PBO)
    python -m src.cli dashboard      生成统一仪表盘 HTML
    python -m src.cli whale          Whale Alert 大额转账监控
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
    # v0.5: 任何命令前都自动 init_db (确保 schema 与新版代码一致, 避免 "no such table")
    init_db()
    if cmd == "init":
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
    elif cmd == "screen":
        top_n = 30
        if len(sys.argv) > 2 and sys.argv[2] == "--top":
            top_n = int(sys.argv[3]) if len(sys.argv) > 3 else 30
        from .research.screener import run_screen, generate_screen_report
        console.print("[bold cyan]📊 启动 Top-500 多因子实时筛选 v2...[/]")
        console.print("[dim]10 因子 · 元学习动态调权 · Regime Detection · 异动信号[/]")
        console.print("[dim]数据源: CoinGecko + DeFiLlama + Binance (spot+futures)[/]")
        result = run_screen(top_n=top_n)
        if result.get("ok"):
            report_path = generate_screen_report(result)
            regime = result.get("regime", "unknown")
            regime_map = {"bull": "🐂牛市", "bear": "🐻熊市", "sideways": "➡️震荡", "volatile": "🌊高波动"}
            console.print(f"\n[green]✓ 筛选完成![/] {result['total_screened']} 个代币 · "
                          f"Regime: [bold]{regime_map.get(regime, regime)}[/]")
            anomalies = result.get("anomalies", [])
            if anomalies:
                console.print(f"[yellow]⚡ {len(anomalies)} 条异动信号[/]")
                for a in anomalies[:5]:
                    console.print(f"   [{a['severity'].upper()}] {a['symbol']}: {a['detail']}")
            console.print(f"\n[bold]Top 10:[/]")
            for i, c in enumerate(result["top"][:10], 1):
                chg = c.get("change_30d", 0)
                tag = f"[green]+{chg:.1f}%[/]" if chg > 0 else f"[red]{chg:.1f}%[/]"
                console.print(f"  {i:>2}. [bold]{c['symbol']:>8}[/] ${c['price']:>10,.4f}  "
                              f"MCap {c['market_cap']/1e9:.2f}B  30d {tag}  "
                              f"Score [cyan]{c['composite_score']:.4f}[/]")
            console.print(f"\n[green]📄 报告:[/] {report_path}")
            import platform
            if platform.system() == "Darwin":
                import subprocess as sp
                sp.Popen(["open", str(report_path)])
        else:
            console.print(f"[red]✗ {result.get('error')}[/]")
    elif cmd == "daily-screen":
        # 每日自动化: 筛选 → IC更新(如果有足够快照) → 飞书推送
        from .research.screener import run_screen, generate_screen_report
        from .research.meta_learner import run_ic_backtest, update_weights_from_ic
        from .research.returns_tracker import push_screen_to_feishu
        console.print("[bold cyan]📊 每日自动筛选 (定时调度模式)[/]")

        # Step 1: 尝试 IC 回测 + 权重更新 (需要 ≥2 次快照)
        console.print("[dim]Step 1: 尝试 IC 回测...[/]")
        for days in [7, 14]:
            ic_r = run_ic_backtest(lookback_days=days)
            if ic_r.get("ok"):
                ur = update_weights_from_ic(ic_r)
                if ur.get("ok") and ur.get("factors_updated", 0) > 0:
                    console.print(f"[green]  ✓ IC {days}d 回测 → {ur['factors_updated']} 因子权重已更新[/]")
                break
        else:
            console.print("[dim]  跳过 (快照不足, 继续积累中)[/]")

        # Step 2: 筛选
        console.print("[dim]Step 2: 10 因子筛选...[/]")
        result = run_screen(top_n=30)
        if not result.get("ok"):
            console.print(f"[red]✗ {result.get('error')}[/]")
            sys.exit(1)

        report_path = generate_screen_report(result)
        regime = result.get("regime", "unknown")
        console.print(f"[green]  ✓ {result['total_screened']} 代币 · Regime: {regime}[/]")

        # Step 3: 自适应再训练
        console.print("[dim]Step 3: 自适应再训练...[/]")
        try:
            from .research.adaptive_trainer import run_adaptive_training
            train_r = run_adaptive_training()
            if train_r.get("ok") and not train_r.get("skipped"):
                console.print(f"[green]  ✓ 训练 {train_r['steps_run']} 步[/]")
            else:
                console.print(f"[dim]  跳过 ({train_r.get('reason', 'already done')})[/]")
        except Exception as e:
            console.print(f"[dim]  训练跳过: {e}[/]")

        # Step 4: 飞书推送
        console.print("[dim]Step 4: 飞书推送...[/]")
        fs = push_screen_to_feishu(result)
        if fs.get("ok"):
            console.print(f"[green]  ✓ 已推送到飞书[/]")
        else:
            console.print(f"[yellow]  跳过: {fs.get('error', '未配置飞书')}[/]")

        console.print(f"\n[green]✅ 每日筛选完成![/] 报告: {report_path}")
    elif cmd == "verify-returns":
        from .research.returns_tracker import verify_returns
        days = 7
        if len(sys.argv) > 2:
            try:
                days = int(sys.argv[2])
            except ValueError:
                pass
        console.print(f"[bold cyan]📈 收益追踪 (验证 {days} 天前的筛选结果)[/]")
        result = verify_returns(lookback_days=days)
        if result.get("ok"):
            console.print(f"快照日期: {result['snapshot_date']}")
            console.print(f"匹配代币: {result['matched_coins']}\n")
            console.print("[bold]Top-30 收益统计:[/]")
            stats = result.get("stats", {})
            avg = stats.get("avg_return", 0)
            color = "green" if avg > 0 else "red"
            console.print(f"  平均收益: [{color}]{avg:+.2%}[/]")
            console.print(f"  中位收益: [{color}]{stats.get('median_return', 0):+.2%}[/]")
            console.print(f"  胜率:     {stats.get('win_rate', 0):.0%}")
            console.print(f"  最佳:     {stats.get('best_coin', 'N/A')} ({stats.get('best_return', 0):+.2%})")
            console.print(f"  最差:     {stats.get('worst_coin', 'N/A')} ({stats.get('worst_return', 0):+.2%})")
            if result.get("vs_btc") is not None:
                vb = result["vs_btc"]
                vcolor = "green" if vb > 0 else "red"
                console.print(f"  vs BTC:   [{vcolor}]{vb:+.2%}[/]")
            console.print(f"\n[bold]逐币收益:[/]")
            for c in result.get("coins", [])[:30]:
                ret = c.get("return_pct", 0)
                rc = "green" if ret > 0 else "red"
                console.print(f"  {c['symbol']:>8s}  "
                              f"${c.get('old_price',0):>10,.4f} → ${c.get('new_price',0):>10,.4f}  "
                              f"[{rc}]{ret:+.2%}[/]")
        else:
            console.print(f"[yellow]{result.get('error')}[/]")
    elif cmd == "scorecard":
        from .research.returns_tracker import push_returns_scorecard
        days = 7
        if len(sys.argv) > 2:
            try:
                days = int(sys.argv[2])
            except ValueError:
                pass
        console.print(f"[bold cyan]📋 每周复盘记分卡 (近 {days} 天 Top-N 实盘表现 → 飞书)[/]")
        r = push_returns_scorecard(lookback_days=days)
        if r.get("ok"):
            console.print(f"[green]✓ 已推送 ({r.get('pushed')} 群)[/]")
        elif r.get("stats"):
            console.print(f"[yellow]复盘完成但未推送: {r.get('error')}[/]")
        else:
            console.print(f"[red]✗ {r.get('error')}[/]")
        if r.get("stats"):
            console.print_json(data=r["stats"])
    elif cmd == "data-quality":
        from .research.data_quality import run_data_quality
        no_push = "--no-push" in sys.argv
        no_backfill = "--no-backfill" in sys.argv
        console.print("[bold cyan]🩺 数据自愈 (快照缺口检测 + backfill + 核心源掉线告警)[/]")
        r = run_data_quality(push=not no_push, backfill=not no_backfill)
        console.print(f"快照缺口: {len(r['gaps_before'])} → {len(r['gaps_after'])} (backfill 后)")
        if r["recent_gaps"]:
            console.print(f"[yellow]近 2 天缺口: {r['recent_gaps']} (云端可能漏跑)[/]")
        if r["dry_sources"]:
            console.print("[red]可能掉线的核心源:[/]")
            for d in r["dry_sources"]:
                console.print(f"  • {d['source']}: 最近 {d['last_ts']} ({d['age_days']}d 前)")
        else:
            console.print("[green]✓ 核心数据源都新鲜[/]")
        if r["alerted"]:
            console.print("[green]✓ 已推飞书告警[/]")
        elif r["alert_lines"]:
            console.print("[yellow](有告警但未推送/未配飞书)[/]")
    elif cmd == "meta-report":
        from .research.meta_learner import generate_factor_report, load_factor_config
        console.print("[bold cyan]📈 因子池健康报告[/]")
        report = generate_factor_report()
        cfg = load_factor_config()
        console.print(f"\nRegime: [bold]{report.get('regime', 'unknown').upper()}[/]")
        console.print(f"因子数: {report.get('total_factors', 0)}")
        console.print(f"配置更新: {cfg.get('updated', 'N/A')}\n")
        console.print("[bold]因子状态:[/]")
        for fname, finfo in report.get("factors", {}).items():
            status_colors = {"strong": "green", "healthy": "blue", "noisy": "yellow", "weak_negative": "red"}
            sc = status_colors.get(finfo["status"], "white")
            console.print(f"  {fname:>20s}  权重={finfo['weight']:.1%}  "
                          f"IC={finfo['avg_ic_10']:+.4f}  "
                          f"记录={finfo['ic_records']:>2d}  "
                          f"[{sc}]{finfo['status']}[/]")
    elif cmd == "ic-backtest":
        from .research.meta_learner import run_ic_backtest
        days = 7
        if len(sys.argv) > 2:
            try:
                days = int(sys.argv[2])
            except ValueError:
                pass
        console.print(f"[bold cyan]🔬 IC 回测 ({days} 天)[/]")
        result = run_ic_backtest(lookback_days=days)
        if result.get("ok"):
            console.print(f"快照日期: {result.get('snapshot_date')}")
            console.print(f"匹配代币: {result.get('matched_coins')}\n")
            console.print("[bold]因子 IC (Information Coefficient):[/]")
            for fname, ic in sorted(result.get("factor_ic", {}).items(),
                                     key=lambda x: abs(x[1]), reverse=True):
                color = "green" if ic > 0.05 else "red" if ic < -0.05 else "yellow"
                console.print(f"  {fname:>25s}  IC = [{color}]{ic:+.4f}[/]")
        else:
            console.print(f"[yellow]{result.get('error')}[/]")
    elif cmd == "update-weights":
        from .research.meta_learner import run_ic_backtest, update_weights_from_ic
        days = 7
        if len(sys.argv) > 2:
            try:
                days = int(sys.argv[2])
            except ValueError:
                pass
        console.print(f"[bold cyan]⚙️ 元学习自动调权 (IC 回测 {days}d → 权重更新)[/]")
        ic_result = run_ic_backtest(lookback_days=days)
        if not ic_result.get("ok"):
            console.print(f"[yellow]IC 回测失败: {ic_result.get('error')}[/]")
            console.print("[dim]需要至少两次筛选快照才能回测[/]")
        else:
            console.print(f"IC 回测完成 ({ic_result.get('matched_coins')} coins)")
            update_result = update_weights_from_ic(ic_result)
            if update_result.get("ok"):
                console.print(f"[green]✓ {update_result['factors_updated']} 个因子权重已更新[/]")
                console.print("\n[bold]新权重:[/]")
                for fname, w in sorted(update_result.get("new_weights", {}).items(),
                                        key=lambda x: x[1], reverse=True):
                    console.print(f"  {fname:>20s}  {w:.1%}")
            else:
                console.print(f"[red]✗ {update_result.get('error')}[/]")
    elif cmd == "discover-alpha":
        from .research.alpha_discovery import run_evolution_cycle, get_discovery_report
        use_llm = "--no-llm" not in sys.argv
        console.print(f"[bold cyan]🧬 LLM 因子自动发现 ({'LLM' if use_llm else '离线'}模式)[/]")
        result = run_evolution_cycle(use_llm=use_llm)
        if result.get("ok"):
            console.print(f"[green]✓ 进化完成![/]")
            console.print(f"  生成: {result['generated']}  新增: {result['new_added']}  "
                          f"晋升: {result['promoted']}  淘汰: {result['retired']}")
            console.print(f"  候选池: {result['pool_size']}  已毕业: {result['graduated_total']}")
            if result.get("top_candidates"):
                console.print("\n[bold]Top 候选因子:[/]")
                for c in result["top_candidates"]:
                    ic = c["ic"]
                    color = "green" if ic > 0.03 else "red" if ic < -0.02 else "yellow"
                    console.print(f"  {c['name']:>30s}  IC=[{color}]{ic:+.4f}[/]  "
                                  f"({c['records']} records)  {c['expr']}")
        else:
            console.print(f"[red]✗ {result.get('error')}[/]")
    elif cmd == "alpha-report":
        from .research.alpha_discovery import get_discovery_report
        console.print("[bold cyan]🧬 Alpha 因子发现状态[/]")
        report = get_discovery_report()
        console.print(f"候选池: {report['pool_size']}  已毕业: {len(report['graduated'])}  "
                      f"已淘汰: {report['retired_count']}")
        if report["graduated"]:
            console.print("\n[bold green]已毕业因子 (可纳入正式池):[/]")
            for g in report["graduated"]:
                console.print(f"  🎓 {g['name']:>30s}  avg IC={g['avg_ic']:+.4f}  "
                              f"({g.get('graduated_date', 'N/A')})")
        if report["candidates"]:
            console.print(f"\n[bold]候选因子 (Top 10 by IC):[/]")
            ranked = sorted(report["candidates"].items(),
                            key=lambda x: abs(x[1].get("last_ic") or 0), reverse=True)
            for name, info in ranked[:10]:
                ic = info.get("last_ic", 0) or 0
                color = "green" if ic > 0.03 else "red" if ic < -0.02 else "yellow"
                console.print(f"  {name:>30s}  IC=[{color}]{ic:+.4f}[/]  "
                              f"records={info['records']}  origin={info['origin']}")
    elif cmd == "backtest-wf":
        from .research.portfolio_backtest import run_walkforward_backtest, generate_backtest_report
        top_n = 10
        rb_days = 7
        args = sys.argv[2:]
        for i, a in enumerate(args):
            if a == "--top" and i + 1 < len(args):
                top_n = int(args[i + 1])
            elif a == "--rebalance" and i + 1 < len(args):
                rb_days = int(args[i + 1])
        console.print(f"[bold cyan]📈 Walk-forward 回测 (Top-{top_n}, {rb_days}d 换仓)[/]")
        result = run_walkforward_backtest(top_n=top_n, rebalance_days=rb_days)
        if result.get("ok"):
            tr = result["total_return"]
            ar = result["annual_return"]
            tr_c = "green" if tr > 0 else "red"
            ar_c = "green" if ar > 0 else "red"
            console.print(f"\n[bold]回测结果: {result['date_range']} ({result['total_days']}d)[/]")
            console.print(f"  总收益:   [{tr_c}]{tr:+.2%}[/]")
            console.print(f"  年化收益: [{ar_c}]{ar:+.2%}[/]")
            console.print(f"  Sharpe:   {result['sharpe']:.2f}")
            console.print(f"  Sortino:  {result['sortino']:.2f}")
            console.print(f"  MaxDD:    [red]{result['max_drawdown']:.2%}[/]")
            console.print(f"  Calmar:   {result['calmar']:.2f}")
            console.print(f"  胜率:     {result['win_rate']:.0%}")
            btc_c = "green" if result["vs_btc_excess"] > 0 else "red"
            console.print(f"  vs BTC:   [{btc_c}]{result['vs_btc_excess']:+.2%}[/]")
            report_path = generate_backtest_report(result)
            if report_path:
                console.print(f"\n[green]📄 报告:[/] {report_path}")
                import platform
                if platform.system() == "Darwin":
                    import subprocess as sp
                    sp.Popen(["open", str(report_path)])
        else:
            console.print(f"[yellow]{result.get('error')}[/]")
    elif cmd == "param-sweep":
        from .research.portfolio_backtest import run_parameter_sweep
        console.print("[bold cyan]🔍 参数扫描 (Top-N × 换仓周期)[/]")
        result = run_parameter_sweep()
        if result.get("ok"):
            console.print(f"\n测试 {result['configs_tested']} 种配置:\n")
            console.print(f"{'Top-N':>6} {'Rebal':>6} {'Total':>8} {'Annual':>8} "
                          f"{'Sharpe':>7} {'MaxDD':>7} {'WinR':>6} {'vsBTC':>7}")
            console.print("─" * 60)
            for r in result["all_results"]:
                tc = "green" if r["total_return"] > 0 else "red"
                console.print(f"  {r['top_n']:>4}  {r['rebalance_days']:>4}d  "
                              f"[{tc}]{r['total_return']:>+7.2%}[/]  "
                              f"{r['annual_return']:>+7.2%}  "
                              f"{r['sharpe']:>6.2f}  "
                              f"[red]{r['max_drawdown']:>6.2%}[/]  "
                              f"{r['win_rate']:>5.0%}  "
                              f"{r['vs_btc']:>+6.2%}")
            best = result["best_config"]
            console.print(f"\n[green]最优配置:[/] Top-{best['top_n']}, "
                          f"{best['rebalance_days']}d 换仓, Sharpe={best['sharpe']:.2f}")
        else:
            console.print(f"[yellow]{result.get('error')}[/]")
    elif cmd == "sentiment":
        from .research.sentiment_nlp import compute_sentiment_factors
        use_claude = "--claude" in sys.argv
        console.print(f"[bold cyan]💬 NLP 情绪分析 ({'Claude' if use_claude else '关键词'}模式)[/]")
        # 取 Top-30 symbols from 最近快照
        from pathlib import Path
        import json as _json
        snap_dir = Path(__file__).resolve().parents[1] / "data" / "meta"
        snap_files = sorted(snap_dir.glob("snapshot_*.json"))
        if snap_files:
            last_snap = _json.loads(snap_files[-1].read_text())
            symbols = [c["symbol"] for c in last_snap.get("coins", [])[:30]]
        else:
            symbols = ["BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "AVAX", "DOT",
                       "LINK", "UNI", "AAVE", "ARB", "OP", "SUI", "NEAR"]
        result = compute_sentiment_factors(symbols, use_claude=use_claude)
        console.print(f"\n{'Symbol':>8} {'Sentiment':>10} {'Volume':>8} {'Hype':>6} {'News#':>6}")
        console.print("─" * 45)
        for sym in symbols:
            if sym in result:
                d = result[sym]
                s = d["sentiment_score"]
                sc = "green" if s > 0.1 else "red" if s < -0.1 else "yellow"
                console.print(f"  {sym:>6}  [{sc}]{s:>+8.3f}[/]  {d['sentiment_volume']:>6.3f}  "
                              f"{d['hype_score']:>5.3f}  {d['news_count']:>4}")
    elif cmd == "risk-report":
        from .research.risk_control import apply_risk_controls, generate_risk_report_text
        console.print("[bold cyan]🛡️ 综合风控报告[/]")
        from pathlib import Path
        import json as _json
        snap_dir = Path(__file__).resolve().parents[1] / "data" / "meta"
        snap_files = sorted(snap_dir.glob("snapshot_*.json"))
        if not snap_files:
            console.print("[yellow]无快照数据, 请先运行 screen[/]")
            sys.exit(1)
        last_snap = _json.loads(snap_files[-1].read_text())
        coins = last_snap.get("coins", [])
        result = apply_risk_controls(coins, top_n=30)
        text = generate_risk_report_text(result)
        console.print(f"\n{text}")
        if result.get("vol_weights"):
            console.print("\n[bold]波动率权重 (风险平价, Top 10):[/]")
            vw = sorted(result["vol_weights"].items(), key=lambda x: x[1], reverse=True)
            for sym, w in vw[:10]:
                bar = "█" * int(w * 200)
                console.print(f"  {sym:>8}  {w:.2%}  {bar}")
    elif cmd == "swarm":
        from .research.swarm_decision import run_swarm_decision
        use_jury = "--jury" in sys.argv
        console.print(f"[bold cyan]🐝 Swarm 多Agent 决策 ({'+ LLM Jury' if use_jury else '加权投票'})[/]")
        from pathlib import Path
        import json as _json
        snap_dir = Path(__file__).resolve().parents[1] / "data" / "meta"
        snap_files = sorted(snap_dir.glob("snapshot_*.json"))
        if not snap_files:
            console.print("[yellow]无快照数据, 请先运行 screen[/]")
            sys.exit(1)
        last_snap = _json.loads(snap_files[-1].read_text())
        coins = last_snap.get("coins", [])
        result = run_swarm_decision(coins, use_llm_jury=use_jury, top_n=30)
        if result.get("ok"):
            trace = result["decision_trace"]
            dist = trace.get("consensus_distribution", {})
            console.print(f"\n[green]✓ Swarm 决策完成[/]")
            console.print(f"  共识: {dist.get('strong',0)} strong, "
                          f"{dist.get('moderate',0)} moderate, {dist.get('weak',0)} weak")
            console.print(f"\n{'#':>3} {'Symbol':>8} {'Swarm':>7} {'Consensus':>10} {'Agents':>3} Signals")
            console.print("─" * 70)
            for i, t in enumerate(result["top"][:15], 1):
                cons = t["consensus"]
                cons_c = {"strong": "green", "moderate": "yellow", "weak": "red"}.get(cons, "white")
                sigs = "; ".join(t.get("top_signals", [])[:2])
                console.print(f"  {i:>2} {t['symbol']:>8} {t['swarm_score']:>6.3f} "
                              f"[{cons_c}]{cons:>9}[/] {t['agents_agree']:>2}/4  {sigs[:50]}")
            if result.get("jury") and result["jury"].get("ok"):
                jury = result["jury"]
                console.print(f"\n[bold]🧑‍⚖️ LLM Jury:[/] stance={jury.get('position_stance', 'N/A')}")
                for hc in jury.get("high_conviction", [])[:3]:
                    console.print(f"  ✨ {hc.get('symbol', '')}: {hc.get('reason', '')[:60]}")
    elif cmd == "train":
        from .research.adaptive_trainer import run_adaptive_training, get_training_summary
        force = "--force" in sys.argv
        console.print("[bold cyan]🔄 自适应再训练[/]")
        result = run_adaptive_training(force=force)
        if result.get("skipped"):
            console.print(f"[yellow]今日已训练, 跳过 (--force 强制)[/]")
        elif result.get("ok"):
            console.print(f"[green]✓ 训练完成: {result['steps_run']} 步[/]")
            if result["ic_updated"]:
                console.print(f"  IC 权重更新: {result['weights_changed']} 因子")
            if result["alpha_evolved"]:
                console.print(f"  Alpha 因子进化: ✓")
            if result["pbo_checked"]:
                console.print(f"  PBO 过拟合检测: ✓")
            summary = get_training_summary(7)
            console.print(f"\n[bold]最近 7 天:[/] {summary['total_ic_updates']} 次 IC 更新, "
                          f"{summary['total_alpha_evolutions']} 次因子进化")
    elif cmd == "dashboard":
        from .research.dashboard import generate_dashboard
        console.print("[bold cyan]📊 生成统一仪表盘[/]")
        path = generate_dashboard()
        console.print(f"[green]✓ 仪表盘:[/] {path}")
        import platform
        if platform.system() == "Darwin":
            import subprocess as sp
            sp.Popen(["open", str(path)])
    elif cmd == "whale":
        from .research.whale_alert import run_whale_check, push_whale_alert_feishu
        console.print("[bold cyan]🐋 Whale Alert 大额转账监控[/]")
        result = run_whale_check()
        if result.get("ok"):
            console.print(f"\n[bold]BTC 大额 (>100 BTC):[/] {len(result.get('btc_large', []))} 笔, "
                          f"共 {result['btc_flow_btc']:.0f} BTC")
            for t in result.get("btc_large", [])[:5]:
                console.print(f"  • {t['amount']:.0f} BTC  {t['hash']}")
            console.print(f"\n[bold]ETH 大额 (>5000 ETH):[/] {len(result.get('eth_large', []))} 笔, "
                          f"共 {result['eth_flow_eth']:.0f} ETH")
            for t in result.get("eth_large", [])[:5]:
                console.print(f"  • {t['amount']:.0f} ETH  {t['hash']}")
            if "--push" in sys.argv and result.get("total_alerts", 0) > 0:
                fs = push_whale_alert_feishu(result)
                if fs.get("ok"):
                    console.print(f"\n[green]✓ 已推送飞书[/]")
    elif cmd == "research":
        if len(sys.argv) < 3:
            console.print("[red]用法: python -m src.cli research <项目名> [代币符号][/]")
            console.print("例: python -m src.cli research TON")
            sys.exit(1)
        project_name = sys.argv[2]
        token_symbol = sys.argv[3] if len(sys.argv) > 3 else ""
        from .research.orchestrator import run_research
        from .research.report import generate_report
        console.print(f"[bold cyan]🔍 启动 Multi-Agent 深度研究: {project_name}[/]")
        console.print("[dim]5 个 Agent 将依次分析: GitHub·链上·社区·风险·Alpha...[/]")
        result = run_research(project_name, token=token_symbol)
        report_path = generate_report(result)
        console.print(f"\n[green]✓ 研究完成![/] {result.get('agents_ok',0)}/{result.get('agents_total',5)} Agents 成功")
        console.print(f"[green]📄 报告:[/] {report_path}")
        # 尝试打开报告
        import platform
        if platform.system() == "Darwin":
            import subprocess as sp
            sp.Popen(["open", str(report_path)])
    elif cmd == "serve":
        from .scheduler.runner import start
        start()
    elif cmd == "oss-check":
        # v0.7: 检测全部 OSS 集成模块可用性 (Phase 1 + Phase 2 + Phase 3)
        console.print("[bold cyan]🔌 OSS 集成 健康检查 (v0.7)[/]")
        rows = []
        # Phase 1
        try:
            from .research.alpha158_features import compute_alpha158  # noqa
            rows.append(("[P1] Alpha158 因子库", "[green]✓ 可用[/]", "src/research/alpha158_features.py"))
        except Exception as e:
            rows.append(("[P1] Alpha158 因子库", f"[red]✗ {e}[/]", ""))
        try:
            from .research import sentiment_bert as sb
            if sb.is_available():
                rows.append(("[P1] CryptoBERT", f"[green]✓ 已加载[/] ({sb.model_id()})", "transformers + torch"))
            else:
                rows.append(("[P1] CryptoBERT", "[yellow]降级到关键词[/]", "pip install transformers torch"))
        except Exception as e:
            rows.append(("[P1] CryptoBERT", f"[red]✗ {e}[/]", ""))
        try:
            from .research.onchain_real import cryo_health
            h = cryo_health()
            if h.get("installed"):
                rows.append(("[P1] cryo (链上)", f"[green]✓ 已装[/]", f"RPC: {h.get('eth_rpc')}"))
            else:
                rows.append(("[P1] cryo (链上)", "[yellow]未安装[/]", "brew install paradigmxyz/cryo/cryo"))
        except Exception as e:
            rows.append(("[P1] cryo (链上)", f"[red]✗ {e}[/]", ""))

        # Phase 2
        try:
            from .research import portfolio_backtest_vbt as vbt
            avail = vbt.is_available()
            rows.append(("[P2] vectorbt 回测", "[green]✓ 已装[/]" if avail else "[yellow]未装→fallback到旧回测[/]",
                        "pip install vectorbt" if not avail else "秒级参数扫描"))
        except Exception as e:
            rows.append(("[P2] vectorbt 回测", f"[red]✗ {e}[/]", ""))
        try:
            from .evolution import graph as eg
            avail = eg.is_available()
            rows.append(("[P2] LangGraph DAG", "[green]✓ 已装[/]" if avail else "[yellow]降级为顺序执行[/]",
                        "pip install langgraph" if not avail else "可观测/可重试"))
        except Exception as e:
            rows.append(("[P2] LangGraph DAG", f"[red]✗ {e}[/]", ""))
        try:
            from .adapters import ccxt_exchange as cx
            avail = cx.is_available()
            rows.append(("[P2] ccxt 统一交易所", "[green]✓ 已装[/]" if avail else "[yellow]未装→沿用单独 adapter[/]",
                        "pip install ccxt" if not avail else "5+ 交易所 fallback"))
        except Exception as e:
            rows.append(("[P2] ccxt 统一交易所", f"[red]✗ {e}[/]", ""))

        # Phase 3
        try:
            from .research import rd_agent
            rows.append(("[P3] RD-Agent skeleton", "[green]✓ 可用[/]", "假设→实验→评估→反馈"))
        except Exception as e:
            rows.append(("[P3] RD-Agent skeleton", f"[red]✗ {e}[/]", ""))
        try:
            from .adapters import cryo_warehouse as cw
            stats = cw.warehouse_stats()
            duck = "DuckDB✓" if stats["duckdb_available"] else "DuckDB✗"
            cryo = "cryo✓" if stats["cryo_available"] else "cryo✗"
            rows.append(("[P3] cryo 仓库 + DuckDB",
                        f"[yellow]文件={stats['total_parquet_files']} ({duck}, {cryo})[/]",
                        "pip install duckdb"))
        except Exception as e:
            rows.append(("[P3] cryo 仓库 + DuckDB", f"[red]✗ {e}[/]", ""))
        # Overfitting controls (Phase 2.5)
        try:
            from .research import overfitting as of_mod  # noqa
            rows.append(("[OF] PBO + DSR + Bonferroni",
                        "[green]✓ 可用[/]",
                        "src/research/overfitting.py · CLI: pbo"))
        except Exception as e:
            rows.append(("[OF] PBO + DSR + Bonferroni", f"[red]✗ {e}[/]", ""))
        # 实时预警 watchdog (v0.9 W4-A)
        try:
            from .research import watchdog as wd_mod
            n_checks = len(wd_mod.CHECKS)
            n_alerts = len(wd_mod.history(200))
            rows.append(("(v0.9) Watchdog 实时预警",
                        f"[green]✓ {n_checks} 检测器[/] · 历史告警 {n_alerts}",
                        "src/research/watchdog.py · CLI: watchdog"))
        except Exception as e:
            rows.append(("(v0.9) Watchdog 实时预警", f"[red]✗ {e}[/]", ""))
        # LLM 预算 + 熔断器 (v0.9 W3-M2)
        try:
            from .llm_budget import budget
            r = budget.report()
            t = r["today"]
            pct = t["budget_pct"]
            status = (
                f"[red]⛔ 熔断 OPEN[/]" if r["circuit"]["is_open"]
                else f"[green]✓ 今日 {t['total']:,}t / {r['budgets']['daily_cap']:,} ({pct}%)[/]"
            )
            rows.append(("(v0.9) LLM 预算+熔断器", status,
                        "src/llm_budget.py · CLI: llm-budget"))
        except Exception as e:
            rows.append(("(v0.9) LLM 预算+熔断器", f"[red]✗ {e}[/]", ""))
        # 统一 HTTP 客户端 (v0.9 W2-S3)
        try:
            from .http_client import http
            stats = http.stats()
            n_hosts = len(stats["hosts"])
            rows.append(("(v0.9) 统一 HTTP 客户端",
                        f"[green]✓ 已激活[/] ({n_hosts} hosts 在用)",
                        f"src/http_client.py · {stats['cache_files']} 缓存文件"))
        except Exception as e:
            rows.append(("(v0.9) 统一 HTTP 客户端", f"[red]✗ {e}[/]", ""))
        # 回测引擎路由 (v0.9 W2-S2)
        try:
            from .research import backtest_router as br_mod
            h = br_mod.health()
            engine = h["auto_chosen"]
            rows.append(("(v0.9) 回测引擎路由",
                        f"[green]✓ auto→{engine}[/]",
                        "src/research/backtest_router.py · CLI: backtest-router"))
        except Exception as e:
            rows.append(("(v0.9) 回测引擎路由", f"[red]✗ {e}[/]", ""))
        # DefiLlama 完整 API (v0.8)
        try:
            from .adapters import defillama_full as dlf_mod
            rows.append(("(v0.8) DefiLlama 31 端点",
                        "[green]✓ 可用[/]",
                        "src/adapters/defillama_full.py · CLI: defillama"))
        except Exception as e:
            rows.append(("(v0.8) DefiLlama 31 端点", f"[red]✗ {e}[/]", ""))

        console.print(f"\n{'模块':<28} {'状态':<35} 备注")
        console.print("─" * 95)
        for name, status, note in rows:
            console.print(f"  {name:<28} {status:<45} {note}")
        console.print(f"\n[dim]调研报告: docs/opensource_landscape.html[/]")
        console.print(f"[dim]冒烟测试: python scripts/smoke_oss_integrations.py[/]")
    elif cmd == "backtest-vbt":
        # Phase 2-A: vectorbt 回测
        from .research.portfolio_backtest_vbt import (
            run_walkforward_backtest_vbt, run_parameter_sweep_vbt, is_available
        )
        console.print("[bold cyan]📈 vectorbt 回测 (v0.7)[/]")
        if not is_available():
            console.print("[yellow]vectorbt 未装, 提示: pip install vectorbt[/]")
            console.print("[dim]当前会 fallback 到 python -m src.cli backtest-wf[/]")
            sys.exit(0)
        if "--sweep" in sys.argv:
            r = run_parameter_sweep_vbt()
            console.print_json(data=r)
        else:
            r = run_walkforward_backtest_vbt()
            console.print_json(data={k: v for k, v in r.items() if k != "equity_curve"})
    elif cmd == "backtest-router":
        # W2-S2: 统一 facade — 智能选 vbt 或 legacy
        from .research.backtest_router import run_backtest, run_sweep, health
        console.print("[bold cyan]📊 智能回测 (auto-engine)[/]")
        console.print_json(data=health())
        if "--sweep" in sys.argv:
            r = run_sweep()
        else:
            r = run_backtest()
        console.print_json(data={k: v for k, v in r.items() if k != "equity_curve"})
    elif cmd == "evolve-graph":
        # Phase 2-B: LangGraph 编排
        from .evolution.graph import run_evolution, get_last_state
        console.print("[bold cyan]🕸️ Evolution DAG (LangGraph 或顺序 fallback)[/]")
        state = run_evolution()
        console.print(f"  nodes_ok: {state.get('nodes_ok')}")
        console.print(f"  nodes_failed: {state.get('nodes_failed')}")
        if state.get("errors"):
            console.print(f"[yellow]  errors:[/] {len(state['errors'])}")
            for e in state["errors"][:3]:
                console.print(f"    - {e['node']}: {e['error'][:120]}")
    elif cmd == "ccxt-health":
        # Phase 2-C: ccxt 健康
        from .adapters.ccxt_exchange import health
        console.print_json(data=health())
    elif cmd == "rd-agent":
        # Phase 3-A: RD-Agent 自演化
        from .research.rd_agent import run_rd_agent, get_last_trajectory
        rounds = 3
        n_hyps = 5
        prefer_llm = "--llm" in sys.argv
        resume = "--resume" in sys.argv
        if "--rounds" in sys.argv:
            i = sys.argv.index("--rounds")
            try:
                rounds = int(sys.argv[i + 1])
            except (ValueError, IndexError):
                pass
        console.print(f"[bold cyan]🧬 RD-Agent (rounds={rounds}, n_hyps={n_hyps}, llm={prefer_llm})[/]")
        traj = run_rd_agent(rounds=rounds, n_hyps=n_hyps,
                            prefer_llm=prefer_llm, resume=resume)
        console.print(f"  rounds run: {len(traj.rounds)}")
        console.print(f"  factors promoted: {len(traj.promoted)}")
        if traj.promoted:
            for name in traj.promoted[:5]:
                console.print(f"    ★ {name}")
        console.print(f"[dim]  state: data/rd_agent/trajectory_latest.json[/]")
    elif cmd == "api-health":
        # v0.9: 真实跑所有 adapter + DefiLlama 端点, 列失败清单
        import time as _t
        from .http_client import http
        http.reset_metrics()
        console.print("[bold cyan]🩺 API 健康审计 (真实调用各 endpoint)[/]")

        results = []
        # 11 个老 adapter
        adapter_names = [
            "feargreed", "defillama", "defillama_extra", "okx", "coinbase",
            "binance", "coinglass", "farside", "cg_global", "cg_trending",
            "yfinance_macro",
        ]
        skip_cg = "--no-cg" in sys.argv
        for name in adapter_names:
            if skip_cg and name.startswith("cg") or skip_cg and name == "coingecko":
                console.print(f"  [dim]- {name:18s} skipped (--no-cg)[/]")
                continue
            try:
                mod = __import__(f"src.adapters.{name}", fromlist=[name])
                t0 = _t.time()
                rows = mod.fetch()
                n = len(rows) if rows else 0
                dt = _t.time() - t0
                marker = "[green]✓[/]" if n > 0 else "[yellow]⚠️[/]"
                console.print(f"  {marker} {name:18s} [dim]{n:>4} rows ({dt:.1f}s)[/]")
                results.append((name, n > 0, n))
            except Exception as e:
                console.print(f"  [red]✗[/] {name:18s} [red]{type(e).__name__}: {str(e)[:60]}[/]")
                results.append((name, False, 0))

        # DefiLlama 关键端点
        console.print()
        console.print("[bold]DefiLlama 完整版 6 关键端点[/]")
        from .adapters import defillama_full as dlf
        dlf_tests = [
            ("list_chains", dlf.list_chains),
            ("list_protocols", dlf.list_protocols),
            ("dex_overview", dlf.dex_overview),
            ("open_interest_overview", dlf.open_interest_overview),
            ("list_yield_pools", dlf.list_yield_pools),
            ("list_stablecoins", dlf.list_stablecoins),
        ]
        for name, fn in dlf_tests:
            try:
                r = fn()
                marker = "[green]✓[/]" if r else "[red]✗[/]"
                console.print(f"  {marker} dlf.{name}")
                results.append((f"dlf.{name}", bool(r), 0))
            except Exception as e:
                console.print(f"  [red]✗[/] dlf.{name}: {type(e).__name__}")
                results.append((f"dlf.{name}", False, 0))

        # HTTP stats
        console.print()
        console.print("[bold]HTTP per-host stats[/]")
        for row in http.stats()["hosts"]:
            marker = "[red]✗[/]" if row["errors"] > 0 else "[green]✓[/]"
            console.print(
                f"  {marker} {row['host']:35s} "
                f"calls={row['calls']:>3} cached={row['cached']:>3} "
                f"errors={row['errors']} rate_lim={row['rate_limited']} "
                f"avg={row['avg_latency_ms']}ms"
            )

        ok_count = sum(1 for _, ok, _ in results if ok)
        # 区分: 0 rows (graceful) vs 真 exception
        empty_count = sum(1 for _, ok, n in results if not ok)
        console.print()
        console.print(
            f"[bold]总结:[/] [green]{ok_count}[/] 拿到数据 · "
            f"[yellow]{empty_count}[/] 返回空/已 graceful "
            f"(都 [green]✓ 不抛异常[/], 看 ⚠️ 标记)"
        )
        console.print("[dim]已知 graceful 失败 (有替代源):[/]")
        console.print("[dim]  - binance (451 美国机房) → OKX 兜底[/]")
        console.print("[dim]  - farside (403 Cloudflare) → 暂无免费替代[/]")
        console.print("[dim]  - coinglass (整体改 API-key) → DefiLlama OI 兜底[/]")
        console.print("[dim]  - yfinance (未装) → pip install yfinance[/]")
    elif cmd == "watchdog":
        # W4-A: 实时风险预警 watchdog
        from .research import watchdog as wd
        sub = sys.argv[2] if len(sys.argv) > 2 else "check"
        no_push = "--no-push" in sys.argv
        console.print(f"[bold cyan]🚨 Watchdog · {sub}[/]")
        if sub == "check":
            alerts = wd.run_check(push=not no_push)
            if not alerts:
                console.print("[green]✓ 当前无告警触发[/]")
                console.print("[dim]  阈值: peg>50bps · funding>0.1% · F&G ≥80/≤20 · DEX TVL ±10%[/]")
            else:
                for a in alerts:
                    color = {"critical": "red", "warning": "yellow", "info": "blue"}.get(a["severity"], "white")
                    console.print(f"  [{color}]{a['title']}[/]")
                    console.print(f"    {a['detail']}")
        elif sub == "loop":
            interval = 300
            if "--interval" in sys.argv:
                i = sys.argv.index("--interval")
                try: interval = int(sys.argv[i + 1])
                except (ValueError, IndexError): pass
            console.print(f"[dim]循环间隔 {interval}s, Ctrl+C 退出[/]")
            wd.run_loop(interval_sec=interval, push=not no_push)
        elif sub == "history":
            n = 20
            for a in wd.history(n):
                color = {"critical": "red", "warning": "yellow", "info": "blue"}.get(a["severity"], "white")
                console.print(f"  {a['ts'][:19]}  [{color}]{a['title']}[/]")
        elif sub == "reset":
            wd.reset_state()
            console.print("[green]✓ 告警去重状态已清空[/]")
        else:
            console.print("[red]子命令: check | loop [--interval N] | history | reset[/]")
    elif cmd == "llm-budget":
        # W3-M2: LLM token 预算 + 熔断器报告
        from .llm_budget import budget
        sub = sys.argv[2] if len(sys.argv) > 2 else "report"
        if sub == "report":
            r = budget.report()
            t = r["today"]
            b = r["budgets"]
            c = r["circuit"]
            console.print(f"[bold cyan]💰 LLM Token 预算 (今天 {t['date']})[/]")
            pct_color = "green" if t["budget_pct"] < 50 else ("yellow" if t["budget_pct"] < 90 else "red")
            console.print(f"  今日: [{pct_color}]{t['total']:>8,} tokens / {b['daily_cap']:,} 上限 ({t['budget_pct']}%)[/]")
            console.print(f"        in={t['tokens_in']:,}  out={t['tokens_out']:,}  calls={t['calls']}  fails={t['failures']}")
            console.print(f"  本月: {b['monthly_used']:>8,} tokens / {b['monthly_cap']:,} 上限 ({b['monthly_pct']}%)")
            console.print()
            if c["is_open"]:
                console.print(f"[red]⛔ 熔断器 OPEN[/] (until {c['open_until']})")
                console.print(f"   连续失败: {c['consecutive_failures']}")
            else:
                console.print(f"[green]✓ 熔断器 CLOSED[/]  连续失败: {c['consecutive_failures']}")
            console.print()
            console.print("[bold]最近 7 天:[/]")
            for d in r["recent_7d"]:
                console.print(f"  {d['date']}  {d['tokens']:>6,} tokens  {d['calls']:>3} calls  {d['failures']} fails")
        elif sub == "reset":
            budget.reset_circuit()
            console.print("[green]✓ 熔断器已重置[/]")
        else:
            console.print("[red]子命令: report | reset[/]")
    elif cmd == "backfill":
        # W1-S1: 一键 backfill 历史快照, 让元学习启动
        import subprocess as _sp
        console.print("[bold cyan]📥 Backfill 历史快照[/]")
        days = 60
        top = 30
        if "--days" in sys.argv:
            i = sys.argv.index("--days")
            try: days = int(sys.argv[i + 1])
            except (ValueError, IndexError): pass
        if "--top" in sys.argv:
            i = sys.argv.index("--top")
            try: top = int(sys.argv[i + 1])
            except (ValueError, IndexError): pass
        script = Path(__file__).resolve().parents[1] / "scripts" / "backfill_snapshots.py"
        r = _sp.run([sys.executable, str(script), "--days", str(days), "--top", str(top)],
                    capture_output=False)
        console.print(f"\n[green]✓ exit code {r.returncode}[/]")
    elif cmd == "defillama":
        # v0.8: DefiLlama 完整免费 API 调用
        from .adapters import defillama_full as dlf
        sub = sys.argv[2] if len(sys.argv) > 2 else "health"
        console.print(f"[bold cyan]📡 DefiLlama · {sub}[/]")
        if sub == "health":
            console.print_json(data=dlf.health())
        elif sub == "chains":
            chains = dlf.list_chains() or []
            top = sorted(chains, key=lambda c: c.get("tvl", 0) or 0, reverse=True)[:15]
            for c in top:
                console.print(f"  {c.get('name', '?'):20s}  TVL=${(c.get('tvl', 0) or 0)/1e9:.2f}B")
        elif sub == "protocols":
            top = dlf.get_top_protocols_by_tvl(20)
            for p in top:
                console.print(f"  {p.get('name', '?'):25s}  TVL=${(p.get('tvl', 0) or 0)/1e9:.2f}B  "
                              f"7d={p.get('change_7d', 0) or 0:+.1f}%  chain={p.get('chain', '?')}")
        elif sub == "stables":
            pegs = dlf.get_stable_peg_health()
            for sym, d in list(pegs.items())[:15]:
                color = "green" if d["status"] == "ok" else ("yellow" if d["status"] == "deviating" else "red")
                console.print(f"  {sym:8s}  price={d['price']:.4f}  "
                              f"[{color}]dev={d['deviation_pct']:+.3f}%[/]  {d['status']}")
        elif sub == "dex":
            shares = dlf.get_chain_dex_volume_share()
            top = sorted(shares.items(), key=lambda x: x[1], reverse=True)[:15]
            for c, s in top:
                bar = "█" * int(s * 60)
                console.print(f"  {c:20s}  {s*100:6.2f}%  {bar}")
        elif sub == "perp":
            for p in dlf.get_perp_oi_by_protocol()[:10]:
                console.print(f"  {p['name']:25s}  OI=${p['open_interest_usd']/1e9:.2f}B  "
                              f"24h={p.get('change_24h')}  7d={p.get('change_7d')}")
        elif sub == "yields":
            stable = "--stable" in sys.argv
            ys = dlf.get_top_yield_opportunities(stable_only=stable, max_apy=200)[:15]
            for y in ys:
                console.print(f"  {y['chain']:10s} {y['project']:18s} {y['symbol']:22s} "
                              f"APY=[yellow]{y['apy']:6.1f}%[/]  TVL=${y['tvl_usd']/1e6:.0f}M")
        elif sub == "factors":
            from .factors.defillama_factors import compute_all_defillama_factors
            res = compute_all_defillama_factors()
            console.print("\n[bold]TVL Momentum (Top 5):[/]")
            for slug, d in list(res["tvl_momentum"].items())[:5]:
                console.print(f"  {slug:25s}  {d['tvl_change_pct']*100:+6.2f}%  score={d['momentum_score']:+.3f}")
            console.print("\n[bold]DEX Volume Growth (Top 5):[/]")
            for name, d in list(res["dex_volume_growth"].items())[:5]:
                console.print(f"  {name:25s}  ratio={d['growth_ratio']:.2f}  score={d['score']:+.3f}")
            peg = res["stable_peg_deviation"]
            console.print(f"\n[bold]Stable Peg:[/] {peg.get('interpretation')}")
            ys = res["yield_spike"]
            console.print(f"[bold]Yield Regime:[/] {ys.get('interpretation')}")
        elif sub == "clear-cache":
            dlf.clear_cache()
            console.print("[green]✓ cache cleared[/]")
        else:
            console.print("[red]子命令: health | chains | protocols | stables | dex | perp | yields [--stable] | factors | clear-cache[/]")
    elif cmd == "pbo":
        # Phase 2.5: PBO 评估 — 对最近的参数扫描跑过拟合诊断
        console.print("[bold cyan]🔬 PBO / DSR 过拟合诊断[/]")
        from .research import overfitting as of_mod
        from .research.portfolio_backtest_vbt import (
            run_parameter_sweep_vbt, _vbt_available,
        )

        if not _vbt_available():
            console.print("[yellow]vectorbt 未装, 用合成数据演示 PBO 工具[/]\n")
            # 演示模式: 跑合成数据
            import numpy as np
            rng = np.random.default_rng(42)
            T = 252
            N = int(sys.argv[2]) if len(sys.argv) > 2 else 30
            console.print(f"[dim]生成 {T}d x {N} 配置的随机收益矩阵 (纯噪声)...[/]")
            R = rng.normal(0, 0.01, size=(T, N))
            res = of_mod.diagnose_backtest(R)
        else:
            console.print("[dim]跑参数扫描并诊断 ...[/]")
            sweep = run_parameter_sweep_vbt(diagnose_overfitting=True)
            res = sweep.get("overfit", {})
            if sweep.get("best_config"):
                bc = sweep["best_config"]
                console.print(f"\nbest_config: Top-{bc['top_n']} {bc['rebalance_days']}d "
                              f"Sharpe={bc['sharpe']:.2f} | overfit_verdict={bc.get('overfit_verdict')}")
                console.print()

        if "error" in res:
            console.print(f"[red]{res['error']}[/]")
            sys.exit(1)

        # 渲染结果
        pbo = res.get("pbo", {})
        dsr = res.get("dsr", {})
        mt = res.get("multiple_testing", {})

        def _color(v):
            return "green" if v == "robust" else "yellow" if v == "borderline" else "red"

        if pbo:
            c = _color(pbo.get("verdict", "n/a"))
            console.print(f"[bold]PBO (Bailey 2014):[/] [{c}]{pbo.get('pbo')}[/] "
                          f"({pbo.get('verdict')})")
            console.print(f"  {pbo.get('interpretation', '')}")
            console.print(f"  n_strategies={pbo.get('n_strategies')}, "
                          f"combos={pbo.get('n_combos_tested')}")

        if dsr:
            c = _color(dsr.get("verdict", "n/a"))
            console.print(f"\n[bold]DSR (Deflated Sharpe):[/] [{c}]{dsr.get('dsr')}[/] "
                          f"({dsr.get('verdict')})")
            console.print(f"  {dsr.get('interpretation', '')}")
            console.print(f"  SR observed={dsr.get('sr_observed')}, "
                          f"SR threshold (deflation)={dsr.get('sr_threshold')}")

        if mt:
            console.print(f"\n[bold]多重检验 (Bonferroni):[/]")
            console.print(f"  N={mt.get('n_tests')} candidates → "
                          f"alpha_corrected={mt.get('alpha_corrected')}, "
                          f"z={mt.get('z_threshold')}")
            console.print(f"  IC floors: {mt.get('ic_floor')}")

        overall = res.get("overall_verdict", "n/a")
        c = _color(overall)
        console.print(f"\n[bold]Overall verdict:[/] [{c}]{overall.upper()}[/]")
        console.print(f"[dim]{res.get('summary', '')}[/]")
    elif cmd == "warehouse":
        # Phase 3-B: cryo 仓库管理
        from .adapters.cryo_warehouse import (
            warehouse_stats, list_partitions, cex_flow_summary,
            ingest_token, ingest_all, top_whales
        )
        sub = sys.argv[2] if len(sys.argv) > 2 else "stats"
        if sub == "stats":
            console.print_json(data=warehouse_stats())
        elif sub == "ingest":
            tok = sys.argv[3] if len(sys.argv) > 3 else None
            r = ingest_token(tok) if tok else ingest_all()
            console.print_json(data=r)
        elif sub == "flow":
            tok = sys.argv[3] if len(sys.argv) > 3 else "USDT"
            console.print_json(data=cex_flow_summary(tok))
        elif sub == "whales":
            tok = sys.argv[3] if len(sys.argv) > 3 else "USDT"
            for w in top_whales(tok)[:10]:
                console.print(w)
        else:
            console.print("[red]子命令: stats | ingest <TOKEN> | flow <TOKEN> | whales <TOKEN>[/]")
    else:
        console.print(f"[red]Unknown command:[/] {cmd}")
        console.print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
