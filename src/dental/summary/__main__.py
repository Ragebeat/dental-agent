"""uv run python -m dental.summary [--date 2026-10-07] [--profile local] [--out data/summary/2026-10-07.html]

Сводка — утренняя (08:30 JST) и описывает СЕГОДНЯ; в закрытый день не строится (--force). Без --date берётся дата замороженного «сейчас».
База — из DATABASE_URL."""

from __future__ import annotations

import argparse
from datetime import datetime, time
from pathlib import Path

from dental.agent.llm import get_llm
from dental.config import ROOT, load_config
from dental.core.service import ClinicCore
from dental.summary.facts import collect
from dental.summary.html import render
from dental.summary.writer import write


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD: сводка на утро этого дня (08:30 JST)")
    ap.add_argument("--force", action="store_true", help="строить, даже если клиника в этот день закрыта")
    ap.add_argument("--profile", default="agent", help="профиль LLM из llm.toml (по умолчанию роль agent)")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    cfg = load_config()
    now = cfg.now()
    day = datetime.fromisoformat(a.date).date() if a.date else now.date()
    now = datetime.combine(day, time(8, 30), tzinfo=now.tzinfo)
    f = collect(ClinicCore(), now)
    if not f.open_today and not a.force:
        print(f"{day}: клиника закрыта, сводка не строится (--force, чтобы построить)")
        return
    llm = get_llm(a.profile)
    sections = {lang: write(f, lang, llm) for lang in ("ja", "ru")}
    out = Path(a.out) if a.out else ROOT / "data" / "summary" / f"{now.date().isoformat()}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(f, sections), encoding="utf-8")
    for lang, s in sections.items():
        print(f"{lang}: {s.source}, попыток {s.attempts}, recall {s.check.recall:.0%}, выдуманных {s.check.fabricated}")
    print("записано:", out)


if __name__ == "__main__":
    main()
