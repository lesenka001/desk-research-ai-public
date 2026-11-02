"""Worker: extracts evidence-backed statistics useful for audience reach research."""

import json
import os
import re
import time
import urllib.error
import urllib.request

import db

SERVICE = "worker"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = os.environ.get("MODEL", "anthropic/claude-haiku-5.5")
PROMPT_VERSION = os.environ.get("PROMPT_VERSION", "audience-stats-v2")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "10"))
RETRY_SECONDS = int(os.environ.get("RETRY_SECONDS", "3600"))
MAX_TEXT_CHARS = int(os.environ.get("MAX_TEXT_CHARS", "12000"))
MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "6000"))
WORKER_BATCH_SIZE = int(os.environ.get("WORKER_BATCH_SIZE", "20"))
REPROCESS_OLD = os.environ.get("REPROCESS_OLD", "0").lower() in {"1", "true", "yes"}

INDICATOR_TYPES = {
    "population_share",
    "conditional_share",
    "absolute_count",
    "market_metric",
    "other",
}

IR_APPLICABILITY_VALUES = {"direct", "indirect", "context_only"}

RESTRICTED_AUDIENCE_MARKERS = (
    "пользовател",
    "покупател",
    "клиент",
    "владельц",
    "продавц",
    "сотрудник",
    "работник",
    "специалист",
    "руководител",
    "компани",
    "организац",
    "предприяти",
    "использующ",
    "пользующ",
    "покупавш",
    "продававш",
    "имеющ",
    "страдающ",
    "пациент",
)

PROMPT = """Ты отбираешь проверяемые статистические находки для предварительной оценки достижимости целевых аудиторий онлайн-опросов.

Источник: {source}
Дата публикации: {published_at}
Заголовок: {title}
Текст публикации:
{text}

Верни строго один JSON-объект без markdown:
{{
  "relevant": true или false,
  "findings": [
    {{
      "topic": "короткая тема исследования",
      "indicator_type": "population_share | conditional_share | absolute_count | market_metric | other",
      "ir_applicability": "direct | indirect | context_only",
      "geography": "география исследования или null",
      "audience": "аудитория, к которой относится показатель, или null",
      "indicator": "что именно измерено, или null",
      "value": "значение вместе с единицей, например 40% или 2,3 млн, или null",
      "percent_base": "дословная короткая формулировка знаменателя/базы процента из текста или null",
      "study_period": "период проведения исследования или null",
      "useful_for": "для оценки какой ЦА показатель может быть полезен",
      "evidence": "точная короткая цитата из текста, подтверждающая значение и аудиторию, или null",
      "summary": "одна короткая фраза по-русски"
    }}
  ]
}}

Правила:
- Материал релевантен, если содержит статистику о населении, потребителях, работниках, организациях, поведении, пользовании товарами/услугами или характеристиках рынка, потенциально полезную для оценки ЦА.
- Один материал может дать от 0 до 5 находок. Не дублируй один показатель разными формулировками.
- population_share — только доля от всего населения либо широкой демографической базы, заданной лишь географией, полом или возрастом.
- conditional_share — любая доля внутри группы, уже отобранной по поведению, пользованию, покупке, владению, профессии, заболеванию или характеристике организации. Доли среди пользователей, покупателей, продавцов, клиентов, владельцев и работников всегда conditional_share, даже если они составляют всю выборку исследования.
- absolute_count — число людей, организаций или объектов.
- market_metric — объем рынка, число заказов, оборот, средний чек, темп роста и подобные рыночные показатели. Не представляй их как распространенность ЦА.
- Если тип не подходит, используй other.
- ir_applicability=direct, только если показатель непосредственно является долей людей/организаций с нужным признаком относительно ясно указанной базы.
- ir_applicability=indirect для отношения, намерения, частоты или связанного признака, который лишь помогает косвенно оценить ЦА.
- ir_applicability=context_only для объема рынка, заказов, оборота, среднего чека и прочего контекста, из которого нельзя получить долю ЦА. Любой market_metric должен иметь context_only.
- Не вычисляй новые значения, не объединяй проценты и не делай предположений о знаменателе.
- Перед заполнением percent_base проверь не только абзац с показателем, но и описание выборки/методологии. Укажи точную аудиторию, относительно которой посчитан процент. Для исследования на специальной выборке эта выборка является базой. Если показатель рассчитан на еще более узкой подгруппе, укажи именно ее. Копируй percent_base дословным компактным фрагментом исходного текста; если такого фрагмента нет или база достоверно не определяется, оставь null.
- Географию, аудиторию, период и базу указывай только если они явно следуют из текста. Иначе null.
- evidence должна быть дословным фрагментом переданного текста. Если числового значения нет, value и evidence могут быть null.
- Дата публикации не является периодом исследования.
- Если пригодной статистики нет, relevant=false и findings=[].
"""


