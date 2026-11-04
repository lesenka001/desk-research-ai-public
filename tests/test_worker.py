import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from worker.main import (
    ModelResponseError,
    load_targeting_criteria,
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
        with TemporaryDirectory() as directory:
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
