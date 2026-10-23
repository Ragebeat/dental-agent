"""Eval of the owner summary (E5): known events are planted in a clean database, the summary is written N times, and
recall of the planted facts and the number of fabricated ones are counted on the model's FIRST attempt (retries and the fallback template hide model errors).

  uv run python -m dental.eval.summary_eval --profile local --repeats 3
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from statistics import mean

from dental.agent.llm import get_llm
from dental.config import ROOT, load_config
from dental.core.db import database_url, with_dbname
from dental.core.loader import build_template, create_database, drop_database, unique_name
from dental.core.service import ClinicCore
from dental.summary.facts import Facts, collect
from dental.summary.text import verify
from dental.summary.writer import write
from dental.synth.generate import build_snapshot


def plant(core: ClinicCore, now: datetime) -> list[str]:
    """Plants events after the previous working day has closed; returns the expected references, computed independently of collect()."""
    ev = now - timedelta(hours=3)
    refs: list[str] = []
    _quiet_day(core, now)
    p1 = core.register_patient(name="新規 花子", name_kana="しんき はなこ", phone="090-1111-2222", birth_date=datetime(1995, 3, 3).date(), now=ev)
    p2 = core.register_patient(name="新規 次郎", name_kana="しんき じろう", phone="090-3333-4444", birth_date=datetime(1990, 7, 7).date(), now=ev)
    refs += [f"N{p1.card_number}", f"N{p2.card_number}"]
    start = _first(core, "shoshin", now, ev, skip=0)
    a = core.book(p1.id, "shoshin", start, source="web", actor="patient_web", now=ev)
    refs.append(f"B{a.id}")
    clean = _first(core, "cleaning", now, ev, skip=0)
    pid = _fixture_id(core, 100001)
    b = core.book(pid, "cleaning", clean, source="line", actor="patient_agent", now=ev)
    refs.append(f"B{b.id}")
    kenshin = _first(core, "kenshin", now, ev, last=True)       # a different day/time than the cleaning: a patient cannot be in two places
    c = core.book(pid, "kenshin", kenshin, source="web", actor="patient_web", now=ev)
    core.cancel(pid, c.id, actor="patient_web", now=ev)
    refs.append(f"C{c.id}")
    h1 = core.create_handoff(pid, "ambiguity", "要望が不明確", now=ev)
    h2 = core.create_handoff(None, "medical", "痛みの相談", now=now - timedelta(hours=30))
    refs += [f"H{h1}", f"H{h2}"]
    return refs


def _quiet_day(core: ClinicCore, now: datetime) -> None:
    """The synthetic snapshot was created "yesterday-today" and itself falls into the 24 h window. It is shifted into the past:
    the test gets a "quiet day", and only the planted events remain in the window."""
    since = now - timedelta(hours=24)
    with core._connect() as conn, conn.transaction():
        for table, col in (("appointments", "created_at"), ("appointments", "cancelled_at"),
                           ("patients", "created_at"), ("handoffs", "created_at")):
            conn.execute(f"UPDATE {table} SET {col} = {col} - interval '3 days' WHERE {col} > %s", [since])


def _first(core: ClinicCore, code: str, now: datetime, ev: datetime, skip: int = 0, last: bool = False) -> datetime:
    """The first free slot of a service in the coming days (after tomorrow: tomorrow is the plan, not new bookings)."""
    d1 = (now + timedelta(days=3)).date()
    slots = core.find_slots(code, d1, d1 + timedelta(days=11), now=ev)
    if len(slots) <= skip:
        raise RuntimeError(f"no free {code} slots for the planted event")
    return slots[-1] if last else slots[skip]


def _fixture_id(core: ClinicCore, card: int) -> int:
    with core._connect() as conn:
        return conn.execute("SELECT id FROM patients WHERE card_number = %s", [card]).fetchone()["id"]


def evaluate(llm, repeats: int, cfg=None) -> dict:
    cfg = cfg or load_config()
    now = cfg.now().replace(hour=8, minute=30)      # the morning summary
    admin = database_url()
    tpl, name = unique_name("dental_sumtpl"), unique_name("dental_sum")
    build_template(admin, tpl, build_snapshot(cfg))
    create_database(admin, name, template=tpl)
    try:
        core = ClinicCore(with_dbname(admin, name))
        planted = plant(core, now)
        f: Facts = collect(core, now)
        assert sorted(f.must_refs()) == sorted(planted), (sorted(f.must_refs()), sorted(planted))
        rows = []
        for lang in ("ja", "ru"):
            for _ in range(repeats):
                s = write(f, lang, llm)
                first = s.first or verify("", f)
                print(f"  {lang} #{len([r for r in rows if r['lang'] == lang]) + 1}: {s.source}, attempts {s.attempts}, "
                      f"1st attempt recall {first.recall:.0%}, fabricated {first.fabricated}", flush=True)
                if not first.ok:
                    print(f"     first attempt: missing {first.missing[:6]}, unknown refs {first.unknown_refs}, "
                          f"wrong numbers {first.bad_numbers}, total count {'present' if first.total_ok else 'absent'}\n     finish={s.first_finish} text: {s.first_text[:300]!r}", flush=True)
                rows.append({"lang": lang, "source": s.source, "attempts": s.attempts, "first_recall": first.recall,
                             "first_fabricated": first.fabricated, "first_ok": first.ok, "first_text": s.first_text if not first.ok else "", "final_ok": s.check.ok})
        out = {"planted": len(planted), "runs": len(rows), "first_ok_rate": mean(r["first_ok"] for r in rows),
               "first_recall_mean": mean(r["first_recall"] for r in rows),
               "first_fabricated_total": sum(r["first_fabricated"] for r in rows),
               "fallback_rate": mean(r["source"] == "template" for r in rows),
               "by_lang": {lang: mean(r["first_ok"] for r in rows if r["lang"] == lang) for lang in ("ja", "ru")}}
        return out
    finally:
        drop_database(admin, name)
        drop_database(admin, tpl)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="agent")
    ap.add_argument("--repeats", type=int, default=3)
    a = ap.parse_args(argv)
    llm = get_llm(a.profile)
    res = evaluate(llm, a.repeats)
    res |= {"profile": a.profile, "model": llm.profile.model}
    for k, v in res.items():
        print(f"{k:24} {v}")
    path = ROOT / "data" / "summary_eval.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(res, ensure_ascii=False) + "\n")
    print("written:", path)


if __name__ == "__main__":
    main()