FINDING_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "topic": {"type": ["string", "null"]},
        "indicator_type": {"type": "string", "enum": sorted(INDICATOR_TYPES)},
        "ir_applicability": {
            "type": "string",
            "enum": sorted(IR_APPLICABILITY_VALUES),
        },
        "geography": {"type": ["string", "null"]},
        "audience": {"type": ["string", "null"]},
        "indicator": {"type": ["string", "null"]},
        "value": {"type": ["string", "null"]},
        "percent_base": {"type": ["string", "null"]},
        "study_period": {"type": ["string", "null"]},
        "useful_for": {"type": ["string", "null"]},
        "evidence": {"type": ["string", "null"]},
        "summary": {"type": "string"},
    },
    "required": [
        "topic",
        "indicator_type",
        "ir_applicability",
        "geography",
        "audience",
        "indicator",
        "value",
        "percent_base",
        "study_period",
        "useful_for",
        "evidence",
        "summary",
    ],
}

RESPONSE_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "relevant": {"type": "boolean"},
        "findings": {
            "type": "array",
            "items": FINDING_JSON_SCHEMA,
        },
    },
    "required": ["relevant", "findings"],
}

# Learned once per process. OpenRouter may advertise structured outputs for the
# model while no currently routable provider accepts the strict parameter set.
STRICT_JSON_SCHEMA_AVAILABLE = None


class ModelResponseError(RuntimeError):
    """The provider answered, but the structured result was unusable."""

    def __init__(self, message, diagnostics=None):
        self.diagnostics = diagnostics or []
        super().__init__(message)


class OpenRouterHTTPError(RuntimeError):
    """Safe OpenRouter HTTP diagnostics without request headers or credentials."""

    def __init__(self, status, response_excerpt, structured_mode):
        self.status = status
        self.response_excerpt = response_excerpt
        self.structured_mode = structured_mode
        super().__init__(
            f"OpenRouter HTTP {status} in {structured_mode} mode"
        )


def safe_excerpt(value, limit=800):
    value = re.sub(r"sk-or-v1-[A-Za-z0-9_-]+", "[REDACTED]", value or "")
    value = re.sub(r"(?i)Bearer\s+\S+", "Bearer [REDACTED]", value)
    return re.sub(r"\s+", " ", value).strip()[:limit]


def model_request_body(source, title, text, published_at, structured_mode):
    retry_note = (
        "\nПредыдущий ответ не удалось разобрать. Верни только полный JSON, "
        "строго соответствующий заданной схеме; не обрывай объект."
        if structured_mode == "json_object"
        else ""
    )
    body = {
        "model": MODEL,
        "temperature": 0,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "messages": [
            {
                "role": "user",
                "content": PROMPT.format(
                    source=source,
                    published_at=published_at.isoformat() if published_at else "не указана",
                    title=title,
                    text=(text or "")[:MAX_TEXT_CHARS],
                )
                + retry_note,
            }
        ],
    }
    if structured_mode == "json_schema":
        body["provider"] = {"require_parameters": True}
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "audience_statistics",
                "strict": True,
                "schema": RESPONSE_JSON_SCHEMA,
            },
        }
    else:
        # Compatibility path used by the original workshop deployment. Local
        # validation remains mandatory even when the provider only supports JSON mode.
        body["response_format"] = {"type": "json_object"}
    return body


