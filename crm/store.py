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
        if needs_initialization(row[0]):
            with conn.transaction():
                locked = conn.execute('SELECT data FROM crm_dataset WHERE id = 1 FOR UPDATE').fetchone()
                _initialize_locked(conn, locked[0])
            row = conn.execute('SELECT data, revision FROM crm_dataset WHERE id = 1').fetchone()
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


def _persist(conn, state, kind, report, filename=None):
    conn.execute('UPDATE crm_dataset SET data = %s, revision = revision + 1, '
                 'updated_at = now() WHERE id = 1', (Jsonb(state),))
    return conn.execute(
        'INSERT INTO crm_history (kind, filename, report) VALUES (%s, %s, %s) RETURNING id',
        (kind, filename, Jsonb(report)),
    ).fetchone()[0]


def needs_initialization(state):
    return state.get('metadata_version') != 5 or state.get('audience_matching_version') != 2


def _initialize_locked(conn, state):
    if not needs_initialization(state):
        return state
    reports = []
    if state.get('metadata_version') != 5:
        reports = conn.execute("SELECT report FROM crm_history WHERE kind = 'import' ORDER BY id").fetchall()
    upgraded = domain.upgrade_state(state, (row[0] for row in reports))
    report = dict(action='initialize_metadata', version=5)
    if state.get('audience_matching_version') != 2:
        upgraded = domain.refresh(upgraded)
        report.update(action='refresh_audience_matching', matching_version=2,
                      resolved=[pn for pn,r in state['projects'].items()
                                if 'audience' in r.get('pending', [])
                                and 'audience' not in upgraded['projects'][pn].get('pending', [])])
    _persist(conn, upgraded, 'decision', report)
    return upgraded


def _change(operation, kind, filename=None):
    with db.connect() as conn:
        with conn.transaction():
            row = conn.execute('SELECT data FROM crm_dataset WHERE id = 1 FOR UPDATE').fetchone()
            if row is None:
                raise RuntimeError('CRM schema is not initialized')
            state, report = operation(_initialize_locked(conn, row[0]))
            event_id = _persist(conn, state, kind, report, filename)
    return event_id, report


def upload(content, filename):
    # Parse before taking the lock; malformed files cannot change any persisted data.
    rows = domain.read_xlsx(content)
    filename = PurePosixPath(str(filename).replace('\\', '/')).name[:200]
    return _change(lambda state: domain.import_rows(state, rows), 'import', filename)


def decision_report(state, new, pn, action, fingerprint, target=None):
    record = new['projects'][pn]
    aorg, group = record['data']['aorg'], record.get('group')
    affected = [key for key, r in new['projects'].items()
                if (r.get('group'), r.get('pending')) !=
                (state['projects'].get(key, {}).get('group'), state['projects'].get(key, {}).get('pending'))]
    selected = next((g for g in domain.group_catalog(new) if g['aorg'] == aorg and g['group'] == group), None)
    return dict(pn=pn, action=action, fingerprint=fingerprint, target=target,
                before=state['projects'][pn], after=record, aorg=aorg, group=group,
                group_label=selected['label'] if selected else '', affected=affected,
                pending=record.get('pending', []))


def decision(pn, action, fingerprint, target=None):
    def operation(state):
        new = domain.decide(state, pn, action, fingerprint, target)
        return new, decision_report(state, new, pn, action, fingerprint, target)
    return _change(operation, 'decision')


def rename(aorg, group, name, fingerprint):
    def operation(state):
        new = domain.rename_group(state, aorg, group, name, fingerprint)
        return new, dict(action='rename_group', aorg=aorg, group=group,
                         before=state['group_meta'][aorg][group],
                         after=new['group_meta'][aorg][group],
                         group_label=next(g['label'] for g in domain.group_catalog(new) if g['aorg'] == aorg and g['group'] == group))
    return _change(operation, 'decision')
