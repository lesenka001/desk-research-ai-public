"""Web: one page with the latest audience-research statistics."""

import html
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import db

SERVICE = "web"

PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Навигатор достижимости ЦА</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; margin: 0; background: #f6f5f1; color: #1a1a1a; }}
  main {{ max-width: 960px; margin: 0 auto; padding: 24px 16px; }}
  h1 {{ font-size: 28px; margin: 0 0 4px; }}
  .muted {{ color: #6b6b6b; font-size: 14px; }}
  .map {{ display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 24px 0; }}
  .block {{ background: #fff; border: 1px solid #1a1a1a; padding: 12px 14px; min-width: 150px; }}
  .block b {{ display: block; font-size: 16px; }}
  .block span {{ font-size: 13px; color: #6b6b6b; }}
  .arrow {{ font-size: 22px; }}
  .stats {{ display: flex; gap: 24px; flex-wrap: wrap; margin: 8px 0 24px; font-size: 15px; }}
  table {{ width: 100%; border-collapse: collapse; background: #fff; font-size: 14px; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #e3e1da; vertical-align: top; }}
  th {{ font-weight: 600; background: #ecebe5; }}
  a {{ color: #1a1a1a; }}
  .tag {{ background: #e8f5c8; padding: 1px 6px; white-space: nowrap; }}
</style></head>
<body><main>
<h1>Навигатор достижимости ЦА</h1>
<div class="muted">Открытые исследования и статистика, потенциально полезные для оценки целевых аудиторий.</div>

<div class="map">
  <div class="block"><b>Сбор</b><span>collector: обходит источники</span></div><div class="arrow">&rarr;</div>
  <div class="block"><b>База</b><span>Postgres: хранит все</span></div><div class="arrow">&rarr;</div>
  <div class="block"><b>Обработка</b><span>worker: извлекает проверяемые показатели</span></div><div class="arrow">&rarr;</div>
  <div class="block"><b>Страница</b><span>web: показывает находки</span></div>
</div>

<div class="stats">
  <div>Источников: <b>{sources}</b></div>
  <div>Материалов: <b>{materials}</b></div>
  <div>Находок: <b>{findings}</b></div>
  <div>Последний сбор: <b>{last_run}</b></div>
</div>

<table>
<tr><th>Когда</th><th>Источник</th><th>Тип</th><th>Для IR</th><th>Показатель</th><th>Аудитория и база</th><th>Находка</th></tr>
{rows}
</table>
</main></body></html>"""

ROW = """<tr><td>{date}</td><td>{source}</td><td>{kind}</td><td>{applicability}</td><td>{value}</td>
<td>{audience}</td><td><a href="{url}" target="_blank">{summary}</a></td></tr>"""

TYPE_LABELS = {
    "population_share": "доля аудитории",
    "conditional_share": "доля в подгруппе",
    "absolute_count": "абсолютное число",
    "market_metric": "рыночный показатель",
    "other": "другое",
}

IR_LABELS = {
    "direct": "прямо",
    "indirect": "косвенно",
    "context_only": "только контекст",
}


def render(conn):
    sources, materials, findings = conn.execute(
        "SELECT (SELECT count(*) FROM sources), (SELECT count(*) FROM materials), (SELECT count(*) FROM findings)"
    ).fetchone()
    last = conn.execute(
        "SELECT status, started_at FROM runs WHERE service = 'collector' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    last_run = f"{last[1]:%d.%m %H:%M} UTC, {last[0]}" if last else "еще не было"
    rows = conn.execute(
        """SELECT m.published_at, s.name, f.indicator_type, f.ir_applicability, f.value,
                  f.audience, f.percent_base, f.summary, m.url
           FROM findings f
           JOIN materials m ON m.id = f.material_id
           JOIN sources s ON s.id = m.source_id
           WHERE m.processed_prompt_version IS NULL
              OR f.prompt_version = m.processed_prompt_version
           ORDER BY m.published_at DESC NULLS LAST LIMIT 100"""
    ).fetchall()
    rows_html = (
        "\n".join(
            ROW.format(
                date=f"{published:%d.%m %H:%M}" if published else "",
                source=html.escape(source),
                kind=f'<span class="tag">{html.escape(TYPE_LABELS.get(indicator_type, indicator_type or "—"))}</span>',
                applicability=html.escape(IR_LABELS.get(ir_applicability, ir_applicability or "—")),
                value=html.escape(value or "—"),
                audience="<br>".join(
                    part
                    for part in (
                        html.escape(audience) if audience else "",
                        f'<span class="muted">База: {html.escape(percent_base)}</span>'
                        if percent_base
                        else "",
                    )
                    if part
                )
                or "—",
                summary=html.escape(summary),
                url=html.escape(url, quote=True),
            )
            for published, source, indicator_type, ir_applicability, value, audience, percent_base, summary, url in rows
        )
        or '<tr><td colspan="7">Находок пока нет</td></tr>'
    )
    return PAGE.format(
        sources=sources,
        materials=materials,
        findings=findings,
        last_run=last_run,
        rows=rows_html,
    )


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            # healthy only when the database answers too
            try:
                with db.connect() as conn:
                    conn.execute("SELECT 1")
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
            except Exception as error:
                db.log(SERVICE, f"проверка упала: {error}")
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b"database unavailable")
            return
        try:
            with db.connect() as conn:
                body = render(conn).encode()
            self.send_response(200)
        except Exception as error:
            db.log(SERVICE, f"страница упала: {error}")
            body = "Страница временно недоступна".encode()
            self.send_response(500)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        db.log(SERVICE, f"запрос {self.command} {self.path}")


def main():
    with db.connect() as conn:
        db.init_schema(conn)
    port = int(os.environ.get("PORT", 8000))
    db.log(SERVICE, f"страница открыта на порту {port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
