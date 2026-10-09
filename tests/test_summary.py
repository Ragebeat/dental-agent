"""Сводка владельцу: шаблон, проверка текста против фактов, запасной шаблон при плохом LLM, HTML. Без базы."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

from dental.agent.llm import FakeLLM, LLMReply
from dental.summary.facts import Facts
from dental.summary.html import render
from dental.summary.text import template, verify
from dental.summary.writer import write

JST = ZoneInfo("Asia/Tokyo")
LABELS = {"chiryo": "治療", "cleaning": "クリーニング", "kenshin": "定期検診", "shoshin": "初診"}


def facts() -> Facts:
    f = Facts(now=datetime(2026, 10, 7, 8, 30, tzinfo=JST), today=date(2026, 10, 7), since=datetime(2026, 10, 6, 18, 30, tzinfo=JST), labels=LABELS)
    f.today_appts = [
        {"id": 601, "start": "2026-10-07T09:00+09:00", "service": "cleaning", "label_ja": "クリーニング", "card": 100001, "name": "田中 <b>太郎</b>"},
        {"id": 602, "start": "2026-10-07T10:30+09:00", "service": "chiryo", "label_ja": "治療", "card": 100002, "name": "高橋 さくら"}]
    f.cancellations = [{"id": 633, "start": "2026-10-09T15:00+09:00", "service": "cleaning", "label_ja": "クリーニング", "card": 100003}]
    f.new_patients = [{"card": 100050, "name": "新規 花子"}]
    f.new_bookings = [{"id": 640, "start": "2026-10-10T09:30+09:00", "service": "kenshin", "label_ja": "定期検診", "card": 100050, "source": "web"}]
    f.free_slots = {"cleaning": 12, "chiryo": 7}
    f.handoffs = [{"id": 3, "reason": "medical", "age_h": 30, "stale": True, "card": 100004, "appointment_id": None, "summary": "痛い <script>x</script>"}]
    return f


def test_template_passes_own_check_in_both_languages():
    f = facts()
    for lang in ("ja", "ru"):
        c = verify(template(f, lang), f)
        assert c.ok and c.recall == 1.0 and c.fabricated == 0, (lang, c)


def test_llm_view_has_no_personal_data():
    s = str(facts().llm_view())
    for bad in ("田中", "高橋", "新規 花子", "痛い", "name"):
        assert bad not in s


def test_check_detects_missing_unknown_and_invented():
    f = facts()
    base = template(f, "ja")
    assert verify(base.replace("[C633]", ""), f).missing == ["C633"]
    assert verify(base + "\n[A999]", f).unknown_refs == ["A999"]
    assert verify(base + "\n本日は57名が来院しました。", f).bad_numbers == [57]
    assert not verify("本日は予定通りです。", f).total_ok


def test_writer_accepts_good_llm_text():
    f = facts()
    good = template(f, "ru")
    sec = write(f, "ru", FakeLLM([LLMReply(good)]))
    assert sec.source == "llm" and sec.attempts == 1 and sec.check.ok


def test_writer_retries_then_falls_back_to_template():
    f = facts()
    bad = LLMReply("本日は5件です。")
    sec = write(f, "ja", FakeLLM([bad, bad, bad]))
    assert sec.source == "template" and sec.attempts == 3 and sec.check.ok
    sec2 = write(f, "ja", FakeLLM([bad, LLMReply(template(f, "ja"))]))
    assert sec2.source == "llm" and sec2.attempts == 2


def test_html_has_switch_both_languages_and_escapes_untrusted_text():
    f = facts()
    out = render(f, {"ja": write(f, "ja", FakeLLM([LLMReply(template(f, "ja"))])), "ru": write(f, "ru", FakeLLM([LLMReply(template(f, "ru"))]))})
    assert 'data-l="ja"' in out and 'data-l="ru"' in out and 'lang="ru"' in out
    assert "<script>x</script>" not in out and "&lt;script&gt;" in out and "&lt;b&gt;" in out
    assert "[C633]" in out


def test_check_catches_swapped_service_numbers_and_llm_sees_names_not_codes():
    f = facts()                      # свободных: чистка 12, лечение 7; сегодня приёмов: чистка 1, лечение 1
    good = template(f, "ja")
    assert verify(good, f).bad_pairs == []
    swapped = good.replace("クリーニング12", "クリーニング7").replace("治療7", "治療12")
    assert set(verify(swapped, f).bad_pairs) == {"クリーニング 7", "治療 12"} and verify(swapped, f).fabricated == 2
    assert verify(template(f, "ru").replace("чистка 12", "чистка 7"), f).bad_pairs == ["чистка 7"]
    v = f.llm_view("ru")
    assert v["free_start_times_today"] == {"чистка": 12, "лечение": 7} and "cleaning" not in str(v)
    assert f.llm_view("ja")["free_start_times_today"] == {"クリーニング": 12, "治療": 7}
