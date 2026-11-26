from contextlib import contextmanager
from copy import deepcopy
import unittest
from unittest.mock import patch

from crm import domain, store
from tests.test_crm_domain import row


class Result:
    def __init__(self, rows): self.rows = rows
    def fetchone(self): return self.rows[0] if self.rows else None
    def fetchall(self): return self.rows


class Connection:
    def __init__(self):
        self.state = domain.empty_state()
        self.events = []
        self.queries = []
        self.fail_history = False
        self.import_reports = []
    def __enter__(self): return self
    def __exit__(self, *args): return False
    @contextmanager
    def transaction(self):
        prior = deepcopy((self.state, self.events))
        try: yield
        except Exception:
            self.state, self.events = prior
            raise
    def execute(self, sql, params=()):
        self.queries.append(sql)
        if sql.startswith('SELECT report FROM crm_history'):
            return Result([(deepcopy(report),) for report in self.import_reports])
        if sql.startswith('SELECT data, revision'): return Result([(deepcopy(self.state), len(self.events))])
        if sql.startswith('SELECT data'): return Result([(deepcopy(self.state),)])
        if sql.startswith('UPDATE crm_dataset'):
            self.state = deepcopy(params[0].obj)
            return Result([])
        if sql.startswith('INSERT INTO crm_history'):
            if self.fail_history: raise RuntimeError('history unavailable')
            self.events.append(deepcopy(params))
            return Result([(len(self.events),)])
        if 'FROM crm_history' in sql: return Result([])
        raise AssertionError(sql)


class StoreTests(unittest.TestCase):
    def test_upload_locked_and_history_and_state_commit_together(self):
        conn = Connection()
        with patch.object(store.db, 'connect', return_value=conn), patch.object(domain, 'read_xlsx', return_value=[row()]):
            event, report = store.upload(b'file', r'C:\private\projects.xlsx')
        self.assertEqual((event, report['added']), (1, 1))
        self.assertIn('FOR UPDATE', conn.queries[0])
        self.assertEqual(conn.events[0][1], 'projects.xlsx')
        self.assertEqual(len(conn.events[0][2].obj['rows']), 1)
        self.assertIn('PN1', conn.state['projects'])

    def test_history_error_rolls_back_updated_snapshot(self):
        conn = Connection()
        conn.fail_history = True
        with patch.object(store.db, 'connect', return_value=conn), patch.object(domain, 'read_xlsx', return_value=[row()]):
            with self.assertRaises(RuntimeError): store.upload(b'file', 'file.xlsx')
        self.assertEqual(conn.state, domain.empty_state())

    def test_malformed_file_does_not_open_database(self):
        with patch.object(store.db, 'connect') as connect:
            with self.assertRaises(domain.IntakeError): store.upload(b'invalid', 'file.xlsx')
        connect.assert_not_called()

    def test_decision_persists_auditable_before_after(self):
        conn = Connection()
        conn.state, _ = domain.import_rows(conn.state, [row(), row(2, audience='Другая ЦА')])
        fingerprint = domain.identity(conn.state['projects']['PN2']['data'])
        with patch.object(store.db, 'connect', return_value=conn):
            store.decision('PN2', 'different', fingerprint)
            loaded, _, _ = store.load()
        self.assertFalse(loaded['projects']['PN2']['pending'])
        self.assertEqual(conn.events[0][0], 'decision')
        self.assertEqual(conn.events[0][2].obj['before']['pending'], ['audience'])



class MetadataStoreTests(unittest.TestCase):
    def legacy(self):
        conn = Connection()
        state, _ = domain.import_rows(domain.empty_state(), [row()])
        conn.state = {k: state[k] for k in ('projects', 'aliases')}
        # Earliest event predates more than one dashboard page of events.
        conn.import_reports = [dict(rows=[row(2, ir=0)])] + [dict(rows=[]) for _ in range(35)]
        return conn

    def test_load_upgrades_full_history_once_and_keeps_original_data(self):
        conn = self.legacy()
        old = deepcopy(conn.state)
        with patch.object(store.db, 'connect', return_value=conn):
            loaded, _, _ = store.load()
            store.load()
        self.assertEqual(domain.project_info(loaded, '100')['total'], 2)
        self.assertEqual(loaded['projects'], old['projects'])
        self.assertEqual(loaded['aliases'], old['aliases'])
        self.assertEqual(len(conn.events), 1)
        full = [q for q in conn.queries if q.startswith('SELECT report')]
        self.assertEqual(len(full), 1)
        self.assertNotIn('LIMIT', full[0])
        self.assertIn('FOR UPDATE', conn.queries[1])

    def test_upgrade_rolls_back_on_audit_failure(self):
        conn = self.legacy()
        before = deepcopy(conn.state)
        conn.fail_history = True
        with patch.object(store.db, 'connect', return_value=conn):
            with self.assertRaises(RuntimeError): store.load()
        self.assertEqual(conn.state, before)
        self.assertFalse(conn.events)

    def test_rename_persists_and_audits_without_changing_ir(self):
        conn = self.legacy()
        with patch.object(store.db, 'connect', return_value=conn):
            state, _, _ = store.load()
            group = domain.group_catalog(state)[0]
            original = deepcopy(state['projects'])
            store.rename('100', group['group'], 'Покупатели', domain.identity(group['meta']))
            loaded, _, _ = store.load()
        self.assertEqual(domain.group_catalog(loaded)[0]['label'], 'ЦА 1 — Покупатели')
        self.assertEqual(loaded['projects'], original)
        report = conn.events[-1][2].obj
        self.assertEqual((report['before']['name'], report['after']['name']), ('', 'Покупатели'))
        conn.fail_history = True
        before = deepcopy(conn.state)
        meta = domain.group_catalog(before)[0]['meta']
        with patch.object(store.db, 'connect', return_value=conn):
            with self.assertRaises(RuntimeError):
                store.rename('100', group['group'], 'Сбой', domain.identity(meta))
        self.assertEqual(conn.state, before)

    def test_first_mutation_also_upgrades_before_operation(self):
        conn = self.legacy()
        with patch.object(store.db, 'connect', return_value=conn), patch.object(domain, 'read_xlsx', return_value=[row(3)]):
            store.upload(b'file', 'file.xlsx')
        self.assertEqual(domain.project_info(conn.state, '100')['total'], 3)
        self.assertEqual(len(conn.events), 2)

if __name__ == '__main__': unittest.main()
