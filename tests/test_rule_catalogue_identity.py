"""Catalogue proposals bind to the active version; today's empty reach does not erase future rules."""
from datetime import date
import uuid

import pytest
from sqlalchemy import func, select

from apps.api import models as m
from apps.api.db import transaction
from .test_rule_catalogue_api import approve, entry, line, person, propose


@pytest.fixture(params=['sqlite', pytest.param('postgres', marks=pytest.mark.postgres)])
def catalogue_db(request):
    return request.getfixturevalue('h' if request.param == 'sqlite' else 'pg')


def persisted_state(h):
    with transaction(h.app.state.engine) as db:
        counts = {cls.__tablename__: db.scalar(select(func.count()).select_from(cls)) for cls in (
            m.Policy, m.DefinitionHistory, m.AuditEvent, m.CommandReceipt, m.RuleAcknowledgement, m.Outbox)}
        org = db.get(m.Organisation, h.seed['organisation_id'])
        return counts, org.input_revision, org.availability_revision


def test_equal_revisions_on_replacement_policies_cannot_accept_an_old_draft(catalogue_db):
    h = catalogue_db
    everyone = line(numbers={'max': {'value': 5}})
    # A first version has neither an active ID nor a revision to replace.
    refused = propose(h, 'duties_per_week', [everyone], policy_id=str(uuid.uuid4()), status=409)
    assert refused['error']['code'] == 'STALE_REVISION'
    first = approve(h, propose(h, 'duties_per_week', [everyone])['record'])
    replacement = {**everyone, 'numbers': {'max': {'value': 6}}}
    second = approve(h, propose(h, 'duties_per_week', [replacement], policy_id=first['id'], revision=first['revision'])['record'])
    assert first['id'] != second['id'] and first['revision'] == second['revision'] == 2
    current = entry(h, 'duties_per_week')
    before = persisted_state(h)
    old_draft = {**everyone, 'numbers': {'max': {'value': 7}}}
    for policy_id, revision in (
        (first['id'], first['revision']),  # the stale draft that revision alone would accept
        (second['id'], second['revision'] + 1),
        (None, second['revision']),
        (second['id'], None),
        (None, None),
    ):
        refused = propose(h, 'duties_per_week', [old_draft], policy_id=policy_id, revision=revision, status=409)
        assert refused['error']['code'] == 'STALE_REVISION'
        assert persisted_state(h) == before and entry(h, 'duties_per_week') == current
    # Older clients omitting the new field also fail closed against an active version.
    refused = h.command('coordinator', 'rule.propose', {
        'catalogue_id': 'duties_per_week', 'expected_revision': second['revision'],
        'lines': [old_draft], 'reason': 'A draft from a client without the active policy ID'}, status=409)
    assert refused['error']['code'] == 'STALE_REVISION' and persisted_state(h) == before
    waiting = propose(h, 'duties_per_week', [old_draft], policy_id=second['id'], revision=second['revision'])['record']
    assert waiting['definition']['merged_ids'] == [second['id']]
    refused = propose(h, 'duties_per_week', [old_draft], policy_id=second['id'], revision=second['revision'], status=409)
    assert refused['error']['code'] == 'PROPOSAL_WAITING'
    third = approve(h, waiting)
    assert entry(h, 'duties_per_week')['rule']['id'] == third['id']


def test_empty_today_person_rule_is_kept_for_a_future_membership(catalogue_db):
    h = catalogue_db
    pid = h.seed['people'][0]
    with transaction(h.app.state.engine, write=True) as db:
        memberships = list(db.scalars(select(m.Membership).where(
            m.Membership.department_id == h.seed['department_id'], m.Membership.person_id == pid)))
        assert memberships
        for membership in memberships:
            membership.starts_on, membership.ends_on = date(2026, 11, 1), None
    future_line = line(who=person(pid), numbers={'apply': {'value': None}})
    active = approve(h, propose(h, 'keep_off', [future_line])['record'])
    page = h.get('coordinator', '/api/rules/catalogue')
    [nobody] = [x for x in page['attention'] if x['rule_id'] == active['id'] and x['kind'] == 'nobody']
    assert nobody['facts'] == {'group': False}
    assert [(f['kind'], f['recommended']) for f in nobody['fixes']] == [('acknowledge', True), ('remove', False)]
    assert nobody['fixes'][0]['ops'] == []
    assert h.get('auditor', '/api/policies/reach?day=2026-10-31')['reach'][active['id']] == 0
    assert h.get('auditor', '/api/policies/reach?day=2026-11-01')['reach'][active['id']] == 1
    # Keep and its Undo leave the future restriction intact, including its enriched GET projection.
    current = entry(h, 'keep_off')['rule']
    assert {key: current[key] for key in ('id', 'revision', 'definition')} == {
        key: active[key] for key in ('id', 'revision', 'definition')}
    h.command('coordinator', 'rule.acknowledge', {'fingerprints': [nobody['fingerprint']], 'note': 'Joining next month'})
    assert entry(h, 'keep_off')['rule'] == current
    h.command('coordinator', 'rule.unacknowledge', {'fingerprints': [nobody['fingerprint']]})
    assert entry(h, 'keep_off')['rule'] == current
    assert h.get('auditor', '/api/policies/reach?day=2026-11-01')['reach'][active['id']] == 1
