# Crypto Intel · 全项目 Code Review

**Review date**: 2026-05-15 · **Code base**: ~15,500 行 Python · **Reviewer focus**: 可量化/真实性/自动化 — 与 PM 用例对齐

## 🎯 Executive Summary

**整体评分: B+ (有惊艳模块, 但工程债务正在积累)**

亮点: PBO/DSR 过拟合控制 (业界稀缺) · DefiLlama 31 端点覆盖 · 软依赖+优雅降级一致贯穿 · 控制中心降低非程序员使用门槛。

主要问题: **新老模块并存 (4 对重复)**, **HTTP 工具 9 处重写**, **测试覆盖率 < 1%** (36 行测试 vs 15500 行业务), **40 个 CLI 命令缺少 namespace**, **元学习从未启动** (factor_config.json 不存在 → IC 历史为空), **快照只有 9 个** (PBO/回测都跑不起来真实数据)。

按严重度排, 致命问题 0 个, 严重 5 个, 中 8 个, 轻 6 个 → 大致 1 周高强度可清理一半。

---

## 🔴 严重问题 (Severity: 严重) — 必须修

### S1. 元学习闭环从未跑起来 — 项目核心价值未兑现
**证据**:
- `data/factor_config.json` 不存在 (`python check` 输出 "无 factor_config.json")
- `data/meta/snapshot_*.json` 只有 **9 个**, 不够 PBO/IC 加权所需的最少 30 天
- README v0.5 称 "10 因子 + 元学习 IC 加权 + Regime", 但实际加权未生效

**影响**: 项目最有价值的差异化能力没产出
**修复**: (a) 加 launchd 调度真跑 30 天, 或 (b) 写一个 backfill 脚本从历史价格构造 60 天合成快照
**工作量**: M (3-4 天)

### S2. 4 对新老模块并存, 用户不知道用哪个
**证据**:
| 旧 | 新 | 共存原因 |
|----|-----|---------|
| `defillama.py` / `defillama_extra.py` | `defillama_full.py` | v0.8 重写, 旧版还被 `pipeline.py` 调 |
| `sentiment_nlp.py` (关键词) | `sentiment_bert.py` (BERT) | sentiment_nlp 已经内置 BERT fallback, 双入口冗余 |
| `portfolio_backtest.py` | `portfolio_backtest_vbt.py` | API 不兼容, CLI 双命令 (`backtest-wf` vs `backtest-vbt`) |
| `onchain_real.py` | `cryo_onchain.py` + `cryo_warehouse.py` | cryo 系列做地址级, onchain_real 做聚合, 无清晰边界 |

**影响**: 维护成本翻倍, 调用方分不清, 测试覆盖率被稀释
**修复**: 给每对写一个"路由层" (`adapters/defillama.py` 改成 facade → 内部调 full), 老 API 标 `DeprecationWarning`, 半年后删
**工作量**: M (2-3 天)

### S3. HTTP 工具 9 处重写, 没有统一的速率限制 / 重试 / 缓存
**证据**: `grep "def _get\|def _http_get"` 在 9 个文件命中, `utils.http_get_json` 只在 31 处用 (基本只在老 adapter)
- `onchain_real.py` L24 自己实现 _get
- `screener.py` L55 自己实现 _get (含 retries=3, backoff=10)
- `sentiment_nlp.py` L64 自己实现 _get (无 retry)
- `defillama_full.py` 自己实现 _http_get (含双层缓存, 最好的)
- `factors_extended.py` 又一份

**影响**: CoinGecko 限速时不同模块不会互相协调 → 全局 429; 没有统一的指标 (谁拖慢全流程)
**修复**: 把 `defillama_full._http_get` 提到 `utils/http_client.py` (单例 + token bucket + LRU 缓存 + Prometheus-style 计数), 9 处替换
**工作量**: M (1-2 天)

