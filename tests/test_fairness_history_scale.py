"""Years of published participation are read once, with bounded publication-sized working data."""
from copy import deepcopy
from datetime import date, timedelta
import json
import time
from uuid import uuid4

import pytest
from sqlalchemy import event, insert

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services.fairness import history
from packages.domain import fairness as f
from packages.domain.conditions import _month_shift
from packages.domain.quotas import METRIC_VERSION
from packages.contracts.schemas import BalanceGoal
from tests.test_engine_fairness_review import policy


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize('window', ['all_history', 'default'])
def test_three_years_of_department_history_streams_once_and_prorates_new_joiner(h, request, window):
    workspace = h.workspace(start='2026-11-01', end='2026-12-01')
    workspace = h.shift(workspace, start='2026-11-05T08:00:00+03:00', end='2026-11-05T12:00:00+03:00',
                        people=[h.seed['people'][1]])['workspace']
    data = h.snapshot(workspace)
    template = deepcopy(next(s for s in data['shifts'] if s['id'] in data['focus_ids']))
    original = template['participants'][0]['person_id']
    people = [str(uuid4()) for _ in range(150)]
    person = data['people'][original]
    base = {key: [r for r in data[key] if r.get('person_id') == original]
            for key in ('memberships', 'eligibilities', 'grants')}
    data.update(people={p: {**person, 'id': p} for p in people}, availability=[], rotations=[],
                memberships=[], eligibilities=[], grants=[], policies=[])
    for key, rows in base.items():
        data[key] = [{**row, 'person_id': pid, 'starts_on': '2026-10-15' if pid == people[-1] else '2020-01-01'}
                     for pid in people for row in rows]
    data['shifts'] = [{**template, 'participants': [{**template['participants'][0], 'person_id': p} for p in people]}]
    settings = BalanceGoal(kind='balance_goal', metric='duties',
                           **({'period': window} if window != 'default' else {})).model_dump(mode='json')
    goal = policy(settings)
    goal.update(department_id=data['workspace']['department_id'], starts_on='2026-11-01')
    data['policies'] = [goal]
    # Use the harness's recorded workspace creator rather than an invented identity.
    with transaction(h.app.state.engine, write=True) as db:
        current = db.get(m.Workspace, workspace['id'])
        coordinator = current.created_by
        db.execute(insert(m.Person), [{'id': p, 'organisation_id': h.seed['organisation_id'], 'display_name': f'History {i}'}
                                      for i, p in enumerate(people)])
        ledger = []
        for month in range(36):
            first = _month_shift(date(2023, 11, 1), month)
            last = _month_shift(first, 1)
            w = m.Workspace(department_id=h.seed['department_id'], name=f'History {first}', starts_on=first, ends_on=last,
                            status='draft', created_by=coordinator, last_edited_by=coordinator)
            db.add(w)
            db.flush()
            pub_id = str(uuid4())
            shifts = []
            for i in range(600):
                pid = people[i % 150]
                day = first + timedelta(days=i // 150 + 16)
                if pid == people[-1] and day < date(2026, 10, 15):
                    continue
                start, end = f'{day}T08:00:00+03:00', f'{day}T12:00:00+03:00'
                shifts.append({**template, 'id': str(uuid4()), 'start': start, 'end': end,
                    'participants': [{**template['participants'][0], 'person_id': pid, 'start': start, 'end': end}]})
            ledger.extend({'department_id': h.seed['department_id'], 'publication_id': pub_id,
                'person_id': shift['participants'][0]['person_id'], 'metric_key': 'duties',
                'bucket': date.fromisoformat(shift['start'][:10]), 'value': 1.0} for shift in shifts)
            db.add(m.Publication(id=pub_id, department_id=h.seed['department_id'], workspace_id=w.id,
                sequence=month + 1, starts_on=first, ends_on=last, current=True, published_by=coordinator,
                fingerprint='0' * 64, validation={'contracts': {'block_quota': METRIC_VERSION, 'programmes': 'programmes-v1'}},
                snapshot={'timezone': 'Asia/Riyadh', 'shifts': shifts}))
            w.status = 'published'
            db.flush()
            day = first
            while day < last:
                ledger.extend({'department_id': h.seed['department_id'], 'publication_id': pub_id,
                    'person_id': pid, 'metric_key': f.AVAILABLE, 'bucket': day, 'value': 1.0}
                    for pid in people if pid != people[-1] or day >= date(2026, 10, 15))
                day += timedelta(days=1)
            for k in range(0, len(ledger), 5000):
                db.execute(insert(m.FairnessLedger), ledger[k:k + 5000])
            ledger.clear()
    data.pop('fairness_history_start', None)  # Publications were seeded after the draft snapshot.
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        if context.execution_options.get('yield_per') == 1 and 'FROM publications' in statement:
            statements.append(statement)
    event.listen(h.app.state.engine, 'before_cursor_execute', capture)
    started = time.perf_counter()
    loaded_months = []
    try:
        with transaction(h.app.state.engine) as db:
            event.listen(db, 'loaded_as_persistent', lambda session, row:
                loaded_months.append(row.starts_on) if isinstance(row, m.Publication) else None)
            result = history(db, data, [goal])
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', capture)
    measurement = {'people': 150, 'stored_months': 36, 'loaded_months': len(loaded_months), 'window': window,
                   'sessions_per_month': '596–600',
                   'history_seconds': time.perf_counter() - started, 'publication_reads': len(statements)}
    request.node.user_properties.append(('fairness_history_scale', json.dumps(measurement)))
    assert len(statements) == 1
    entries = result[f.goal_identity(goal)]['people']
    assert len(entries) == 150
    history_start = date(2023, 11, 1) if window == 'all_history' else date(2025, 12, 1)
    months = 36 if window == 'all_history' else 11  # November draft is the twelfth month of the window.
    assert len(loaded_months) == months
    assert min(loaded_months) == history_start
    assert entries[people[0]] == {'value': 4 * months, 'available': (date(2026, 11, 1) - history_start).days}
    assert entries[people[-1]] == {'value': 4, 'available': 17}
    assert data['fairness_history_start'] == '2023-11-01'
