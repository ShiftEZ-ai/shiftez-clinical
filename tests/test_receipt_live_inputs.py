"""Receipt currency includes live facts in the rota's reach, with no organisation-wide false staleness."""
from datetime import date

import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.api.snapshots import currency_hint, currency_match, feasibility_fingerprints
from packages.domain.time import aware
from .test_approval_completeness import backend, checked_approval, approval_receipt  # noqa: F401


@pytest.mark.parametrize('kind', ['leave', 'person_fact', 'calendar_day', 'clinic_deactivation',
                                  'location_deactivation', 'parent_location_deactivation', 'protected_time'])
@pytest.mark.parametrize('in_reach', [True, False], ids=['this-rota', 'unrelated'])
def test_live_input_currency_tracks_rota_reach(backend, kind, in_reach):
    h = backend
    day = {'id': m.uid(), 'name': 'Gate day', 'first_day': '2026-10-04',
           'last_day': '2026-10-04', 'repeat': 'once'}
    with transaction(h.app.state.engine, write=True) as db:
        other = m.Department(organisation_id=h.seed['organisation_id'], name='Unrelated receipt department')
        person = m.Person(organisation_id=h.seed['organisation_id'], display_name='Unrelated receipt person')
        db.add_all([other, person])
        db.flush()
        calendar = m.DateSet(department_id=h.seed['department_id'], name='Receipt days', entries=[day])
        other_calendar = m.DateSet(department_id=other.id, name='Unrelated receipt days', entries=[day])
        location = m.Location(department_id=other.id, name='Unrelated receipt location', starts_on=date(2020, 1, 1),
            location_type_id=db.get(m.Location, h.seed['locations'][0]).location_type_id)
        clinic = m.Service(department_id=other.id, name='Unrelated receipt clinic', starts_on=date(2020, 1, 1))
        db.add_all([calendar, other_calendar, location, clinic])
        db.flush()
        parent = None
        if kind == 'parent_location_deactivation':
            parent = m.Location(department_id=h.seed['department_id'], name='Receipt parent location',
                starts_on=date(2020, 1, 1), location_type_id=location.location_type_id)
            db.add(parent)
            db.flush()
            db.get(m.Location, h.seed['locations'][0]).parent_id = parent.id
        calendar_id = calendar.id
        target_calendar = calendar.id if in_reach else other_calendar.id
        person_id = h.seed['people'][0] if in_reach else person.id
        department_id = h.seed['department_id'] if in_reach else other.id
        location_id = (parent.id if parent else h.seed['locations'][0]) if in_reach else location.id
        clinic_id = h.seed['services'][0] if in_reach else clinic.id
    h, workspace, policy, job = checked_approval(h, where={'conditions': [
        {'field': 'date_set_ids', 'values': [calendar_id], 'entry_names': ['Gate day']}]})
    assert h.get('approver', f"/api/policies/{policy['id']}/review")['ready']
    with transaction(h.app.state.engine, write=True) as db:
        if kind in {'leave', 'protected_time'}:
            db.add(m.Availability(person_id=person_id, department_id=department_id,
                kind='recorded_leave' if kind == 'leave' else 'protected_time',
                start=aware('2026-10-04T08:00:00+03:00'), end=aware('2026-10-04T12:00:00+03:00'),
                private_reason='Synthetic receipt currency test', recorded_by=h.seed['identities']['coordinator']))
        elif kind == 'person_fact':
            db.get(m.Person, person_id).active = False
        elif kind == 'calendar_day':
            db.get(m.DateSet, target_calendar).entries = [{**day, 'first_day': '2026-10-05', 'last_day': '2026-10-05'}]
        else:
            db.get(m.Service if kind == 'clinic_deactivation' else m.Location,
                   clinic_id if kind == 'clinic_deactivation' else location_id).active = False
    with transaction(h.app.state.engine) as db:
        current = db.get(m.Workspace, workspace['id'])
        receipt = db.get(m.Job, job['id'])
        assert not currency_match(receipt.read_metadata['input_snapshot']['currency_hint'], currency_hint(db, current))
        assert (receipt.input_fingerprint == feasibility_fingerprints(db, [current])[current.id]) is (not in_reach)
    review = h.get('approver', f"/api/policies/{policy['id']}/review")
    effect, = review['effect']
    assert review['ready'] is (not in_reach)
    assert effect['status'] == ('not_checked' if in_reach else 'ready')
    activated = h.command('approver', 'policy.activate', {'reason': 'Independent live-input receipt review',
        'check_job_ids': [job['id']]}, policy['id'], policy['revision'], status=409 if in_reach else 200)
    if in_reach:
        assert activated['error']['code'] == 'RULE_CHECK_REQUIRED'
    else:
        assert activated['record']['status'] == 'active'


@pytest.mark.parametrize('setting,value', [('timezone', 'UTC'), ('week_start', 0)])
def test_live_time_settings_invalidate_receipts(approval_receipt, setting, value):
    h, _, policy, job = approval_receipt
    assert h.get('approver', f"/api/policies/{policy['id']}/review")['ready']
    with transaction(h.app.state.engine, write=True) as db:
        setattr(db.get(m.Organisation, h.seed['organisation_id']), setting, value)
    review = h.get('approver', f"/api/policies/{policy['id']}/review")
    assert not review['ready'] and review['effect'][0]['status'] == 'not_checked'
    refused = h.command('approver', 'policy.activate', {'reason': 'Time settings changed', 'check_job_ids': [job['id']]},
        policy['id'], policy['revision'], status=409)
    assert refused['error']['code'] == 'RULE_CHECK_REQUIRED'
