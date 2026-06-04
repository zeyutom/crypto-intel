#!/usr/bin/env bash
# Nightly 自动化编排 — 让系统在晚上自己跑完整套流水线
#
# 顺序 (失败不阻塞下一步):
#   1. backfill 增量 (拉最新一天历史快照)
#   2. ingest (拉今天的实时数据到 SQLite)
#   3. factors (算因子)
#   4. snapshot (写今天的 snapshot.json 供 PBO/回测用)
#   5. backtest-router (跑回测, 30 配置参数扫描带 PBO)
#   6. rd-agent --rounds 1 (跑 1 轮自演化, 找新因子)
#   7. discover-alpha (LLM/offline 变异候选因子)
#   8. weekly-review (仅周日跑)
#   9. watchdog check (实时告警)
#  10. 把当日简报推飞书
#
# 全部日志写到 data/nightly_logs/<日期>.log
# 失败的步骤记到 data/nightly_logs/<日期>.failures.json (供控制中心读)

set -u   # 注意: 不用 -e, 让某一步失败也继续
cd "$(dirname "$0")/.."

# ─── env 加载 ────────────────────────────────────────
if [ -f .env ]; then
  set -a; source .env; set +a
fi

# 自动激活 venv (如果存在)
if [ -d ".venv" ]; then
  source .venv/bin/activate
fi

# ─── 日志 ────────────────────────────────────────────
DATE=$(date +%Y-%m-%d)
LOGDIR="data/nightly_logs"
mkdir -p "$LOGDIR"
LOG="$LOGDIR/$DATE.log"
FAILS="$LOGDIR/$DATE.failures.json"
echo '{"failures":[]}' > "$FAILS"

log() {
  echo "[$(date '+%H:%M:%S')] $1" | tee -a "$LOG"
}

run_step() {
  local name="$1"; shift
  log "▶ $name: $*"
  local t0=$(date +%s)
  if "$@" >> "$LOG" 2>&1; then
    local dt=$(($(date +%s) - t0))
    log "  ✓ $name 完成 (${dt}s)"
    return 0
  else
    local rc=$?
    log "  ✗ $name 失败 rc=$rc"
    # JSON append (用 python 避免 jq 依赖)
    python3 -c "
import json, pathlib
p = pathlib.Path('$FAILS')
d = json.loads(p.read_text())
d['failures'].append({'step': '$name', 'rc': $rc, 'ts': '$(date -Iseconds)'})
p.write_text(json.dumps(d, indent=2))
" 2>/dev/null || true
    return $rc
  fi
}

# ─── 主流水线 ────────────────────────────────────────
log "═══ Nightly Run · $DATE ═══"

# 0. 健康检查 (失败不阻塞)
run_step "api-health" python3 -m src.cli api-health --no-cg || true

# 1. backfill 增量 (拿最新 7 天补齐, 用缓存避免每次拉 60 天)
run_step "backfill-incremental" python3 scripts/backfill_snapshots.py --days 7 --top 30 || true

# 2-4. 主 pipeline
run_step "init-db"  python3 -m src.cli init || true
run_step "ingest"   python3 -m src.cli ingest || true
run_step "factors"  python3 -m src.cli factors || true
run_step "snapshot" python3 -m src.cli snapshot || true

# 5. 回测 + 参数扫描 (含 PBO)
run_step "backtest-router" python3 -m src.cli backtest-router || true
run_step "backtest-sweep"  python3 -m src.cli backtest-router --sweep || true

# 6. RD-Agent 1 轮 (内置 Bonferroni 校正, 不会乱 promote)
#    Token 预算守门, 失败不影响主流程
run_step "rd-agent" python3 -m src.cli rd-agent --rounds 1 || true

# 7. Alpha discovery (offline 模式, 不烧 token)
run_step "discover-alpha" python3 -m src.cli discover-alpha || true

# 8. weekly-review (仅周日)
DOW=$(date +%u)   # 1=Mon, 7=Sun
if [ "$DOW" = "7" ]; then
  log "今天是周日 → 跑 weekly-review"
  run_step "weekly-review" python3 -m src.cli weekly-review || true
fi

# 9. Watchdog 检查 (会真推飞书, 如果配了)
run_step "watchdog" python3 -m src.cli watchdog check || true

# 10. 推飞书简报 (合成今天的 daily summary)
if [ -n "${FEISHU_GROUP_1_URL:-}${FEISHU_WEBHOOK_URL:-}" ]; then
  run_step "push-feishu" python3 -m src.cli push-feishu || true
fi

# 11. LLM 预算报告 (给监控用)
log ""
log "═══ 预算报告 ═══"
python3 -m src.cli llm-budget >> "$LOG" 2>&1 || true

# ─── 总结 ────────────────────────────────────────────
N_FAILS=$(python3 -c "import json; print(len(json.load(open('$FAILS'))['failures']))")
log ""
log "═══ Nightly 完成 · 失败 $N_FAILS 步 ═══"

# 12. 失败告警: 有步骤失败就推飞书, 让"静默 miss"变可见 (未配飞书时优雅跳过)
if [ "${N_FAILS:-0}" -gt 0 ]; then
  log "▶ 有 $N_FAILS 步失败 → 推飞书告警"
  python3 -c "
import json, sys
sys.path.insert(0, '.')
from src.notifier import push_alert
fails = json.load(open('$FAILS')).get('failures', [])
lines = ['**夜间任务有 %d 个步骤失败:**' % len(fails)]
for f in fails:
    lines.append('- ❌ %s (rc=%s)' % (f.get('step'), f.get('rc')))
lines.append('')
lines.append('日志: data/nightly_logs/$DATE.log')
r = push_alert('🚨 Crypto Intel 夜间任务失败 · $DATE', lines)
print('alert:', r.get('ok'), r.get('error') or ('pushed %s' % r.get('pushed')))
" >> "$LOG" 2>&1 || log "  (告警推送失败, 见日志)"
fi

# 滚动清理 30 天前的日志
find "$LOGDIR" -name "*.log" -mtime +30 -delete 2>/dev/null || true
find "$LOGDIR" -name "*.failures.json" -mtime +30 -delete 2>/dev/null || true

exit 0