### S4. 测试覆盖率 < 1%, 重构无安全网
**证据**:
- `tests/test_smoke.py` 只有 **36 行**, 验证 4 件事 (config 加载/db 建表/adapter 数=6/factor 数=5)
- `ALL_FACTORS` 实际是 15+ 个 (smoke 测试早过时, assert len==5 早不对了)
- `scripts/smoke_oss_integrations.py` 是新加的 12 case 集成测试, 但没接入 pytest, 也没 CI
- 没有单元测试: 因子计算正确性零验证, IC 算法零验证, PBO 算法零验证 (虽然 self-test 通过但不是 pytest)

**影响**: 用户不会代码, 但每次改动都有破坏可能; PBO/DSR 这种数学敏感模块尤其需要测试
**修复**: 
- 把 `tests/test_smoke.py` 中 `assert len(ALL_FACTORS) == 5` 改成 `>= 5`
- 把 `scripts/smoke_oss_integrations.py` 改造成 pytest 风格
- 给 PBO/Alpha158/Bonferroni/IC 加 unit test (~10 个 test, 已知输入输出)
- 加 GitHub Actions 跑 pytest
**工作量**: M (2 天)

### S5. CLI 40 个命令无 namespace, 没文档化, 用户记不住
**证据**:
- `python -m src.cli` 没参数时显示 9 个命令的帮助 (`cli.py` 顶部 docstring), 但实际有 40 个
- `python -m src.cli pbo` / `defillama` / `rd-agent` / `warehouse` 等新命令完全没出现在帮助里
- 没有 `--help` 子命令系统
- 子命令命名风格不一致: `backtest-wf` / `backtest-vbt` / `backtest` / `daily-screen` / `screen`

**影响**: 不写代码的用户找不到入口; 控制中心 tab 才是用户实际接触面
**修复**: 
- 改用 `argparse` 或 `click` 子命令组 (group: data/factor/research/agent/diag/dev)
- 自动生成帮助
- 控制中心 tab 标签可用, 但 CLI 也要修
**工作量**: L (1 天结构化, 但要触全部 40 个分支)

---

## 🟡 中等问题 — 应该修

### M1. `_v04_factors.py` (10 个因子) 是单文件 286 行混在一起, 应拆
**证据**: `src/factors/_v04_factors.py` 11KB, 包含 10 个因子函数 (open_interest / liquidation / long_short / btc_dom / total_mcap / 等). 命名带下划线表"内部", 但实际从 `__init__.py` 暴露
**修复**: 拆成 10 个文件, 命名规范化 (去掉 `_` 前缀)
**工作量**: S (1 小时)

### M2. `agents.py` 5 个研究 agent (~600 行) 各自调 LLM, 没有 token 预算保护
**证据**: 
- `research/agents.py` L58/L114 等 6 处 `run_claude(..., timeout=600)` 
- 没有 token 计数, 没有月度上限, 没有失败累计告警
- 测试模式 (mock_claude) 不存在
**影响**: 跑一次 `research` 命令可能烧几万 token, 用户没感知
**修复**: 在 `_claude_runner.py` 加 token 计数 + .env 配 `MAX_TOKENS_PER_DAY`; 单次失败 3 次熔断
**工作量**: S (半天)