def parse_model_content(content, finish_reason=None):
    if finish_reason in {"length", "max_tokens"}:
        raise ModelResponseError(f"ответ модели обрезан: finish_reason={finish_reason}")
    if not isinstance(content, str) or not content.strip():
        raise ModelResponseError("модель вернула пустой структурированный ответ")
    cleaned = content.strip()
    if cleaned.startswith("```"):
        first_newline = cleaned.find("\n")
        cleaned = cleaned[first_newline + 1 :] if first_newline >= 0 else cleaned[3:]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3].rstrip()
    try:
        answer = json.loads(cleaned)
        if isinstance(answer, str):
            answer = json.loads(answer)
    except json.JSONDecodeError as error:
        raise ModelResponseError(
            f"невалидный JSON: строка {error.lineno}, столбец {error.colno}, позиция {error.pos}"
        ) from error
    if not isinstance(answer, dict):
        raise ModelResponseError("структурированный ответ не является JSON-объектом")
    if not isinstance(answer.get("relevant"), bool) or not isinstance(
        answer.get("findings"), list
    ):
        raise ModelResponseError("в JSON отсутствуют корректные relevant/findings")
    return answer


def request_model_once(source, title, text, published_at, structured_mode):
    body = model_request_body(
        source, title, text, published_at, structured_mode=structured_mode
    )
    request = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            try:
                payload = json.loads(response.read())
                choice = payload["choices"][0]
                content = choice["message"]["content"]
            except (json.JSONDecodeError, KeyError, IndexError, TypeError) as error:
                raise ModelResponseError("некорректная оболочка ответа OpenRouter") from error
    except urllib.error.HTTPError as error:
        try:
            body_excerpt = error.read(4096).decode("utf-8", errors="replace")
        except Exception:
            body_excerpt = ""
        raise OpenRouterHTTPError(
            error.code, safe_excerpt(body_excerpt), structured_mode
        ) from error
    return parse_model_content(content, choice.get("finish_reason"))


def ask_model(source, title, text, published_at):
    global STRICT_JSON_SCHEMA_AVAILABLE
    errors = []
    diagnostics = []
    modes = (
        ("json_object", "json_object")
        if STRICT_JSON_SCHEMA_AVAILABLE is False
        else ("json_schema", "json_object")
    )
    for structured_mode in modes:
        try:
            answer = request_model_once(
                source, title, text, published_at, structured_mode=structured_mode
            )
            if structured_mode == "json_schema":
                STRICT_JSON_SCHEMA_AVAILABLE = True
            answer["_response_meta"] = {
                "structured_mode": structured_mode,
                "previous_attempts": diagnostics,
                "strict_schema_available": STRICT_JSON_SCHEMA_AVAILABLE,
            }
            return answer
        except (ModelResponseError, OpenRouterHTTPError) as error:
            errors.append(str(error))
            if isinstance(error, OpenRouterHTTPError):
                if error.status == 404 and structured_mode == "json_schema":
                    STRICT_JSON_SCHEMA_AVAILABLE = False
                diagnostics.append(
                    {
                        "stage": "openrouter_request",
                        "service_url": OPENROUTER_URL,
                        "http_status": error.status,
                        "structured_mode": error.structured_mode,
                        "response_excerpt": error.response_excerpt,
                    }
                )
            else:
                diagnostics.append(
                    {
                        "stage": "openrouter_response_parse",
                        "service_url": OPENROUTER_URL,
                        "http_status": None,
                        "structured_mode": structured_mode,
                        "response_excerpt": None,
                        "message": str(error),
                    }
                )
            # Authentication/payment errors will not be fixed by changing format.
            if isinstance(error, OpenRouterHTTPError) and error.status in {401, 402, 403}:
                raise
    raise ModelResponseError(
        "две неудачные попытки структурированного ответа: " + "; ".join(errors),
        diagnostics=diagnostics,
    )


