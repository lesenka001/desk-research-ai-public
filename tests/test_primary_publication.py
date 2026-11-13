"""SPEC003 T2: preserve all findings and publish only justified model selections."""

from contextlib import nullcontext
from copy import deepcopy
import importlib.util
import os
from pathlib import Path
import re
import sqlite3
import unittest
from unittest.mock import patch

import worker.main as worker
from worker.policy import select_primary_findings
from bot.main import CURRENT_AUDIENCE_FINDING_SQL


ROOT = Path(__file__).resolve().parents[1]


def candidate(index=1, scope="base", **overrides):
    row = {
        "finding_index": index,
        "finding_key": f"finding_{index}",
        "block_index": index,
        "population_scope": scope,
        "is_primary": True,
        "primary_reason": "Ключевой результат исследования",
        "is_demographic_breakdown": False,
        "primary_rank": None,
        "summary": f"40% россиян выбирают сервис {index}.",
        "value": "40%",
    }
    row.update(overrides)
    return row


def model_finding(index=1, **overrides):
    row = candidate(index)
    row.update({
        "indicator_type": "population_share",
        "audience": "россияне",
        "percent_base": "россиян",
        "base_kind": "targetable_explicit",
        "targeting_criteria": ["geography"],
        "evidence": row["summary"],
    })
    row.update(overrides)
    return row


def validate(raw):
    text = " ".join(row["evidence"] for row in raw)
    return worker.validate_answer({"relevant": True, "findings": raw}, text)


def selected(rows):
    return [row["finding_index"] for row in rows if row["is_primary"] is True]


class PrimarySelectionTests(unittest.TestCase):
    def test_preserves_every_finding_in_source_order_as_a_copy(self):
        rows = [candidate(i, primary_rank=10-i) for i in range(1, 9)]
        original = deepcopy(rows)
        result = select_primary_findings(rows)
        self.assertEqual(rows, original)
        self.assertIsNot(result, rows)
        self.assertEqual([row["finding_index"] for row in result], list(range(1, 9)))
        self.assertTrue(all(new is not old for new, old in zip(result, rows)))
        self.assertEqual(selected(result), [4, 5, 6, 7, 8])
        for old, new in zip(rows, result):
            self.assertEqual(new["summary"], old["summary"])
            self.assertEqual(new["primary_reason"], old["primary_reason"])

    def test_base_has_priority_over_conditional_without_filling_remaining_places(self):
        rows = [candidate(1, "conditional", primary_rank=1),
                candidate(2, "base", primary_rank=9), candidate(3, "conditional")]
        self.assertEqual(selected(select_primary_findings(rows)), [2])

    def test_conditional_fallback_when_only_base_is_sample_composition(self):
        rows = [candidate(1, "unknown"),
                candidate(3, is_demographic_breakdown=True), candidate(4, "conditional")]
        self.assertEqual(selected(select_primary_findings(rows)), [4])

    def test_unselected_noncomposition_base_does_not_trigger_auto_fill(self):
        rows = [candidate(1, is_primary=False), candidate(2, "conditional")]
        self.assertEqual(selected(select_primary_findings(rows)), [])

    def test_unknown_is_never_published_or_promoted(self):
        self.assertEqual(selected(select_primary_findings([candidate(scope="unknown")])), [])

    def test_only_exact_true_and_nonempty_reason_are_eligible(self):
        for flag in (False, None, 1, "true", "false", [], {}):
            with self.subTest(flag=flag):
                self.assertEqual(selected(select_primary_findings([
                    candidate(is_primary=flag)])), [])
        for reason in (None, "", " \n\t "):
            with self.subTest(reason=reason):
                self.assertEqual(selected(select_primary_findings([
                    candidate(primary_reason=reason)])), [])

    def test_no_automatic_selection_when_model_selects_nothing(self):
        rows = [candidate(i, is_primary=False) for i in range(1, 12)]
        result = select_primary_findings(rows)
        self.assertEqual(len(result), 11)
        self.assertEqual(selected(result), [])
        self.assertTrue(all(type(row["is_primary"]) is bool for row in result))

    def test_demographic_composition_is_excluded_before_base_preference(self):
        rows = [candidate(1, is_demographic_breakdown=True),
                candidate(2, "conditional", is_demographic_breakdown=True),
                candidate(3, "conditional")]
        self.assertEqual(selected(select_primary_findings(rows)), [3])

    def test_behavioral_finding_for_age_cohort_can_be_primary(self):
        row = candidate(summary="40% людей 18–30 лет читают ежедневно.",
                        targeting_criteria=["age"])
        self.assertEqual(selected(select_primary_findings([row])), [1])

    def test_limit_is_global_across_blocks_and_cannot_exceed_five(self):
        rows = [candidate(i) for i in range(1, 13)]
        for limit, expected in ((1, 1), (3, 3), (5, 5), (30, 5), (0, 0)):
            with self.subTest(limit=limit):
                result = select_primary_findings(rows, limit=limit)
                self.assertEqual(len(result), 12)
                self.assertEqual(selected(result), list(range(1, expected + 1)))

    def test_model_rank_then_stable_order_controls_boundary(self):
        rows = [candidate(1, primary_rank=3), candidate(2, primary_rank=1),
                candidate(3, primary_rank=1), candidate(4, primary_rank=2)]
        self.assertEqual(selected(select_primary_findings(rows, limit=2)), [2, 3])

    def test_absent_rank_uses_stable_order(self):
        rows = [candidate(i) for i in (9, 3, 7, 1)]
        self.assertEqual(selected(select_primary_findings(rows, limit=2)), [9, 3])

    def test_empty_input(self):
        self.assertEqual(select_primary_findings([]), [])


