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
    public_findings,
    render_page,
    send_new,
)
from web.main import render
import web.main as web_main


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
        if "FROM runs WHERE service = 'collector'" in query:
            return Rows([])
        if "SELECT m.published_at" in query:
            return Rows([])
        raise AssertionError(query)


class ViewTests(unittest.TestCase):
    def test_defensive_publication_requires_complete_explicit_metadata(self):
        valid = dict(material_id=10, is_primary=True, population_scope="base",
                     primary_reason="Главный вывод исследования")
        rows = [dict(valid, id=1, is_primary="true"),
                dict(valid, id=2, is_primary=1),
                dict(valid, id=3, population_scope="unknown"),
                dict(valid, id=4, primary_reason=None),
                dict(valid, id=5, primary_reason=" \t "),
                dict(valid, id=6, is_demographic_breakdown=True),
                dict(valid, id=7),
                dict(valid, id=8, population_scope="conditional"),
                dict(id=9, material_id=10),
                dict(id=10, material_id=10, is_primary=None),
                dict(valid, id=11), dict(valid, id=12)]
        self.assertEqual([row["id"] for row in public_findings(rows)], [7, 8, 9, 10, 11])
        groups = group_findings(rows)
        self.assertEqual([row["id"] for block in groups[0]["blocks"]
                          for row in block["findings"]], [7, 8, 9, 10, 11])

    def test_defensive_renderer_cap_excludes_unselected_and_composition(self):
        rows = [dict(id=1, material_id=10, is_primary=False),
                dict(id=2, material_id=10, is_demographic_breakdown=True)]
        rows += [dict(id=i, material_id=material, block_index=i, block_position=1)
                 for material in (10, 20) for i in range(3, 11)]
        result = public_findings(rows)
        self.assertEqual(len(result), 10)
        for material in (10, 20):
            self.assertEqual([row["id"] for row in result if row["material_id"] == material],
                             [3, 4, 5, 6, 7])
        groups = group_findings(rows)
        self.assertEqual(len(groups), 2)
        for group in groups:
            self.assertEqual(sum(len(block["findings"]) for block in group["blocks"]), 5)

    def test_mini_app_audit_data_keeps_scope_reason_and_rank(self):
        class AuditConnection(MiniAppQueryConnection):
            def execute(self, query, params=None):
                if "SELECT f.id, m.id" not in query:
                    return super().execute(query, params)
                self.queries.append((query, params))
                row = [None] * 45
                for index, value in {
                    0: 1, 1: 10, 2: 1, 3: "40% россиян выбирают сервис.", 5: "40%",
                    6: 1, 7: 1, 10: "population_share", 12: "россияне", 13: "россиян",
                    24: "targetable_explicit", 27: False, 28: [], 29: [],
                    30: "Материал", 31: "https://example.org/study", 35: "ФОМ",
                    40: "base", 41: True, 42: 1, 43: "Главный вывод <источника>", 44: False,
                }.items():
                    row[index] = value
                return Rows([tuple(row)])

        data = page_data(AuditConnection())
        finding = data["findings"][0]
        self.assertEqual(finding["population_scope"], "base")
        self.assertIs(finding["is_primary"], True)
        self.assertEqual(finding["primary_rank"], 1)
        self.assertEqual(finding["primary_reason"], "Главный вывод <источника>")
        self.assertIs(finding["is_demographic_breakdown"], False)
        with patch("bot.main.page_data", return_value=data):
            page = render_page(object())
        self.assertIn("Почему основная", page)
        self.assertIn("esc(f.primary_reason)", page)
        self.assertIn("f.population_scope", page)

    def test_web_and_bot_share_primary_publication_predicate(self):
        self.assertEqual(web_main.CURRENT_AUDIENCE_FINDING_SQL, CURRENT_AUDIENCE_FINDING_SQL)
        compact = "".join(CURRENT_AUDIENCE_FINDING_SQL.lower().split())
        self.assertIn("coalesce(f.is_primary,true)", compact)
        self.assertIn("f.is_demographic_breakdown", compact)

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
        self.assertLess(
            first_block_only.index("38% россиян"),
            first_block_only.index("13% россиян"),
        )
        self.assertNotIn("Базовая", first_block_only)
        self.assertNotIn("Производная", first_block_only)

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
        self.assertTrue(
            all(
                "finding_role" not in item
                for item in groups[0]["blocks"][0]["findings"]
            )
        )

    def test_mini_app_template_contains_grouped_source_findings(self):
        template = (
            __import__("pathlib").Path(__file__).parents[1] / "bot" / "template.html"
        ).read_text(encoding="utf-8")
        self.assertNotIn("Расчёт от базовой доли", template)
        self.assertNotIn("calculation_formula", template)
        self.assertIn("candidate_criteria", template)
        self.assertIn("finding-block", template)

    @patch("bot.main.page_data")
    def test_mini_app_html_has_source_values_without_roles_or_calculations(
        self, page_data_mock
    ):
        findings = [
            {
                "id": 1,
                "material_id": 10,
                "finding_index": 1,
                "value": "38%",
                "summary": "38% россиян покупали подержанные товары.",
            },
            {
                "id": 2,
                "material_id": 10,
                "finding_index": 2,
                "value": "33%",
                "summary": "33% покупателей использовали сайты объявлений.",
            },
            {
                "id": 3,
                "material_id": 10,
                "finding_index": 3,
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

        self.assertEqual([item["value"] for item in payload["findings"]], ["38%", "33%", "22%"])
        self.assertTrue(all("finding_role" not in item for item in payload["findings"]))
        self.assertTrue(all("calculation_formula" not in item for item in payload["findings"]))
        for label in ("Базовая", "Производная", "Условная"):
            self.assertNotIn(label, page)
        self.assertIn('class="finding-value"', page)
        self.assertNotIn("Исходная условная доля", page)
        self.assertIn("findingOrder", page)

    def test_web_renders_statistical_fields(self):
        conn = WebConnection()
        page = render(conn)
        self.assertIn("Навигатор достижимости ЦА", page)
        self.assertNotIn("Условная", page)
        self.assertIn("покупатели кофе", page)
        self.assertIn("22%", page)
        self.assertIn("Исследование кофе", page)
        self.assertIn("Покупали", page)
        findings_query = next(
            query for query, _ in conn.queries if "SELECT m.published_at" in query
        )
        self.assertIn(CURRENT_AUDIENCE_FINDING_SQL, findings_query)
        self.assertNotIn("CASE WHEN f.is_derived THEN 1", findings_query)

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
