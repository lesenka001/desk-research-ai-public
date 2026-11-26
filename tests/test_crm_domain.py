import unittest
from copy import deepcopy
from datetime import datetime
from io import BytesIO

from openpyxl import Workbook

from crm.domain import (HEADERS, IntakeError, audience_key, bounds, decide, empty_state,
                        identity, import_rows, project, read_xlsx, rounded, summaries)


def row(pn=1, aorg='100', audience='Мужчины 18-45 лет', ir=20, n=100, end='2026-11-01'):
    return project([f'PN{pn}', aorg, audience, ir, n, end], pn + 1)


def workbook(rows, headers=HEADERS, percent=False):
    w = Workbook()
    s = w.active
    s.append(list(headers))
    for r in rows:
        s.append(r)
    if percent:
        s['D2'].number_format = '0.00%'
    buf = BytesIO()
    w.save(buf)
    return buf.getvalue()


class ImportTests(unittest.TestCase):
    def test_original_xlsx_numeric_and_excel_percent(self):
        values = ['PN1', '100', 'Мужчины', 7.81, 100, datetime(2026, 11, 1)]
        self.assertEqual(read_xlsx(workbook([values]))[0]['data']['ir'], 7.81)
        values[3] = .0781
        self.assertAlmostEqual(read_xlsx(workbook([values], percent=True))[0]['data']['ir'], 7.81)

    def test_structure_and_formulas(self):
        with self.assertRaises(IntakeError):
            read_xlsx(workbook([['PN1']], headers=['PN']))
        values = ['PN1', '100', 'Мужчины', '=10+20', 100, datetime(2026, 11, 1)]
        self.assertIn('formula', read_xlsx(workbook([values]))[0]['reasons'])
        with self.assertRaises(IntakeError): read_xlsx(b'not excel')

    def test_rejects_zero_missing_and_bad_population(self):
        bad = [row(ir=0), row(ir=None), row(n=0), row(n=None), row(audience=''),
               row(aorg='PN1'), row(ir=101), row(n=1.5), row(n=-1), row(ir=float('nan'))]
        state, report = import_rows(empty_state(), bad)
        self.assertEqual(report['excluded'], len(bad))
        self.assertFalse(state['projects'])

    def test_recontact_removes_previously_accepted_and_preserves_audit(self):
        state, _ = import_rows(empty_state(), [row()])
        state, report = import_rows(state, [row(audience='Recontact: реконтакт')])
        self.assertFalse(summaries(state))
        self.assertEqual(report['rows'][0]['status'], 'excluded')
        self.assertIn('recontact', report['rows'][0]['reasons'])

    def test_add_replace_repeat_and_partial_file(self):
        state, _ = import_rows(empty_state(), [row(), row(2)])
        original = deepcopy(state)
        state, report = import_rows(state, [row(ir=50)])
        self.assertEqual(report['updated'], 1)
        self.assertEqual(len(state['projects']), 2)
        self.assertEqual(original['projects']['PN1']['data']['ir'], 20)
        state, report = import_rows(state, [row(ir=50)])
        self.assertEqual(report['unchanged'], 1)

    def test_conflicting_same_file_pn_is_not_chosen_arbitrarily(self):
        state, report = import_rows(empty_state(), [row(ir=20), row(ir=30)])
        self.assertEqual(report['excluded'], 2)
        self.assertFalse(state['projects'])
        self.assertTrue(all('duplicate' in r['reasons'] for r in report['rows']))
        state, report = import_rows(empty_state(), [row(), row()])
        self.assertEqual((report['added'], report['unchanged']), (1, 1))

    def test_permanent_invalid_update_deactivates_old_pn_and_valid_import_restores_it(self):
        for bad in (row(ir=0), row(n=None), row(audience=''), row(aorg='PN1')):
            with self.subTest(bad=bad):
                state, _ = import_rows(empty_state(), [row()])
                state, _ = import_rows(state, [bad])
                self.assertFalse(summaries(state))
                state, _ = import_rows(state, [row()])
                self.assertEqual(summaries(state)[0]['mean'], 20)

    def test_conflicting_duplicates_do_not_deactivate_old_accepted_pn(self):
        state, _ = import_rows(empty_state(), [row()])
        state, _ = import_rows(state, [row(ir=30), row(ir=40)])
        self.assertEqual(summaries(state)[0]['mean'], 20)


