"""Team lead: duty-type settings, validator findings, solver and greedy choices, reports."""
from tests.historical_staffing import retained_staffing
import copy
from datetime import datetime

import pytest

from tests.legacy_lead import historical_type
from packages.domain import generation
from packages.domain.cp_sat import generate
from packages.domain.validation import evaluate
from packages.contracts import schemas as s

START, END = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'


def lead_type(h, lead=None, **definition):
    lead = {'minimum': 1, 'maximum': 1, 'capability_ids': [h.seed['supervisor_capability']], 'category_ids': []} if lead is None else lead
    return historical_type(h, {'name': 'Led duty', 'starts_on': '2020-01-01', 'definition': {
        'requirements': [{'minimum': 2, 'maximum': 2}], 'lead': lead, **definition}})['id']


def codes(data):
    return [f['code'] for f in evaluate(data)['findings'] if f['severity'] == 'error']


def only_lead_capable(data, person):
    """Leave the lead skill (supervision) with `person` only."""
    supervision = next(c for c in data['capabilities'].values() if c['name'] == 'Demo supervision')['id']
    data['grants'] = [g for g in data['grants'] if g['capability_id'] != supervision or g['person_id'] == person]
    return data


def _lead_fix(h, workspace):
    listed = h.get('coordinator', f"/api/workspaces/{workspace['id']}/fix-list")
    return next(cause for cause in listed['causes']
                if any(row['code'] == 'LEAD_GAP' for row in cause['evidence']))


def _shift_update(h, workspace, shift, participants):
    return h.command('coordinator', 'shift.update', {
        'workspace_id': workspace['id'], 'shift_id': shift['id'],
        **{key: copy.deepcopy(shift[key]) for key in (
            'assignment_type_id', 'location_id', 'service_ids', 'start', 'end', 'attributes')},
        'participants': [{key: copy.deepcopy(row[key]) for key in s.ParticipantInput.model_fields
                         if key in row} for row in participants]}, revision=workspace['revision'])


def test_full_lead_gap_fix_list_marks_an_present_qualified_person(h):
    a, b = h.seed['people'][:2]
    typ = lead_type(h, lead={'minimum': 1, 'maximum': 1,
                             'capability_ids': [h.seed['supervisor_capability']], 'category_ids': []})
    w = retained_staffing(h, h.workspace('Lead picker, present candidate'), typ=typ, people=[a, b])['workspace']
    shift = h.detail(w)['shifts'][0]
    cause = _lead_fix(h, w)
    assert cause['action']['kind'] == 'choose_cover'
    finding_key = cause['session_findings'][shift['id']]

    participants = copy.deepcopy(shift['participants'])
    next(row for row in participants if row['person_id'] == a)['team_lead'] = True
    saved = _shift_update(h, w, shift, participants)
    checked = h.detail(saved['workspace'])['workspace']['validation']
    assert not [row for row in checked['findings'] if row['code'] == 'LEAD_GAP']
    summary = h.get('coordinator', f"/api/workspaces/{saved['workspace']['id']}/summary")
    assert (summary['places_needed'], summary['places_filled'], summary['places_open']) == (2, 2, 0)
    assert finding_key


def test_locked_full_lead_gap_points_to_the_picker_and_every_write_is_refused(h):
    """A locked session keeps its "No team lead" cause and its way to the picker (opening it is not an edit);
    the writes behind the picker refuse the locked session."""
    a, b = h.seed['people'][:2]
    typ = lead_type(h)
    w = retained_staffing(h, h.workspace('Lead picker, locked session'), typ=typ, people=[a, b])['workspace']
    shift = h.detail(w)['shifts'][0]
    w = h.command('coordinator', 'shift.action', {'workspace_id': w['id'], 'shift_ids': [shift['id']],
                  'action': 'lock', 'reason': 'Keep this session locked'}, revision=w['revision'])['workspace']
    shift = h.detail(w)['shifts'][0]
    cause = _lead_fix(h, w)
    assert cause['action']['kind'] == 'choose_cover'
    options = h.client('coordinator').post('/api/planner/fill-options', json={
        'workspace_id': w['id'], 'shift_id': shift['id'], 'expected_revision': w['revision'],
        'replace_assignment_id': shift['participants'][0]['id'], 'finding_key': cause['session_findings'][shift['id']]})
    assert options.status_code == 409 and options.json()['error']['code'] == 'ASSIGNMENT_LOCKED', options.text
    participants = copy.deepcopy(shift['participants'])
    next(row for row in participants if row['person_id'] == a)['team_lead'] = True
    refused = h.command('coordinator', 'shift.update', {
        'workspace_id': w['id'], 'shift_id': shift['id'],
        **{key: copy.deepcopy(shift[key]) for key in (
            'assignment_type_id', 'location_id', 'service_ids', 'start', 'end', 'attributes')},
        'participants': [{key: copy.deepcopy(row[key]) for key in s.ParticipantInput.model_fields if key in row}
                         for row in participants]}, revision=w['revision'], status=409)
    assert refused['error']['code'] == 'ASSIGNMENT_LOCKED'
    assert [f['code'] for f in h.detail(w)['workspace']['validation']['findings'] if f['code'] == 'LEAD_GAP']


