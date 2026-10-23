"""Patient simulator: an LLM plays the patient from the scenario's persona_ja. It knows nothing of the agent's tools or rules.

The role prompt below stays in Russian on purpose: it is the input of every logged run, and changing
its wording would change the simulator's behaviour and make new runs incomparable with the old ones."""

from __future__ import annotations

from datetime import datetime

from dental.agent.baseline import WEEKDAYS, Usage
from dental.agent.llm import LLM
from dental.scenarios import Scenario, render

END = "<END>"

STYLE_HINTS = {
    "keigo": "очень вежливая речь (敬語)",
    "casual": "разговорная речь без вежливых форм, как с знакомым",
    "rude": "грубый, раздражённый тон, короткие резкие фразы (без оскорблений и угроз)",
    "typos": "много опечаток и пропущенных символов, как в быстрой переписке с телефона",
    "halfwidth": "цифры и катакана полуширинные (半角), без знаков препинания в конце",
    "vague": "расплывчатые формулировки, сам не до конца знаешь, чего хочешь",
}

SYSTEM = """\
Ты играешь ПАЦИЕНТА, который пишет в чат стоматологической клиники. Это ролевая игра для тестирования.
Пиши только по-японски, одной короткой репликой за раз (1–3 предложения), от первого лица.
Никогда не упоминай, что это тест, симуляция, инструкции или что ты ИИ. Не пиши ремарок и пояснений в скобках.
Знай только то, что указано в описании; чего там нет, того ты не помнишь и не выдумываешь (скажи «覚えていません»).
Сообщай личные данные (номер карты, дату рождения, телефон) только когда тебя об этом просят.
Когда твоя цель достигнута, ты окончательно отказался или разговор зашёл в тупик, добавь в конец последней реплики {end}.

Текущая дата (Asia/Tokyo): {now} ({weekday}).
Манера речи: {style}.

ОПИСАНИЕ ТВОЕЙ РОЛИ:
{persona}
{scripted}"""


def build_system(sc: Scenario, now: datetime) -> str:
    """Everything the simulator sees about its role. Manual mode gives the human/Claude exactly this text and nothing more."""
    styles = [sc.style] if isinstance(sc.style, str) else sc.style
    scripted = ""
    if sc.scripted_lines_ja:
        lines = "\n".join(f"- {render(x)}" for x in sc.scripted_lines_ja)
        scripted = f"\nВ подходящий момент ОБЯЗАТЕЛЬНО скажи дословно эти реплики:\n{lines}\n"
    return SYSTEM.format(end=END, now=now.strftime("%Y-%m-%d %H:%M"), weekday=WEEKDAYS[now.weekday()],
                         style="; ".join(STYLE_HINTS[s] for s in styles), persona=render(sc.persona_ja).strip(),
                         scripted=scripted)


class Simulator:
    def __init__(self, llm: LLM, sc: Scenario, now: datetime) -> None:
        self.llm = llm
        self.messages = [{"role": "system", "content": build_system(sc, now)}]
        self.usage = Usage()

    def _next(self, incoming: str) -> tuple[str, bool]:
        self.messages.append({"role": "user", "content": incoming})
        raw = ""
        for _ in range(3):                  # the simulator model sometimes returns an empty answer — not a patient message, ask again
            r = self.llm.chat(self.messages)
            self.usage.add(r)
            raw = (r.text or "").strip()
            if raw:
                break
        self.messages.append({"role": "assistant", "content": raw})
        ended = END in raw
        text = raw.replace(END, "").strip()
        if not text and not ended:
            text = "……"
        return text, ended

    def first(self) -> tuple[str, bool]:
        return self._next("（あなたはクリニックのチャットを開きました。最初のメッセージを書いてください。）")

    def respond(self, agent_text: str) -> tuple[str, bool]:
        return self._next(agent_text)
