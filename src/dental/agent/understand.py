"""Parsing patient messages in code: card number, dates, phone, name, service, action, yes/no, option choice, dangerous requests.

Pure functions with no LLM and no database — ordinary tests cover them. The graph needs the LLM only where the rules found nothing
(and even then only for explicitly stated values, not for guesses).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date

from dental.agent.dates import DateIntent, parse_ja


def norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


# ---------- identification ----------
def parse_card(text: str) -> int | None:
    m = re.search(r"(?<!\d)(\d{6})(?!\d)", norm(text))
    return int(m.group(1)) if m else None


def parse_birth(text: str) -> str | None:
    t = norm(text).replace("ねん", "年").replace("がつ", "月").replace("にち", "日")      # «1988ねん4がつ12にち»
    m = re.search(r"(?<!\d)((?:19|20)\d{2})\s*[年/\-.]\s*(\d{1,2})\s*[月/\-.]\s*(\d{1,2})", t) \
        or re.search(r"(?<!\d)((?:19|20)\d{2})(\d{2})(\d{2})(?!\d)", t)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return None


def parse_phone(text: str) -> str | None:
    for m in re.finditer(r"(?<!\d)0\d{1,4}[-\s]?\d{1,4}[-\s]?\d{3,4}(?!\d)", norm(text)):
        digits = re.sub(r"\D", "", m.group(0))
        if len(digits) in (10, 11):
            return m.group(0).strip()
    return None


def to_hiragana(s: str) -> str:
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s)


_NAME_PREFIX = re.compile(r"^(?:私は|わたしは|名前は|お名前は|氏名は|患者の|申し遅れました|はい、?)")


def parse_name_kana(text: str) -> tuple[str, str] | None:
    """«山本美咲（やまもと みさき）», «山本 美咲（やまもと みさき）» → name and reading. Without brackets we do not guess: see parse_reg_part and the LLM fallback."""
    m = re.search(r"((?:[^\s（(、,。\n「」]{1,8}[ \u3000]+)?[^\s（(、,。\n「」]{1,10})\s*[（(]\s*([ぁ-んァ-ヶー][ぁ-んァ-ヶー\s\u3000]{1,20})\s*[）)]", text)
    if not m:
        return None
    name = _NAME_PREFIX.sub("", m.group(1)).strip()
    parts = re.split(r"[ \u3000]+", name)
    if len(parts) >= 2:                                   # «山本 美咲» — family and given name; an extra word before them («こんにちは 山本美咲») is dropped
        name = " ".join(parts[-2:]) if all(len(x) <= 4 for x in parts[-2:]) and len(parts) == 2 else parts[-1]
    kana = to_hiragana(re.sub(r"[\s\u3000]+", " ", m.group(2)).strip())
    return (name, kana) if len(name.replace(" ", "")) >= 2 else None


# Step-by-step registration: the patient answers a question with one field («山本 美咲と申します», «ふりがなは、やまもと みさきです»).
_KANA_LABEL = re.compile(r"(?:ふりがな|フリガナ|ふり仮名|振り仮名|よみがな|読み方)\s*(?:は|を)?\s*[:：、,，]?\s*"
                         r"([ぁ-んァ-ヶー][ぁ-んァ-ヶー \u3000]{1,20}?)\s*(?:です|と申します|といいます|でございます|[。．、,，]|$)")
_TAIL = re.compile(r"\s*(?:と申します|といいます|と言います|でございます|です)?[。．.!！]*\s*$")
_NAME_SHAPE = re.compile(r"^[一-鿿々]{1,4}[ \u3000]?[一-鿿々ぁ-ん]{1,4}$")
_NOT_NAME = re.compile(r"予約|診|検|治療|変更|取消|曜|午前|午後|今日|明日|来週|今週|初め|お願い|電話|番号|生年|相談|質問|診察券")
_NOT_KANA = re.compile(r"ありがとう|すみません|ください|ありません|いいえ|はい|ます|です|ません|おねがい|よろしく|こんにちは|はじめ")


def parse_reg_part(text: str, need: list[str]) -> dict:
    """One field from an answer to a registration question. need: which of name / name_kana are still empty. Obvious forms only."""
    t = norm(text).strip()
    out: dict[str, str] = {}
    rest = t
    if "name_kana" in need and (m := _KANA_LABEL.search(t)):
        out["name_kana"] = to_hiragana(re.sub(r"[ \u3000]+", " ", m.group(1)).strip())
        rest = (t[:m.start()] + " " + t[m.end():]).strip(" 、,。")
    cand = _TAIL.sub("", _NAME_PREFIX.sub("", rest.strip())).strip(" 、,。")
    if "name" in need and cand and len(cand) <= 12 and _NAME_SHAPE.match(cand) and not _NOT_NAME.search(cand):
        out["name"] = re.sub(r"[ \u3000]+", " ", cand)
    elif ("name" not in need and "name_kana" in need and "name_kana" not in out
          and re.fullmatch(r"[ぁ-んァ-ヶー \u3000]{3,24}", cand) and not _NOT_KANA.search(cand)):
        out["name_kana"] = to_hiragana(re.sub(r"[ \u3000]+", " ", cand))
    return out


NEW_PATIENT = re.compile(r"初めて|はじめて|初診|新規|新患|診察券(?:は)?(?:持っていません|ありません|ない|を持っていない)|カード(?:は)?(?:持っていません|ありません)")


# ---------- intent ----------
_CANCEL = re.compile(r"キャンセル|取り消|取消|やめたい|行けなくな|行けない")
_RESCHEDULE = re.compile(r"変更|変えたい|変えて|変えられ|変えれ|変わりたい|ずらし|ずらせ|振替|振り替え|日にちを変|別の日|移したい|動かし|リスケ|延期")
_BOOK = re.compile(r"予約|取りたい|入れたい|入れて|お願いしたい|診てもらい|見てもらい|受けたい|行きたい")


def parse_action(text: str) -> str | None:
    t = norm(text)
    if _CANCEL.search(t):
        return "cancel"
    if _RESCHEDULE.search(t):
        return "reschedule"
    if _BOOK.search(t):
        return "book"
    return None


_ALIASES = {
    "shoshin": ("初診",),
    "cleaning": ("クリーニング", "歯石", "くりーにんぐ"),
    "kenshin": ("定期検診", "検診", "健診", "定期チェック"),
    "chiryo": ("治療", "詰め物", "詰めもの"),
}


def parse_service(text: str, services: list[dict] | None = None) -> str | None:
    """Explicit mentions only. «歯の状態を見てもらいたい» is not a service."""
    t = norm(text)
    codes = {s["code"] for s in services} if services else set(_ALIASES)
    found = [code for code, words in _ALIASES.items() if code in codes and any(w in t for w in words)]
    if len(found) > 1:
        return None                     # «治療なのかクリーニングなのか» is not a choice of service
    if found:
        return found[0]
    for s in services or []:
        if s.get("label_ja") and s["label_ja"] in t:
            return s["code"]
    return None


# ---------- yes / no ----------
_NO = re.compile(r"いいえ|違(?:う|い|います)|やめ|やっぱ|だめ|ダメ|駄目|ではなく|じゃなく|別の|他の|やり直|変え|キャンセル|結構です")
_YES = re.compile(r"^(?:はい|ええ|うん|そう|OK|ok|オッケー|お願い)|お願いします|それで(?:お願い|いい|大丈夫|結構)|でいい|で大丈夫|大丈夫です|構いません|問題ありません|確定して|よろしく")


def parse_yesno(text: str) -> bool | None:
    t = norm(text).strip()
    if _NO.search(t):
        return False
    if _YES.search(t):
        return True
    return None


# ---------- option choice ----------
_ORD = {"1": 0, "2": 1, "3": 2, "一": 0, "二": 1, "三": 2}


def parse_choice(text: str, options: list[dict]) -> int | None:
    """options: [{'date': 'YYYY-MM-DD', 'time': 'HH:MM'}]. Index of the chosen one, or None."""
    t = norm(text)
    if re.search(r"最初|一番目|1つ目|一つ目|はじめの|先頭|最も早い|一番早い", t):
        return 0
    if re.search(r"最後|最終", t):
        return len(options) - 1
    m = (re.search(r"([1-3１-３一二三])\s*(?:番|つ目|番目)", t) or re.fullmatch(r"\s*([1-3])\s*", t)
         or re.match(r"\s*([1-3])(?![\d/:時月日年])", t))             # «1だって言ってるだろ…»
    if m:
        i = _ORD.get(unicodedata.normalize("NFKC", m.group(1)))
        if i is not None and i < len(options):
            return i
    it = parse_ja(t)
    hits = list(range(len(options)))
    if it.time:
        hits = [i for i in hits if options[i]["time"] == it.time]
    if it.day is not None or it.month:
        hits = [i for i in hits if (it.day is None or int(options[i]["date"][8:]) == it.day)
                and (it.month is None or int(options[i]["date"][5:7]) == it.month)]
    if it.weekday is not None:
        hits = [i for i in hits if date.fromisoformat(options[i]["date"]).weekday() == it.weekday]
    if (it.time or it.day is not None or it.month or it.weekday is not None) and len(hits) == 1:
        return hits[0]
    return None


DATE_FIELDS = ("day_offset", "week_offset", "month_offset", "month_part", "month", "day", "weekday",
               "after_days", "after_weeks", "after_months")


def has_date_info(it: DateIntent) -> bool:
    return any(getattr(it, f) is not None for f in DATE_FIELDS)


def merge_intent(old: DateIntent, new: DateIntent) -> DateIntent:
    """A new date replaces the old one entirely; time of day / exact time are replaced only when stated."""
    out = old.model_copy()
    if has_date_info(new):
        for f in DATE_FIELDS:
            setattr(out, f, getattr(new, f))
    if new.part_of_day or new.time or new.time_after or new.time_before:       # a new time displaces the previous time conditions
        out.part_of_day, out.time, out.time_after, out.time_before = new.part_of_day, new.time, new.time_after, new.time_before
    out.asap = out.asap or new.asap
    return out


# ---------- dangerous requests ----------
MANIPULATION = re.compile(r"これまでの指示|指示は?(?:すべて|全て)無視|システムメッセージ|\[システム|管理者権限|すべての予約枠|全ての予約枠|"
                          r"ignore (?:all|previous|the)|system prompt|プロンプト|ジェイルブレイク", re.I)
COMPLAINT = re.compile(r"クレーム|苦情|最悪|責任者|訴え|許せない|ひどい(?:対応|扱い|態度)")
MEDICAL_ADVICE = re.compile(r"薬|痛み止め|飲(?:ん|め)ば|原因|何の病気|病気|診断|虫歯(?:です|ですか)|どうすれば|対処|処方|副作用|麻酔")


@dataclass
class Flags:
    manipulation: bool = False
    complaint: bool = False
    medical: bool = False


def parse_flags(text: str) -> Flags:
    t = norm(text)
    return Flags(bool(MANIPULATION.search(t)), bool(COMPLAINT.search(t)), bool(MEDICAL_ADVICE.search(t)))
