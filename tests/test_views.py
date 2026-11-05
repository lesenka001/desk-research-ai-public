import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer

from bot.main import (
    CURRENT_AUDIENCE_FINDING_SQL,
    build_digest,
    finding_lines,
    group_findings,
    page_data,
    render_page,
    send_new,
)
from web.main import render


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows

    def __iter__(self):
        return iter(self.rows)


class MiniAppQueryConnection:
    def __init__(self):
        self.queries = []

    def execute(self, query, params=None):
        self.queries.append((query, params))
        if "SELECT f.id, m.id" in query:
            return Rows([])
        if "FROM runs WHERE service = 'collector'" in query:
            return Rows([])
        if "SELECT (SELECT count(*) FROM sources WHERE active)" in query:
            return Rows([(4, 8, 0, 3, 1)])
        raise AssertionError(query)


class ReprocessedMaterialConnection:
    def __init__(self):
        self.queries = []

    def execute(self, query, params=None):
        self.queries.append((query, params))
        if "SELECT EXISTS" in query:
            return Rows([(False,)])
        if "SELECT chat_id FROM subscribers" in query:
            return Rows([(1001,)])
        if query.startswith("SELECT f.id FROM findings"):
            return Rows([(501,)])
        if "GROUP BY s.name" in query:
            return Rows([("ФОМ", 1)])
        if query.startswith("SELECT f.id, m.id"):
            return Rows(
                [
                    (
                        501,
                        10,
                        "Старый материал, обработанный заново",
                        "https://example.org/old-study",
                        "ФОМ",
                        1,
                        1,
                        "Пользование",
                        None,
                        "38% респондентов пользовались сервисом.",
                        "38%",
                        False,
                        "targetable_explicit",
                        [{"finding_index": 1, "percent": "38"}],
                        1,
                    )
                ]
            )
        if query.startswith("UPDATE findings SET sent_at"):
            return Rows([])
        raise AssertionError(query)


class WebConnection:
    def __init__(self):
        self.queries = []

    def execute(self, query, params=None):
        self.queries.append((query, params))
        if "count(*) FROM sources" in query:
            return Rows([(4, 8, 2)])
        if "GROUP BY role" in query:
            return Rows([("base", 1), ("conditional", 1)])
        if "FROM materials GROUP BY status" in query:
            return Rows([("done", 6), ("failed", 2)])
        if "FROM runs WHERE service = 'collector'" in query:
            return Rows([])
        if "SELECT m.published_at" in query:
            return Rows(
                [
                    (
                        datetime(2026, 11, 2, tzinfo=timezone.utc),
                        "ФОМ",
                        10,
                        "Исследование кофе",
                        "https://example.org/study",
                        1,
                        1,
                        "Покупали",
                        "conditional_share",
                        "direct",
                        "22%",
                        "покупатели кофе",
                        "покупатели кофе",
                        "22% покупателей выбирают растворимый кофе.",
                        None,
                        None,
                        "conditional",
                        False,
                        None,
                        1,
                    )
                ]
            )
        raise AssertionError(query)


class MarketOnlyConnection:
    def __init__(self):
        self.queries = []

    def execute(self, query, params=None):
        self.queries.append((query, params))
        if "SELECT EXISTS" in query:
            return Rows([(False,)])
        if "SELECT chat_id FROM subscribers" in query:
            return Rows([(1001,)])
        if query.startswith("SELECT f.id FROM findings"):
            return Rows([])
        raise AssertionError(query)


class EmptyWebConnection:
    def __init__(self):
        self.queries = []

    def execute(self, query, params=None):
        self.queries.append((query, params))
        if "count(*) FROM sources" in query:
            return Rows([(4, 8, 0)])
        if "GROUP BY role" in query:
            return Rows([])
        if "FROM materials GROUP BY status" in query:
            return Rows([("done", 8)])
        if "FROM runs WHERE service = 'collector'" in query:
            return Rows([])
        if "SELECT m.published_at" in query:
            return Rows([])
        raise AssertionError(query)


