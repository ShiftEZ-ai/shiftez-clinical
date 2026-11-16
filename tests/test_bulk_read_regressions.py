"""Bulk reads stay bounded without bypassing published-duty protections."""
from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from apps.api import models as m
from apps.api.commands import require_workspace_command
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.services.credential_impact import affected


def test_bulk_workspace_guard_reads_once_and_still_refuses_hidden_targets(h):
    publication = h.publish(h.shift(h.workspace())['workspace'])['publication_id']
    hidden = h.workspace()['id']
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.PublicationChangeList(department_id=h.seed['department_id'],
            publication_id=publication, workspace_id=hidden))
    statements = []

    def sql(_connection, _cursor, statement, *_):
        statements.append(statement)

    actor = SimpleNamespace(department_id=h.seed['department_id'])
    with transaction(h.app.state.engine) as db:
        db.info['bulk'] = {'history': []}
        event.listen(h.app.state.engine, 'before_cursor_execute', sql)
        try:
            for target in h.seed['people']:
                require_workspace_command(db, actor, SimpleNamespace(
                    kind='catalogue.update', target_id=target, payload={}))
            for target, payload in ((hidden, {}), (None, {'workspace_id': hidden})):
                with pytest.raises(Problem) as error:
                    require_workspace_command(db, actor, SimpleNamespace(
                        kind='shift.action', target_id=target, payload=payload))
                assert error.value.code == 'PUBLICATION_CHANGE_LIST'
        finally:
            event.remove(h.app.state.engine, 'before_cursor_execute', sql)
    assert len([sql for sql in statements if 'FROM publication_change_lists' in sql]) == 1
    with transaction(h.app.state.engine) as db:
        with pytest.raises(Problem) as error:
            require_workspace_command(db, actor, SimpleNamespace(
                kind='workspace.submit', target_id=hidden, payload={}))
        assert error.value.code == 'PUBLICATION_CHANGE_LIST'


def test_bulk_credential_impacts_read_empty_publications_once(h):
    statements = []

    def sql(_connection, _cursor, statement, *_):
        statements.append(statement)

    with transaction(h.app.state.engine) as db:
        credential = db.scalar(select(m.Eligibility).where(m.Eligibility.department_id == h.seed['department_id']))
        db.info['bulk'] = {'history': []}
        actor = SimpleNamespace(department_id=h.seed['department_id'])
        event.listen(h.app.state.engine, 'before_cursor_execute', sql)
        try:
            for _ in range(20):
                assert affected(db, actor, credential) == {'count': 0, 'sessions': []}
        finally:
            event.remove(h.app.state.engine, 'before_cursor_execute', sql)
    assert len([sql for sql in statements if 'FROM publications' in sql]) == 1
    assert len(statements) <= 3


def test_bulk_credential_impacts_recheck_changed_coverage_and_notify(h):
    h.publish(h.shift(h.workspace())['workspace'])
    statements = []

    def sql(_connection, _cursor, statement, *_):
        statements.append(statement)

    with transaction(h.app.state.engine, write=True) as db, db.no_autoflush:
        credential = db.scalar(select(m.Eligibility).where(
            m.Eligibility.department_id == h.seed['department_id'],
            m.Eligibility.person_id == h.seed['people'][0], m.Eligibility.service_id == h.seed['services'][0]))
        db.info['bulk'] = {'history': []}
        actor = SimpleNamespace(department_id=h.seed['department_id'], identity_id=h.seed['identities']['credentialer'])
        event.listen(h.app.state.engine, 'before_cursor_execute', sql)
        try:
            credential.ends_on = date(2026, 10, 4)
            assert affected(db, actor, credential, notify=True)['count'] == 1
            assert any(isinstance(row, m.Outbox) and row.kind == 'credential_impact' for row in db.new)
            replacement = m.Eligibility(department_id=actor.department_id, person_id=credential.person_id,
                service_id=credential.service_id, starts_on=date(2026, 10, 4))
            db.add(replacement)
            assert affected(db, actor, credential, notify=True) == {'count': 0, 'sessions': []}
            db.expunge(replacement)
            credential.ends_on = None
            for _ in range(20):
                assert affected(db, actor, credential, notify=True) == {'count': 0, 'sessions': []}
        finally:
            event.remove(h.app.state.engine, 'before_cursor_execute', sql)
    assert len(statements) <= 5