def test_full_lead_gap_fix_list_offers_qualified_replacement_and_clears_it(h):
    a, b, candidate = h.seed['people'][:3]
    typ = historical_type(h, {'name': 'Lead replacement duty', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 2, 'maximum': 2}],
                       'lead': {'minimum': 1, 'maximum': 1, 'capability_ids': [], 'category_ids': [],
                                 'conditions': {'match': 'all', 'conditions': [
                                     {'field': 'person_ids', 'op': 'any', 'values': [candidate]}]}}}})['id']
    w = retained_staffing(h, h.workspace('Lead picker, replacement candidate'), typ=typ, people=[a, b])['workspace']
    shift = h.detail(w)['shifts'][0]
    cause = _lead_fix(h, w)
    assert cause['action']['kind'] == 'choose_cover'
    finding_key = cause['session_findings'][shift['id']]
    replaced = shift['participants'][0]
    result = h.client('coordinator').post('/api/planner/fill-options', json={
        'workspace_id': w['id'], 'shift_id': shift['id'], 'expected_revision': w['revision'],
        'replace_assignment_id': replaced['id'], 'finding_key': finding_key})
    assert result.status_code == 200, result.text
    row = next(row for row in result.json()['candidates'] if row['person_id'] == candidate)
    assert row['can_lead'] and row['fills']
    participants = [copy.deepcopy(item) for item in shift['participants'] if item['id'] != replaced['id']]
    participants.append({**copy.deepcopy(result.json()['slot']), 'person_id': candidate, 'team_lead': True})
    saved = _shift_update(h, w, shift, participants)
    checked = h.detail(saved['workspace'])['workspace']['validation']
    assert not [finding for finding in checked['findings'] if finding['code'] == 'LEAD_GAP']
    summary = h.get('coordinator', f"/api/workspaces/{saved['workspace']['id']}/summary")
    assert (summary['places_needed'], summary['places_filled'], summary['places_open']) == (2, 2, 0)


def test_a_lead_on_a_duty_type_without_a_lead_setting_is_flagged(h):
    a = h.seed['people'][0]
    w = h.shift(h.workspace(), people=[{'person_id': a, 'start': START, 'end': END, 'team_lead': True}])['workspace']
    findings = [f for f in h.lifecycle(w, 'validate')['workspace']['validation']['findings'] if f['code'] == 'LEAD_NOT_QUALIFIED']
    assert [(f['person_id'], f['severity']) for f in findings] == [(a, 'warning')]


