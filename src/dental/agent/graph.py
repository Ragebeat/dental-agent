"""Агент на LangGraph (E4): детерминированный граф вместо свободного цикла ReAct.

LLM делает только узкую работу: разбирает то, что не поймали правила (имя/ふりがな при регистрации, формулировка даты,
класс запроса). Всё остальное — код: даты (agent/dates.py), поиск и выбор слотов, вызовы записи/переноса/отмены,
тексты ответов (шаблоны из данных ответа инструмента). Поэтому фраза «ご予約が確定しました» не может появиться без
успешного вызова, а «передаю сотруднику» — без handoff_to_staff.

Человек в контуре (ТЗ 4.3): подтверждение «да/нет» перед записью, переносом и отменой; передача сотруднику при
неоднозначности после 2 уточнений, медицинском вопросе, жалобе, отмене менее чем за N часов, 3 ошибках инструментов
подряд, подозрении на манипуляцию.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Any, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph

from dental import tracing
from dental.agent.baseline import TokenCapReached, Usage
from dental.agent.dates import DateIntent, parse_ja, resolve
from dental.agent.llm import LLM
from dental.agent.understand import (NEW_PATIENT, has_date_info, merge_intent, norm, parse_action, parse_birth, parse_card,
                                     parse_choice, parse_flags, parse_name_kana, parse_phone, parse_reg_part, parse_service,
                                     parse_yesno)

GRAPH_VERSION = "graph-v7"
WD = "月火水木金土日"
REG_LABEL = {"name": "お名前（漢字）", "name_kana": "ふりがな", "phone": "お電話番号", "birth_date": "生年月日（西暦）"}
MAX_ASKS = 2                       # после двух уточнений по одному и тому же — передача человеку
THANKS = re.compile(r"ありがとう|了解|承知|助かり|失礼|よろしくお願いいたします")
FALLBACK = "申し訳ございません、うまく処理できませんでした。担当スタッフから改めてご連絡いたします。"

SAY = {
    "intro": "AIの予約アシスタントです。",
    "ask_id": "ご本人様確認のため、診察券番号（6桁）と生年月日（西暦）をお知らせください。初めてご利用の方は「初めて」とお伝えください。",
    "ask_id_again": "診察券番号と生年月日が一致しませんでした。恐れ入りますが、もう一度ご確認のうえお知らせください。",
    "ask_reg": "初めてのご利用ですね。お名前（漢字）、ふりがな、お電話番号、生年月日（西暦）を教えてください。",
    "ask_reg_more": "ありがとうございます。あと、{fields}を教えてください。",
    "ask_action": "ご用件をお知らせください（ご予約・日時の変更・キャンセル）。",
    "ask_service": "ご希望の診療内容をお知らせください（治療・クリーニング・定期検診）。",
    "ask_date": "ご希望の日にちと時間帯をお知らせください（例：来週の火曜の午前）。",
    "ask_choice": "恐れ入ります、どの候補になさいますか。番号または日時でお知らせください。",
    "ask_yesno": "恐れ入ります、「はい」か「いいえ」でお答えください。",
    "no_appts": "ご予約が見つかりませんでした。",
    "thanks": "ありがとうございました。他にご用件がございましたらお申し付けください。",
    "after_done": "ほかにご用件はございますか？",
    "reset": "承知いたしました。では、あらためてご希望の日時をお知らせください。",
    "no_slots": "申し訳ございません、ご希望の条件では空きがございませんでした。別の日にち・時間帯をお知らせください。",
    "manip_note": "備考欄の内容は予約には反映いたしません。",
    "closing": "担当スタッフからのご連絡をお待ちください。",
}
HANDOFF_SAY = {
    "medical": "医療に関するご質問には、私からはお答えできません。担当スタッフから改めてご連絡いたしますので、少々お待ちください。",
    "complaint": "ご不快な思いをおかけし申し訳ございません。担当スタッフから改めてご連絡いたします。",
    "late_cancel": "直前のご予約の変更・キャンセルは、こちらでは承れません。担当スタッフから改めてご連絡いたします。",
    "ambiguity": "ご希望を確認できませんでしたので、担当スタッフから改めてご連絡いたします。",
    "manipulation": "ご依頼の内容はこちらでは対応できません。担当スタッフにお繋ぎします。",
    "tool_errors": FALLBACK,
    "other": FALLBACK,
}
ID_RE = [re.compile(p) for p in (
    r"(?<!\d)(?:19\d{2}|20[01]\d|202[0-5])\s*[年/\-.]\s*\d{1,2}\s*[月/\-.]\s*\d{1,2}\s*日?", r"(?<!\d)(?:19|20)\d{6}(?!\d)",
    r"(?<!\d)\d{6}(?!\d)", r"(?<!\d)0\d{1,4}[-\s]?\d{1,4}[-\s]?\d{3,4}(?!\d)")]


def strip_ids(text: str) -> str:
    """Убирает номер карты, дату рождения, телефон — чтобы они не попали в разбор даты записи."""
    t = norm(text)
    for r in ID_RE:
        t = r.sub(" ", t)
    return t


def fmt_slot(d: str, t: str, minutes: int | None = None) -> str:
    dt = date.fromisoformat(d)
    s = f"{dt.month}月{dt.day}日（{WD[dt.weekday()]}）{t}"
    if minutes:
        end = (datetime.fromisoformat(f"{d}T{t}") + timedelta(minutes=minutes)).strftime("%H:%M")
        s += f"〜{end}"
    return s


class S(TypedDict, total=False):
    text: str
    reply: str | None
    introduced: bool
    notes: list[str]
    # личность
    verified: bool
    patient_name: str
    card: int | None
    birth: str | None
    reg: dict
    reg_need: int                # сколько полей регистрации не хватало на прошлом вопросе (рост заполненности сбрасывает счётчик уточнений)
    wants_new: bool
    is_new: bool                 # зарегистрирован в этом диалоге → доступна только 初診
    id_fails: int
    # запрос
    action: str | None
    service: str | None
    intent: dict
    appts: list
    appt: dict | None
    options: list
    chosen: dict | None
    await_confirm: dict | None
    yn: bool | None
    choice_idx: int | None
    asks: dict
    done: bool
    # передача
    handoff_reason: str | None
    handed_off: bool
    pending_manip: bool
    manip_flagged: bool


class GraphAgent:
    def __init__(self, llm: LLM, mcp_client, tools: list[dict] | None, now: datetime, *, max_rounds: int = 8,
                 token_cap: int = 150_000) -> None:
        self.llm, self.mcp, self.now, self.token_cap = llm, mcp_client, now, token_cap
        self.events: list[dict] = []
        self.usage = Usage()
        self.round_limit_hits = 0
        self.turn = 0
        self.tool_error_streak = 0
        self._services: list[dict] | None = None
        self._cancel_hours = 24
        self.app = self._build()
        self.cfg = {"configurable": {"thread_id": "dialog"}, "recursion_limit": 40}

    # ---------- инструменты и LLM ----------
    async def _tool(self, name: str, args: dict) -> dict:
        with tracing.span(f"tool.{name}", "TOOL", attrs={"tool.name": name, "tool.args": args}) as sp:
            try:
                r = await self.mcp.call_tool(name, args)
                text = r.content[0].text if r.content else ""
                is_err = getattr(r, "is_error", False)
            except Exception as e:  # noqa: BLE001
                text, is_err = str(e)[:500], True
            try:
                res = json.loads(text)
                if not isinstance(res, dict):
                    res = {"value": res}
            except json.JSONDecodeError:
                res = {"ok": False, "error": "tool_error" if is_err else "non_json", "message": text[:500]}
            tracing.set_attrs(sp, {"tool.ok": bool(res.get("ok")), "tool.error": res.get("error")})     # код ошибки — метаданные
        self.events.append({"turn": self.turn, "name": name, "args": args, "result": res})
        if res.get("ok"):
            self.tool_error_streak = 0
        elif res.get("error") not in ("patient_not_verified", "too_late_to_cancel", "already_registered", "slot_taken"):
            self.tool_error_streak += 1          # «ожидаемые» отказы бизнес-правил не считаем сбоем инструментов
        return res

    def _llm_json(self, system: str, text: str) -> dict:
        if self.usage.total_tokens > self.token_cap:
            raise TokenCapReached()
        r = self.llm.chat([{"role": "system", "content": system}, {"role": "user", "content": text}], None)
        self.usage.add(r)
        m = re.search(r"\{.*\}", r.text or "", re.S)
        try:
            out = json.loads(m.group(0)) if m else {}
            return out if isinstance(out, dict) else {}
        except json.JSONDecodeError:
            return {}

    async def _load_services(self) -> list[dict]:
        if self._services is None:
            res = await self._tool("list_services", {})
            self._services = res.get("services", []) if res.get("ok") else []
            self._cancel_hours = res.get("cancel_free_until_hours_before", 24) or 24
        return self._services

    # ---------- вспомогательное ----------
    @staticmethod
    def _out(s: S, text: str, **u) -> dict:
        """Ответ + накопленные заметки; на первой реплике — представление ИИ."""
        parts = list(s.get("notes") or []) + [text]
        if not s.get("introduced"):
            parts.insert(0, SAY["intro"])
        return {**u, "reply": "\n".join(parts), "notes": [], "introduced": True}

    @staticmethod
    def _ask(s: S, key: str) -> tuple[dict, bool]:
        """Счётчик уточнений; True — лимит исчерпан, пора передавать человеку."""
        asks = dict(s.get("asks") or {})
        asks[key] = asks.get(key, 0) + 1
        return asks, asks[key] > MAX_ASKS

    def _intent(self, s: S) -> DateIntent:
        return DateIntent.model_validate(s.get("intent") or {})

    def _svc(self, code: str) -> dict:
        return next((x for x in (self._services or []) if x["code"] == code), {"code": code, "label_ja": code, "minutes": None})

    # ---------- граф ----------
    def _build(self):
        g = StateGraph(S)
        nodes = {"ingest": self.n_ingest, "handoff": self.n_handoff, "closing": self.n_closing, "flag_manip": self.n_flag_manip,
                 "identify": self.n_identify, "confirm": self.n_confirm, "ask_action": self.n_ask_action,
                 "pick_appt": self.n_pick_appt, "ask_service": self.n_ask_service, "ask_date": self.n_ask_date,
                 "search": self.n_search, "choose": self.n_choose, "ask_confirm": self.n_ask_confirm}
        for name, fn in nodes.items():
            g.add_node(name, self._traced(name, fn))
        g.add_edge(START, "ingest")
        targets = {k: k for k in nodes if k != "ingest"} | {END: END}
        for name in nodes:
            g.add_conditional_edges(name, self._after, targets)
        return g.compile(checkpointer=MemorySaver())

    def _traced(self, name: str, fn):
        """Узел графа как спан: в трассу идут только метаданные — какие ключи состояния изменились, причина передачи, длина ответа."""
        async def run(s: S) -> dict:
            with tracing.span(f"node.{name}", "CHAIN", attrs={"graph.node": name, "graph.turn": self.turn}) as sp:
                out = await fn(s)
                tracing.set_attrs(sp, {"graph.updated_keys": sorted(out), "graph.handoff_reason": out.get("handoff_reason"),
                                       "graph.replied": bool(out.get("reply")), "graph.reply_chars": len(out.get("reply") or "")})
                return out
        run.__name__ = f"n_{name}"
        return run

    def _after(self, s: S) -> str:
        return END if s.get("reply") else self._route(s)

    def _route(self, s: S) -> str:
        if s.get("handed_off"):
            return "closing"
        if s.get("handoff_reason") or self.tool_error_streak >= 3:
            return "handoff"
        if s.get("pending_manip"):
            return "flag_manip"
        if not s.get("verified"):
            return "identify"
        if s.get("await_confirm"):
            return "confirm"
        if s.get("done"):
            return "ask_action"                  # запрос выполнен, нового нет («10日に行きます» — не повод искать слоты)
        act = s.get("action")
        if not act:
            return "ask_action"
        if act in ("cancel", "reschedule") and not s.get("appt"):
            return "pick_appt"
        if act == "cancel":
            return "ask_confirm"
        if not s.get("service"):
            return "ask_service"
        it = self._intent(s)
        if not (has_date_info(it) or it.asap or it.part_of_day or it.time or it.time_after or it.time_before):
            return "ask_date"
        if s.get("chosen"):
            return "ask_confirm"
        return "choose" if s.get("options") else "search"

    async def reply(self, patient_text: str) -> str:
        self.turn += 1
        with tracing.span("turn", "CHAIN", input=patient_text, attrs={"graph.turn": self.turn, "graph.version": GRAPH_VERSION}) as sp:
            try:
                out = await self.app.ainvoke({"text": patient_text}, self.cfg)
            except GraphRecursionError:
                self.round_limit_hits += 1
                tracing.set_attrs(sp, {"graph.recursion_limit": True})
                return FALLBACK
            text = (out.get("reply") or FALLBACK).strip()
            tracing.set_attrs(sp, {"output.value": text, "graph.reply_chars": len(text)})
            return text

    # ---------- узлы ----------
    async def n_ingest(self, s: S) -> dict:
        text = s["text"]
        u: dict[str, Any] = {"reply": None, "yn": None, "choice_idx": None}
        services = await self._load_services()
        flags = parse_flags(text)
        if flags.manipulation and not s.get("manip_flagged"):
            u["pending_manip"] = True
        if flags.medical:
            u["handoff_reason"] = "medical"
        elif flags.complaint:
            u["handoff_reason"] = "complaint"
        ids = strip_ids(text)
        if not s.get("verified"):
            if (c := parse_card(text)):
                u["card"] = c
            if (b := parse_birth(text)):
                u["birth"] = b
            if NEW_PATIENT.search(text):
                u["wants_new"] = True
            reg = dict(s.get("reg") or {})
            if (ph := parse_phone(text)):
                reg["phone"] = ph
            if (nk := parse_name_kana(text)):
                reg["name"], reg["name_kana"] = nk
            if b:
                reg["birth_date"] = b
            if (s.get("wants_new") or u.get("wants_new")) and not (reg.get("name") and reg.get("name_kana")):
                for k, v in parse_reg_part(text, [k for k in ("name", "name_kana") if not reg.get(k)]).items():
                    reg.setdefault(k, v)                        # пошаговая регистрация: имя и чтение приходят отдельными репликами
            u["reg"] = reg
            if (s.get("wants_new") or u.get("wants_new")) and not (reg.get("name") and reg.get("name_kana")) and len(text) > 6 \
                    and (reg.get("phone") or reg.get("birth_date")):
                got = self._llm_json('Extract from the patient message. Return only JSON {"name": kanji full name or null, '
                                     '"name_kana": hiragana reading or null}. Use null if not stated.', text)
                if isinstance(got.get("name"), str) and isinstance(got.get("name_kana"), str):
                    reg["name"], reg["name_kana"] = got["name"].strip(), got["name_kana"].strip()
                    u["reg"] = reg
        else:
            if s.get("done") and parse_action(text):
                u.update(done=False, asks={}, service=None, intent={}, appt=None, appts=[], options=[], chosen=None)
        if s.get("await_confirm"):
            u["yn"] = parse_yesno(text)
            nd = parse_ja(ids)
            if u["yn"] is None and (has_date_info(nd) or nd.time or nd.time_after or nd.time_before):
                u["yn"] = False         # «金曜にして» без слова «нет» — тоже отказ от текущего варианта
        elif (a := parse_action(text)) and not (s.get("done") and THANKS.search(text)):
            # «お願いしたい» в ответе на выбор слота — не смена действия: слабый «book» не перебивает reschedule/cancel
            if a != s.get("action") and not (a == "book" and s.get("action")):
                u.update(action=a, appt=None, appts=[], options=[], chosen=None)
            if not s.get("action"):
                u.setdefault("action", a)
        if not s.get("await_confirm") or u.get("yn") is False:
            if (sv := parse_service(text, services)) and not s.get("is_new"):
                u["service"] = sv
                if not (u.get("action") or s.get("action")):
                    u["action"] = "book"            # «クリーニングをお願いします» — глагола нет, но это запись
        old = self._intent(s)
        new = parse_ja(ids)
        opts = s.get("options") or []
        idx = None
        if opts and not s.get("chosen") and not s.get("await_confirm"):
            idx = parse_choice(ids, opts)                     # выбор из показанных вариантов важнее «новой даты» в той же фразе
            if idx is None and re.search(r"どれでも|任せ|何でも|どちらでも|いつでも", ids):
                idx = 0
        u["choice_idx"] = idx
        if idx is None and (not s.get("await_confirm") or u.get("yn") is False):
            changed = has_date_info(new) or new.time or new.time_after or new.time_before or new.part_of_day
            if changed or new.asap:
                merged = merge_intent(old, new)
                if changed:
                    u.update(options=[], chosen=None)             # условия поменялись — старые варианты недействительны
                u["intent"] = merged.model_dump()
        return u

    async def n_flag_manip(self, s: S) -> dict:
        await self._tool("handoff_to_staff", {"reason": "manipulation", "summary": f"指示の埋め込みの疑い：{s['text'][:200]}"})
        return {"pending_manip": False, "manip_flagged": True, "notes": list(s.get("notes") or []) + [SAY["manip_note"]]}

    async def n_handoff(self, s: S) -> dict:
        reason = s.get("handoff_reason") or ("tool_errors" if self.tool_error_streak >= 3 else "other")
        summary = f"{reason}｜依頼：{s.get('action') or '-'}/{s.get('service') or '-'}｜患者の発話：{s['text'][:200]}"
        appt = s.get("appt") or {}
        args = {"reason": reason, "summary": summary}
        if appt.get("appointment_id"):
            args["appointment_id"] = appt["appointment_id"]
        res = await self._tool("handoff_to_staff", args)
        say = HANDOFF_SAY.get(reason, FALLBACK) if res.get("ok") else FALLBACK
        self.tool_error_streak = 0
        return self._out(s, say, handed_off=True, handoff_reason=None, await_confirm=None)

    async def n_closing(self, s: S) -> dict:
        return self._out(s, SAY["closing"])

    async def n_identify(self, s: S) -> dict:
        card, birth, reg = s.get("card"), s.get("birth"), s.get("reg") or {}
        if card and birth and not s.get("wants_new"):
            res = await self._tool("verify_identity", {"card_number": card, "birth_date": birth})
            if res.get("ok"):
                return {"verified": True, "patient_name": res.get("name", ""), "id_fails": 0, "card": None, "birth": None}
            fails = s.get("id_fails", 0) + 1
            if fails >= 2:
                return {"id_fails": fails, "handoff_reason": "other", "card": None, "birth": None}
            return self._out(s, SAY["ask_id_again"], id_fails=fails, card=None, birth=None)
        if s.get("wants_new"):
            need = [k for k in ("name", "name_kana", "phone", "birth_date") if not reg.get(k)]
            if need:
                prev = s.get("reg_need")
                if prev is not None and len(need) < prev:           # пациент продвинулся: это не повтор вопроса, счётчик не растёт
                    s = {**s, "asks": {**(s.get("asks") or {}), "reg": 0}}
                asks, over = self._ask(s, "reg")
                if over:
                    return {"asks": asks, "handoff_reason": "ambiguity"}
                if len(need) == 4:
                    say = SAY["ask_reg"]
                else:                                                # спрашиваем только недостающее
                    say = SAY["ask_reg_more"].format(fields="、".join(REG_LABEL[k] for k in need))
                return self._out(s, say, asks=asks, reg_need=len(need))
            res = await self._tool("register_new_patient", {"name": reg["name"], "name_kana": reg["name_kana"],
                                                            "phone": reg["phone"], "birth_date": reg["birth_date"]})
            if res.get("ok"):
                note = f"診察券番号は {res.get('card_number')} です。初診のご予約を承ります。"
                return {"verified": True, "is_new": True, "patient_name": res.get("name", ""), "service": "shoshin",
                        "action": s.get("action") or "book", "notes": list(s.get("notes") or []) + [note]}
            return {"handoff_reason": "other"}
        asks, over = self._ask(s, "id")
        if over:
            return {"asks": asks, "handoff_reason": "other"}
        return self._out(s, SAY["ask_id"], asks=asks)

    async def n_ask_action(self, s: S) -> dict:
        if s.get("done"):
            return self._out(s, SAY["thanks"])
        text = s["text"]
        got = self._llm_json('Classify the patient message. Return only JSON {"action": "book" | "cancel" | "reschedule" | null}. '
                             'Use null if the intent is not clearly one of these.', text).get("action")
        if got in ("book", "cancel", "reschedule"):
            return {"action": got}
        asks, over = self._ask(s, "action")
        if over:
            return {"asks": asks, "handoff_reason": "ambiguity"}
        return self._out(s, SAY["ask_action"], asks=asks)

    async def n_ask_service(self, s: S) -> dict:
        asks, over = self._ask(s, "service")
        if over:
            return {"asks": asks, "handoff_reason": "ambiguity"}
        return self._out(s, SAY["ask_service"], asks=asks)

    async def n_ask_date(self, s: S) -> dict:
        got = self._llm_json("Extract the date/time the patient wants. Return only JSON with these optional keys: " +
                             ", ".join(f"{k} ({v.get('description', '')})" for k, v in DateIntent.model_json_schema()["properties"].items()) +
                             ". Use null when not stated. Do not guess.", strip_ids(s["text"]))
        try:
            it = DateIntent.model_validate({k: v for k, v in got.items() if v is not None})
        except Exception:  # noqa: BLE001
            it = DateIntent()
        if not it.is_empty():
            return {"intent": merge_intent(self._intent(s), it).model_dump()}
        asks, over = self._ask(s, "date")
        if over:
            return {"asks": asks, "handoff_reason": "ambiguity"}
        return self._out(s, SAY["ask_date"], asks=asks)

    async def n_pick_appt(self, s: S) -> dict:
        res = await self._tool("get_my_appointments", {})
        appts = [a for a in (res.get("appointments") or []) if a.get("status") == "active"] if res.get("ok") else []
        if not res.get("ok"):
            return {}
        if not appts:
            return self._out(s, SAY["no_appts"], action=None, done=True)
        view = [{"date": a["start"][:10], "time": a["start"][11:16], **a} for a in appts]
        pick = view[0] if len(view) == 1 else None
        if pick is None:
            idx = parse_choice(strip_ids(s["text"]), view)
            pick = view[idx] if idx is not None else None
        if pick is None:
            asks, over = self._ask(s, "appt")
            if over:
                return {"asks": asks, "handoff_reason": "ambiguity"}
            lines = "\n".join(f"{i}. {self._svc(a['service']).get('label_ja', a['service'])} {fmt_slot(a['date'], a['time'])}"
                              for i, a in enumerate(view, 1))
            return self._out(s, f"どのご予約でしょうか。\n{lines}", asks=asks, appts=view)
        start = datetime.fromisoformat(pick["start"])
        if s.get("action") in ("cancel", "reschedule") and start - self.now < timedelta(hours=self._cancel_hours):
            return {"appt": pick, "handoff_reason": "late_cancel"}
        return {"appt": pick, "service": pick["service"], "appts": view}

    async def n_search(self, s: S) -> dict:
        it = self._intent(s)
        try:
            w = resolve(it, self.now)
        except ValueError:
            return self._out(s, SAY["ask_date"], intent={})
        code = s["service"]
        plans = [(w.date_from, w.date_to, w.time_from, w.time_to, False),
                 (w.date_from, w.date_from + timedelta(days=13), w.time_from, w.time_to, True),
                 (w.date_from, w.date_from + timedelta(days=13), None, None, True)]
        slots: list[tuple[str, str]] = []
        widened = False
        for d1, d2, t1, t2, wide in plans:
            args = {"service": code, "date_from": d1.isoformat(), "date_to": d2.isoformat()}
            if t1:
                args["time_from"] = t1.strftime("%H:%M")
            if t2:
                args["time_to"] = t2.strftime("%H:%M")
            res = await self._tool("search_slots", args)
            if not res.get("ok"):
                return {}
            slots = [(d["date"], t) for d in res.get("days", []) for t in d["times"]]
            if slots:
                widened = wide
                break
        if not slots:
            asks, over = self._ask(s, "nothing")
            if over:
                return {"asks": asks, "handoff_reason": "other"}
            return self._out(s, SAY["no_slots"], asks=asks, intent={})
        near: list[tuple[str, str]] = []
        if it.time:
            tm = lambda t: int(t[:2]) * 60 + int(t[3:])      # noqa: E731
            want = tm(it.time)
            slots = sorted(slots, key=lambda x: (abs(tm(x[1]) - want), x[0]))
            if widened:                                      # точного времени нет: сначала ближайшее время в тот же день
                res = await self._tool("search_slots", {"service": code, "date_from": w.date_from.isoformat(),
                                                        "date_to": w.date_to.isoformat()})
                same = [(d["date"], t) for d in res.get("days", []) for t in d["times"]] if res.get("ok") else []
                near = sorted(same, key=lambda x: (abs(tm(x[1]) - want), x[0]))[:2]
        picked = near + self._spread([x for x in slots if x not in near], 3 - len(near))
        opts = [{"date": d, "time": t} for d, t in sorted(picked)]
        minutes = self._svc(code).get("minutes")
        head = ""
        if widened or (it.time and not any(o["time"] == it.time for o in opts)):
            head = "ご希望の条件では空きがございませんでした。近い日時の候補です。\n"
        if len(opts) == 1:
            return {"options": opts, "chosen": opts[0], "reply": None, "notes": list(s.get("notes") or []) + ([head.strip()] if head else [])}
        lines = "\n".join(f"{i}. {fmt_slot(o['date'], o['time'], minutes)}" for i, o in enumerate(opts, 1))
        return self._out(s, f"{head}{self._svc(code).get('label_ja', code)}の空き状況です。\n{lines}\nご希望の番号または日時をお知らせください。",
                         options=opts, chosen=None)

    @staticmethod
    def _spread(slots: list[tuple[str, str]], n: int) -> list[tuple[str, str]]:
        """Разные дни по кругу: первый слот каждого дня, затем следующие — чтобы варианты не сидели в одном дне."""
        by_day: dict[str, list[tuple[str, str]]] = {}
        for sl in slots:
            by_day.setdefault(sl[0], []).append(sl)
        out: list[tuple[str, str]] = []
        while len(out) < n and any(by_day.values()):
            for d in list(by_day):
                if by_day[d] and len(out) < n:
                    out.append(by_day[d].pop(0))
        return out

    async def n_choose(self, s: S) -> dict:
        idx = s.get("choice_idx")
        opts = s.get("options") or []
        if idx is not None and idx < len(opts):
            return {"chosen": opts[idx]}
        asks, over = self._ask(s, "choice")
        if over:
            return {"asks": asks, "handoff_reason": "ambiguity"}
        minutes = self._svc(s["service"]).get("minutes")
        lines = "\n".join(f"{i}. {fmt_slot(o['date'], o['time'], minutes)}" for i, o in enumerate(opts, 1))
        return self._out(s, f"{SAY['ask_choice']}\n{lines}", asks=asks)

    async def n_ask_confirm(self, s: S) -> dict:
        act, appt = s["action"], s.get("appt") or {}
        if act == "cancel":
            pend = {"kind": "cancel", "appointment_id": appt["appointment_id"]}
            what = f"{self._svc(appt['service'])['label_ja']} {fmt_slot(appt['date'], appt['time'])} のご予約を"
            q = f"{what}キャンセルしてもよろしいでしょうか？（はい／いいえ）"
        else:
            ch = s["chosen"]
            minutes = self._svc(s["service"]).get("minutes")
            label = self._svc(s["service"]).get("label_ja", s["service"])
            if act == "reschedule":
                pend = {"kind": "reschedule", "appointment_id": appt["appointment_id"], "start": f"{ch['date']}T{ch['time']}"}
                q = (f"{label} {fmt_slot(appt['date'], appt['time'])} のご予約を\n→ {fmt_slot(ch['date'], ch['time'], minutes)} に"
                     f"変更してもよろしいでしょうか？（はい／いいえ）")
            else:
                pend = {"kind": "book", "service": s["service"], "start": f"{ch['date']}T{ch['time']}"}
                q = f"{label} {fmt_slot(ch['date'], ch['time'], minutes)} でご予約をお取りしてもよろしいでしょうか？（はい／いいえ）"
        return self._out(s, q, await_confirm=pend)

    async def n_confirm(self, s: S) -> dict:
        yn, pend = s.get("yn"), s["await_confirm"]
        if yn is None:
            asks, over = self._ask(s, "confirm")
            if over:
                return {"asks": asks, "handoff_reason": "ambiguity"}
            return self._out(s, SAY["ask_yesno"], asks=asks)
        if yn is False:
            it_changed = s.get("intent") is not None and (s.get("options") == [] and not s.get("chosen"))
            keep = {"await_confirm": None, "chosen": None, "options": []}
            if s.get("action") == "cancel":
                return self._out(s, "承知いたしました。キャンセルは行いません。", action=None, appt=None, done=True, **keep)
            if it_changed:
                return keep                                        # новая дата уже в состоянии → ищем заново
            return self._out(s, SAY["reset"], intent={}, **keep)
        kind = pend["kind"]
        if kind == "book":
            res = await self._tool("book_appointment", {"service": pend["service"], "start": pend["start"]})
        elif kind == "reschedule":
            res = await self._tool("reschedule_appointment", {"appointment_id": pend["appointment_id"], "new_start": pend["start"]})
        else:
            res = await self._tool("cancel_appointment", {"appointment_id": pend["appointment_id"]})
        if not res.get("ok"):
            return self._on_error(s, res)
        a = res.get("appointment") or {}
        svc = self._svc(a.get("service", pend.get("service", "")))
        minutes = svc.get("minutes")
        when = fmt_slot(a["start"][:10], a["start"][11:16], minutes) if a.get("start") else ""
        if kind == "book":
            text = f"ご予約を承りました。\n【ご予約内容】{svc.get('label_ja')} {when}\n予約番号：{a.get('appointment_id')}"
            if res.get("intake_url"):
                text += f"\n問診票は任意です。事前にこちらからご記入いただけます：{res['intake_url']}"
        elif kind == "reschedule":
            text = f"ご予約を変更いたしました。\n【変更後】{svc.get('label_ja')} {when}"
        else:
            text = f"ご予約のキャンセルを承りました。\n【キャンセルした予約】{svc.get('label_ja')} {when}"
        return self._out(s, f"{text}\n{SAY['after_done']}", await_confirm=None, action=None, service=None, intent={}, appt=None,
                         appts=[], options=[], chosen=None, asks={}, done=True)

    def _on_error(self, s: S, res: dict) -> dict:
        err = res.get("error")
        base = {"await_confirm": None, "chosen": None, "options": []}
        if err == "too_late_to_cancel":
            return {**base, "handoff_reason": "late_cancel"}
        if err == "slot_taken":
            return self._out(s, "申し訳ございません、その時間は先に埋まってしまいました。あらためて候補をお調べします。", **base)
        if err in ("not_found", "already_cancelled"):
            return self._out(s, SAY["no_appts"], action=None, appt=None, done=True, **base)
        return self._out(s, "申し訳ございません、うまく処理できませんでした。ご希望をもう一度お知らせください。", **base)
