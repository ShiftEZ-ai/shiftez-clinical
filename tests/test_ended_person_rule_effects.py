"""Only a wholly ended, single-person change with no saved placements can omit checks."""
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.worker.main import process_one
from packages.domain import feasibility
from .test_contextual_feasibility import use_checked_activation
from .test_rule_catalogue_api import approve, line, person, propose


@pytest.fixture(params=['h', pytest.param('pg', marks=pytest.mark.postgres)])
def backend(request):
    return request.getfixturevalue(request.param)


def removal(h, entry='protected_time', *, end='2026-11-30', case=None):
    pid = h.seed['people'][0]
    numbers = {'apply': {'value': None}} if entry == 'protected_time' else {'max': {'value': 5}}
    who = person(pid)
    if case == 'multiple_people':
        who['conditions'][0]['values'].append(h.seed['people'][1])
    removed = line(who=who, numbers=numbers, starts_on='2026-11-01' if case == 'earlier_span' else '2026-12-01',
                   ends_on='2027-01-01')
    lines = [removed]
    if case == 'all_three':
        lines = [line(who=person(person_id), numbers=numbers, starts_on='2026-12-01', ends_on='2027-01-01')
                 for person_id in h.seed['people']]
    if case == 'mixed_active':
        lines.append(line(who=person(h.seed['people'][1]), numbers=numbers,
                          starts_on='2026-12-01', ends_on='2027-01-01'))
    current = approve(h, propose(h, entry, lines)['record'])
    workspace = h.workspace('December effect', '2026-12-01', '2027-01-01')
    if case == 'saved_assignment':
        workspace = h.shift(workspace, start='2026-12-06T08:00:00+03:00',
                            end='2026-12-06T12:00:00+03:00', people=[])['workspace']
    with transaction(h.app.state.engine, write=True) as db:
        ended = set(h.seed['people']) if case == 'all_three' else {pid, h.seed['people'][1]} if case == 'multiple_people' else {pid}
        for row in db.scalars(select(m.Membership).where(m.Membership.person_id.in_(ended))):
            row.ends_on = date.fromisoformat(end) if end else None
        if case == 'future_membership':
            db.add(m.Membership(department_id=h.seed['department_id'], person_id=pid,
                category_id=h.seed['category'], starts_on=date(2026, 12, 15), ends_on=date(2027, 1, 15)))
        if case == 'saved_assignment':
            shift = db.scalar(select(m.Shift).where(m.Shift.workspace_id == workspace['id']))
            db.add(m.Assignment(shift_id=shift.id, person_id=pid, start=shift.start, end=shift.end,
                                participation='staff'))
    pending = propose(h, entry, [], policy_id=current['id'], revision=current['revision'])['record']
    return pending, h.detail(workspace)['workspace']


@pytest.mark.parametrize('entry', ['protected_time', 'duties_per_week'])
@pytest.mark.parametrize('end', ['2026-11-30', '2026-12-01'])
@pytest.mark.parametrize('count', [1, 3])
def test_removing_wholly_ended_person_line_needs_no_check_including_safety(backend, monkeypatch, entry, end, count):
    h = backend
    pending, _ = removal(h, entry, end=end, case='all_three' if count == 3 else None)
    use_checked_activation(h)
    h.clients.clear()
    monkeypatch.setattr(feasibility, 'check', lambda *a, **kw: pytest.fail('An empty span needs no solve'))
    review = h.get('approver', f"/api/policies/{pending['id']}/review")
    assert review['effect'] == [] and review['ready'] and not review['requires_acknowledgement']
    if count == 1:
        person = next(row for row in h.get('approver', '/api/catalogues?include=people')['people']
                      if row['id'] == h.seed['people'][0])
        assert review['summary'] == (f"No check needed: {person['display_name']} left on "
            f"{date.fromisoformat(end) - timedelta(days=1)} and has no later placements.")
    else:
        assert review['summary'] == 'No check needed: 3 people left before these lines start and have no later placements.'
    assert approve(h, pending)['status'] == 'active'


@pytest.mark.parametrize('case,end', [('active', '2027-01-01'), ('unknown_end', None),
    ('future_membership', '2026-11-30'), ('mixed_active', '2026-11-30'),
    ('multiple_people', '2026-11-30'), ('earlier_span', '2026-11-30'), ('saved_assignment', '2026-11-30')])
def test_uncertain_or_nonempty_person_spans_keep_the_affected_rota(backend, case, end):
    h = backend
    pending, workspace = removal(h, end=end, case=case)
    review = h.get('approver', f"/api/policies/{pending['id']}/review")
    assert [row['workspace_id'] for row in review['effect']] == [workspace['id']]
    assert not review['ready']
    assert 'summary' not in review


def test_skip_reason_uses_the_shared_person_visibility_projection(backend, monkeypatch):
    from apps.api.services import people
    h = backend
    pending, _ = removal(h)
    visible_person = people.visible_person
    def redacted(actor, person, contacts=()):
        return {**visible_person(actor, person, contacts), 'display_name': 'Former staff member'}
    monkeypatch.setattr(people, 'visible_person', redacted)
    review = h.get('approver', f"/api/policies/{pending['id']}/review")
    assert review['summary'] == 'No check needed: Former staff member left on 2026-11-29 and has no later placements.'


def test_unchanged_lines_keep_the_existing_empty_review_without_a_skip_reason(backend):
    h = backend
    pending, _ = removal(h)
    with transaction(h.app.state.engine, write=True) as db:
        policy = db.get(m.Policy, pending['id'])
        previous = db.get(m.Policy, policy.definition['merged_ids'][0])
        policy.definition = {**policy.definition, 'rule': previous.definition['rule']}
    review = h.get('approver', f"/api/policies/{pending['id']}/review")
    assert review['effect'] == [] and review['ready'] and review['can_decide']
    assert not review['requires_acknowledgement'] and 'summary' not in review


def test_active_person_line_still_runs_the_feasibility_check(backend, monkeypatch):
    h = backend
    pending, workspace = removal(h, end='2027-01-01')
    calls, check = [], feasibility.check
    def checked(snapshot, **kwargs):
        calls.append(snapshot['bindings'][0]['policy_id'])
        return check(snapshot, **kwargs)
    monkeypatch.setattr(feasibility, 'check', checked)
    job = h.command('approver', 'job.create', {'kind': 'feasibility', 'workspace_id': workspace['id'],
        'rule_changes': [{'policy_id': pending['id'], 'expected_revision': pending['revision']}]},
        revision=workspace['revision'])['job']
    assert process_one(h.app.state.engine)
    assert calls == [pending['id']]
    assert h.get('approver', f"/api/jobs/{job['id']}")['job']['status'] == 'succeeded'
