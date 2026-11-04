"""Bot: Telegram bot and a Mini App with statistical findings.

- remembers everyone who pressed Start (table subscribers);
- after each collection is sorted by the worker, sends one summary to subscribers and marks findings.sent_at;
- answers /start and /status;
- serves the Mini App page on PORT, the bot opens it inside Telegram.
"""

import html
import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import db

SERVICE = "bot"
HERE = Path(__file__).resolve().parent
SEND_EVERY_SECONDS = 60
LATEST = 5  # findings listed in the summary, the rest are in the feed

COMMANDS = [
    {"command": "start", "description": "подписаться на новые исследования"},
    {"command": "status", "description": "что было в последних прогонах"},
]


def setting(name):
    return os.environ.get(name, "").strip()


def page_url():
    """Address of the Mini App: set by hand or taken from the platform (Railway, Coolify)."""
    url = (
        setting("MINI_APP_URL")
        or setting("RAILWAY_PUBLIC_DOMAIN")
        or setting("COOLIFY_URL")
    )
    if not url:
        return ""
    url = url.split(",")[0].strip()  # Coolify lists several domains separated by commas
    if not url.startswith("http"):
        url = f"https://{url}"
    return url.rstrip("/") + "/"


# ---------- Telegram ----------


def api(method, payload=None, timeout=20):
    base = setting("TELEGRAM_API") or "https://api.telegram.org"
    request = urllib.request.Request(
        f"{base}/bot{setting('TELEGRAM_TOKEN')}/{method}",
        data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())["result"]


def say(chat_id, text, button=True):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if button and page_url():
        payload["reply_markup"] = {
            "inline_keyboard": [
                [{"text": "Открыть ленту", "web_app": {"url": page_url()}}]
            ]
        }
    api("sendMessage", payload)


def finding_lines(rows):
    lines = []
    for summary, value, audience, percent_base, ir_applicability, url, source in rows:
        headline = f"{value} — {summary}" if value and value not in summary else summary
        line = f'• <a href="{html.escape(url)}">{html.escape(headline)}</a>\n  {html.escape(source)}'
        details = []
        if audience:
            details.append(f"аудитория: {html.escape(audience)}")
        if percent_base:
            details.append(f"база: {html.escape(percent_base)}")
        if ir_applicability:
            details.append(f"для IR: {html.escape(ir_applicability)}")
        if details:
            line += "\n  " + " · ".join(details)
        lines.append(line)
    return lines


def setup(conn):
    """Menu commands, the permanent page button, and a starting point for sending."""
    api("setMyCommands", {"commands": COMMANDS})
    if page_url():
        api(
            "setChatMenuButton",
            {
                "menu_button": {
                    "type": "web_app",
                    "text": "Лента",
                    "web_app": {"url": page_url()},
                }
            },
        )
    # before the first subscriber, old findings count as sent: the bot pushes only what comes later
    if not conn.execute("SELECT 1 FROM subscribers LIMIT 1").fetchone():
        marked = conn.execute(
            "UPDATE findings SET sent_at = now() WHERE sent_at IS NULL"
        ).rowcount
        if marked:
            db.log(
                SERVICE,
                f"подписчиков нет, {marked} старых находок лежат в ленте, рассылать их не буду",
            )
    me = api("getMe")
    db.log(SERVICE, f"@{me['username']} на связи, лента {page_url() or 'без адреса'}")


