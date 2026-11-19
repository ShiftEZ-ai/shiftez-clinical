"""Period reuse and availability repair exercise real configuration and validation."""
from datetime import date, timedelta

import pytest

from packages.domain.generation import generate


def test_repair_unpaired_duty_does_not_invent_supervision(h):
    w = h.shift(h.workspace())['workspace']
    pid = h.seed['people'][0]
    h.policy({'kind': 'max_duties', 'maximum': 1, 'window': 'week', 'group_by': 'supervisor'},
             scope={'person_id': pid})
    data = h.snapshot(w)
    data['generation_options'] = {'mode': 'rebalance'}
    result = generate(data)
    assert result['status'] in {'candidate_feasible', 'candidate_with_gaps'}
    assert any(r['person_id'] == pid for r in result['removals'])
    assert not any(f['code'] == 'SUPERVISOR_REQUIRED' for f in result['validation']['findings'])
    assert data['shifts'][0]['participants'][0]['person_id'] == pid


def test_repair_absence_is_reviewable_and_preserves_locks(h):
    w = h.shift(h.workspace())['workspace']
    pid = h.seed['people'][0]
    h.command('coordinator', 'availability.record', {'person_id': pid, 'kind': 'recorded_leave',
        'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-05T00:00:00+03:00', 'private_reason': 'Leave'})
    data = h.snapshot(w)
    data['generation_options'] = {'mode': 'rebalance'}
    result = generate(data)
    assert result['status'] == 'candidate_feasible'
    assert len(result['removals']) == 1 and result['removals'][0]['person_id'] == pid
    assert result['removals'][0]['service_names'] and result['removals'][0]['session_label']
    assert result['additions'] and all(r['participant']['person_id'] != pid for r in result['additions'])
    assert data['shifts'][0]['participants'][0]['person_id'] == pid
    data['shifts'][0]['locked'] = True
    kept = generate(data)
    assert kept['status'] == 'candidate_with_gaps' and not kept['removals']
    assert any(f['code'] == 'UNAVAILABLE' for f in kept['issues'])
    data['shifts'][0]['locked'] = False
    data['workspace']['base_publication_id'] = 'protected'
    kept = generate(data)
    assert kept['status'] == 'candidate_with_gaps' and not kept['removals']
    assert any(f['code'] == 'UNAVAILABLE' for f in kept['issues'])


def test_copy_any_equal_period_preserves_offset_and_no_publication(h):
    w = h.workspace(start='2026-10-04', end='2026-11-01')
    w = h.shift(w)['workspace'] # first Sunday
    payload = {'name': 'November copied rota', 'starts_on': '2026-11-01', 'ends_on': '2026-11-29',
        'source_workspace_id': w['id'], 'source_revision': w['revision'], 'copy_alignment': 'weekday'}
    result = h.command('coordinator', 'workspace.create', payload)
    assert result['setup'] == {'created': 1, 'skipped': 0}
    copy = h.detail(result['workspace'])['shifts'][0]
    assert copy['start'].startswith('2026-11-01T05:00')
    assert copy['participants'] == [] and not copy['locked']
    assert result['workspace']['status'] == 'draft' and result['workspace']['base_publication_id'] is None
    assert h.detail(w)['shifts'][0]['participants']
    payload['source_revision'] -= 1
    h.command('coordinator', 'workspace.create', payload, status=409)


def test_worker_applies_reviewed_repair_and_rechecks_rest(h):
    from apps.worker.main import process_one
    w = h.shift(h.workspace())['workspace']
    w = h.shift(w, start='2026-10-04T13:00:00+03:00', end='2026-10-04T17:00:00+03:00')['workspace']
    h.policy({'kind': 'min_rest', 'hours': 12})
    job = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation',
        'repair_unavailable': True, 'repair_conflicts': True,
        'generation_options': {'mode': 'rebalance'}}, revision=w['revision'])['job']
    process_one(h.app.state.engine)
    result = h.get('coordinator', '/api/jobs/'+job['id'])['job']['result']
    assert result['status'] == 'candidate_feasible'
    assert len(result['removals']) == len(result['additions']) == 1
    applied = h.command('coordinator', 'generation.apply', {'workspace_id': w['id'], 'job_id': job['id'],
        'reason': 'Reviewed automatic rest repair'}, revision=w['revision'])
    assert applied['validation']['valid']
    assert applied['workspace']['status'] == 'draft'
    assert h.get('coordinator', '/api/publications')['publications'] == []


def test_copy_rejects_only_different_period_lengths(h):
    w = h.workspace(start='2026-10-01', end='2026-11-01')
    w = h.shift(w, start='2026-10-31T08:00:00+03:00', end='2026-10-31T12:00:00+03:00')['workspace']
    before = h.get('coordinator', '/api/workspaces')['total']
    result = h.command('coordinator', 'workspace.create', {'name': 'Short month', 'starts_on': '2026-11-01',
        'ends_on': '2026-12-01', 'source_workspace_id': w['id'], 'source_revision': w['revision'], 'copy_alignment': 'date'},
        status=422)
    assert result['error']['code'] == 'PERIOD_LENGTH_MISMATCH'
    assert 'same number of days' in result['error']['message']
    assert h.get('coordinator', '/api/workspaces')['total'] == before
    assert h.detail(w)['shifts'][0]['start'].startswith('2026-10-31T05:00')