class PrimaryValidationTests(unittest.TestCase):
    def test_banking_holders_depositors_and_borrowers_survive_human_validation(self):
        for base in ("держателей банковских карт", "вкладчиков", "заемщиков", "заёмщиков"):
            with self.subTest(base=base):
                quote = f"40% {base} выбирают сервис."
                rows = validate([model_finding(
                    percent_base=base, audience=base, evidence=quote, summary=quote,
                    indicator_type="conditional_share", targeting_criteria=["banking_product_use"])])
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["population_scope"], "base")
                self.assertEqual(rows[0]["indicator_type"], "conditional_share")
                self.assertIs(rows[0]["is_primary"], True)
                self.assertEqual(rows[0]["percent_base"], base)

    def test_thirty_saved_five_selected_for_whole_material(self):
        rows = validate([model_finding(i) for i in range(1, 32)])
        self.assertEqual(len(rows), 30)
        self.assertEqual(selected(rows), [1, 2, 3, 4, 5])
        self.assertEqual(rows[-1]["finding_index"], 30)

    def test_invalid_base_does_not_prevent_valid_conditional_fallback(self):
        invalid = model_finding(1, evidence="40% россиян выбирают сервис 1.",
                                value="99%")
        conditional = model_finding(
            2, indicator_type="conditional_share", audience="покупатели кофе",
            percent_base="покупателей кофе", base_kind="conditional",
            targeting_criteria=[], evidence="40% покупателей кофе выбирают сервис.",
            summary="40% покупателей кофе выбирают сервис.")
        rows = validate([invalid, conditional])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["population_scope"], "conditional")
        self.assertEqual(selected(rows), [2])

    def test_omitted_primary_metadata_keeps_finding_without_auto_selection(self):
        row = model_finding()
        for key in ("is_primary", "primary_reason", "is_demographic_breakdown", "primary_rank"):
            row.pop(key)
        rows = validate([row])
        self.assertEqual(len(rows), 1)
        self.assertIs(rows[0]["is_primary"], False)
        self.assertIsNone(rows[0]["primary_reason"])
        self.assertIsNone(rows[0]["primary_rank"])
        self.assertIs(type(rows[0]["is_demographic_breakdown"]), bool)

    def test_raw_truthy_flags_cannot_trigger_publication(self):
        for flag in (1, "true", "false"):
            with self.subTest(flag=flag):
                rows = validate([model_finding(is_primary=flag)])
                self.assertEqual(len(rows), 1)
                self.assertIs(rows[0]["is_primary"], False)

    def test_nonstring_primary_reason_is_not_coerced_into_publication(self):
        for reason in (123, True, ["Главный вывод"], {"reason": "Главный вывод"}):
            with self.subTest(reason=reason):
                rows = validate([model_finding(primary_reason=reason)])
                self.assertEqual(len(rows), 1)
                self.assertIsNone(rows[0]["primary_reason"])
                self.assertIs(rows[0]["is_primary"], False)

    def test_primary_requires_explicit_boolean_demographic_classification(self):
        invalid_rows = [model_finding(is_demographic_breakdown=value)
                        for value in (None, 0, 1, "false", "true", [], {})]
        omitted = model_finding()
        omitted.pop("is_demographic_breakdown")
        for raw in [*invalid_rows, omitted]:
            with self.subTest(raw_flag=raw.get("is_demographic_breakdown", "missing")):
                rows = validate([raw])
                self.assertEqual(len(rows), 1)
                self.assertIs(rows[0]["is_primary"], False)
        rows = validate([model_finding(is_demographic_breakdown=False)])
        self.assertIs(rows[0]["is_primary"], True)

    def test_rank_is_integer_or_null_never_bool_or_numeric_string(self):
        # Inspect validation before selection assigns final publication ranks.
        with patch("worker.main.select_primary_findings", side_effect=lambda rows: rows):
            for rank in (True, False, "1", 1.5):
                with self.subTest(rank=rank):
                    rows = validate([model_finding(primary_rank=rank)])
                    self.assertIsNone(rows[0]["primary_rank"])
            rows = validate([model_finding(primary_rank=2)])
            self.assertEqual(rows[0]["primary_rank"], 2)
            self.assertIs(type(rows[0]["primary_rank"]), int)

    def test_demographic_rows_retained_with_metadata_but_not_selected(self):
        rows = validate([model_finding(is_demographic_breakdown=True, primary_rank=1)])
        self.assertEqual(len(rows), 1)
        self.assertIs(rows[0]["is_demographic_breakdown"], True)
        self.assertEqual(rows[0]["primary_reason"], "Ключевой результат исследования")
        self.assertEqual(selected(rows), [])

    def test_environment_cannot_raise_extraction_hard_cap(self):
        # Load a separate module so this check never mutates the shared worker.
        with patch.dict(os.environ, {"MAX_FINDINGS_PER_MATERIAL": "100"}):
            spec = importlib.util.spec_from_file_location("_worker_t2_config", worker.__file__)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        self.assertEqual(module.MAX_FINDINGS_PER_MATERIAL, 30)
        raw = [model_finding(i) for i in range(1, 36)]
        text = " ".join(row["evidence"] for row in raw)
        self.assertEqual(len(module.validate_answer({"relevant": True, "findings": raw}, text)), 30)

    def test_schema_requires_typed_primary_metadata(self):
        fields = worker.FINDING_JSON_SCHEMA["properties"]
        for name, expected in (("is_primary", {"boolean"}),
                               ("primary_reason", {"string", "null"}),
                               ("is_demographic_breakdown", {"boolean"}),
                               ("primary_rank", {"integer", "null"})):
            with self.subTest(name=name):
                self.assertIn(name, worker.FINDING_JSON_SCHEMA["required"])
                value = fields[name]["type"]
                self.assertEqual(set(value if isinstance(value, list) else [value]), expected)