def test_lead_settings_are_checked_and_findings_explain_gaps(h):
    a, b, c = h.seed['people']
    bad = {'minimum': 2, 'maximum': 1, 'capability_ids': [], 'category_ids': []}
    h.configure('assignment_types', {'name': 'Bad lead', 'starts_on': '2020-01-01',
                'definition': {'lead': bad}}, status=422)
    h.configure('assignment_types', {'name': 'Protected lead', 'starts_on': '2020-01-01', 'definition': {
        'purpose': 'protected', 'requirements': [], 'lead': {'minimum': 1, 'maximum': 1}}}, status=422)
    typ = lead_type(h)
    result = retained_staffing(h, h.workspace(), typ=typ, people=[a, b])
    gap = [f for f in result['validation']['findings'] if f['code'] == 'LEAD_GAP']
    assert gap and gap[0]['severity'] == 'error' and gap[0]['message'] == 'This session needs a team lead'
    data = h.snapshot(result['workspace'])
    for p in data['shifts'][0]['participants']:
        p['team_lead'] = True
    assert 'LEAD_GAP' in codes(data)  # too many
    data['shifts'][0]['participants'][1]['team_lead'] = False
    assert codes(data) == []
    lead = data['shifts'][0]['participants'][0]['person_id']
    unqualified = only_lead_capable(copy.deepcopy(data), c)
    assert {'LEAD_NOT_QUALIFIED', 'LEAD_GAP'} <= set(codes(unqualified))
    # A dated skill: the lead's supervision ends at midnight during an overnight duty.
    night = copy.deepcopy(data)
    shift = night['shifts'][0]
    shift['start'] = '2026-10-04T20:00:00+03:00'
    shift['end'] = '2026-10-05T04:00:00+03:00'
    for p in shift['participants']:
        p['start'], p['end'] = shift['start'], shift['end']
    assert 'LEAD_NOT_QUALIFIED' not in codes(night)
    for g in night['grants']:
        if g['person_id'] == lead:
            g['ends_on'] = '2026-10-05'
    found = [f for f in evaluate(night)['findings'] if f['code'] == 'LEAD_NOT_QUALIFIED']
    assert found and datetime.fromisoformat(found[0]['start']) == datetime.fromisoformat('2026-10-05T00:00:00+03:00')


def test_solver_picks_one_qualified_lead_and_keeps_existing_leads(h):
    a, b, c = h.seed['people']
    typ = lead_type(h)
    data = only_lead_capable(h.snapshot(h.shift(h.workspace(), typ=typ, people=[])['workspace']), c)
    result = generate(data, 10)
    assert result['status'] == 'candidate_feasible', result
    leads = [r['participant']['person_id'] for r in result['additions'] if r['participant']['team_lead']]
    assert leads == [c] and len(result['additions']) == 2
    # An existing valid lead is kept: nothing to change.
    w = h.shift(h.workspace('Kept lead'), typ=typ, people=[{'person_id': a, 'start': START, 'end': END},
                {'person_id': b, 'start': START, 'end': END, 'team_lead': True}])['workspace']
    kept = generate(h.snapshot(w), 10)
    assert kept['status'] == 'candidate_feasible' and kept['additions'] == kept['removals'] == []
    # Fill-gaps keeps everyone in place but may still name one of them lead; a fixed person is never changed.
    w = retained_staffing(h, h.workspace('Fill lead'), typ=typ, people=[a, {'person_id': b, 'start': START, 'end': END, 'fixed': True}])['workspace']
    data = h.snapshot(w)
    data['generation_options'] = {'mode': 'fill_gaps'}
    filled = generate(data, 10)
    assert filled['status'] == 'candidate_feasible', filled
    assert [(r['participant']['person_id'], r['participant']['team_lead']) for r in filled['additions']] == [(a, True)]
    assert [r['person_id'] for r in filled['removals']] == [a]


def test_solver_keeps_lead_flag_when_a_person_is_rewritten(h):
    """Regression: a changed assignment (here overtime cleared) used to lose team_lead."""
    a = h.seed['people'][0]
    typ = lead_type(h, requirements=[{'minimum': 1, 'maximum': 1}])
    w = h.shift(h.workspace(), typ=typ, people=[{'person_id': a, 'start': START, 'end': END, 'overtime': True, 'team_lead': True}])['workspace']
    data = h.snapshot(w)
    data['generation_options'] = {'mode': 'rebalance'}
    result = generate(data, 10)
    assert result['status'] == 'candidate_feasible', result
    assert [(r['participant']['overtime'], r['participant']['team_lead']) for r in result['additions']] == [(False, True)]


def test_greedy_names_a_lead_among_assigned_people(h):
    a, b, c = h.seed['people']
    typ = lead_type(h)
    w = retained_staffing(h, h.workspace(), typ=typ, people=[a, b])['workspace']
    result = generation.generate(h.snapshot(w), 20)
    assert result['validation']['valid'], result['validation']['findings']
    assert [r['participant']['team_lead'] for r in result['additions']] == [True]
    assert len(result['removals']) == 1
    # A session with nobody yet: the greedy path adds a qualified lead.
    empty = only_lead_capable(h.snapshot(h.shift(h.workspace('Empty'), typ=typ, people=[])['workspace']), c)
    result = generation.generate(empty, 20)
    assert result['validation']['valid'], result['validation']['findings']
    assert [r['participant']['person_id'] for r in result['additions'] if r['participant'].get('team_lead')] == [c]


