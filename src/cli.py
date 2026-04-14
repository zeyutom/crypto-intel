"""Crypto Intel CLI。

用法:
    python -m src.cli init           初始化数据库
    python -m src.cli ingest         跑一轮数据采集
    python -m src.cli factors        计算因子 + 合成信号
    python -m src.cli review         跑复核检查
    python -m src.cli report         生成日报 HTML
    python -m src.cli all            一键跑全流程并生成日报
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
    elif cmd == "serve":
        from .scheduler.runner import start
        start()
    else:
        console.print(f"[red]Unknown command:[/] {cmd}")
        console.print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
