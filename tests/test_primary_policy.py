"""SPEC003 T1: source-backed population scope, independent of primary selection.

Inputs use worker finding fields. percent_base and methodology_evidence are
source quotes; audience/summary/base_reason are model claims, not evidence.
Negative ambiguous cases require only 'not base', leaving conditional versus
unknown to the policy when the source cannot resolve the denominator.
"""

import unittest
from copy import deepcopy

from worker.main import FINDING_JSON_SCHEMA, load_targeting_criteria, validate_answer
from worker.policy import population_scope


def finding(base="респондентов", **overrides):
    quote = f"40% {base} выбирают сервис."
    row = {
        "indicator_type": "population_share",
        "percent_base": base,
        "audience": base,
        "base_kind": "targetable_explicit",
        "base_reason": None,
        "methodology_evidence": None,
        "targeting_criteria": [],
        "candidate_criteria": [],
        "value": "40%",
        "evidence": quote,
        "summary": quote,
    }
    row.update(overrides)
    return row


class PopulationScopeTests(unittest.TestCase):
    def test_age_and_gender_are_targetable_independently_of_legacy_indicator(self):
        for base, criteria in (("женщин", ["gender"]),
                               ("мужчин", ["gender"]),
                               ("людей в возрасте от 18 до 30 лет", ["age"]),
                               ("женщин 18–30 лет из Москвы", ["age", "gender", "geography"])):
            for indicator in ("population_share", "conditional_share"):
                with self.subTest(base=base, indicator=indicator):
                    row = finding(base, targeting_criteria=criteria,
                                  indicator_type=indicator)
                    self.assertEqual(population_scope(row, row["evidence"]), "base")

    def test_classification_does_not_mutate_finding(self):
        row = finding("пользователей банковских карт",
                      targeting_criteria=["banking_product_use"])
        original = deepcopy(row)
        population_scope(row, row["evidence"])
        self.assertEqual(row, original)

    def test_unrestricted_source_backed_people_base_needs_no_criteria(self):
        for base in ("респондентов", "россиян", "опрошенных"):
            with self.subTest(base=base):
                row = finding(base)
                self.assertEqual(population_scope(row, row["evidence"]), "base")

    def test_approved_targetable_groups_remain_base_for_conditional_share(self):
        cases = (
            ("пользователей банковских карт", ["banking_product_use"]),
            ("клиентов банка", ["banking_product_use"]),
            ("заёмщиков", ["banking_product_use"]),
            ("руководителей", ["job_title"]),
            ("сотрудников отдела закупок", ["department"]),
            ("родителей детей от 3 до 7 лет", ["children", "children_age"]),
            ("руководителей малого бизнеса", ["job_title", "business_size"]),
            ("работников строительной отрасли", ["company_industry"]),
        )
        for base, criteria in cases:
            with self.subTest(base=base):
                row = finding(base, indicator_type="conditional_share",
                              base_kind="conditional", targeting_criteria=criteria)
                self.assertEqual(population_scope(row, row["evidence"]), "base")

    def test_approved_geographic_detail_is_targetable(self):
        for base in ("жителей России", "жителей Москвы",
                     "жителей городов с населением свыше миллиона человек",
                     "жителей Московской области",
                     "жителей Центрального федерального округа"):
            with self.subTest(base=base):
                row = finding(base, targeting_criteria=["geography"])
                self.assertEqual(population_scope(row, row["evidence"]), "base")

    def test_source_methodology_can_establish_targetable_sample(self):
        methodology = "Опрос проведён среди жителей России в возрасте от 18 лет."
        row = finding(base_kind="targetable_inferred",
                      base_reason="База определена методологией опроса",
                      methodology_evidence=methodology,
                      targeting_criteria=["geography", "age"])
        self.assertEqual(population_scope(row, methodology + " " + row["evidence"]),
                         "base")

    def test_behavioral_users_are_conditional_even_if_model_claims_population(self):
        for base in ("пользователей сайтов объявлений", "покупателей кофе",
                     "россиян, использующих ИИ в работе"):
            with self.subTest(base=base):
                row = finding(base, base_kind="conditional")
                self.assertEqual(population_scope(row, row["evidence"]), "conditional")

    def test_mixed_known_and_unknown_criteria_cannot_be_base(self):
        for extra in ({"targeting_criteria": ["geography", "ai_use"]},
                      {"targeting_criteria": ["geography"],
                       "candidate_criteria": ["ai_use"]}):
            with self.subTest(criteria=extra):
                row = finding("жителей Москвы, использующих ИИ в работе", **extra)
                self.assertNotEqual(population_scope(row, row["evidence"]), "base")

    def test_unlabelled_extra_source_restriction_cannot_be_ignored(self):
        cases = (
            ("жителей Москвы, регулярно читающих книги", ["geography"]),
            ("женщин, довольных своим доходом", ["gender"]),
            ("руководителей, планирующих сменить работу", ["job_title"]),
            ("пользователей банковских карт, покупающих кофе ежедневно",
             ["banking_product_use"]),
        )
        for base, criteria in cases:
            with self.subTest(base=base):
                row = finding(base, targeting_criteria=criteria)
                self.assertNotEqual(population_scope(row, row["evidence"]), "base")

    def test_methodology_restricts_generic_respondent_denominator(self):
        methodology = "Опрос проведён среди россиян, использующих ИИ в работе."
        row = finding(base_kind="targetable_inferred",
                      base_reason="База задана выборкой",
                      methodology_evidence=methodology,
                      targeting_criteria=["geography"])
        self.assertNotEqual(population_scope(row, methodology + " " + row["evidence"]),
                            "base")

    def test_generic_base_cannot_hide_a_candidate_criterion(self):
        row = finding(candidate_criteria=["brand_loyalty"])
        self.assertNotEqual(population_scope(row, row["evidence"]), "base")

    def test_missing_denominator_is_unknown_despite_targetable_model_claims(self):
        row = finding(None, audience="россияне 18–30 лет",
                      targeting_criteria=["age", "geography"],
                      evidence="40% выбирают сервис.",
                      summary="40% россиян 18–30 лет выбирают сервис.")
        self.assertEqual(population_scope(row, row["evidence"]), "unknown")

    def test_unverified_denominator_is_not_base(self):
        row = finding("жителей России в возрасте от 18 лет",
                      targeting_criteria=["geography", "age"])
        self.assertNotEqual(population_scope(row, "40% выбирают сервис."), "base")

    def test_unknown_base_without_support_is_unknown(self):
        row = finding(None, base_kind="unknown", evidence="40% выбирают сервис.")
        self.assertEqual(population_scope(row, row["evidence"]), "unknown")

    def test_youth_with_source_age_bounds_in_denominator_is_base(self):
        for base in ("молодёжи в возрасте от 18 до 30 лет",
                     "молодёжи (18–30 лет)", "молодежи 18-30 лет"):
            with self.subTest(base=base):
                row = finding(base, targeting_criteria=["age"])
                self.assertEqual(population_scope(row, row["evidence"]), "base")

    def test_youth_with_source_methodology_bounds_is_base(self):
        methodology = "В исследовании молодёжь — это люди в возрасте от 18 до 30 лет."
        row = finding("молодёжи", base_kind="targetable_inferred",
                      base_reason="Возраст молодёжи определён в методологии",
                      methodology_evidence=methodology, targeting_criteria=["age"])
        self.assertEqual(population_scope(row, methodology + " " + row["evidence"]),
                         "base")

    def test_bare_youth_and_model_age_claims_are_not_numeric_source_support(self):
        for base in ("молодёжи", "молодежи"):
            with self.subTest(base=base):
                row = finding(base, audience="молодёжь 18–30 лет",
                              summary="40% молодёжи 18–30 лет выбирают сервис.",
                              base_reason="Молодёжь означает возраст от 18 до 30 лет",
                              targeting_criteria=["age"])
                self.assertNotEqual(population_scope(row, row["evidence"]), "base")

    def test_whole_survey_adult_age_does_not_define_youth_cohort(self):
        methodology = "Опрос проведён среди жителей России в возрасте от 18 лет."
        row = finding("молодёжи", base_kind="targetable_inferred",
                      base_reason="Модель считает молодёжью людей 18–30 лет",
                      methodology_evidence=methodology, targeting_criteria=["age"])
        self.assertNotEqual(population_scope(row, methodology + " " + row["evidence"]),
                            "base")

    def test_fabricated_numeric_methodology_cannot_define_youth(self):
        row = finding("молодёжи", base_kind="targetable_inferred",
                      base_reason="Возраст указан в методологии",
                      methodology_evidence="Молодёжь — люди от 18 до 30 лет.",
                      targeting_criteria=["age"])
        self.assertNotEqual(population_scope(row, row["evidence"]), "base")

    def test_age_numbers_elsewhere_do_not_establish_youth_bounds(self):
        row = finding("молодёжи", targeting_criteria=["age"])
        text = row["evidence"] + " Среди людей 18–30 лет 25% читают каждый день."
        self.assertNotEqual(population_scope(row, text), "base")

    def test_unrelated_numeric_methodology_does_not_define_youth(self):
        methodology = "В опросе участвовали люди в возрасте от 18 до 65 лет."
        row = finding("молодёжи", base_kind="targetable_inferred",
                      base_reason="Модель переносит возраст всей выборки на молодёжь",
                      methodology_evidence=methodology, targeting_criteria=["age"])
        self.assertNotEqual(population_scope(row, methodology + " " + row["evidence"]),
                            "base")

    def test_explicit_approved_ids_control_new_criterion(self):
        row = finding("людей с подпиской на сервис", targeting_criteria=["service_subscription"])
        self.assertNotEqual(population_scope(row, row["evidence"], set()), "base")
        self.assertEqual(population_scope(row, row["evidence"], {"service_subscription"}),
                         "base")


