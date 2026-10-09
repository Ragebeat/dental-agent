"""Ручной симулятор пациента: роль играет человек или Claude, обмен репликами через файлы в data/manual/.

Запуск (агент и проверки работают как обычно):
  uv run python -m dental.eval.run --sim-profile manual --scenarios s04,s08 --repeats 1 --label manual-diag

Игрок получает ровно то же, что LLM-симулятор: `persona.txt` (системный промпт роли) и реплики агента. Ожидаемых
результатов сценария он не видит и не должен читать. Команды игрока:
  python -m dental.eval.manual wait [--timeout 150]     # ждать, пока агент ответит; печатает роль (один раз) и диалог
  python -m dental.eval.manual say <имя_папки> < текст   # ответить за пациента (текст со stdin; <END> в конце = закончить)
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from datetime import datetime

from dental.agent.baseline import Usage
from dental.agent.llm import LLMError
from dental.config import ROOT
from dental.eval.simulator import END, build_system
from dental.scenarios import Scenario

MANUAL_DIR = ROOT / "data" / "manual"


def _atomic_write(path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


class ManualSimulator:
    def __init__(self, sc: Scenario, now: datetime, *, timeout_s: float = 3600, poll_s: float = 1.0) -> None:
        self.usage = Usage()
        self.timeout_s, self.poll_s, self.turn = timeout_s, poll_s, 0
        self.dir = MANUAL_DIR / f"{datetime.now():%H%M%S}-{sc.id.split('_')[0]}-{uuid.uuid4().hex[:4]}"
        self.dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.dir / "persona.txt", build_system(sc, now))
        self._status("awaiting_patient")

    def _status(self, state: str) -> None:
        _atomic_write(self.dir / "status.json", json.dumps({"state": state, "turn": self.turn, "dir": self.dir.name}))

    def _wait(self) -> tuple[str, bool]:
        path = self.dir / f"turn_{self.turn}_patient.txt"
        t0 = time.monotonic()
        while not path.exists():
            if time.monotonic() - t0 > self.timeout_s:
                self._status("timeout")
                raise LLMError(f"ручной симулятор: нет ответа за {self.timeout_s:.0f} с ({self.dir.name})")
            time.sleep(self.poll_s)
        raw = path.read_text(encoding="utf-8").strip()
        ended = END in raw
        text = raw.replace(END, "").strip()
        if not text and not ended:
            text = "……"
        self._status("done" if ended else "working")
        return text, ended

    def first(self) -> tuple[str, bool]:
        return self._wait()

    def respond(self, agent_text: str) -> tuple[str, bool]:
        self.turn += 1
        _atomic_write(self.dir / f"turn_{self.turn}_agent.txt", agent_text)
        self._status("awaiting_patient")
        return self._wait()


# ---------- команды игрока ----------
def _dialog_text(d) -> str:
    out, n = [], 0
    while (d / f"turn_{n}_patient.txt").exists() or (d / f"turn_{n}_agent.txt").exists():
        if n and (d / f"turn_{n}_agent.txt").exists():
            out.append("АГЕНТ: " + (d / f"turn_{n}_agent.txt").read_text(encoding="utf-8").strip())
        if (d / f"turn_{n}_patient.txt").exists():
            out.append("ВЫ: " + (d / f"turn_{n}_patient.txt").read_text(encoding="utf-8").strip())
        n += 1
    return "\n".join(out)


def _waiting():
    for st in sorted(MANUAL_DIR.glob("*/status.json")):
        try:
            s = json.loads(st.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        d = st.parent
        if s["state"] == "awaiting_patient" and not (d / f"turn_{s['turn']}_patient.txt").exists():
            yield d, s


def cmd_wait(timeout: float) -> int:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        for d, s in _waiting():
            print(f"### {d.name}  (реплика №{s['turn']})")
            shown = d / ".persona_shown"
            if not shown.exists():
                print("--- РОЛЬ (играйте только её) ---\n" + (d / "persona.txt").read_text(encoding="utf-8"))
                shown.write_text("1")
            print("--- ДИАЛОГ ---\n" + _dialog_text(d))
            print(f"--- ответьте: python -m dental.eval.manual say {d.name} ---")
            return 0
        time.sleep(2)
    print("NOTHING (никто не ждёт ответа)")
    return 1


def cmd_say(name: str) -> int:
    d = MANUAL_DIR / name
    s = json.loads((d / "status.json").read_text(encoding="utf-8"))
    path = d / f"turn_{s['turn']}_patient.txt"
    if s["state"] != "awaiting_patient" or path.exists():
        print("сейчас этот диалог ответа не ждёт")
        return 2
    text = sys.stdin.read().strip()
    if not text:
        print("пустая реплика")
        return 2
    _atomic_write(path, text)
    print(f"записано: {path.name}")
    return 0


def main(argv=None) -> int:
    a = list(sys.argv[1:] if argv is None else argv)
    if a[:1] == ["wait"]:
        return cmd_wait(float(a[a.index("--timeout") + 1]) if "--timeout" in a else 150)
    if a[:1] == ["say"] and len(a) == 2:
        return cmd_say(a[1])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