def test_lead_turns_report_quota_and_fill_options(h):
    a, b, c = h.seed['people']
    typ = lead_type(h)
    quota = h.policy({'kind': 'block_quota', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01',
                      'metric': 'lead_turns', 'comparison': 'maximum', 'amount': 0}, {'person_id': a})
    result = h.shift(h.workspace(), typ=typ, people=[{'person_id': a, 'start': START, 'end': END, 'team_lead': True},
                                                      {'person_id': b, 'start': START, 'end': END}])
    over = [f for f in result['validation']['findings'] if f['code'] == 'QUOTA_ABOVE_MAXIMUM']
    assert over and over[0]['rule_id'] == quota['id'] and over[0]['person_id'] == a
    # The solver moves the lead turn to the other person rather than break the quota.
    data = h.snapshot(result['workspace'])
    data['generation_options'] = {'mode': 'rebalance'}
    moved = generate(data, 10)
    assert moved['status'] == 'candidate_feasible', moved
    assert {r['participant']['person_id'] for r in moved['additions'] if r['participant']['team_lead']} <= {b, c}
    w = h.shift(h.workspace('Reported'), typ=typ, people=[{'person_id': b, 'start': START, 'end': END, 'team_lead': True},
                                                          {'person_id': c, 'start': START, 'end': END}])['workspace']
    published = h.publish(w)
    report = h.get('coordinator', '/api/reports/' + published['publication_id'])
    assert {r['person_id']: r['lead_turns'] for r in report['rows']} == {b: 1, c: 0}
    assert 'lead_turns' in h.client('coordinator').get('/api/reports/' + published['publication_id'] + '?format=csv').text
    w = retained_staffing(h, h.workspace('Open'), typ=typ, people=[a])['workspace']
    target = h.detail(w)['shifts'][0]
    options = h.client('coordinator').post('/api/planner/fill-options', json={
        'workspace_id': w['id'], 'shift_id': target['id'], 'expected_revision': w['revision']}).json()
    assert {r['person_id']: r['can_lead'] for r in options['candidates']} == {b: True, c: True}


def programme_lead(h, person, ends_on='2026-11-01'):
    """A programme only `person` belongs to, and a duty type whose leads must be in it (the shared conditions)."""
    p = h.configure('programmes', {'name': 'Lead programme ' + ends_on, 'starts_on': '2026-01-01'})['record']['id']
    h.configure('programme_memberships', {'person_id': person, 'programme_id': p, 'starts_on': '2026-10-01',
                                          'ends_on': ends_on}, role='coordinator')
    tree = {'match': 'all', 'conditions': [{'field': 'programme_ids', 'op': 'any', 'values': [p]}]}
    return p, lead_type(h, {'minimum': 1, 'maximum': 1, 'capability_ids': [], 'category_ids': [], 'conditions': tree})


def lead_codes(data, lead):
    for p in data['shifts'][0]['participants']:
        p['team_lead'] = p['person_id'] == lead
    return set(codes(data)) & {'LEAD_GAP', 'LEAD_NOT_QUALIFIED'}


