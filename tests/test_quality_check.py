import json
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import worker.main as worker_main
from quality_check import error_details, run_check, write_report
from worker.main import (
    ModelResponseError,
    OpenWebUIHTTPError,
    ask_model,
    model_request_body,
    parse_model_content,
    request_model_once,
    require_model_config,
    safe_excerpt,
)


class QualityReportTests(unittest.TestCase):
    def test_quality_report_keeps_source_percentages_without_calculation(self):
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
        accepted = report[0]["accepted_findings"]
        self.assertEqual([finding["value"] for finding in accepted], ["40%", "25%"])
        self.assertTrue(all(finding["calculation_formula"] is None for finding in accepted))
        self.assertTrue(all(finding["calculated_percent"] is None for finding in accepted))

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
        self.assertIsNone(report[0]["thinking_disabled_confirmed"])
        self.assertEqual(report[1]["processing_status"], "ok")

    def test_quality_report_exposes_confirmed_thinking_state(self):
        item = {
            "title": "Контрольный материал",
            "url": "https://example.org/study",
            "text": "Текст",
            "published_at": None,
        }
        answer = {
            "relevant": False,
            "findings": [],
            "_response_meta": {
                "reasoning_tokens": 0,
                "thinking_disabled_confirmed": True,
            },
        }
        with patch("quality_check.write_report"):
            report = run_check(
                [("ФОМ", "test", "https://example.org/feed")],
                {"test": lambda url: iter([item])},
                1,
                Path("unused.json"),
                model_call=lambda *args: answer,
            )
        self.assertEqual(report[0]["response_meta"]["reasoning_tokens"], 0)
        self.assertIs(report[0]["thinking_disabled_confirmed"], True)

    def test_openwebui_http_diagnostics_are_safe_and_attributed(self):
        error = OpenWebUIHTTPError(
            404,
            "No endpoints; authorization Bearer [REDACTED]",
            "json_schema",
        )
        details = error_details(
            error, "openwebui", "https://inside.omirussia.ru/api/chat/completions"
        )
        self.assertEqual(details["stage"], "openwebui")
        self.assertEqual(details["http_status"], 404)
        self.assertEqual(details["structured_mode"], "json_schema")
        self.assertNotIn("secret-key", json.dumps(details))

    def test_openwebui_request_uses_internal_url_and_bearer_key(self):
        response = MagicMock()
        response.read.return_value = json.dumps(
            {
                "choices": [
                    {
                        "message": {"content": '{"relevant": false, "findings": []}'},
                        "finish_reason": "stop",
                    }
                ]
            }
        ).encode("utf-8")
        response.__enter__.return_value = response
        with (
            patch.dict(os.environ, {"OPENWEBUI_API_KEY": "secret-key"}),
            patch.object(worker_main, "MODEL", "test-model"),
            patch.object(
                worker_main,
                "OPENWEBUI_URL",
                "https://inside.omirussia.ru/api/chat/completions",
            ),
            patch("worker.main.urllib.request.urlopen", return_value=response) as open_url,
        ):
            answer = request_model_once("НАФИ", "Заголовок", "Текст", None, "json_object")
        request = open_url.call_args.args[0]
        self.assertEqual(
            request.full_url, "https://inside.omirussia.ru/api/chat/completions"
        )
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-key")
        body = json.loads(request.data)
        self.assertEqual(body["model"], "test-model")
        self.assertNotIn("provider", body)
        self.assertEqual(answer, {"relevant": False, "findings": []})

    def test_model_must_be_explicitly_configured(self):
        with patch.dict(os.environ, {"OPENWEBUI_API_KEY": "dummy-key"}):
            with patch.object(worker_main, "MODEL", ""):
                with self.assertRaisesRegex(RuntimeError, "MODEL"):
                    require_model_config()

    def test_legacy_prompt_version_is_rejected_before_api_request(self):
        for version in ("audience-human-shares-v5", "audience-targetable-bases-v6"):
            with (
                self.subTest(version=version),
                patch.object(worker_main, "PROMPT_VERSION", version),
                patch.object(worker_main, "MODEL", "qwen3.8-27b"),
                patch.dict(os.environ, {"OPENWEBUI_API_KEY": "dummy-key"}),
                patch("worker.main.urllib.request.urlopen") as open_url,
            ):
                with self.assertRaisesRegex(RuntimeError, "PROMPT_VERSION"):
                    request_model_once("ФОМ", "Материал", "Текст", None, "json_object")
                open_url.assert_not_called()

    def test_legacy_prompt_version_stops_worker_before_database_access(self):
        for version in ("audience-human-shares-v5", "audience-targetable-bases-v6"):
            with (
                self.subTest(version=version),
                patch.object(worker_main, "PROMPT_VERSION", version),
                patch("worker.main.db.connect") as connect,
                patch("worker.main.db.init_schema") as init_schema,
            ):
                with self.assertRaisesRegex(RuntimeError, "PROMPT_VERSION"):
                    worker_main.main()
                connect.assert_not_called()
                init_schema.assert_not_called()

    def test_current_and_custom_prompt_versions_pass_configuration_guard(self):
        for version in ("audience-primary-findings-v7", "audience-primary-findings-v7-control"):
            with (
                self.subTest(version=version),
                patch.object(worker_main, "PROMPT_VERSION", version),
                patch.object(worker_main, "MODEL", "qwen3.8-27b"),
                patch.dict(os.environ, {"OPENWEBUI_API_KEY": "dummy-key"}),
            ):
                require_model_config()

    def test_error_excerpt_redacts_internal_key(self):
        with patch.dict(os.environ, {"OPENWEBUI_API_KEY": "internal-secret"}):
            excerpt = safe_excerpt(
                "upstream echoed internal-secret and Bearer internal-secret"
            )
        self.assertNotIn("internal-secret", excerpt)
        self.assertIn("[REDACTED]", excerpt)

    def test_structured_schema_request_has_no_provider_specific_field(self):
        with patch.object(worker_main, "MODEL", "test-model"):
            body = model_request_body("НАФИ", "Заголовок", "Текст", None, "json_schema")
        self.assertNotIn("provider", body)
        self.assertEqual(body["response_format"]["type"], "json_schema")

    def test_qwen_request_disables_thinking_and_keeps_thirty_findings(self):
        with patch.object(worker_main, "MODEL", "qwen3.8-27b"):
            body = model_request_body("ФОМ", "Заголовок", "Текст", None, "json_schema")
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(body["max_tokens"], 10000)
        self.assertEqual(worker_main.MAX_FINDINGS_PER_MATERIAL, 30)
        self.assertIn("от 0 до 30 находок", body["messages"][0]["content"])
        self.assertNotIn("reasoning_effort", body)

    def test_other_model_request_does_not_receive_qwen_thinking_setting(self):
        with patch.object(worker_main, "MODEL", "gpt-6.1-sol"):
            body = model_request_body("ФОМ", "Заголовок", "Текст", None, "json_schema")
        self.assertNotIn("chat_template_kwargs", body)

    def test_openwebui_request_allows_qwen_longer_timeout(self):
        response = MagicMock()
        response.read.return_value = json.dumps(
            {
                "choices": [
                    {
                        "message": {"content": '{"relevant": false, "findings": []}'},
                        "finish_reason": "stop",
                    }
                ]
            }
        ).encode("utf-8")
        response.__enter__.return_value = response
        with (
            patch.dict(os.environ, {"OPENWEBUI_API_KEY": "test-key"}),
            patch.object(worker_main, "MODEL", "qwen3.8-27b"),
            patch("worker.main.urllib.request.urlopen", return_value=response) as open_url,
        ):
            answer = request_model_once("ФОМ", "Заголовок", "Текст", None, "json_schema")
        self.assertEqual(open_url.call_args.kwargs["timeout"], 240)
        self.assertFalse(answer["relevant"])

    def test_strict_schema_uses_only_supported_constraints(self):
        schema = worker_main.RESPONSE_JSON_SCHEMA
        finding = schema["properties"]["findings"]["items"]
        for object_schema in (schema, finding):
            self.assertIs(object_schema["additionalProperties"], False)
            self.assertEqual(
                set(object_schema["required"]), set(object_schema["properties"])
            )

        def check_keywords(node):
            if isinstance(node, dict):
                self.assertFalse(
                    {"uniqueItems", "minLength", "minimum"}.intersection(node),
                    node,
                )
                for value in node.values():
                    check_keywords(value)
            elif isinstance(node, list):
                for value in node:
                    check_keywords(value)

        check_keywords(schema)


