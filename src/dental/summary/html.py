"""One self-contained HTML page: the JA/RU summary (switchable), tables with the real data, the check."""

from __future__ import annotations

import html
import re
from datetime import datetime

from dental.summary.facts import Facts
from dental.summary.text import REASON, SERVICE_RU
from dental.summary.writer import Section

CSS = """:root{--bg:#fafaf7;--fg:#1d2220;--mut:#66706b;--card:#fff;--line:#dfe3df;--acc:#1f6f5c;--warn:#b4531b}
@media (prefers-color-scheme:dark){:root{--bg:#161a18;--fg:#e8ece9;--mut:#9aa6a0;--card:#1e2421;--line:#333c37;--acc:#5ec2a6;--warn:#e29a63}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.6 system-ui,"Hiragino Sans","Noto Sans JP",sans-serif}
main{max-width:860px;margin:0 auto;padding:24px 16px 56px}h1{font-size:1.4rem;margin:0}h2{font-size:1.05rem;margin:32px 0 8px}
.top{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}.sub{color:var(--mut);font-size:.9rem}
button{font:inherit;background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:6px 14px;cursor:pointer}
button[aria-pressed=true]{background:var(--acc);border-color:var(--acc);color:#fff}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 20px;white-space:pre-line}
.ref{color:var(--acc);font-weight:600;font-size:.85em}table{width:100%;border-collapse:collapse;font-size:.92rem;background:var(--card);border:1px solid var(--line);border-radius:8px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line)}th{color:var(--mut);font-weight:600}.stale{color:var(--warn);font-weight:600}
.note{color:var(--mut);font-size:.82rem;margin-top:8px}main [lang]:not(.on){display:none}"""

JS = """const b=document.querySelectorAll('[data-l]');function s(l){document.querySelectorAll('main [lang]').forEach(e=>e.classList.toggle('on',e.lang===l));
b.forEach(x=>x.setAttribute('aria-pressed',x.dataset.l===l))}
b.forEach(x=>x.addEventListener('click',()=>s(x.dataset.l)));s('ja')"""


def _fmt(text: str) -> str:
    return re.sub(r"\[([ABCHN]\d+)\]", r'<span class="ref">[\1]</span>', html.escape(text))


def render(f: Facts, sections: dict[str, Section]) -> str:
    e = html.escape
    rows_a = "".join(f"<tr><td>{a['start'][11:16]}</td><td>{e(a['label_ja'])} / {e(SERVICE_RU.get(a['service'], ''))}</td>"
                     f"<td>{e(a['name'])}（{a['card']}）</td><td>A{a['id']}</td></tr>" for a in f.today_appts) \
        or '<tr><td colspan=4>—</td></tr>'
    rows_h = "".join(f"<tr><td>H{h['id']}</td><td>{e(REASON['ja'][h['reason']])} / {e(REASON['ru'][h['reason']])}</td>"
                     f"<td class=\"{'stale' if h['stale'] else ''}\">{h['age_h']}h</td><td>{e(h['summary'])}</td></tr>"
                     for h in f.handoffs) or '<tr><td colspan=4>—</td></tr>'
    secs = "".join(f'<div class="card" lang="{lang}">{_fmt(s.text)}</div>' for lang, s in sections.items())
    notes = "".join(f'<span lang="{lang}">{"AI（LLM）作成・事実と照合済み" if lang == "ja" else "Текст написан ИИ и сверен с фактами"}'
                    f'{"" if s.source == "llm" else (" — LLM не прошёл проверку, показан шаблон" if lang == "ja" else " — LLM не прошёл проверку, показан шаблон")}'
                    f" · recall {s.check.recall:.0%} · fabricated {s.check.fabricated}</span>" for lang, s in sections.items())
    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>朝のサマリー {f.today.isoformat()}</title><style>{CSS}</style></head><body><main>
<div class="top"><div><h1>朝のサマリー / Утренняя сводка</h1><div class="sub">{f.today.isoformat()} · 作成 {f.now.strftime('%H:%M')} JST · 集計対象 / события с {f.since.strftime('%m/%d %H:%M')}</div></div>
<div><button data-l="ja">日本語</button> <button data-l="ru">Русский</button></div></div>
<h2>サマリー / Сводка</h2>{secs}<div class="note">{notes}</div>
<h2>本日の予約 / Приёмы на сегодня</h2><table><tr><th>時刻</th><th>診療 / услуга</th><th>患者 / пациент</th><th>ID</th></tr>{rows_a}</table>
<h2>対応待ち / Нерешённые передачи</h2><table><tr><th>ID</th><th>理由 / причина</th><th>経過</th><th>メモ / заметка агента</th></tr>{rows_h}</table>
<div class="note">[A…] 予約 · [C…] キャンセル · [N…] 新規患者(診察券) · [B…] 新規予約 · [H…] 引き継ぎ</div>
</main><script>{JS}</script></body></html>"""