def test_who_may_lead_uses_the_shared_conditions_on_any_dimension(h):
    a, b, c = h.seed['people']
    # Session fields and unknown records are refused; a person condition is saved on the duty type.
    session_line = {'match': 'all', 'conditions': [{'field': 'service_ids', 'op': 'any', 'values': [h.seed['services'][0]]}]}
    h.configure('assignment_types', {'name': 'Clinic lead', 'starts_on': '2020-01-01', 'definition': {
        'lead': {'minimum': 1, 'maximum': 1, 'conditions': session_line}}}, status=422)
    h.configure('assignment_types', {'name': 'Ghost lead', 'starts_on': '2020-01-01', 'definition': {
        'lead': {'minimum': 1, 'maximum': 1, 'conditions': {'match': 'all', 'conditions': [
            {'field': 'programme_ids', 'op': 'any', 'values': ['00000000-0000-4000-8000-000000000000']}]}}}}, status=422)
    programme, typ = programme_lead(h, c)
    data = h.snapshot(retained_staffing(h, h.workspace(), typ=typ, people=[a, c])['workspace'])
    assert lead_codes(copy.deepcopy(data), c) == set()
    assert lead_codes(copy.deepcopy(data), a) == {'LEAD_NOT_QUALIFIED', 'LEAD_GAP'}
    unqualified = copy.deepcopy(data)
    lead_codes(unqualified, a)
    found = next(f for f in evaluate(unqualified)['findings'] if f['code'] == 'LEAD_NOT_QUALIFIED')
    assert found['message'] == 'This team lead does not meet who may lead this duty' and found['detail']['conditions']
    lead = data['shifts'][0]['type_snapshot']['definition']['lead']
    # Other dimensions through the same evaluator: a tag, a person (except), a rank or a tag.
    data['people'][a]['tags'] = ['Charge']
    lead['conditions'] = {'match': 'all', 'conditions': [{'field': 'tags', 'op': 'any', 'values': ['charge']}]}
    assert lead_codes(copy.deepcopy(data), a) == set() and lead_codes(copy.deepcopy(data), c) == {'LEAD_NOT_QUALIFIED', 'LEAD_GAP'}
    lead['conditions'] = {'match': 'all', 'conditions': [{'field': 'person_ids', 'op': 'none', 'values': [a]}]}
    assert lead_codes(copy.deepcopy(data), a) == {'LEAD_NOT_QUALIFIED', 'LEAD_GAP'} and lead_codes(copy.deepcopy(data), c) == set()
    rank = next(m['category_id'] for m in data['memberships'] if m['person_id'] == c)
    lead['conditions'] = {'match': 'any', 'conditions': [{'field': 'category_ids', 'op': 'any', 'values': [rank]},
                                                         {'field': 'tags', 'op': 'any', 'values': ['charge']}]}
    assert lead_codes(copy.deepcopy(data), a) == set() and lead_codes(copy.deepcopy(data), c) == set()
    # Legacy lists AND conditions: c is in the programme but may lose the listed skill.
    lead['capability_ids'] = [h.seed['supervisor_capability']]
    lead['conditions'] = {'match': 'all', 'conditions': [{'field': 'programme_ids', 'op': 'any', 'values': [programme]}]}
    assert lead_codes(only_lead_capable(copy.deepcopy(data), c), c) == set()
    assert lead_codes(only_lead_capable(copy.deepcopy(data), a), c) == {'LEAD_NOT_QUALIFIED', 'LEAD_GAP'}


def test_lead_conditions_split_at_dated_membership_and_drive_solver_greedy_and_options(h):
    a, b, c = h.seed['people']
    _, typ = programme_lead(h, c, ends_on='2026-10-05')
    night = {'start': '2026-10-04T20:00:00+03:00', 'end': '2026-10-05T04:00:00+03:00'}
    w = retained_staffing(h, h.workspace(), typ=typ, people=[a, {'person_id': c, **night, 'team_lead': True}], **night)['workspace']
    found = [f for f in h.detail(w)['workspace']['validation']['findings'] if f['code'] == 'LEAD_NOT_QUALIFIED']
    assert found and datetime.fromisoformat(found[0]['start']) == datetime.fromisoformat('2026-10-05T00:00:00+03:00')
    _, typ = programme_lead(h, c)
    result = generate(h.snapshot(h.shift(h.workspace('Solver'), typ=typ, people=[])['workspace']), 10)
    assert result['status'] == 'candidate_feasible', result
    assert [r['participant']['person_id'] for r in result['additions'] if r['participant']['team_lead']] == [c]
    greedy = generation.generate(h.snapshot(retained_staffing(h, h.workspace('Greedy'), typ=typ, people=[a, c])['workspace']), 20)
    assert greedy['validation']['valid'], greedy['validation']['findings']
    assert [r['participant']['person_id'] for r in greedy['additions'] if r['participant'].get('team_lead')] == [c]
    w = retained_staffing(h, h.workspace('Options'), typ=typ, people=[a])['workspace']
    options = h.client('coordinator').post('/api/planner/fill-options', json={
        'workspace_id': w['id'], 'shift_id': h.detail(w)['shifts'][0]['id'], 'expected_revision': w['revision']}).json()
    assert {r['person_id']: r['can_lead'] for r in options['candidates']} == {b: False, c: True}


def test_several_leads_up_to_the_maximum(h):
    a, b, c = h.seed['people']
    typ = lead_type(h, {'minimum': 1, 'maximum': 2, 'capability_ids': [], 'category_ids': []}, requirements=[{'minimum': 3, 'maximum': 3}])
    def lead(p):
        return {'person_id': p, 'start': START, 'end': END, 'team_lead': True}
    ok = h.shift(h.workspace(), typ=typ, people=[lead(a), lead(b), c])['workspace']
    assert not [f for f in h.detail(ok)['workspace']['validation']['findings'] if f['code'].startswith('LEAD')]
    over = retained_staffing(h, h.workspace('Three'), typ=typ, people=[lead(a), lead(b), lead(c)])['workspace']
    assert [f['message'] for f in h.detail(over)['workspace']['validation']['findings'] if f['code'] == 'LEAD_GAP'] == [
        'This session has more team leads than allowed']


