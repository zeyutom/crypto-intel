# 数据采集云端化方案 (减少 miss)

> 目标: 让"采数据"这件事不依赖某台 Mac 开机、不被地域封锁、失败能被发现并补采。
> 现状调研 + 分层改造建议。2026-06。

## 一、现状(调研结论)

| 组件 | 跑在哪 | 状态 |
|------|--------|------|
| `refresh.yml` (UTC 00:30) | GitHub Actions `ubuntu-latest`(美国机房) | ✅ 在跑。`init` + `all-no-llm`,把 `data/intel.db` + `data/reports/` commit 回 `main` |
| `nightly.yml` (UTC 19:00) | GitHub Actions | ❌ **静默失败**。`working-directory: crypto-intel` 指向不存在的子目录(仓库根=项目本身),加上 `continue-on-error: true`,每晚都挂但没人发现 |
| `screen` / `daily-screen` / RD-agent / 飞书推送 | **只在本地** `.command` / launchd | ⚠️ Mac 不开机就不跑 → 真正的 miss 来源 |
| 代码版本 | `main` 停在 2026-04-15(只有每日数据 commit),v0.6–v0.9 代码只在本地 `rescue/...` 分支 | ⚠️ 云端跑的是 4 月的旧代码 |

### "miss" 的根因(失败模式)
1. **机器没开机** — 本地 cron/launchd 只在 Mac 醒着时跑。← 云端能解决
2. **地域封锁** — `binance` 451、`farside` 403、`coinglass` 收费墙,从**美国 IP** 触发(本地在美 *或* GitHub 的美国 runner 都一样)。**搬到 GitHub Actions 并不能解决**,只是换了个美国 IP。
3. **一天只打一枪** — 每天 1 次,失败=整天丢;当天没有补采/重试。
4. **失败无告警** — 到处 `continue-on-error` + 没有对 workflow 本身的失败做通知 → 坏掉的 nightly 没人知道。
5. **数据持久化脆弱** — 把二进制 SQLite + 快照 commit 回 git `main`,靠 `pull --rebase --strategy-option=theirs` 抢救竞态(refresh.yml 里那段重试循环就是症状),还撑大仓库。
6. **没有单一真相源** — 本地快照 / 云端快照 / main 三者发散。

## 二、分层改造建议

### Tier 0 · 先修已有的(零成本,当天可做)
- **修 `nightly.yml`**:删掉 `working-directory: crypto-intel` 和 `cache-dependency-path` 里的 `crypto-intel/` 前缀(仓库根就是项目)。修完云端 nightly(screen/RD-agent/watchdog/飞书)才真正开始跑。
- **加失败告警**:nightly 末尾加一步——只要 `data/nightly_logs/<date>.failures.json` 非空就推一条飞书。让"静默 miss"变成"看得见"。
- **CI 守门**:`ci.yml` 里加一个 "workflow 自检"(lint yml / 跑 `python -m src.cli --help`),防止再出现路径类低级错误。

### Tier 1 · 解决地域封锁(真正的 miss 大头)
GitHub runner 是美国 IP,`binance`/`farside` 仍会被挡。三选一:
- **(A) 出口代理** — 项目已经装了 `httpx[socks]`。给被封的 adapter 配一个非美区 SOCKS/HTTP 代理(env 注入,GitHub Secrets 存代理地址)。改动最小。
- **(B) 非美区 self-hosted runner / VPS** — 在新加坡/东京开一台 ~$5/月 小机,注册成 GitHub self-hosted runner *或* 直接 cron 跑。一并解决"机器没开机"+"地域封锁"+"单一真相源"。**推荐**。
- **(C) 多源冗余** — 接受现有兜底(OKX 顶 binance),再给每个关键指标多加 1–2 个免费替代源,降低单源失败的影响。和 A/B 不冲突,建议都做。

### Tier 2 · 持久化(别再把二进制 DB 塞进 git)
- **快照(JSON)** → 对象存储:Cloudflare R2 / S3(免费额度足够),无 git 竞态、无仓库膨胀。
- **DB** → 二选一:
  - **Turso**(云端 SQLite,免费档):代码几乎不动(还是 SQLite 方言)。
  - **Supabase / Neon**(Postgres 免费档):README 本来就写了"后续可迁 Postgres"。
- Streamlit Cloud 改成从 DB/对象存储读,而不是从 git 读 `data/`。

### Tier 3 · 目标架构(单一真相源)
```
[非美区采集器 (VPS cron 或 GH Actions + 代理)]
        │  每日(可多次)
        ▼
[云端 DB (Turso/Supabase) + 对象存储 (R2) ]  ← 单一真相源
        │                         │
        ▼                         ▼
 [Streamlit Cloud 看板]     [飞书推送]
```
本地 Mac 变成**可选**(想手动跑/调试随时跑,但系统不依赖它)。