class ViewTests(unittest.TestCase):
    def test_audience_filter_percent_is_safe_with_and_without_parameters(self):
        plain_query = PostgresQuery(Transformer())
        plain_query.convert(
            "SELECT 1 FROM findings f WHERE " + CURRENT_AUDIENCE_FINDING_SQL,
            None,
        )
        parameterized_query = PostgresQuery(Transformer())
        parameterized_query.convert(
            "SELECT 1 FROM findings f WHERE "
            + CURRENT_AUDIENCE_FINDING_SQL
            + " AND f.material_id = %s",
            (1,),
        )

        plain = plain_query.query.decode()
        parameterized = parameterized_query.query.decode()
        for prepared in (plain, parameterized):
            self.assertIn("chr(37)", prepared)
            self.assertNotIn("%", prepared)
        self.assertIn("f.material_id = $1", parameterized)

    def test_current_feed_whitelists_audience_shares_and_one_prompt_version(self):
        conn = MiniAppQueryConnection()
        data = page_data(conn)
        self.assertEqual(data["findings"], [])
        feed_query = next(
            query for query, params in conn.queries if "SELECT f.id, m.id" in query
        )
        self.assertIn(
            "f.indicator_type IN ('population_share', 'conditional_share')",
            feed_query,
        )
        self.assertIn("f.value ~", feed_query)
        self.assertIn("btrim(f.percent_base)", feed_query)
        self.assertIn("f.prompt_version = coalesce(m.processed_prompt_version", feed_query)
        self.assertIn("ORDER BY latest_f.created_at DESC", feed_query)
        self.assertNotIn("market_metric", CURRENT_AUDIENCE_FINDING_SQL)
        self.assertNotIn("absolute_count", CURRENT_AUDIENCE_FINDING_SQL)

    @patch("bot.main.db.finish_run")
    @patch("bot.main.db.start_run", return_value=77)
    @patch("bot.main.say")
    def test_reprocessed_old_material_is_sent_when_new_version_is_current(
        self, say_mock, start_run_mock, finish_run_mock
    ):
        conn = ReprocessedMaterialConnection()
        send_new(conn)
        say_mock.assert_called_once()
        self.assertIn("Старый материал, обработанный заново", say_mock.call_args.args[1])
        selection_query = next(
            query
            for query, params in conn.queries
            if query.startswith("SELECT f.id FROM findings")
        )
        self.assertIn("f.sent_at IS NULL", selection_query)
        self.assertIn("f.prompt_version = coalesce(m.processed_prompt_version", selection_query)
        self.assertNotIn("m.run_id", selection_query)
        self.assertTrue(
            any(query.startswith("UPDATE findings SET sent_at") for query, _ in conn.queries)
        )
        finish_run_mock.assert_called_once()

    def test_telegram_digest_groups_material_and_keeps_whole_blocks(self):
        rows = [
            {
                "id": 1,
                "material_id": 10,
                "title": "Подержанные товары",
                "url": "https://example.org/study",
                "source": "НАФИ",
                "block_index": 1,
                "block_position": 1,
                "block_title": "Покупали",
                "parent_finding_index": None,
                "summary": "38% россиян 18+ покупали подержанные товары.",
                "value": "38%",
                "base_kind": "targetable_explicit",
                "is_derived": False,
                "calculation_chain": [{"finding_index": 1, "percent": "38"}],
            },
            {
                "id": 2,
                "material_id": 10,
                "title": "Подержанные товары",
                "url": "https://example.org/study",
                "source": "НАФИ",
                "block_index": 1,
                "block_position": 2,
                "block_title": "Покупали",
                "parent_finding_index": 1,
                "summary": "13% россиян 18+ использовали сайты объявлений.",
                "value": "13%",
                "base_kind": "conditional",
                "is_derived": True,
                "calculation_chain": [
                    {"finding_index": 1, "percent": "38"},
                    {"finding_index": 2, "percent": "33"},
                ],
            },
            {
                "id": 3,
                "material_id": 10,
                "title": "Подержанные товары",
                "url": "https://example.org/study",
                "source": "НАФИ",
                "block_index": 2,
                "block_position": 1,
                "block_title": "Продавали",
                "parent_finding_index": None,
                "summary": "30% россиян 18+ продавали подержанные товары. " + "очень " * 100,
                "value": "30%",
                "base_kind": "targetable_explicit",
                "is_derived": False,
                "calculation_chain": [{"finding_index": 3, "percent": "30"}],
            },
        ]
        first_block_only, included = build_digest(rows, [("НАФИ", 3)], limit=450)
        self.assertEqual(included, [1, 2])
        self.assertIn("Покупали", first_block_only)
        self.assertNotIn("Продавали", first_block_only)
        self.assertEqual(first_block_only.count("https://example.org/study"), 1)
        self.assertIn("Еще 1 находок", first_block_only)
        self.assertLess(first_block_only.index("Базовая"), first_block_only.index("Производная"))

    def test_mini_app_groups_one_material_into_ordered_blocks(self):
        findings = [
            {
                "id": 3,
                "material_id": 10,
                "title": "Подержанные товары",
                "url": "https://example.org/study",
                "source": "НАФИ",
                "date": "2026-11-04T10:00:00Z",
                "block_index": 2,
                "block_position": 1,
                "block_title": "Продавали",
                "value": "30%",
                "parent_finding_index": None,
                "base_kind": "targetable_explicit",
                "is_derived": False,
            },
            {
                "id": 2,
                "material_id": 10,
                "title": "Подержанные товары",
                "url": "https://example.org/study",
                "source": "НАФИ",
                "date": "2026-11-04T10:00:00Z",
                "block_index": 1,
                "block_position": 2,
                "block_title": "Покупали",
                "value": "13%",
                "parent_finding_index": 1,
                "base_kind": "conditional",
                "is_derived": True,
            },
            {
                "id": 1,
                "material_id": 10,
                "title": "Подержанные товары",
                "url": "https://example.org/study",
                "source": "НАФИ",
                "date": "2026-11-04T10:00:00Z",
                "block_index": 1,
                "block_position": 1,
                "block_title": "Покупали",
                "value": "38%",
                "parent_finding_index": None,
                "base_kind": "targetable_explicit",
                "is_derived": False,
            },
        ]
        groups = group_findings(findings)
        self.assertEqual(len(groups), 1)
        self.assertEqual([block["block_title"] for block in groups[0]["blocks"]], ["Покупали", "Продавали"])
        self.assertEqual(
            [item["value"] for item in groups[0]["blocks"][0]["findings"]],
            ["38%", "13%"],
        )
        self.assertEqual(
            [item["finding_role"] for item in groups[0]["blocks"][0]["findings"]],
            ["base", "derived"],
        )

    def test_control_article_shows_four_bases_ten_derivatives_and_no_duplicates(self):
        rows = []

        def add(value, role, block, position, summary):
            finding_id = len(rows) + 1
            rows.append(
                {
                    "id": finding_id,
                    "material_id": 3,
                    "finding_index": finding_id,
                    "title": "Продажа и покупка подержанных товаров",
                    "url": "https://example.org/second-hand",
                    "source": "ФОМ",
                    "block_index": block,
                    "block_position": position,
                    "block_title": "Покупка" if block == 1 else "Продажа",
                    "parent_finding_index": None if role == "base" else 3,
                    "summary": summary,
                    "value": value,
                    "base_kind": "targetable_inferred" if role == "base" else "conditional",
                    "is_derived": role == "derived",
                    "calculation_chain": (
                        [{"finding_index": 3, "percent": "50"}, {"finding_index": finding_id, "percent": "1"}]
                        if role == "derived"
                        else [{"finding_index": finding_id, "percent": value.rstrip("%") }]
                    ),
                }
            )

        add("14%", "base", 1, 1, "Обычная практика покупки")
        add("36%", "base", 1, 2, "Покупали несколько раз")
        add("50%", "base", 1, 3, "Когда-либо приобретали подержанные товары")
        for position, value, summary in (
            (4, "10,5%", "Приобретали автомобили"),
            (5, "7%", "Приобретали бытовую технику"),
            (6, "7%", "Приобретали электронику"),
            (7, "17,5%", "Покупали с целью экономии"),
            (8, "16,5%", "Покупали через сайты объявлений"),
        ):
            add(value, "derived", 1, position, summary)
        add("38%", "base", 2, 1, "Когда-либо продавали подержанные товары")
        for position, value, summary in (
            (2, "6%", "Продавали автомобили"),
            (3, "4%", "Продавали бытовую технику"),
            (4, "4%", "Продавали электронику"),
            (5, "12%", "Продавали через сайты объявлений"),
            (6, "9%", "Продавали с целью освободить место"),
        ):
            add(value, "derived", 2, position, summary)

        groups = group_findings([dict(row) for row in rows])
        roles = [
            finding["finding_role"]
            for block in groups[0]["blocks"]
            for finding in block["findings"]
        ]
        digest, included = build_digest(rows, [("ФОМ", 14)], limit=10000)

        self.assertEqual(roles.count("base"), 4)
        self.assertEqual(roles.count("derived"), 10)
        self.assertNotIn("conditional", roles)
        self.assertEqual(len(included), 14)
        self.assertEqual(digest.count("<b>Базовая:</b>"), 4)
        self.assertEqual(digest.count("<b>Производная:</b>"), 10)
        self.assertNotIn("Условная", digest)
        for raw_duplicate in ("21%", "35%", "33%", "15%", "11%", "31%", "23%"):
            self.assertNotIn(raw_duplicate, digest)

    def test_mini_app_template_contains_calculation_audit(self):
        template = (
            __import__("pathlib").Path(__file__).parents[1] / "bot" / "template.html"
        ).read_text(encoding="utf-8")
        self.assertIn("Расчёт от базовой доли", template)
        self.assertIn("Расчёт составной базы", template)
        self.assertIn("composite_component_indices", template)
        self.assertIn("calculation_formula", template)
        self.assertIn("candidate_criteria", template)
        self.assertIn("finding-block", template)

    @patch("bot.main.page_data")
    def test_mini_app_html_has_roles_calculated_value_and_source_details(
        self, page_data_mock
    ):
        findings = [
            {
                "id": 1,
                "material_id": 10,
                "finding_index": 1,
                "finding_role": "base",
                "value": "38%",
                "summary": "38% россиян покупали подержанные товары.",
            },
            {
                "id": 2,
                "material_id": 10,
                "finding_index": 2,
                "finding_role": "derived",
                "value": "13%",
                "source_value": "33%",
                "calculated_percent": "12.540000",
                "calculation_formula": "38% × 33% = 12,54%",
                "summary": "13% россиян покупали товары через сайты объявлений.",
            },
            {
                "id": 3,
                "material_id": 10,
                "finding_index": 3,
                "finding_role": "conditional",
                "value": "22%",
                "summary": "22% покупателей выбирают растворимый кофе.",
            },
        ]
        page_data_mock.return_value = {
            "findings": findings,
            "material_groups": [],
            "run": None,
            "sources": [],
            "totals": {"sources": 1, "materials": 1, "findings": 3, "runs": 1, "subscribers": 1},
            "generated_at": "2026-11-05 10:00",
        }
        page = render_page(object())
        marker = "const DATA = "
        payload_start = page.index(marker) + len(marker)
        payload_end = page.index(";", payload_start)
        payload = json.loads(page[payload_start:payload_end])

        self.assertEqual(
            [finding["finding_role"] for finding in payload["findings"]],
            ["base", "derived", "conditional"],
        )
        self.assertEqual(payload["findings"][1]["value"], "13%")
        self.assertEqual(payload["findings"][1]["source_value"], "33%")
        self.assertEqual(
            payload["findings"][1]["calculation_formula"],
            "38% × 33% = 12,54%",
        )
        for label in ("Базовая", "Производная", "Условная"):
            self.assertIn(label, page)
        self.assertIn('class="finding-value"', page)
        self.assertIn("Исходная условная доля", page)
        self.assertIn("findingOrder", page)

    def test_web_renders_statistical_fields(self):
        conn = WebConnection()
        page = render(conn)
        self.assertIn("Навигатор достижимости ЦА", page)
        self.assertIn("Условная", page)
        self.assertIn("покупатели кофе", page)
        self.assertIn("22%", page)
        self.assertIn("Исследование кофе", page)
        self.assertIn("Покупали", page)
        self.assertIn("Роли находок", page)
        self.assertIn("Базовые: <b>1</b>", page)
        self.assertIn("Условные: <b>1</b>", page)
        self.assertIn("С ошибкой: <b>2</b>", page)
        self.assertNotIn("<form", page.lower())
        compose = (__import__("pathlib").Path(__file__).parents[1] / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("SERVICE_FQDN_WEB_8000", compose)
        findings_query = next(
            query for query, _ in conn.queries if "SELECT m.published_at" in query
        )
        self.assertIn(CURRENT_AUDIENCE_FINDING_SQL, findings_query)
        self.assertIn("CASE WHEN f.is_derived THEN 1", findings_query)

    @patch("bot.main.say")
    def test_market_only_material_is_absent_from_telegram_and_web(self, say_mock):
        telegram_conn = MarketOnlyConnection()
        send_new(telegram_conn)
        say_mock.assert_not_called()
        selection_query = next(
            query
            for query, _ in telegram_conn.queries
            if query.startswith("SELECT f.id FROM findings")
        )
        self.assertIn(CURRENT_AUDIENCE_FINDING_SQL, selection_query)

        web_conn = EmptyWebConnection()
        page = render(web_conn)
        self.assertIn("Находок пока нет", page)
        web_query = next(
            query for query, _ in web_conn.queries if "SELECT m.published_at" in query
        )
        self.assertIn(CURRENT_AUDIENCE_FINDING_SQL, web_query)

    def test_telegram_line_contains_audience_and_base(self):
        lines = finding_lines(
            [
                (
                    "22% покупателей выбирают растворимый кофе.",
                    "22%",
                    "покупатели кофе",
                    "покупатели кофе",
                    "direct",
                    "https://example.org/study",
                    "ФОМ",
                )
            ]
        )
        self.assertIn("аудитория: покупатели кофе", lines[0])
        self.assertIn("база: покупатели кофе", lines[0])
        self.assertIn("для IR: direct", lines[0])
        self.assertEqual(lines[0].count("22%"), 1)


if __name__ == "__main__":
    unittest.main()
