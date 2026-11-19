"""D65: existing blockers allow unrelated edits; a newly introduced clash refuses atomically."""
import json
import pytest
from .test_be18_command_latency import edit, worker_up

pytestmark = pytest.mark.postgres


def scenario(h, conflict=False):
    a, b = h.seed['people'][:2]
    w = h.workspace()
    w = h.shift(w, people=[a])['workspace']
    w = h.shift(w, people=[b], start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00')['workspace']
    w = h.shift(w, people=[b] if conflict else [], service=1)['workspace']
    rows = sorted(h.detail(w)['shifts'], key=lambda r: (r['start'], r['service_ids']))
    first = next(r for r in rows if r['participants'] and r['participants'][0]['person_id'] == a)
    second = next(r for r in rows if r['start'].startswith('2026-10-05'))
    return w, first, second


def swap_payload(w, first, second):
    return {'workspace_id': w['id'], 'first_assignment_id': first['participants'][0]['id'],
            'second_assignment_id': second['participants'][0]['id'], 'reason': 'Exchange duties'}


def test_exchange_with_unrelated_blockers_is_allowed(pg):
    w, first, second = scenario(pg)
    assert not pg.detail(w)['workspace']['validation']['valid']
    result = pg.command('coordinator', 'draft.swap', swap_payload(w, first, second), revision=w['revision'])
    assert result['workspace']['revision'] == w['revision'] + 1
    assert not result['validation']['valid']


@pytest.mark.parametrize('preview', [False, True])
def test_exchange_new_double_booking_names_clash_and_rolls_back(pg, preview):
    w, first, second = scenario(pg, conflict=True)
    before = pg.detail(w)
    payload = swap_payload(w, first, second)
    if preview:
        response = pg.client('coordinator').post('/api/planner/preview', json={
            'kind': 'draft.swap', 'payload': payload, 'expected_revision': w['revision']})
        assert response.status_code == 422
        result = response.json()
    else:
        result = pg.command('coordinator', 'draft.swap', payload, revision=w['revision'], status=422)
    assert result['error']['code'] == 'SWAP_INVALID'
    findings = result['error']['details']['findings']
    assert any(f['person_id'] == pg.seed['people'][1] and f.get('start') for f in findings)
    assert len(json.dumps(result)) < 10000
    assert pg.detail(w) == before


@pytest.mark.parametrize('worker', [False, True])
def test_edit_allows_existing_blockers_but_refuses_new_overlap(pg, worker):
    w, first, second = scenario(pg, conflict=True)
    if worker:
        worker_up(pg)
    result = edit(pg, w, first, [{**first['participants'][0], 'team_lead': True}])
    w = result['workspace']
    before = pg.detail(w)
    participant = {**first['participants'][0], 'person_id': pg.seed['people'][1]}
    failed = edit(pg, w, first, [participant], status=422)
    assert failed['error']['code'] == 'EDIT_INVALID'
    assert pg.detail(w) == before


def test_background_reply_can_remove_required_cover_in_draft(pg):
    w = pg.shift(pg.workspace())['workspace']
    worker_up(pg)
    result = edit(pg, w, pg.detail(w)['shifts'][0], [])
    assert any(row['code'] == 'COVERAGE_GAP' for row in result['validation']['findings'])
    assert pg.detail(w)['shifts'][0]['participants'] == []


def test_edit_can_worsen_person_minimum_but_keeps_must_fix_submission_gate(pg):
    person = pg.seed['people'][0]
    pg.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
               'metric': 'duties', 'comparison': 'minimum', 'amount': 2}, {'person_id': person})
    w = pg.shift(pg.workspace(), people=[person])['workspace']
    shift = pg.detail(w)['shifts'][0]

    result = edit(pg, w, shift, [])
    finding = next(f for f in result['validation']['findings'] if f['code'] == 'QUOTA_BELOW_MINIMUM')
    assert finding['severity'] == 'error'
    assert result['validation']['valid'] is False
    assert result['fix_list']['summary']['submit_blocked']
    assert any(cause['level'] == 'must' and finding['code'] in {
        row['code'] for row in cause.get('evidence', [])} for cause in result['fix_list']['causes'])
    assert pg.lifecycle(result['workspace'], 'submit', status=422)['error']['code'] == 'VALIDATION_FAILED'


@pytest.mark.parametrize('rule, code', [
    ({'kind': 'max_duties', 'maximum': 1, 'window': 'week'}, 'MAX_DUTIES'),
    ({'kind': 'min_rest', 'hours': 24}, 'MIN_REST'),
])
def test_edit_new_maximum_or_safety_finding_still_refuses(pg, rule, code):
    person = pg.seed['people'][0]
    pg.policy(rule, {'person_id': person})
    w = pg.shift(pg.workspace(), people=[person])['workspace']
    w = pg.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    shift = sorted(pg.detail(w)['shifts'], key=lambda row: row['start'])[-1]
    payload = {key: shift[key] for key in ('assignment_type_id', 'location_id', 'service_ids', 'start', 'end', 'attributes')}
    payload.update(workspace_id=w['id'], shift_id=shift['id'],
                   participants=[{'person_id': person, 'start': shift['start'], 'end': shift['end']}])

    refused = pg.command('coordinator', 'shift.update', payload, revision=w['revision'], status=422)
    assert refused['error']['code'] == 'EDIT_INVALID'
    assert any(f['code'] == code for f in refused['error']['details']['findings'])


def test_deepening_an_existing_overlap_is_refused(pg):
    a = pg.seed['people'][0]
    w = pg.shift(pg.workspace(), people=[a])['workspace']
    w = pg.shift(w, people=[a], service=1, end='2026-10-04T14:00:00+03:00')['workspace']
    before = pg.detail(w)
    shift = next(row for row in before['shifts'] if row['end'].endswith('09:00:00+00:00'))
    payload = {key: shift[key] for key in ('assignment_type_id', 'location_id', 'service_ids', 'start', 'attributes')}
    end = '2026-10-04T13:00:00+03:00'
    payload.update(workspace_id=w['id'], shift_id=shift['id'], end=end,
        participants=[{'person_id': a, 'start': shift['start'], 'end': end}])
    result = pg.command('coordinator', 'shift.update', payload, revision=w['revision'], status=422)
    assert result['error']['code'] == 'EDIT_INVALID'
    assert any(row['code'] == 'TIME_OVERLAP' for row in result['error']['details']['findings'])
    assert pg.detail(w) == before


def test_existing_absence_does_not_block_unrelated_location_edit(pg):
    w = pg.shift(pg.workspace())['workspace']
    shift = pg.detail(w)['shifts'][0]
    pg.command('coordinator', 'availability.record', {'person_id': pg.seed['people'][0],
        'start': shift['start'], 'end': shift['end'], 'kind': 'actual_absence',
        'private_reason': 'Existing absence'})
    payload = {key: shift[key] for key in ('assignment_type_id', 'service_ids', 'start', 'end', 'attributes')}
    payload.update(workspace_id=w['id'], shift_id=shift['id'], location_id=pg.seed['locations'][1],
                   participants=[{'person_id': pg.seed['people'][0], 'start': shift['start'], 'end': shift['end']}])
    result = pg.command('coordinator', 'shift.update', payload, revision=w['revision'])
    assert any(row['code'] == 'UNAVAILABLE' for row in result['validation']['findings'])
    assert pg.detail(w)['shifts'][0]['location_id'] == pg.seed['locations'][1]
