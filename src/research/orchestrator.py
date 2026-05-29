"""Multi-Agent 研究编排器。

流程:
  1. GitHub Agent  ─┐
  2. Onchain Agent  ├─ 并行 (3 个 agent 同时跑)
  3. Narrative Agent─┘
  4. Risk Agent ← 汇总 1-3 的结果
  5. Alpha Agent ← 汇总 1-4 的结果 → 最终投资分析
  6. 生成 HTML 报告
"""
from __future__ import annotations
import json
import re
import os
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from ..utils import setup_logger
from .agents import (
    run_github_agent, run_onchain_agent, run_narrative_agent,
    run_risk_agent, run_alpha_agent,
)

log = setup_logger("research_orchestrator", "INFO")


def _parse_agent_json(result: dict, agent_name: str) -> dict | None:
    """从 agent 返回的 markdown 中提取 JSON。容错处理。"""
    if not result.get("ok"):
        log.warning(f"[{agent_name}] 调用失败: {result.get('error')}")
        return None
    raw = result.get("markdown", "")

    # 去掉 markdown 代码块围栏
    cleaned = re.sub(r"```(?:json)?\s*", "", raw).replace("```", "")
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < 0:
        log.warning(f"[{agent_name}] 输出不含 JSON")
        return None
    json_text = cleaned[start:end+1]

    # 修复常见 JSON 瑕疵
    json_text = re.sub(r":\s*\+(\d)", r": \1", json_text)   # +5 → 5
    json_text = re.sub(r",(\s*[}\]])", r"\1", json_text)     # 尾随逗号

    try:
        return json.loads(json_text)
    except Exception as e:
        log.warning(f"[{agent_name}] JSON 解析失败: {e}")
        # 尝试更激进的修复
        try:
            # 替换单引号
            json_text2 = json_text.replace("'", '"')
            return json.loads(json_text2)
        except Exception:
            return None


def run_research(project: str, token: str = "",
                 context: str = "", parallel: bool = True) -> dict:
    """运行完整的 multi-agent 研究流程。

    Args:
        project: 项目名称 (如 "TON", "Solana")
        token: 代币符号 (如 "TON", "SOL"), 默认同 project
        context: 额外背景信息
        parallel: 是否并行跑前 3 个 agent

    Returns:
        dict 包含所有 agent 结果 + 最终 alpha 分析
    """
    token = token or project
    start_time = datetime.now(timezone.utc)
    log.info(f"{'='*60}")
    log.info(f"开始 Multi-Agent 研究: {project} ({token})")
    log.info(f"{'='*60}")

    results = {
        "project": project,
        "token": token,
        "started_at": start_time.isoformat(),
        "agents": {},
    }

    # ── Phase 1: 并行跑 GitHub / Onchain / Narrative ──
    phase1_agents = {
        "github": lambda: run_github_agent(project, context),
        "onchain": lambda: run_onchain_agent(project, token, context),
        "narrative": lambda: run_narrative_agent(project, context),
    }

    if parallel:
        log.info("Phase 1: 并行启动 3 个 Agent...")
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {
                executor.submit(fn): name
                for name, fn in phase1_agents.items()
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    raw = future.result()
                    parsed = _parse_agent_json(raw, name)
                    results["agents"][name] = parsed or {"error": "parse_failed",
                                                          "raw": raw.get("markdown", "")[:500]}
                    status = "OK" if parsed else "PARSE_FAIL"
                    log.info(f"  [{name}] {status}")
                except Exception as e:
                    results["agents"][name] = {"error": str(e)}
                    log.error(f"  [{name}] EXCEPTION: {e}")
    else:
        log.info("Phase 1: 串行跑 3 个 Agent...")
        for name, fn in phase1_agents.items():
            log.info(f"  运行 {name} Agent...")
            raw = fn()
            parsed = _parse_agent_json(raw, name)
            results["agents"][name] = parsed or {"error": "parse_failed",
                                                  "raw": raw.get("markdown", "")[:500]}

    # ── Phase 2: Risk Agent (需要 Phase 1 数据) ──
    log.info("Phase 2: Risk Agent...")
    phase1_summary = json.dumps(
        {k: v for k, v in results["agents"].items() if k != "risk"},
        ensure_ascii=False, indent=2, default=str
    )
    # 截断以防超长
    if len(phase1_summary) > 8000:
        phase1_summary = phase1_summary[:8000] + "\n...(truncated)"

    risk_raw = run_risk_agent(project, phase1_summary)
    risk_data = _parse_agent_json(risk_raw, "risk")
    results["agents"]["risk"] = risk_data or {"error": "parse_failed"}
    log.info(f"  [risk] {'OK' if risk_data else 'PARSE_FAIL'}")

    # ── Phase 3: Alpha 综合 Agent (需要全部数据) ──
    log.info("Phase 3: Alpha 综合 Agent...")
    all_summary = json.dumps(results["agents"], ensure_ascii=False, indent=2, default=str)
    if len(all_summary) > 12000:
        all_summary = all_summary[:12000] + "\n...(truncated)"

    alpha_raw = run_alpha_agent(project, all_summary)
    alpha_data = _parse_agent_json(alpha_raw, "alpha")
    results["agents"]["alpha"] = alpha_data or {"error": "parse_failed"}
    log.info(f"  [alpha] {'OK' if alpha_data else 'PARSE_FAIL'}")

    # ── 汇总 ──
    end_time = datetime.now(timezone.utc)
    results["completed_at"] = end_time.isoformat()
    results["duration_seconds"] = (end_time - start_time).total_seconds()

    # 统计成功率
    ok_count = sum(1 for v in results["agents"].values()
                   if isinstance(v, dict) and "error" not in v)
    results["agents_ok"] = ok_count
    results["agents_total"] = len(results["agents"])

    log.info(f"{'='*60}")
    log.info(f"研究完成: {ok_count}/{len(results['agents'])} agents 成功, "
             f"耗时 {results['duration_seconds']:.0f}s")
    log.info(f"{'='*60}")

    return results