class AudienceTests(unittest.TestCase):
    def test_punctuation_numeric_ranges_and_meaning(self):
        pairs = [('Мужчины, 18–45 лет.', 'мужчины 18-45 лет', True),
                 ('18-45 лет', '1845 лет', False), ('18-45 лет', '18 45 лет', False),
                 ('100+', '100', False), ('1,5 года', '15 года', False),
                 ('1,5 года', '1.5 года', True),
                 ('мужчины; женщины...', 'мужчины женщины', True),
                 ('16-55', '18-55', False), ('не курят', 'курят', False),
                 ('более 30%', 'более 30', False)]
        for a, b, same in pairs:
            with self.subTest(a=a, b=b):
                self.assertEqual(audience_key(a) == audience_key(b), same)

    def test_same_text_does_not_require_review_and_uses_date_and_numeric_pn(self):
        state, _ = import_rows(empty_state(), [row(9), row(10, audience='мужчины, 18–45 лет.')])
        self.assertTrue(all(not r['pending'] for r in state['projects'].values()))
        s = summaries(state)[0]
        self.assertEqual((s['count'], s['latest']), (2, 'PN10'))
        state, _ = import_rows(state, [row(2, end='2026-11-02')])
        self.assertEqual(summaries(state)[0]['latest'], 'PN2')

    def test_new_audience_keep_separate_or_merge_and_repeat(self):
        state, _ = import_rows(empty_state(), [row(), row(2, audience='Женщины', ir=40)])
        record = state['projects']['PN2']
        self.assertEqual(record['pending'], ['audience'])
        separate = decide(state, 'PN2', 'different', identity(record['data']))
        self.assertEqual(len(summaries(separate)), 2)
        merged = decide(state, 'PN2', 'same', identity(record['data']), target=state['projects']['PN1']['group'])
        self.assertEqual((len(summaries(merged)), summaries(merged)[0]['mean']), (1, 30))
        merged, _ = import_rows(merged, [row(3, audience='Женщины', ir=30)])
        self.assertFalse(merged['projects']['PN3']['pending'])

    def test_changed_audience_uses_previous_accepted_until_decision(self):
        state, _ = import_rows(empty_state(), [row()])
        state, _ = import_rows(state, [row(audience='Женщины', ir=40)])
        self.assertEqual(summaries(state)[0]['mean'], 20)
        record = state['projects']['PN1']
        state = decide(state, 'PN1', 'different', identity(record['data']))
        self.assertEqual(summaries(state)[0]['mean'], 40)

    def test_stale_or_cross_tracker_decision_fails(self):
        state, _ = import_rows(empty_state(), [row(), row(2, audience='Женщины')])
        with self.assertRaises(IntakeError): decide(state, 'PN2', 'different', 'stale')
        record = state['projects']['PN2']
        with self.assertRaises(IntakeError): decide(state, 'PN2', 'same', identity(record['data']), 'unknown')


class TrackerTests(unittest.TestCase):
    def test_iqr_two_waves_and_zero_iqr(self):
        self.assertEqual(bounds([10, 90]), (-30, 130))
        self.assertEqual(bounds([10, 10]), (10, 10))
        state, _ = import_rows(empty_state(), [row(i, ir=10) for i in range(1, 5)] + [row(5, ir=90)])
        self.assertEqual(state['projects']['PN5']['pending'], ['outlier'])
        self.assertEqual(summaries(state)[0]['count'], 4)

    def test_outlier_decision_and_repeat_are_persistent(self):
        rows = [row(i, ir=10) for i in range(1, 5)] + [row(5, ir=90)]
        state, _ = import_rows(empty_state(), rows)
        fingerprint = identity(state['projects']['PN5']['data'])
        state = decide(state, 'PN5', 'exclude', fingerprint)
        state, report = import_rows(state, rows)
        self.assertFalse(state['projects']['PN5']['pending'])
        self.assertTrue(state['projects']['PN5']['outlier'])
        self.assertEqual(summaries(state)[0]['count'], 4)
        state, _ = import_rows(state, [row(5, ir=10)])
        self.assertEqual(summaries(state)[0]['count'], 5)

    def test_keep_outlier_then_n_update_does_not_repeat_question(self):
        state, _ = import_rows(empty_state(), [row(i, ir=10) for i in range(1, 5)] + [row(5, ir=90)])
        state = decide(state, 'PN5', 'keep', identity(state['projects']['PN5']['data']))
        state, _ = import_rows(state, [row(5, ir=90, n=200)])
        self.assertFalse(state['projects']['PN5']['pending'])
        self.assertEqual(summaries(state)[0]['mean'], 26)

    def test_rounding_half_up_only_for_display(self):
        self.assertEqual([rounded(x) for x in (7.81, 2.5, 10.49)], [8, 3, 10])
        state, _ = import_rows(empty_state(), [row(ir=7.81), row(2, ir=8.91)])
        self.assertAlmostEqual(summaries(state)[0]['mean'], 8.36)