@pytest.mark.parametrize('alignment', ['date', 'weekday'])
def test_copy_period_crosses_months_and_preserves_overnight_end_boundary(h, alignment):
    w = h.workspace(start='2026-10-29', end='2026-11-02')
    for day in ('2026-10-29', '2026-11-01'):
        tomorrow = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
        w = h.shift(w, start=day+'T20:00:00+03:00', end=tomorrow+'T00:00:00+03:00')['workspace']
    before = h.detail(w)['shifts']
    result = h.command('coordinator', 'workspace.create', {'name': 'Offset period', 'starts_on': '2026-11-03',
        'ends_on': '2026-11-07', 'source_workspace_id': w['id'], 'source_revision': w['revision'],
        'copy_alignment': alignment})
    assert result['setup'] == {'created': 2, 'skipped': 0}
    copied = h.detail(result['workspace'])['shifts']
    assert [s['start'][:10] for s in copied] == ['2026-11-03', '2026-11-06']
    assert copied[-1]['end'].startswith('2026-11-06T21:00')
    assert h.detail(w)['shifts'] == before


@pytest.mark.parametrize('source_day,target_day,status', [
    ('2026-10-24', '2026-10-31', 200),
    ('2026-10-31', '2026-10-24', 200),
    ('2026-10-17', '2026-10-24', 422),
])
def test_copy_segmented_overnight_period_preserves_local_clocks_across_dst(h, source_day, target_day, status):
    from packages.domain.time import local_stamp
    me = h.get('admin', '/api/me')
    org, dept = me['organisation'], me['department']
    h.command('admin', 'department.settings', {'department_name': dept['name'], 'department_revision': dept['revision'],
        'organisation_name': org['name'], 'timezone': 'Europe/London', 'week_start': org['week_start'],
        'reason': 'Synthetic timezone copy acceptance'}, dept['id'], org['revision'])
    first = date.fromisoformat(source_day)
    following = first + timedelta(days=1)
    instant = lambda day, clock: local_stamp(day, clock, 'Europe/London').isoformat()
    start, end = instant(first, '22:00'), instant(following, '04:00')
    participant = {'person_id': h.seed['people'][0], 'start': start, 'end': end, 'segments': [
        {'start': start, 'end': instant(following, '00:00')},
        {'start': instant(following, '01:30' if status == 422 else '02:00'), 'end': end}]}
    w = h.shift(h.workspace(start=source_day, end=(first + timedelta(days=2)).isoformat()),
                start=start, end=end, people=[participant])['workspace']
    before = h.detail(w)['shifts']
    total = h.get('coordinator', '/api/workspaces')['total']
    target = date.fromisoformat(target_day)
    result = h.command('coordinator', 'workspace.create', {'name': 'Segmented offset copy',
        'starts_on': target_day, 'ends_on': (target + timedelta(days=2)).isoformat(),
        'source_workspace_id': w['id'], 'source_revision': w['revision'], 'copy_people': True}, status=status)
    assert h.detail(w)['shifts'] == before
    if status == 422:
        assert result['error']['code'] == 'VALUE_INVALID'
        assert 'Ambiguous' in result['error']['message']
        assert h.get('coordinator', '/api/workspaces')['total'] == total
    else:
        copied = h.detail(result['workspace'])['shifts'][0]
        tomorrow = target + timedelta(days=1)
        assert (copied['start'], copied['end']) == (instant(target, '22:00'), instant(tomorrow, '04:00'))
        assert copied['participants'][0]['segments'] == [
            {'start': instant(target, '22:00'), 'end': instant(tomorrow, '00:00')},
            {'start': instant(tomorrow, '02:00'), 'end': instant(tomorrow, '04:00')}]


def test_workspace_creation_materialises_templates(h):
    from .test_series_reconciliation import template
    series = template(h)
    result = h.command('coordinator', 'workspace.create', {'name': 'From recurring template',
        'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'series_ids': [series['id']], 'generate_assignments': True})
    assert result['setup']['created'] == 12
    assert all(s['series_id'] == series['id'] for s in h.detail(result['workspace'])['shifts'])

    jobs = h.get('coordinator', '/api/jobs?workspace_id='+result['workspace']['id'])['jobs']
    assert len(jobs) == 1 and jobs[0]['status'] == 'queued'


def test_repair_can_move_spare_cover_but_never_create_another_gap(h):
    first, second = h.seed['people'][:2]
    w = h.shift(h.workspace(), people=[first, second])['workspace']
    w = h.shift(w, people=[], service=1, location=1)['workspace']
    data = h.snapshot(w)
    data['people'] = {k:v for k,v in data['people'].items() if k in {first,second}}
    data['eligibilities'] = [e for e in data['eligibilities'] if e['service_id'] != h.seed['services'][1] or e['person_id'] == first]
    kept = generate({**data, 'generation_options': {'mode': 'fill_gaps'}})
    assert kept['status'] == 'candidate_with_gaps' and not kept['removals']
    data['generation_options'] = {'mode': 'rebalance'}
    result = generate(data)
    assert result['status'] == 'candidate_feasible'
    assert len(result['removals']) == len(result['additions']) == 1
    assert result['additions'][0]['participant']['person_id'] == first
    for shift in data['shifts']:
        if shift['participants']: shift['locked'] = True
    assert generate(data)['status'] == 'candidate_with_gaps'
# Include this file in hosted supervision and team-lead qualification.
