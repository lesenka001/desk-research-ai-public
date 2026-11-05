import unittest
from contextlib import nullcontext
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from worker.main import (
    DEFAULT_PROMPT_VERSION,
    ModelResponseError,
    build_composite_bases,
    calculate_derived_findings,
    classify_finding,
    load_targeting_criteria,
    model_request_body,
    parse_material_ids,
    partition_targeting_criteria,
    pending_materials,
    process_new,
    save_result,
    validate_answer,
)


TEXT = (
    "Исследование проведено среди 1500 россиян старше 18 лет. "
    "40% россиян называют натуральность важным фактором выбора напитков. "
    "Среди покупателей кофе 22% выбирают растворимый кофе ежедневно."
)


class WorkerValidationTests(unittest.TestCase):
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
        rows = calculate_derived_findings(validate_answer(answer, TEXT))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["finding_role"], "conditional")
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
            "scale_choice_mode",
            "question_audience",
            "general_population",
            "behavior_period_origin",
            "composite_base_key",
            "is_composite_base",
            "composite_component_indices",
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

        strict_body = model_request_body(
            "ФОМ", "Тест", "Текст", None, "json_schema"
        )
        finding_schema = strict_body["response_format"]["json_schema"]["schema"][
            "properties"
        ]["findings"]["items"]
        for field in (
            "scale_key",
            "scale_category",
            "scale_choice_mode",
            "scale_evidence",
            "composite_base_key",
            "composite_base_label",
            "composite_base_behavior_period",
            "include_in_composite_base",
            "question_audience",
            "question_audience_evidence",
            "general_population",
            "general_population_reason",
            "general_population_evidence",
            "behavior_period_origin",
            "behavior_period_evidence",
        ):
            self.assertIn(field, finding_schema["required"])

    def test_extracts_scale_question_audiences_and_general_population(self):
        methodology = (
            "«ФОМнибус» – всероссийский опрос россиян в возрасте 18 лет и старше, "
            "51 субъект РФ, 97 населенных пунктов."
        )
        frequency = (
            "Обычной практикой покупку подержанных товаров называют 14% опрошенных, "
            "еще 36% приобретали их несколько раз."
        )
        buyer_question = (
            "Вопрос задавался тем, кто покупал подержанные товары, – отвечали 50% "
            "респондентов. Карточка, любое число ответов."
        )
        seller_question = (
            "Вопрос задавался тем, кто продавал подержанные товары. Карточка, "
            "любое число ответов."
        )
        text = " ".join(
            (
                methodology,
                frequency,
                buyer_question,
                "Автомобили покупали 21% покупателей.",
                "Подержанные товары продавали 38% респондентов.",
                seller_question,
                "Автомобили продавали 15% продавцов.",
            )
        )

        def finding(key, value, evidence, **overrides):
            row = {
                "finding_key": key,
                "block_index": 1,
                "block_title": "Покупка",
                "parent_key": None,
                "topic": "подержанные товары",
                "indicator_type": "population_share",
                "ir_applicability": "direct",
                "audience": "взрослые россияне",
                "value": value,
                "percent_base": "опрошенных",
                "behavior_period_origin": "not_stated",
                "scale_choice_mode": "not_applicable",
                "general_population": "взрослые россияне",
                "general_population_reason": "всероссийская выборка людей 18+",
                "general_population_evidence": methodology,
                "evidence": evidence,
                "summary": evidence,
            }
            row.update(overrides)
            return row

        scale_fields = {
            "scale_key": "second_hand_purchase_frequency",
            "scale_choice_mode": "single_choice",
            "scale_evidence": frequency,
        }
        answer = {
            "relevant": True,
            "findings": [
                finding(
                    "regular_buyers",
                    "14%",
                    frequency,
                    **scale_fields,
                    scale_category="обычная практика",
                ),
                finding(
                    "several_times_buyers",
                    "36%",
                    frequency,
                    **scale_fields,
                    scale_category="приобретали несколько раз",
                ),
                finding(
                    "buyer_cars",
                    "21%",
                    "Автомобили покупали 21% покупателей.",
                    parent_key=None,
                    indicator_type="conditional_share",
                    audience="покупатели подержанных товаров",
                    percent_base="покупателей",
                    scale_key="purchased_categories",
                    scale_category="автомобили",
                    scale_choice_mode="multiple_choice",
                    scale_evidence=buyer_question,
                    question_audience="покупатели подержанных товаров",
                    question_audience_evidence=buyer_question,
                ),
                finding(
                    "sellers",
                    "38%",
                    "Подержанные товары продавали 38% респондентов.",
                    block_index=2,
                    block_title="Продажа",
                ),
                finding(
                    "seller_cars",
                    "15%",
                    "Автомобили продавали 15% продавцов.",
                    block_index=2,
                    block_title="Продажа",
                    indicator_type="conditional_share",
                    audience="продавцы подержанных товаров",
                    percent_base="продавцов",
                    scale_key="sold_categories",
                    scale_category="автомобили",
                    scale_choice_mode="multiple_choice",
                    scale_evidence=seller_question,
                    question_audience="продавцы подержанных товаров",
                    question_audience_evidence=seller_question,
                ),
            ],
        }

        rows = validate_answer(answer, text)

        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["scale_key"], rows[1]["scale_key"])
        self.assertEqual(rows[0]["scale_choice_mode"], "single_choice")
        self.assertEqual(rows[2]["scale_choice_mode"], "multiple_choice")
        self.assertEqual(rows[2]["question_audience"], "покупатели подержанных товаров")
        self.assertEqual(rows[4]["question_audience"], "продавцы подержанных товаров")
        self.assertTrue(all(row["general_population"] == "взрослые россияне" for row in rows))

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


