"""Relative dates and times of day, parsed entirely in code, no LLM.

Two entry points produce the same `DateIntent` structure:
  • parse_ja(text) — deterministic parsing of common Japanese phrases («来週の金曜の午前», «明後日の夕方», «月末»);
  • the LLM can fill `DateIntent` itself (JSON schema: DateIntent.model_json_schema()) for wordings the rules miss.
`resolve(intent, now)` turns the intent into a slot-search window: a date range (at most 14 days, the search_slots limit)
and a window of start times. The model does no date arithmetic: a week starts on Monday, «金曜» alone is the nearest Friday after today.
"""
from __future__ import annotations

import calendar
import re
import unicodedata
from datetime import date, datetime, time, timedelta
from typing import Literal, Optional

from pydantic import BaseModel, Field

MAX_DAYS = 14
WEEKDAY_CHARS = "月火水木金土日"       # 0 = Monday
# windows of appointment START times (inclusive); 夕方 = 16:00–18:00 per scenario s03
PARTS: dict[str, tuple[time, time]] = {
    "morning": (time(9, 0), time(11, 59)),
    "afternoon": (time(12, 0), time(18, 30)),
    "evening": (time(16, 0), time(18, 0)),
    "night": (time(17, 0), time(18, 30)),
}


class DateIntent(BaseModel):
    """Что пациент сказал про дату и время. Пустые поля — не сказал."""
    day_offset: Optional[int] = Field(None, description="через сколько дней от сегодня: 0 сегодня, 1 завтра, 2 послезавтра")
    week_offset: Optional[int] = Field(None, description="0 эта неделя, 1 следующая (来週), 2 через неделю (再来週)")
    month_offset: Optional[int] = Field(None, description="0 этот месяц, 1 следующий (来月), 2 через месяц")
    month_part: Optional[Literal["start", "end"]] = Field(None, description="начало/конец месяца (月初/月末)")
    after_days: Optional[int] = Field(None, ge=0, description="через N дней (N日後)")
    after_weeks: Optional[int] = Field(None, ge=0, description="через N недель (N週間後)")
    after_months: Optional[int] = Field(None, ge=0, description="через N месяцев (Nか月後, 半年後 = 6)")
    month: Optional[int] = Field(None, ge=1, le=12)
    day: Optional[int] = Field(None, ge=1, le=31)
    weekday: Optional[int] = Field(None, ge=0, le=6, description="0=понедельник … 6=воскресенье")
    part_of_day: Optional[Literal["morning", "afternoon", "evening", "night"]] = None
    time: Optional[str] = Field(None, description="точное время HH:MM, если названо")
    time_after: Optional[str] = Field(None, description="не раньше HH:MM («14時以降»)")
    time_before: Optional[str] = Field(None, description="не позже HH:MM («15時まで»)")
    asap: bool = Field(False, description="как можно раньше / в любое время")

    def is_empty(self) -> bool:
        return not any(v not in (None, False) for v in self.model_dump().values())


class Window(BaseModel):
    date_from: date
    date_to: date
    time_from: Optional[time] = None
    time_to: Optional[time] = None
    truncated: bool = False          # range cut down to 14 days
    note: str = ""


# ---------- parsing Japanese text ----------
_REL_DAY = (("明後日", 2), ("あさって", 2), ("明日", 1), ("あした", 1), ("あす", 1), ("今日", 0), ("本日", 0), ("きょう", 0))
_PART_WORDS = (("午前", "morning"), ("朝", "morning"), ("午後", "afternoon"), ("夕方", "evening"), ("夜", "night"), ("晩", "night"))
_ASAP = re.compile(r"なるべく早く|できるだけ早く|出来るだけ早く|早めに|最短|いつでも|お任せ")


