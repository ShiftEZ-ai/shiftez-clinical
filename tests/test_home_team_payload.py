"""Home uses the calendar's recorded pairs, dated labels and full-session presence."""
import json
import subprocess

import pytest

from packages.domain.supervision_teams import team_facts, team_order, with_duty_ranks


def supervision_type(h):
    return h.configure('assignment_types', {'name': 'Synthetic team duty', 'starts_on': '2020-01-01',
        'definition': {'supervision': {'trainee_category_ids': [h.seed['trainee_category']],
            'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 2}}})['record']['id']


def participant(pid, supervisor=None, suffix=''):
    return {'id': pid + suffix, 'person_id': pid, 'supervisor_person_id': supervisor}


@pytest.mark.parametrize('rows', [
    [], [participant('a', 's'), participant('s'), participant('b', 's')],
    [participant('a', 'gone'), participant('b', 'b')],
    [participant('a', 'b'), participant('b', 'a')],
    [participant('a', 's'), participant('s'), participant('s', suffix='-later'), participant('a', 's', '-later')],
    [participant('a', 'b'), participant('b', 'c'), participant('c')],
])
def test_backend_order_matches_planner_for_recorded_edge_cases(client_modules, rows):
    script = ("globalThis.document={cookie:''};globalThis.matchMedia=()=>({matches:false,addEventListener(){}});"
              f"const m=await import({json.dumps((client_modules/'planner_teams.js').as_uri())});"
              "console.log(JSON.stringify(m.teamOrder(JSON.parse(process.argv[1]))));")
    expected = json.loads(subprocess.check_output(['node', '--input-type=module', '-e', script, json.dumps(rows)], text=True))
    assert team_order(rows) == expected


def test_hidden_supervisor_is_not_missing_and_duplicate_trainees_are_counted_once():
    trainee, supervisor = participant('a', 's'), participant('s')
    assert team_order([trainee], [trainee, supervisor])[0]['missing'] is False
    assert team_order([trainee])[0]['missing'] is True
    facts = team_facts([trainee, supervisor, participant('a', 's', '-later')])
    assert facts['s']['trainee_ids'] == ['a']
    assert not facts['a']['supervisor_not_on_session']


def test_rank_uses_local_session_day_and_half_open_membership_dates_without_mutation():
    data = {'organisation': {'timezone': 'Asia/Riyadh'}, 'categories': {
        'junior': {'name': 'Synthetic junior', 'short_code': 'R1'},
        'senior': {'name': 'Synthetic senior', 'short_code': ''}}, 'memberships': [
        {'person_id': 'a', 'category_id': 'junior', 'starts_on': '2026-10-01', 'ends_on': '2026-10-05'},
        {'person_id': 'a', 'category_id': 'senior', 'starts_on': '2026-10-05', 'ends_on': None}]}
    shifts = [{'start': day, 'participants': [participant('a'), participant('absent')]}
              for day in ('2026-10-04T20:00:00Z', '2026-10-04T21:00:00Z', '2026-09-30T08:00:00Z')]
    result = with_duty_ranks(data, shifts)
    assert [s['participants'][0]['rank'] for s in result] == ['R1', 'Synthetic senior', '']
    assert all(s['participants'][1]['rank'] == '' for s in result)
    assert all('rank' not in p for s in shifts for p in s['participants'])
    assert with_duty_ranks({**data, 'memberships': []}, result) == result


@pytest.mark.postgres
@pytest.mark.parametrize('present', [True, False])
def test_home_calendar_shows_paired_rank_and_supervisor_presence(pg, present):
    h = pg
    me, supervisor, unrelated = h.seed['people']
    start, end = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'
    rows = [{'person_id': me, 'start': start, 'end': end, 'participation': 'trainee', 'supervisor_person_id': supervisor},
            {'person_id': unrelated, 'start': start, 'end': end}]
    if present:
        rows.append({'person_id': supervisor, 'start': start, 'end': end, 'participation': 'supervisor'})
    w = h.shift(h.workspace(), typ=supervision_type(h), people=rows)['workspace']
    path = '/api/calendar?start=2026-10-04&end=2026-10-05'
    if present:
        h.publish(w)
        calendar = h.get('staff', path)
    else:
        # Missing supervisors block publication; the draft uses the same calendar projection.
        calendar = h.get('coordinator', path + f'&workspace_id={w["id"]}&person_id={me}')
    duty, = [e for e in calendar['events'] if e['kind'] == 'duty']
    rank = next(c['name'] for c in h.get('admin', '/api/catalogues')['categories'] if c['id'] == h.seed['category'])
    assert duty['rank'] == rank
    assert duty['pair_ranks'] == {pid: rank for pid in ([me, supervisor] if present else [me])}
    assert duty['supervisor_not_on_session'] is (not present)
    assert duty['supervisor_person_id'] == supervisor
    assert unrelated not in calendar['pair_names']
    assert unrelated not in duty['pair_ranks']
# Include this file in hosted supervision and team-lead qualification.
