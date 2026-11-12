import unittest
from pathlib import Path
from unittest.mock import patch

from worker.main import (
    DEFAULT_PROMPT_VERSION,
    ModelResponseError,
    load_targeting_criteria,
    model_request_body,
    parse_material_ids,
    partition_targeting_criteria,
    pending_materials,
    prepare_source_findings,
    process_new,
    validate_answer,
)


TEXT = (
    "Исследование проведено среди 1500 россиян старше 18 лет. "
    "40% россиян называют натуральность важным фактором выбора напитков. "
    "Среди покупателей кофе 22% выбирают растворимый кофе ежедневно."
)


class WorkerValidationTests(unittest.TestCase):
    def test_source_findings_keep_article_percentages_without_calculation(self):
        rows = prepare_source_findings(
            [
                {"value": "50%", "percent_base": "взрослые россияне"},
                {
                    "value": "21%",
                    "percent_base": "покупатели подержанных товаров",
                    "parent_finding_index": 1,
                },
            ]
        )

        self.assertEqual([row["value"] for row in rows], ["50%", "21%"])
        self.assertEqual([row["source_value"] for row in rows], ["50%", "21%"])
        self.assertTrue(all(row["is_derived"] is False for row in rows))
        self.assertTrue(all(row["calculated_percent"] is None for row in rows))
        self.assertTrue(all(row["calculation_formula"] is None for row in rows))

    def test_accepts_supported_population_share(self):
        answer = {
            "relevant": True,
            "findings": [
                {
                    "topic": "напитки",
                    "indicator_type": "population_share",
                    "ir_applicability": "direct",
                    "geography": "Россия",
                    "audience": "россияне 18+",
                    "indicator": "важность натуральности",
                    "value": "40%",
                    "percent_base": "1500 россиян старше 18 лет",
                    "study_period": None,
                    "useful_for": "покупатели натуральных продуктов",
                    "evidence": "40% россиян называют натуральность важным фактором выбора напитков.",
                    "summary": "Для 40% россиян натуральность важна при выборе напитков.",
                }
            ],
        }
        rows = validate_answer(answer, TEXT)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["indicator_type"], "population_share")
        self.assertEqual(rows[0]["ir_applicability"], "direct")

    def test_population_share_inside_users_becomes_conditional(self):
        answer = {
            "relevant": True,
            "findings": [
                {
                    "indicator_type": "population_share",
                    "ir_applicability": "direct",
                    "audience": "пользователи сайтов объявлений, продававшие товары за последний год",
                    "percent_base": "пользователей сайтов объявлений",
                    "value": "50%",
                    "evidence": "50% пользователей сайтов объявлений продали товары на сумму не более 10 тыс. рублей.",
                    "summary": "Половина пользователей продала товары не более чем на 10 тыс. рублей.",
                }
            ],
        }
        text = "50% пользователей сайтов объявлений продали товары на сумму не более 10 тыс. рублей."
        rows = validate_answer(answer, text)
        self.assertEqual(rows[0]["indicator_type"], "conditional_share")
        self.assertEqual(rows[0]["ir_applicability"], "direct")

    def test_conditional_share_without_verifiable_base_is_rejected(self):
        answer = {
            "relevant": True,
            "findings": [
                {
                    "indicator_type": "conditional_share",
                    "ir_applicability": "direct",
                    "audience": "покупатели кофе",
                    "percent_base": "все жители крупных городов",
                    "value": "22%",
                    "evidence": "Среди покупателей кофе 22% выбирают растворимый кофе ежедневно.",
                    "summary": "22% покупателей кофе выбирают растворимый кофе ежедневно.",
                }
            ],
        }
        self.assertEqual(validate_answer(answer, TEXT), [])

    def test_standalone_share_with_known_human_base_stays_conditional(self):
        answer = {
            "relevant": True,
            "findings": [
                {
                    "finding_key": "coffee",
                    "block_index": 1,
                    "parent_key": None,
                    "indicator_type": "conditional_share",
                    "ir_applicability": "direct",
                    "audience": "покупатели кофе",
                    "value": "22%",
                    "percent_base": "Среди покупателей кофе",
                    "base_kind": "unknown",
                    "evidence": "Среди покупателей кофе 22% выбирают растворимый кофе ежедневно.",
                    "summary": "22% покупателей кофе выбирают растворимый кофе ежедневно.",
                }
            ],
        }
        rows = prepare_source_findings(validate_answer(answer, TEXT))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["base_kind"], "conditional")
        self.assertEqual(rows[0]["value"], "22%")
        self.assertIsNone(rows[0]["calculation_formula"])

    def test_market_metric_is_rejected(self):
        answer = {
            "relevant": True,
            "findings": [
                {
                    "indicator_type": "market_metric",
                    "ir_applicability": "direct",
                    "value": "12 млн заказов",
                    "evidence": "Рынок достиг 12 млн заказов за месяц.",
                    "summary": "Рынок достиг 12 млн заказов.",
                }
            ],
        }
        self.assertEqual(
            validate_answer(answer, "Рынок достиг 12 млн заказов за месяц."), []
        )

    def test_accepts_percentage_characteristics_of_people_including_b2b(self):
        cases = (
            (
                "behavior",
                "38% россиян покупали подержанные товары.",
                "россиян",
                "покупали подержанные товары",
            ),
            (
                "ownership",
                "24% жителей имеют собаку.",
                "жителей",
                "имеют собаку",
            ),
            (
                "awareness",
                "61% потребителей знают этот бренд.",
                "потребителей",
                "знают бренд",
            ),
            (
                "intention",
                "19% респондентов планируют купить автомобиль.",
                "респондентов",
                "планируют купить автомобиль",
            ),
            (
                "opinion",
                "47% опрошенных доверяют онлайн-опросам.",
                "опрошенных",
                "доверяют онлайн-опросам",
            ),
            (
                "b2b",
                "42% руководителей отделов закупок используют сервис.",
                "руководителей отделов закупок",
                "используют сервис",
            ),
        )
        for name, evidence, percent_base, indicator in cases:
            with self.subTest(name=name):
                value = evidence.split("%", 1)[0] + "%"
                answer = {
                    "relevant": True,
                    "findings": [
                        {
                            "indicator_type": "conditional_share",
                            "ir_applicability": "direct",
                            "audience": percent_base,
                            "indicator": indicator,
                            "value": value,
                            "percent_base": percent_base,
                            "evidence": evidence,
                            "summary": evidence,
                        }
                    ],
                }
                rows = validate_answer(answer, evidence)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["value"], value)

    def test_rejects_non_human_and_non_percentage_metrics(self):
        cases = (
            ("absolute", "1500 человек приняли участие.", "1500", "человек"),
            (
                "orders",
                "30% заказов оформлено пользователями онлайн.",
                "30%",
                "заказов",
            ),
            ("companies", "25% компаний используют сервис.", "25%", "компаний"),
            ("market", "Рынок вырос на 15%.", "15%", "рынка"),
            ("forecast", "Продажи вырастут на 12%.", "12%", "продаж"),
            ("average_check", "Средний чек вырос на 8%.", "8%", "среднего чека"),
        )
        for name, evidence, value, percent_base in cases:
            with self.subTest(name=name):
                answer = {
                    "relevant": True,
                    "findings": [
                        {
                            # Even a model classification error must not let the metric through.
                            "indicator_type": "population_share",
                            "ir_applicability": "direct",
                            "audience": percent_base,
                            "value": value,
                            "percent_base": percent_base,
                            "evidence": evidence,
                            "summary": evidence,
                        }
                    ],
                }
                self.assertEqual(validate_answer(answer, evidence), [])

    def test_rejects_number_missing_from_evidence(self):
        answer = {
            "relevant": True,
            "findings": [
                {
                    "indicator_type": "population_share",
                    "value": "75%",
                    "evidence": "40% россиян называют натуральность важным фактором выбора напитков.",
                    "summary": "Большинство выбирает натуральные напитки.",
                }
            ],
        }
        self.assertEqual(validate_answer(answer, TEXT), [])

    def test_rejects_non_verbatim_evidence(self):
        answer = {
            "relevant": True,
            "findings": [
                {
                    "indicator_type": "conditional_share",
                    "value": "22%",
                    "evidence": "22% покупателей ежедневно пьют растворимый кофе.",
                    "summary": "22% покупателей кофе выбирают растворимый кофе ежедневно.",
                }
            ],
        }
        self.assertEqual(validate_answer(answer, TEXT), [])

    def test_invalid_type_is_rejected(self):
        answer = {
            "relevant": True,
            "findings": [
                {
                    "indicator_type": "made_up_type",
                    "value": None,
                    "evidence": None,
                    "summary": "Материал описывает методику исследования.",
                }
            ],
        }
        self.assertEqual(validate_answer(answer, TEXT), [])

    def test_irrelevant_material_has_no_findings(self):
        self.assertEqual(validate_answer({"relevant": False, "findings": []}, TEXT), [])


