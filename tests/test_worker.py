import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from worker.main import (
    ModelResponseError,
    load_targeting_criteria,
    model_request_body,
    partition_targeting_criteria,
    process_new,
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

    def test_unverifiable_percent_base_is_cleared_and_direct_is_downgraded(self):
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
        rows = validate_answer(answer, TEXT)
        self.assertIsNone(rows[0]["percent_base"])
        self.assertEqual(rows[0]["ir_applicability"], "indirect")

    def test_market_metric_is_context_only(self):
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
        rows = validate_answer(answer, "Рынок достиг 12 млн заказов за месяц.")
        self.assertEqual(rows[0]["ir_applicability"], "context_only")

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

    def test_invalid_type_becomes_other(self):
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
        rows = validate_answer(answer, TEXT)
        self.assertEqual(rows[0]["indicator_type"], "other")

    def test_irrelevant_material_has_no_findings(self):
        self.assertEqual(validate_answer({"relevant": False, "findings": []}, TEXT), [])


class TargetingCriteriaTests(unittest.TestCase):
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
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "criteria.json"
            path.write_text(
                '{"version":1,"criteria":['
                '{"id":"age","label":"возраст","synonyms":[]},'
                '{"id":"age","label":"возраст","synonyms":[]}]}' ,
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "повторяющийся"):
                load_targeting_criteria(path)

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
                    "value": "40%",
                    "evidence": "40% покупали товары.",
                    "summary": "40% покупали товары.",
                },
                {
                    "finding_key": "child",
                    "block_index": 2,
                    "parent_key": "base",
                    "value": "20%",
                    "evidence": "20% выбирали сайты объявлений.",
                    "summary": "20% выбирали сайты объявлений.",
                },
            ],
        }
        rows = validate_answer(
            answer, "40% покупали товары. 20% выбирали сайты объявлений."
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