### M3. PBO 接入 vbt 时, 实际数据维度不够
**证据**: `portfolio_backtest_vbt.run_parameter_sweep_vbt` 跑 5×3=15 配置, PBO 需要 T×N 收益矩阵, T=快照数=9, n_splits=min(16, 9//2)=4 → 太少, PBO 估计噪声大
**影响**: PBO verdict 不可信 (合成测试通过 ≠ 真实数据下可用)
**修复**: 
- 改成从 OHLCV (DefiLlama price_chart) 拉每日 close, 重算每个配置的真实日收益
- 至少 60 天历史
**工作量**: M (1 天)

### M4. Streamlit 控制中心: `run_cmd` 子进程没有取消按钮
**证据**: `pages/0_🚀_控制中心.py` `run_cmd` 函数 — 用户点了"跑全流程"后只能等, 没有 Stop 按钮; timeout=600 写死
**影响**: PM 跑回测时如果 hang 住, 只能关窗
**修复**: 用 `st.session_state` 存 PID, 加 ❌ 取消按钮
**工作量**: S (1 小时)

### M5. 错误处理静默 — 用户看不到失败
**证据**: 
- `adapters/defillama.py` 3 处 bare `except Exception: pass`
- `adapters/farside.py` 3 处 `except Exception: pass`
- `cryo_warehouse.py` 3 处 silent except
- `defillama_full._http_get` 失败只 log.warning, 不报告给 caller
**影响**: 数据缺失被吞掉, PM 看的日报可能基于不完整数据 (例如 ETF Flow 失败)
**修复**: 把 silent except 改成 log.warning + 在 raw_metrics 表记 `_meta source=X status=failed`; 控制中心 "🩺 健康检查" tab 显示
**工作量**: M (1 天)

### M6. snapshot.py 缺少 schema versioning
**证据**: `src/snapshot.py` 直接 dump dict 到 json, 没有 `schema_version` 字段
**影响**: factors 改名/新增后, 历史快照与新代码不兼容 (e.g. `verify-returns` 会 KeyError)
**修复**: 加 `schema_version: 1`, 加 migration 函数
**工作量**: S (半天)

### M7. RD-Agent 集成 alpha_discovery 失败 — `evaluate_expression` 不存在
**证据**: 跑 `rd-agent` 命令时输出 `WARNING alpha_discovery 无 evaluate/backtest 函数`, 实际 alpha_discovery.py 的函数叫 `evaluate_candidates`, 接受 list 而非单个 expression
**影响**: RD-Agent 跑了等于没跑, promoted = 0 永远
**修复**: 在 rd_agent.experiment_agent 加适配层, 把 hypothesis 包成 candidate dict 喂给 evaluate_candidates
**工作量**: S (1 小时)

### M8. 控制中心 sidebar 没有 "🚀 控制中心" 入口提示, 用户首次进可能看不到
**证据**: streamlit 自动按文件名排序, `0_🚀_控制中心.py` 的 `0_` 是想置顶, 但 streamlit 在 sidebar 排序行为不稳定
**影响**: 用户第一次进默认进 `streamlit_app.py` 的简报页, 不知道有控制中心
**修复**: 在 `streamlit_app.py` 顶部加一条横幅 "🚀 第一次用? 去左侧 [控制中心]"
**工作量**: S (10 分钟)

---

## ⚪ 轻微问题

### L1. config.yaml 105 行, 但很多 adapter 硬编码绕过它
`adapters/defillama_full.py BASES` 全硬编码, 不从 config 读; `cryo_onchain.WELL_KNOWN_TOKENS` / `CEX_HOT_WALLETS` 也是硬编码 → 上线后想加新地址要改代码

### L2. 大文件超 800 行 → 难维护
- `cli.py` 785 行 (40 个 elif 分支)
- `screener.py` 860 行 (10 因子+元学习+regime 全在一个文件)
拆成子文件能显著提升可读性

### L3. `_meta_n_tests` 是字符串 key 写到 candidates[0] 里 — hack
`alpha_discovery.py` 的多重检验 metadata 用第一个 candidate 字典携带, 应该单独写到 `pool["meta"]`

### L4. `.env.example` 已严重过时
只有 CryptoPanic / Telegram, 缺 ANTHROPIC_API_KEY / FEISHU_WEBHOOK / ETHEREUM_RPC_URL 等

### L5. 大量 module-level 全局变量 (内存缓存)
`defillama_full._mem_cache` / `sentiment_bert._pipeline` / `evolution.graph._exchange_cache` 都是 module-level → 不能多进程跑, restart 丢缓存

### L6. 没有版本号统一管理
README 提 v0.7/v0.8, 代码里没有 `__version__` 常量, factor schema 没版本

---

## 🚀 业务价值缺口 (按 PM 视角, 不是工程问题)

按用户档案 "重视可量化/真实性/复核", 还缺这些:

### 缺口 1: 持仓 attribution / 业绩归因
**现状**: 有 `portfolio_backtest.py` 算 Sharpe/MaxDD, 但**没有把每天涨跌归因到具体因子贡献**
**建议**: 加 Brinson-style attribution: 选币贡献 vs 权重贡献; 因子暴露分解
**价值**: PM 早会上能说"今天 +2% 主要靠 momentum 因子, funding 因子拖累"

### 缺口 2: 实时风险预警 (event-driven)
**现状**: 所有信号都是日级聚合, 没有实时告警通道
**建议**: 加 watchdog: 当 stablecoin peg deviation > 50bps / Funding rate > 0.1% / liquidations > $1B 时立刻飞书 push (复用现有 notifier)
**价值**: 不用等第二天早会才发现风险

### 缺口 3: 对标基准 (benchmark)
**现状**: 回测显示 +37% 年化, 但没有跟 BTC 之外的对标 (BTC + ETH 等权 / Crypto20 指数 / Bitwise BITX)
**建议**: 加 `research/benchmarks.py` 拉 4-5 个对标, 输出超额收益分解

### 缺口 4: 信号衰减 / 持仓时长建议
**现状**: 筛选给出"今天 BTC 排第 3"但不说"建议持有多久"
**建议**: 用 IC 半衰期 + 实际 forward return 算最优持仓窗口 (e.g. funding 因子最优 3 天, momentum 因子最优 14 天)

### 缺口 5: 仓位调整建议带 LP/Yield 维度
**现状**: 选币给的是现货排名, 没考虑"BTC 该买现货还是 Coinbase ETF? ETH 该买现货还是借出去吃 Aave"
**建议**: 整合 DefiLlama yield + ETF NAV, 给出"持有方式"建议

### 缺口 6: 历史复盘界面
**现状**: 跑过的快照存在 data/meta/, 但 streamlit dashboard 没有"看 N 天前那次推荐结果如何"
**建议**: 加一个 dashboard 页, 选日期 → 看那天推荐 vs 实际行情, 形成可信度证据

### 缺口 7: 同行/KOL 一致性检查
**现状**: 自家系统的判断没有跟外部对照
**建议**: 把 Twitter/Telegram top KOL 当天言论做情绪聚合 (用现成的 CryptoBERT), 跟系统判断算一致性分; 不一致时高亮

### 缺口 8: 持仓/订单跟踪 (paper trading)
**现状**: 系统是只读的情报系统, 不记录"PM 听建议买了/没买"
**建议**: 加轻量 paper trading: PM 点"接受推荐" → 系统按当时价记成虚拟仓位 → 累计真实业绩

---

## 📋 30 天清债顺序建议

按 ROI (价值 / 工作量) 排:

| 周 | 重点 | 收益 |
|----|------|------|
| W1 | M7 (RD-Agent 适配 1h) + S1 backfill 快照 (3d) + M4 取消按钮 (1h) | 让现有功能真跑起来 |
| W2 | S3 统一 HTTP (2d) + S2 路由层去重 (2d) | 减一半重复代码 |
| W3 | S4 测试 + CI (2d) + M2 token 预算 (0.5d) + M5 错误可见性 (1d) | 安全网 + 成本控制 |
| W4 | 业务缺口 #2 (实时预警) + #6 (历史复盘) | 给 PM 真实新价值 |

S5 (CLI namespace) 投入大, 收益是给"会写代码的人", 优先级最低 — 控制中心已经覆盖非程序员场景。

---

## ✅ 已经做得好的 (保持/不要破)

1. **过拟合控制** — PBO/DSR/Bonferroni 完整实现, 业界稀缺
2. **软依赖+优雅降级** — 所有 OSS 集成模块都有 `is_available()` + fallback
3. **DefiLlama 全覆盖** — 31 端点 + 双层缓存 + 4 衍生因子, 行业 free 数据天花板
4. **memory 系统** — `MEMORY.md` 沉淀工作流目标, 跨 session 一致
5. **控制中心** — 把 40 个 CLI 命令降到 7 个 tab 按钮, 非程序员可用
6. **冒烟测试结构** — 12 个 case 即便没装可选依赖也能跑 (虽然不是 pytest)

---

**总结**: 项目已经是个能跑的 v0.8 系统, 不是 prototype。最大风险不是缺功能, 而是 **代码债务在加速积累** + **核心闭环 (元学习) 还没真正跑通**。建议先把这两件事做完, 再考虑加新功能。