def send_new(conn):
    """One message per collection: waits until the worker has sorted it, then sends a summary to every subscriber."""
    # the worker is still busy while materials wait in the queue or the collector is running
    busy = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM materials WHERE status = 'new')"
        " OR EXISTS (SELECT 1 FROM runs WHERE service = 'collector' AND status IN ('started', 'running')"
        "            AND started_at > now() - interval '30 minutes')"
    ).fetchone()[0]
    if busy:
        return
    subscribers = [row[0] for row in conn.execute("SELECT chat_id FROM subscribers")]
    ids = [
        row[0]
        for row in conn.execute(
            "SELECT f.id FROM findings f JOIN materials m ON m.id = f.material_id"
            " WHERE f.sent_at IS NULL AND (m.processed_prompt_version IS NULL"
            " OR f.prompt_version = m.processed_prompt_version)"
        )
    ]
    if not subscribers or not ids:
        return
    sources = conn.execute(
        "SELECT s.name, count(*) FROM findings f"
        " JOIN materials m ON m.id = f.material_id JOIN sources s ON s.id = m.source_id"
        " WHERE f.id = ANY(%s) GROUP BY s.name ORDER BY count(*) DESC, s.name",
        (ids,),
    ).fetchall()
    latest = conn.execute(
        "SELECT f.summary, f.value, f.audience, f.percent_base, f.ir_applicability, m.url, s.name FROM findings f"
        " JOIN materials m ON m.id = f.material_id JOIN sources s ON s.id = m.source_id"
        " WHERE f.id = ANY(%s) ORDER BY f.id DESC LIMIT %s",
        (ids, LATEST),
    ).fetchall()
    text = f"<b>Новых статистических находок: {len(ids)}</b>\n" + " · ".join(
        f"{html.escape(name)} {count}" for name, count in sources
    )
    text += "\n\nСвежие:\n\n" + "\n\n".join(finding_lines(latest))
    if len(ids) > LATEST:
        text += f"\n\nЕще {len(ids) - LATEST} в ленте."
    run_id = db.start_run(conn, SERVICE)
    delivered = 0
    for chat_id in subscribers:
        try:
            say(chat_id, text)
            delivered += 1
        except urllib.error.HTTPError as error:
            if error.code == 403:  # the person blocked the bot
                conn.execute("DELETE FROM subscribers WHERE chat_id = %s", (chat_id,))
            db.log(SERVICE, f"чат {chat_id}: не доставлено, ответ {error.code}")
    conn.execute("UPDATE findings SET sent_at = now() WHERE id = ANY(%s)", (ids,))
    db.finish_run(conn, SERVICE, run_id, len(ids), f"сводка по {len(ids)} находкам, получили {delivered} из {len(subscribers)}")


def status_text(conn):
    lines = []
    for service, name in (
        ("collector", "Сбор"),
        ("worker", "Разбор"),
        ("bot", "Рассылка"),
    ):
        run = conn.execute(
            "SELECT started_at, status, note FROM runs WHERE service = %s ORDER BY id DESC LIMIT 1",
            (service,),
        ).fetchone()
        if run:
            state = {"finished": "завершен", "failed": "упал"}.get(run[1], "идет")
            lines.append(
                f"{name}: {run[0].astimezone(timezone.utc):%d.%m %H:%M} UTC, {state}. {html.escape(run[2] or '')}"
            )
        else:
            lines.append(f"{name}: еще не было")
    findings, subscribers = conn.execute(
        "SELECT (SELECT count(*) FROM findings), (SELECT count(*) FROM subscribers)"
    ).fetchone()
    lines.append(f"\nВсего находок {findings}, подписчиков {subscribers}.")
    return "\n".join(lines)


def handle(conn, message):
    chat = message["chat"]
    if chat.get("type") != "private":
        return
    words = (message.get("text") or "").split()
    command = words[0].split("@")[0] if words else ""
    if command == "/start":
        name = chat.get("username") or chat.get("first_name") or ""
        conn.execute(
            "INSERT INTO subscribers (chat_id, name) VALUES (%s, %s) ON CONFLICT (chat_id) DO NOTHING",
            (chat["id"], name),
        )
        latest = conn.execute(
            "SELECT f.summary, f.value, f.audience, f.percent_base, f.ir_applicability, m.url, s.name FROM findings f"
            " JOIN materials m ON m.id = f.material_id JOIN sources s ON s.id = m.source_id"
            " WHERE m.processed_prompt_version IS NULL OR f.prompt_version = m.processed_prompt_version"
            " ORDER BY f.id DESC LIMIT 5"
        ).fetchall()
        text = "Готово, вы подписаны. После каждого сбора пришлю сводку новых исследований и статистических показателей."
        if latest:
            text += "\n\nПоследние:\n\n" + "\n\n".join(finding_lines(latest))
        text += "\n\nВсе находки в ленте: кнопка ниже или «Лента» рядом с полем ввода. /status покажет, как работают блоки."
        say(chat["id"], text)
        db.log(SERVICE, f"новый подписчик {name or chat['id']}")
    elif command == "/status":
        say(chat["id"], status_text(conn))
    else:
        say(chat["id"], "Я понимаю /start и /status. Находки в ленте:")


