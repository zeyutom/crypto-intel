"""Crypto Intel · 一键控制中心 (v0.7)

不写代码也能跑完整套工作流:
  - 一键检查所有模块状态
  - 一键安装可选依赖 (vectorbt / langgraph / ccxt / transformers / duckdb)
  - 一键跑筛选 / 回测 / PBO 诊断 / RD-Agent / 飞书推送
  - 实时显示日志
  - 结果文件一键打开
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import streamlit as st

# 让 streamlit 找到 src/
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

st.set_page_config(page_title="🚀 控制中心 · Crypto Intel", layout="wide")
st.title("🚀 Crypto Intel · 控制中心")
st.caption("不用写代码 — 每个按钮都是一个完整流程")

# 🛑 全局任务管理 — 显示当前跑的子进程 + 一键 kill
_running = st.session_state.get("running_pids", {})
if _running:
    cols = st.columns([4, 1])
    with cols[0]:
        st.info(f"🔄 当前后台任务: {len(_running)} 个 · {list(_running.keys())[:3]}")
    with cols[1]:
        if st.button("🛑 全部终止", key="kill_all_top"):
            killed = []
            try:
                # 函数在脚本下方定义, 这里前向引用通过 globals
                killed = globals().get("kill_all_running", lambda: [])()
            except Exception:
                pass
            st.success(f"已终止 {len(killed)} 个任务")
            st.rerun()

# ── 共用工具 ─────────────────────────────────────────────────────────

def run_cmd(args: list[str], cwd: Path = ROOT, timeout: int = 1800,
            label: str = None) -> dict:
    """在子进程跑命令, 实时把 stdout/stderr 写到 streamlit.

    v0.9: 加上"❌ 取消"按钮 — 用 st.session_state 保存运行中的 PID,
    用户随时能终止 hang 住的任务.
    """
    import signal
    t0 = time.time()
    log_box = st.empty()
    cancel_box = st.empty()
    log = ""
    label = label or " ".join(args[-3:])

    # 初始化 running tasks 注册表
    if "running_pids" not in st.session_state:
        st.session_state["running_pids"] = {}

    try:
        proc = subprocess.Popen(
            args, cwd=str(cwd),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            # 启用新进程组, kill 时连子进程一起干掉
            start_new_session=True,
        )
        # 注册 PID
        task_key = f"{label}_{proc.pid}"
        st.session_state["running_pids"][task_key] = proc.pid

        # 渲染 cancel 按钮 (注: streamlit 同步执行, 真正的"中途取消"需要 polling)
        cancel_box.warning(
            f"⏳ 正在跑 `{label}` (PID {proc.pid})  ·  "
            f"卡住可在终端按 Ctrl+C 或 `kill {proc.pid}`"
        )

        # 用 select 轮询, 即使子进程长时间无输出也能按时触发超时 (旧版 readline 会阻塞死等)
        import select
        import signal
        timed_out = False
        while True:
            if time.time() - t0 > timeout:
                timed_out = True
                break
            rlist, _, _ = select.select([proc.stdout], [], [], 1.0)
            if rlist:
                line = proc.stdout.readline()
                if not line:
                    break  # EOF: 输出结束
                log += line
                log_box.code(log[-5000:], language="bash")
            elif proc.poll() is not None:
                break  # 进程已退出且无新输出
        if timed_out:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                proc.kill()
            log += f"\n[超时 {timeout}s, 已中止]"

        proc.wait()
        # 清理 cancel 提示
        cancel_box.empty()
        # 从 running 列表移除
        st.session_state["running_pids"].pop(task_key, None)

        return {"ok": proc.returncode == 0, "code": proc.returncode,
                "log": log, "elapsed": round(time.time() - t0, 1),
                "pid": proc.pid}
    except Exception as e:
        cancel_box.empty()
        return {"ok": False, "code": -1, "log": log + f"\n{e}",
                "elapsed": round(time.time() - t0, 1)}


def kill_all_running():
    """终止所有 session 里登记的子进程 (用户点 'kill all' 时调)."""
    import signal
    pids = st.session_state.get("running_pids", {})
    killed = []
    for key, pid in list(pids.items()):
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            killed.append((key, pid))
        except (ProcessLookupError, PermissionError):
            pass
    st.session_state["running_pids"] = {}
    return killed


def pip_install(packages: list[str]) -> dict:
    return run_cmd(
        [sys.executable, "-m", "pip", "install", "--upgrade",
         *packages, "--break-system-packages", "-q"]
    )


def cli(args: list[str]) -> dict:
    """跑 src.cli 子命令."""
    return run_cmd([sys.executable, "-m", "src.cli", *args])


# ── 选项卡布局 ───────────────────────────────────────────────────────

tabs = st.tabs([
    "🩺 健康检查",
    "⚡ 第一次跑",
    "📈 日常筛选 / 回测",
    "🔬 PBO 过拟合诊断",
    "🧬 因子发现 / 自演化",
    "📡 DefiLlama 数据",
    "🚨 实时预警",
    "🌙 夜间自动化",
    "💰 LLM 预算",
    "🛠️ 安装可选依赖",
    "📂 结果文件",
])

# ────────────────────────────────────────────────────────────────────
#  Tab 1: 健康检查
# ────────────────────────────────────────────────────────────────────
with tabs[0]:
    st.subheader("🩺 系统健康检查")
    st.write("看看哪些模块已经装好了, 哪些还没装。**首次进来建议先点这里。**")
    if st.button("▶️ 跑 oss-check", type="primary", key="run_oss_check"):
        with st.spinner("检查中..."):
            res = cli(["oss-check"])
        if res["ok"]:
            st.success(f"完成 ({res['elapsed']}s)")
        else:
            st.error(f"出错 (code={res['code']})")

# ────────────────────────────────────────────────────────────────────
#  Tab 2: 第一次跑
# ────────────────────────────────────────────────────────────────────
with tabs[1]:
    st.subheader("⚡ 第一次跑 · 从零到一份日报")
    st.markdown("""
    建议**按顺序**点这 4 个按钮 (每步约 1-3 分钟):

    1. **初始化数据库** — 建本地 SQLite 表 (秒级)
    2. **采集数据** — 拉 CoinGecko/Binance/DeFiLlama 等公开数据 (~30s)
    3. **算因子 + 合成信号** — 跑 5 个核心因子 (~10s)
    4. **生成日报 HTML** — 在 data/reports/ 下生成可视化报告
    """)

    c1, c2, c3, c4 = st.columns(4)
    if c1.button("1️⃣ 初始化", key="init_db"):
        res = cli(["init"])
        (st.success if res["ok"] else st.error)(f"完成 ({res['elapsed']}s)")
    if c2.button("2️⃣ 采集数据", key="ingest"):
        res = cli(["ingest"])
        (st.success if res["ok"] else st.error)(f"完成 ({res['elapsed']}s)")
    if c3.button("3️⃣ 算因子", key="factors"):
        res = cli(["factors"])
        (st.success if res["ok"] else st.error)(f"完成 ({res['elapsed']}s)")
    if c4.button("4️⃣ 生成日报", key="report"):
        res = cli(["report"])
        (st.success if res["ok"] else st.error)(f"完成 ({res['elapsed']}s)")

    st.divider()
    st.caption("懒人模式: 一键跑完上面 4 步")
    if st.button("🎯 一键跑全流程 (init → ingest → factors → report)",
                 type="primary", key="all_flow"):
        with st.spinner("跑全流程中..."):
            res = cli(["all-no-llm"])
        if res["ok"]:
            st.success(f"✅ 全流程完成 ({res['elapsed']}s)")
            st.info("结果看左侧 sidebar 其他页面, 或者去 [📂 结果文件] tab")
        else:
            st.error(f"出错 (code={res['code']})")

    st.divider()
    st.markdown("**📥 Backfill 历史快照 (让元学习启动)**")
    st.caption("从 DefiLlama 拉历史价格构造合成快照. 跑过一次后, "
               "PBO/回测/元学习就能在 90 天历史上跑. 约 1-2 分钟.")
    c1, c2, c3 = st.columns([1, 1, 2])
    days = c1.number_input("天数", min_value=30, max_value=180, value=60, key="bf_days")
    top = c2.number_input("Top N 币种", min_value=10, max_value=50, value=25, key="bf_top")
    if c3.button("📥 Backfill 历史快照", type="primary", key="backfill"):
        res = cli(["backfill", "--days", str(days), "--top", str(top)])
        (st.success if res["ok"] else st.error)(f"完成 ({res['elapsed']}s)")

# ────────────────────────────────────────────────────────────────────
#  Tab 3: 日常筛选 / 回测
# ────────────────────────────────────────────────────────────────────
with tabs[2]:
    st.subheader("📈 日常: 跑 Top-500 多因子筛选 + 回测")
    st.markdown("""
    - **多因子筛选** — 用 10 因子 + 元学习自动调权, 输出 Top 候选
    - **Walk-forward 回测 (旧)** — 验证策略在历史上能不能赚钱
    - **vectorbt 回测 (新)** — 秒级参数扫描, 自动跑 PBO 诊断
    """)

    c1, c2 = st.columns(2)
    with c1:
        if st.button("📊 跑 Top-500 多因子筛选", type="primary", key="screen"):
            with st.spinner("筛选中 (~2 分钟, 拉公开 API)..."):
                res = cli(["screen"])
            (st.success if res["ok"] else st.error)(f"完成 ({res['elapsed']}s)")
        if st.button("📅 跑 daily-screen (含飞书推送配置)", key="daily_screen"):
            with st.spinner("跑中..."):
                res = cli(["daily-screen"])
            (st.success if res["ok"] else st.error)(f"完成 ({res['elapsed']}s)")

    with c2:
        if st.button("📉 walk-forward 回测 (旧引擎)", key="bt_wf"):
            res = cli(["backtest-wf"])
            (st.success if res["ok"] else st.error)(f"完成 ({res['elapsed']}s)")
        if st.button("⚡ vectorbt 参数扫描 (新引擎, 含 PBO)",
                     type="primary", key="bt_vbt"):
            res = cli(["backtest-vbt", "--sweep"])
            (st.success if res["ok"] else st.error)(f"完成 ({res['elapsed']}s)")

# ────────────────────────────────────────────────────────────────────
#  Tab 4: PBO 过拟合诊断
# ────────────────────────────────────────────────────────────────────
with tabs[3]:
    st.subheader("🔬 PBO / DSR 过拟合诊断")
    st.markdown("""
    根据 Bailey 等 (2014) 论文实现, 帮你判断:
    - 一组参数扫描里的 "best Sharpe" 是真信号还是过拟合?
    - 多重检验下, IC=0.03 到底显不显著?
    """)
    n_demo = st.slider("演示用候选数 (没装 vectorbt 时跑合成数据)",
                       min_value=5, max_value=100, value=30)
    if st.button("🧪 跑 PBO 诊断", type="primary", key="pbo_run"):
        with st.spinner("PBO 计算中..."):
            res = cli(["pbo", str(n_demo)])
        (st.success if res["ok"] else st.error)(f"完成 ({res['elapsed']}s)")

    st.divider()
    st.caption("📚 怎么读结果:")
    st.markdown("""
    - **PBO < 30%** = 稳健 (绿) ✅ · **30-50%** = 警惕 (黄) ⚠️ · **> 50%** = 严重过拟合 (红) ❌
    - **DSR ≥ 95%** = 真实显著 ✅ · **80-95%** = 边缘 ⚠️ · **< 80%** = 大概率噪声 ❌
    - **Bonferroni IC floor** = 在 N 个候选下, IC 需要 ≥ 多少才算显著 (随 N 增大)
    """)

# ────────────────────────────────────────────────────────────────────
#  Tab 5: 因子发现 / 自演化
# ────────────────────────────────────────────────────────────────────
with tabs[4]:
    st.subheader("🧬 因子发现 + RD-Agent 自演化")
    st.markdown("""
    - **alpha-discovery** — LLM 或离线变异生成候选因子 → IC 回测 → 晋升/淘汰
    - **rd-agent** — 假设→实验→评估→反馈 4 阶段循环, **自动加 Bonferroni 校正**
    - **evolve-graph** — 跑一整轮 LangGraph 编排的 evolution
    """)

    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown("**Alpha Discovery**")
        if st.button("🔍 跑离线变异 (不调 LLM)", key="alpha_offline"):
            res = cli(["discover-alpha"])
            (st.success if res["ok"] else st.error)(f"完成 ({res['elapsed']}s)")
    with c2:
        st.markdown("**RD-Agent**")
        rd_rounds = st.number_input("rounds", min_value=1, max_value=10, value=1)
        if st.button("🧬 跑 RD-Agent", type="primary", key="rd_agent"):
            res = cli(["rd-agent", "--rounds", str(rd_rounds)])
            (st.success if res["ok"] else st.error)(f"完成 ({res['elapsed']}s)")
    with c3:
        st.markdown("**Evolution DAG**")
        if st.button("🕸️ 跑 evolution graph", key="evo_graph"):
            res = cli(["evolve-graph"])
            (st.success if res["ok"] else st.error)(f"完成 ({res['elapsed']}s)")

# ────────────────────────────────────────────────────────────────────
#  Tab 6: DefiLlama 数据 (v0.8)
# ────────────────────────────────────────────────────────────────────
with tabs[5]:
    st.subheader("📡 DefiLlama 完整免费 API · 31 端点 + 4 衍生因子")
    st.markdown("""
    **免费, 无需 API key.** 覆盖 TVL / Coins / Stablecoins / Yields / DEX / Perp OI / Fees.
    数据缓存在 `data/cache/defillama/`, 重复请求秒级返回。
    """)

    sub_tabs = st.tabs([
        "🌐 链总览", "🏆 Top 协议", "💰 稳定币脱锚", "📊 DEX 成交量份额",
        "♾️ Perp 持仓", "🌾 高 APY 池子", "🧪 4 个衍生因子", "🔧 工具",
    ])

    with sub_tabs[0]:
        st.caption("各链当前 TVL 排名 (top 15)")
        if st.button("▶️ 拉取", key="dlf_chains"):
            res = cli(["defillama", "chains"])
            (st.success if res["ok"] else st.error)(f"完成 ({res['elapsed']}s)")

    with sub_tabs[1]:
        st.caption("Top 20 协议 (按 TVL 排序) + 7 天涨跌")
        if st.button("▶️ 拉取", key="dlf_protocols"):
            res = cli(["defillama", "protocols"])
            (st.success if res["ok"] else st.error)(f"完成 ({res['elapsed']}s)")

    with sub_tabs[2]:
        st.caption("所有稳定币当前价格 + 脱锚程度 (绿=ok, 黄=偏离, 红=脱锚)")
        if st.button("▶️ 拉取", key="dlf_stables"):
            res = cli(["defillama", "stables"])
            (st.success if res["ok"] else st.error)(f"完成 ({res['elapsed']}s)")

    with sub_tabs[3]:
        st.caption("各链 DEX 成交量市占率 (近 24h)")
        if st.button("▶️ 拉取", key="dlf_dex"):
            res = cli(["defillama", "dex"])
            (st.success if res["ok"] else st.error)(f"完成 ({res['elapsed']}s)")

    with sub_tabs[4]:
        st.caption("Top 10 Perp DEX 当前持仓量 + 涨跌")
        if st.button("▶️ 拉取", key="dlf_perp"):
            res = cli(["defillama", "perp"])
            (st.success if res["ok"] else st.error)(f"完成 ({res['elapsed']}s)")

    with sub_tabs[5]:
        st.caption("高 APY 池子 (TVL > $10M, APY < 200% 防刷量)")
        stable_only = st.checkbox("只看稳定币池子", value=False, key="dlf_yields_stable")
        if st.button("▶️ 拉取", key="dlf_yields"):
            args = ["defillama", "yields"]
            if stable_only:
                args.append("--stable")
            res = cli(args)
            (st.success if res["ok"] else st.error)(f"完成 ({res['elapsed']}s)")

    with sub_tabs[6]:
        st.caption(
            "**4 个衍生因子** · TVL Momentum / DEX Volume Growth / "
            "Stable Peg Deviation / Yield Regime"
        )
        if st.button("▶️ 一键算 4 个因子", type="primary", key="dlf_factors"):
            res = cli(["defillama", "factors"])
            (st.success if res["ok"] else st.error)(f"完成 ({res['elapsed']}s)")

    with sub_tabs[7]:
        st.caption("健康检查 + 缓存管理")
        col1, col2 = st.columns(2)
        if col1.button("🩺 健康检查", key="dlf_health"):
            res = cli(["defillama", "health"])
            (st.success if res["ok"] else st.error)(f"完成 ({res['elapsed']}s)")
        if col2.button("🗑️ 清空缓存", key="dlf_clear"):
            res = cli(["defillama", "clear-cache"])
            (st.success if res["ok"] else st.error)(f"完成 ({res['elapsed']}s)")

    st.divider()
    with st.expander("📚 31 个端点和 4 个因子干啥用"):
        st.markdown("""
        **31 个原始端点** 在 `src/adapters/defillama_full.py`, 分 6 大类:

        - **TVL** (6): protocols / protocol/{slug} / tvl/{slug} / chains / chain_tvl_history
        - **Coins** (7): current_prices / chart / percentage / batch_historical / ...
        - **Stablecoins** (6): list / chart / peg deviation 等
        - **Yields** (2): pools / pool APY history
        - **DEX & Options** (6): overview / by chain / summary
        - **Perp OI** (1) + **Fees** (3)

        **4 个衍生因子** (`src/factors/defillama_factors.py`):

        | 因子 | 用途 | 数据源 |
        |------|------|--------|
        | TVL Momentum 7d | 哪个协议在抽资金/吸资金 | /protocol/{slug} |
        | DEX Volume Growth | 突然放量预示叙事/事件 | /overview/dexs |
        | Stable Peg Deviation | USDT/USDC 加权脱锚 = 流动性紧张 | /stablecoins |
        | Yield Spike Regime | 稳定币池子中位 APY = 资金成本 | /pools |
        """)


# ────────────────────────────────────────────────────────────────────
#  Tab 7: 实时预警 watchdog (v0.9 W4-A)
# ────────────────────────────────────────────────────────────────────
with tabs[6]:
    st.subheader("🚨 实时风险预警 Watchdog")
    st.markdown("""
    监听 4 个关键阈值, 触发即推飞书 + 写本地日志:
    - **稳定币加权脱锚 > 50bps** (流动性紧张/危机预警)
    - **主流币 funding rate > 0.1%** (多空过度拥挤)
    - **Fear & Greed ≥80 或 ≤20** (极端情绪反转)
    - **主流 DEX TVL 24h ±10%** (DeFi 风险)

    阈值在 `.env` 配:
    `WATCHDOG_PEG_BPS=50 / WATCHDOG_FUNDING_PCT=0.10 / WATCHDOG_FG_HIGH=80 / WATCHDOG_FG_LOW=20 / WATCHDOG_TVL_DROP_PCT=0.10`
    """)

    col1, col2, col3 = st.columns(3)
    if col1.button("🩺 跑一次检查", type="primary", key="wd_check"):
        res = cli(["watchdog", "check", "--no-push"])
        (st.success if res["ok"] else st.error)(f"完成 ({res['elapsed']}s)")

    if col2.button("📜 看历史告警 (近 20)", key="wd_history"):
        res = cli(["watchdog", "history"])
        (st.success if res["ok"] else st.error)(f"完成 ({res['elapsed']}s)")

    if col3.button("🔄 重置去重状态", key="wd_reset"):
        res = cli(["watchdog", "reset"])
        (st.success if res["ok"] else st.error)(f"完成 ({res['elapsed']}s)")

    st.divider()
    st.caption("💡 24h 内同一类型告警只发一次, 避免刷屏. 推送到 .env 配的 FEISHU_GROUP_*_URL")

    # 显示近 10 条告警
    try:
        from src.research.watchdog import history as wd_history
        hist = wd_history(10)
        if hist:
            st.markdown("**最近 10 条告警**")
            import pandas as pd
            df = pd.DataFrame(hist)
            if not df.empty:
                cols = ["ts", "severity", "type", "title", "value", "threshold"]
                df = df[[c for c in cols if c in df.columns]]
                st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info("还没有任何告警记录 (没触发, 或还没跑过 watchdog check)")
    except Exception as e:
        st.error(f"加载历史告警失败: {e}")


# ────────────────────────────────────────────────────────────────────
#  Tab 8: 夜间自动化 (v0.9 nightly)
# ────────────────────────────────────────────────────────────────────
with tabs[7]:
    st.subheader("🌙 夜间自动化 · 让系统替你跑")
    st.markdown("""
    **三种调度方式** (任选, 也可同时开):

    1. **本地 Mac launchd** — 双击项目根目录的 `01_设置夜间自动运行.command`,
       选 1 安装. 每天 03:00 自动跑全套, 30 分钟跑一次 watchdog.
       电脑睡眠也没事, 醒来补跑.
    2. **GitHub Actions 云端** — push 到 main 后, `.github/workflows/nightly.yml`
       会在 UTC 19:00 (北京 03:00) 自动跑. 不依赖本地开机.
       前提是配好 Secrets (FEISHU_GROUP_1_URL / ANTHROPIC_API_KEY).
    3. **APScheduler 常驻** — `python -m src.cli serve`, 终端不能关.

    **nightly 一轮跑什么** (约 5-15 分钟):
    backfill 增量 → ingest → factors → snapshot → backtest+PBO → RD-Agent 1 轮 →
    discover-alpha → watchdog → 推飞书简报
    """)

    col1, col2, col3 = st.columns(3)

    if col1.button("▶️ 立刻跑一次 nightly", type="primary", key="nightly_run"):
        with st.spinner("跑 nightly 中 (约 5-15 分钟)..."):
            res = run_cmd(["bash", str(ROOT / "scripts" / "nightly_run.sh")],
                          label="nightly", timeout=1200)
        if res["ok"]:
            st.success(f"✅ 完成 ({res['elapsed']}s)")
        else:
            st.warning(f"⚠️ 部分步骤失败 (code={res['code']}), 看下方日志")

    if col2.button("🩺 看 launchd 状态", key="launchd_status"):
        res = run_cmd(["bash", "-c", "launchctl list | grep cryptointel || echo '(无 cryptointel 任务)'"],
                      label="launchd-status", timeout=10)
        if res["ok"]:
            st.success(f"完成 ({res['elapsed']}s)")

    if col3.button("📊 health-check", key="api_health_btn"):
        res = cli(["api-health", "--no-cg"])
        (st.success if res["ok"] else st.error)(f"完成 ({res['elapsed']}s)")

    st.divider()

    # 最近 7 天 nightly 日志
    st.markdown("### 📋 最近 7 天 nightly 运行情况")
    log_dir = ROOT / "data" / "nightly_logs"
    if not log_dir.exists():
        st.info("还没有任何 nightly 运行记录. 点上面 ▶️ 立刻跑一次 nightly 开始.")
    else:
        log_files = sorted(log_dir.glob("*.log"), reverse=True)[:7]
        if not log_files:
            st.caption("(目录存在但还没日志)")
        else:
            import pandas as pd
            rows = []
            for f in log_files:
                date = f.stem
                size_kb = round(f.stat().st_size / 1024, 1)
                mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%H:%M")
                # 读 failures
                fail_file = f.parent / f"{date}.failures.json"
                n_fails = 0
                if fail_file.exists():
                    try:
                        n_fails = len(json.loads(fail_file.read_text()).get("failures", []))
                    except Exception:
                        pass
                # 读 log tail 看是否完成
                try:
                    tail = f.read_text(errors="replace")[-2000:]
                    completed = "Nightly 完成" in tail
                except Exception:
                    tail = ""
                    completed = False
                rows.append({
                    "日期": date,
                    "状态": ("✅ 完成" if completed else "⚠️ 未完成") + (f" ({n_fails} 失败)" if n_fails else ""),
                    "大小 KB": size_kb,
                    "时间": mtime,
                })
            df = pd.DataFrame(rows)
            st.dataframe(df, use_container_width=True, hide_index=True)

            # 查看某一天的完整日志
            st.markdown("**查看某天日志**")
            sel_log = st.selectbox("选日期", [f.stem for f in log_files], key="log_sel")
            if sel_log:
                log_path = log_dir / f"{sel_log}.log"
                if log_path.exists():
                    log_content = log_path.read_text(errors="replace")
                    with st.expander(f"📄 {sel_log}.log (尾部 100 行)"):
                        st.code("\n".join(log_content.split("\n")[-100:]), language="bash")
                    # 失败明细
                    fail_path = log_dir / f"{sel_log}.failures.json"
                    if fail_path.exists():
                        try:
                            fails = json.loads(fail_path.read_text())
                            if fails.get("failures"):
                                st.warning(f"⚠️ 当日 {len(fails['failures'])} 步失败:")
                                st.json(fails)
                        except Exception:
                            pass

    st.divider()
    st.caption(
        "💡 **如何启动**: 关闭本网页, 打开 Finder → 项目根目录 → 双击 "
        "**`01_设置夜间自动运行.command`** → 选 1 安装. 之后每晚 03:00 自动跑."
    )


# ────────────────────────────────────────────────────────────────────
#  Tab 9: LLM 预算 + 熔断器 (v0.9 W3-M2)
# ────────────────────────────────────────────────────────────────────
with tabs[8]:
    st.subheader("💰 LLM Token 预算 + 熔断器")
    st.markdown("""
    防止 evolution agent / RD-Agent / alpha-discovery / sentiment-nlp 等
    LLM 调用失控烧光配额. 配置在 `crypto-intel/.env`:
    ```
    MAX_TOKENS_PER_DAY=200000       # 默认 20 万
    MAX_TOKENS_PER_MONTH=5000000    # 默认 500 万
    MAX_FAILS_BEFORE_BREAK=5        # 连续失败几次后熔断
    LLM_COOLDOWN_MINUTES=60         # 熔断冷却时长
    ```
    """)

    try:
        from src.llm_budget import budget
        r = budget.report()
        t = r["today"]
        b = r["budgets"]
        c = r["circuit"]

        # Top row: 今日 + 本月用量
        col1, col2, col3 = st.columns(3)
        col1.metric(
            "今日 tokens",
            f"{t['total']:,}",
            f"{t['budget_pct']}% of {b['daily_cap']:,}",
            delta_color="inverse" if t['budget_pct'] > 80 else "normal",
        )
        col2.metric(
            "本月 tokens",
            f"{b['monthly_used']:,}",
            f"{b['monthly_pct']}% of {b['monthly_cap']:,}",
            delta_color="inverse" if b['monthly_pct'] > 80 else "normal",
        )
        col3.metric(
            "今日调用 / 失败",
            f"{t['calls']} / {t['failures']}",
            delta_color="inverse",
        )

        # 熔断器状态
        if c["is_open"]:
            st.error(f"⛔ 熔断器 OPEN — 冷却到 {c['open_until']} "
                     f"(连续失败 {c['consecutive_failures']} 次)")
            if st.button("🔓 手动重置熔断器", key="reset_circuit"):
                budget.reset_circuit()
                st.rerun()
        else:
            st.success(f"✓ 熔断器 CLOSED  ·  连续失败 {c['consecutive_failures']} 次")

        # 进度条
        st.progress(min(t['budget_pct'] / 100, 1.0),
                    text=f"今日预算 {t['budget_pct']}%")
        st.progress(min(b['monthly_pct'] / 100, 1.0),
                    text=f"本月预算 {b['monthly_pct']}%")

        # 最近 7 天柱状图
        st.markdown("**最近 7 天用量**")
        recent = r["recent_7d"]
        if recent:
            import pandas as pd
            df = pd.DataFrame(recent).set_index("date")
            st.bar_chart(df[["tokens"]])
            st.dataframe(df, use_container_width=True)
        else:
            st.caption("(还没有任何 LLM 调用记录)")

    except Exception as e:
        st.error(f"加载预算报告失败: {e}")

    st.divider()
    st.caption("🔧 调试: 临时禁用预算检查 (不推荐)")
    st.code("export CRYPTO_INTEL_DISABLE_BUDGET=1", language="bash")


# ────────────────────────────────────────────────────────────────────
#  Tab 10: 安装可选依赖
# ────────────────────────────────────────────────────────────────────
with tabs[9]:
    st.subheader("🛠️ 安装可选依赖")
    st.warning("⚠️ 装完后**重启** Streamlit (关掉再开) 才会生效。")
    st.markdown("没装也能跑 — 所有模块都有 fallback。但装上能解锁更深功能:")

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Phase 2 引擎 (建议装)**")
        if st.button("📦 装 vectorbt (秒级回测)", key="inst_vbt"):
            res = pip_install(["vectorbt"])
            (st.success if res["ok"] else st.error)(f"完成 ({res['elapsed']}s)")
        if st.button("📦 装 langgraph (DAG 编排)", key="inst_lg"):
            res = pip_install(["langgraph"])
            (st.success if res["ok"] else st.error)(f"完成 ({res['elapsed']}s)")
        if st.button("📦 装 ccxt (统一交易所)", key="inst_ccxt"):
            res = pip_install(["ccxt"])
            (st.success if res["ok"] else st.error)(f"完成 ({res['elapsed']}s)")
        if st.button("📦 装 duckdb (链上仓库查询)", key="inst_duck"):
            res = pip_install(["duckdb"])
            (st.success if res["ok"] else st.error)(f"完成 ({res['elapsed']}s)")

    with c2:
        st.markdown("**NLP 模型 (可选, 较大)**")
        st.caption("CryptoBERT ~120MB, 首次会自动下载")
        if st.button("📦 装 transformers + torch", key="inst_tx"):
            res = pip_install(["transformers", "torch"])
            (st.success if res["ok"] else st.error)(f"完成 ({res['elapsed']}s)")

    st.divider()
    if st.button("🚀 一键装全部 Phase 2 + Phase 3 依赖",
                 type="primary", key="inst_all"):
        with st.spinner("正在装 vectorbt / langgraph / ccxt / duckdb / transformers / torch (~3-5 分钟)..."):
            res = pip_install([
                "vectorbt", "langgraph", "ccxt", "duckdb", "transformers", "torch"
            ])
        if res["ok"]:
            st.success(f"✅ 装完 ({res['elapsed']}s) — 关掉本网页, 重新双击启动脚本就生效")
            st.balloons()
        else:
            st.error("有些包没装上, 看上面日志找原因")

    st.divider()
    st.markdown("**cryo (Rust 链上工具, 需手动装)**")
    st.code("brew install paradigmxyz/cryo/cryo  # 在终端跑", language="bash")
    st.caption("装完后在项目 .env 加: ETHEREUM_RPC_URL=https://eth.llamarpc.com")

# ────────────────────────────────────────────────────────────────────
#  Tab 11: 结果文件
# ────────────────────────────────────────────────────────────────────
with tabs[10]:
    st.subheader("📂 结果文件")
    DATA_DIR = ROOT / "data"

    sections = [
        ("📄 日报 HTML", DATA_DIR / "reports", "*.html"),
        ("📊 筛选快照", DATA_DIR / "meta", "snapshot_*.json"),
        ("🔬 RD-Agent 轨迹", DATA_DIR / "rd_agent", "*.json"),
        ("🧬 候选因子提案", DATA_DIR / "proposals", "*"),
        ("📈 回测产物", DATA_DIR / "backtest", "*"),
        ("🌐 onchain 仓库", DATA_DIR / "onchain", "**/*.parquet"),
    ]
    for label, folder, pattern in sections:
        with st.expander(f"{label}  —  {folder.relative_to(ROOT)}"):
            if not folder.exists():
                st.caption("(目录还没生成, 跑相应任务后会出现)")
                continue
            files = sorted(folder.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)[:30]
            if not files:
                st.caption("(空)")
                continue
            for f in files:
                rel = f.relative_to(ROOT)
                size_kb = round(f.stat().st_size / 1024, 1)
                mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                col1, col2, col3 = st.columns([6, 2, 2])
                col1.code(str(rel))
                col2.caption(f"{size_kb} KB")
                col3.caption(mtime)
                # 提供下载
                try:
                    if f.suffix in (".html", ".json", ".md", ".txt", ".csv"):
                        with open(f, "rb") as fh:
                            st.download_button(
                                f"⬇ 下载 {f.name}",
                                data=fh.read(),
                                file_name=f.name,
                                key=f"dl_{rel}",
                            )
                except Exception:
                    pass

# ── Footer ─────────────────────────────────────────────────────────
st.divider()
st.caption(
    "Crypto Intel v0.7 · OSS 集成完整版 · "
    "Phase 1+2+3 + 过拟合控制 · "
    f"工作目录: {ROOT}"
)
