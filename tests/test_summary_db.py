"""Сводка на живой БД: collect() находит ровно заложенные события; имена не попадают в представление для LLM."""
from dental.eval.summary_eval import plant
from dental.summary.facts import collect
from dental.summary.text import template, verify
from helpers import CFG, NOW


def test_collect_finds_exactly_the_planted_events(core):
    planted = plant(core, NOW)
    f = collect(core, NOW)
    assert sorted(f.must_refs()) == sorted(planted)
    assert f.today_total == len(f.today_appts) and all(a["start"][:10] == f.today.isoformat() for a in f.today_appts)
    assert any(h["stale"] for h in f.handoffs) and any(not h["stale"] for h in f.handoffs)
    for lang in ("ja", "ru"):
        assert verify(template(f, lang), f).ok
    s = str(f.llm_view())
    assert all(p["name"] not in s for p in f.new_patients) and all(a["name"] not in s for a in f.today_appts)
