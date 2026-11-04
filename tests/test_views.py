import unittest
from datetime import datetime, timezone

from bot.main import finding_lines, group_findings
from web.main import render


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


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
                        "conditional_share",
                        "direct",
                        "22%",
                        "покупатели кофе",
                        "покупатели кофе",
                        "22% покупателей выбирают растворимый кофе.",
                        "https://example.org/study",
                    )
                ]
            )
        raise AssertionError(query)


class ViewTests(unittest.TestCase):
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
