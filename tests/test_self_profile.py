"""Staff read their own dated facts without inheriting catalogue or colleague-record access."""
import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import self_profile
from .governance_support import identity


@pytest.fixture
def facts(h):
    own, mentor, trainee = h.seed['people'][:3]
    with transaction(h.app.state.engine, write=True) as db:
        dept = db.get(m.Department, h.seed['department_id'])
        foreign = m.Department(organisation_id=dept.organisation_id, name='Other department')
        programme = m.Programme(department_id=dept.id, name='Own programme', starts_on=date(2026, 1, 1))
        other_programme = m.Programme(department_id=dept.id, name='Unrelated programme', starts_on=date(2026, 1, 1))
        skill = m.Capability(department_id=dept.id, name='Own skill', starts_on=date(2026, 1, 1))
        db.add_all([foreign, programme, other_programme, skill]); db.flush()
        cohort = m.Cohort(department_id=dept.id, programme_id=programme.id, name='Own cohort',
                         starts_on=date(2026, 1, 1), ends_on=date(2027, 1, 1))
        db.add(cohort); db.flush()
        member = m.ProgrammeMembership(department_id=dept.id, person_id=own, programme_id=programme.id,
                                      starts_on=date(2026, 10, 1), ends_on=date(2026, 11, 1))
        rotation = m.Rotation(department_id=dept.id, person_id=own, mentor_id=mentor, cohort_id=cohort.id,
                             service_id=h.seed['services'][0], starts_on=date(2026, 10, 5), ends_on=date(2026, 10, 7), department_available=False)
        attachments = [m.Mentorship(department_id=dept.id, person_id=a, mentor_id=b, programme_id=programme.id,
            assignment_type_ids=[h.seed['type']], service_ids=[h.seed['services'][0]], strength='required',
            starts_on=date(2026, 10, 1), ends_on=date(2026, 11, 1)) for a, b in ((own, mentor), (trainee, own), (trainee, mentor))]
        grants = [m.CapabilityGrant(department_id=department, person_id=person, capability_id=skill.id,
            granted_by=h.seed['identities']['credentialer'], evidence='PRIVATE verification evidence',
            starts_on=date(2026, 10, 1), ends_on=date(2026, 11, 1), revoked=False)
            for department, person in ((dept.id, own), (dept.id, mentor), (foreign.id, own))]
        entries = [m.ProgrammeCalendarEntry(department_id=dept.id, programme_id=program, name=name,
            kind='academic_block', description='PRIVATE programme note', starts_on=start, ends_on=end)
            for program, name, start, end in (
                (programme.id, 'Own academic dates', date(2026, 10, 5), date(2026, 10, 7)),
                (programme.id, 'After membership', date(2026, 11, 1), date(2026, 11, 3)),
                (other_programme.id, 'Unrelated academic dates', date(2026, 10, 5), date(2026, 10, 7)))]
        db.add_all([member, rotation, *attachments, *grants, *entries]); db.flush()
        return {'person': own, 'peers': {mentor, trainee}, 'member': member.id, 'rotation': rotation.id,
                'attachments': {r.id for r in attachments[:2]}, 'unrelated_attachment': attachments[2].id,
                'grant': grants[0].id, 'other_grants': {r.id for r in grants[1:]}, 'entry': entries[0].id,
                'other_entries': {r.id for r in entries[1:]}}


def test_self_profile_dates_references_and_both_attachment_directions_are_private(h, facts):
    result = h.get('staff', '/api/me/profile')
    data = result['catalogues']
    assert result['person_id'] == facts['person']
    assert {r['id'] for r in data['people']} == {facts['person'], *facts['peers']}
    assert all(set(r) == {'id', 'display_name'} for r in data['people'])
    assert {r['id'] for r in data['mentorships']} == facts['attachments']
    assert {r['id'] for r in data['rotations']} == {facts['rotation']}
    assert data['rotations'][0]['department_available'] is False
    assert {r['id'] for r in data['programme_memberships']} == {facts['member']}
    assert {r['id'] for r in data['programme_calendar_entries']} == {facts['entry']}
    assert facts['grant'] in {r['id'] for r in data['grants']}
    assert not facts['other_grants'] & {r['id'] for r in data['grants']}
    for collection in ('memberships', 'eligibilities', 'grants', 'programme_memberships', 'rotations'):
        assert all(r['person_id'] == facts['person'] for r in data[collection])
    forbidden = {'private_reason', 'note', 'description', 'evidence', 'granted_by', 'staff_number', 'tags', 'actor_id', 'identity_id'}
    assert all(not forbidden.intersection(row) for rows in data.values() for row in rows)
    assert 'PRIVATE' not in json.dumps(result)
    # There is no arbitrary-person selector; a caller cannot widen the bound self profile.
    assert h.get('staff', '/api/me/profile?person_id=' + next(iter(facts['peers']))) == result
    h.get('staff', '/api/catalogues', status=403)


@pytest.mark.parametrize('role', ['coordinator', 'approver', 'credentialer', 'auditor', 'operator', 'programme_lead'])
def test_non_staff_role_does_not_gain_profile_access(h, role):
    who = 'only_' + role
    identity(h, who, [role], person=h.seed['people'][0])
    h.get(who, '/api/me/profile', status=403)


def test_unlinked_staff_and_anonymous_have_no_profile(h):
    identity(h, 'unlinked', ['staff'])
    assert h.get('unlinked', '/api/me/profile', status=403)['error']['code'] == 'PERSON_REQUIRED'
    with TestClient(h.app, base_url=h.settings.public_origin) as client:
        assert client.get('/api/me/profile').status_code == 401


def test_foreign_organisation_person_binding_is_not_read(h):
    with transaction(h.app.state.engine, write=True) as db:
        org = m.Organisation(name='Other organisation')
        db.add(org); db.flush()
        person = m.Person(organisation_id=org.id, display_name='Foreign person')
        db.add(person); db.flush()
        pid = person.id
    identity(h, 'foreign_person', ['staff'], person=pid)
    h.get('foreign_person', '/api/me/profile', status=404)


def test_self_profile_limit_is_explicit_instead_of_silently_truncating(h, facts, monkeypatch):
    monkeypatch.setattr(self_profile, 'LIMIT', 1)
    assert h.get('staff', '/api/me/profile', status=409)['error']['code'] == 'PROFILE_LIMIT'