def nullable_text(value, limit=1000):
    if value is None:
        return None
    value = re.sub(r"\s+", " ", str(value)).strip()
    return value[:limit] or None


def normalized(value):
    return re.sub(r"\s+", " ", value or "").strip().lower()


def evidence_is_verbatim(evidence, text):
    return bool(evidence and normalized(evidence) in normalized(text))


def numeric_tokens(value):
    return [token.replace(",", ".") for token in re.findall(r"\d+(?:[.,]\d+)?", value or "")]


def value_supported(value, evidence):
    if not value:
        return True
    tokens = numeric_tokens(value)
    evidence_numbers = {number.replace(",", ".") for number in numeric_tokens(evidence)}
    return not tokens or all(token in evidence_numbers for token in tokens)


def has_restricted_base(audience, percent_base):
    value = normalized(f"{audience or ''} {percent_base or ''}")
    return any(marker in value for marker in RESTRICTED_AUDIENCE_MARKERS)


def normalized_indicator_type(indicator_type, audience, percent_base):
    if indicator_type not in INDICATOR_TYPES:
        indicator_type = "other"
    if indicator_type == "population_share" and has_restricted_base(audience, percent_base):
        return "conditional_share"
    return indicator_type


def normalized_ir_applicability(value, indicator_type, percent_base):
    # Market totals can inform context, but never directly estimate incidence.
    if indicator_type == "market_metric":
        return "context_only"
    if value == "direct" and not percent_base:
        return "indirect"
    if value in IR_APPLICABILITY_VALUES:
        return value
    # Missing/invalid labels are handled conservatively rather than promoted to direct.
    if indicator_type in {"population_share", "conditional_share"}:
        return "indirect"
    return "context_only"


def validate_answer(answer, text):
    if not isinstance(answer, dict) or answer.get("relevant") is not True:
        return []
    findings = answer.get("findings")
    if not isinstance(findings, list):
        return []

    valid = []
    for position, raw in enumerate(findings[:5], 1):
        if not isinstance(raw, dict):
            continue
        summary = nullable_text(raw.get("summary"), 500)
        value = nullable_text(raw.get("value"), 100)
        evidence = nullable_text(raw.get("evidence"), 500)
        if not summary:
            continue
        # Numeric findings require a verbatim quote containing the same numbers.
        if value and (
            not evidence_is_verbatim(evidence, text)
            or not value_supported(value, evidence)
        ):
            continue
        audience = nullable_text(raw.get("audience"), 500)
        percent_base = nullable_text(raw.get("percent_base"), 500)
        if percent_base and not evidence_is_verbatim(percent_base, text):
            percent_base = None
        indicator_type = normalized_indicator_type(
            raw.get("indicator_type"), audience, percent_base
        )
        ir_applicability = normalized_ir_applicability(
            raw.get("ir_applicability"), indicator_type, percent_base
        )
        valid.append(
            {
                "finding_index": position,
                "topic": nullable_text(raw.get("topic"), 300),
                "indicator_type": indicator_type,
                "ir_applicability": ir_applicability,
                "geography": nullable_text(raw.get("geography"), 300),
                "audience": audience,
                "indicator": nullable_text(raw.get("indicator"), 500),
                "value": value,
                "percent_base": percent_base,
                "study_period": nullable_text(raw.get("study_period"), 300),
                "useful_for": nullable_text(raw.get("useful_for"), 700),
                "evidence": evidence,
                "summary": summary,
            }
        )
    return valid


def pending_materials(conn):
    return conn.execute(
        """SELECT m.id, s.name, m.title, m.text, m.published_at,
                  m.processed_prompt_version
           FROM materials m
           JOIN sources s ON s.id = m.source_id
           WHERE m.status = 'new'
              OR (%s AND m.status IN ('done', 'skipped')
                  AND m.processed_prompt_version IS DISTINCT FROM %s)
           ORDER BY m.id
           LIMIT %s""",
        (REPROCESS_OLD, PROMPT_VERSION, WORKER_BATCH_SIZE),
    ).fetchall()


