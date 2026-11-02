import unittest
from datetime import datetime, timezone

from bot.main import finding_lines
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
