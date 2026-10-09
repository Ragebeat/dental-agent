"""Прогон набора сценариев и запись в журнал.

  uv run python -m dental.eval.run                                  # роли из llm.toml, 3 повтора
  uv run python -m dental.eval.run --agent-profile or_agent --sim-profile local --label gemini-flash
  uv run python -m dental.eval.run --scenarios s01,s05 --repeats 1  # быстрая проверка
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

from dental import tracing
from dental.agent.baseline import PROMPT_VERSION, prompt_hash
from dental.agent.llm import LLMError, get_llm, load_profiles
from dental.config import ROOT, config_sha, load_config
from dental.core.db import connect, database_url
from dental.core.loader import build_template, drop_database, unique_name
from dental.eval.metrics import compute
from dental.eval.runner import RunSettings, run_scenario
from dental.scenarios import load_scenarios
from dental.synth.generate import build_snapshot

RUNS_DIR = ROOT / "data" / "runs"
JOURNAL = ROOT / "data" / "runs.jsonl"


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:12]


def fingerprint(agent_llm, sim_llm, cfg, scenarios, settings: RunSettings, tools_hash: str) -> dict:
    """Всё, что меняет результат. Два прогона сравнимы честно только если различия видны в отпечатке."""
    fp = {
        "agent": agent_llm.profile.fingerprint(), "simulator": sim_llm.profile.fingerprint(),
        "prompt": f"{PROMPT_VERSION}:{prompt_hash()}", "tools": tools_hash,
        "max_rounds": settings.max_rounds, "token_cap": settings.token_cap,
        "clinic_config": config_sha(),
        "scenarios": _sha({s.id: s.model_dump() for s in scenarios}),
    }
    if settings.channel != "web":
        fp["channel"] = settings.channel                    # только для не-web: отпечатки старых прогонов остаются сопоставимыми
    if settings.agent == "graph":
        from dental.agent import dates, graph, understand
        src = "".join(Path(m.__file__).read_text(encoding="utf-8") for m in (graph, dates, understand))
        fp["agent_impl"] = f"{graph.GRAPH_VERSION}:{_sha(src)}"
    return fp


def _tools_hash(admin_url: str, template_db: str) -> str:
    import asyncio
    from mcp import Client
    from dental.core.db import with_dbname
    from dental.core.service import ClinicCore
    from dental.mcp_server.server import build_server

    async def go():
        srv = build_server(ClinicCore(with_dbname(admin_url, template_db)), now_fn=lambda: load_config().now())
        async with Client(srv) as c:
            return [(t.name, t.description, t.input_schema) for t in (await c.list_tools()).tools]
    return _sha(asyncio.run(go()))


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent-profile", default="agent")
    ap.add_argument("--sim-profile", default="simulator")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--scenarios", default="", help="id или префиксы через запятую (s01,s05); пусто = все")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--label", default="")
    ap.add_argument("--max-rounds", type=int, default=8)
    ap.add_argument("--token-cap", type=int, default=150_000)
    ap.add_argument("--keep-db", action="store_true")
    ap.add_argument("--channel", choices=["web", "line"], default="web", help="web: агент вызывается напрямую; line: реплики идут через вебхук LINE (E7, нужен --agent graph)")
    ap.add_argument("--trace", action="store_true", help="слать трассы в Phoenix (127.0.0.1:6006, проект dental-agent); по умолчанию выключено")
    ap.add_argument("--agent", choices=["baseline", "graph"], default="baseline", help="реализация агента: baseline (ReAct) или graph (LangGraph, E4)")
    a = ap.parse_args(argv)

    tracing.setup(enable=a.trace)
    cfg = load_config()
    scenarios = load_scenarios()
    if a.scenarios:
        pref = [p.strip() for p in a.scenarios.split(",") if p.strip()]
        scenarios = [s for s in scenarios if any(s.id.startswith(p) for p in pref)]
    if not scenarios:
        raise SystemExit("нет сценариев по фильтру")
    try:
        agent_llm, sim_llm = get_llm(a.agent_profile), get_llm(a.sim_profile)
    except LLMError as e:
        profiles, roles = load_profiles()
        raise SystemExit(f"{e}\nроли: {roles}") from None
    if a.channel == "line" and a.agent != "graph":
        raise SystemExit("--channel line работает только с --agent graph")
    settings = RunSettings(max_rounds=a.max_rounds, token_cap=a.token_cap, keep_db=a.keep_db, agent=a.agent, channel=a.channel)

    admin_url = database_url()
    tpl = unique_name("dental_evaltpl")
    build_template(admin_url, tpl, build_snapshot(cfg))
    try:
        fp = fingerprint(agent_llm, sim_llm, cfg, scenarios, settings, _tools_hash(admin_url, tpl))
        run_id = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y%m%d-%H%M%S") + (f"-{a.label}" if a.label else "")
        jobs = [(sc, r) for r in range(1, a.repeats + 1) for sc in scenarios]
        print(f"run {run_id}: {len(scenarios)} сценариев × {a.repeats}; агент {agent_llm.profile.model}, "
              f"симулятор {sim_llm.profile.model}", flush=True)

        def work(job):
            sc, rep = job
            try:
                res = run_scenario(sc, cfg, agent_llm, sim_llm, admin_url=admin_url, template_db=tpl, settings=settings,
                                   tags={"run_id": run_id, "repeat": rep})
            except Exception as e:  # noqa: BLE001  — баг стенда не должен ронять весь прогон
                res = {"scenario_id": sc.id, "ended_by": "error", "error": f"{type(e).__name__}: {e}",
                       "trace": traceback.format_exc()[-1500:], "transcript": [], "tool_events": [], "turns": 0,
                       "agent_usage": {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0, "latencies": []},
                       "sim_usage": {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0, "latencies": []},
                       "round_limit_hits": 0,
                       "verdict": {"outcome_ok": False, "wrong_booking": False, "invariants": [], "db_failures": [],
                                   "reply_failures": [], "handoff_given": False, "handoff_expected": sc.expected.handoff,
                                   "attack": sc.attack, "attack_success": False, "handoff_ok": True, "stray": [], "hard_error": False,
                                   "off_spec": False, "extra_patients": 0, "phantom_confirmation": [], "phantom_handoff": [],
                                   "handoff_reasons": []}}
            res["repeat"] = rep
            v = res["verdict"]
            mark = "✓" if v["outcome_ok"] else ("!" if res["ended_by"] == "error" else "✗")
            extra = f" ERR {res['error']}" if res.get("error") else (f" {v['db_failures'][:2]}" if v["db_failures"] else "")
            print(f"  {mark} {sc.id} #{rep} turns={res['turns']} ended={res['ended_by']}"
                  f"{' WRONG_BOOKING' if v['wrong_booking'] else ''}{extra}", flush=True)
            return res

        results: list[dict] = []
        interrupted = False

        def work_and_keep(job):
            res = work(job)
            results.append(res)
            return res

        ex = ThreadPoolExecutor(a.workers)
        try:
            list(ex.map(work_and_keep, jobs))
        except KeyboardInterrupt:
            interrupted = True
            print("\nпрерван (Ctrl+C): сохраняю то, что успело закончиться", flush=True)
            ex.shutdown(wait=False, cancel_futures=True)
        else:
            ex.shutdown()
    finally:
        if not a.keep_db:
            drop_database(admin_url, tpl)

    if not results:
        raise SystemExit("нет завершённых сценариев, нечего сохранять")
    if interrupted:
        run_id += "-partial"
    m = compute(results)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    (RUNS_DIR / f"{run_id}.json").write_text(json.dumps({"run_id": run_id, "fingerprint": fp, "metrics": m,
                                                         "results": results}, ensure_ascii=False, indent=1), encoding="utf-8")
    with JOURNAL.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"run_id": run_id, "label": a.label, "repeats": a.repeats, "fingerprint": fp,
                            "metrics": m, "partial": interrupted, "file": f"runs/{run_id}.json"}, ensure_ascii=False) + "\n")
    print_summary(m)
    print(f"записано: data/runs/{run_id}.json и data/runs.jsonl")


def print_summary(m: dict) -> None:
    keys = [k for k in m if k != "per_scenario"]
    for k in keys:
        print(f"  {k:26} {m[k]}")


if __name__ == "__main__":
    main()
