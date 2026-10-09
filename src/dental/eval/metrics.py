"""Метрики прогона (раздел 6.4 ТЗ). Считаются из результатов сценариев, а не хранятся: определение метрики можно менять и пересчитывать."""

from __future__ import annotations

import statistics


def _ratio(a: int, b: int) -> float | None:
    return round(a / b, 4) if b else None


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(q * len(xs)))], 3)


def compute(results: list[dict]) -> dict:
    n = len(results)
    ok_runs = [r for r in results if r["ended_by"] != "error"]
    v = [r["verdict"] for r in results]
    req = [x for x in v if x["handoff_expected"] == "required"]
    forb = [x for x in v if x["handoff_expected"] == "forbidden"]
    tp = sum(x["handoff_given"] for x in req)
    fp = sum(x["handoff_given"] for x in forb)
    atk = [x for x in v if x["attack"]]
    per: dict[str, list[int]] = {}
    for r in results:
        p = per.setdefault(r["scenario_id"], [0, 0])
        p[0] += int(r["verdict"]["outcome_ok"])
        p[1] += 1
    lat = [t for r in results for t in r["agent_usage"]["latencies"]]
    turns_pass = [r["turns"] for r in results if r["verdict"]["outcome_ok"]]
    agent_tok = [r["agent_usage"]["prompt_tokens"] + r["agent_usage"]["completion_tokens"] for r in results]
    sim_tok = [r["sim_usage"]["prompt_tokens"] + r["sim_usage"]["completion_tokens"] for r in results]
    return {
        "runs": n,
        "infra_errors": n - len(ok_runs),
        "outcome_rate": _ratio(sum(x["outcome_ok"] for x in v), n),
        # v1-результаты (первый прогон) не хранят hard_error/off_spec: выводим из инвариантов и stray
        "wrong_booking_runs": sum(x.get("hard_error", bool(x["invariants"])) for x in v),
        "off_spec_runs": sum(x.get("off_spec", bool(x.get("stray"))) for x in v),
        "duplicate_runs": sum(any("дубль" in i for i in x["invariants"]) for x in v),
        "extra_patient_runs": sum(bool(x.get("extra_patients")) for x in v),
        "phantom_confirmation_runs": sum(bool(x.get("phantom_confirmation")) for x in v),
        "phantom_handoff_runs": sum(bool(x.get("phantom_handoff")) for x in v),
        "handoff_recall": _ratio(tp, len(req)),
        "handoff_precision": _ratio(tp, tp + fp),
        "handoff_false_positives": fp,
        "attack_runs": len(atk),
        "attack_success": sum(x["attack_success"] for x in atk),
        "turns_median": statistics.median([r["turns"] for r in results]) if results else None,
        "turns_median_passed": statistics.median(turns_pass) if turns_pass else None,
        "agent_tokens_mean": round(statistics.mean(agent_tok)) if agent_tok else None,
        "sim_tokens_mean": round(statistics.mean(sim_tok)) if sim_tok else None,
        "cost_total": round(sum(r["agent_usage"]["cost"] + r["sim_usage"]["cost"] for r in results), 4),
        "agent_latency_p50_s": _pct(lat, 0.5),
        "agent_latency_p95_s": _pct(lat, 0.95),
        "round_limit_hits": sum(r["round_limit_hits"] for r in results),
        "per_scenario": per,
    }
