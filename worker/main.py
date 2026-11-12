"""Worker: extracts evidence-backed statistics useful for audience reach research."""

import json
import os
import re
import time
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path

import db

SERVICE = "worker"
OPENWEBUI_BASE_URL = os.environ.get(
    "OPENWEBUI_BASE_URL", "https://inside.omirussia.ru"
).rstrip("/")
OPENWEBUI_URL = f"{OPENWEBUI_BASE_URL}/api/chat/completions"
MODEL = os.environ.get("MODEL", "")
DEFAULT_PROMPT_VERSION = "audience-human-shares-v5"
PROMPT_VERSION = os.environ.get("PROMPT_VERSION", DEFAULT_PROMPT_VERSION)
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "10"))
RETRY_SECONDS = int(os.environ.get("RETRY_SECONDS", "3600"))
MAX_TEXT_CHARS = int(os.environ.get("MAX_TEXT_CHARS", "12000"))
MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "10000"))
WORKER_BATCH_SIZE = int(os.environ.get("WORKER_BATCH_SIZE", "20"))
MAX_FINDINGS_PER_MATERIAL = int(os.environ.get("MAX_FINDINGS_PER_MATERIAL", "20"))
REPROCESS_OLD = os.environ.get("REPROCESS_OLD", "0").lower() in {"1", "true", "yes"}
TARGETING_CRITERIA_PATH = Path(__file__).with_name("targeting_criteria.json")


def parse_material_ids(value):
    ids = []
    for part in (value or "").split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit() or int(part) <= 0:
            raise ValueError("REPROCESS_MATERIAL_IDS должен содержать положительные id через запятую")
        material_id = int(part)
        if material_id not in ids:
            ids.append(material_id)
    return tuple(ids)


REPROCESS_MATERIAL_IDS = parse_material_ids(os.environ.get("REPROCESS_MATERIAL_IDS"))

INDICATOR_TYPES = {
    "population_share",
    "conditional_share",
}

IR_APPLICABILITY_VALUES = {"direct", "indirect"}


def load_targeting_criteria(path=TARGETING_CRITERIA_PATH):
    """Load the explicit allow-list used for targetable denominator checks."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("version") != 1 or not isinstance(data.get("criteria"), list):
        raise ValueError("некорректная конфигурация таргетируемых критериев")

    criteria = {}
    for item in data["criteria"]:
        if not isinstance(item, dict):
            raise ValueError("критерий должен быть объектом")
        criterion_id = nullable_text(item.get("id"), 100)
        label = nullable_text(item.get("label"), 200)
        synonyms = item.get("synonyms")
        if not criterion_id or not label or not isinstance(synonyms, list):
            raise ValueError("у критерия обязательны id, label и synonyms")
        if criterion_id in criteria:
            raise ValueError(f"повторяющийся критерий: {criterion_id}")
        clean_synonyms = [
            synonym
            for value in synonyms
            if (synonym := nullable_text(value, 200))
        ]
        criteria[criterion_id] = {
            "id": criterion_id,
            "label": label,
            "synonyms": clean_synonyms,
        }
    if not criteria:
        raise ValueError("список таргетируемых критериев пуст")
    return criteria


def partition_targeting_criteria(criterion_ids, criteria=None):
    """Separate approved criteria from candidates without auto-approving them."""
    approved_ids = set((criteria or load_targeting_criteria()).keys())
    requested = []
    for value in criterion_ids or []:
        criterion_id = nullable_text(value, 100)
        if criterion_id and criterion_id not in requested:
            requested.append(criterion_id)
    return (
        [value for value in requested if value in approved_ids],
        [value for value in requested if value not in approved_ids],
    )

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

HUMAN_AUDIENCE_MARKERS = (
    "населени",
    "россиян",
    "жител",
    "граждан",
    "респондент",
    "опрошенн",
    "участник",
    "человек",
    "людей",
    "мужчин",
    "женщин",
    "девуш",
    "юнош",
    "детей",
    "подрост",
    "молодеж",
    "пенсионер",
    "родител",
    "матер",
    "мам",
    "отц",
    "потребител",
    "домохозяйств",
    "пользовател",
    "покупател",
    "клиент",
    "владельц",
    "продавц",
    "сотрудник",
    "работник",
    "специалист",
    "руководител",
    "представител",
    "директор",
    "менеджер",
    "предпринимател",
    "пациент",
)

PROMPT = """Ты отбираешь проверяемые статистические находки для предварительной оценки достижимости целевых аудиторий онлайн-опросов.

