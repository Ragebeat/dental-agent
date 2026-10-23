"""Deterministic summary text (JA/RU) and checking any text against the facts.

The template is the reference and the fallback: if the LLM fails the check, the template goes into the report. The check:
  recall        — every cancellation, new patient, new booking and open handoff is mentioned by reference [C640], [N100010], [B12], [H3];
  fabrications  — a reference absent from the facts; a number absent from the facts' numbers; no total of today's appointments in the text.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from dental.summary.facts import SERVICE_RU, Facts

REASON = {
    "ja": {"medical": "医療的な相談", "ambiguity": "要望が不明確", "late_cancel": "直前キャンセル", "manipulation": "不審な指示",
           "complaint": "苦情", "tool_errors": "システムエラー", "other": "その他"},
    "ru": {"medical": "медицинский вопрос", "ambiguity": "нечёткая просьба", "late_cancel": "поздняя отмена",
           "manipulation": "подозрительные инструкции", "complaint": "жалоба", "tool_errors": "сбои системы", "other": "другое"},
}
REF_RE = re.compile(r"\[([ABCHN]\d+)\]")


def _svc(f: Facts, code: str, lang: str) -> str:
    return f.labels.get(code, code) if lang == "ja" else SERVICE_RU.get(code, code)


def _hhmm(iso: str) -> str:
    return iso[11:16]


def template(f: Facts, lang: str) -> str:
    ja = lang == "ja"
    L: list[str] = []
    md = f"{f.today.month}月{f.today.day}日" if ja else f"{f.today.day:02d}.{f.today.month:02d}"
    by = {}
    for a in f.today_appts:
        by[a["service"]] = by.get(a["service"], 0) + 1
    split = "、".join(f"{_svc(f, k, lang)}{v}件" for k, v in by.items()) if ja else ", ".join(f"{_svc(f, k, lang)} {v}" for k, v in by.items())
    if ja:
        L.append(f"本日（{md}）の予約は{f.today_total}件です" + (f"（{split}）。" if split else "。"))
    else:
        L.append(f"Приёмов на сегодня ({md}): {f.today_total}" + (f" ({split})." if split else "."))
    if f.free_slots:
        fs = "、".join(f"{_svc(f, k, lang)}{v}" for k, v in f.free_slots.items()) if ja else ", ".join(f"{_svc(f, k, lang)} {v}" for k, v in f.free_slots.items())
        L.append(f"本日の空き（開始可能な時間数）：{fs}。" if ja else f"Свободных времён начала на сегодня: {fs}.")
    else:
        L.append("本日の空きはありません。" if ja else "Свободных окон на сегодня нет.")
    if f.cancellations:
        items = "；".join(f"{c['start'][5:7].lstrip('0')}月{c['start'][8:10].lstrip('0')}日{_hhmm(c['start'])}{_svc(f, c['service'], lang)}[C{c['id']}]" for c in f.cancellations) if ja \
            else "; ".join(f"{c['start'][8:10]}.{c['start'][5:7]} {_hhmm(c['start'])} {_svc(f, c['service'], lang)} [C{c['id']}]" for c in f.cancellations)
        L.append(f"前回の営業終了後のキャンセル：{len(f.cancellations)}件（{items}）。" if ja else f"Отмены с прошлого закрытия: {len(f.cancellations)} ({items}).")
    else:
        L.append("前回の営業終了後のキャンセルはありません。" if ja else "Отмен с прошлого закрытия нет.")
    if f.new_patients:
        items = "、".join(f"診察券{p['card']}[N{p['card']}]" for p in f.new_patients) if ja else ", ".join(f"карта {p['card']} [N{p['card']}]" for p in f.new_patients)
        L.append(f"新規患者：{len(f.new_patients)}名（{items}）。" if ja else f"Новые пациенты: {len(f.new_patients)} ({items}).")
    else:
        L.append("新規患者はいません。" if ja else "Новых пациентов нет.")
    if f.new_bookings:
        items = "、".join(f"{_svc(f, b['service'], lang)}{b['start'][5:7].lstrip('0')}月{b['start'][8:10].lstrip('0')}日{_hhmm(b['start'])}[B{b['id']}]" for b in f.new_bookings) if ja \
            else "; ".join(f"{_svc(f, b['service'], lang)} {b['start'][8:10]}.{b['start'][5:7]} {_hhmm(b['start'])} [B{b['id']}]" for b in f.new_bookings)
        L.append(f"前回の営業終了後の新規予約：{len(f.new_bookings)}件（{items}）。" if ja else f"Новые записи с прошлого закрытия: {len(f.new_bookings)} ({items}).")
    else:
        L.append("前回の営業終了後の新規予約はありません。" if ja else "Новых записей с прошлого закрытия нет.")
    if f.handoffs:
        stale = sum(1 for h in f.handoffs if h["stale"])
        items = "；".join(f"{REASON['ja'][h['reason']]}・{h['age_h']}時間経過[H{h['id']}]" for h in f.handoffs) if ja \
            else "; ".join(f"{REASON['ru'][h['reason']]}, {h['age_h']} ч [H{h['id']}]" for h in f.handoffs)
        L.append(f"スタッフ対応待ちの引き継ぎ：{len(f.handoffs)}件" + (f"（うち24時間以上：{stale}件）" if stale else "") + f"（{items}）。" if ja
                 else f"Передачи, ждущие сотрудника: {len(f.handoffs)}" + (f" (дольше 24 ч: {stale})" if stale else "") + f" ({items}).")
    else:
        L.append("対応待ちの引き継ぎはありません。" if ja else "Нерешённых передач нет.")
    return "\n".join(L)


@dataclass
class Check:
    recall: float
    missing: list[str]
    unknown_refs: list[str]
    bad_numbers: list[int]
    total_ok: bool
    bad_pairs: list[str] = field(default_factory=list)      # a "service + number" pair that does not match the facts

    @property
    def fabricated(self) -> int:
        return len(self.unknown_refs) + len(self.bad_numbers) + len(self.bad_pairs) + (0 if self.total_ok else 1)

    @property
    def ok(self) -> bool:
        return not self.missing and self.fabricated == 0


def allowed_numbers(f: Facts) -> set[int]:
    nums = {f.today.month, f.today.day, f.today_total, len(f.cancellations),
            len(f.new_patients), len(f.new_bookings), len(f.handoffs), 0, 1}
    nums |= set(f.free_slots.values())
    nums.add(sum(f.free_slots.values()))
    nums.add(sum(1 for h in f.handoffs if h["stale"]))
    nums |= set(_count_values(f))
    for a in f.today_appts + f.cancellations + f.new_bookings:
        nums |= _time_nums(a["start"]) | {a["id"], a["card"]}
    for p in f.new_patients:
        nums.add(p["card"])
    for h in f.handoffs:
        nums |= {h["id"], h["age_h"], 24}
        if h["card"]:
            nums.add(h["card"])
        if h["appointment_id"]:
            nums.add(h["appointment_id"])
    return nums


def _count_values(f: Facts) -> list[int]:
    by: dict[str, int] = {}
    for a in f.today_appts:
        by[a["service"]] = by.get(a["service"], 0) + 1
    return list(by.values())


def _time_nums(iso: str) -> set[int]:
    return {int(iso[5:7]), int(iso[8:10]), int(iso[11:13]), int(iso[14:16])}


def _bad_pairs(body: str, f: Facts) -> list[str]:
    """A service name immediately followed by a number («歯科治療8枠», «Лечение (3)») must match one of that service's numbers:
    free start times or today's appointment count. Catches swapped numbers that are each valid on their own."""
    ok: dict[str, set[int]] = {}
    for code in set(f.labels) | set(f.free_slots):
        ok[code] = {f.free_slots.get(code, 0)} | {sum(1 for a in f.today_appts if a["service"] == code)}
    bad = []
    for code, nums in ok.items():
        for name in {f.labels.get(code, ""), SERVICE_RU.get(code, "")} - {""}:
            for m in re.finditer(re.escape(name) + r"[\s:：（(]{0,3}(\d+)(?![\d月日時:.])", body, flags=re.I):
                if int(m.group(1)) not in nums:
                    bad.append(f"{name} {m.group(1)}")
    return bad


def verify(text: str, f: Facts) -> Check:
    t = unicodedata.normalize("NFKC", text)
    refs = REF_RE.findall(t)
    known = f.known_refs()
    unknown = sorted({r for r in refs if r not in known})
    must = f.must_refs()
    missing = [r for r in must if r not in set(refs)]
    body = REF_RE.sub("", t)
    allowed = allowed_numbers(f)
    bad = sorted({int(n) for n in re.findall(r"\d+", body) if int(n) not in allowed})
    total_ok = bool(re.search(rf"(?<!\d){f.today_total}(?!\d)", body))
    recall = 1.0 if not must else (len(must) - len(missing)) / len(must)
    return Check(recall=recall, missing=missing, unknown_refs=unknown, bad_numbers=bad, total_ok=total_ok,
                 bad_pairs=_bad_pairs(body, f))
