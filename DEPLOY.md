# Crypto Intel · 上线指南 (面向非程序员)

把你的仪表盘部署到互联网上,30 分钟内别人就能用密码访问。**完全免费**。

## 🎯 你将得到什么

- 一个永久 URL,例如 `https://crypto-intel-tom.streamlit.app`
- 用密码保护(只有知道密码的人才能进)
- 每天 UTC 00:30(北京时间 08:30)自动刷新数据
- 任何人有链接 + 密码就能在手机/电脑/平板上看

## 📋 准备工作 (5 分钟)

需要 3 个免费账号:

1. **GitHub 账号** — https://github.com/signup
2. **Streamlit Community Cloud 账号** — https://share.streamlit.io (用 GitHub 一键登录)
3. (可选) **Anthropic 账号** — 不需要,这个项目本身不调 LLM API

---

## 🚀 上线步骤

### 第 1 步:把代码推到 GitHub (10 分钟)

1. 登录 GitHub,点右上角 `+` → `New repository`
2. 仓库名填 `crypto-intel`(或你喜欢的名字)
3. **重要**:选 `Private`(私有,只有你能看到代码) — 反正部署后访问者用密码就行,代码不需要公开
4. 不勾任何 README/.gitignore/license,直接 `Create repository`
5. 在你 Mac 上,打开 `crypto-intel` 文件夹 (双击 `启动.command` 同级目录)
6. 打开 Mac 自带的「终端」(Spotlight 搜 Terminal),粘贴以下命令(把 `YOUR_USERNAME` 改成你的 GitHub 用户名):

```bash
cd ~/path/to/crypto-intel    # 改成你解压 crypto-intel 的实际路径
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/crypto-intel.git
git push -u origin main
```

如果是第一次用 git,它会问你 GitHub 账号密码 — 但 GitHub 现在要 **Personal Access Token** 而不是密码。
- 去 https://github.com/settings/tokens
- `Generate new token (classic)` → 勾选 `repo` → Generate
- 复制那串 `ghp_xxx...`,粘贴当密码用

✅ 推完后刷新你的 GitHub 仓库页面,应该能看到所有代码。

---

### 第 2 步:部署到 Streamlit Cloud (5 分钟)

1. 打开 https://share.streamlit.io
2. 用 GitHub 登录
3. 点右上角 `New app`
4. 填写:
   - Repository: `YOUR_USERNAME/crypto-intel`
   - Branch: `main`
   - Main file path: `streamlit_app.py`
   - App URL (可改): 自定义为你想要的子域名,比如 `crypto-intel-tom`
5. **关键步骤**:点 `Advanced settings...`
   - Python version: 选 `3.11`
   - **Secrets** 框里粘贴:

   ```toml
   dashboard_password = "你的密码改这里-越长越好"
   ```

6. 点 `Deploy!`

⏳ 等约 3–5 分钟(它在装依赖)。完成后会跳到你的仪表盘 URL,看到密码登录页 — 输入你刚设的密码就进去了!

---

### 第 3 步:开启每日自动刷新 (5 分钟)

数据需要每天更新一次,用 GitHub Actions 自动跑(完全免费,2000 分钟/月够用)。

1. 打开你的 GitHub 仓库 → `Settings` → `Actions` → `General`
2. 滚到底部 `Workflow permissions`
3. 选 `Read and write permissions`(允许 Action 提交数据回仓库)
4. 勾上 `Allow GitHub Actions to create and approve pull requests`
5. `Save`

完成。Action 会在每天 UTC 00:30 自动跑(代码已配置好)。

✅ **想立即测试一次?**
- 仓库页 → `Actions` 标签 → 左侧 `Daily Data Refresh` → 右侧 `Run workflow`
- 等 2 分钟看是否绿色 ✓
- 如果绿了,你的 Streamlit App 重新加载就能看到最新数据

---

## 🔐 怎么分享给别人

把这两样发给对方就行:

- **链接**: `https://YOUR-APP-NAME.streamlit.app`
- **密码**: 你在 Secrets 里设的那个

对方不需要 GitHub 账号、不需要装 Python、不需要任何技术 — 浏览器打开就用。

## 🔄 怎么改密码

1. https://share.streamlit.io → 找到你的 App → 点右侧 `Settings`
2. `Secrets` 标签 → 改 `dashboard_password = "新密码"`
3. `Save` (1 分钟内自动重启)

## 🎨 改完代码怎么发布?

```bash
cd ~/path/to/crypto-intel
git add .
git commit -m "改了点东西"
git push
```

Streamlit Cloud 检测到 push 后自动重新部署(约 1–2 分钟)。

## 🐛 常见问题

**Q: GitHub Actions 跑失败了?**
- 看 `Actions` → 失败的那次 → 看红色错误日志
- 大概率是某个 API 被墙(例如沙箱/CI 环境的网络问题)— 把对应 source 在 `config.yaml` 里 `enabled: false` 即可
- 推回去后重试

**Q: Streamlit App 提示 "App is in error state"?**
- 仪表盘 URL → `Manage app` → 看 logs
- 常见:依赖装不上 → 检查 `requirements.txt` 是否所有包都能装(用 pip 测一下)

**Q: 想升级到能给真实交易用?**
- 接入付费数据源 (Glassnode/Nansen/CryptoQuant) — 在 Streamlit Secrets 里加 API key
- 升级到付费 VPS(DigitalOcean $5/月)解决 Streamlit Cloud 资源限制
- 加 LLM 让简报更智能 — 接 Claude/OpenAI API

---

⭐ 部署成功后,把 URL + 密码私信告诉团队就完事。