class RecordingConnection:
    def __init__(self):
        self.queries = []

    def transaction(self):
        return nullcontext()

    def execute(self, query, params=None):
        self.queries.append((query, params))


class PrimaryPersistenceTests(unittest.TestCase):
    def test_save_entry_caps_unvalidated_callers_at_thirty_and_five(self):
        template = worker.prepare_source_findings(validate([model_finding()]))[0]
        rows = [dict(template, finding_index=index, finding_key=f"row_{index}",
                     block_index=index, is_primary=True, primary_rank=index)
                for index in range(1, 36)]
        original = deepcopy(rows)
        conn = RecordingConnection()
        worker.save_result(conn, 123, rows)
        inserts = [(sql, params) for sql, params in conn.queries if "INSERT INTO findings" in sql]
        self.assertEqual(len(inserts), 30)
        saved = []
        for sql, params in inserts:
            columns = re.search(r"INSERT INTO findings\s*\((.*?)\)\s*VALUES", sql, re.S).group(1)
            saved.append(dict(zip([name.strip() for name in columns.split(",")], params)))
        self.assertEqual([row["finding_index"] for row in saved], list(range(1, 31)))
        self.assertEqual(sum(row["is_primary"] is True for row in saved), 5)
        self.assertEqual(rows, original)

    def test_failed_insert_cannot_switch_current_successful_version(self):
        class FailingConnection(RecordingConnection):
            def execute(self, query, params=None):
                super().execute(query, params)
                if "INSERT INTO findings" in query:
                    raise RuntimeError("storage failed")

        conn = FailingConnection()
        rows = worker.prepare_source_findings(validate([model_finding()]))
        with self.assertRaisesRegex(RuntimeError, "storage failed"):
            worker.save_result(conn, 123, rows)
        self.assertFalse(any("UPDATE materials" in sql for sql, _ in conn.queries))
        self.assertFalse(any("DELETE" in sql.upper() for sql, _ in conn.queries))

    def test_all_thirty_rows_and_audit_fields_are_persisted(self):
        rows = worker.prepare_source_findings(validate([model_finding(i) for i in range(1, 31)]))
        conn = RecordingConnection()
        self.assertEqual(worker.save_result(conn, 123, rows), "done")
        inserts = [(sql, params) for sql, params in conn.queries if "INSERT INTO findings" in sql]
        self.assertEqual(len(inserts), 30)
        for (sql, params), row in zip(inserts, rows):
            columns = re.search(r"INSERT INTO findings\s*\((.*?)\)\s*VALUES", sql, re.S).group(1)
            names = [name.strip() for name in columns.split(",")]
            self.assertEqual(len(names), len(params))
            saved = dict(zip(names, params))
            self.assertEqual(saved["material_id"], 123)
            for key in ("population_scope", "is_primary", "primary_reason",
                        "is_demographic_breakdown", "primary_rank"):
                self.assertEqual(saved[key], row[key])
        self.assertEqual(sum(row["is_primary"] for row in rows), 5)
        sql = " ".join(query for query, _ in conn.queries).upper()
        self.assertNotIn("DELETE FROM FINDINGS", sql)
        self.assertNotIn("UPDATE FINDINGS", sql)
        updates = [(q, p) for q, p in conn.queries if "UPDATE materials" in q]
        self.assertEqual(len(updates), 1)
        self.assertIn(worker.PROMPT_VERSION, updates[0][1])

    def test_new_columns_are_nullable_additions_for_legacy_rows(self):
        schema = (ROOT / "db/schema.sql").read_text(encoding="utf-8")
        for name, sql_type in (("population_scope", "TEXT"), ("is_primary", "BOOLEAN"),
                               ("primary_reason", "TEXT"), ("is_demographic_breakdown", "BOOLEAN"),
                               ("primary_rank", "INTEGER")):
            with self.subTest(name=name):
                match = re.search(rf"ADD COLUMN IF NOT EXISTS {name}\s+{sql_type}([^;]*);", schema, re.I)
                self.assertIsNotNone(match)
                self.assertNotIn("NOT NULL", match.group(1).upper())


