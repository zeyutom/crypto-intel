# Crypto-Intel · 多 Agent 代码审查报告 (v0.9)

> 9 个子系统并行审查 + 运行时冒烟 + 每条发现逆向核验 · 40 条原始发现 → **33 条确认 bug** / 7 条驳回 · 运行时: **all-pass**

> 运行时冒烟全过: 102 文件编译、87 模块导入、`all-no-llm` exit 0、`pytest` 91 passed。所有 bug 都是"能跑但算错/静默失效"类，非崩溃。


---

## HIGH (5)

### H1. vbt from_orders uses ffill'd target weights, forcing daily re-trading and fee over-drag (rebalance_days ignored for costs)

- **位置**: `src/research/portfolio_backtest_vbt.py:186-196` · 子系统 `backtest-risk` · 置信度 high

- **问题**: run_walkforward_backtest_vbt passes size=schedule.ffill() with size_type='targetpercent' to vbt.Portfolio.from_orders. _apply_rebalance_schedule already builds a DataFrame that is NaN on non-rebalance days (so vbt would treat NaN as 'no order' and only rebalance on the intended ~weekly cadence). Forward-filling that schedule replaces the NaNs with a constant target percentage on EVERY bar. Because the held target% is constant while prices drift, vectorbt re-issues orders on every single bar to restore the target weights, charging the fee_rate on every holding every day. This (a) defeats the documented intent on lines 183-185 ('在 rebalance 日…按新目标权重买入'), (b) makes the rebalance_days parameter effectively meaningless for transaction costs (portfolio is rebalanced daily regardless), and (c) inflates fee drag, biasing total_return / sharpe / calmar downward. The fix is to pass the un-ffilled `schedule` (NaN on non-rebalance days).

- **建议修复**: In run_walkforward_backtest_vbt, change line 188 from `size=schedule.ffill(),` to `size=schedule,` so the un-ffilled schedule (NaN on non-rebalance days = "hold") is passed to from_orders. Verified this runs cleanly through the full stats path (valid Sharpe/Max Drawdown/Win Rate, 95-point equity curve, no NaN) and makes order count track the rebalance cadence (15 for rb=7). Optionally drop or update the now-inaccurate inline comment on line 188.

### H2. Evolution LangGraph DAG silently no-ops 4 of 5 stages (wrong function names) yet reports success

- **位置**: `src/evolution/graph.py:113-159` · 子系统 `evolution` · 置信度 high

