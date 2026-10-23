"""Running one scenario: a clean database from the template → agent ↔ simulator dialogue over MCP → checks in code."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from mcp import Client

from dental import tracing
from dental.agent.baseline import BaselineAgent, TokenCapReached, openai_tools
from dental.agent.llm import LLM, LLMError, ManualLLM
from dental.config import ClinicConfig
from dental.core.db import connect, with_dbname
from dental.core.loader import create_database, drop_database, unique_name
from dental.core.service import ClinicCore
from dental.eval.checks import evaluate, take_baseline
from dental.eval.manual import ManualSimulator
from dental.eval.simulator import Simulator
from dental.mcp_server.server import build_server
from dental.scenarios import Scenario


@dataclass
class RunSettings:
    max_rounds: int = 8
    token_cap: int = 150_000
    keep_db: bool = False
    agent: str = "baseline"            # baseline | graph
    channel: str = "web"               # web (direct agent call) | line (through the LINE webhook and session gateway, E7)


async def _dialog(sc: Scenario, core: ClinicCore, cfg: ClinicConfig, agent_llm: LLM, sim_llm: LLM,
                  st: RunSettings) -> dict:
    now = cfg.now()
    srv = build_server(core, now_fn=lambda: now, channel="web")
    sim = ManualSimulator(sc, now) if isinstance(sim_llm, ManualLLM) else Simulator(sim_llm, sc, now)
    transcript: list[dict] = []
    ended_by, error = "max_turns", None
    async with Client(srv) as client:
        tools = openai_tools((await client.list_tools()).tools)
        if st.channel == "line":
            from dental.channels.eval_bridge import LineAgent
            agent = LineAgent(core, agent_llm, now, st)
        elif st.agent == "graph":
            from dental.agent.graph import GraphAgent
            agent = GraphAgent(agent_llm, client, tools, now, max_rounds=st.max_rounds, token_cap=st.token_cap)
        else:
            agent = BaselineAgent(agent_llm, client, tools, now, max_rounds=st.max_rounds, token_cap=st.token_cap)
        try:
            text, ended = sim.first()
            for _ in range(sc.max_turns):
                if text:
                    transcript.append({"role": "patient", "text": text})
                if ended and not text:
                    ended_by = "patient_end"
                    break
                reply = await agent.reply(text)          # the last message with <END> also goes to the agent («はい» = a confirmation)
                transcript.append({"role": "agent", "text": reply})
                if ended:
                    ended_by = "patient_end"
                    break
                text, ended = sim.respond(reply)
        except TokenCapReached:
            ended_by = "token_cap"
        except LLMError as e:
            ended_by, error = "error", str(e)[:300]
        finally:
            if hasattr(agent, "aclose"):
                await agent.aclose()
    return {"transcript": transcript, "tool_events": agent.events, "ended_by": ended_by, "error": error,
            "agent_usage": agent.usage.as_dict(), "sim_usage": sim.usage.as_dict(),
            "round_limit_hits": agent.round_limit_hits,
            "turns": sum(1 for m in transcript if m["role"] == "patient")}


def run_scenario(sc: Scenario, cfg: ClinicConfig, agent_llm: LLM, sim_llm: LLM, *, admin_url: str, template_db: str,
                 settings: RunSettings | None = None, tags: dict | None = None) -> dict:
    """One dialogue = one trace: a root `dialog` span with the run tags and the verdict (Phoenix filters failures by them)."""
    st = settings or RunSettings()
    attrs = {"eval.scenario": sc.id, "eval.agent": st.agent, "eval.agent_model": agent_llm.profile.model,
             **{f"eval.{k}": v for k, v in (tags or {}).items()}}
    with tracing.span(f"dialog {sc.id}", "AGENT", attrs=attrs) as sp:
        res = _run_scenario(sc, cfg, agent_llm, sim_llm, admin_url, template_db, st)
        v = res["verdict"]
        tracing.set_attrs(sp, {"eval.outcome_ok": v["outcome_ok"], "eval.wrong_booking": v["wrong_booking"],
                               "eval.ended_by": res["ended_by"], "eval.turns": res["turns"],
                               "eval.off_spec": v["off_spec"], "eval.handoff_given": v["handoff_given"],
                               "eval.db_failures": len(v["db_failures"]), "eval.agent_tokens": res["agent_usage"].get("prompt_tokens", 0) + res["agent_usage"].get("completion_tokens", 0)})
        return res


def _run_scenario(sc: Scenario, cfg: ClinicConfig, agent_llm: LLM, sim_llm: LLM, admin_url: str, template_db: str,
                  st: RunSettings) -> dict:
    name = unique_name("dental_run")
    create_database(admin_url, name, template=template_db)
    url = with_dbname(admin_url, name)
    try:
        core = ClinicCore(url)
        with connect(url) as c:
            baseline = take_baseline(c)
        res = asyncio.run(_dialog(sc, core, cfg, agent_llm, sim_llm, st))
        with connect(url) as c:
            verdict = evaluate(c, sc, cfg, cfg.now(), baseline,
                               [m["text"] for m in res["transcript"] if m["role"] == "agent"], res["tool_events"])
        res["verdict"] = verdict.as_dict()
        res["scenario_id"] = sc.id
        return res
    finally:
        if not st.keep_db:
            drop_database(admin_url, name)