## 三、建议的最小落地顺序
1. **先把代码理顺**:把 v0.6–v0.9 + 本次 33 修复合到 `main`(否则云端一直跑 4 月旧码)。见下方 PR 说明。
2. **修 `nightly.yml` 路径 + 加飞书失败告警**(Tier 0)——半小时,立刻止血。
3. **给 binance/farside 配非美区代理,或开一台 $5 VPS 当采集器**(Tier 1)。
4. **快照迁 R2 / DB 迁 Turso**,停止 commit 二进制(Tier 2)。

## 四、成本
- 仓库是**公开**的 → GitHub Actions **分钟数无限免费**。
- VPS ~$5/月(可选,但最干净)。
- Turso / Supabase / Neon / R2 都有够用的免费档。
- 代理:自建在 VPS 上即可(和 Tier 1-B 合并),或用便宜的住宅/机房代理。

## 五、启用云端出口代理(操作步骤,配合 refresh.yml / nightly.yml)

两个 workflow 已支持**可选**出口代理:配了 `EGRESS_PROXY_URL` secret 就走代理,没配就直连(行为不变)。代码侧全部 httpx + `trust_env=True`,设环境变量即生效,无需改 adapter。

> ⚠️ 前提:代理必须是 GitHub 云端能访问的**公网地址**——你本地的 `127.0.0.1:15236` 在云端 runner 上无效。

### 方案 1 · 非美区小 VPS 自建代理(推荐,~$5/月)
1. 在新加坡/东京/香港开一台便宜 VPS。
2. 装个轻量代理(务必加鉴权):
   ```bash
   sudo apt install tinyproxy          # HTTP 代理, 最简单
   # /etc/tinyproxy/tinyproxy.conf: Port 8888 + BasicAuth user pass (强烈建议)
   sudo systemctl restart tinyproxy
   ```
   或 `gost` / `3proxy` / `dante`(SOCKS5)。
3. GitHub 仓库 → Settings → Secrets and variables → Actions → New repository secret:
   - Name: `EGRESS_PROXY_URL`
   - Value: `http://user:pass@你的VPS_IP:8888`(或 `socks5://user:pass@IP:port`)
4. Actions 页手动 Run workflow 验证:日志出现「出口代理已启用」,且 binance/farside 不再失败。

### 方案 2 · 商业代理
买个支持 HTTP/SOCKS 的机房/住宅代理,把 `http://user:pass@host:port` 填进 `EGRESS_PROXY_URL`。最省事,有月费。

### 方案 3 · 非美区 self-hosted runner(彻底,但要保活一台机器)
非美区 VPS 注册成 GitHub self-hosted runner,workflow 里 `runs-on: ubuntu-latest` → `runs-on: self-hosted`。整个 job 从非美区出口,连代理都不用配。

> 🔒 代理一定要加鉴权(账号密码/IP 白名单),否则公网开放代理会被薅。`EGRESS_PROXY_URL` 当密码管,别进代码。

## 六、免费 AI 简报(不付费用 Anthropic API)

`src/llm_brief.py` 已改成**多 provider 路由**:配哪个 key 用哪个,**免费源优先**,全走 httpx REST(无新依赖)。优先级 `GEMINI_API_KEY > GROQ_API_KEY > ANTHROPIC_API_KEY`,或用 `LLM_PROVIDER` 显式指定。

> ✅ 不想付费:**只配下面其一,别配 `ANTHROPIC_API_KEY` 即可。**

### 选项 A · Google Gemini(推荐,免费档很大)
1. https://aistudio.google.com/apikey → Create API key(免费,Google 账号即可)。
2. GitHub 仓库 → Settings → Secrets → New secret:Name `GEMINI_API_KEY`,Value 粘 key。
3. 默认模型 `gemini-2.5-flash`(实测免费档有 quota;`2.0-flash` 无免费 quota 会 429)。
   代码已对 2.5 的 thinking 模型关掉思考预算(`thinkingBudget:0`),否则正文会被截断。想换设变量 `GEMINI_MODEL`。

### 选项 B · Groq(免费,极快,Llama 3.3 70B)
1. https://console.groq.com/keys → Create API Key(免费)。
2. GitHub Secret:Name `GROQ_API_KEY`。默认模型 `llama-3.3-70b-versatile`,可用 `GROQ_MODEL` 改。

### 和本地 Claude Max 的关系(为什么云端不能白嫖 Max)
- Max 订阅只能在**本地 Claude Code**(`llm-local`)里用,**云端 headless 登录不了** —— 这就是云端必须用免费 API 的根本原因。
- 想要 Claude 质量又不付费 → **混合模式**:云端用 Gemini/Groq 出每日简报;偶尔要 Opus 深度版,本地手动 `python -m src.cli llm-local`(走 Max,免费)。

> 注:免费档没有 Anthropic 内置 web 搜索,所以「24h 关键事件」章节代码里已加**防编造提示**(让模型谨慎概述/标注需人工补充,不瞎编新闻)。