class PopulationScopeIntegrationTests(unittest.TestCase):
    def test_validator_recomputes_model_scope_from_source(self):
        cases = (
            ("пользователей банковских карт", ["banking_product_use"],
             "conditional_share", "conditional", "base", "targetable_explicit"),
            ("россиян, использующих ИИ в работе", ["geography"],
             "population_share", "base", "conditional", "conditional"),
            ("молодёжи", ["age"], "population_share", "base", "unknown", "unknown"),
        )
        for base, criteria, indicator, model_scope, scope, base_kind in cases:
            with self.subTest(base=base):
                raw = finding(base, targeting_criteria=criteria,
                              indicator_type=indicator, population_scope=model_scope)
                rows = validate_answer({"relevant": True, "findings": [raw]},
                                       raw["evidence"])
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["population_scope"], scope)
                self.assertEqual(rows[0]["base_kind"], base_kind)
                self.assertEqual(rows[0]["evidence"], raw["evidence"])
                self.assertEqual(rows[0]["value"], "40%")

    def test_validator_keeps_unknown_registry_extension_as_candidate(self):
        raw = finding("жителей Москвы с подпиской на сервис",
                      targeting_criteria=["geography", "service_subscription"],
                      population_scope="base")
        rows = validate_answer({"relevant": True, "findings": [raw]}, raw["evidence"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["targeting_criteria"], ["geography"])
        self.assertEqual(rows[0]["candidate_criteria"], ["service_subscription"])
        self.assertNotEqual(rows[0]["population_scope"], "base")

    def test_response_schema_requires_independent_scope(self):
        self.assertIn("population_scope", FINDING_JSON_SCHEMA["required"])
        self.assertEqual(
            set(FINDING_JSON_SCHEMA["properties"]["population_scope"]["enum"]),
            {"base", "conditional", "unknown"},
        )


class ApprovedTaxonomyTests(unittest.TestCase):
    def test_canonical_taxonomy_includes_owner_approved_new_criteria(self):
        criteria = load_targeting_criteria()
        for criterion in ("geography", "age", "gender", "children", "children_age", "banking_product_use", "job_title",
                          "department", "business_size", "company_industry"):
            with self.subTest(criterion=criterion):
                self.assertIn(criterion, criteria)

    def test_geography_registry_covers_approved_levels(self):
        geography = load_targeting_criteria()["geography"]
        vocabulary = " ".join([geography["label"], *geography["synonyms"]]).lower()
        for detail in ("стран", "город", "регион", "населен", "размер", "федеральн"):
            with self.subTest(detail=detail):
                self.assertIn(detail, vocabulary.replace("ё", "е"))


if __name__ == "__main__":
    unittest.main()
