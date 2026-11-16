"""Grouped issues keep every hard constraint in staffing decisions and stored count projections."""
import copy

import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services.staffing import check_people
from packages.domain.candidate_evidence import candidate_evidence
from packages.domain.solver_summary import summarise
from packages.domain.validation import evaluate, finding_observations
from .test_rule_catalogue_api import approve, line, person, propose


@pytest.mark.parametrize('maximum', [1, 2])
@pytest.mark.postgres
def test_person_score_checks_secondary_session_limit_even_when_personal_pick_is_allowed(pg, maximum):
    h = pg
    kept, candidate = h.seed['people'][:2]
    clinics = h.seed['services'][:2]
    policy = approve(h, propose(h, 'people_per_session', [
        line(who=person(kept), where={'conditions': [{'field': 'service_ids', 'values': clinics[:1]}]},
             numbers={'max': {'value': 0}}),
        line(where={'conditions': [{'field': 'service_ids', 'values': clinics[1:]}]},
             numbers={'max': {'value': maximum}})])['record'])
    typ = h.configure('assignment_types', {'name': 'Shared staffing guard fixture', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 2}], 'allow_shared_services': True,
                       'shared_duty_reason': 'Synthetic shared clinic duty'}})['record']
    w = h.workspace()
    start, end = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'
    w = h.command('coordinator', 'shift.create', {'workspace_id': w['id'], 'assignment_type_id': typ['id'],
        'location_id': h.seed['locations'][0], 'service_ids': clinics, 'start': start, 'end': end,
        'participants': [{'person_id': kept, 'start': start, 'end': end, 'fixed': True}]}, revision=w['revision'])['workspace']
    source = h.snapshot(w)
    [shift] = source['shifts']
    baseline = evaluate(source)
    [primary] = [f for f in baseline['findings'] if f['rule_id'] == policy['id']]
    picks, slot = check_people(source, shift, None, {candidate})
    assert picks[0]['status'] == ('blocked' if maximum == 1 else 'ok'), \
        'Direct staffing also blocks a newly breached secondary session limit'
    trial = {**source, 'shifts': [dict(shift, participants=[*shift['participants'],
        {**slot, 'id': 'synthetic-trial', 'shift_id': shift['id'], 'person_id': candidate}])]}
    after = evaluate(trial)
    [visible] = [f for f in after['findings'] if f['rule_id'] == policy['id']]
    assert visible['detail']['line'] == primary['detail']['line'] == 0
    assert len([f for f in finding_observations(after) if f['rule_id'] == policy['id']]) == (2 if maximum == 1 else 1)
    saved = h.detail(w)
    result = h.get('coordinator', f"/api/people/{candidate}/score?open_limit=20&workspace_id={w['id']}")['draft']
    assert [row['shift_id'] for row in result['open_places']] == ([] if maximum == 1 else [shift['id']])
    assert h.detail(w) == saved, 'Opportunity reads never change the rota'


def quota_world(h, unsafe, buckets):
    pid = h.seed['people'][0]
    clinics = h.seed['services'][:2]
    if buckets:
        policy = h.policy({'kind': 'block_quota', 'metric': 'duties', 'comparison': 'maximum',
            'amount': 2 if unsafe else 3, 'starts_on': '2026-10-04', 'ends_on': '2026-10-11'},
            scope={'person_id': pid, 'conditions': {'match': 'all', 'conditions': [
                {'field': 'service_ids', 'op': 'any', 'values': clinics, 'count': 'each'}]}})
        services = [0, 0, 0, 0, 1, 1]
    else:
        dates = {'starts_on': '2026-10-04', 'ends_on': '2026-10-11', 'window_alignment': 'calendar_week'}
        policy = approve(h, propose(h, 'duties_per_week', [
            line(who=person(pid), where={'conditions': [{'field': 'service_ids', 'values': [clinic]}]},
                 numbers={'max': {'value': maximum}}, settings={'block_quota': dates})
            for clinic, maximum in zip(clinics, (0, 1 if unsafe else 2))])['record'])
        services = [0, 0, 1]
    w = h.workspace()
    for day, service in enumerate(services, 4):
        w = h.shift(w, service=service, start=f'2026-10-{day:02}T08:00:00+03:00',
                    end=f'2026-10-{day:02}T12:00:00+03:00', people=[pid])['workspace']
    day = 4 + len(services)
    created = h.shift(w, service=1, start=f'2026-10-{day:02}T08:00:00+03:00',
                      end=f'2026-10-{day:02}T12:00:00+03:00', people=[])
    source = h.snapshot(created['workspace'])
    shift = next(s for s in source['shifts'] if s['id'] == created['shift_id'])
    return created['workspace'], source, shift, pid, policy


