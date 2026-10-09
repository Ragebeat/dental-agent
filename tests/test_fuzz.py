import random
from datetime import timedelta

import pytest

from dental.core import errors as E
from dental.core.db import connect
from helpers import CAL, NOW, TZ, assert_invariants

OPS = 300


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_random_operations_keep_invariants(core, db_url, seed):
    rnd = random.Random(seed)
    with connect(db_url) as c:
        pats = [r["id"] for r in c.execute("SELECT id FROM patients ORDER BY id LIMIT 40").fetchall()]
    services = list(core.clinic.services)
    d0 = NOW.date()
    stats = {"ok": 0, "err": 0}
    for i in range(OPS):
        p = rnd.choice(pats)
        op = rnd.choice(["book", "book", "book", "cancel", "reschedule"])
        try:
            if op == "book":
                code = rnd.choice(services)
                day = d0 + timedelta(days=rnd.randint(0, 70))
                starts = CAL.valid_starts(day, core.clinic.services[code].duration_slots)
                if not starts:
                    continue
                core.book(p, code, rnd.choice(starts), source="web", actor="patient_web", now=NOW,
                          chair_id=rnd.choice([None, None, 1, 4]))
            else:
                act = core.patient_appointments(p, now=NOW)
                act = [a for a in act if a.status == "active"]
                if not act:
                    continue
                a = rnd.choice(act)
                if op == "cancel":
                    core.cancel(p, a.id, actor="patient_web", now=NOW)
                else:
                    day = d0 + timedelta(days=rnd.randint(0, 40))
                    starts = CAL.valid_starts(day, core.clinic.services[a.service_code].duration_slots)
                    if starts:
                        core.reschedule(p, a.id, rnd.choice(starts), actor="patient_web", now=NOW)
            stats["ok"] += 1
        except E.DentalError:
            stats["err"] += 1
        if i % 50 == 49:
            assert_invariants(db_url)
    assert_invariants(db_url)
    assert stats["ok"] > 20
