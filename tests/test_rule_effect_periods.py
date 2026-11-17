"""Changed-line date sets stay exact at the supported catalogue size."""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import rule_effects
from .test_rule_catalogue_api import approve, line, propose


def insert_publication(db, h, name, starts_on, ends_on, sequence, *, effective_until=None):
    workspace = m.Workspace(department_id=h.seed['department_id'], name=name,
        starts_on=starts_on, ends_on=ends_on, status='published',
        created_by=h.seed['identities']['coordinator'], last_edited_by=h.seed['identities']['coordinator'])
    db.add(workspace)
    db.flush()
    publication = m.Publication(department_id=h.seed['department_id'], workspace_id=workspace.id,
        sequence=sequence, starts_on=starts_on, ends_on=ends_on, current=True,
        published_by=h.seed['identities']['coordinator'], snapshot={},
        validation={'contracts': {'block_quota': 'block-quota-v6', 'programmes': 'programmes-v1'}}, fingerprint='test',
        effective_until=effective_until)
    db.add(publication)
    db.flush()
    return workspace


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_published_impact_uses_local_today_and_effective_until(request, database, monkeypatch):
    from apps.api.services.independence import today
    h = request.getfixturevalue(database)
    with transaction(h.app.state.engine, write=True) as db:
        day = today(db, h.seed['department_id'])
        past = insert_publication(db, h, 'Past publication', day - timedelta(days=7), day,
                                  1, effective_until=day - timedelta(days=1))
        current = insert_publication(db, h, "Today's publication", day, day + timedelta(days=7), 2)
        ended = insert_publication(db, h, 'Ended publication', day - timedelta(days=1), day + timedelta(days=7),
                                   3, effective_until=day)
        superseded = insert_publication(db, h, 'Superseded publication', day + timedelta(days=7), day + timedelta(days=14), 4)
        archived = insert_publication(db, h, 'Archived publication', day + timedelta(days=14), day + timedelta(days=21), 5)
        db.scalar(select(m.Publication).where(m.Publication.workspace_id == superseded.id)).current = False
        archived.archived_at = m.now()
        db.flush()
        monkeypatch.setattr(rule_effects, 'reachable_workspaces', lambda db, actor, policies, rows: rows)
        found = rule_effects.affected_workspaces(db, SimpleNamespace(department_id=h.seed['department_id']), [],
            periods=[(day - timedelta(days=365), None, None)])
        assert [row.id for row in found] == [current.id]
        assert past.id not in {row.id for row in found}
        assert not {ended.id, superseded.id, archived.id} & {row.id for row in found}


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_backdated_rule_with_24_month_publication_history_stays_below_impact_limit(request, database, monkeypatch):
    from apps.api.services.independence import today
    h = request.getfixturevalue(database)
    with transaction(h.app.state.engine, write=True) as db:
        day = today(db, h.seed['department_id'])
        month = day.year * 12 + day.month - 1
        for offset in range(-23, 1):
            total = month + offset
            starts_on = date(total // 12, total % 12 + 1, 1)
            next_total = total + 1
            ends_on = date(next_total // 12, next_total % 12 + 1, 1)
            for copy in range(5):
                window_start = day + timedelta(days=copy) if offset == 0 else starts_on + timedelta(days=copy * 5)
                window_end = window_start + timedelta(days=1 if offset == 0 else 5)
                insert_publication(db, h, f'Publication {offset} {copy}', window_start, window_end,
                                   (offset + 23) * 5 + copy + 1)
        monkeypatch.setattr(rule_effects, 'reachable_workspaces', lambda db, actor, policies, rows: rows)
        found = rule_effects.affected_workspaces(db, SimpleNamespace(department_id=h.seed['department_id']), [],
            periods=[(date(2020, 1, 1), None, None)])
        assert len(found) == 5
        assert all(day <= row.starts_on < day + timedelta(days=5) for row in found)


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_thousand_dated_lines_activate_with_exact_affected_rotas_and_audit(request, database):
    h = request.getfixturevalue(database)
    first = date(2027, 1, 1)
    last = first + timedelta(days=1998)
    early = h.workspace('First changed day', str(first), str(first + timedelta(days=1)))
    gap = h.workspace('Unchanged gap', str(first + timedelta(days=1)), str(first + timedelta(days=2)))
    late = h.workspace('Last changed day', str(last), str(last + timedelta(days=1)))
    lines = [line(numbers={'max': {'value': 5}}, starts_on=str(first + timedelta(days=2 * n)),
                  ends_on=str(first + timedelta(days=2 * n + 1))) for n in range(1000)]
    pending = propose(h, 'duties_per_week', lines)['record']
    with transaction(h.app.state.engine) as db:
        affected = rule_effects.affected_workspaces(db, SimpleNamespace(department_id=h.seed['department_id']),
                                                   [db.get(m.Policy, pending['id'])])
        assert [w.id for w in affected] == [early['id'], late['id']]
        assert gap['id'] not in {w.id for w in affected}
    activated = approve(h, pending)
    assert activated['status'] == 'active' and len(activated['definition']['rule']['lines']) == 1000
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.AuditEvent.id).where(m.AuditEvent.action == 'policy.activate',
            m.AuditEvent.target_id == pending['id']))
        versions = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.kind == 'policy',
            m.DefinitionHistory.object_id == pending['id']).order_by(m.DefinitionHistory.object_revision)))
        assert [v.snapshot['status'] for v in versions] == ['proposed', 'active']


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_changed_period_query_preserves_exclusive_bounds_scope_and_open_end(request, database, monkeypatch):
    h = request.getfixturevalue(database)
    with transaction(h.app.state.engine, write=True) as db:
        rows = [m.Workspace(department_id=h.seed['department_id'], name=name,
            created_by=h.seed['identities']['coordinator'], last_edited_by=h.seed['identities']['coordinator'],
            starts_on=date.fromisoformat(start), ends_on=date.fromisoformat(end), status='draft')
            for name, start, end in [('match', '2027-01-01', '2027-01-02'),
                ('touching', '2027-01-02', '2027-01-03'), ('unscoped', '2030-01-01', '2030-01-02'),
                ('scoped', '2030-01-01', '2030-01-02')]]
        db.add_all(rows)
        db.flush()
        for periods, expected in [
            ([(date(2027, 1, 1), date(2027, 1, 2), None), (date(2028, 1, 1), None, rows[3].id)],
             [rows[0].id, rows[3].id]),
            ([(date(2028, 1, 1), None, None)], sorted([rows[2].id, rows[3].id])),
        ]:
            monkeypatch.setattr(rule_effects, 'changed_periods', lambda *_: periods)
            affected = rule_effects.affected_workspaces(db, SimpleNamespace(department_id=h.seed['department_id']), [])
            assert [w.id for w in affected] == expected