def anyone_leads(h, **lead):
    return lead_type(h, {'minimum': 1, 'maximum': 1, 'capability_ids': [], 'category_ids': [], **lead})


def lead_counts(result):
    counts = {}
    for r in result['additions']:
        if r['participant'].get('team_lead'):
            counts[r['participant']['person_id']] = counts.get(r['participant']['person_id'], 0) + 1
    return counts


def week(h, typ, name, first, people, days=4, *, retained=False):
    """A week's draft from day `first` of October with one session a day for `days` days; `people(start, end)`."""
    w = h.workspace(name, start=f'2026-10-{first:02}', end=f'2026-10-{first + 7:02}')
    for day in range(first, first + days):
        s, e = f'2026-10-{day:02}T08:00:00+03:00', f'2026-10-{day:02}T12:00:00+03:00'
        participants = [p if isinstance(p, dict) else {'person_id': p, 'start': s, 'end': e} for p in people(s, e)]
        created = (retained_staffing(h, w, start=s, end=e, typ=typ, people=participants) if retained
                   else h.shift(w, start=s, end=e, typ=typ, people=participants))
        w = created['workspace']
    return w


def goal_policy(h, **rule):
    return h.policy({'kind': 'balance_goal', 'metric': 'lead_turns', 'aim': 'equal_share', 'period': 'rota', **rule}, {}, severity='advisory')


def lead_goal(data, typ):
    from packages.domain.fairness import balance_goals
    return next(p for p in balance_goals(data) if p['definition']['rule']['metric'] == 'lead_turns'
                and p['definition']['rule'].get('assignment_type_ids') == [typ])


def disable_lead_balance(data, typ):
    from packages.domain.rule_lines import seeded_policies
    data['policies'] = seeded_policies(data['policies'], data.pop('policy_seed_lines', []))
    for policy in data['policies']:
        rule = policy['definition']['rule']
        if rule['kind'] == 'catalogue' and rule['id'] == 'fair_share':
            for row in rule['lines']:
                settings = {**rule.get('settings', {}).get('balance_goal', {}),
                            **row.get('settings', {}).get('balance_goal', {})}
                if settings.get('metric') == 'lead_turns' and typ in settings.get('assignment_type_ids', []):
                    for number in row['numbers'].values():
                        number['level'] = 'off'
    data.pop('_line_index_cache', None)
    assert lead_goal(data, typ)['definition']['strength'] == 'off'


def test_lead_turns_are_shared_fairly_by_default_and_can_be_switched_off(h):
    a, b, _ = h.seed['people']
    typ = anyone_leads(h)
    data = h.snapshot(week(h, typ, 'Fair leads', 4, lambda s, e: [a, b], retained=True))
    from packages.domain.fairness import goal_identity
    lead = lead_goal(data, typ)
    assert lead['definition']['strength'] == 'medium'
    data['people'] = {p: data['people'][p] for p in (a, b)}
    result = generate(copy.deepcopy(data), 20)
    assert result['status'] == 'candidate_feasible', result.get('status')
    assert lead_counts(result) == {a: 2, b: 2}
    report = next(r for r in result['validation']['goals'] if r['goal_id'] == goal_identity(lead))
    assert {a, b} <= {r['person_id'] for r in report['people']} and report['metric'] == 'lead_turns'
    disable_lead_balance(data, typ)
    off = generate(data, 20)
    assert all(not r['people'] and not r['pairs'] for r in off['validation'].get('goals', [])
               if r['metric'] == 'lead_turns')


def test_lead_turn_shares_count_only_people_who_may_lead(h):
    a, b, c = h.seed['people']
    _, typ = programme_lead(h, c)
    users = goal_policy(h)
    data = h.snapshot(week(h, typ, 'Eligible only', 4, lambda s, e: [a, c], days=2, retained=True))
    from packages.domain.fairness import balance_goals, goal_report
    goals = {p['id']: p for p in balance_goals(data) if p['definition']['rule']['metric'] == 'lead_turns'}
    assert users['id'] in goals
    assert [r['person_id'] for r in goal_report(data, goals[users['id']])['people']] == [c]
    fallback = goal_report(data, lead_goal(data, typ))
    assert fallback['people'] == fallback['pairs'] == []  # The explicit goal covers the default's audience.