class DerivedCalculationTests(unittest.TestCase):
    @staticmethod
    def finding(index, value, parent=None, indicator=None, **overrides):
        finding = {
            "finding_index": index,
            "finding_key": f"finding_{index}",
            "block_index": 1,
            "block_position": index,
            "block_title": "Покупали",
            "parent_finding_index": parent,
            "topic": "подержанные товары",
            "indicator_type": "population_share" if parent is None else "conditional_share",
            "ir_applicability": "direct",
            "geography": "Россия",
            "audience": "населения России в возрасте 18+" if parent is None else "покупателей",
            "indicator": indicator or "покупали подержанные товары",
            "value": value,
            "percent_base": "население России 18+" if parent is None else "покупатели",
            "study_period": None,
            "behavior_period": "за последние 6 месяцев" if parent is None else None,
            "base_kind": "targetable_explicit" if parent is None else "conditional",
            "base_reason": None,
            "methodology_evidence": None,
            "targeting_criteria": ["geography", "age"] if parent is None else [],
            "candidate_criteria": [],
            "useful_for": "оценка аудитории",
            "evidence": f"В тексте указано {value}.",
            "summary": f"{value} респондентов имеют признак.",
        }
        finding.update(overrides)
        return finding

    def test_calculates_control_percentages_and_rounds_only_display(self):
        rows = calculate_derived_findings(
            [
                self.finding(1, "38%"),
                self.finding(2, "33%", 1, "покупали через сайты объявлений"),
                self.finding(3, "35%", 1, "руководствовались экономией"),
                self.finding(4, "21%", 1, "покупали подержанные автомобили"),
            ]
        )
        self.assertEqual(
            [row["calculated_percent"] for row in rows[1:]],
            [Decimal("12.54"), Decimal("13.3"), Decimal("7.98")],
        )
        self.assertEqual([row["value"] for row in rows], ["38%", "13%", "13%", "8%"])
        self.assertEqual(
            [row["finding_role"] for row in rows],
            ["base", "derived", "derived", "derived"],
        )
        self.assertEqual(rows[1]["calculation_formula"], "38% × 33% = 12,54%")
        self.assertIn("населения России в возрасте 18+", rows[1]["summary"])
        self.assertIn("за последние 6 месяцев", rows[1]["summary"])
        self.assertEqual(rows[1]["source_value"], "33%")

    def test_calculates_multistep_chain(self):
        rows = calculate_derived_findings(
            [
                self.finding(1, "40%"),
                self.finding(2, "50%", 1, "использовали сайты объявлений"),
                self.finding(3, "25%", 2, "покупали автомобиль"),
            ]
        )
        self.assertEqual(rows[2]["calculated_percent"], Decimal("5"))
        self.assertEqual(rows[2]["value"], "5%")
        self.assertEqual(rows[2]["calculation_formula"], "40% × 50% × 25% = 5%")
        self.assertEqual(len(rows[2]["calculation_chain"]), 3)

    def test_composite_base_sums_only_selected_single_choice_categories(self):
        common = {
            "behavior_period": None,
            "scale_key": "purchase_frequency",
            "scale_choice_mode": "single_choice",
            "scale_evidence": "единая шкала частоты",
            "composite_base_key": "ever_bought",
            "composite_base_label": (
                "взрослых россиян когда-либо приобретали подержанные товары"
            ),
            "composite_base_behavior_period": "когда-либо",
            "include_in_composite_base": True,
            "general_population": "взрослые россияне",
            "general_population_reason": "всероссийская выборка 18+",
            "general_population_evidence": "всероссийский опрос 18+",
            "base_kind": "targetable_inferred",
        }
        components = [
            self.finding(1, "14%", scale_category="обычная практика", **common),
            self.finding(2, "36%", scale_category="несколько раз", **common),
        ]

        rows = calculate_derived_findings(build_composite_bases(components))

        self.assertEqual(len(rows), 3)
        composite = rows[2]
        self.assertEqual(composite["value"], "50%")
        self.assertEqual(composite["calculated_percent"], Decimal("50"))
        self.assertEqual(composite["calculation_formula"], "14% + 36% = 50%")
        self.assertEqual(composite["composite_component_indices"], [1, 2])
        self.assertTrue(composite["is_composite_base"])
        self.assertEqual(composite["finding_role"], "base")

        multiple = [
            self.finding(
                1,
                "21%",
                scale_category="автомобили",
                **{**common, "scale_choice_mode": "multiple_choice"},
            ),
            self.finding(
                2,
                "14%",
                scale_category="бытовая техника",
                **{**common, "scale_choice_mode": "multiple_choice"},
            ),
        ]
        self.assertEqual(len(build_composite_bases(multiple)), 2)

    def test_candidate_criteria_do_not_block_base_but_unlinked_share_is_conditional(self):
        candidate_root = self.finding(1, "38%", candidate_criteria=["income"])
        ambiguous_child = self.finding(
            2,
            "33%",
            None,
            "использовали сайты объявлений",
            indicator_type="conditional_share",
            base_kind="conditional",
            audience="покупателей",
            percent_base="покупателей",
            targeting_criteria=[],
        )
        rows = calculate_derived_findings([candidate_root, ambiguous_child])
        self.assertEqual(rows[0]["calculated_percent"], Decimal("38"))
        self.assertFalse(rows[0]["is_derived"])
        self.assertEqual(rows[0]["finding_role"], "base")
        self.assertEqual(rows[1]["value"], "33%")
        self.assertIsNone(rows[1]["calculation_formula"])
        self.assertEqual(rows[1]["finding_role"], "conditional")

    def test_classification_uses_fields_persisted_in_database(self):
        self.assertEqual(
            classify_finding(
                {
                    "is_derived": False,
                    "parent_finding_index": None,
                    "base_kind": "targetable_explicit",
                }
            ),
            "base",
        )
        self.assertEqual(
            classify_finding(
                {
                    "is_derived": True,
                    "parent_finding_index": 1,
                    "base_kind": "conditional",
                }
            ),
            "derived",
        )
        self.assertEqual(
            classify_finding(
                {
                    "is_derived": False,
                    "parent_finding_index": None,
                    "base_kind": "conditional",
                }
            ),
            "conditional",
        )

    def test_non_percentage_value_is_not_multiplied(self):
        rows = calculate_derived_findings(
            [self.finding(1, "38%"), self.finding(2, "2,3 млн", 1)]
        )
        self.assertEqual(rows[1]["value"], "2,3 млн")
        self.assertIsNone(rows[1]["calculated_percent"])


class SavingCalculatedFindingsTests(unittest.TestCase):
    class Connection:
        def __init__(self):
            self.queries = []

        def transaction(self):
            return nullcontext()

        def execute(self, query, params=None):
            self.queries.append((query, params))
            return Rows(rowcount=1)

    def test_save_includes_formula_chain_and_source_values(self):
        findings = calculate_derived_findings(
            [
                DerivedCalculationTests.finding(1, "38%"),
                DerivedCalculationTests.finding(2, "33%", 1, "использовали сайты объявлений"),
            ]
        )
        conn = self.Connection()
        save_result(conn, 10, findings)
        insert_sql, params = next(
            (query, params) for query, params in conn.queries if "INSERT INTO findings" in query
        )
        self.assertIn("calculation_formula", insert_sql)
        self.assertIn("calculation_chain", insert_sql)
        self.assertIn("source_value", insert_sql)
        self.assertEqual(insert_sql.count("%s"), len(params))
        self.assertIn("38%", str(conn.queries))
        self.assertIn("12,54%", str(conn.queries))


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
