from datetime import date, timedelta

import pytest

from dental.core import errors as E
from helpers import NOW, PID

BORN = date(1988, 4, 12)    # P_CLEAN, card 100001


def v(core, card, born, client, i=0, now=None):
    return core.verify_patient(card, born, channel="web", client_key=client, now=(now or NOW) + timedelta(seconds=i))


def test_verify_ok(core):
    p = v(core, 100001, BORN, "c1")
    assert p.id == PID["P_CLEAN"] and p.name == "鈴木 健太"


def test_wrong_date_and_unknown_card_look_the_same(core):
    with pytest.raises(E.PatientNotVerified) as a:
        v(core, 100001, date(1990, 1, 1), "c1")
    with pytest.raises(E.PatientNotVerified) as b:
        v(core, 999999, BORN, "c2")
    assert a.value.message == b.value.message


def test_lockout_after_5_failures_even_with_correct_date(core):
    for i in range(5):
        with pytest.raises(E.PatientNotVerified):
            v(core, 100001, date(1990, 1, 1 + i), "c1", i)
    with pytest.raises(E.Locked) as e:
        v(core, 100001, BORN, "c1", 6)
    assert e.value.details["retry_after"] == NOW + timedelta(seconds=4, minutes=15)
    assert v(core, 100001, BORN, "c1", now=NOW + timedelta(minutes=16)).id == PID["P_CLEAN"]


def test_success_resets_the_streak(core):
    for i in range(4):
        with pytest.raises(E.PatientNotVerified):
            v(core, 100001, date(1990, 1, 1 + i), "c1", i)
    v(core, 100001, BORN, "c1", 10)
    for i in range(4):
        with pytest.raises(E.PatientNotVerified):
            v(core, 100001, date(1991, 1, 1 + i), "c1", 20 + i)
    assert v(core, 100001, BORN, "c1", 30).id == PID["P_CLEAN"]


def test_client_lockout_across_different_cards(core):
    for i in range(5):
        with pytest.raises(E.PatientNotVerified):
            v(core, 100010 + i, BORN, "attacker", i)       # different cards tried by one client
    with pytest.raises(E.Locked):
        v(core, 100001, BORN, "attacker", 9)               # even the correct pair is closed for this client
    assert v(core, 100001, BORN, "honest", 10).id == PID["P_CLEAN"]


def test_card_lockout_across_clients(core):
    for i in range(5):
        with pytest.raises(E.PatientNotVerified):
            v(core, 100001, date(1990, 1, 1 + i), f"bot{i}", i)
    with pytest.raises(E.Locked):
        v(core, 100001, BORN, "newclient", 9)


def test_register_new_patient(core):
    p = core.register_patient(name="山本　美咲", name_kana="やまもと みさき", phone="070-0000-0006",
                              birth_date=date(1995, 5, 17), now=NOW)
    assert p.card_number == 100604 and p.name == "山本 美咲"       # NFKC: the full-width space is normalised
    assert v(core, 100604, date(1995, 5, 17), "n").id == p.id
    with pytest.raises(E.AlreadyRegistered):
        core.register_patient(name="山本 美咲", name_kana="やまもと みさき", phone="070-0000-0006",
                              birth_date=date(1995, 5, 17), now=NOW)


@pytest.mark.parametrize("kw", [
    dict(name_kana="山本 美咲"),               # kana expected, kanji given
    dict(phone="abc"),
    dict(birth_date=date(2031, 1, 1)),
    dict(name="   "),
])
def test_register_rejects_bad_input(core, kw):
    base = dict(name="山本 美咲", name_kana="やまもと みさき", phone="070-0000-0006", birth_date=date(1995, 5, 17), now=NOW)
    with pytest.raises(E.InvalidInput):
        core.register_patient(**{**base, **kw})