def test_lead_turns_follow_history_in_solver_and_greedy(h):
    a, b, _ = h.seed['people']
    typ = anyone_leads(h)
    h.publish(week(h, typ, 'Last week', 4, lambda s, e: [{'person_id': a, 'start': s, 'end': e, 'team_lead': True}, b], days=2))
    data = h.snapshot(week(h, typ, 'This week', 11, lambda s, e: [a, b], days=2, retained=True))
    from packages.domain.fairness import goal_identity
    history = data['fairness_history'][goal_identity(lead_goal(data, typ))]['people']
    assert history[a]['value'] == 2 and history[b]['value'] == 0
    data['people'] = {p: data['people'][p] for p in (a, b)}
    solved = generate(copy.deepcopy(data), 20)
    assert solved['status'] == 'candidate_feasible', solved.get('status')
    assert lead_counts(solved) == {b: 2}
    greedy = generation.generate(copy.deepcopy(data), 20)
    assert greedy['validation']['valid'], greedy['validation']['findings']
    assert lead_counts(greedy) == {b: 2}


def test_most_lead_turns_per_person_is_one_setting_checked_as_a_quota(h):
    a, b, _ = h.seed['people']
    h.configure('assignment_types', {'name': 'Bad cap', 'starts_on': '2020-01-01', 'definition': {
        'lead': {'minimum': 1, 'maximum': 1, 'most_turns': 0}}}, status=422)
    typ = anyone_leads(h, most_turns=1, most_turns_per='week')
    w = week(h, typ, 'Capped', 4, lambda s, e: [{'person_id': a, 'start': s, 'end': e, 'team_lead': True}, b], days=2)
    over = [f for f in h.detail(w)['workspace']['validation']['findings'] if f['code'] == 'QUOTA_ABOVE_MAXIMUM']
    data = h.snapshot(w)
    from packages.domain.conditions import active_rules
    cap = next(p for p in active_rules(data) if p['definition']['rule']['kind'] == 'block_quota'
               and p['definition']['rule']['metric'] == 'lead_turns')
    assert [(f['person_id'], f['severity'], f['rule_id']) for f in over] == [(a, 'warning', cap['id'])]
    data['generation_options'] = {'mode': 'rebalance'}
    disable_lead_balance(data, typ)  # The visible quota alone spreads the turns.
    result = generate(data, 20)
    assert result['status'] == 'candidate_feasible', result.get('status')
    assert not [f for f in result['validation']['findings'] if f['code'] == 'QUOTA_ABOVE_MAXIMUM']


@pytest.mark.parametrize('changed', [{'most_turns': 3}, {'most_turns': None}, {'most_turns_per': 'month'}])
def test_legacy_lead_cap_edits_point_to_workload_limit_but_unchanged_values_save(h, changed):
    created = historical_type(h, {'name': 'Legacy lead cap', 'starts_on': '2020-01-01',
        'definition': {'lead': {'most_turns': 2, 'most_turns_per': 'week'}}})
    value = {'name': created['name'], 'starts_on': created['starts_on'], 'definition': copy.deepcopy(created['definition'])}
    value['definition']['lead'].update(changed)
    refused = h.configure('assignment_types', value, created['id'], created['revision'], status=422)
    assert refused['error']['code'] == 'USE_RULE_CATALOGUE'
    assert 'Rules' in refused['error']['message']
    value['definition'] = created['definition']
    value['name'] = 'Renamed duty with its unchanged legacy cap'
    updated = h.configure('assignment_types', value, created['id'], created['revision'])['record']
    assert updated['definition']['lead'] == created['definition']['lead']


def test_adding_a_lead_cap_on_update_points_to_workload_limit(h):
    created = h.configure('assignment_types', {'name': 'No legacy cap', 'starts_on': '2020-01-01',
        'definition': {}})['record']
    value = {'name': created['name'], 'starts_on': created['starts_on'], 'definition': {'lead': {'most_turns': 2}}}
    refused = h.configure('assignment_types', value, created['id'], created['revision'], status=422)
    assert refused['error']['code'] == 'USE_RULE_CATALOGUE'