class VerbalShareTests(unittest.TestCase):
    @staticmethod
    def answer(value, evidence, percent_base="респондентов", summary=None):
        return {
            "relevant": True,
            "findings": [
                {
                    "finding_key": "share",
                    "block_index": 1,
                    "parent_key": None,
                    "indicator_type": "population_share",
                    "ir_applicability": "direct",
                    "audience": "взрослые россияне",
                    "percent_base": percent_base,
                    "value": value,
                    "evidence": evidence,
                    "summary": summary or f"{value} взрослых россиян сообщили об изменении.",
                }
            ],
        }

    def test_two_thirds_becomes_approximate_67_percent_with_original_source_value(self):
        evidence = "Две трети респондентов не заметили перемен."
        rows = prepare_source_findings(
            validate_answer(self.answer("67%", evidence), evidence)
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["value"], "67%")
        self.assertEqual(rows[0]["source_value"].lower(), "две трети")
        self.assertEqual(rows[0]["evidence"], evidence)
        self.assertIn("около 67%", rows[0]["summary"].lower())
        self.assertIsNone(rows[0]["calculated_percent"])

    def test_exact_half_does_not_need_approximate_wording(self):
        evidence = "Половина респондентов купили подержанный товар."
        rows = prepare_source_findings(
            validate_answer(self.answer("50%", evidence), evidence)
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_value"].lower(), "половина")
        self.assertNotIn("около", rows[0]["summary"].lower())

    def test_one_third_rounds_to_approximate_33_percent(self):
        evidence = "Треть респондентов пользуется сервисом."
        rows = prepare_source_findings(
            validate_answer(self.answer("33%", evidence), evidence)
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_value"].lower(), "треть")
        self.assertIn("около 33%", rows[0]["summary"].lower())

    def test_fraction_must_match_the_claimed_percentage(self):
        evidence = "Две трети респондентов не заметили перемен."
        self.assertEqual(validate_answer(self.answer("68%", evidence), evidence), [])
        self.assertEqual(validate_answer(self.answer("50%", evidence), evidence), [])

    def test_verbal_fraction_requires_human_denominator_and_verbatim_evidence(self):
        evidence = "Две трети заказов оформлены онлайн."
        self.assertEqual(
            validate_answer(self.answer("67%", evidence, "заказов"), evidence), []
        )
        fabricated = "Две трети респондентов не заметили перемен."
        source = "Половина респондентов не заметила перемен."
        self.assertEqual(validate_answer(self.answer("67%", fabricated), source), [])

    def test_numeric_percent_keeps_existing_source_value_and_quote(self):
        evidence = "40% респондентов покупали подержанные товары."
        rows = prepare_source_findings(
            validate_answer(self.answer("40%", evidence), evidence)
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["value"], "40%")
        self.assertEqual(rows[0]["source_value"], "40%")
        self.assertEqual(rows[0]["evidence"], evidence)


class TargetingCriteriaTests(unittest.TestCase):
    def test_f2_prompt_version_and_control_reprocessing_docs_are_synchronized(self):
        root = Path(__file__).parents[1]
        env_example = (root / ".env.example").read_text(encoding="utf-8")
        compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
        readme = (root / "README.md").read_text(encoding="utf-8")

        self.assertEqual(DEFAULT_PROMPT_VERSION, "audience-human-shares-v4")
        self.assertIn(f"PROMPT_VERSION={DEFAULT_PROMPT_VERSION}", env_example)
        self.assertIn(
            f"PROMPT_VERSION=${{PROMPT_VERSION:-{DEFAULT_PROMPT_VERSION}}}",
            compose,
        )
        self.assertIn("REPROCESS_OLD=0", env_example)
        self.assertIn("REPROCESS_OLD=${REPROCESS_OLD:-0}", compose)
        self.assertIn("REPROCESS_MATERIAL_IDS=<ID>,29,30", readme)
        self.assertRegex(
            readme,
            r"Полный архив запускается только после отдельного\s+подтверждения пользователя",
        )
        self.assertNotIn("audience-targetable-v3", env_example + compose + readme)

    def test_control_reprocessing_ids_are_strict_positive_integers(self):
        self.assertEqual(parse_material_ids("10, 20,10"), (10, 20))
        with self.assertRaisesRegex(ValueError, "положительные id"):
            parse_material_ids("10, all")

    def test_pending_materials_can_limit_old_reprocessing_to_control_ids(self):
        class Connection:
            def __init__(self):
                self.query = None
                self.params = None

            def execute(self, query, params=None):
                self.query, self.params = query, params
                return Rows([])

        conn = Connection()
        with (
            patch("worker.main.REPROCESS_OLD", True),
            patch("worker.main.REPROCESS_MATERIAL_IDS", (10, 20)),
        ):
            pending_materials(conn)
        self.assertIn("m.id = ANY(%s)", conn.query)
        self.assertIn([10, 20], conn.params)

    def test_loads_explicit_targeting_criteria(self):
        criteria = load_targeting_criteria()
        self.assertIn("geography", criteria)
        self.assertIn("gender", criteria)
        self.assertIn("dog_ownership", criteria)
        self.assertIn("business_size", criteria)

    def test_unknown_criterion_is_candidate_not_approved(self):
        approved, candidates = partition_targeting_criteria(
            ["age", "income", "age"]
        )
        self.assertEqual(approved, ["age"])
        self.assertEqual(candidates, ["income"])

    def test_rejects_duplicate_config_ids(self):
        duplicate_config = (
            '{"version":1,"criteria":['
            '{"id":"age","label":"возраст","synonyms":[]},'
            '{"id":"age","label":"возраст","synonyms":[]}]}'
        )
        with patch("worker.main.Path.read_text", return_value=duplicate_config):
            with self.assertRaisesRegex(ValueError, "повторяющийся"):
                load_targeting_criteria("criteria.json")

    def test_schema_is_additive_for_calculation_fields(self):
        schema = (Path(__file__).parents[1] / "db" / "schema.sql").read_text(
            encoding="utf-8"
        )
        for column in (
            "block_index",
            "block_position",
            "parent_finding_index",
            "calculated_percent",
            "calculation_chain",
            "targeting_criteria",
            "candidate_criteria",
        ):
            self.assertIn(
                f"ADD COLUMN IF NOT EXISTS {column}", schema
            )
        self.assertNotIn("DROP TABLE", schema.upper())


class FindingBlockExtractionTests(unittest.TestCase):
    def test_prompt_renders_criteria_and_twenty_finding_limit(self):
        body = model_request_body("НАФИ", "Тест", "Текст", None, "json_object")
        prompt = body["messages"][0]["content"]
        self.assertIn("geography: география", prompt)
        self.assertIn("от 0 до 20 находок", prompt)
        self.assertIn("знаменателем всегда должны быть люди", prompt.lower())
        self.assertNotIn("absolute_count", prompt)
        self.assertNotIn("market_metric", prompt)

    def test_extracts_two_blocks_with_parent_links_and_periods(self):
        text = (
            "Опрос проведен среди жителей России в возрасте от 18 лет. "
            "38% респондентов покупали подержанные товары за последние 6 месяцев. "
            "Среди покупателей 33% использовали сайты объявлений. "
            "30% респондентов продавали подержанные товары за последние 6 месяцев. "
            "Среди продавцов 17% продавали автомобили."
        )
        shared = {
            "topic": "подержанные товары",
            "indicator_type": "population_share",
            "ir_applicability": "direct",
            "geography": "Россия",
            "study_period": None,
            "useful_for": "рынок подержанных товаров",
        }
        answer = {
            "relevant": True,
            "findings": [
                {
                    **shared,
                    "finding_key": "buyers",
                    "block_index": 1,
                    "block_title": "Покупали",
                    "parent_key": None,
                    "audience": "жители России 18+",
                    "indicator": "покупали подержанные товары",
                    "value": "38%",
                    "percent_base": "жителей России в возрасте от 18 лет",
                    "behavior_period": "за последние 6 месяцев",
                    "base_kind": "targetable_inferred",
                    "base_reason": "описание выборки задает базу исследования",
                    "methodology_evidence": "Опрос проведен среди жителей России в возрасте от 18 лет.",
                    "targeting_criteria": ["geography", "age"],
                    "evidence": "38% респондентов покупали подержанные товары за последние 6 месяцев.",
                    "summary": "38% жителей России 18+ покупали подержанные товары за последние 6 месяцев.",
                },
                {
                    **shared,
                    "finding_key": "buyers_classifieds",
                    "block_index": 1,
                    "block_title": "Покупали",
                    "parent_key": "buyers",
                    "indicator_type": "conditional_share",
                    "audience": "покупатели подержанных товаров",
                    "indicator": "использовали сайты объявлений",
                    "value": "33%",
                    "percent_base": "Среди покупателей",
                    "behavior_period": None,
                    "base_kind": "conditional",
                    "base_reason": None,
                    "methodology_evidence": None,
                    "targeting_criteria": ["income"],
                    "evidence": "Среди покупателей 33% использовали сайты объявлений.",
                    "summary": "33% покупателей использовали сайты объявлений.",
                },
                {
                    **shared,
                    "finding_key": "sellers",
                    "block_index": 2,
                    "block_title": "Продавали",
                    "parent_key": None,
                    "audience": "жители России 18+",
                    "indicator": "продавали подержанные товары",
                    "value": "30%",
                    "percent_base": "жителей России в возрасте от 18 лет",
                    "behavior_period": "за последние 6 месяцев",
                    "base_kind": "targetable_inferred",
                    "base_reason": "описание выборки задает базу исследования",
                    "methodology_evidence": "Опрос проведен среди жителей России в возрасте от 18 лет.",
                    "targeting_criteria": ["geography", "age"],
                    "evidence": "30% респондентов продавали подержанные товары за последние 6 месяцев.",
                    "summary": "30% жителей России 18+ продавали подержанные товары за последние 6 месяцев.",
                },
                {
                    **shared,
                    "finding_key": "seller_cars",
                    "block_index": 2,
                    "block_title": "Продавали",
                    "parent_key": "sellers",
                    "indicator_type": "conditional_share",
                    "audience": "продавцы подержанных товаров",
                    "indicator": "продавали автомобили",
                    "value": "17%",
                    "percent_base": "Среди продавцов",
                    "behavior_period": None,
                    "base_kind": "conditional",
                    "base_reason": None,
                    "methodology_evidence": None,
                    "targeting_criteria": [],
                    "evidence": "Среди продавцов 17% продавали автомобили.",
                    "summary": "17% продавцов продавали автомобили.",
                },
            ],
        }

        rows = validate_answer(answer, text)

        self.assertEqual([row["block_index"] for row in rows], [1, 1, 2, 2])
        self.assertEqual([row["block_position"] for row in rows], [1, 2, 1, 2])
        self.assertIsNone(rows[0]["parent_finding_index"])
        self.assertEqual(rows[1]["parent_finding_index"], 1)
        self.assertIsNone(rows[2]["parent_finding_index"])
        self.assertEqual(rows[3]["parent_finding_index"], 3)
        self.assertEqual(rows[0]["behavior_period"], "за последние 6 месяцев")
        self.assertEqual(rows[0]["base_kind"], "targetable_inferred")
        self.assertEqual(rows[1]["candidate_criteria"], ["income"])

    def test_inferred_base_without_methodology_quote_becomes_unknown(self):
        answer = {
            "relevant": True,
            "findings": [
                {
                    "finding_key": "base",
                    "block_index": 1,
                    "block_title": "Покупали",
                    "parent_key": None,
                    "indicator_type": "population_share",
                    "value": "38%",
                    "percent_base": None,
                    "base_kind": "targetable_inferred",
                    "base_reason": "предположение модели",
                    "methodology_evidence": "Несуществующая цитата",
                    "targeting_criteria": ["geography"],
                    "evidence": "38% респондентов покупали товары.",
                    "summary": "38% респондентов покупали товары.",
                }
            ],
        }
        rows = validate_answer(answer, "38% респондентов покупали товары.")
        self.assertEqual(rows[0]["base_kind"], "unknown")

    def test_parent_from_another_block_is_not_linked(self):
        answer = {
            "relevant": True,
            "findings": [
                {
                    "finding_key": "base",
                    "block_index": 1,
                    "parent_key": None,
                    "indicator_type": "population_share",
                    "audience": "респонденты",
                    "percent_base": "респондентов",
                    "value": "40%",
                    "evidence": "40% респондентов покупали товары.",
                    "summary": "40% респондентов покупали товары.",
                },
                {
                    "finding_key": "child",
                    "block_index": 2,
                    "parent_key": "base",
                    "indicator_type": "conditional_share",
                    "audience": "респонденты",
                    "percent_base": "респондентов",
                    "value": "20%",
                    "evidence": "20% респондентов выбирали сайты объявлений.",
                    "summary": "20% респондентов выбирали сайты объявлений.",
                },
            ],
        }
        rows = validate_answer(
            answer,
            "40% респондентов покупали товары. "
            "20% респондентов выбирали сайты объявлений.",
        )
        self.assertIsNone(rows[1]["parent_finding_index"])


class Rows:
    def __init__(self, rows=None, rowcount=0):
        self.rows = rows or []
        self.rowcount = rowcount

    def fetchall(self):
        return self.rows


class FailingMaterialConnection:
    def __init__(self):
        self.queries = []

    def execute(self, query, params=None):
        self.queries.append((query, params))
        if "FROM materials m" in query:
            return Rows([(1, "НАФИ", "Заголовок", "Текст", None, None)])
        return Rows(rowcount=1)


class WorkerFailureTests(unittest.TestCase):
    @patch("worker.main.db.finish_run")
    @patch("worker.main.db.start_run", return_value=10)
    @patch("worker.main.ask_model", side_effect=ModelResponseError("невалидный JSON"))
    def test_invalid_response_marks_material_failed_without_saving_findings(
        self, ask_model_mock, start_run_mock, finish_run_mock
    ):
        conn = FailingMaterialConnection()
        process_new(conn)
        sql = "\n".join(query for query, params in conn.queries)
        self.assertIn("processing_error", sql)
        self.assertIn("status = 'failed'", sql)
        self.assertNotIn("INSERT INTO findings", sql)


if __name__ == "__main__":
    unittest.main()
