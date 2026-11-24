"""Atomic private CRM snapshots in the application's existing PostgreSQL."""

from pathlib import PurePosixPath

from psycopg.types.json import Jsonb

import db
from crm import domain


def load(before=None):
    with db.connect() as conn:
        row = conn.execute('SELECT data, revision FROM crm_dataset WHERE id = 1').fetchone()
        if row is None:
            raise RuntimeError('CRM schema is not initialized')
        events = conn.execute(
            "SELECT id, created_at, kind, filename, report - 'rows' AS summary "
            'FROM crm_history WHERE (%s::bigint IS NULL OR id < %s) ORDER BY id DESC LIMIT 30',
            (before, before),
        ).fetchall()
    return row[0], row[1], events


def history(event_id):
    with db.connect() as conn:
        return conn.execute(
            'SELECT id, created_at, kind, filename, report FROM crm_history WHERE id = %s',
            (event_id,),
        ).fetchone()


def _change(operation, kind, filename=None):
    with db.connect() as conn:
        with conn.transaction():
            row = conn.execute('SELECT data FROM crm_dataset WHERE id = 1 FOR UPDATE').fetchone()
            if row is None:
                raise RuntimeError('CRM schema is not initialized')
            state, report = operation(row[0])
            conn.execute('UPDATE crm_dataset SET data = %s, revision = revision + 1, '
                         'updated_at = now() WHERE id = 1', (Jsonb(state),))
            event_id = conn.execute(
                'INSERT INTO crm_history (kind, filename, report) VALUES (%s, %s, %s) RETURNING id',
                (kind, filename, Jsonb(report)),
            ).fetchone()[0]
    return event_id, report


def upload(content, filename):
    # Parse before taking the lock; malformed files cannot change any persisted data.
    rows = domain.read_xlsx(content)
    filename = PurePosixPath(str(filename).replace('\\', '/')).name[:200]
    return _change(lambda state: domain.import_rows(state, rows), 'import', filename)


def decision(pn, action, fingerprint, target=None):
    def operation(state):
        new = domain.decide(state, pn, action, fingerprint, target)
        return new, dict(pn=pn, action=action, fingerprint=fingerprint, target=target,
                         before=state['projects'][pn], after=new['projects'][pn])
    return _change(operation, 'decision')