Утвержденные таргетируемые критерии (id: название):
{targeting_criteria}

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
      "finding_key": "уникальный внутри материала короткий ключ",
      "block_index": 1,
      "block_title": "короткое название смыслового блока",
      "parent_key": "finding_key непосредственной родительской находки или null",
      "topic": "короткая тема исследования",
      "indicator_type": "population_share | conditional_share",
      "ir_applicability": "direct | indirect",
      "geography": "география исследования или null",
      "audience": "аудитория, к которой относится показатель, или null",
      "indicator": "что именно измерено, или null",
      "value": "значение вместе с единицей, например 40% или 2,3 млн, или null",
      "percent_base": "дословная короткая формулировка знаменателя/базы процента из текста или null",
      "study_period": "период проведения исследования или null",
      "behavior_period": "период, срок или частота поведения, покупки или пользования или null",
      "base_kind": "targetable_explicit | targetable_inferred | conditional | unknown",
      "base_reason": "почему эта база определена именно так или null",
      "methodology_evidence": "дословная цитата из методологии, подтверждающая базу, или null",
      "targeting_criteria": ["id критерия из утвержденного списка или новый id-кандидат"],
      "useful_for": "для оценки какой ЦА показатель может быть полезен",
      "evidence": "точная короткая цитата из текста, подтверждающая значение и аудиторию, или null",
      "summary": "одна короткая фраза по-русски"
    }}
  ]
}}

