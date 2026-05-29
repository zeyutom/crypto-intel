# Crypto Intel · v0.3 (Streamlit Dashboard)

面向投资经理的加密货币情报仪表盘。6 个免费数据源 → 5 个可解释因子 → 多因子合成信号
→ PM 早会简报 → 互动 Web 仪表盘。一键部署到 Streamlit Cloud(完全免费,密码保护)。

**🚀 想直接上线? 看 [DEPLOY.md](DEPLOY.md) — 30 分钟手把手指南**

## v0.8 DefiLlama 完整免费 API (2026-05-15)

完整对接 [DefiLlama 免费 API](https://api-docs.defillama.com/) 全部 **31 个端点** + 4 个衍生因子。无需 API key。

**新增模块**
- [`src/adapters/defillama_full.py`](src/adapters/defillama_full.py) — 31 端点 + 6 个高级聚合函数
- [`src/factors/defillama_factors.py`](src/factors/defillama_factors.py) — 4 个链上原生因子

**31 个免费端点分类**

| 大类 | 端点数 | 代表函数 |
|------|--------|---------|
| TVL | 6 | `list_protocols` / `protocol_history` / `chain_tvl_history` |
| Coins / Prices | 7 | `current_prices` / `price_chart` / `price_percentage` |
| Stablecoins | 6 | `list_stablecoins` / `stable_detail` / `stable_prices` |
| Yields | 2 | `list_yield_pools` / `pool_apy_history` |
| DEX / Options | 6 | `dex_overview` / `options_overview` / `dex_summary` |
| Perp OI | 1 | `open_interest_overview` |
| Fees / Revenue | 3 | `fees_overview` / `fees_summary` |

**4 个衍生因子**

| 因子 | 用途 | 数据源 |
|------|------|--------|
| **TVL Momentum 7d** | 哪个协议在吸/抽资金 | `/protocol/{slug}` |
| **DEX Volume Growth** | 突然放量预示叙事/事件 | `/overview/dexs` |
| **Stable Peg Deviation** | USDT/USDC 加权脱锚 = 流动性紧张 | `/stablecoins` |
| **Yield Spike Regime** | 稳定币池中位 APY = 资金成本 | `/pools` |

**CLI 用法**

```bash
python -m src.cli defillama health          # 4 个 base URL 健康度
python -m src.cli defillama chains          # 各链 TVL Top 15
python -m src.cli defillama protocols       # Top 20 协议
python -m src.cli defillama stables         # 稳定币脱锚检测
python -m src.cli defillama dex             # 各链 DEX 成交量份额
python -m src.cli defillama perp            # Perp DEX 持仓量
python -m src.cli defillama yields --stable # 高 APY 稳定币池子
python -m src.cli defillama factors         # 一键算 4 个衍生因子
python -m src.cli defillama clear-cache     # 清空缓存
```

**内置 2 级缓存**: 内存 (Module-level dict) + 磁盘 (`data/cache/defillama/*.json`), 热数据 TTL 60s, 历史数据 1h, 极慢数据 24h。

控制中心 (Streamlit) 也加了 **📡 DefiLlama 数据** tab, 8 个子按钮全图形化。

---

## v0.7 OSS 集成 全工程完成 (2026-05-15)

按 [docs/opensource_landscape.html](docs/opensource_landscape.html) 调研报告, 落地了 8 个 OSS 集成 (Phase 1+2+3)。所有模块都是**软依赖** + **优雅降级**——未装可选依赖时主流程不中断, 自动 fallback。

### Phase 1 · 因子+情绪+链上 (3 件)
| 模块 | 来源 | 文件 | 用途 |
|------|------|------|------|
| **CryptoBERT 情绪** | [ElKulako/cryptobert](https://huggingface.co/ElKulako/cryptobert) | `src/research/sentiment_bert.py` | BERT 替换关键词词典, 文本情绪三分类 |
| **Alpha158 风格因子库** | 移植自 [microsoft/qlib](https://github.com/microsoft/qlib) | `src/research/alpha158_features.py` | 148 个手工技术因子, 元学习的原料库 |
| **cryo 链上 adapter** | [paradigmxyz/cryo](https://github.com/paradigmxyz/cryo) | `src/adapters/cryo_onchain.py` | EVM 链地址级粒度: CEX inflow/outflow |

### Phase 2 · 引擎升级 (3 件)
| 模块 | 来源 | 文件 | 用途 |
|------|------|------|------|
| **vectorbt 回测内核** | [vectorbt](https://vectorbt.dev/) | `src/research/portfolio_backtest_vbt.py` | 秒级参数扫描, 兼容 portfolio_backtest API |
| **LangGraph DAG** | [LangGraph](https://github.com/langchain-ai/langgraph) | `src/evolution/graph.py` | 编排 5 个 evolution agent, 可观测/可重试 |
| **ccxt 统一交易所** | [ccxt/ccxt](https://github.com/ccxt/ccxt) | `src/adapters/ccxt_exchange.py` | 5+ 交易所 fallback, 替换 binance/okx/coinbase |

### Phase 3 · 长程能力 (2 件)
| 模块 | 来源 | 文件 | 用途 |
|------|------|------|------|
| **RD-Agent skeleton** | 借鉴 [microsoft/RD-Agent-Quant](https://github.com/microsoft/RD-Agent) | `src/research/rd_agent.py` | 假设→实验→评估→反馈 自演化循环 |
| **cryo 数据仓库 + DuckDB** | [paradigmxyz/cryo](https://github.com/paradigmxyz/cryo) + [DuckDB](https://duckdb.org/) | `src/adapters/cryo_warehouse.py` | 按月分区采集 + cross-table 查询 |

### 一键诊断与冒烟测试
```bash
# 健康检查 (8 个模块状态一览)
python -m src.cli oss-check

# 完整冒烟测试 (8 个 case)
python scripts/smoke_oss_integrations.py
```

### 新 CLI 子命令
| 命令 | 用途 |
|------|------|
| `oss-check` | 健康检查 (Phase 1+2+3 全部模块) |
| `backtest-vbt [--sweep]` | vectorbt 回测 / 参数扫描 |
| `evolve-graph` | 跑一轮 LangGraph evolution DAG |
| `ccxt-health` | ccxt 各交易所连通性 |
| `rd-agent [--rounds N] [--llm] [--resume]` | 跑 R&D 自演化 |
| `warehouse <stats\|ingest TOKEN\|flow TOKEN\|whales TOKEN>` | cryo 仓库管理 |

### 可选安装 (按需开启更深功能)
```bash
# CryptoBERT (~120MB)
pip install transformers torch --break-system-packages

# Phase 2 三件套
pip install vectorbt langgraph ccxt --break-system-packages

# Phase 3 (cryo 仓库需要 DuckDB)
pip install duckdb --break-system-packages

# cryo 二进制 (链上数据)
brew install paradigmxyz/cryo/cryo
echo 'ETHEREUM_RPC_URL=https://eth.llamarpc.com' >> .env
```

## v0.3 新增

- ⭐ Streamlit 多页面交互仪表盘 (6 页: 简报/因子/信号/历史/数据/词典)
- ⭐ 密码门 (Streamlit Secrets)
- ⭐ Plotly 交互式图表 (ETF 柱状图、F&G 走势、稳定币流通、因子历史)
- ⭐ GitHub Actions 每日 UTC 00:30 自动刷新
- ⭐ DEPLOY.md - 面向非程序员的部署指南

## v0.2 已有

- PM 早会简报 (看多/看空/风险/行动建议四栏)
- 因子卡片化 (中文别名 + 通俗解释 + PM 怎么读 + PM 怎么做)
- 复核状态人话化
- 数据源覆盖度面板
- 折叠式术语词典

## v0.2 改进

- ⭐ **PM 早会简报** — 报告顶部自动生成"看多/看空论据 + 风险 + 行动建议"
- ⭐ **因子卡片化** — 每个因子带中文名、通俗解释、PM 怎么读、PM 怎么做
- ⭐ **复核人话化** — "obs=3, IR proxy=N/A" → "已积累 3 个观测, 7 天后才能开始评估有效性"
- 数据源覆盖度面板(哪些源采到 / 哪些挂了一目了然)
- 折叠式术语词典(11 个核心名词中文解释)
- 修复:DEMO 误判(用 sentinel marker)、信号表 nan asset_id、Farside 抓取健壮性

## 特性

- **6 个数据源 (全部免费)**: CoinGecko、Binance、Coinbase、DefiLlama、Alternative.me F&G、Farside ETF
- **5 个因子**: Funding Composite、Coinbase Premium、Stablecoin Mint 7d、F&G Reversal、ETF Flow 5d
- **信号合成**: 多因子加权 + Regime 识别(BULL/BEAR/CHOP/CRISIS 等)
- **复核闭环**: 价格多源交叉验证 + 因子 IC/IR 监控框架
- **自动化**: APScheduler 常驻 或 cron 调度;日报 HTML 渲染
- **存储**: SQLite(零配置);后续可迁 Postgres

## 项目结构

```
crypto-intel/
├── config.yaml              # 所有可调参数集中在此
├── .env.example             # 可选 API key
├── src/
│   ├── cli.py               # CLI 入口
│   ├── config.py            # 加载配置
│   ├── db.py                # SQLite schema + 读写 helpers
│   ├── utils.py             # HTTP/日志/时间
│   ├── pipeline.py          # 编排 ingest/factor/review/report
│   ├── adapters/            # 6 个数据源适配器
│   ├── factors/             # 5 个因子
│   ├── signals/composite.py # 多因子合成
│   ├── review/              # 价格交叉 + IC 监控
│   ├── report/              # 日报生成 (Jinja2 模板)
│   └── scheduler/runner.py  # APScheduler 常驻
├── scripts/
│   ├── init_db.py
│   └── run_once.py          # 一键跑全流程
└── tests/test_smoke.py
```

## 本地快速开始

### 方案 A: 跑一次得到静态 HTML 日报 (最简单)

```bash
pip install -r requirements.txt
python -m src.cli init
python -m src.cli all
open data/reports/daily_*.html        # macOS
```

### 方案 B: 启动 Streamlit 交互仪表盘

```bash
pip install -r requirements.txt

# 创建本地密码 (上线时改成 Streamlit Cloud Secrets)
echo 'dashboard_password = "你的密码"' > .streamlit/secrets.toml

streamlit run streamlit_app.py
# 浏览器自动打开 http://localhost:8501
```

### 方案 C: 上线给团队/朋友访问

→ 看 [DEPLOY.md](DEPLOY.md), 30 分钟搞定 Streamlit Cloud + GitHub Actions 每日刷新

## 每个命令做什么

| 命令 | 动作 |
|------|------|
| `init` | 建 SQLite schema (raw_metrics/factors/signals/reviews/events) |
| `ingest` | 跑所有 adapter,抓最新行情 + 稳定币 + F&G + ETF |
| `factors` | 计算所有因子,合成信号 |
| `review` | 价格交叉验证 + IC 监控框架 |
| `report` | 从 DB 组装数据 → 渲染 HTML 日报 |
| `all` | 以上四步依次执行 |
| `serve` | APScheduler 常驻,按 config.yaml 里的 cron 自动跑 |

## 因子说明

| 因子 | 公式 | 信号方向 |
|------|------|---------|
| Funding Composite | 多币种加权 funding rate | 极端反转 |
| Coinbase Premium | (CB价 - Binance价) / Binance价 | 正→美区主导买入 |
| Stablecoin Mint 7d | USD 流通 7d 差 | 正→新增购买力 |
| F&G Reversal | FGI 0-100 | <25 抄底 / >75 减仓 |
| ETF Flow 5d | 近 5 日净流入累计 (\$M) | 正→机构流入 |

## 扩展路线

- Phase 2: 接入 Glassnode/Nansen/CryptoQuant (付费),增加链上因子
- Phase 3: 接 LunarCrush/Kaito,引入社交与叙事因子
- Phase 4: 完整 IC/IR 回测 + KOL 质量评分库

## 注意

- 所有输出仅供投资经理参考,**非投资建议**
- Farside ETF 数据来自网页抓取,结构变化可能需要重写解析器
- 免费 API 存在速率限制,config.yaml 里已预设保守值