def parse_ja(text: str) -> DateIntent:
    t = unicodedata.normalize("NFKC", text)
    it = DateIntent()
    for w, off in _REL_DAY:
        if w in t:
            it.day_offset = off
            break
    if "再来週" in t:
        it.week_offset = 2
    elif "来週" in t:
        it.week_offset = 1
    elif "今週" in t:
        it.week_offset = 0
    if "再来月" in t:
        it.month_offset = 2
    elif "来月" in t:
        it.month_offset = 1
    elif "今月" in t:
        it.month_offset = 0
    if "月末" in t:
        it.month_part = "end"
    elif "月初" in t:
        it.month_part = "start"
    if "半年後" in t:
        it.after_months = 6
    m = re.search(r"(\d{1,2})\s*[かヶケカ]月後", t)
    if m:
        it.after_months = int(m.group(1))
    m = re.search(r"(\d{1,2})\s*週間後", t)
    if m:
        it.after_weeks = int(m.group(1))
    m = re.search(r"(?<!\d)(\d{1,3})\s*日後", t)
    if m:
        it.after_days = int(m.group(1))
    m = re.search(r"(\d{1,2})月\s*(\d{1,2})日", t) or re.search(r"(?<!\d)(\d{1,2})/(\d{1,2})(?!\d)", t)
    if m:
        it.month, it.day = int(m.group(1)), int(m.group(2))
    else:
        m = re.search(r"(?<![\d月])(\d{1,2})日(?!間|後|前)", t)
        if m and it.day_offset is None:
            it.day = int(m.group(1))
    m = re.search(rf"([{WEEKDAY_CHARS}])曜", t)
    if m:
        it.weekday = WEEKDAY_CHARS.index(m.group(1))
    for w, part in _PART_WORDS:
        if w in t:
            it.part_of_day = part
            break
    m = re.search(r"(?<!\d)(\d{1,2}):(\d{2})", t)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
    else:
        m = re.search(r"(?<!\d)(\d{1,2})時(?:\s*(半)|\s*(\d{1,2})分)?", t)
        h, mi = (int(m.group(1)), 30 if m.group(2) else int(m.group(3) or 0)) if m else (None, 0)
    if h is not None:
        after = bool(re.search(r"(?<!\d)\d{1,2}(?:時|:\d{2})(?:\s*(?:半|\d{1,2}分))?\s*(?:以降|以後|から)", t))
        before = bool(re.search(r"(?<!\d)\d{1,2}(?:時|:\d{2})(?:\s*(?:半|\d{1,2}分))?\s*(?:まで|以前|までに)", t))
        if h < 9 and it.part_of_day in ("afternoon", "evening", "night"):      # 午後3時 → 15:00
            h += 12
        elif h < 9 and it.part_of_day is None and h >= 1:                      # «3時» with no qualifier: the clinic opens at 9
            h += 12
        if 0 <= h < 24 and 0 <= mi < 60:
            hhmm = f"{h:02d}:{mi:02d}"
            if after and not before:
                it.time_after = hhmm
            elif before and not after:
                it.time_before = hhmm
            else:
                it.time = hhmm
    if _ASAP.search(t):
        it.asap = True
    return it


# ---------- computing the window ----------
def _month_start(d: date, offset: int) -> date:
    idx = d.year * 12 + d.month - 1 + offset
    return date(idx // 12, idx % 12 + 1, 1)


def resolve(it: DateIntent, now: datetime) -> Window:
    today = now.date()
    note = ""
    if it.after_days is not None or it.after_weeks is not None or it.after_months is not None:
        base = today + timedelta(days=(it.after_days or 0) + 7 * (it.after_weeks or 0))
        if it.after_months:
            first = _month_start(base, it.after_months)
            base = first.replace(day=min(base.day, calendar.monthrange(first.year, first.month)[1]))
        lo, hi = base, base + timedelta(days=MAX_DAYS - 1)      # search for the nearest open day from this date
    elif it.month and it.day:
        try:
            d = date(today.year, it.month, it.day)
            if d < today:
                d = date(today.year + 1, it.month, it.day)
        except ValueError:
            raise ValueError(f"no such date: {it.month}/{it.day}") from None
        lo = hi = d
    elif it.day and not it.month:
        d = None
        for off in (0, 1):                      # this month, otherwise the next
            first = _month_start(today, off)
            if it.day <= calendar.monthrange(first.year, first.month)[1] and first.replace(day=it.day) >= today:
                d = first.replace(day=it.day)
                break
        if d is None:
            raise ValueError(f"no such date: day {it.day}")
        lo = hi = d
    elif it.day_offset is not None:
        lo = hi = today + timedelta(days=it.day_offset)
    elif it.weekday is not None:
        if it.week_offset is not None:
            monday = today - timedelta(days=today.weekday()) + timedelta(weeks=it.week_offset)
            d = monday + timedelta(days=it.weekday)
            if d < today:
                d += timedelta(weeks=1)
                note = "this weekday has already passed this week; the next one is taken"
        else:
            d = today + timedelta(days=(it.weekday - today.weekday() - 1) % 7 + 1)      # strictly after today
        lo = hi = d
    elif it.week_offset is not None:
        monday = today - timedelta(days=today.weekday()) + timedelta(weeks=it.week_offset)
        lo, hi = max(monday, today), monday + timedelta(days=6)
    elif it.month_offset is not None or it.month_part:
        first = _month_start(today, it.month_offset or 0)
        if it.month_part == "start" and it.month_offset is None and first <= today:
            first = _month_start(today, 1)          # «月初» said mid-month → the start of the next month
        last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
        if it.month_part == "end":
            lo, hi = last - timedelta(days=6), last
        elif it.month_part == "start":
            lo, hi = first, first + timedelta(days=6)
        else:
            lo, hi = first, last
    else:
        lo, hi = today, today + timedelta(days=MAX_DAYS - 1)
    lo = max(lo, today)
    hi = max(hi, lo)
    truncated = (hi - lo).days + 1 > MAX_DAYS
    if truncated:
        hi = lo + timedelta(days=MAX_DAYS - 1)
    tf = tt = None
    if it.time:
        tf = tt = time.fromisoformat(it.time)
    elif it.time_after or it.time_before:
        tf = time.fromisoformat(it.time_after) if it.time_after else time(0, 0)
        tt = time.fromisoformat(it.time_before) if it.time_before else time(23, 59)
    elif it.part_of_day:
        tf, tt = PARTS[it.part_of_day]
    return Window(date_from=lo, date_to=hi, time_from=tf, time_to=tt, truncated=truncated, note=note)
