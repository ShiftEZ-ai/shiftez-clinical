"""Matched end-date and frozen-evidence controls for maintenance's three failing commands."""
import json
from datetime import datetime

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from .clock_support import freeze_clock


REASON = 'Synthetic matched retirement control'


@pytest.mark.parametrize('kind', ['membership.withdraw', 'eligibility.withdraw', 'rotation.remove'])
@pytest.mark.parametrize('ended', [False, True], ids=['other_records_unchanged', 'other_records_ended_Nov11'])
@pytest.mark.parametrize('frozen_evidence', [False, True], ids=['no_frozen_reference', 'frozen_review_reference'])
def test_future_removal_controls(h, monkeypatch, kind, ended, frozen_evidence):
    freeze_clock(monkeypatch, datetime.fromisoformat('2026-11-11T19:00:00+00:00'))
    person = h.configure('people', {'display_name': 'Synthetic active retirement control'})['record']
    categories = [h.configure('categories', {'name': f'Control rank {n}', 'starts_on': '2020-01-01'})['record']
                  for n in range(2)]
    services = [h.configure('services', {'name': f'Control clinic {n}', 'starts_on': '2020-01-01'})['record']
                for n in range(2)]
    programme = h.configure('programmes', {'name': 'Control programme', 'starts_on': '2020-01-01'})['record']
    cohort = h.configure('cohorts', {'name': 'Control cohort', 'programme_id': programme['id'],
                         'starts_on': '2026-01-01', 'ends_on': '2030-01-01'})['record']
    typ = h.configure('rotation_types', {'name': 'Control rotation', 'starts_on': '2020-01-01'})['record']
    rank = h.command('coordinator', 'membership.create', {'person_id': person['id'],
                     'category_id': categories[0]['id'], 'starts_on': '2026-01-01'})['record']
    clinic = h.command('credentialer', 'eligibility.create', {'person_id': person['id'],
                       'service_id': services[0]['id'], 'starts_on': '2026-01-01'})['record']
    member_value = {'person_id': person['id'], 'programme_id': programme['id'],
                    'starts_on': '2026-01-01', 'ends_on': '2030-01-01'}
    member = h.configure('programme_memberships', member_value, role='coordinator')['record']
    rotation_value = {'person_id': person['id'], 'cohort_id': cohort['id'], 'rotation_type_id': typ['id'],
                      'service_id': services[0]['id'], 'starts_on': '2026-10-01', 'ends_on': '2026-12-01'}
    rotation = h.configure('rotations', rotation_value, role='coordinator')['record']
    dates = {'starts_on': '2026-12-01', 'ends_on': '2027-01-01'}
    if kind == 'membership.withdraw':
        role, cls = 'coordinator', m.Membership
        target = h.command(role, 'membership.create', {'person_id': person['id'],
                           'category_id': categories[1]['id'], **dates})['record']
    elif kind == 'eligibility.withdraw':
        role, cls = 'credentialer', m.Eligibility
        target = h.command(role, 'eligibility.create', {'person_id': person['id'],
                           'service_id': services[1]['id'], **dates})['record']
    else:
        role, cls = 'coordinator', m.Rotation
        target = h.configure('rotations', {**rotation_value, **dates}, role=role)['record']
    if ended:
        last = '2026-11-12'
        h.configure('rotations', {**rotation_value, 'ends_on': last}, rotation['id'], rotation['revision'], role='coordinator')
        h.command('coordinator', 'membership.end', {'ends_on': last, 'reason': REASON}, rank['id'], rank['revision'])
        h.command('credentialer', 'eligibility.end', {'ends_on': last, 'reason': REASON}, clinic['id'], clinic['revision'])
        h.configure('programme_memberships', {**member_value, 'ends_on': last}, member['id'], member['revision'], role='coordinator')
    frozen = workspace = None
    if frozen_evidence:
        workspace = h.workspace(start='2026-12-01', end='2026-12-08')
        frozen = h.snapshot(workspace)
        assert target['id'] in json.dumps(frozen)
        with transaction(h.app.state.engine, write=True) as db:
            db.get(m.Workspace, workspace['id']).review_snapshot = frozen
    if kind == 'rotation.remove':
        reply = h.command(role, kind, {'items': [{'id': target['id'], 'revision': target['revision']}], 'reason': REASON})
        assert reply['removed'] == 1 and reply['kept'] == 0 and reply['removed_ids'] == [target['id']]
    else:
        reply = h.command(role, kind, {'reason': REASON}, target['id'], target['revision'])
        assert reply['removed_id'] == target['id']
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Person, person['id']).active
        assert db.get(cls, target['id']) is None
        history = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.object_id == target['id'])
                                  .order_by(m.DefinitionHistory.object_revision)))
        assert history[-1].object_revision == target['revision'] + 1 and history[-1].reason == REASON
        if frozen_evidence:
            assert db.get(m.Workspace, workspace['id']).review_snapshot == frozen