- **问题**: Each graph node probes the wrong module entrypoints. node_source_discover looks for source_discoverer.discover_sources() or .main(); node_factor_propose for factor_proposer.propose_factors() or .main(); node_narrative_track for narrative_tracker.track_narratives() or .main(); node_prompt_evolve for prompt_evolver.evolve_prompts() or .main(). NONE of those names exist — the real entrypoints are run_source_discovery / run_factor_proposal / run_narrative_tracking / run_prompt_evolution. Because both hasattr branches are False, the inner _do() returns without doing anything, and _safe_call still appends the node to state['nodes_ok']. So `run_evolution()` (exposed via `cli.py evolve-graph`, cli.py:740-744) prints nodes_ok=['source_discover','factor_propose','narrative_track','prompt_evolve',...] and reports success while never invoking Claude, never writing any data/proposals/*.md, and never inserting any evolution_log rows for 4 of the 5 stages. Only node_weekly_review works, because it happens to probe run_weekly_review (graph.py:165), which does exist. The entire DAG orchestration is a success-reporting facade.

- **建议修复**: Point each node at the real entrypoint. In src/evolution/graph.py replace the probed names:

- node_source_discover (line 117/121): probe/call source_discoverer.run_source_discovery() instead of discover_sources()/main(). It returns a dict {ok,file,chars}, not a list, so store accordingly (e.g. state["sources_proposed"] = [out] if isinstance(out, dict) else out[:50]).
- node_factor_propose (line 129/133): factor_proposer.run_factor_proposal().
- node_narrative_track (line 141/145): narrative_tracker.run_narrative_tracking().
- node_prompt_evolve (line 153/157): prompt_evolver.run_prompt_evolution().

weekly_review already correct. Minimal patch — change the hasattr/getattr target strings to run_source_discovery/run_factor_proposal/run_narrative_tracking/run_prompt_evolution and adapt the dict-shaped return handling (these return dicts with ok/file/chars, not lists). Also consider: in _safe_call, treat a node whose _do found no callable entrypoint as failed rather than ok, so a future rename can't silently re-introduce a false-success facade.

### H3. SentimentAgent inverts funding-rate factor sign (rewards overheated longs, penalizes oversold)

- **位置**: `src/research/swarm_decision.py:194-200` · 子系统 `research-agents` · 置信度 high

- **问题**: f_funding_rate is the NORMALIZED screener factor (0-1), defined in factors_extended.calc_funding_rate_score so that HIGH score = negative raw funding = shorts crowded / oversold = BULLISH, and LOW score = high positive raw funding = longs overheated = BEARISH/risk. The swarm SentimentAgent reads this normalized factor but applies the opposite sign: when f_funding_rate > 0.7 (canonically bullish/oversold) it does score -= 0.1 and emits the label '资金费率偏高 (多方过度拥挤)' (longs over-crowded); when f_funding_rate < 0.3 (canonically bearish/overheated) it does score += 0.05 and emits '资金费率健康'. Both the score adjustment and the human-readable rationale are flipped. The screener's own UI (screener.py:670) confirms the canonical direction by coloring f_funding_rate>0.6 GREEN and <0.3 RED. Net effect: oversold/bottoming coins are pushed DOWN in the sentiment vote and overheated/top-heavy coins are pushed UP, directly corrupting the multi-agent ensemble score and the decision_trace rationale.

- **建议修复**: In src/research/swarm_decision.py, sentiment_agent (lines 194-200), flip the sign and the labels so the normalized factor is interpreted correctly (HIGH score = negative funding = shorts crowded/oversold = bullish; LOW score = positive funding = longs overheated = bearish):

```python
funding = coin.get("f_funding_rate", 0) or 0
if funding > 0.7:
    score += 0.05
    signals.append("资金费率健康 (空头拥挤/超卖, 反转倾向)")
elif funding < 0.3:
    score -= 0.1
    signals.append("⚠️ 资金费率偏高 (多方过度拥挤)")
```

(i.e. swap the two branches' score deltas and labels). Leave risk_agent lines 242-245 unchanged — its symmetric extreme-penalty is already correct.

### H4. Spearman IC formula ignores ties → constant/dead factor (e.g. funding_rate=0.5 for all coins) produces a spurious strong IC that poisons weight evolution

- **位置**: `src/research/meta_learner.py:137-153` · 子系统 `screener-meta` · 置信度 high

- **问题**: _spearman_rank_corr uses the naive no-ties formula `1 - 6·Σd²/(n(n²-1))`. It (a) never guards against a constant factor column (zero variance, where Spearman is mathematically undefined) and (b) is wrong whenever there are tied values, because it assigns distinct sequential ranks to equal values via enumerate-order. In this codebase factor columns are frequently constant or heavily tied: f_funding_rate defaults to 0.5 for EVERY coin whenever Binance Futures is blocked (the common US case — premiumIndex returns nothing, calc_funding_rate_score(0)=0.5), and f_market_cap_size only takes 5 discrete values. When run_ic_backtest computes IC for such a column the bogus formula returns a large nonzero value driven purely by the incidental ordering of coins in the snapshot, not by any predictive signal. That fake IC then flows into update_weights_from_ic (raising the dead factor's weight by 1+IC·0.5 each cycle until it pins to its max) and into generate_factor_report (labeling it 'strong' when IC>0.15). The meta-learner is therefore reallocating weight toward a no-information factor.

- **建议修复**: Fix `_rank` to assign average (fractional) ranks to ties, and guard zero-variance input by returning 0.0 (treat dead factor as no-information, NOT ±1). Apply identically in meta_learner.py:137-153 and alpha_discovery.py:279-292 (or factor it into one shared helper).

def _spearman_rank_corr(x, y):
    n = len(x)
    if n < 5:
        return 0.0
    def _rank(arr):
        order = sorted(range(n), key=lambda i: arr[i])
        ranks = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and arr[order[j + 1]] == arr[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0   # average of tied positions (1-based)
            for k in range(i, j + 1):
                ranks[order[k]] = avg
            i = j + 1
        return ranks
    rx, ry = _rank(x), _rank(y)
    # zero-variance guard: a constant column has no rank spread -> undefined corr
    if len(set(rx)) < 2 or len(set(ry)) < 2:
        return 0.0
    # Pearson correlation of average ranks (correct, tie-aware Spearman)
    mx = sum(rx) / n
    my = sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    denom = (vx * vy) ** 0.5
    return cov / denom if denom > 0 else 0.0

Returning 0.0 for a constant factor makes update_weights leave its weight unchanged (adjustment = 1.0 + 0*0.5 = 1.0) and keeps it out of the "strong" bucket, eliminating the spurious reallocation. The Pearson-of-average-ranks form matches scipy.stats.spearmanr on tied data (verified above), fixing the wrong-sign results for dev/onchain/narrative/tvl factors.

### H5. IC backtest uses the newest snapshot as 'current price' regardless of lookback window — with a clustered/small lookback the window snapshot equals the latest snapshot, giving all-zero returns and a fabricated IC

- **位置**: `src/research/meta_learner.py:186-225` · 子系统 `screener-meta` · 置信度 high

- **问题**: run_ic_backtest selects best_snap as the snapshot nearest to (now − lookback_days), then takes 'current prices' from snapshots[-1] (the newest file on disk) — it never excludes snapshots[-1] from the best_snap search and never re-fetches live prices. Two failures result. (1) Degenerate self-pairing: when only recent snapshots exist and lookback is small (CLI accepts an arbitrary int via sys.argv[2], e.g. `crypto ic-backtest 1`), best_snap can BE snapshots[-1]; then old_price==new_price for every coin, every return is 0.0, and the tie-broken Spearman of a factor against an all-zero return vector returns a spurious nonzero IC (~ -0.24 measured). (2) Holding-period mislabeling / staleness: the realized return window is (newest_snap − best_snap), not lookback_days. As long as a fresh snapshot exists at 'now' the two roughly coincide, but if no screen has run for several days, snapshots[-1] is stale and the IC is computed over the wrong horizon while still being recorded and weighted as the requested lookback. The inline comment ('实际使用时应从 API 获取, 这里用最新快照近似') acknowledges the approximation but the code ships it into the live weight-update path.

- **建议修复**: In run_ic_backtest (meta_learner.py:164-195): (1) Exclude the price-source snapshot from best_snap candidacy and require a genuine gap. Build candidates = snapshots[:-1] for the best_snap search, and use latest = snapshots[-1] as the price source; then guard that best_snap's timestamp is strictly older than latest's by a meaningful margin (e.g. reject if (latest_ts - best_ts) < 0.5 * lookback_days). (2) Validate the realized horizon: after picking best_snap, compute realized_days = (latest_ts - best_ts)/86400 and bail with ok:False if abs(realized_days - lookback_days) exceeds tolerance, instead of silently recording a wrong-horizon IC; alternatively record realized_days into the result and ic_history so weights are not mislabeled. (3) Make _spearman_rank_corr tie-aware (use average ranks for ties and the Pearson-on-ranks formula), so a zero-variance return vector yields IC=0 (or skip the factor) rather than a spurious value; at minimum, in run_ic_backtest skip the factor when the returns vector has zero variance (e.g. len(set(returns)) <= 1 -> continue). (4) Add a lower-bound check on the CLI lookback (cli.py:297,317) and in adaptive_trainer, e.g. reject lookback < some minimum. (5) Fix matched_coins (line 223) to report a stable count computed once (e.g. number of coins with old_price>0 and new_price>0) rather than the leaked last-iteration factor_vals.


---

## MEDIUM (8)

### M1. Legacy walk-forward BTC benchmark latches to 0 when BTC absent from the first snapshot, silently zeroing vs_btc_excess

- **位置**: `src/research/portfolio_backtest.py:134-136` · 子系统 `backtest-risk` · 置信度 high

- **问题**: btc_start_price is initialized to None (line 112) and captured only once, on the first rebalance period, via `if btc_start_price is None: btc_start_price = entry_prices.get('BTC', 0)`. If BTC is not present in the FIRST snapshot's coin universe, entry_prices.get('BTC', 0) returns 0, so btc_start_price becomes 0 (no longer None) and is never re-captured on later periods even once BTC appears. The final benchmark (line 220-221) then guards `if btc_start_price and btc_start_price > 0 else 0`, so btc_return collapses to 0 and vs_btc_excess = total_return - 0, i.e. the BTC-relative benchmark is meaningless. The screener universe routinely omits BTC early (the bundled snapshots start with an 18-coin alt universe that excludes BTC), so this fires on real data.

- **建议修复**: Capture BTC start price from the first period where BTC is actually present, instead of latching on None. Minimal change at portfolio_backtest.py:134-136 — guard on a falsy/missing start price rather than only None, and only set when BTC exists in that snapshot:

    # BTC 基准 (从第一个含 BTC 的快照开始计)
    if not btc_start_price and "BTC" in entry_prices:
        btc_start_price = entry_prices["BTC"]
    if "BTC" in exit_prices:
        btc_end_price = exit_prices["BTC"]

This makes the legacy engine track BTC's first-available -> last-available price like the vbt engine (close["BTC"].dropna().iloc[0]/[-1]). Note: btc_end_price should likewise only update when BTC is present in the exit snapshot (current line 136 defaults to btc_start_price, which is fine once the start is captured correctly). Keep the existing `if btc_start_price and btc_start_price > 0 else 0` guard at 220-221 so periods entirely without BTC still yield btc_return=0 gracefully.

### M2. knowledge.render_for_llm drops all past-call detail: loop body only assigns an unused variable

- **位置**: `src/knowledge.py:73-77` · 子系统 `evolution` · 置信度 high

- **问题**: The `for c in calls[-5:]` loop is meant to render each recent past judgement into the LLM knowledge context, but its body contains ONLY the assignment `correct_icon = {...}.get(...)`. correct_icon is never appended to `parts` (and the two `parts.append(...)` lines at 76-77 are dedented OUT of the loop, so they run once). Result: the computed icon is discarded every iteration and the per-call detail (date / thesis / outcome) is never emitted. The '过往判断复盘' section that gets injected into every briefing prompt is empty except for the trailing count line. This is silent knowledge loss — the LLM never sees the actual historical calls that are supposed to prevent repeated mistakes.

- **建议修复**: Indent the two trailing lines into the loop and actually emit each call's detail using correct_icon. Replace lines 73-77 with:

        for c in calls[-5:]:
            correct_icon = {"true": "✓", "false": "✗", "partial": "◐"}.get(
                str(c.get("correct", "")).lower(), "?")
            parts.append(
                f"- [{correct_icon}] {c.get('date', '—')} · {c.get('claim', '')} "
                f"→ {c.get('outcome', '')} (教训: {c.get('lesson', '—')})")
        parts.append("")
        parts.append(f"(共有 {len(calls)} 条历史判断, 用于防止重复错误。)")

This appends one detail line per recent call (using the computed icon) and keeps the blank line + summary count after the loop. Field names (claim/outcome/lesson) match the schema documented in knowledge/past_calls.yaml.

### M3. NaN correlation bypasses `is None` guard → NaN raw_value + invalid JSON in meta

- **位置**: `src/factors/_v04_factors.py:265-266, 271, 289` · 子系统 `factors` · 置信度 high

- **问题**: `_btc_yfin_corr` computes `corr = aligned['yf'].pct_change().corr(aligned['btc'].pct_change())`. When either aligned price series is constant over the window (a real and common case: a stale/flat yfinance fill, or a BTC price snapshot that dedups to one repeated value), pandas `.corr()` returns float('nan'), NOT None. The callers `compute_btc_nasdaq_corr` (line 271) and `compute_btc_gold_corr` (line 289) only guard `if corr is None: return []`, so NaN slips through. The factor then writes `raw_value = NaN` into the `factors.raw_value` REAL column AND serializes `meta = json.dumps({'obs':.., 'corr': NaN})`. Python's json emits the bare token `NaN`, which is INVALID per the JSON spec: any strict consumer (JS `JSON.parse` in the dashboard, or `json.loads(..., parse_constant=...)`) rejects it, and the NaN raw_value silently corrupts any downstream IC/zscore math. All NaN-vs-0.x threshold comparisons also fall through to signal=0, masking the bad data instead of skipping it.

- **建议修复**: Harden the guard to also reject NaN/inf. Minimal change in src/factors/_v04_factors.py — make _btc_yfin_corr return None on a non-finite correlation so the existing `if corr is None: return []` callers skip it. Replace line 265-266:

    corr = aligned["yf"].pct_change().corr(aligned["btc"].pct_change())
    return float(corr), {"obs": int(len(aligned))}

with:

    corr = aligned["yf"].pct_change().corr(aligned["btc"].pct_change())
    if corr is None or not math.isfinite(corr):
        return None, {"obs": int(len(aligned))}
    return float(corr), {"obs": int(len(aligned))}

(add `import math` at the top of the module if not already present). This makes both compute_btc_nasdaq_corr (line 271) and compute_btc_gold_corr (line 289) cleanly return [] on a flat/degenerate window, eliminating the NaN raw_value, the invalid `NaN` JSON token in meta, and the misleading signal=0. Optionally, as defense-in-depth, change the callers' guard to `if corr is None or not math.isfinite(corr): return []`.

### M4. `liquidation_heat` emits signal with INVERTED sign vs. the project-wide +1=bullish convention

- **位置**: `src/factors/_v04_factors.py:68-75` · 子系统 `factors` · 置信度 high

- **问题**: Every other factor and the global SIGNAL_EXPLAIN map (_metadata.py:11-15) use +1=看多/bullish, -1=看空/bearish, and signals/composite.py aggregates them as composite = Σ(signal_i × conf_i × weight_i). `liquidation_heat` violates this: when SHORTS dominate liquidations (long_ratio<0.35, i.e. a short squeeze / price pumping → a short-term TOP is near) it emits sig=+1; when LONGS dominate (price dumping → washout / bottom) it emits sig=-1. The factor's own metadata confirms the intended meaning is reversed: how_to_read[+1]='空头清算占主导...反向看空' and pm_action[+1]='减仓或对冲' (a BEARISH action), with the comment '注: 此因子反向解读'. But the composite engine does NOT special-case it — it just sums the raw signal — so liquidation_heat pushes the composite toward BULL exactly when the author says to reduce/hedge, and toward BEAR during a washout the author labels as an entry. It also renders as the opposite word via signal_label(). The 'reverse-read' annotation is an admission that the produced sign is non-canonical; the produced sign should be flipped (shorts-dominate → -1, longs-dominate → +1) so it composes correctly.

- **建议修复**: Swap the two signs in compute_liquidation_heat so the emitted signal matches the canonical convention and the factor's own how_to_read/pm_action text. In src/factors/_v04_factors.py:69-73 change to:
    # 信号(反向/contrarian): 多头清算占主导=washout底部=+1看多; 空头清算占主导=squeeze顶部=-1看空
    if long_ratio > 0.65:
        sig = 1     # longs liquidated (washout) -> contrarian bullish
    elif long_ratio < 0.35:
        sig = -1    # shorts liquidated (squeeze) -> contrarian bearish
    else:
        sig = 0
Then update _metadata.py: how_to_read[+1] should describe the long-liquidation/washout bullish case and pm_action[+1]="可考虑分批入场"; how_to_read[-1] the short-squeeze bearish case and pm_action[-1]="减仓或对冲"; and delete the now-incorrect "注: 此因子反向解读" annotation. No changes needed in composite.py / factor_bridge.py — they then compose correctly. (Note: a backtest-derived IR weight could mask this if it had learned a negative IR from the inverted sign, but get_latest_ir_weights filters IR<0, so the inverted factor would simply be dropped/under-weighted rather than corrected — fixing the source sign is the right remedy.)

### M5. Funding source-priority 'binance preferred' is defeated by the MAX(ts) join when okx is fresher

- **位置**: `src/factors/funding_composite.py:22-34` · 子系统 `factors` · 置信度 high

- **问题**: The intent (docstring + the sort_values('src')/drop_duplicates(keep='first') at lines 32-34) is to prefer binance over okx for the same asset. But the inner join selects rows at `MAX(ts)` PER asset_id across BOTH sources combined. If okx reported a newer funding_rate_8h than binance for that asset (common, since the two sources poll on independent schedules), only the okx row sits at MAX(ts), so the binance row is never even returned to the dedup step — the okx value is used regardless of the stated binance preference. Because funding signs can disagree between venues, this can flip the composite sign. The dedup logic only matters in the rare case both sources share the exact same MAX(ts).

- **建议修复**: Make source priority authoritative by taking MAX(ts) PER (asset_id, source) instead of per asset, then let the existing dedup enforce binance>okx. Replace the inner subquery + join (lines 25-28) with:

   JOIN (SELECT asset_id AS a_, source AS s_, MAX(ts) AS mts FROM raw_metrics
         WHERE source IN ('binance','okx') AND metric='funding_rate_8h'
         GROUP BY asset_id, source) m
   ON r.asset_id = m.a_ AND r.source = m.s_ AND r.ts = m.mts

This returns the latest binance row AND the latest okx row per asset; the unchanged df.sort_values('src').drop_duplicates(subset=['asset_id'], keep='first') at lines 33-34 then deterministically keeps binance when present and falls back to okx otherwise. The raw_metrics PK (ts,source,asset_id,metric) guarantees exactly one row per (asset,source,max-ts), so no duplicate-row ambiguity is introduced.

### M6. rd_agent LLM hypothesis path is dead code — calls .strip() on the dict returned by run_claude

- **位置**: `src/research/rd_agent.py:196-202` · 子系统 `research-agents` · 置信度 high

- **问题**: _propose_via_llm assumes run_claude returns a raw string and does s = (raw or '').strip().strip('`').strip() then json.loads(s). But _claude_runner.run_claude always returns a dict ({'ok': True, 'markdown': str} or {'ok': False, 'error': str}) — never a string (see _claude_runner.py:139,156,164,169). Calling .strip() on a dict raises AttributeError, which is swallowed by the broad except that logs 'LLM propose failed ... fallback to mutation'. Consequently the LLM-driven hypothesis generation NEVER works: even a perfectly valid Claude response is discarded and rd_agent silently falls back to rule-based mutation every time prefer_llm=True. The code never reads result['markdown'] nor checks result['ok'].

- **建议修复**: In src/research/rd_agent.py, replace lines 196-201 to consume the dict contract instead of treating raw as a string:

    result = run_claude(prompt, system="You output ONLY JSON.")
    if not isinstance(result, dict) or not result.get("ok"):
        log.warning(f"LLM propose declined: {result.get('error') if isinstance(result, dict) else result}; fallback to mutation")
        return []
    raw = result.get("markdown", "")
    s = (raw or "").strip().strip("`").strip()
    if s.startswith("json"):
        s = s[4:].strip()
    data = json.loads(s)

The remaining parsing (markdown code-fence strip + json.loads + Hypothesis construction at lines 202-211) is unchanged and verified to correctly parse a real ```json-fenced response. The existing broad except at 212-214 still guards against malformed JSON.

### M7. generate_report crashes (AttributeError) when narrative agent JSON contains sentiment: null

- **位置**: `src/research/report.py:429` · 子系统 `research-agents` · 置信度 high

- **问题**: The narrative-sentiment cell does nr.get('sentiment', 'N/A').replace('_', ' '). dict.get's default ('N/A') only applies when the key is ABSENT; if the LLM-produced agent JSON has the key present with a null value ("sentiment": null — entirely plausible LLM output, and the JSON is parsed by orchestrator._parse_agent_json without null-stripping), .get returns None and None.replace(...) raises AttributeError, aborting the ENTIRE HTML report generation (no report file written). The author already guards this exact pattern elsewhere — line 466 uses (rk.get('risk_level','N/A') or 'N/A').upper() — but missed it at line 429. This is the only unguarded chained string method on a .get default in the file.

- **建议修复**: Mirror the existing guard at line 466. Change report.py:429 from `{nr.get('sentiment', 'N/A').replace('_', ' ')}` to `{(nr.get('sentiment', 'N/A') or 'N/A').replace('_', ' ')}`. The `or 'N/A'` coalesces a present-but-None value to the 'N/A' string before .replace is called. (Optionally, for broader robustness against other None-valued fields, also strip null values in orchestrator._parse_agent_json, but the one-line guard fully fixes the reported defect.)

### M8. update_weights_from_ic feeds unconditioned (possibly tie/self-pairing-corrupted) IC into multiplicative weight updates with no statistical-significance gate

- **位置**: `src/research/meta_learner.py:274-298` · 子系统 `screener-meta` · 置信度 high

- **问题**: Per factor the decayed average IC is turned into a multiplicative bump `adjustment = 1 + avg_ic*0.5` applied to the raw weight, then all weights are renormalized. There is no check that the IC is statistically distinguishable from zero (unlike alpha_discovery, which at least attempts a Bonferroni floor) and no guard that the factor column had any variance. Combined with findings #1/#2, a dead constant factor with fabricated IC≈0.24 gets +12% weight every cycle and ratchets to its max cap; conversely a genuinely useful factor whose IC was computed from a degenerate window can be down-weighted. Because the final normalization (line 296-298) re-scales every factor's weight on each call, weights drift even on cycles where the gate at line 271 (`len(history)<MIN_IC_RECORDS → continue`) updated nothing, since merged-in new factors and rounding perturb the sum. Net effect: the 'self-evolving' weight vector is driven by statistically meaningless IC estimates.

- **建议修复**: Two minimal, independent fixes (either alone substantially mitigates; both is best):

1. Fix the tie handling in `_spearman_rank_corr` (meta_learner.py:137-153) so a constant/low-variance column yields IC=0. Either (a) detect zero variance and return 0.0 early: `if len(set(x)) < 2 or len(set(y)) < 2: return 0.0`, or (b) use average ranks for ties and the Pearson-on-ranks formula (the d² shortcut is invalid under ties).

2. Add a significance/variance gate in `update_weights_from_ic` before line 285, mirroring alpha_discovery. After computing `avg_ic` (line 280), skip the adjustment when the factor column was degenerate or the IC is not distinguishable from zero, e.g.:
```python
import math
n = ic_result.get("matched_coins", 0)
ic_floor = 1.96 / math.sqrt(max(n - 1, 1)) if n > 1 else 1.0  # ~95% noise band for Spearman
if abs(avg_ic) < ic_floor:
    continue  # IC indistinguishable from zero → leave weight unchanged
```
This prevents a dead/constant factor (and any factor whose IC came from a degenerate window) from ratcheting its weight on noise.


---

## LOW (18)

### L1. Wrong USDC contract address in cryo_onchain WELL_KNOWN_TOKENS → silently wrong/empty on-chain CEX-flow for USDC

- **位置**: `src/adapters/cryo_onchain.py:src/adapters/cryo_onchain.py:74` · 子系统 `adapters` · 置信度 high

- **问题**: The Ethereum USDC contract is hard-coded as 0xa0b86a33E6441E6f6E5BB1c5bF7B98Fc7e8E1Bf2 (lowercased), which is NOT the real Circle USDC contract. The canonical mainnet USDC is 0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48 — and the sibling module cryo_warehouse.py:64 uses the correct one, so the two files disagree. When enrich_onchain('USDC') (reachable via src/research/onchain_real.py:308) runs, it passes this bogus address to `cryo erc20_transfers --contract <bad>`. cryo will find no Transfer logs for a non-existent token, so detect_cex_flow returns all-zero counts (cex_inflow_count=0, net_cex_flow=0, total_transfers=0) and the caller treats that as a legitimate 'no CEX activity' reading. This is silent data corruption: USDC whale/CEX-flow signals are permanently zero rather than missing. This is NOT graceful degradation — the bug is in the data (wrong address), independent of cryo being installed.

- **建议修复**: Fix the constant at src/adapters/cryo_onchain.py:74 to the canonical Circle USDC contract, matching cryo_warehouse.py:64:

    "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48".lower(),  # ETH USDC (Circle)

(Optionally de-duplicate by sourcing token addresses from a single shared map so the two modules cannot drift again.)

### L2. binance.fetch() crashes (not degrades) on an HTTP-200 error body, dropping the entire binance source for the run

- **位置**: `src/adapters/binance.py:src/adapters/binance.py:30` · 子系统 `adapters` · 置信度 medium

- **问题**: Binance public endpoints can return HTTP 200 with a JSON *object* error body (e.g. {"code":-1003,"msg":"Too much request weight used; IP banned ..."}) under weight bans / IP throttling. _safe_get only returns None on transport failure, so on a 200-with-error-body it returns that dict. Line 27 guards `if not price_data` but a non-empty error dict is truthy, so execution reaches line 30: `prices = {p["symbol"]: float(p["price"]) for p in price_data}`. Iterating a dict yields its string keys, so p["symbol"] raises `TypeError: string indices must be integers`. The same pattern bites the funding loop at line 41 `float(data[-1]["fundingRate"])` (data[-1] on a dict → KeyError: -1) and the 24h stats comprehension at line 48 `s["symbol"]`. fetch() has no try/except, so the exception propagates to pipeline.run_ingest_all (src/pipeline.py:26), which logs it and records stats['binance']=-1 — i.e. the WHOLE binance source is dropped for that cycle instead of degrading to []. The intended-degradation policy covers a clean 451/blocked → [], but here a malformed-200 body crashes instead of degrading.

- **建议修复**: Validate the response shape before iterating, matching the codebase's own convention (isinstance(..., list)). In src/adapters/binance.py:

Line 27: change `if not price_data:` to `if not price_data or not isinstance(price_data, list):` (keeps the existing 451/blocked → [] degradation and now also degrades on a 200 error-object).

Line 41: guard the element shape, e.g. `if data and isinstance(data, list):` (replacing `if data:`), so a dict body skips instead of doing data[-1] on a dict.

Line 45-46: change `if stats_data is None: stats_data = []` to `if not isinstance(stats_data, list): stats_data = []` so a 200 error-object on the 24hr endpoint degrades to empty stats instead of crashing the comprehension at line 47-48.

Optionally, fix at the root in src/http_client.py by detecting Binance-style error envelopes (a dict containing both "code" and "msg") on a 200 and returning None, but the per-adapter isinstance guards are the minimal change and are consistent with screener.py / factors_extended.py / coinglass.py.

### L3. Router unifies incompatible win_rate semantics: vbt reports per-trade win rate, legacy reports per-period win rate

- **位置**: `src/research/portfolio_backtest_vbt.py:211` · 子系统 `backtest-risk` · 置信度 high

- **问题**: backtest_router._normalize maps both engines' results into a single 'win_rate' field, but the two engines compute fundamentally different quantities. The legacy engine (portfolio_backtest.py:216-217) computes win_rate = fraction of rebalance PERIODS with positive portfolio return. The vbt engine pulls stats['Win Rate [%]'], which vectorbt defines as the fraction of closed TRADES (per-asset round trips) that were profitable. These are not comparable: on the same data legacy reports 0.43 (period win rate) while vbt reports 0.587 (per-trade win rate over 429 closed trades). Any downstream consumer or UI comparing engines, or any test asserting cross-engine consistency, will be misled because the field name implies identical meaning.

- **建议修复**: Make the two engines emit the same definition of `win_rate`, or rename so the meaning is unambiguous. Cheapest correct fix: compute a per-period win rate for the vbt engine from its equity/period returns instead of pulling vectorbt's per-trade `Win Rate [%]`. In `portfolio_backtest_vbt.py` around line 211, replace:
    win_rate = float(stats.get("Win Rate [%]", 50.0)) / 100.0
with a period-based computation aligned to legacy, e.g. derive returns over the rebalance periods (the schedule has notna rows at lines 181/238) and take the fraction positive:
    rb_mask = schedule.notna().any(axis=1)
    eq_rb = equity[rb_mask]                         # equity at each rebalance boundary
    period_rets = eq_rb.pct_change().dropna()
    win_rate = float((period_rets > 0).mean()) if len(period_rets) else 0.5
This yields a per-period win rate comparable to legacy's `wins / n_periods`. Alternatively, keep the per-trade number but rename the vbt field to `trade_win_rate` and exclude it from UNIFIED_FIELDS (or tag it), so `_normalize` never conflates the two definitions under one name.

### L4. vbt Sharpe/annualization assumes a contiguous daily index but the snapshot index has multi-day gaps

- **位置**: `src/research/portfolio_backtest_vbt.py:195-209` · 子系统 `backtest-risk` · 置信度 high

- **问题**: _build_price_matrix produces a DatetimeIndex straight from snapshot dates with no resampling and index.freq is None; the actual index has gaps (day-to-day spacing of 1, 2, 3 and 6 days — 95 rows over a 103-day span). The code nonetheless hard-codes freq='1D' in from_orders (line 196), so vectorbt's stats['Sharpe Ratio'] annualizes by treating every row as exactly one calendar day (factor ~sqrt(365) per row). A return realized over a 6-day gap is scaled as if it were a 1-day return, so the per-row volatility and hence the annualized Sharpe are systematically distorted. (max_drawdown/equity are path metrics and unaffected, but the reported sharpe — and therefore the calmar/Sharpe-ranked best_config in the sweep — are biased.)

- **建议修复**: Derive periods_per_year from the actual elapsed time of the index instead of hard-coding freq="1D", and pass it through so vbt annualizes correctly. Minimal change: compute the mean gap and set freq accordingly, e.g. replace freq="1D" with a computed median/mean spacing:

    span_days = (close.index[-1] - close.index[0]).days
    n = len(close.index)
    avg_gap_days = span_days / (n - 1) if n > 1 else 1.0
    ... from_orders(..., freq=pd.Timedelta(days=avg_gap_days))   # vbt then uses year_freq/freq = 365/avg_gap

This makes the per-row annualization factor sqrt(365/avg_gap) ≈ sqrt(333) here, matching the true sampling. A more robust fix is to resample close/score to a regular daily grid (close = close.resample("1D").ffill()) before from_orders so every row really is one calendar day, which also fixes the legacy-engine inconsistency. Either way, recompute sharpe at line 209 from the corrected freq.

### L5. Legacy annualization and Sharpe use n_periods*rebalance_days instead of the true calendar span

- **位置**: `src/research/portfolio_backtest.py:189-197` · 子系统 `backtest-risk` · 置信度 high

- **问题**: total_days is computed as n_periods * rebalance_days (line 189) and periods_per_year as 365/rebalance_days (line 195), both assuming every period is exactly rebalance_days long. But rebalance_dates are derived from actual snapshot dates with a '>= rebalance_days' gate (line 94) AND the final snapshot date is force-appended regardless of spacing (line 100-101), so real periods can be longer/shorter than rebalance_days and the last period in particular is arbitrary. This makes annual_return (which uses 365/total_days as the exponent) and the Sharpe annualization slightly biased relative to the true span. Low severity because snapshots are ~daily so the error is small, but it is a genuine wrong-units assumption.

- **建议修复**: Mirror the vbt engine: derive total_days from the actual rebalance-date endpoints and base the Sharpe annualization on the true average period length instead of assuming every period equals rebalance_days.

Replace line 189:
    total_days = n_periods * rebalance_days
with a true-span computation (rebalance_dates is in scope, all entries parse as %Y-%m-%d):
    _d0 = datetime.strptime(rebalance_dates[0], "%Y-%m-%d")
    _d1 = datetime.strptime(rebalance_dates[-1], "%Y-%m-%d")
    total_days = max((_d1 - _d0).days, 1)

And replace line 195:
    periods_per_year = 365 / rebalance_days
with one based on the realized average period length:
    avg_period_days = total_days / max(n_periods, 1)
    periods_per_year = 365 / avg_period_days

This makes annual_return (line 190), periods_per_year/risk_free_per_period/sharpe (196-198), sortino (228), and calmar (213) consistent with the true calendar span and with the vectorbt engine. Note total_days is also surfaced in the result dict (line 248) and logs (lines 259-260), which then become accurate too.

### L6. cross_price deviation computation divides by mean with no zero guard

- **位置**: `src/review/cross_price.py:27-29` · 子系统 `backtest-risk` · 置信度 high

- **问题**: After fetching the latest price per source, mean = sum(prices.values())/len(prices) and devs = {s:(p-mean)/mean ...}. There is no guard for mean==0. If every source's latest price_usd row is 0 (e.g. a stored stale/zero price for an asset), mean is 0 and the dict comprehension raises ZeroDivisionError, aborting the entire cross_price.run() review for all remaining assets in CFG['universe']. raw_metrics.value is a nullable REAL so a literal 0 price is storable.

- **建议修复**: Guard the degenerate case and isolate per-asset failures so one bad asset cannot wipe the whole pass. Replace lines 26-29 region and wrap the loop body:

    for asset in CFG["universe"]:
        try:
            df = query_df(...same SQL...)
            if len(df) < 2:
                continue
            # drop NULL/NaN and non-positive prices before computing deviations
            valid = {s: float(v) for s, v in zip(df["source"], df["value"])
                     if v is not None and pd.notna(v) and float(v) > 0}
            if len(valid) < 2:
                continue
            prices = valid
            mean = sum(prices.values()) / len(prices)
            if mean <= 0:          # extra safety; can't happen after >0 filter
                continue
            devs = {s: (p - mean) / mean for s, p in prices.items()}
            max_d = max(abs(d) for d in devs.values())
            ... existing severity + results.append ...
        except Exception as e:
            log.warning("price_cross skip %s: %s", asset["id"], e)
            continue

(Requires `import pandas as pd` and a logger.) Filtering on `v > 0` simultaneously removes the ZeroDivisionError, the NULL->TypeError, and the silent-NaN cases; the per-asset try/except ensures earlier results still reach upsert_review.

### L7. compose() confidence is divided by the global weight-sum, not the asset's present factors — every signal's confidence is structurally understated

- **位置**: `src/signals/composite.py:120` · 子系统 `core-pipeline` · 置信度 high

- **问题**: Per-asset confidence is computed as min(1.0, denom / sum(FACTOR_WEIGHTS.values())) where denom = Σ(conf_i * w_i) over ONLY the factors that exist for that asset, but the divisor is the sum of ALL 15 default weights (=12.6). Because factors are partitioned across assets (each coin carries only its own 2-3 factors; market-level factors group under 'market'), the numerator can never approach the denominator. This is wrong every single run: a per-coin signal whose two factors both fire strongly reports confidence ~0.14, and even an (impossible) asset carrying all 15 factors at conf 0.7 caps at 0.70. The confidence written to the signals table and surfaced to the dashboard/LLM brief is systematically deflated. Correct normalizer is the sum of weights of factors actually present in the group (Σ w_i over grp), i.e. denom / Σ(w_i), not the global constant.

- **建议修复**: In src/signals/composite.py, normalize confidence by the weight mass of the factors actually present in the group instead of the global constant. Accumulate the present-group weight sum alongside denom in the per-row loop, e.g. add `wsum = 0.0` before the loop (line 100) and `wsum += w` inside it (after line 107), then replace line 120:
    confidence = min(1.0, denom / sum(FACTOR_WEIGHTS.values()))
with
    confidence = min(1.0, denom / wsum) if wsum else 0.0
This makes denom/wsum equal the weighted-average per-factor confidence of the present factors, so a fully-firing 2-factor coin can reach ~0.8 and confidence becomes comparable across assets regardless of how many factors live in each group.

### L8. cross_price.run() divides by mean without guarding mean==0 / None prices (ZeroDivisionError or TypeError)

- **位置**: `src/review/cross_price.py:27-28` · 子系统 `core-pipeline` · 置信度 high

- **问题**: mean = sum(prices.values()) / len(prices) then devs = {s: (p - mean) / mean ...}. If two+ sources exist but a source stored value=NULL (raw_metrics.value is nullable), prices.values() contains None and sum() raises TypeError. If all returned prices are 0 (a degraded/sentinel source still emitting price_usd=0), mean==0 and (p-mean)/mean raises ZeroDivisionError. The per-source fetch degradation does not protect this aggregation step because the rows are already in the DB; one bad NULL/0 price row aborts the entire price_cross review (caught by pipeline's try/except, so the whole check is skipped rather than producing partial results).

- **建议修复**: Guard both the NaN/None inputs and mean==0 before computing deviations in src/review/cross_price.py (after line 26):

    prices = dict(zip(df["source"], df["value"]))
    # drop NULL/NaN/non-positive prices so one bad row can't poison the mean
    prices = {s: float(p) for s, p in prices.items()
              if p is not None and not pd.isna(p) and float(p) > 0}
    if len(prices) < 2:
        continue
    mean = sum(prices.values()) / len(prices)
    if mean <= 0:
        continue
    devs = {s: (p - mean) / mean for s, p in prices.items()}

(requires `import pandas as pd` at top, or replace pd.isna(p) with `p == p` NaN-check). This prevents ZeroDivisionError on all-zero inputs and stops NaN/None prices from silently producing a bogus severity="OK" review row, while still emitting a valid cross-price result for the remaining good sources.

### L9. compose() uses `signal or 0` / `confidence or 0.5`, which yields NaN (not the default) for NULL factor values

- **位置**: `src/signals/composite.py:104-105` · 子系统 `core-pipeline` · 置信度 high

- **问题**: The factors table permits NULL signal/confidence (schema: signal INTEGER, confidence REAL, both nullable). pandas reads a NULL as float NaN. `sig = row['signal'] or 0` and `conf = row['confidence'] or 0.5` do NOT substitute the default because NaN is truthy in Python — `np.nan or 0` returns nan. A single NaN then poisons num/denom (composite becomes NaN) and int(sig) on line 109 raises ValueError('cannot convert float NaN to integer'), aborting compose() for that timestamp. Currently latent because every shipped factor emits concrete ints, but the guard is incorrect and any future factor (or manual DB row) that leaves signal NULL silently breaks the whole signal stage. Use pd.notna()/explicit None checks instead of truthiness.

- **建议修复**: Replace truthiness guards with explicit NaN/None handling at src/signals/composite.py:104-105. Mirror the raw_value pattern already used on line 110, e.g.:\n\n    import pandas as pd  # at module top\n    ...\n    sig = 0 if pd.isna(row["signal"]) else int(row["signal"])\n    conf = 0.5 if pd.isna(row["confidence"]) else float(row["confidence"])\n\nThen line 106 becomes `num += sig * conf * w`, line 109 can use `int(sig)` safely (already coerced), and round(float(conf), 3) is safe. pd.isna() correctly handles both Python None and float NaN.

### L10. get_latest_ir_weights cherry-picks MAX(ir) across all forward_days/window_days, biasing dynamic weights upward

- **位置**: `src/review/backtest.py:159-171` · 子系统 `core-pipeline` · 置信度 medium

- **问题**: The query GROUPs only by (factor, asset_id) and takes MAX(ir) over every (window_days>=30, forward_days in {1,7,30}) combination, i.e. it selects the single best-performing horizon per factor. _effective_weights (composite.py:53) then sets weight = max(default, avg_ir), so weights can only ratchet up. The combination means a factor that looks good on just one of three forward windows is promoted using that best window, never penalized for the others. Coupled with the upward-only max(default, ir), this is a selection-bias overweighting of factors rather than an unbiased IR-weighting. Not a crash and the negative-IR filter is intentional, but the MAX-across-horizons makes the 'dynamic weight' optimistic. Consider averaging IR over horizons or fixing a single forward window.

- **建议修复**: Pick the IR row and its OWN n_obs atomically instead of independent column MAXes, and choose the aggregation deliberately. Minimal correct version that both (a) ties n_obs to the selected IR and (b) optionally fixes a single horizon to avoid cherry-picking:

```sql
-- one row per (factor, asset_id): the row with the highest IR among recent runs,
-- carrying THAT row's own n_obs
SELECT factor, asset_id, ir, n_obs
FROM (
  SELECT factor, asset_id, ir, n_obs,
         ROW_NUMBER() OVER (
           PARTITION BY factor, asset_id
           ORDER BY ir DESC
         ) AS rn
  FROM factor_performance
  WHERE window_days >= 30 AND n_obs >= 10
        AND date = (SELECT MAX(date) FROM factor_performance)  -- avoid mixing run dates
) t
WHERE rn = 1
```

This guarantees the returned n_obs belongs to the chosen IR row (fixing the decoupled-MAX defect) and restricts to the latest computation date. To additionally remove the cross-horizon optimism flagged in issue (1), either add `AND forward_days = 7` to fix a single forward window, or replace `MAX(ir)`/ROW_NUMBER-by-IR with `AVG(ir)` over horizons so a factor weak on 2 of 3 windows is penalized rather than promoted by its single best horizon.

### L11. weekly_review graph node stores stringified dict into weekly_review_path instead of a path

- **位置**: `src/evolution/graph.py:165-167` · 子系统 `evolution` · 置信度 high

- **问题**: node_weekly_review does `path = weekly_review.run_weekly_review(); if path: state['weekly_review_path'] = str(path)`. But run_weekly_review() returns a dict ({'ok':True,'file':..., 'week':..., 'chars':...}), not a path. The dict is truthy, so weekly_review_path is set to the repr of the whole dict (e.g. "{'ok': True, 'file': '/…/weekly_review_2026-W22.md', …}"). Any downstream consumer (dashboard / get_last_state) that treats weekly_review_path as a filesystem path will get a malformed value. Should read path = result.get('file').

- **建议修复**: In src/evolution/graph.py node_weekly_review, read the file key off the returned dict instead of stringifying the whole dict:

    result = weekly_review.run_weekly_review()  # returns dict
    fpath = (result or {}).get("file") if isinstance(result, dict) else None
    if fpath:
        state["weekly_review_path"] = str(fpath)

This stores the actual .md path on success and correctly leaves weekly_review_path as None when run_weekly_review fails (the failure dict has no 'file' key).

### L12. narrative_tracker crashes if LLM emits non-numeric heat_score/delta_7d (float() outside try)

- **位置**: `src/evolution/narrative_tracker.py:116-124` · 子系统 `evolution` · 置信度 high

- **问题**: After json.loads succeeds, the row-building loop calls float(n.get('heat_score', 0)) and float(n.get('delta_7d', 0)) for each narrative. These casts are NOT inside any try/except (the only try wraps json.loads at line 105-109). If the LLM returns a non-numeric value such as "heat_score": "high" or "heat_score": "~80" (well within the realm of malformed LLM JSON), float() raises ValueError, which propagates out of run_narrative_tracking() uncaught and aborts the narrative-tracking step. The function otherwise goes to great lengths to tolerate malformed JSON, so this is an inconsistent gap. ('+12' style values parse fine via float(), so the risk is specifically non-numeric strings.)

- **建议修复**: Add a tiny tolerant numeric coercion helper and use it for both fields so malformed values degrade to 0.0 instead of crashing (consistent with the rest of the function's defensive JSON handling). In src/evolution/narrative_tracker.py, replace lines 120 and 123:

  "heat_score": float(n.get("heat_score", 0)),
  ...
  "delta_7d": float(n.get("delta_7d", 0)),

with:

  "heat_score": _to_float(n.get("heat_score", 0)),
  ...
  "delta_7d": _to_float(n.get("delta_7d", 0)),

and define near the top of the module:

  def _to_float(v, default=0.0):
      try:
          return float(v)
      except (TypeError, ValueError):
          return default

Optionally, if you want to salvage values like "~80" / "+12" / "85%", strip non-numeric chars first (e.g. re.sub(r"[^0-9.\-]", "", str(v))) before float(), still falling back to default on failure. Either way the key fix is wrapping the cast so a non-numeric LLM value cannot abort run_narrative_tracking().

### L13. Dashboard Top-10 "30d" column always renders +0.0% (wrong key name)

- **位置**: `src/research/dashboard.py:src/research/dashboard.py:93` · 子系统 `report-notify-infra` · 置信度 high

- **问题**: generate_dashboard() reads each coin's 30-day change via c.get('change_30d', 0), but the snapshot JSON the dashboard loads (data/meta/snapshot_*.json) stores momentum under 'f_momentum_30d' / 'f_momentum_7d' and has NO 'change_30d' key. So the default 0 is ALWAYS used: every row's 30d cell shows '+0.0%' and is colored green (chg>0 is false, so chg_color falls to the red branch but value is literally +0.0%). The displayed 30d performance column is permanently fake/zero — a silent data-correctness bug in the user-facing dashboard, not a crash.

- **建议修复**: Persist the raw 30d change in the snapshot so the dashboard can read it. In src/research/meta_learner.py save_snapshot() (around line 117-125), add change_30d to the per-coin dict:

    snapshot["coins"].append({
        "symbol": c["symbol"],
        "price": c["price"],
        "market_cap": c["market_cap"],
        "composite_score": c["composite_score"],
        "change_30d": c.get("change_30d", 0),   # <-- add this line
        **{k: v for k, v in c.items() if k.startswith("f_")},
    })

(Optionally also change_7d/change_24h if the dashboard ever needs them.) Do NOT try to derive the percentage from f_momentum_30d in the dashboard: f_momentum_30d is a sigmoid score `2/(1+exp(-chg_30d/30)) - 1` in [-1,1] (screener.py:243), not a percent, so it cannot be displayed as "%". Note: existing historical snapshots won't have the key and will keep showing +0.0% until regenerated — acceptable since they backfill on the next screen run. Minor extra: dashboard.py:94 colors a 0% change red; arguably it should be neutral, but that is out of scope for this fix.

### L14. Two documented watchdog alert types (liquidations >$500M, ETF outflow >$200M) are never checked

- **位置**: `src/research/watchdog.py:src/research/watchdog.py:60-61` · 子系统 `report-notify-infra` · 置信度 high

- **问题**: The module docstring (lines 6-9) promises 6 alerts including '24h 全市场清算量 > $500M' and 'BTC ETF 单日净流出 > $200M', and THRESHOLDS defines liquidations_usd_24h and etf_outflow_usd. But the CHECKS registry (lines 259-264) only registers peg / funding / fear_greed / dex_tvl. grep confirms liquidations_usd_24h and etf_outflow_usd are referenced ONLY at their definition lines — no check function consumes them. Result: two of the six advertised risk alerts silently never fire, so operators relying on the documented liquidation / ETF-outflow warnings get no notification.

- **建议修复**: Reconcile the documentation with the implementation. Either (a) implement check_liquidations() and check_etf_outflow() and register them in CHECKS as ("liquidations", check_liquidations) and ("etf_outflow", check_etf_outflow) — each returning None when its (now-paywalled) upstream source is unavailable, matching the existing graceful-degradation pattern; or (b) if those sources are intentionally dropped, remove lines 6-7 from the docstring and delete the unused liquidations_usd_24h / etf_outflow_usd entries (lines 60-61) so the docstring, THRESHOLDS, and CHECKS stay consistent and operators are not misled into expecting alerts that can never fire.

### L15. Notifier 24h-change query lacks source-priority CASE, can pair binance price with a different source's change%

- **位置**: `src/notifier.py:src/notifier.py:76-83` · 子系统 `report-notify-infra` · 置信度 high

- **问题**: _gather_summary builds the price snapshot with an explicit source-priority CASE (binance>okx>coingecko, lines 68-71) but the parallel change_24h_pct query (lines 77-82) orders ONLY by `r.ts DESC` with no source CASE. For a given asset the card therefore shows the binance price next to whichever source happened to write change_24h_pct most recently (possibly okx/coingecko), so price and 24h% on the Feishu card can come from different exchanges. daily.py's equivalent query (lines 71-81) DOES apply the same CASE to change, so the two surfaces are inconsistent. Cosmetic (values are close), no crash.

- **建议修复**: In src/notifier.py:76-83, add the same source-priority CASE to the change_24h_pct query's ORDER BY so price and change% come from the same prioritized source. Replace line 79 `ROW_NUMBER() OVER (PARTITION BY r.asset_id ORDER BY r.ts DESC) AS rn` with:

  ROW_NUMBER() OVER (PARTITION BY r.asset_id
    ORDER BY CASE r.source WHEN 'binance' THEN 1 WHEN 'okx' THEN 2
                           WHEN 'coingecko' THEN 3 ELSE 9 END,
             r.ts DESC) AS rn

This makes notifier identical to src/report/daily.py:71-81 and dashboard_utils.py:216-225. Apply the same one-line fix to llm_brief.py:83-89 (line 86), which has the identical omission.

### L16. Control-center subprocess timeout never fires on a no-output hang (blocks entire Streamlit run)

- **位置**: `pages/0_🚀_控制中心.py:84-93` · 子系统 `ui` · 置信度 high

- **问题**: run_cmd() streams the child's stdout with `for line in iter(proc.stdout.readline, "")` and only checks the elapsed-time timeout INSIDE that loop body (line 87). readline() blocks until a newline or EOF, so if a launched CLI subprocess produces no output but does not exit (e.g. an upstream HTTP call hanging on connect, or a command waiting on stdin), readline() never returns, the timeout branch at line 87-93 is never reached, and proc.wait() (line 95) is never reached either. Because Streamlit executes the page script single-threaded and synchronously, the whole page hangs indefinitely — the 'cancel'/'kill all' buttons cannot be clicked because the script never yields back to the event loop. This subsystem is built entirely around shelling out to long jobs (screen ~2min, nightly 5-15min), so a stuck network call mid-job freezes the UI with no recovery except killing Streamlit.

- **建议修复**: Make the elapsed-time timeout independent of incoming output. Replace the blocking readline loop (lines 84-93) with a poll-based read that wakes up regardless of child output, e.g.:

```python
import selectors
sel = selectors.DefaultSelector()
sel.register(proc.stdout, selectors.EVENT_READ)
while True:
    if proc.poll() is not None and not sel.get_map():
        break
    for _ in sel.select(timeout=1.0):          # wakes every 1s even with no output
        line = proc.stdout.readline()
        if line:
            log += line
            log_box.code(log[-5000:], language="bash")
    if time.time() - t0 > timeout:             # now checked on every 1s tick
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            proc.kill()
        log += f"\n[超时 {timeout}s, 已中止]"
        break
    if proc.poll() is not None:
        # drain any remaining buffered output, then exit
        for line in proc.stdout:
            log += line
        break
sel.close()
proc.wait()
```

(selectors works on the POSIX pipe fd here; this is macOS/Linux, matching the os.killpg/os.getpgid usage already in the file. On Windows a watchdog thread that calls proc.kill() after `timeout` would be the portable alternative.) This guarantees the advertised `timeout` fires on a silent, still-running child.

### L17. 历史复盘: selectable date list is sliced positionally by `lookahead` days, but snapshots are not one-per-day (and contain duplicate dates)

- **位置**: `pages/9_📜_历史复盘.py:151` · 子系统 `ui` · 置信度 high

- **问题**: `selectable_dates = [s["date"] for s in snaps[:-lookahead] if s["date"]]` assumes the snapshot list has exactly one entry per calendar day spaced one day apart, so that dropping the last `lookahead` list elements equals dropping the last `lookahead` days. Two facts break this: (1) load_snapshots() (lines 41-51) appends EVERY snapshot_*.json without deduping by date, and the data dir actually has 104 files for only 95 unique dates (verified) — so duplicate date strings end up in selectable_dates and are shown twice in the st.selectbox. (2) The dates are not contiguous (verified gaps of 2, 3 and 6 days), so `snaps[:-lookahead]` removes a fixed COUNT of trailing snapshots rather than a `lookahead`-day window. The downstream build_forward_returns() (lines 64-108) instead uses real date arithmetic (dt + timedelta(days=lookahead)), so the set of dates offered for selection is inconsistent with the set that actually has a comparable forward snapshot — the cutoff is off by however many duplicate/missing days exist, and a user can pick a date for which forward.get(sel_date) is empty even though it survived the positional slice.

- **建议修复**: Dedupe by date and build the selectable list from real date arithmetic instead of a positional slice. E.g. replace lines around 151:

    # one entry per date (keep latest file per date), keyed for date math
    by_date = {s["date"]: s for s in snaps if s.get("date")}
    all_dates = sorted(by_date)
    cutoff = datetime.strptime(all_dates[-1], "%Y-%m-%d") - timedelta(days=lookahead)
    selectable_dates = [d for d in all_dates
                        if datetime.strptime(d, "%Y-%m-%d") <= cutoff]

This removes duplicates from the selectbox and makes the offered set consistent with build_forward_returns' date-based window. Ideally also dedupe inside load_snapshots() so every consumer sees one snapshot per date.

### L18. 历史走势: per-asset price normalization divides by first sample, yielding inf/NaN when the first price is 0

- **位置**: `pages/4_📈_历史走势.py:50` · 子系统 `ui` · 置信度 high

- **问题**: norm = (grp["value"] / grp["value"].iloc[0] - 1) * 100 normalizes each asset's price series to its first sampled value. There is no guard that grp["value"].iloc[0] is non-zero/non-NaN. If the very first binance price_usd sample for an asset is 0 or NULL (e.g. a malformed/partial ingest row, or a newly-listed asset that returned 0), the whole normalized series becomes inf/NaN and the line is dropped/blank in the plotly chart with a RuntimeWarning. It does not crash (plotly tolerates NaN) but the asset silently disappears from the relative-return chart. Real-world likelihood is low because live crypto prices are rarely exactly 0, hence low severity.

- **建议修复**: Guard the normalization base so a zero/NaN first sample skips the trace (or is dropped) instead of producing an inf/NaN series. Replace pages/4_📈_历史走势.py:47-50:

    for asset, grp in price_df.groupby("asset_id"):
        base = grp["value"].iloc[0]
        if len(grp) < 2 or not pd.notna(base) or base == 0:
            continue
        # 归一化为 0 = 第一次采集的价格
        norm = (grp["value"] / base - 1) * 100

Optionally also drop NULL/zero price rows up front, e.g. `price_df = price_df[price_df["value"].notna() & (price_df["value"] != 0)]` after parsing ts, so a bad first row no longer anchors the series. A defense-in-depth complement is to skip emitting the price_usd row in src/adapters/binance.py when `prices.get(sym) is None` (line 54/59), avoiding NULL price samples at the source.


---

## NIT (2)

### N1. Correlation `obs` meta over-reports by one (counts aligned price rows, not return observations)

- **位置**: `src/factors/_v04_factors.py:262-266` · 子系统 `factors` · 置信度 high

- **问题**: `obs` is set to len(aligned) (number of aligned daily PRICE points), but the correlation is computed on pct_change(), which drops the first row, so the actual number of return observations used is len(aligned)-1. The reported obs is therefore always one higher than the true sample size feeding corr, slightly overstating confidence/coverage in the meta and any audit that reads it. The < 10 minimum-obs gate at line 263 also gates on price-rows, so the real minimum returns used is 9, not 10.

- **建议修复**: Report the number of return observations actually used rather than the price-row count, and gate on it. E.g. compute the paired returns once and use their length:

```python
yf_ret = aligned["yf"].pct_change()
btc_ret = aligned["btc"].pct_change()
paired = pd.concat({"yf": yf_ret, "btc": btc_ret}, axis=1).dropna()
n = len(paired)
if n < 10:
    return None, {"obs": n}
corr = paired["yf"].corr(paired["btc"])
return float(corr), {"obs": int(n)}
```

This makes obs equal the true sample size feeding corr (len(aligned)-1 in the typical fully-overlapping case) and makes the `< 10` gate count returns, not prices.

### N2. 历史复盘: median uses upper-middle element for even-length lists, biasing the Top-N hit-rate baseline

- **位置**: `pages/9_📜_历史复盘.py:117` · 子系统 `ui` · 置信度 high

- **问题**: topn_hit_rate() computes the market median as `median = sorted(all_rets)[len(all_rets) // 2]`. For an even number of compared coins this returns the upper of the two central values, not their average — e.g. for [-0.02, 0.05, 0.10, 0.20] it returns 0.10 instead of the true median 0.075 (verified). This median is the benchmark every Top-N hit ('ret_pct > median', line 121) is measured against, and it is also the '全市场中位' metric shown at line 187 and used for the 'beat_btc/超额收益天数' count at line 257-258. The upward bias makes the displayed hit-rate and excess-return slightly pessimistic/inconsistent. Pure statistical inaccuracy, no crash — low severity.

- **建议修复**: Use a true median. Replace line 117 `median = sorted(all_rets)[len(all_rets) // 2]` with `import statistics` at top of file and `median = statistics.median(all_rets)`, or inline: `_s = sorted(all_rets); median = _s[len(_s)//2] if len(_s) % 2 else (_s[len(_s)//2 - 1] + _s[len(_s)//2]) / 2`. all_rets is guaranteed non-empty here because per_coin truthiness is checked at line 113 and rows derive from it.


---

## 已驳回的误报 (7) — 核验层正确拦截

- [adapters] get_stable_peg_health: `circ < 1e7` raises TypeError when a stablecoin's circulating.peggedUSD is null

- [factors] `stablecoin_mint` confidence compares row-count against a day-count threshold

- [core-pipeline] snapshot._get_current_prices selects a 0-valued binance price over a valid lower-priority price, then silently drops it to NULL

- [backtest-risk] deflated_sharpe public API documents annualized SR with raw per-observation n, which inflates PSR/DSR toward 1.0

- [report-notify-infra] build_briefing crashes on None raw_value in F&G-neutral / funding / coinbase / ETF branches (missing None guard asymmetry)

- [report-notify-infra] HTTP cache key truncated to 240 chars — long URL/param combos can collide and return wrong cached data

- [ui] 数据健康: price cross-validation deviation divides by the row mean (0 when all sources report 0)
