# Crypto Intel · v0.3 (Streamlit Dashboard)

面向投资经理的加密货币情报仪表盘。6 个免费数据源 → 5 个可解释因子 → 多因子合成信号
→ PM 早会简报 → 互动 Web 仪表盘。一键部署到 Streamlit Cloud(完全免费,密码保护)。

**🚀 想直接上线? 看 [DEPLOY.md](DEPLOY.md) — 30 分钟手把手指南**

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
