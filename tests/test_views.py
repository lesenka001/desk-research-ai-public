import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from bot.main import (
    CURRENT_AUDIENCE_FINDING_SQL,
    build_digest,
    finding_lines,
    group_findings,
    page_data,
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
                    )
                ]
            )
        if query.startswith("UPDATE findings SET sent_at"):
            return Rows([])
        raise AssertionError(query)


class WebConnection:
    def execute(self, query, params=None):
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
                        None,
                    )
                ]
            )
        raise AssertionError(query)


class ViewTests(unittest.TestCase):
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
            },
        ]
        first_block_only, included = build_digest(rows, [("НАФИ", 3)], limit=450)
        self.assertEqual(included, [1, 2])
        self.assertIn("Покупали", first_block_only)
        self.assertNotIn("Продавали", first_block_only)
        self.assertEqual(first_block_only.count("https://example.org/study"), 1)
        self.assertIn("Еще 1 находок", first_block_only)

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
            },
        ]
        groups = group_findings(findings)
        self.assertEqual(len(groups), 1)
        self.assertEqual([block["block_title"] for block in groups[0]["blocks"]], ["Покупали", "Продавали"])
        self.assertEqual(
            [item["value"] for item in groups[0]["blocks"][0]["findings"]],
            ["38%", "13%"],
        )

    def test_mini_app_template_contains_calculation_audit(self):
        template = (
            __import__("pathlib").Path(__file__).parents[1] / "bot" / "template.html"
        ).read_text(encoding="utf-8")
        self.assertIn("Расчёт от таргетируемой базы", template)
        self.assertIn("calculation_formula", template)
        self.assertIn("candidate_criteria", template)
        self.assertIn("finding-block", template)

    def test_web_renders_statistical_fields(self):
        page = render(WebConnection())
        self.assertIn("Навигатор достижимости ЦА", page)
        self.assertIn("доля в подгруппе", page)
        self.assertIn("покупатели кофе", page)
        self.assertIn("22%", page)
        self.assertIn("Исследование кофе", page)
        self.assertIn("Покупали", page)

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
