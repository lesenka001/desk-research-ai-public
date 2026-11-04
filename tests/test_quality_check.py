import json
import unittest
from pathlib import Path
from unittest.mock import patch

import worker.main as worker_main
from quality_check import error_details, run_check, write_report
from worker.main import (
    ModelResponseError,
    OpenRouterHTTPError,
    ask_model,
    parse_model_content,
)


class QualityReportTests(unittest.TestCase):
    def test_quality_report_contains_deterministic_derived_share(self):
        text = (
            "Опрос проведен среди жителей России 18+. "
            "40% жителей России 18+ покупали подержанные товары за 6 месяцев. "
            "Среди покупателей 25% использовали сайты объявлений."
        )
        item = {
            "title": "Подержанные товары",
            "url": "https://example.org/study",
            "text": text,
            "published_at": None,
        }
        common = {
            "topic": "подержанные товары",
            "ir_applicability": "direct",
            "geography": "Россия",
            "study_period": None,
            "useful_for": "оценка аудитории",
        }
        answer = {
            "relevant": True,
            "findings": [
                {
                    **common,
                    "finding_key": "buyers",
                    "block_index": 1,
                    "block_title": "Покупали",
                    "parent_key": None,
                    "indicator_type": "population_share",
                    "audience": "жители России 18+",
                    "indicator": "покупали подержанные товары",
                    "value": "40%",
                    "percent_base": "жителей России 18+",
                    "behavior_period": "за 6 месяцев",
                    "base_kind": "targetable_explicit",
                    "base_reason": None,
                    "methodology_evidence": None,
                    "targeting_criteria": ["geography", "age"],
                    "evidence": "40% жителей России 18+ покупали подержанные товары за 6 месяцев.",
                    "summary": "40% жителей России 18+ покупали подержанные товары за 6 месяцев.",
                },
                {
                    **common,
                    "finding_key": "classifieds",
                    "block_index": 1,
                    "block_title": "Покупали",
                    "parent_key": "buyers",
                    "indicator_type": "conditional_share",
                    "audience": "покупатели",
                    "indicator": "использовали сайты объявлений",
                    "value": "25%",
                    "percent_base": "Среди покупателей",
                    "behavior_period": None,
                    "base_kind": "conditional",
                    "base_reason": None,
                    "methodology_evidence": None,
                    "targeting_criteria": [],
                    "evidence": "Среди покупателей 25% использовали сайты объявлений.",
                    "summary": "25% покупателей использовали сайты объявлений.",
                },
            ],
        }
        with patch("quality_check.write_report"):
            report = run_check(
                [("Тест", "test", "https://example.org/feed")],
                {"test": lambda url: iter([item])},
                1,
                Path("unused.json"),
                model_call=lambda *args: answer,
            )
        derived = report[0]["accepted_findings"][1]
        self.assertEqual(derived["value"], "10%")
        self.assertEqual(derived["calculation_formula"], "40% × 25% = 10%")
        self.assertEqual(derived["calculated_percent"], "10")

    def test_report_is_written_as_utf8_without_terminal_transcoding(self):
        path = Path("report.json")
        with patch.object(Path, "write_text") as write_text:
            write_report([{"source": "НАФИ", "country": "Россия"}], path)
        payload = write_text.call_args.args[0]
        self.assertEqual(write_text.call_args.kwargs["encoding"], "utf-8")
        raw = payload.encode("utf-8")
        self.assertIn("Россия".encode("utf-8"), raw)
        self.assertEqual(json.loads(raw.decode("utf-8"))[0]["source"], "НАФИ")

    def test_invalid_json_is_reported_without_stopping_other_publications(self):
        items = [
            {
                "title": "сломанный ответ",
                "url": "https://example.org/1",
                "text": "текст 1",
                "published_at": None,
            },
            {
                "title": "успешный ответ",
                "url": "https://example.org/2",
                "text": "текст 2",
                "published_at": None,
            },
        ]
        calls = iter(
            [
                ModelResponseError("невалидный JSON"),
                {"relevant": False, "findings": []},
            ]
        )

        def model_call(*args):
            result = next(calls)
            if isinstance(result, Exception):
                raise result
            return result

        with patch("quality_check.write_report"):
            report = run_check(
                [("Тест", "test", "https://example.org/feed")],
                {"test": lambda url: iter(items)},
                2,
                Path("unused.json"),
                model_call=model_call,
            )
        self.assertEqual(len(report), 2)
        self.assertEqual(report[0]["processing_status"], "error")
        self.assertEqual(report[0]["accepted_findings"], [])
        self.assertEqual(report[1]["processing_status"], "ok")

    def test_openrouter_http_diagnostics_are_safe_and_attributed(self):
        error = OpenRouterHTTPError(
            404,
            "No endpoints; authorization Bearer [REDACTED]",
            "json_schema",
        )
        details = error_details(
            error, "openrouter", "https://openrouter.ai/api/v1/chat/completions"
        )
        self.assertEqual(details["stage"], "openrouter")
        self.assertEqual(details["http_status"], 404)
        self.assertEqual(details["structured_mode"], "json_schema")
        self.assertNotIn("sk-or-", json.dumps(details))


class StructuredResponseTests(unittest.TestCase):
    def setUp(self):
        worker_main.STRICT_JSON_SCHEMA_AVAILABLE = None

    def test_parser_rejects_invalid_json(self):
        with self.assertRaises(ModelResponseError):
            parse_model_content('{"relevant": true, "findings": [}', "stop")

    @patch("worker.main.request_model_once")
    def test_ask_model_retries_invalid_structured_response_once(self, request_once):
        request_once.side_effect = [
            ModelResponseError("невалидный JSON"),
            {"relevant": False, "findings": []},
        ]
        result = ask_model("НАФИ", "Заголовок", "Текст", None)
        self.assertFalse(result["relevant"])
        self.assertEqual(result["_response_meta"]["structured_mode"], "json_object")
        self.assertEqual(request_once.call_count, 2)
        self.assertEqual(
            request_once.call_args_list[0].kwargs["structured_mode"], "json_schema"
        )
        self.assertEqual(
            request_once.call_args_list[1].kwargs["structured_mode"], "json_object"
        )

    @patch("worker.main.request_model_once")
    def test_ask_model_falls_back_from_unsupported_schema_404(self, request_once):
        request_once.side_effect = [
            OpenRouterHTTPError(404, "No endpoints support the parameters", "json_schema"),
            {"relevant": False, "findings": []},
        ]
        result = ask_model("НАФИ", "Заголовок", "Текст", None)
        self.assertFalse(result["relevant"])
        self.assertEqual(result["_response_meta"]["structured_mode"], "json_object")
        self.assertEqual(
            result["_response_meta"]["previous_attempts"][0]["http_status"], 404
        )
        request_once.reset_mock()
        request_once.side_effect = None
        request_once.return_value = {"relevant": False, "findings": []}
        second = ask_model("НАФИ", "Вторая публикация", "Текст", None)
        self.assertEqual(request_once.call_count, 1)
        self.assertEqual(
            request_once.call_args.kwargs["structured_mode"], "json_object"
        )
        self.assertFalse(second["_response_meta"]["strict_schema_available"])

    @patch("worker.main.request_model_once")
    def test_ask_model_raises_after_second_invalid_response(self, request_once):
        request_once.side_effect = ModelResponseError("невалидный JSON")
        with self.assertRaisesRegex(ModelResponseError, "две неудачные попытки"):
            ask_model("НАФИ", "Заголовок", "Текст", None)
        self.assertEqual(request_once.call_count, 2)


if __name__ == "__main__":
    unittest.main()