class ProjectMetadataTests(unittest.TestCase):
    def test_type_counts_rejected_pending_and_deleted_waves(self):
        from crm.domain import project_info, upgrade_state
        state, _ = import_rows(empty_state(), [row(), row(2, audience='Женщины'), row(3, ir=0)])
        self.assertEqual(project_info(state, '100'), dict(total=3, project_type='трекер'))
        self.assertEqual(summaries(state)[0]['count'], 1)
        state['projects'].pop('PN2')
        state, _ = import_rows(state, [row()])
        self.assertEqual(project_info(state, '100')['total'], 3)
        self.assertEqual(upgrade_state(state), state)

    def test_history_upgrade_numbers_and_names_do_not_change_decisions(self):
        from crm.domain import upgrade_state, group_catalog, rename_group
        state, report = import_rows(empty_state(), [row(), row(2, ir=0)])
        state = {k: state[k] for k in ('projects', 'aliases')}
        original = deepcopy(state)
        state = upgrade_state(state, [report])
        self.assertEqual(state['projects'], original['projects'])
        self.assertEqual(state['aliases'], original['aliases'])
        g = group_catalog(state)[0]
        state = rename_group(state, '100', g['group'], '  Покупатели  ', identity(g['meta']))
        state, _ = import_rows(state, [row(3, audience='Другая ЦА')])
        state = decide(state, 'PN3', 'different', identity(state['projects']['PN3']['data']))
        groups = group_catalog(state)
        self.assertEqual([g['label'] for g in groups], ['ЦА 1 — Покупатели', 'ЦА 2'])
        self.assertEqual(summaries(state)[0]['mean'], 20)
        with self.assertRaises(IntakeError):
            rename_group(state, '100', g['group'], 'stale', identity(g['meta']))
        first = groups[0]
        state = rename_group(state, '100', first['group'], '', identity(first['meta']))
        self.assertEqual(group_catalog(state)[0]['label'], 'ЦА 1')

    def test_groups_without_ir_keep_descriptions_and_pn(self):
        from crm.domain import group_catalog
        state, _ = import_rows(empty_state(), [row(audience='Полный исходный текст ЦА')])
        state, _ = import_rows(state, [row(ir=0)])
        g = group_catalog(state)[0]
        self.assertIsNone(g['summary'])
        self.assertIn('Полный исходный текст ЦА', g['descriptions'])
        self.assertEqual(g['members'][0]['pn'], 'PN1')

    def test_third_wave_can_join_second_group_and_reimport(self):
        from crm.domain import group_catalog
        state, _ = import_rows(empty_state(), [row(), row(2, audience='Женщины', ir=20)])
        state = decide(state, 'PN2', 'different', identity(state['projects']['PN2']['data']))
        second = group_catalog(state)[1]
        state, _ = import_rows(state, [row(3, audience='Женщины покупатели', ir=40)])
        state = decide(state, 'PN3', 'same', identity(state['projects']['PN3']['data']), second['group'])
        state, _ = import_rows(state, [row(3, audience='Женщины покупатели', ir=40)])
        groups = group_catalog(state)
        self.assertEqual(groups[1]['summary']['mean'], 30)
        self.assertEqual(groups[1]['summary']['count'], 2)
        self.assertEqual(groups[0]['summary']['count'], 1)
        self.assertEqual([g['project_type'] for g in groups], ['трекер', 'трекер'])

    def test_invalid_identity_not_invented_and_rename_limits(self):
        from crm.domain import project_info, group_catalog, rename_group
        state, _ = import_rows(empty_state(), [row(), row(2, aorg='PN2')])
        self.assertEqual(project_info(state, '100')['total'], 1)
        g = group_catalog(state)[0]
        for aorg, name in [('999', 'name'), ('100', 'x' * 201)]:
            with self.assertRaises(IntakeError):
                rename_group(state, aorg, g['group'], name, identity(g['meta']))

if __name__ == '__main__':
    unittest.main()
