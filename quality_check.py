"""Run a small, paid live quality check without writing anything to PostgreSQL."""

import json
import os
import urllib.error
from datetime import datetime
from itertools import islice
from pathlib import Path

from collector.sources import READERS
from worker.main import (
    MODEL,
    OPENWEBUI_URL,
    PROMPT_VERSION,
    OpenWebUIHTTPError,
    ask_model,
    prepare_source_findings,
    safe_excerpt,
    validate_answer,
)


SOURCES = (
    ("ФОМ", "fom_rss", "https://fom.ru/rss/all"),
    ("НАФИ", "nafi_html", "https://nafi.ru/polls/"),
    ("Data Insight", "datainsight_rss", "https://datainsight.ru/rss.xml"),
    ("Банк России", "cbr_rss", "https://www.cbr.ru/rss/RssNews"),
)


def as_datetime(value):
    return datetime.fromisoformat(value) if value else None


def write_report(report, output_path):
    output_path = Path(output_path)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    output_path.write_text(payload + "\n", encoding="utf-8")
    return output_path


def error_details(error, stage, service_url):
    details = {
        "type": type(error).__name__,
        "message": str(error)[:1000],
        "stage": stage,
        "service_url": service_url,
        "http_status": None,
        "response_excerpt": None,
    }
    if isinstance(error, OpenWebUIHTTPError):
        details.update(
            http_status=error.status,
            response_excerpt=error.response_excerpt,
            structured_mode=error.structured_mode,
        )
    elif getattr(error, "diagnostics", None):
        details["attempts"] = error.diagnostics
        last_http = next(
            (
                attempt
                for attempt in reversed(error.diagnostics)
                if attempt.get("http_status") is not None
            ),
            None,
        )
        if last_http:
            details.update(
                http_status=last_http["http_status"],
                response_excerpt=last_http.get("response_excerpt"),
                structured_mode=last_http.get("structured_mode"),
            )
    else:
        response = getattr(error, "response", None)
        if response is not None:
            details["http_status"] = getattr(response, "status_code", None)
            details["response_excerpt"] = safe_excerpt(getattr(response, "text", ""))
        elif isinstance(error, urllib.error.HTTPError):
            details["http_status"] = error.code
    return details


def error_record(source_name, item, error, stage, service_url):
    return {
        "source": source_name,
        "title": item.get("title"),
        "url": item.get("url"),
        "published_at": item.get("published_at"),
        "processing_status": "error",
        "error": error_details(error, stage, service_url),
        "response_meta": None,
        "model_relevant": None,
        "model_findings": 0,
        "accepted_findings": [],
        "rejected_by_validator": 0,
    }


def run_check(
    sources, readers, per_source, output_path, model_call=ask_model, total_limit=None
):
    report = []
    for source_name, kind, url in sources:
        if total_limit and len(report) >= total_limit:
            break
        remaining = total_limit - len(report) if total_limit else per_source
        source_limit = min(per_source, remaining)
        try:
            items = list(islice(readers[kind](url), source_limit))
        except Exception as error:
            report.append(
                error_record(
                    source_name,
                    {"url": url},
                    error,
                    stage="source_fetch",
                    service_url=url,
                )
            )
            write_report(report, output_path)
            continue
        for item in items:
            try:
                answer = model_call(
                    source_name,
                    item["title"],
                    item["text"],
                    as_datetime(item.get("published_at")),
                )
                accepted = prepare_source_findings(
                    validate_answer(answer, item["text"])
                )
                report.append(
                    {
                        "source": source_name,
                        "title": item["title"],
                        "url": item["url"],
                        "published_at": item.get("published_at"),
                        "processing_status": "ok",
                        "error": None,
                        "response_meta": answer.get("_response_meta"),
                        "model_relevant": answer.get("relevant"),
                        "model_findings": len(answer.get("findings") or []),
                        "accepted_findings": accepted,
                        "rejected_by_validator": len(answer.get("findings") or [])
                        - len(accepted),
                    }
                )
            except Exception as error:
                report.append(
                    error_record(
                        source_name,
                        item,
                        error,
                        stage="openwebui",
                        service_url=OPENWEBUI_URL,
                    )
                )
            # Keep a valid partial UTF-8 report even if a later publication fails.
            write_report(report, output_path)
    return report


def main():
    if not os.environ.get("OPENWEBUI_API_KEY"):
        raise SystemExit("OPENWEBUI_API_KEY is required for the live quality check")
    if not MODEL:
        raise SystemExit("MODEL is required: use an ID from GET /api/models")
    per_source = int(os.environ.get("QUALITY_ITEMS_PER_SOURCE", "2"))
    total_limit = int(os.environ.get("QUALITY_TOTAL_ITEMS", "0")) or None
    output_path = Path(os.environ.get("QUALITY_REPORT_PATH", "quality_report.json"))
    report = run_check(
        SOURCES, READERS, per_source, output_path, total_limit=total_limit
    )
    write_report(report, output_path)
    offered = sum(item["model_findings"] for item in report)
    accepted = sum(len(item["accepted_findings"]) for item in report)
    rejected = sum(item["rejected_by_validator"] for item in report)
    errors = sum(item["processing_status"] == "error" for item in report)
    # Keep stdout ASCII-only: Windows PowerShell 5 may decode native-process
    # output with an OEM code page. The actual report bypasses stdout entirely.
    print(
        f"UTF-8 report: {output_path.resolve()} | model={MODEL} | "
        f"prompt={PROMPT_VERSION} | publications={len(report)} | "
        f"offered={offered} | accepted={accepted} | rejected={rejected} | errors={errors}"
    )


if __name__ == "__main__":
    main()
