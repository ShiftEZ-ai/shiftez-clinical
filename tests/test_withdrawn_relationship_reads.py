"""Retained relationships never grant live rank, reach, authority or collection counts."""
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import rows_many
from apps.api.snapshots import snapshot_inputs
from apps.api.services import bulk_selection, people
from packages.domain.conditions import rule_holds, scope_applies
from packages.domain.validation import department_memberships, qualification_failures, qualified
from .test_future_record_withdrawal import REASON, _rank, _eligibility, _programme
from .test_rotation_withdrawal import future_value
from .test_bulk_selection import actor as selection_actor


@pytest.mark.parametrize('name,cls,make,kind,role', [
    ('memberships', m.Membership, _rank, 'membership.withdraw', 'coordinator'),
    ('eligibilities', m.Eligibility, _eligibility, 'eligibility.withdraw', 'credentialer'),
    ('programme_memberships', m.ProgrammeMembership, _programme, 'programme_membership.withdraw', 'coordinator'),
    ('rotations', m.Rotation, None, 'rotation.remove', 'coordinator')])
def test_withdrawn_rows_absent_from_core_snapshots_people_reach_and_counts(h, name, cls, make, kind, role):
    if make:
        row = make(h, '2028-02-01')
    else:
        row = h.configure('rotations', future_value(h), role='coordinator')['record']
    person_id = row['person_id']
    day = date.fromisoformat(row['starts_on'])
    a = datetime.combine(day, datetime.min.time(), timezone.utc) + timedelta(hours=6)
    b = a + timedelta(hours=1)
    # Public selection/count paths must agree even after their ORM query becomes a Core UNION.
    client = h.client('coordinator')
    selection = {'collection': name, 'where': {'filters': {'id': [row['id']]}}}
    def page():
        response = client.post('/api/bulk/rows', json={'selection': selection})
        assert response.status_code == 200, response.text
        return response.json()
    assert page()['count'] == 1
    if name == 'eligibilities':
        with transaction(h.app.state.engine) as db:
            before = snapshot_inputs(db, db.get(m.Organisation, h.seed['organisation_id']))
        shift = {'assignment_type_id': next(iter(before['types'])), 'service_ids': [row['service_id']]}
        assert 'ELIGIBILITY_MISSING' not in {f['code'] for f in qualification_failures(before, person_id, shift, {}, a, b)}
    payload = {'items': [{'id': row['id'], 'revision': row['revision']}], 'reason': REASON} if not make else {'reason': REASON}
    h.command(role, kind, payload, *([row['id'], row['revision']] if make else []))
    assert page()['count'] == 0
    with transaction(h.app.state.engine) as db:
        org = db.get(m.Organisation, h.seed['organisation_id'])
        data = snapshot_inputs(db, org)
        assert row['id'] not in {r['id'] for r in data[name]}
        assert rows_many(db, {name: (cls, cls.id == row['id'])})[name] == []
        assert db.scalar(select(cls.id).where(cls.id == row['id'])) is None
        retained = db.get(cls, row['id'], execution_options={'include_withdrawn': True})
        assert retained.withdrawn
        actor = selection_actor(db, h)
        facts = people.current_people(db, actor, day, person_ids=[person_id])['people'][person_id]
        field = {'memberships': 'category_ids', 'programme_memberships': 'programme_ids',
                 'rotations': 'rotation_type_ids'}.get(name)
        if field:
            foreign = {'category_ids': 'category_id', 'programme_ids': 'programme_id', 'rotation_type_ids': 'rotation_type_id'}[field]
            assert row[foreign] not in facts['current_' + field]
            assert row[foreign] not in rule_holds(data, person_id, None, a, b)(field)
            scope = {'conditions': {'conditions': [{'field': field, 'op': 'any', 'values': [row[foreign]]}]}}
            assert not scope_applies(data, scope, person_id, None, a, b)
        if name == 'memberships':
            assert row['id'] not in {r['id'] for r in department_memberships(data, person_id, actor.department_id, a, b)}
            current = people.current_people(db, actor, day, person_ids=data['people'])
            assert people.current_people_counts(db, actor, day) == {
                key: current[key] for key in ('active_people', 'people_without_current_rank')}
        if name == 'eligibilities':
            missing = [f for f in qualification_failures(data, person_id, shift, {}, a, b) if f['code'] == 'ELIGIBILITY_MISSING']
            assert len(missing) == 1 and row['id'] not in missing[0]['existing_record_ids']
            assert not qualified(data, person_id, shift, {}, a, b)  # The solver uses this same qualification function.
        if name == 'programme_memberships':
            from apps.api.services.training import is_trainee
            assert not is_trainee(db, actor.department_id, person_id, day, day + timedelta(days=1), [row['programme_id']])
        counts = bulk_selection.collections(db, actor)['collections']
        assert counts[name]['count'] == len(list(db.scalars(select(cls).where(cls.department_id == actor.department_id))))
    workspace = h.workspace(start=day.isoformat(), end=(day + timedelta(days=7)).isoformat())
    assert row['id'] not in {r['id'] for r in h.snapshot(workspace)[name]}
    if field:
        response = client.post('/api/bulk/rows', json={'selection': {'collection': 'people',
            'where': {'as_of': day.isoformat(), 'filters': {'id': [person_id]},
                      'conditions': scope['conditions']}}})
        assert response.status_code == 200, response.text
        assert response.json()['count'] == 0
    # Internal DML bookkeeping must stamp retained targets too; it is not a live authority read.
    with transaction(h.app.state.engine, write=True) as db:
        org = db.get(m.Organisation, h.seed['organisation_id'])
        before = org.input_revision
        assert row['id'] not in {r['id'] for r in snapshot_inputs(db, org)[name]}
        db.execute(update(cls.__table__).where(cls.id == row['id']).values(withdrawn=False))
        db.flush()
        assert org.input_revision == before + 1
        assert row['id'] in {r['id'] for r in snapshot_inputs(db, org)[name]}
