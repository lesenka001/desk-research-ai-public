import unittest
from unittest.mock import Mock, patch

from collector import sources


RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><item>
<title>Исследование покупателей</title>
<link>https://example.org/study</link>
<description>40% покупателей выбирают товар онлайн</description>
<pubDate>Mon, 02 Nov 2026 10:00:00 +0300</pubDate>
</item></channel></rss>""".encode()

HTML = """<!doctype html><html><head>
<meta property="article:published_time" content="2026-11-02T10:00:00+03:00">
<title>Исследование покупателей</title></head><body><main>
<h1>Исследование покупателей</h1>
<p>40% покупателей выбирают товар онлайн.</p>
<p>Опрос проведен среди жителей России старше 18 лет.</p>
</main></body></html>""".encode()


def response(body, url, content_type):
    result = Mock()
    result.content = body
    result.url = url
    result.headers = {"Content-Type": content_type}
    result.raise_for_status.return_value = None
    return result


class SourceAdapterTests(unittest.TestCase):
    @patch.object(sources.SESSION, "get")
    def test_rss_fetches_full_article(self, get):
        get.side_effect = [
            response(RSS, "https://example.org/feed", "application/rss+xml"),
            response(HTML, "https://example.org/study", "text/html"),
        ]
        item = next(sources.read_rss_with_pages("https://example.org/feed"))
        self.assertIn("40% покупателей", item["text"])
        self.assertEqual(item["title"], "Исследование покупателей")
        self.assertEqual(item["published_at"], "2026-11-02T10:00:00+03:00")
        self.assertIsNotNone(item["content_hash"])

    @patch.object(sources.SESSION, "get")
    def test_nafi_catalog_deduplicates_links(self, get):
        catalog = """<html><body>
        <a href="/polls/example-study/">Первый</a>
        <a href="/polls/example-study/">Повтор</a>
        <a href="/news/not-a-poll/">Новость</a>
        </body></html>""".encode()
        get.side_effect = [
            response(catalog, "https://nafi.ru/polls/", "text/html"),
            response(HTML, "https://nafi.ru/polls/example-study/", "text/html"),
        ]
        items = list(sources.read_nafi("https://nafi.ru/polls/"))
        self.assertEqual(len(items), 1)

    def test_content_hash_ignores_spacing_and_case(self):
        self.assertEqual(
            sources.content_hash("  Один   Текст "),
            sources.content_hash("один текст"),
        )

    def test_russian_catalog_date(self):
        self.assertEqual(
            sources.parse_russian_date("27 октября 2026 | Маркетинг"),
            "2026-10-27T00:00:00+03:00",
        )

    def test_cbr_prefilter_rejects_questions_but_accepts_research(self):
        self.assertFalse(sources.is_cbr_relevant("Ответы на вопросы об отзыве лицензии"))
        self.assertTrue(sources.is_cbr_relevant("Результаты опроса населения России"))


if __name__ == "__main__":
    unittest.main()