class StructuredResponseTests(unittest.TestCase):
    def setUp(self):
        worker_main.STRICT_JSON_SCHEMA_AVAILABLE = None

    def test_parser_rejects_invalid_json(self):
        with self.assertRaises(ModelResponseError):
            parse_model_content('{"relevant": true, "findings": [}', "stop")

    def test_qwen_reasoning_usage_is_reflected_in_response_metadata(self):
        cases = ((0, True), (12, False), (None, None))
        for reasoning_tokens, expected in cases:
            with self.subTest(reasoning_tokens=reasoning_tokens):
                payload = {
                    "choices": [
                        {
                            "message": {"content": '{"relevant": false, "findings": []}'},
                            "finish_reason": "stop",
                        }
                    ]
                }
                if reasoning_tokens is not None:
                    payload["usage"] = {
                        "completion_tokens_details": {
                            "reasoning_tokens": reasoning_tokens
                        }
                    }
                response = MagicMock()
                response.read.return_value = json.dumps(payload).encode("utf-8")
                response.__enter__.return_value = response
                with (
                    patch.dict(os.environ, {"OPENWEBUI_API_KEY": "test-key"}),
                    patch.object(worker_main, "MODEL", "qwen3.8-27b"),
                    patch.object(worker_main, "STRICT_JSON_SCHEMA_AVAILABLE", None),
                    patch("worker.main.urllib.request.urlopen", return_value=response),
                ):
                    answer = ask_model("ФОМ", "Тест", "Текст", None)
                meta = answer["_response_meta"]
                self.assertEqual(meta["reasoning_tokens"], reasoning_tokens)
                self.assertIs(meta["thinking_disabled_confirmed"], expected)
                self.assertNotIn("usage", meta)

    def test_non_qwen_reasoning_usage_does_not_claim_thinking_disabled(self):
        response = MagicMock()
        response.read.return_value = json.dumps(
            {
                "choices": [
                    {
                        "message": {"content": '{"relevant": false, "findings": []}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"completion_tokens_details": {"reasoning_tokens": 0}},
            }
        ).encode("utf-8")
        response.__enter__.return_value = response
        with (
            patch.dict(os.environ, {"OPENWEBUI_API_KEY": "test-key"}),
            patch.object(worker_main, "MODEL", "gpt-6.1-sol"),
            patch.object(worker_main, "STRICT_JSON_SCHEMA_AVAILABLE", None),
            patch("worker.main.urllib.request.urlopen", return_value=response),
        ):
            answer = ask_model("ФОМ", "Тест", "Текст", None)
        self.assertIsNone(answer["_response_meta"]["thinking_disabled_confirmed"])

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
    def test_ask_model_falls_back_from_unsupported_schema_400(self, request_once):
        request_once.side_effect = [
            OpenWebUIHTTPError(400, "unsupported response_format json_schema", "json_schema"),
            {"relevant": False, "findings": []},
        ]
        result = ask_model("НАФИ", "Заголовок", "Текст", None)
        self.assertFalse(result["relevant"])
        self.assertEqual(result["_response_meta"]["structured_mode"], "json_object")
        self.assertEqual(
            result["_response_meta"]["previous_attempts"][0]["http_status"], 400
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
    def test_ask_model_retries_qwen_grammar_rejection(self, request_once):
        request_once.side_effect = [
            OpenWebUIHTTPError(
                400,
                'Grammar error: Unimplemented keys: ["uniqueItems"]',
                "json_schema",
            ),
            {"relevant": False, "findings": []},
        ]
        result = ask_model("НАФИ", "Заголовок", "Текст", None)
        self.assertEqual(result["_response_meta"]["structured_mode"], "json_object")
        self.assertEqual(request_once.call_count, 2)
        self.assertEqual(
            request_once.call_args_list[1].kwargs["structured_mode"], "json_object"
        )

    @patch("worker.main.request_model_once")
    def test_ask_model_does_not_treat_missing_model_as_schema_failure(self, request_once):
        request_once.side_effect = OpenWebUIHTTPError(
            404, "model not found", "json_schema"
        )
        with self.assertRaises(OpenWebUIHTTPError):
            ask_model("НАФИ", "Заголовок", "Текст", None)
        self.assertEqual(request_once.call_count, 1)
        self.assertIsNone(worker_main.STRICT_JSON_SCHEMA_AVAILABLE)

    @patch("worker.main.request_model_once")
    def test_ask_model_raises_after_second_invalid_response(self, request_once):
        request_once.side_effect = ModelResponseError("невалидный JSON")
        with self.assertRaisesRegex(ModelResponseError, "две неудачные попытки"):
            ask_model("НАФИ", "Заголовок", "Текст", None)
        self.assertEqual(request_once.call_count, 2)


if __name__ == "__main__":
    unittest.main()
