"""Eval сводки владельцу (E5): в чистую БД закладываются известные события, сводка пишется N раз, считаются
recall заложенных фактов и число выдуманных — по ПЕРВОЙ попытке модели (повторы и запасной шаблон скрывают ошибки модели).

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
    """Закладывает события после закрытия предыдущего рабочего дня; возвращает ожидаемые ссылки, посчитанные независимо от collect()."""
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
    kenshin = _first(core, "kenshin", now, ev, last=True)       # другой день/время, чем у чистки: пациент не может быть в двух местах
    c = core.book(pid, "kenshin", kenshin, source="web", actor="patient_web", now=ev)
    core.cancel(pid, c.id, actor="patient_web", now=ev)
    refs.append(f"C{c.id}")
    h1 = core.create_handoff(pid, "ambiguity", "要望が不明確", now=ev)
    h2 = core.create_handoff(None, "medical", "痛みの相談", now=now - timedelta(hours=30))
    refs += [f"H{h1}", f"H{h2}"]
    return refs


def _quiet_day(core: ClinicCore, now: datetime) -> None:
    """Снимок синтетических данных создан «вчера-сегодня» и сам попадает в окно 24 ч. Сдвигаем его в прошлое:
    у теста «тихий день», в окне остаются только заложенные события."""
    since = now - timedelta(hours=24)
    with core._connect() as conn, conn.transaction():
        for table, col in (("appointments", "created_at"), ("appointments", "cancelled_at"),
                           ("patients", "created_at"), ("handoffs", "created_at")):
            conn.execute(f"UPDATE {table} SET {col} = {col} - interval '3 days' WHERE {col} > %s", [since])


def _first(core: ClinicCore, code: str, now: datetime, ev: datetime, skip: int = 0, last: bool = False) -> datetime:
    """Первый свободный слот услуги в ближайшие дни (после завтрашнего: завтра — план, а не новые записи)."""
    d1 = (now + timedelta(days=3)).date()
    slots = core.find_slots(code, d1, d1 + timedelta(days=11), now=ev)
    if len(slots) <= skip:
        raise RuntimeError(f"нет свободных слотов {code} для заложенного события")
    return slots[-1] if last else slots[skip]


def _fixture_id(core: ClinicCore, card: int) -> int:
    with core._connect() as conn:
        return conn.execute("SELECT id FROM patients WHERE card_number = %s", [card]).fetchone()["id"]


def evaluate(llm, repeats: int, cfg=None) -> dict:
    cfg = cfg or load_config()
    now = cfg.now().replace(hour=8, minute=30)      # утренняя сводка
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
                print(f"  {lang} #{len([r for r in rows if r['lang'] == lang]) + 1}: {s.source}, попыток {s.attempts}, "
                      f"1-я попытка recall {first.recall:.0%}, выдуманных {first.fabricated}", flush=True)
                if not first.ok:
                    print(f"     первая попытка: пропущено {first.missing[:6]}, лишние ссылки {first.unknown_refs}, "
                          f"лишние числа {first.bad_numbers}, общее число {'есть' if first.total_ok else 'нет'}\n     finish={s.first_finish} текст: {s.first_text[:300]!r}", flush=True)
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
    print("записано:", path)


if __name__ == "__main__":
    main()
