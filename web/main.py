"""Web: one page with the latest audience-research statistics."""

import html
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import db
from bot.main import (
    CURRENT_AUDIENCE_FINDING_SQL,
    FINDING_ROLE_LABELS,
    finding_role,
)

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
  .stat-group {{ margin: 0 0 22px; }}
  .stat-group h2 {{ font-size: 16px; margin: 0 0 8px; }}
  table {{ width: 100%; border-collapse: collapse; background: #fff; font-size: 14px; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #e3e1da; vertical-align: top; }}
  th {{ font-weight: 600; background: #ecebe5; }}
  a {{ color: #1a1a1a; }}
  .tag {{ background: #e8f5c8; padding: 1px 6px; white-space: nowrap; }}
  .material-row td {{ background: #1a1a1a; color: #fff; padding-top: 12px; padding-bottom: 12px; }}
  .material-row a {{ color: #fff; font-weight: 700; }}
  .block-row td {{ background: #dddcd5; font-weight: 700; }}
  .ok {{ color: #276214; }}
  .error {{ color: #9b251b; }}
  pre {{ background: #222; color: #f4f4f0; padding: 14px; overflow: auto; white-space: pre-wrap; }}
  nav {{ margin: 14px 0 24px; }}
</style></head>
<body><main>
<h1>Навигатор достижимости ЦА</h1>
<div class="muted">Открытые исследования и статистика, потенциально полезные для оценки целевых аудиторий.</div>
<nav><a href="/">Обзор</a> · <a href="#materials">Материалы</a></nav>

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

<section class="stat-group">
  <h2>Роли находок</h2>
  <div class="stats">
    <div>Базовые: <b>{base_findings}</b></div>
    <div>Производные: <b>{derived_findings}</b></div>
    <div>Условные: <b>{conditional_findings}</b></div>
  </div>
</section>

<section class="stat-group">
  <h2>Статусы материалов</h2>
  <div class="stats">
    <div>Новые: <b>{new_materials}</b></div>
    <div>Готовы: <b>{done_materials}</b></div>
    <div>Пропущены: <b>{skipped_materials}</b></div>
    <div>С ошибкой: <b>{failed_materials}</b></div>
  </div>
</section>

<section class="stat-group">
  <h2>Попытки модели</h2>
  <div class="stats">
    <div>Успешные: <b>{successful_attempts}</b></div>
    <div>Ошибки модели: <b>{model_error_attempts}</b></div>
    <div>Без находок: <b>{validation_empty_attempts}</b></div>
    <div>Принято кандидатов: <b>{accepted_candidates}</b></div>
    <div>Отклонено кандидатов: <b>{rejected_candidates}</b></div>
  </div>
</section>

<section class="stat-group" id="materials">
  <h2>Материалы и диагностика</h2>
  <table>
    <tr><th>Материал</th><th>Статус</th><th>Версия</th><th>Кандидаты</th><th>Итоговые находки</th><th>Ошибка</th></tr>
    {material_rows}
  </table>
</section>

<table>
<tr><th>Когда</th><th>Источник</th><th>Тип</th><th>Для IR</th><th>Показатель</th><th>Аудитория и база</th><th>Находка</th></tr>
{rows}
</table>
</main></body></html>"""

ROW = """<tr><td>{date}</td><td>{source}</td><td>{kind}</td><td>{applicability}</td><td>{value}</td>
<td>{audience}</td><td>{summary}{calculation}</td></tr>"""

IR_LABELS = {
    "direct": "прямо",
    "indirect": "косвенно",
    "context_only": "только контекст",
}


def render(conn):
    sources, materials, findings = conn.execute(
        "SELECT (SELECT count(*) FROM sources), (SELECT count(*) FROM materials),"
        " (SELECT count(*) FROM findings f JOIN materials m ON m.id = f.material_id WHERE "
        + CURRENT_AUDIENCE_FINDING_SQL
        + ")"
    ).fetchone()
    role_counts = dict(
        conn.execute(
            "SELECT CASE WHEN f.is_derived THEN 'derived'"
            " WHEN f.parent_finding_index IS NULL"
            " AND f.base_kind IN ('targetable_explicit','targetable_inferred') THEN 'base'"
            " ELSE 'conditional' END AS role, count(*) FROM findings f"
            " JOIN materials m ON m.id = f.material_id WHERE "
            + CURRENT_AUDIENCE_FINDING_SQL
            + " GROUP BY role"
        ).fetchall()
    )
    material_statuses = dict(
        conn.execute(
            "SELECT status, count(*) FROM materials GROUP BY status"
        ).fetchall()
    )
    attempt_counts = dict(
        conn.execute(
            "SELECT status, count(*) FROM processing_attempts GROUP BY status"
        ).fetchall()
    )
    candidate_counts = conn.execute(
        "SELECT count(*) FILTER (WHERE accepted),"
        " count(*) FILTER (WHERE NOT accepted) FROM finding_candidates"
    ).fetchone() or (0, 0)
    last = conn.execute(
        "SELECT status, started_at FROM runs WHERE service = 'collector' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    last_run = f"{last[1]:%d.%m %H:%M} UTC, {last[0]}" if last else "еще не было"
    rows = conn.execute(
        """SELECT m.published_at, s.name, m.id, m.title, m.url,
                  f.block_index, f.block_position, f.block_title,
                  f.indicator_type, f.ir_applicability, f.value,
                  f.audience, f.percent_base, f.summary, f.calculation_formula,
                  f.parent_finding_index, f.base_kind, f.is_derived,
                  f.calculation_chain, f.finding_index
           FROM findings f
           JOIN materials m ON m.id = f.material_id
           JOIN sources s ON s.id = m.source_id
           WHERE """
        + CURRENT_AUDIENCE_FINDING_SQL
        + """
           ORDER BY m.published_at DESC NULLS LAST, m.id DESC,
                    f.block_index,
                    CASE WHEN f.is_derived THEN 1
                         WHEN f.parent_finding_index IS NULL
                          AND f.base_kind IN ('targetable_explicit', 'targetable_inferred') THEN 0
                         ELSE 2 END,
                    CASE WHEN f.is_derived AND f.calculation_chain IS NOT NULL
                         THEN jsonb_array_length(f.calculation_chain) ELSE 0 END,
                    f.block_position, f.id LIMIT 100"""
    ).fetchall()
    materials_rows = conn.execute(
        """SELECT m.id, s.name, m.title, m.url, m.published_at, m.status,
                  m.processed_prompt_version, m.processing_error,
                  COALESCE(a.candidate_count, 0), COALESCE(a.accepted_count, 0),
                  COALESCE(a.rejected_count, 0),
                  count(f.id) FILTER (WHERE f.prompt_version = m.processed_prompt_version)
           FROM materials m
           JOIN sources s ON s.id = m.source_id
           LEFT JOIN LATERAL (
             SELECT candidate_count, accepted_count, rejected_count
             FROM processing_attempts pa WHERE pa.material_id = m.id
             ORDER BY pa.id DESC LIMIT 1
           ) a ON true
           LEFT JOIN findings f ON f.material_id = m.id
           GROUP BY m.id, s.name, a.candidate_count, a.accepted_count, a.rejected_count
           ORDER BY m.published_at DESC NULLS LAST, m.id DESC LIMIT 100"""
    ).fetchall()
    rendered_rows = []
    current_material = None
    current_block = None
    for (
        published,
        source,
        material_id,
        title,
        url,
        block_index,
        block_position,
        block_title,
        indicator_type,
        ir_applicability,
        value,
        audience,
        percent_base,
        summary,
        calculation_formula,
        parent_finding_index,
        base_kind,
        is_derived,
        calculation_chain,
        finding_index,
    ) in rows:
        role = finding_role(
            {
                "parent_finding_index": parent_finding_index,
                "base_kind": base_kind,
                "is_derived": is_derived,
            }
        )
        if material_id != current_material:
            rendered_rows.append(
                '<tr class="material-row"><td colspan="7">'
                f'<a href="{html.escape(url, quote=True)}" target="_blank">{html.escape(title)}</a>'
                f' · {html.escape(source)}</td></tr>'
            )
            current_material = material_id
            current_block = None
        block_key = (material_id, block_index or 1)
        if block_key != current_block:
            rendered_rows.append(
                f'<tr class="block-row"><td colspan="7">{html.escape(block_title or "Находки")}</td></tr>'
            )
            current_block = block_key
        rendered_rows.append(
            ROW.format(
                date=f"{published:%d.%m %H:%M}" if published else "",
                source=html.escape(source),
                kind=f'<span class="tag">{html.escape(FINDING_ROLE_LABELS[role])}</span>',
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
                calculation=(
                    f'<div class="muted">Расчёт: {html.escape(calculation_formula)}</div>'
                    if calculation_formula
                    else ""
                ),
            )
        )
    rows_html = "\n".join(rendered_rows) or '<tr><td colspan="7">Находок пока нет</td></tr>'
    material_rows = []
    for (
        material_id,
        source,
        title,
        url,
        published,
        status,
        prompt_version,
        processing_error,
        candidate_count,
        accepted_count,
        rejected_count,
        finding_count,
    ) in materials_rows:
        title_link = (
            f'<a href="/materials/{material_id}">{html.escape(title)}</a>'
            f'<div class="muted">{html.escape(source)}'
            + (f" · {published:%d.%m.%Y}" if published else "")
            + f' · <a href="{html.escape(url, quote=True)}" target="_blank">источник</a></div>'
        )
        material_rows.append(
            "<tr>"
            f"<td>{title_link}</td><td>{html.escape(status or '—')}</td>"
            f"<td>{html.escape(prompt_version or '—')}</td>"
            f"<td>{candidate_count}: <span class=\"ok\">{accepted_count} принято</span>, "
            f"<span class=\"error\">{rejected_count} отклонено</span></td>"
            f"<td>{finding_count}</td><td>{html.escape(processing_error or '—')}</td></tr>"
        )
    material_rows_html = "\n".join(material_rows) or '<tr><td colspan="6">Материалов пока нет</td></tr>'
    return PAGE.format(
        sources=sources,
        materials=materials,
        findings=findings,
        base_findings=role_counts.get("base", 0),
        derived_findings=role_counts.get("derived", 0),
        conditional_findings=role_counts.get("conditional", 0),
        new_materials=material_statuses.get("new", 0),
        done_materials=material_statuses.get("done", 0),
        skipped_materials=material_statuses.get("skipped", 0),
        failed_materials=material_statuses.get("failed", 0),
        successful_attempts=attempt_counts.get("success", 0),
        model_error_attempts=attempt_counts.get("model_error", 0),
        validation_empty_attempts=attempt_counts.get("validation_empty", 0),
        accepted_candidates=candidate_counts[0],
        rejected_candidates=candidate_counts[1],
        last_run=last_run,
        material_rows=material_rows_html,
        rows=rows_html,
    )


DETAIL_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — диагностика</title><style>{style}</style></head><body><main>
<nav><a href="/">← К обзору</a></nav><h1>{title}</h1>
<p>{source} · статус: <b>{status}</b> · версия: <b>{version}</b> · <a href="{url}" target="_blank">источник</a></p>
<h2>Исходный текст</h2><pre>{text}</pre>
<h2>Попытки модели</h2>{attempts}
<h2>Кандидаты</h2>{candidates}
<h2>Итоговые находки</h2>{findings}
</main></body></html>"""

DETAIL_STYLE = """body{font-family:-apple-system,system-ui,sans-serif;margin:0;background:#f6f5f1;color:#1a1a1a}main{max-width:1100px;margin:auto;padding:24px 16px}table{width:100%;border-collapse:collapse;background:#fff;font-size:14px;margin-bottom:24px}th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #ddd;vertical-align:top}th{background:#ecebe5}pre{background:#222;color:#f4f4f0;padding:14px;overflow:auto;white-space:pre-wrap}.ok{color:#276214}.error{color:#9b251b}a{color:#1a1a1a}"""


def _table(headers, rows, empty):
    if not rows:
        return f"<p>{html.escape(empty)}</p>"
    head = "".join(f"<th>{html.escape(item)}</th>" for item in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows
    )
    return f"<table><tr>{head}</tr>{body}</table>"


def render_material(conn, material_id):
    material = conn.execute(
        """SELECT m.id, s.name, m.title, m.url, m.status,
                  m.processed_prompt_version, m.text
           FROM materials m JOIN sources s ON s.id = m.source_id WHERE m.id = %s""",
        (material_id,),
    ).fetchone()
    if not material:
        return None
    attempts = conn.execute(
        """SELECT id, created_at, prompt_version, model, structured_mode, status,
                  finish_reason, candidate_count, accepted_count, rejected_count,
                  error, raw_response
           FROM processing_attempts WHERE material_id = %s ORDER BY id DESC""",
        (material_id,),
    ).fetchall()
    attempt_rows = []
    for row in attempts:
        raw = json.dumps(row[11], ensure_ascii=False, indent=2, default=str)
        attempt_rows.append(
            [
                html.escape(str(row[0])), html.escape(str(row[1])),
                html.escape(row[2] or "—"), html.escape(row[3] or "—"),
                html.escape(row[4] or "—"), html.escape(row[5] or "—"),
                html.escape(row[6] or "—"),
                html.escape(f"{row[7]} / {row[8]} / {row[9]}"),
                html.escape(row[10] or "—"), f"<pre>{html.escape(raw)}</pre>",
            ]
        )
    candidates = conn.execute(
        """SELECT fc.candidate_index, fc.accepted, fc.rejection_code,
                  fc.rejection_reason, fc.finding_index, fc.raw_candidate
           FROM finding_candidates fc JOIN processing_attempts pa ON pa.id = fc.attempt_id
           WHERE pa.material_id = %s ORDER BY pa.id DESC, fc.candidate_index""",
        (material_id,),
    ).fetchall()
    candidate_rows = []
    for row in candidates:
        raw = json.dumps(row[5], ensure_ascii=False, indent=2, default=str)
        result = '<span class="ok">принят</span>' if row[1] else '<span class="error">отклонён</span>'
        candidate_rows.append(
            [html.escape(str(row[0])), result, html.escape(row[2] or "—"),
             html.escape(row[3] or "—"), html.escape(str(row[4] or "—")),
             f"<pre>{html.escape(raw)}</pre>"]
        )
    findings = conn.execute(
        """SELECT finding_index, block_index, block_title, value, source_value,
                  summary, parent_finding_index, is_derived, calculation_formula,
                  evidence, methodology_note
           FROM findings WHERE material_id = %s AND prompt_version = %s
           ORDER BY block_index, block_position, id""",
        (material_id, material[5]),
    ).fetchall()
    finding_rows = [
        [html.escape(str(row[0])), html.escape(str(row[1] or "—")),
         html.escape(row[2] or "—"), html.escape(row[3] or "—"),
         html.escape(row[4] or "—"), html.escape(row[5] or "—"),
         html.escape(str(row[6] or "—")), "да" if row[7] else "нет",
         html.escape(row[8] or "—"), html.escape(row[9] or "—"),
         html.escape(row[10] or "—")]
        for row in findings
    ]
    return DETAIL_PAGE.format(
        style=DETAIL_STYLE, title=html.escape(material[2]),
        source=html.escape(material[1]), status=html.escape(material[4] or "—"),
        version=html.escape(material[5] or "—"), url=html.escape(material[3], quote=True),
        text=html.escape((material[6] or "")[:50000]),
        attempts=_table(
            ["ID", "Когда", "Версия", "Модель", "Режим", "Статус", "Завершение", "Всего / принято / отклонено", "Ошибка", "Сырой JSON"],
            attempt_rows, "Попыток пока нет",
        ),
        candidates=_table(
            ["Индекс", "Итог", "Код", "Причина", "Находка", "Исходный кандидат"],
            candidate_rows, "Кандидатов пока нет",
        ),
        findings=_table(
            ["Индекс", "Блок", "Название блока", "Значение", "Исходное значение", "Подпись", "Родитель", "Производная", "Формула", "Цитата", "Методология"],
            finding_rows, "Итоговых находок пока нет",
        ),
    )


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
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
                if path.startswith("/materials/") and path.removeprefix("/materials/").isdigit():
                    page = render_material(conn, int(path.removeprefix("/materials/")))
                    body = (page or "Материал не найден").encode()
                    status = 200 if page else 404
                elif path == "/":
                    body = render(conn).encode()
                    status = 200
                else:
                    body = "Страница не найдена".encode()
                    status = 404
            self.send_response(status)
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
