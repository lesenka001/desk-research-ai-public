"""Collector: visits every active source once, saves new materials, exits."""

import os
from itertools import islice

import db
from collector.sources import READERS

SERVICE = "collector"
MAX_PER_SOURCE = int(os.environ.get("MAX_PER_SOURCE", "10"))


def main():
    conn = db.connect()
    db.init_schema(conn)
    run_id = db.start_run(conn, SERVICE)
    total_new = 0
    try:
        sources = conn.execute(
            "SELECT id, name, kind, url FROM sources WHERE active"
        ).fetchall()
        for source_id, name, kind, url in sources:
            try:
                # Stop the generator at the configured limit so HTML pages after
                # that point are not downloaded unnecessarily.
                items = list(islice(READERS[kind](url), MAX_PER_SOURCE))
            except Exception as error:
                # one broken source does not stop the run
                db.log(SERVICE, f"источник «{name}» недоступен: {error}")
                continue
            new = 0
            for item in items:
                row = conn.execute(
                    """INSERT INTO materials
                           (source_id, run_id, url, title, text, published_at, content_hash)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT DO NOTHING RETURNING id""",
                    (
                        source_id,
                        run_id,
                        item["url"],
                        item["title"],
                        item["text"],
                        item["published_at"],
                        item.get("content_hash"),
                    ),
                ).fetchone()
                new += row is not None
            total_new += new
            db.progress_run(
                conn,
                SERVICE,
                run_id,
                total_new,
                f"{name}: взял {len(items)}, новых {new}",
            )
        db.finish_run(
            conn, SERVICE, run_id, total_new, f"новых материалов в базе: {total_new}"
        )
    except Exception as error:
        db.fail_run(conn, SERVICE, run_id, error)
        raise


if __name__ == "__main__":
    main()