class PublicPredicateTests(unittest.TestCase):
    """Evaluate the shared predicate on small fixtures, without a server.

    SQLite supplies the correlated SELECT/boolean semantics here; only the
    PostgreSQL regex operator and POSIX whitespace spelling need adapting.
    The real PostgreSQL placeholder handling remains covered in test_views.
    """

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.create_function("btrim", 1, lambda value: value.strip() if value else value)
        self.conn.create_function("chr", 1, chr)
        self.conn.create_function(
            "regexp", 2,
            lambda pattern, value: bool(re.search(pattern.replace("[[:space:]]", r"\s"), value or "")),
        )
        self.conn.execute("CREATE TABLE materials (id INTEGER, processed_prompt_version TEXT)")
        self.conn.execute("""CREATE TABLE findings (
            id INTEGER, material_id INTEGER, prompt_version TEXT, finding_index INTEGER,
            created_at TEXT, block_index INTEGER, block_position INTEGER,
            indicator_type TEXT, value TEXT, audience TEXT, percent_base TEXT,
            population_scope TEXT, is_primary BOOLEAN, primary_reason TEXT,
            is_demographic_breakdown BOOLEAN, primary_rank INTEGER)""")

    def add(self, identity, material=1, version="v7", position=None, **overrides):
        row = dict(id=identity, material_id=material, prompt_version=version,
                   finding_index=position or identity, created_at="2026-11-13",
                   block_index=identity, block_position=1,
                   indicator_type="population_share", value="40%", audience="россияне",
                   percent_base="россиян", population_scope="base", is_primary=True,
                   primary_reason="Ключевой результат", is_demographic_breakdown=False,
                   primary_rank=None)
        row.update(overrides)
        self.conn.execute(
            "INSERT INTO findings (" + ",".join(row) + ") VALUES (" +
            ",".join("?" for _ in row) + ")", tuple(row.values()))

    def public_ids(self):
        predicate = re.sub(r"\s~\s", " REGEXP ", CURRENT_AUDIENCE_FINDING_SQL)
        return [row[0] for row in self.conn.execute(
            "SELECT f.id FROM findings f JOIN materials m ON m.id=f.material_id WHERE "
            + predicate + " ORDER BY f.id")]

    def test_new_material_publishes_five_across_all_blocks(self):
        self.conn.execute("INSERT INTO materials VALUES (1, 'v7')")
        for index in range(1, 9):
            self.add(index)
        self.assertEqual(self.public_ids(), [1, 2, 3, 4, 5])

    def test_legacy_unmarked_rows_use_first_five(self):
        self.conn.execute("INSERT INTO materials VALUES (1, 'v5')")
        for index in range(1, 9):
            self.add(index, version="v5", is_primary=None, primary_reason=None,
                     population_scope=None, is_demographic_breakdown=None)
        self.assertEqual(self.public_ids(), [1, 2, 3, 4, 5])

    def test_rejected_rows_do_not_consume_the_five_eligible_places(self):
        self.conn.execute("INSERT INTO materials VALUES (1, 'v7')")
        self.add(1, is_primary=False)
        self.add(2, is_demographic_breakdown=True)
        self.add(3, indicator_type="market_metric")
        self.add(4, value="нет процента")
        self.add(5, audience=None, percent_base=None)
        for index in range(6, 13):
            self.add(index)
        self.assertEqual(self.public_ids(), [6, 7, 8, 9, 10])

    def test_cap_is_correlated_by_both_material_and_version(self):
        self.conn.executemany("INSERT INTO materials VALUES (?, 'v7')", [(1,), (2,)])
        for index in range(1, 9):
            self.add(index, version="v5")
            self.add(index + 10, version="v7", position=index)
            self.add(index + 20, material=2, version="v7", position=index)
        self.assertEqual(self.public_ids(), [11, 12, 13, 14, 15, 21, 22, 23, 24, 25])

    def test_legacy_missing_pointer_uses_only_latest_version(self):
        self.conn.execute("INSERT INTO materials VALUES (1, NULL)")
        for index in range(1, 8):
            self.add(index, version="v5", created_at="2026-11-12", is_primary=None)
            self.add(index + 10, version="v6", position=index,
                     is_primary=None, created_at="2026-11-13")
        self.assertEqual(self.public_ids(), [11, 12, 13, 14, 15])

    def test_new_unknown_scope_or_missing_reason_never_public(self):
        self.conn.execute("INSERT INTO materials VALUES (1, 'v7')")
        self.add(1, population_scope="unknown")
        self.add(2, primary_reason=None)
        self.add(3, primary_reason="   ")
        self.add(4, population_scope="conditional")
        self.assertEqual(self.public_ids(), [4])

    def test_sql_uses_primary_rank_then_source_position(self):
        self.conn.execute("INSERT INTO materials VALUES (1, 'v7')")
        for index in range(1, 9):
            self.add(index, primary_rank=9-index)
        self.assertEqual(self.public_ids(), [4, 5, 6, 7, 8])


if __name__ == "__main__":
    unittest.main()