def poll_forever():
    """Long polling: asks Telegram for new messages and answers them."""
    offset = None
    while True:
        try:
            updates = api(
                "getUpdates",
                {"timeout": 30, "offset": offset, "allowed_updates": ["message"]},
                timeout=40,
            )
            if updates:
                with db.connect() as conn:
                    for update in updates:
                        offset = update["update_id"] + 1
                        if "message" in update:
                            handle(conn, update["message"])
        except urllib.error.HTTPError as error:
            if error.code == 409:
                # Telegram gives updates to one program at a time
                db.log(SERVICE, "409: этот токен уже слушает другая программа, бот ждет, пока она остановится")
            else:
                db.log(SERVICE, f"ошибка связи с Telegram: ответ {error.code}")
            time.sleep(5)
        except Exception as error:
            db.log(SERVICE, f"ошибка связи с Telegram: {error}")
            time.sleep(5)


def send_forever():
    while True:
        try:
            with db.connect() as conn:
                send_new(conn)
        except Exception as error:
            db.log(SERVICE, f"рассылка не прошла: {error}")
        time.sleep(SEND_EVERY_SECONDS)


# ---------- Mini App page ----------


def iso(moment):
    return (
        moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if moment
        else None
    )


def group_findings(findings):
    """Group a flat feed by source page and then by calculation block."""
    materials = {}
    for finding in sorted(
        findings,
        key=lambda item: (
            item.get("material_id") or 0,
            item.get("block_index") or 1,
            item.get("block_position") or item.get("id") or 0,
        ),
    ):
        material_id = finding.get("material_id")
        material = materials.setdefault(
            material_id,
            {
                "material_id": material_id,
                "title": finding.get("title"),
                "url": finding.get("url"),
                "source": finding.get("source"),
                "date": finding.get("date"),
                "blocks": {},
            },
        )
        block_index = finding.get("block_index") or 1
        block = material["blocks"].setdefault(
            block_index,
            {
                "block_index": block_index,
                "block_title": finding.get("block_title") or "Находки",
                "findings": [],
            },
        )
        block["findings"].append(finding)

    grouped = []
    for material in materials.values():
        material["blocks"] = list(material["blocks"].values())
        grouped.append(material)
    return grouped