def save_result(conn, material_id, findings):
    status = "done" if findings else "skipped"
    with conn.transaction():
        # Prompt upgrades add a new version. Older interpretations stay in the
        # database for auditability; processed_prompt_version marks the current one.
        for finding in findings:
            conn.execute(
                """INSERT INTO findings
                       (material_id, finding_index, topic, indicator_type, ir_applicability, geography,
                        audience, indicator, value, percent_base, study_period,
                        useful_for, evidence, summary, model, prompt_version)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                           %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (material_id, prompt_version, finding_index) DO NOTHING""",
                (
                    material_id,
                    finding["finding_index"],
                    finding["topic"],
                    finding["indicator_type"],
                    finding["ir_applicability"],
                    finding["geography"],
                    finding["audience"],
                    finding["indicator"],
                    finding["value"],
                    finding["percent_base"],
                    finding["study_period"],
                    finding["useful_for"],
                    finding["evidence"],
                    finding["summary"],
                    MODEL,
                    PROMPT_VERSION,
                ),
            )
        conn.execute(
            """UPDATE materials
               SET status = %s, processed_prompt_version = %s, processing_error = NULL
               WHERE id = %s""",
            (status, PROMPT_VERSION, material_id),
        )
    return status


def process_new(conn):
    pending = pending_materials(conn)
    if not pending:
        return
    run_id = db.start_run(conn, SERVICE)
    found = 0
    for done, (material_id, source, title, text, published_at, processed_version) in enumerate(
        pending, 1
    ):
        if processed_version == PROMPT_VERSION:
            has_findings = conn.execute(
                "SELECT EXISTS (SELECT 1 FROM findings WHERE material_id = %s AND prompt_version = %s)",
                (material_id, PROMPT_VERSION),
            ).fetchone()[0]
            conn.execute(
                "UPDATE materials SET status = %s WHERE id = %s",
                ("done" if has_findings else "skipped", material_id),
            )
            continue
        try:
            answer = ask_model(source, title, text, published_at)
            findings = validate_answer(answer, text or "")
        except Exception as error:
            conn.execute(
                """UPDATE materials
                   SET status = 'failed', processing_error = %s
                   WHERE id = %s""",
                (f"{type(error).__name__}: {error}"[:1000], material_id),
            )
            db.log(SERVICE, f"материал {material_id}: модель не ответила: {error}")
            continue

        save_result(conn, material_id, findings)
        found += len(findings)
        if findings:
            db.log(SERVICE, f"материал {material_id} -> {len(findings)} статистических находок")
        else:
            db.log(SERVICE, f"материал {material_id} нерелевантен или без проверяемых чисел")
        if done % 10 == 0:
            db.progress_run(
                conn, SERVICE, run_id, found, f"разобрано {done} из {len(pending)}"
            )
    db.finish_run(conn, SERVICE, run_id, found, f"разобрано {len(pending)}, находок {found}")


def retry_failed(conn):
    retried = conn.execute(
        "UPDATE materials SET status = 'new' WHERE status = 'failed'"
    ).rowcount
    if retried:
        db.log(SERVICE, f"вернул в разбор {retried} материалов после ошибок")


def main():
    conn = db.connect()
    db.init_schema(conn)
    mode = "с повторной обработкой старых материалов" if REPROCESS_OLD else "только новые материалы"
    db.log(SERVICE, f"жду материалы, модель {MODEL}, промпт {PROMPT_VERSION}, {mode}")
    last_retry = float("-inf")
    while True:
        try:
            if time.monotonic() - last_retry >= RETRY_SECONDS:
                retry_failed(conn)
                last_retry = time.monotonic()
            process_new(conn)
        except Exception as error:
            db.log(SERVICE, f"упал: {error}")
            conn = db.connect()
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