Правила:
- Материал релевантен, только если содержит долю людей с каким-либо признаком, выраженную численным процентом или простой словесной дробью. Подходят фактическое поведение, пользование, потребление, покупка, продажа, владение, знание, намерение, мнение, отношение и любая другая доля аудитории.
- Знаменателем всегда должны быть люди: население, респонденты, пользователи, покупатели, родители, сотрудники, руководители, представители компаний или другая явно названная группа людей. B2B-аудитории допустимы.
- Не извлекай абсолютные количества, объем или долю рынка, долю заказов/продаж/выручки/трафика, средний чек, цены, темпы изменения и прогнозы рыночных или операционных показателей. Процент, знаменателем которого являются не люди, не является находкой.
- Один материал может дать от 0 до {max_findings} находок. Не дублируй один показатель разными формулировками.
- Сначала раздели находки одной страницы на смысловые блоки. В каждом блоке первая находка — базовая доля, остальные — уточнения внутри аудитории предыдущей находки.
- finding_key уникален внутри материала. parent_key указывает на непосредственную родительскую находку того же блока. У базовой находки parent_key=null.
- block_index начинается с 1. Для разных базовых измерений, например «покупали» и «продавали», используй разные блоки.
- Базовую находку каждого блока ставь раньше дочерних. Не связывай показатели, если вложенность знаменателей неоднозначна.
- population_share — только доля от всего населения либо широкой демографической базы, заданной лишь географией, полом или возрастом.
- conditional_share — любая доля внутри группы, уже отобранной по поведению, пользованию, покупке, владению, профессии, заболеванию или характеристике организации. Доли среди пользователей, покупателей, продавцов, клиентов, владельцев и работников всегда conditional_share, даже если они составляют всю выборку исследования.
- ir_applicability=direct, только если показатель непосредственно является долей людей с нужным признаком относительно ясно указанной базы.
- ir_applicability=indirect для отношения, намерения, частоты или связанного признака, который лишь помогает косвенно оценить ЦА.
- Не вычисляй вложенные доли и не объединяй разные показатели: извлекай исходные значения и точные связи parent_key. Разрешено только перевести простую словесную долю в процент по правилу ниже.
- Перед заполнением percent_base проверь не только абзац с показателем, но и описание выборки/методологии. Укажи точную аудиторию, относительно которой посчитан процент. Для исследования на специальной выборке эта выборка является базой. Если показатель рассчитан на еще более узкой подгруппе, укажи именно ее. Копируй percent_base дословным компактным фрагментом исходного текста с тем же падежом и написанием, например «среди молодежи», а не нормализованным словом «молодёжь»; если такого фрагмента нет или база достоверно не определяется, оставь null.
- В summary для широкой базы взрослых жителей России предпочитай естественную формулировку «взрослых россиян» вместо «населения 18+», только когда география России и возраст 18+ подтверждены текстом. Для специальной выборки или подгруппы сохраняй её собственное название и базу.
- Включай в summary указанный в источнике период поведения, состояния, намерения или прогноза. Если общий период задан в предыдущем предложении, вопросе анкеты или начале абзаца и относится к нескольким показателям, перенеси его в каждую соответствующую формулировку. Не путай этот период с датой публикации или периодом проведения исследования и не придумывай его при отсутствии в тексте.
- Географию, аудиторию, период и базу указывай только если они явно следуют из текста. Иначе null.
- targetable_explicit используй, когда база прямо названа и состоит только из утвержденных таргетируемых критериев.
- targetable_inferred используй только при обоснованном выводе из описания выборки/методологии. Тогда обязательны base_reason и дословная methodology_evidence.
- conditional используй для доли внутри поведенческой или иной нетаргетируемой группы. unknown — если база неясна.
- В targeting_criteria перечисли id всех критериев, образующих базу. Новый возможный критерий дай коротким английским id: программа сохранит его кандидатом, но не одобрит автоматически.
- behavior_period обязателен, когда в тексте указаны период, срок или частота покупки, продажи или пользования. Не заменяй им study_period исследования.
- value обязано быть процентом от 0% до 100%. Для простых словесных долей «половина», «треть», «две трети», «четверть», «три четверти» переводи значение соответственно в 50%, около 33%, около 67%, 25%, 75%. Округлённую долю отмечай словом «около» в summary, например «две трети респондентов» → «около 67% респондентов». evidence должна дословно сохранять исходное словесное выражение или численный процент и подтверждать человеческую аудиторию. Не выводи численный процент из других, не перечисленных здесь словесных форм. Если проверяемой доли людей нет, не создавай находку.
- Дата публикации не является периодом исследования.
- Если пригодной статистики нет, relevant=false и findings=[].
"""


FINDING_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "topic": {"type": ["string", "null"]},
        "finding_key": {"type": "string"},
        "block_index": {"type": "integer"},
        "block_title": {"type": ["string", "null"]},
        "parent_key": {"type": ["string", "null"]},
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
        "behavior_period": {"type": ["string", "null"]},
        "base_kind": {
            "type": "string",
            "enum": ["targetable_explicit", "targetable_inferred", "conditional", "unknown"],
        },
        "base_reason": {"type": ["string", "null"]},
        "methodology_evidence": {"type": ["string", "null"]},
        "targeting_criteria": {
            "type": "array",
            "items": {"type": "string"},
        },
        "useful_for": {"type": ["string", "null"]},
        "evidence": {"type": ["string", "null"]},
        "summary": {"type": "string"},
    },
    "required": [
        "topic",
        "finding_key",
        "block_index",
        "block_title",
        "parent_key",
        "indicator_type",
        "ir_applicability",
        "geography",
        "audience",
        "indicator",
        "value",
        "percent_base",
        "study_period",
        "behavior_period",
        "base_kind",
        "base_reason",
        "methodology_evidence",
        "targeting_criteria",
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

# Learned once per process after the internal model rejects the strict schema.
STRICT_JSON_SCHEMA_AVAILABLE = None


class ModelResponseError(RuntimeError):
    """The provider answered, but the structured result was unusable."""

    def __init__(self, message, diagnostics=None):
        self.diagnostics = diagnostics or []
        super().__init__(message)


class OpenWebUIHTTPError(RuntimeError):
    """Safe Open WebUI HTTP diagnostics without request headers or credentials."""

    def __init__(self, status, response_excerpt, structured_mode):
        self.status = status
        self.response_excerpt = response_excerpt
        self.structured_mode = structured_mode
        super().__init__(
            f"Open WebUI HTTP {status} in {structured_mode} mode"
        )


def safe_excerpt(value, limit=800):
    value = value or ""
    key = os.environ.get("OPENWEBUI_API_KEY")
    if key:
        value = value.replace(key, "[REDACTED]")
    value = re.sub(r"sk-[A-Za-z0-9_-]+", "[REDACTED]", value)
    value = re.sub(r"(?i)Bearer\s+\S+", "Bearer [REDACTED]", value)
    return re.sub(r"\s+", " ", value).strip()[:limit]


def require_model_config():
    if not os.environ.get("OPENWEBUI_API_KEY"):
        raise RuntimeError("OPENWEBUI_API_KEY не задан")
    if not MODEL:
        raise RuntimeError("MODEL не задан: укажите ID модели из GET /api/models")


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
                    targeting_criteria="\n".join(
                        f"- {item['id']}: {item['label']}"
                        for item in load_targeting_criteria().values()
                    ),
                    source=source,
                    published_at=published_at.isoformat() if published_at else "не указана",
                    title=title,
                    text=(text or "")[:MAX_TEXT_CHARS],
                    max_findings=MAX_FINDINGS_PER_MATERIAL,
                )
                + retry_note,
            }
        ],
    }
    if MODEL == "qwen3.8-27b":
        body["chat_template_kwargs"] = {"enable_thinking": False}
    if structured_mode == "json_schema":
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "audience_statistics",
                "strict": True,
                "schema": RESPONSE_JSON_SCHEMA,
            },
        }
    else:
        # Local validation remains mandatory even when the model only supports JSON mode.
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
    require_model_config()
    body = model_request_body(
        source, title, text, published_at, structured_mode=structured_mode
    )
    request = urllib.request.Request(
        OPENWEBUI_URL,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {os.environ['OPENWEBUI_API_KEY']}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=240) as response:
            try:
                payload = json.loads(response.read())
                choice = payload["choices"][0]
                content = choice["message"]["content"]
            except (json.JSONDecodeError, KeyError, IndexError, TypeError) as error:
                raise ModelResponseError("некорректная оболочка ответа Open WebUI") from error
    except urllib.error.HTTPError as error:
        try:
            body_excerpt = error.read(4096).decode("utf-8", errors="replace")
        except Exception:
            body_excerpt = ""
        raise OpenWebUIHTTPError(
            error.code, safe_excerpt(body_excerpt), structured_mode
        ) from error
    answer = parse_model_content(content, choice.get("finish_reason"))
    usage = payload.get("usage") if isinstance(payload, dict) else None
    details = usage.get("completion_tokens_details") if isinstance(usage, dict) else None
    reasoning_tokens = (
        details.get("reasoning_tokens") if isinstance(details, dict) else None
    )
    if type(reasoning_tokens) is int and reasoning_tokens >= 0:
        answer["_reasoning_tokens"] = reasoning_tokens
    return answer


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
            reasoning_tokens = answer.pop("_reasoning_tokens", None)
            thinking_disabled_confirmed = (
                reasoning_tokens == 0
                if MODEL == "qwen3.8-27b" and reasoning_tokens is not None
                else None
            )
            if structured_mode == "json_schema":
                STRICT_JSON_SCHEMA_AVAILABLE = True
            answer["_response_meta"] = {
                "structured_mode": structured_mode,
                "previous_attempts": diagnostics,
                "strict_schema_available": STRICT_JSON_SCHEMA_AVAILABLE,
                "reasoning_tokens": reasoning_tokens,
                "thinking_disabled_confirmed": thinking_disabled_confirmed,
            }
            return answer
        except (ModelResponseError, OpenWebUIHTTPError) as error:
            errors.append(str(error))
            if isinstance(error, OpenWebUIHTTPError):
                format_error = error.status in {400, 422} and any(
                    marker in error.response_excerpt.lower()
                    for marker in (
                        "response_format",
                        "json_schema",
                        "schema",
                        "grammar error",
                        "unimplemented keys",
                    )
                )
                if format_error and structured_mode == "json_schema":
                    STRICT_JSON_SCHEMA_AVAILABLE = False
                diagnostics.append(
                    {
                        "stage": "openwebui_request",
                        "service_url": OPENWEBUI_URL,
                        "http_status": error.status,
                        "structured_mode": error.structured_mode,
                        "response_excerpt": error.response_excerpt,
                    }
                )
            else:
                diagnostics.append(
                    {
                        "stage": "openwebui_response_parse",
                        "service_url": OPENWEBUI_URL,
                        "http_status": None,
                        "structured_mode": structured_mode,
                        "response_excerpt": None,
                        "message": str(error),
                    }
                )
            # Auth, model access and rate limits will not be fixed by changing format.
            if isinstance(error, OpenWebUIHTTPError) and (
                error.status not in {400, 422} or not format_error
            ):
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


def value_supported(value, evidence):
    if not value:
        return True
    percent = parse_percent(value)
    return percent is not None and any(
        parse_percent(token) == percent
        for token in re.findall(r"\d+(?:[.,]\d+)?\s*%", evidence or "")
    )


def parse_percent(value):
    match = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*%\s*", value or "")
    if not match:
        return None
    try:
        percent = Decimal(match.group(1).replace(",", "."))
    except InvalidOperation:
        return None
    return percent if Decimal("0") <= percent <= Decimal("100") else None


VERBAL_FRACTIONS = (
    (re.compile(r"(?<!\w)три\s+четверти(?!\w)", re.I), 75, False),
    (re.compile(r"(?<!\w)две\s+трети(?!\w)", re.I), 67, True),
    (re.compile(r"(?<!\w)(?:одна|одну)\s+треть(?!\w)", re.I), 33, True),
    (re.compile(r"(?<!\w)половин(?:а|ы|у|е|ой)(?!\w)", re.I), 50, False),
    (re.compile(r"(?<!\w)треть(?:ю)?(?!\w)", re.I), 33, True),
    (re.compile(r"(?<!\w)четверт(?:ь|и|ью)(?!\w)", re.I), 25, False),
)


def supported_verbal_fraction(value, evidence):
    """Return a single source fraction only when it proves the supplied value."""
    if not value or not evidence or re.search(r"\d+(?:[.,]\d+)?\s*%", evidence):
        return None
    matches = []
    for pattern, percent, approximate in VERBAL_FRACTIONS:
        for match in pattern.finditer(evidence):
            if not any(match.start() < end and match.end() > start for start, end, *_ in matches):
                matches.append((match.start(), match.end(), match.group(), percent, approximate))
    if len(matches) != 1:
        return None
    _, _, source_value, percent, approximate = matches[0]
    supplied_percent = parse_percent(value)
    if supplied_percent is None:
        rounded_match = re.fullmatch(r"около\s+(\d+)\s*%", value, re.I)
        if rounded_match:
            if not approximate or Decimal(rounded_match.group(1)) != Decimal(percent):
                return None
        elif normalized(value) != normalized(source_value):
            return None
    elif supplied_percent != Decimal(percent):
        return None
    return source_value, f"{percent}%", approximate


def verbal_fraction_summary(summary, source_value, value, approximate):
    """Show a validated verbal fraction as a percent, marking rounded ones."""
    percentages = re.findall(r"\d+(?:[.,]\d+)?\s*%", summary)
    if any(parse_percent(item) != parse_percent(value) for item in percentages):
        return None
    label = f"{'около ' if approximate else ''}{value}"
    if re.search(rf"(?<!\d){re.escape(value[:-1])}\s*%", summary):
        if not approximate:
            return summary
        pattern = rf"(?<!\w)(?:(?:около|примерно)\s+)?{re.escape(value[:-1])}\s*%"
    else:
        pattern = rf"(?<!\w){re.escape(source_value)}(?!\w)"
        if not re.search(pattern, summary, re.I):
            return None
    return re.sub(
        pattern,
        lambda match: label.capitalize() if match.start() == 0 and match.group()[0].isupper() else label,
        summary,
        count=1,
        flags=re.I,
    )


def prepare_source_findings(findings):
    """Keep source evidence and disable calculations across separate findings."""
    prepared = []
    for original in findings:
        finding = dict(original)
        finding["source_value"] = finding.get("source_value") or finding.get("value")
        finding["source_percent_base"] = finding.get("percent_base")
        finding["calculated_percent"] = None
        finding["calculation_formula"] = None
        finding["calculation_chain"] = None
        finding["is_derived"] = False
        prepared.append(finding)
    return prepared


def has_restricted_base(audience, percent_base):
    value = normalized(f"{audience or ''} {percent_base or ''}")
    return any(marker in value for marker in RESTRICTED_AUDIENCE_MARKERS)


def has_human_denominator(percent_base, evidence, methodology_evidence):
    """Require an explicit people-based denominator in source-backed text."""
    # An explicit, verbatim percent_base is authoritative. Looking at the whole
    # evidence as well could turn "30% заказов оформлены пользователями" into a
    # people share merely because the sentence also mentions users.
    value = normalized(percent_base)
    if not value:
        value = normalized(
            " ".join(
                part for part in (evidence, methodology_evidence) if part
            )
        )
    if not value:
        return False
    return any(marker in value for marker in HUMAN_AUDIENCE_MARKERS)


def normalized_indicator_type(indicator_type, audience, percent_base):
    if indicator_type not in INDICATOR_TYPES:
        return None
    if indicator_type == "population_share" and has_restricted_base(audience, percent_base):
        return "conditional_share"
    return indicator_type


def normalized_ir_applicability(value, indicator_type, percent_base):
    if value == "direct" and not percent_base:
        return "indirect"
    if value in IR_APPLICABILITY_VALUES:
        return value
    return "indirect"


def validate_answer(answer, text):
    if not isinstance(answer, dict) or answer.get("relevant") is not True:
        return []
    findings = answer.get("findings")
    if not isinstance(findings, list):
        return []

    valid = []
    known_keys = {}
    block_positions = {}
    criteria = load_targeting_criteria()
    base_kinds = {
        "targetable_explicit",
        "targetable_inferred",
        "conditional",
        "unknown",
    }
    for position, raw in enumerate(findings[:MAX_FINDINGS_PER_MATERIAL], 1):
        if not isinstance(raw, dict):
            continue
        finding_key = nullable_text(raw.get("finding_key"), 100) or f"finding_{position}"
        if finding_key in known_keys:
            continue
        raw_block_index = raw.get("block_index")
        block_index = (
            raw_block_index
            if isinstance(raw_block_index, int) and raw_block_index > 0
            else 1
        )
        block_positions[block_index] = block_positions.get(block_index, 0) + 1
        summary = nullable_text(raw.get("summary"), 500)
        value = nullable_text(raw.get("value"), 100)
        evidence = nullable_text(raw.get("evidence"), 500)
        if not summary:
            continue
        # A number absent from the quote is valid only for a small, exact
        # verbal-fraction allowlist. Other numeric evidence stays unchanged.
        if value and not evidence_is_verbatim(evidence, text):
            continue
        source_value = value
        if value and not value_supported(value, evidence):
            verbal = supported_verbal_fraction(value, evidence)
            if verbal is None:
                continue
            source_value, value, approximate = verbal
            summary = verbal_fraction_summary(summary, source_value, value, approximate)
            if summary is None:
                continue
        audience = nullable_text(raw.get("audience"), 500)
        percent_base = nullable_text(raw.get("percent_base"), 500)
        if percent_base and not evidence_is_verbatim(percent_base, text):
            percent_base = None
        methodology_evidence = nullable_text(raw.get("methodology_evidence"), 700)
        if methodology_evidence and not evidence_is_verbatim(methodology_evidence, text):
            methodology_evidence = None
        base_kind = raw.get("base_kind")
        if base_kind not in base_kinds:
            base_kind = "unknown"
        base_reason = nullable_text(raw.get("base_reason"), 700)
        if base_kind == "targetable_inferred" and (
            not base_reason or not methodology_evidence
        ):
            base_kind = "unknown"
        approved_criteria, candidate_criteria = partition_targeting_criteria(
            raw.get("targeting_criteria"), criteria
        )
        parent_key = nullable_text(raw.get("parent_key"), 100)
        parent = known_keys.get(parent_key)
        parent_finding_index = (
            parent["finding_index"]
            if parent and parent["block_index"] == block_index
            else None
        )
        indicator_type = normalized_indicator_type(
            raw.get("indicator_type"), audience, percent_base
        )
        if (
            parse_percent(value) is None
            or indicator_type is None
            or not has_human_denominator(
                percent_base, evidence, methodology_evidence
            )
        ):
            continue
        # A conditional share is useful on its own only when the article states
        # the exact human group used as its denominator. Evidence that merely
        # mentions people elsewhere in the sentence is not enough.
        if indicator_type == "conditional_share" and not percent_base:
            continue
        if indicator_type == "conditional_share":
            base_kind = "conditional"
        ir_applicability = normalized_ir_applicability(
            raw.get("ir_applicability"), indicator_type, percent_base
        )
        finding = {
                "finding_index": position,
                "finding_key": finding_key,
                "block_index": block_index,
                "block_position": block_positions[block_index],
                "block_title": nullable_text(raw.get("block_title"), 300),
                "parent_finding_index": parent_finding_index,
                "topic": nullable_text(raw.get("topic"), 300),
                "indicator_type": indicator_type,
                "ir_applicability": ir_applicability,
                "geography": nullable_text(raw.get("geography"), 300),
                "audience": audience,
                "indicator": nullable_text(raw.get("indicator"), 500),
                "value": value,
                "source_value": source_value,
                "percent_base": percent_base,
                "study_period": nullable_text(raw.get("study_period"), 300),
                "behavior_period": nullable_text(raw.get("behavior_period"), 300),
                "base_kind": base_kind,
                "base_reason": base_reason,
                "methodology_evidence": methodology_evidence,
                "targeting_criteria": approved_criteria,
                "candidate_criteria": candidate_criteria,
                "useful_for": nullable_text(raw.get("useful_for"), 700),
                "evidence": evidence,
                "summary": summary,
            }
        valid.append(finding)
        known_keys[finding_key] = finding
    return valid


def pending_materials(conn):
    selection = ""
    params = [REPROCESS_OLD, PROMPT_VERSION]
    if REPROCESS_MATERIAL_IDS:
        selection = " AND m.id = ANY(%s)"
        params.append(list(REPROCESS_MATERIAL_IDS))
    params.append(WORKER_BATCH_SIZE)
    return conn.execute(
        """SELECT m.id, s.name, m.title, m.text, m.published_at,
                  m.processed_prompt_version
           FROM materials m
           JOIN sources s ON s.id = m.source_id
           WHERE m.status = 'new'
              OR (%s AND m.status IN ('done', 'skipped')
                  AND m.processed_prompt_version IS DISTINCT FROM %s"""
        + selection
        + """              )
           ORDER BY m.id
           LIMIT %s""",
        tuple(params),
    ).fetchall()


def save_result(conn, material_id, findings):
    status = "done" if findings else "skipped"
    with conn.transaction():
        # Prompt upgrades add a new version. Older interpretations stay in the
        # database for auditability; processed_prompt_version marks the current one.
        for finding in findings:
            conn.execute(
                """INSERT INTO findings
                       (material_id, finding_index, block_index, block_position, block_title,
                        parent_finding_index, topic, indicator_type, ir_applicability, geography,
                        audience, indicator, value, percent_base, study_period, behavior_period,
                        useful_for, evidence, summary, source_value, source_percent_base,
                        calculated_percent, calculation_formula, calculation_chain, base_kind,
                        base_reason, methodology_evidence, is_derived, targeting_criteria,
                        candidate_criteria, model, prompt_version)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                           %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                           %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s::jsonb,
                           %s::jsonb, %s, %s)
                   ON CONFLICT (material_id, prompt_version, finding_index) DO NOTHING""",
                (
                    material_id,
                    finding["finding_index"],
                    finding["block_index"],
                    finding["block_position"],
                    finding["block_title"],
                    finding["parent_finding_index"],
                    finding["topic"],
                    finding["indicator_type"],
                    finding["ir_applicability"],
                    finding["geography"],
                    finding["audience"],
                    finding["indicator"],
                    finding["value"],
                    finding["percent_base"],
                    finding["study_period"],
                    finding["behavior_period"],
                    finding["useful_for"],
                    finding["evidence"],
                    finding["summary"],
                    finding["source_value"],
                    finding["source_percent_base"],
                    finding["calculated_percent"],
                    finding["calculation_formula"],
                    json.dumps(finding["calculation_chain"], ensure_ascii=False)
                    if finding["calculation_chain"]
                    else None,
                    finding["base_kind"],
                    finding["base_reason"],
                    finding["methodology_evidence"],
                    finding["is_derived"],
                    json.dumps(finding["targeting_criteria"], ensure_ascii=False),
                    json.dumps(finding["candidate_criteria"], ensure_ascii=False),
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
            findings = prepare_source_findings(validate_answer(answer, text or ""))
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
    require_model_config()
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
