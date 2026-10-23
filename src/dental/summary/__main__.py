"""uv run python -m dental.summary [--date 2026-10-07] [--profile local] [--out data/summary/2026-10-07.html]

The summary is a morning one (08:30 JST) and describes TODAY; on a closed day it is not built (--force). Without --date the frozen "now" date is used.
The database comes from DATABASE_URL."""

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
    ap.add_argument("--date", help="YYYY-MM-DD: the summary for the morning of this day (08:30 JST)")
    ap.add_argument("--force", action="store_true", help="build even if the clinic is closed that day")
    ap.add_argument("--profile", default="agent", help="LLM profile from llm.toml (the agent role by default)")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    cfg = load_config()
    now = cfg.now()
    day = datetime.fromisoformat(a.date).date() if a.date else now.date()
    now = datetime.combine(day, time(8, 30), tzinfo=now.tzinfo)
    f = collect(ClinicCore(), now)
    if not f.open_today and not a.force:
        print(f"{day}: the clinic is closed, no summary (use --force to build one)")
        return
    llm = get_llm(a.profile)
    sections = {lang: write(f, lang, llm) for lang in ("ja", "ru")}
    out = Path(a.out) if a.out else ROOT / "data" / "summary" / f"{now.date().isoformat()}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(f, sections), encoding="utf-8")
    for lang, s in sections.items():
        print(f"{lang}: {s.source}, attempts {s.attempts}, recall {s.check.recall:.0%}, fabricated {s.check.fabricated}")
    print("written:", out)


if __name__ == "__main__":
    main()
