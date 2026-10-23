"""An LLM writes the summary text from the facts; code checks it. On failure — up to 2 retries listing the errors, then the template."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from dental.agent.llm import LLM, LLMError
from dental.summary.facts import Facts
from dental.summary.text import Check, template, verify

LANG_NAME = {"ja": "Japanese (natural, polite business style)", "ru": "Russian"}

# gemma spends part of the budget on "thinking"; at 1024 the Japanese answer was cut off (finish=length) — headroom is needed
MAX_TOKENS = 4096

SYSTEM = """You write a short morning briefing for the owner of a dental clinic, in {lang}.
Use ONLY the facts in the JSON the user sends. Rules:
- 5 to 9 short lines, one idea per line, no markdown, no headings, no bullet symbols.
- Mention today_total (the number of appointments today) and the free start times if any. Events are those since the last closing of the clinic.
- Every cancellation, new patient, new booking and open handoff in the JSON MUST be mentioned, and each mention must end with its ref in square brackets exactly as given, e.g. [C640] [N100010] [B12] [H3]. Refs of today's appointments (A...) are optional.
- Never write patient names, phones or birth dates (you do not have them). Never invent facts or numbers. Use the service names exactly as given in the JSON (do not transliterate or translate them), and keep each number next to its own service. Do not give advice, except one line pointing at handoffs with stale=true.
- If a category is empty, say in one short line that there are none; do not write refs for it."""


@dataclass
class Section:
    lang: str
    text: str
    source: str            # "llm" | "template"
    check: Check
    attempts: int
    first_text: str = ""
    first_finish: str = ""
    first: Check | None = None      # the check of the LLM's very first attempt (before retries and the template) — the honest model metric


def _clean(s: str) -> str:
    s = re.sub(r"^```\w*\n|\n```$", "", s.strip())
    s = re.sub(r"^\s*(?:[-*•・]|\d+[.)])\s*", "", s, flags=re.M)
    return s.strip()


def write(f: Facts, lang: str, llm: LLM, retries: int = 2) -> Section:
    payload = json.dumps(f.llm_view(lang), ensure_ascii=False, indent=1)
    need = f.must_refs()
    if need:  # an explicit checklist: the model cannot "overlook" an event
        payload += ("\n\nChecklist: the briefing is incomplete unless it mentions ALL of these "
                    f"({len(need)}), each with its ref in square brackets: " + ", ".join(f"[{r}]" for r in need))
    messages = [{"role": "system", "content": SYSTEM.format(lang=LANG_NAME[lang])},
                {"role": "user", "content": payload}]
    attempts, first, first_text, first_fin = 0, None, "", ""
    for _ in range(retries + 1):
        attempts += 1
        try:
            rep = llm.chat(messages, max_tokens=MAX_TOKENS)
            text = _clean(rep.text or "")
        except LLMError:
            break
        chk = verify(text, f)
        if first is None:
            first, first_text, first_fin = chk, text, rep.finish
        if chk.ok:
            return Section(lang, text, "llm", chk, attempts, first_text, first_fin, first)
        problems = []
        if chk.missing:
            problems.append("missing refs: " + ", ".join(chk.missing))
        if chk.unknown_refs:
            problems.append("unknown refs (remove): " + ", ".join(chk.unknown_refs))
        if chk.bad_numbers:
            problems.append("numbers not in the facts (remove): " + ", ".join(map(str, chk.bad_numbers)))
        if chk.bad_pairs:
            problems.append("service/number pairs that contradict the facts (fix): " + ", ".join(chk.bad_pairs))
        if not chk.total_ok:
            problems.append(f"today_total={f.today_total} must appear as a number")
        messages = messages + [{"role": "assistant", "content": text},
                               {"role": "user", "content": "Fix and rewrite the whole briefing. " + "; ".join(problems)}]
    tpl = template(f, lang)
    return Section(lang, tpl, "template", verify(tpl, f), attempts, first_text, first_fin, first)
