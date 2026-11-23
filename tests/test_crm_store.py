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


if __name__ == '__main__': unittest.main()
