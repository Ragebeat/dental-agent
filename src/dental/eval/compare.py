"""Comparing two runs: metrics side by side, then per-scenario regressions (an average hides what broke).

  uv run python -m dental.eval.compare              # the last two in the log
  uv run python -m dental.eval.compare -3 -1        # by index in the log
  uv run python -m dental.eval.compare a.json b.json
  uv run python -m dental.eval.compare --list
A = the old run, B = the new one. "Regression" = worse in B.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from dental.eval.metrics import compute
from dental.eval.run import JOURNAL, RUNS_DIR

LOWER_BETTER = {"wrong_booking_runs", "off_spec_runs", "extra_patient_runs", "phantom_confirmation_runs",
                "phantom_handoff_runs", "duplicate_runs", "attack_success", "handoff_false_positives", "infra_errors",
                "turns_median", "turns_median_passed", "agent_tokens_mean", "sim_tokens_mean", "cost_total",
                "agent_latency_p50_s", "agent_latency_p95_s", "round_limit_hits"}
HIGHER_BETTER = {"outcome_rate", "handoff_recall", "handoff_precision"}


def journal() -> list[dict]:
    if not JOURNAL.exists():
        return []
    return [json.loads(x) for x in JOURNAL.read_text(encoding="utf-8").splitlines() if x.strip()]


def load(ref: str, rows: list[dict]) -> dict:
    p = Path(ref)
    if p.suffix == ".json" and p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    if ref.lstrip("-").isdigit():
        row = rows[int(ref)]
        return json.loads((JOURNAL.parent / row["file"]).read_text(encoding="utf-8"))
    raise SystemExit(f"run not found: {ref}")


def main(argv=None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    rows = journal()
    if "--list" in argv:
        for i, r in enumerate(rows):
            m = r["metrics"]
            print(f"{i - len(rows):>3} {r['run_id']:28} {r['fingerprint']['agent']['model'][:28]:28} "
                  f"outcome={m['outcome_rate']} wrong={m['wrong_booking_runs']} attack={m['attack_success']}")
        return
    refs = argv or ["-2", "-1"]
    if len(refs) != 2 or (len(rows) < 2 and not all(r.endswith(".json") for r in refs)):
        raise SystemExit("two runs are needed (fewer than two in the log, or not exactly two given)")
    a, b = load(refs[0], rows), load(refs[1], rows)
    ma, mb = compute(a["results"]), compute(b["results"])      # recomputed from the results: one definition of every metric
    print(f"A: {a['run_id']}   B: {b['run_id']}")
    diff = {k: (a["fingerprint"].get(k), b["fingerprint"].get(k)) for k in b["fingerprint"]
            if a["fingerprint"].get(k) != b["fingerprint"].get(k)}
    print("configuration differences:", "none" if not diff else "")
    for k, (x, y) in diff.items():
        print(f"  {k}: {x} → {y}")
    print(f"\n{'metric':26} {'A':>12} {'B':>12}")
    for k in ma:
        if k == "per_scenario":
            continue
        x, y = ma[k], mb[k]
        flag = ""
        if x is not None and y is not None and x != y:
            better = (y < x) if k in LOWER_BETTER else (y > x) if k in HIGHER_BETTER else None
            flag = "" if better is None else ("  better" if better else "  WORSE")
        print(f"{k:26} {str(x):>12} {str(y):>12}{flag}")
    regress, improve = [], []
    for sid in sorted(set(ma["per_scenario"]) | set(mb["per_scenario"])):
        pa, pb = ma["per_scenario"].get(sid, [0, 0]), mb["per_scenario"].get(sid, [0, 0])
        ra, rb = (pa[0] / pa[1] if pa[1] else None), (pb[0] / pb[1] if pb[1] else None)
        if ra is None or rb is None or ra == rb:
            continue
        (regress if rb < ra else improve).append(f"  {sid}: {pa[0]}/{pa[1]} → {pb[0]}/{pb[1]}")
    print("\nper-scenario regressions (B worse than A):" + ("" if regress else " none"))
    print("\n".join(regress))
    print("improvements:" + ("" if improve else " none"))
    print("\n".join(improve))


if __name__ == "__main__":
    main()