@pytest.mark.parametrize('buckets', [False, True], ids=['independent-lines', 'count-each'])
@pytest.mark.parametrize('unsafe', [False, True], ids=['safe', 'new-hard-breach'])
@pytest.mark.postgres
def test_staffing_and_run_reasons_check_secondary_quota_identity(pg, unsafe, buckets):
    h = pg
    w, source, shift, pid, policy = quota_world(h, unsafe, buckets)
    baseline = evaluate(source)
    [primary] = [f for f in baseline['findings'] if f['rule_id'] == policy['id']]
    participant = {'id': 'synthetic-trial', 'shift_id': shift['id'], 'person_id': pid,
                   'start': shift['start'], 'end': shift['end'], 'participation': 'staff', 'overtime': False}
    trial = {**source, 'shifts': [dict(s, participants=[*s['participants'], participant])
                               if s['id'] == shift['id'] else s for s in source['shifts']]}
    after = evaluate(trial)
    [visible] = [f for f in after['findings'] if f['rule_id'] == policy['id']]
    assert {k: v for k, v in visible['detail'].items() if k != 'derived_findings'} == primary['detail']
    assert len([f for f in finding_observations(after) if f['rule_id'] == policy['id']]) == (2 if unsafe else 1)
    saved = h.detail(w)
    response = h.client('coordinator').post('/api/planner/fill-options', json={
        'workspace_id': w['id'], 'expected_revision': w['revision'], 'shift_id': shift['id']})
    assert response.status_code == 200, response.text
    pick = next(r for r in response.json()['candidates'] if r['person_id'] == pid)
    assert pick['status'] == ('blocked' if unsafe else 'ok')
    if unsafe:
        assert any(f['code'] == 'QUOTA_ABOVE_MAXIMUM' for f in pick['findings'])
    evidence = [candidate_evidence(source, pid, shift)]
    summary = summarise(source, baseline, evidence, {shift['id']})
    [opportunity] = summary['open_places']
    [reported] = opportunity['people']
    assert (reported['kind'] == 'FREE') is not unsafe
    assert bool(opportunity['blocked_by']) is unsafe
    assert h.detail(w) == saved


@pytest.mark.postgres
def test_day_and_rule_map_project_old_duplicate_checks_without_rewriting_them(pg):
    h = pg
    clinics = h.seed['services'][:2]
    policy = h.policy({'kind': 'minimum_cover', 'minimum': 1}, scope={'conditions': {'match': 'all', 'conditions': [
        {'field': 'service_ids', 'op': 'any', 'values': clinics, 'count': 'each'}]}})
    typ = h.configure('assignment_types', {'name': 'Historic grouped count fixture', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1}], 'allow_shared_services': True,
                       'shared_duty_reason': 'Synthetic shared clinic duty'}})['record']
    w = h.workspace()
    created = h.command('coordinator', 'shift.create', {'workspace_id': w['id'], 'assignment_type_id': typ['id'],
        'location_id': h.seed['locations'][0], 'service_ids': clinics,
        'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00', 'participants': []}, revision=w['revision'])
    w, checked = created['workspace'], created['validation']
    old = copy.deepcopy(h.detail(w)['workspace']['validation'])
    old['findings'] = list(finding_observations(old))
    old['error_count'] = old['score']['blocking_count'] = len([f for f in old['findings'] if f['severity'] == 'error'])
    assert old['error_count'] == checked['error_count'] + 1
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Workspace, w['id']).validation = old
    mapped = h.get('coordinator', f"/api/workspaces/{w['id']}/rule-map")
    rule = next(r for r in mapped['rules'] if r['id'] == policy['id'])
    assert rule['status']['errors'] == 1 and rule['short_days'][0][1] == 1
    assert mapped['fresh']
    days = h.get('coordinator', '/api/schedule/days?start=2026-10-04&end=2026-10-11')
    [day] = [d for d in days['days']['2026-10-04'] if d['source'] == w['id']]
    assert day['errors'] == checked['error_count']
    with transaction(h.app.state.engine) as db:
        stored = db.get(m.Workspace, w['id'])
        assert stored.validation == old and stored.revision == w['revision']