def page_data(conn):
    fields = (
        "id",
        "material_id",
        "finding_index",
        "summary",
        "reason",
        "value",
        "block_index",
        "block_position",
        "block_title",
        "parent_finding_index",
        "indicator_type",
        "ir_applicability",
        "audience",
        "percent_base",
        "geography",
        "study_period",
        "behavior_period",
        "useful_for",
        "evidence",
        "source_value",
        "source_percent_base",
        "calculated_percent",
        "calculation_formula",
        "calculation_chain",
        "base_kind",
        "base_reason",
        "methodology_evidence",
        "is_derived",
        "targeting_criteria",
        "candidate_criteria",
        "title",
        "url",
        "date",
        "run_id",
        "source_id",
        "source",
        "source_url",
        "text",
        "sent_at",
        "model",
    )
    findings = []
    for row in conn.execute(
        "SELECT f.id, m.id, f.finding_index, f.summary, f.topic, f.value, f.block_index, f.block_position,"
        " f.block_title, f.parent_finding_index, f.indicator_type, f.ir_applicability, f.audience,"
        " f.percent_base, f.geography, f.study_period, f.behavior_period, f.useful_for, f.evidence,"
        " f.source_value, f.source_percent_base, f.calculated_percent, f.calculation_formula,"
        " f.calculation_chain, f.base_kind, f.base_reason, f.methodology_evidence, f.is_derived,"
        " f.targeting_criteria, f.candidate_criteria,"
        " m.title, m.url, coalesce(m.published_at, m.collected_at), m.run_id,"
        " s.id, s.name, s.url, left(m.text, 600), f.sent_at, f.model"
        " FROM findings f JOIN materials m ON m.id = f.material_id JOIN sources s ON s.id = m.source_id"
        " WHERE m.processed_prompt_version IS NULL OR f.prompt_version = m.processed_prompt_version"
        " ORDER BY coalesce(m.published_at, m.collected_at) DESC, m.id DESC,"
        " f.block_index, f.block_position, f.id LIMIT 300"
    ):
        finding = dict(zip(fields, row))
        finding["date"], finding["sent_at"] = (
            iso(finding["date"]),
            iso(finding["sent_at"]),
        )
        if finding["calculated_percent"] is not None:
            finding["calculated_percent"] = str(finding["calculated_percent"])
        findings.append(finding)

    run = conn.execute(
        "SELECT id, finished_at, status, items FROM runs WHERE service = 'collector' AND status <> 'started'"
        " ORDER BY id DESC LIMIT 1"
    ).fetchone()
    run_info, sources = None, []
    if run:
        statuses = dict(
            conn.execute(
                "SELECT status, count(*) FROM materials WHERE run_id = %s GROUP BY status",
                (run[0],),
            ).fetchall()
        )
        added = sum(statuses.values())
        kept = conn.execute(
            "SELECT count(*) FROM findings f JOIN materials m ON m.id = f.material_id WHERE m.run_id = %s",
            (run[0],),
        ).fetchone()[0]
        sent = conn.execute(
            "SELECT count(*) FROM findings f JOIN materials m ON m.id = f.material_id"
            " WHERE m.run_id = %s AND f.sent_at IS NOT NULL",
            (run[0],),
        ).fetchone()[0]
        run_info = {
            "id": run[0],
            "finished_at": iso(run[1]),
            "status": run[2],
            "added": added,
            "processed": statuses.get("done", 0)
            + statuses.get("skipped", 0)
            + statuses.get("failed", 0),
            "kept": kept,
            "skipped": statuses.get("skipped", 0),
            "failed": statuses.get("failed", 0),
            "waiting": statuses.get("new", 0),
            "sent": sent,
        }
        sources = [
            {"name": name, "new": new}
            for name, new in conn.execute(
                "SELECT s.name, count(m.id) FROM sources s"
                " LEFT JOIN materials m ON m.source_id = s.id AND m.run_id = %s WHERE s.active GROUP BY s.id ORDER BY s.id",
                (run[0],),
            )
        ]
    totals = dict(
        zip(
            ("sources", "materials", "findings", "runs", "subscribers"),
            conn.execute(
                "SELECT (SELECT count(*) FROM sources WHERE active), (SELECT count(*) FROM materials),"
                " (SELECT count(*) FROM findings), (SELECT count(*) FROM runs), (SELECT count(*) FROM subscribers)"
            ).fetchone(),
        )
    )
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "totals": totals,
        "run": run_info,
        "sources": sources,
        "findings": findings,
        "material_groups": group_findings(findings),
    }


def render_page(conn):
    page = (
        (HERE / "template.html")
        .read_text(encoding="utf-8")
        .replace("/*BRAND*/", (HERE / "brand.css").read_text(encoding="utf-8"))
    )
    payload = json.dumps(page_data(conn), ensure_ascii=False).replace("</", "<\\/")
    return page.replace("/*DATA*/null/*END*/", payload)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            with db.connect() as conn:
                if path == "/health":
                    conn.execute("SELECT 1")
                    body, status, kind = b"ok", 200, "text/plain"
                elif path == "/":
                    body, status, kind = (
                        render_page(conn).encode("utf-8"),
                        200,
                        "text/html; charset=utf-8",
                    )
                else:
                    body, status, kind = b"not found", 404, "text/plain"
        except Exception as error:
            db.log(SERVICE, f"страница не собралась: {error}")
            body, status, kind = b"database unavailable", 503, "text/plain"
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    with db.connect() as conn:
        db.init_schema(conn)
        if setting("TELEGRAM_TOKEN"):
            try:
                setup(conn)
            except Exception as error:
                db.log(SERVICE, f"не настроил бота: {error}")
    if setting("TELEGRAM_TOKEN"):
        threading.Thread(target=poll_forever, daemon=True).start()
        threading.Thread(target=send_forever, daemon=True).start()
    else:
        db.log(
            SERVICE,
            "переменная TELEGRAM_TOKEN пустая: бот молчит, работает только лента",
        )
    port = int(os.environ.get("PORT", "8080"))
    db.log(SERVICE, f"лента открыта на порту {port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
