"""Counted goals share one publication snapshot read for a whole check."""
import copy
import json
import time
import uuid
from datetime import date

import pytest
from sqlalchemy import event

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services.fairness import history
from packages.contracts.schemas import PolicyDefinition
from packages.domain.fairness import balance_goals, goal_identity
from packages.domain.quotas import METRIC_VERSION


def _history_world(h, people, publications, sessions):
    workspace = h.workspace(start='2027-01-01', end='2027-02-01')
    workspace = h.shift(workspace, '2027-01-01T08:00:00+03:00', '2027-01-01T12:00:00+03:00')['workspace']
    data = h.snapshot(workspace)
    source = h.seed['people'][0]
    members = [r for r in data['memberships'] if r['person_id'] == source]
    eligibilities = [r for r in data['eligibilities'] if r['person_id'] == source]
    for n in range(len(data['people']), people):
        pid = str(uuid.uuid4())
        data['people'][pid] = {**data['people'][source], 'id': pid, 'display_name': f'Synthetic clinician {n}'}
        data['memberships'].extend({**r, 'id': str(uuid.uuid4()), 'person_id': pid} for r in members)
        data['eligibilities'].extend({**r, 'id': str(uuid.uuid4()), 'person_id': pid} for r in eligibilities)
    person_ids = list(data['people'])
    clinics = h.seed['services'][:2]
    template = data['shifts'][0]
    snapshots = []
    with transaction(h.app.state.engine, write=True) as db:
        current = db.get(m.Workspace, workspace['id'])
        for month in range(1, publications + 1):
            first = date(2026, month, 1)
            last = date(2026 + month // 12, month % 12 + 1, 1)
            past = m.Workspace(department_id=current.department_id, name=f'Synthetic history {month}',
                               starts_on=first, ends_on=last, status='published',
                               created_by=current.created_by, last_edited_by=current.last_edited_by)
            db.add(past)
            db.flush()
            shifts = []
            for n in range(sessions):
                shift = copy.deepcopy(template)
                on = date(2026, month, 1 + n // 20)
                shift.update(id=str(uuid.uuid4()), service_ids=[clinics[n % 2]],
                             start=f'{on}T08:00:00+03:00', end=f'{on}T12:00:00+03:00')
                shift['participants'] = [{**template['participants'][0], 'id': str(uuid.uuid4()),
                    'shift_id': shift['id'], 'person_id': person_ids[n % people],
                    'start': shift['start'], 'end': shift['end']}]
                shifts.append(shift)
            snapshot = {'shifts': shifts, 'locations': data['locations']}
            snapshots.append(snapshot)
            publication = m.Publication(department_id=current.department_id, workspace_id=past.id,
                sequence=month, starts_on=first, ends_on=last, current=True,
                published_by=current.created_by, snapshot=snapshot,
                validation={'contracts': {'block_quota': METRIC_VERSION, 'programmes': 'programmes-v1'}},
                fingerprint='a' * 64)
            db.add(publication)
            db.flush()
            db.add_all(m.FairnessLedger(department_id=current.department_id, publication_id=publication.id,
                person_id=pid, metric_key='available_days', bucket=first, value=(last - first).days)
                for pid in person_ids)
    data['policies'] = []
    for metric in ('duties', 'hours', 'session_units', 'weekends'):
        definition = PolicyDefinition.model_validate({
            'scope': {'conditions': {'conditions': [{'field': 'service_ids', 'op': 'any',
                                                     'values': clinics, 'count': 'each'}]}},
            'rule': {'kind': 'balance_goal', 'metric': metric, 'period': 'custom',
                     'starts_on': '2026-01-01', 'ends_on': '2027-02-01', 'aim': 'equal_share'},
            'severity': 'advisory'}).model_dump(mode='json')
        data['policies'].append({'id': str(uuid.uuid4()), 'name': f'Synthetic {metric} goal',
            'kind': 'balance_goal', 'revision': 1, 'status': 'active', 'active': True,
            'department_id': current.department_id, 'starts_on': '2020-01-01', 'ends_on': None,
            'definition': definition})
    return data, sum(len(json.dumps(snapshot).encode()) for snapshot in snapshots)


@pytest.mark.parametrize('people,publications,sessions', [(3, 2, 4), (150, 12, 560)])
def test_counted_history_loads_snapshots_once_for_multiple_goals(h, monkeypatch, people, publications, sessions):
    data, snapshot_bytes = _history_world(h, people, publications, sessions)
    goals = balance_goals(data)
    assert len(goals) == 8
    loads, queries = [], []

    def load(*args, **kwargs):
        started = time.perf_counter()
        sample = [0, 0.0]
        loads.append(sample)
        for row in original(*args, **kwargs):
            sample[0] += 1
            yield row
        sample[1] = time.perf_counter() - started

    def query(_connection, _cursor, statement, _parameters, _context, _many):
        if 'FROM publications' in statement and 'publications.snapshot' in statement:
            queries.append(statement)

    engine = h.app.state.engine
    event.listen(engine, 'before_cursor_execute', query)
    try:
        with transaction(engine) as db:
            original = db.scalars
            monkeypatch.setattr(db, 'scalars', load)
            started = time.perf_counter()
            result = history(db, data, goals)
            elapsed = time.perf_counter() - started
    finally:
        event.remove(engine, 'before_cursor_execute', query)
    assert len(loads) == len(queries) == 1
    assert loads[0][0] == publications
    assert set(result) == {goal_identity(goal) for goal in goals}
    duties = [goal for goal in goals if goal['definition']['rule']['metric'] == 'duties']
    assert sum(row['value'] for goal in duties for row in result[goal_identity(goal)]['people'].values()) == publications * sessions
    assert all(len(report['people']) == people for report in result.values())
    print('counted goal history', {'people': people, 'sessions_per_month': sessions, 'publications': publications,
        'goals': len(goals), 'snapshot_bytes': snapshot_bytes, 'snapshot_queries': len(queries),
        'snapshot_stream_seconds': round(loads[0][1], 4), 'history_seconds': round(elapsed, 4)})
