"""Fill options: every person checked for one open place, with only the problems adding them would create."""
import copy
import pytest
from packages.domain.validation import evaluate
from apps.api.services import staffing
from apps.api.services.issues import issue_key
from packages.contracts import schemas as s
from pydantic import ValidationError
from tests.legacy_lead import historical_type

PATH = '/api/planner/fill-options'


def options(h, w, shift_id, status=200, **extra):
    response = h.client('coordinator').post(PATH, json={'workspace_id': w['id'], 'shift_id': shift_id, 'expected_revision': w['revision'], **extra})
    assert response.status_code == status, response.text
    return response.json()



def test_fill_options_equal_loads_use_per_session_shuffle(h, monkeypatch):
    from apps.api.services.issues import best_replacements
    from tests.test_planner_why import why
    old, *candidates = h.seed['people']
    workspace = h.shift(h.workspace(), people=[old])['workspace']
    source = h.snapshot(workspace)
    for pid, name in zip(sorted(candidates), ('Zulu', 'Alpha')):
        source['people'][pid]['display_name'] = name
    monkeypatch.setattr(staffing, 'domain_snapshot', lambda *_: source)
    shift = source['shifts'][0]
    result = options(h, workspace, shift['id'], replace_assignment_id=shift['participants'][0]['id'])
    ordered = [row['person_id'] for row in result['candidates']]
    assert ordered == sorted(candidates, key=lambda pid: staffing.best_order_key(0, 0, pid, shift['id']))
    assert all(row['status'] == 'ok' and row['week_hours'] == row['rota_hours'] == 0
               for row in result['candidates'])
    plan = best_replacements(source, {(shift['id'], old): ['replace']}, set(source['people']))
    explained = why(h, workspace, [shift['id']])['sessions'][0]['rows']
    assert ordered == [row['person_id'] for row in explained if row['group'] == 'free']
    assert plan[0]['replacement_id'] == ordered[0]


@pytest.mark.parametrize('extra', ['none', 'quotas'])
def test_fill_options_rank_and_explain_and_match_the_full_check(h, extra):
    rest = h.policy({'kind': 'min_rest', 'hours': 12})
    if extra == 'quotas':
        # Rolling and fixed quota windows plus an hours cap: the reduced check must still match exactly.
        h.policy({'kind': 'block_quota', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'metric': 'duties', 'comparison': 'maximum', 'amount': 1, 'window_days': 3})
        h.policy({'kind': 'block_quota', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'metric': 'duties', 'comparison': 'maximum', 'amount': 1})
        h.policy({'kind': 'max_hours', 'hours': 16, 'window': 'week', 'include_overtime': True})
    busy = h.seed['people'][0]
    w = h.workspace('Fill options', '2026-10-04', '2026-10-11')
    w = h.shift(w, '2026-10-04T08:00:00+03:00', '2026-10-04T20:00:00+03:00', people=[busy])['workspace']
    w = h.shift(w, '2026-10-06T08:00:00+03:00', '2026-10-06T16:00:00+03:00', people=h.seed['people'][:2], location=1)['workspace']
    w = h.shift(w, '2026-10-04T22:00:00+03:00', '2026-10-05T06:00:00+03:00', people=[], location=1)['workspace']
    target = next(s for s in h.detail(w)['shifts'] if not s['participants'])
    result = options(h, w, target['id'])
    rows = {r['person_id']: r for r in result['candidates']}
    assert busy in rows and rows[busy]['status'] == 'blocked'
    assert any(f['kind'] == 'REST' and f['rule_id'] == rest['id'] for f in rows[busy]['findings'])
    assert rows[busy]['rest_before_hours'] == 2.0
    statuses = [r['status'] for r in result['candidates']]
    assert statuses == sorted(statuses, key=['ok', 'warn', 'blocked'].index), 'best fit first'

    # The reduced per-person check reports exactly what a full evaluation adds for that person.
    source = h.snapshot(w)
    before = evaluate(source)
    for row in result['candidates']:
        if any(f['kind'] == 'NOT ELIGIBLE' and f['code'] != 'NOT_ELIGIBLE' for f in row['findings']):
            continue
        trial = copy.deepcopy(source)
        shift = next(s for s in trial['shifts'] if s['id'] == target['id'])
        shift['participants'].append({**result['slot'], 'person_id': row['person_id'], 'fixed': False, 'supervisor_person_id': None,
                                      'team_lead': False, 'id': 'x', 'shift_id': target['id']})
        key = lambda res: {(f['code'], f['rule_id'], f['start']) for f in res['findings'] if f['person_id'] == row['person_id']}
        # Candidate findings now retain complete session/global deltas (for example a
        # quota observation with person_id set to the candidate), so they may include
        # more than the candidate-only projection used by this legacy parity check.
        assert {(c, r) for c, r, _ in key(evaluate(trial)) - key(before)} <= {
            (f['code'], f['rule_id']) for f in row['findings']}, row['display_name']


def test_shared_safety_exclusions_keep_finding_detail(h):
    person = h.seed['people'][0]
    rest = h.policy({'kind': 'min_rest', 'hours': 12})
    workspace = h.shift(h.workspace('Safety exclusion details'),
                        start='2026-10-04T08:00:00+03:00', end='2026-10-04T20:00:00+03:00',
                        people=[person])['workspace']
    workspace = h.shift(workspace, start='2026-10-04T22:00:00+03:00', end='2026-10-05T06:00:00+03:00', people=[])['workspace']
    source = h.snapshot(workspace)
    target = next(shift for shift in source['shifts'] if not shift['participants'])
    [row], _ = staffing.check_people(source, target, None, {person})
    finding = next(finding for finding in row['findings'] if finding['code'] == 'MIN_REST')
    assert finding['kind'] == 'REST' and finding['rule_id'] == rest['id']
    assert finding['detail']['required_hours'] == 12 and finding['detail']['actual_hours'] == 2
    assert finding['detail']['previous_shift_id'] == source['shifts'][0]['id']
    assert finding['detail']['next_shift_id'] == target['id']

    transition = h.policy({'kind': 'forbidden_transition', 'from_type_ids': [h.seed['type']],
                           'to_type_ids': [h.seed['type']]})
    workspace = h.shift(h.workspace('Transition exclusion details'),
                        start='2026-10-04T08:00:00+03:00', end='2026-10-04T20:00:00+03:00',
                        people=[person])['workspace']
    workspace = h.shift(workspace, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    source = h.snapshot(workspace)
    target = next(shift for shift in source['shifts'] if not shift['participants'])
    [row], _ = staffing.check_people(source, target, None, {person})
    finding = next(finding for finding in row['findings'] if finding['code'] == 'FORBIDDEN_TRANSITION')
    assert finding['kind'] == 'REST' and finding['rule_id'] == transition['id']
    assert finding['detail'] == {'from_shift_id': source['shifts'][0]['id'], 'to_shift_id': target['id']}


def test_late_zero_duty_cap_appears_once_in_fill_options(h):
    person = h.seed['people'][0]
    session = h.shift(h.workspace(), people=[])
    cap = h.policy({'kind': 'max_duties', 'maximum': 0, 'window': 'week'}, {'person_id': person})
    row = next(row for row in options(h, session['workspace'], session['shift_id'])['candidates']
               if row['person_id'] == person)
    finding, = [finding for finding in row['findings'] if finding['rule_id'] == cap['id']]
    assert (row['status'], finding['code'], finding['severity']) == ('blocked', 'MAX_DUTIES', 'error')


def test_late_weekly_hours_cap_blocks_fill_and_save_and_checks_retained_duty(h):
    from tests.test_assignment_override_reason import payload

    retained, candidate = h.seed['people'][:2]
    draft = h.shift(h.workspace(), people=[retained])['workspace']
    draft = h.shift(draft, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', people=[])['workspace']
    cap = h.policy({'kind': 'max_hours', 'hours': 1, 'window': 'week', 'include_overtime': True},
                   workspace_id=draft['id'])
    draft = h.lifecycle(draft, 'validate')['workspace']
    detail = h.detail(draft)
    target = next(shift for shift in detail['shifts'] if not shift['participants'])
    rows = options(h, draft, target['id'])['candidates']
    value = payload(h, draft, target, start=target['start'], end=target['end'], people=[candidate])
    reply = h.command('coordinator', 'shift.update', value, revision=draft['revision'], status=(200, 422))
    refused = (reply.get('error', {}).get('details') or {}).get('findings', [])
    checked = detail['workspace']['validation']['findings']
    cap_findings = lambda findings: {finding['person_id'] for finding in findings
                                    if finding['code'] == 'MAX_HOURS' and finding['rule_id'] == cap['id']
                                    and finding['severity'] == 'error'}
    assert ([row['status'] for row in rows if row['status'] != 'blocked'],
            cap_findings(refused), cap_findings(checked)) == ([], {candidate}, {retained})
    assert rows and {row['person_id'] for row in rows} >= {candidate}
    assert any(finding['code'] == 'MAX_HOURS' and finding['rule_id'] == cap['id']
               for row in rows if row['person_id'] == candidate for finding in row['findings'])
    assert not next(shift for shift in h.detail(draft)['shifts'] if shift['id'] == target['id'])['participants']


def test_fill_options_need_a_draft_coordinator_and_an_open_session(h):
    w = h.workspace('Fill options access', '2026-10-04', '2026-10-11')
    w = h.shift(w)['workspace']
    sid = h.detail(w)['shifts'][0]['id']
    response = h.client('staff').post(PATH, json={'workspace_id': w['id'], 'shift_id': sid, 'expected_revision': w['revision']})
    assert response.status_code == 403
    options(h, w, '00000000-0000-4000-8000-000000000000', status=404)
    options(h, {**w, 'revision': w['revision'] + 5}, sid, status=409)


def test_fill_options_uses_authoritative_eligibility_and_fails_fast(h, monkeypatch):
    """A clinic/date ineligible person is never presented as a clean fill candidate."""
    w = h.workspace('Fill eligibility proof', '2026-10-04', '2026-10-11')
    w = h.shift(w, people=[])['workspace']
    source = h.snapshot(w)
    shift = source['shifts'][0]
    candidate = h.seed['people'][1]
    service_id = shift['service_ids'][0]
    source['eligibilities'] = [row for row in source['eligibilities']
                               if not (row['person_id'] == candidate and row['service_id'] == service_id)]
    monkeypatch.setattr(staffing, 'evaluate', lambda *args, **kwargs: pytest.fail('ineligible candidates must not enter trial evaluation'))

    [row], _ = staffing.check_people(source, shift, None, {candidate})

    assert row['status'] == 'blocked' and not row['fills']
    assert any(f['kind'] == 'NOT ELIGIBLE' for f in row['findings'])


def test_fill_options_precomputes_mandatory_exclusions_before_trials(h, monkeypatch):
    """An all-ineligible candidate set completes without entering validator trials."""
    w = h.shift(h.workspace('Fill exclusion guard'), people=[])['workspace']
    source = h.snapshot(w)
    shift = source['shifts'][0]
    candidates = set(h.seed['people'])
    source['eligibilities'] = []
    monkeypatch.setattr(staffing, 'evaluate', lambda *args, **kwargs: pytest.fail('mandatory exclusions must skip trials'))

    rows, _ = staffing.check_people(source, shift, None, candidates)

    assert {row['person_id'] for row in rows} == candidates
    assert all(row['status'] == 'blocked' and not row['fills'] for row in rows)


def test_fill_options_diffs_session_global_lead_gap(h):
    """A candidate that leaves the session without a qualified lead is never ranked clean."""
    from .test_team_lead import lead_type

    old, other, candidate = h.seed['people']
    typ = lead_type(h, lead={'minimum': 2, 'maximum': 2, 'capability_ids': [], 'category_ids': [],
                             'conditions': {'match': 'all', 'conditions': [
                                 {'field': 'person_ids', 'op': 'any', 'values': [old, other]}]}})
    attendance = {'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00'}
    w = h.shift(h.workspace('Fill lead proof'), typ=typ,
                people=[{'person_id': old, 'team_lead': True, **attendance},
                        {'person_id': other, 'team_lead': True, **attendance}])['workspace']
    source = h.snapshot(w)
    shift = source['shifts'][0]
    replaced = next(row for row in shift['participants'] if row['person_id'] == old)

    [row], _ = staffing.check_people(source, shift, replaced, {candidate})

    assert row['status'] == 'blocked' and not row['fills']
    assert any(f['code'] == 'LEAD_GAP' for f in row['findings'])


def test_fill_options_demotes_replacement_that_introduces_a_session_lead_gap(h):
    original, candidate = h.seed['people'][:2]
    typ = historical_type(h, {'name': 'QA-079 lead safety', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 1}],
                       'lead': {'minimum': 1, 'maximum': 1, 'capability_ids': [], 'category_ids': []}}})['id']
    w = h.shift(h.workspace('QA-079 lead'), typ=typ,
                start='2026-10-04T08:00:00+03:00', end='2026-10-04T12:00:00+03:00',
                people=[{'person_id': original, 'start': '2026-10-04T08:00:00+03:00',
                         'end': '2026-10-04T12:00:00+03:00', 'team_lead': True}])['workspace']
    shift = h.detail(w)['shifts'][0]
    result = options(h, w, shift['id'], replace_assignment_id=shift['participants'][0]['id'])
    row = next(row for row in result['candidates'] if row['person_id'] == candidate)
    assert row['status'] == 'blocked'
    assert any(f['code'] == 'LEAD_GAP' and f.get('person_id') is None for f in row['findings'])


def test_fill_options_keeps_dated_clinic_not_eligible_explicit(h):
    candidate = h.seed['people'][0]
    service = h.seed['services'][0]
    w = h.shift(h.workspace('QA-079 dated clinic'), service=0, people=[])['workspace']
    data = h.snapshot(w)
    shift = data['shifts'][0]
    records = [row for row in data['eligibilities'] if row['person_id'] == candidate and row['service_id'] == service]
    assert records
    for row in records:
        row['ends_on'] = '2026-10-04'
    [result], _ = staffing.check_people(data, shift, None, {candidate})
    assert result['status'] == 'blocked'
    assert any(f['code'] == 'ELIGIBILITY_MISSING' and f['kind'] == 'NOT ELIGIBLE' for f in result['findings'])


def companion_case(h, *, minimum=1, pooled=False):
    trigger, companion, _ = h.seed['people']
    policy = h.policy({'kind': 'minimum_cover', 'minimum': minimum, 'when_person_ids': [trigger],
                       'aggregation': 'service_group' if pooled else 'assignment'},
                      {**({'person_id': companion} if minimum == 1 else {}),
                       **({'service_id': h.seed['services'][0]} if pooled else {})}, severity='advisory')
    w = h.shift(h.workspace(), people=[trigger])['workspace']
    shift = h.detail(w)['shifts'][0]
    finding = next(f for f in w['validation']['findings'] if f['rule_id'] == policy['id'])
    return w, shift, finding


def test_targeted_companion_options_match_full_validator_without_more_candidate_work(h, monkeypatch):
    from sqlalchemy import event
    w, shift, finding = companion_case(h)
    # This distant duty belongs to someone already on the target: candidate trials should omit it.
    w = h.shift(w, start='2026-10-06T08:00:00+03:00', end='2026-10-06T12:00:00+03:00')['workspace']
    original = h.detail(w)
    source = h.snapshot(w)
    real, calls, queries = staffing.evaluate, [], []

    def measured(data, **kwargs):
        calls.append(len(data['shifts']))
        return real(data, **kwargs)

    def sql(*args):
        queries.append(1)

    monkeypatch.setattr(staffing, 'evaluate', measured)
    event.listen(h.app.state.engine, 'before_cursor_execute', sql)
    try:
        ordinary = options(h, w, shift['id'])
        ordinary_work, ordinary_sql = calls[:], len(queries)
        calls.clear()
        queries.clear()
        targeted = options(h, w, shift['id'], finding_key=issue_key(finding))
        assert len(queries) == ordinary_sql, 'Targeting must not add per-candidate SQL'
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', sql)
    assert calls == ordinary_work, 'A current rota check supplies the finding; only bounded candidate trials run'
    assert set(ordinary_work) == {1, len(source['shifts'])}, \
        'Distant work stays outside reduced candidate trials; the whole quota baseline remains complete'
    assert all('resolves' not in r for r in ordinary['candidates'])
    assert [{k: v for k, v in r.items() if k != 'resolves'} for r in targeted['candidates']] == ordinary['candidates']
    rows = {r['person_id']: r for r in targeted['candidates']}
    assert rows[h.seed['people'][1]]['resolves'] is True
    assert rows[h.seed['people'][2]]['resolves'] is False
    assert all(r['fills'] for r in rows.values()), 'Headcount is already filled; companion cover is a different constraint'
    for row in rows.values():
        trial = copy.deepcopy(source)
        target = next(t for t in trial['shifts'] if t['id'] == shift['id'])
        target['participants'].append({**targeted['slot'], 'person_id': row['person_id'], 'id': 'trial',
                                       'shift_id': shift['id'], 'fixed': False, 'team_lead': False,
                                       'supervisor_person_id': None})
        remains = any(f['rule_id'] == finding['rule_id'] and f['shift_id'] == shift['id'] for f in evaluate(trial)['findings'])
        assert row['resolves'] is (not remains)
    assert h.detail(w) == original, 'Read-only decision support leaves the draft and evidence unchanged'


def test_fill_options_reuses_trial_cache_across_candidates(h, monkeypatch):
    w = h.workspace('Fill option cache', '2026-10-04', '2026-10-11')
    w = h.shift(w, people=[])['workspace']
    shift = h.detail(w)['shifts'][0]
    caches = []
    original = staffing.evaluate

    def measured(data, **kwargs):
        if kwargs.get('trial_cache') is not None:
            caches.append(id(kwargs['trial_cache']))
        return original(data, **kwargs)

    monkeypatch.setattr(staffing, 'evaluate', measured)
    result = options(h, w, shift['id'])
    assert len(result['candidates']) > 1
    assert len(set(caches)) == 1


def test_target_cannot_belong_to_another_session_or_stale_check(h):
    w, shift, finding = companion_case(h)
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00')['workspace']
    other = next(t for t in h.detail(w)['shifts'] if t['id'] != shift['id'])
    assert options(h, w, other['id'], status=409, finding_key=issue_key(finding))['error']['code'] == 'ISSUE_GONE'
    # A same-session issue from another draft is also foreign.
    other_w = h.shift(h.workspace(), people=h.seed['people'][:1])['workspace']
    other_finding = next(f for f in other_w['validation']['findings'] if f['rule_id'] == finding['rule_id'])
    assert options(h, w, shift['id'], status=409, finding_key=issue_key(other_finding))['error']['code'] == 'ISSUE_GONE'
    # Current rule/fact edits can stale the check even without changing this workspace's revision.
    policy = next(p for p in h.get('approver', '/api/policies?status=active')['policies'] if p['id'] == finding['rule_id'])
    h.command('approver', 'policy.retire', {'reason': 'Synthetic stale finding test'}, policy['id'], policy['revision'])
    assert options(h, w, shift['id'], status=409, finding_key=issue_key(finding))['error']['code'] == 'ISSUE_GONE'


def test_remaining_cover_on_a_later_subinterval_is_not_a_resolution(h):
    w, target, _ = companion_case(h, minimum=2)
    data = h.snapshot(w)
    shift = next(t for t in data['shifts'] if t['id'] == target['id'])
    _, candidate, existing = h.seed['people']
    shift['participants'].append({**shift['participants'][0], 'id': 'existing', 'person_id': existing})
    data['availability'].append({'id': 'synthetic-late-absence', 'person_id': existing, 'kind': 'recorded_leave',
                                'start': '2026-10-04T10:00:00+03:00', 'end': shift['end']})
    before = evaluate(data)
    finding = next(f for f in before['findings'] if f['code'] == 'SCOPED_COVERAGE_GAP')
    [row], slot = staffing.check_people(data, shift, None, {candidate}, finding=finding)
    trial = copy.deepcopy(data)
    trial['shifts'][0]['participants'].append({**slot, 'id': 'candidate', 'shift_id': shift['id'], 'person_id': candidate})
    after = evaluate(trial)
    assert issue_key(finding) not in {issue_key(f) for f in after['findings']}
    assert any(f['code'] == finding['code'] and f['rule_id'] == finding['rule_id'] for f in after['findings'])
    # The later absence belongs to an existing person, not this candidate. The candidate
    # can be clean while the targeted coverage deficit remains unresolved.
    assert row['status'] == 'ok' and row['resolves'] is False


def test_every_grouped_cover_line_must_be_resolved(h):
    from .test_rule_catalogue_guard import catalogue, line
    from .test_rule_catalogue_api import person
    from packages.domain.validation import finding_observations
    w, target, old = companion_case(h)
    data = h.snapshot(w)
    trigger, candidate, other = h.seed['people']
    rule = catalogue('people_per_session', *[
        line(who=person(pid), numbers={'min': {'value': 1}},
             settings={'minimum_cover': {'when_person_ids': [trigger]}}) for pid in (candidate, other)])
    rule['department_id'] = data['workspace']['department_id']
    data['policies'] = [p for p in data['policies'] if p['id'] != old['rule_id']] + [rule]
    finding = next(f for f in evaluate(data)['findings'] if f['rule_id'] == rule['id'])
    assert len(list(finding_observations({'findings': [finding]}))) == 2
    shift = next(t for t in data['shifts'] if t['id'] == target['id'])
    [row], _ = staffing.check_people(data, shift, None, {candidate}, finding=finding)
    assert row['status'] in {'ok', 'warn'} and row['resolves'] is False


def test_warning_cover_line_in_a_mixed_policy_uses_its_matched_severity(h):
    from .test_rule_catalogue_guard import catalogue, line
    from .test_rule_catalogue_api import person
    w, target, old = companion_case(h)
    data = h.snapshot(w)
    trigger, candidate, other = h.seed['people']
    rule = catalogue('people_per_session',
        line(who=person(candidate), numbers={'min': {'value': 1, 'level': 'medium'}},
             settings={'minimum_cover': {'when_person_ids': [trigger]}}),
        line(numbers={'max': {'value': 10, 'level': 'mandatory'}}))
    rule['department_id'] = data['workspace']['department_id']
    data['policies'] = [p for p in data['policies'] if p['id'] != old['rule_id']] + [rule]
    finding = next(f for f in evaluate(data)['findings'] if f['rule_id'] == rule['id'])
    assert finding['code'] == 'SCOPED_COVERAGE_GAP' and finding['severity'] == 'warning'
    assert finding['detail']['line'] == 0 and finding['detail']['slot'] == 'min'
    shift = next(t for t in data['shifts'] if t['id'] == target['id'])
    rows, _ = staffing.check_people(data, shift, None, {candidate, other}, finding=finding)
    assert {r['person_id']: r['resolves'] for r in rows} == {candidate: True, other: False}


@pytest.mark.parametrize('unknown', ['pooled', 'ordered_population', 'other_kind', 'ineligible', 'blocked', 'replacement'])
def test_unevaluated_target_resolution_is_unknown(h, unknown):
    w, target, finding = companion_case(h, pooled=unknown == 'pooled')
    data = h.snapshot(w)
    shift = next(t for t in data['shifts'] if t['id'] == target['id'])
    candidate = h.seed['people'][1]
    if unknown == 'other_kind':
        finding = {**finding, 'code': 'QUOTA_BELOW_MINIMUM'}
    elif unknown == 'ordered_population':
        policy = next(p for p in data['policies'] if p['id'] == finding['rule_id'])
        policy['definition']['rule']['selection_order'] = [
            {'match': 'all', 'conditions': [{'field': 'person_ids', 'op': 'any', 'values': [candidate]}]}]
    elif unknown == 'ineligible':
        data['people'][candidate]['active'] = False
    elif unknown == 'blocked':
        data['availability'].append({'id': 'synthetic-blocked-absence', 'person_id': candidate,
                                     'kind': 'recorded_leave', 'start': shift['start'], 'end': shift['end']})
    replaced = shift['participants'][0] if unknown == 'replacement' else None
    [row], _ = staffing.check_people(data, shift, replaced, {candidate}, finding=finding)
    assert row['resolves'] is None


@pytest.mark.parametrize('key', ['xx', 'x' * 401, 123])
def test_target_finding_key_uses_existing_issue_key_bounds(key):
    with pytest.raises(ValidationError):
        s.FillOptionsInput(workspace_id='00000000-0000-4000-8000-000000000000',
                           shift_id='00000000-0000-4000-8000-000000000001', expected_revision=1, finding_key=key)


@pytest.mark.parametrize('kind', ['approved_leave', 'recorded_leave', 'rotation'])
def test_fill_options_offer_reasoned_manual_absence_override_without_waiving_findings(h, kind):
    from .test_assignment_override_reason import absence, payload, REASON
    w = h.shift(h.workspace(), people=[])['workspace']
    shift = h.detail(w)['shifts'][0]
    code = absence(h, kind)
    before = h.detail(w)
    candidate = next(row for row in options(h, w, shift['id'])['candidates']
                     if row['person_id'] == h.seed['people'][0])
    assert candidate['status'] == 'blocked' and candidate['override_allowed']
    assert any(f['code'] == code and f['severity'] == 'error' for f in candidate['findings'])
    assert h.detail(w) == before
    value = payload(h, w, shift)
    refused = h.command('coordinator', 'shift.update', value, revision=w['revision'], status=422)
    assert refused['error']['code'] == 'PERSON_UNAVAILABLE'
    saved = h.command('coordinator', 'shift.update', {**value, 'override_reason': REASON}, revision=w['revision'])
    participant, = h.detail(saved['workspace'])['shifts'][0]['participants']
    assert participant['source'] == 'manual' and participant['override_reason'] == REASON
    assert any(f['code'] == code and f['severity'] == 'error' for f in saved['validation']['findings'])
    assert h.lifecycle(saved['workspace'], 'submit', status=422)['error']['code'] == 'VALIDATION_FAILED'


@pytest.mark.parametrize('blocker', ['overlap', 'ineligible'])
def test_fill_options_keep_overlap_and_ineligible_overrides_refused(h, blocker):
    from apps.api import models as m
    from apps.api.db import transaction
    w = h.shift(h.workspace(), people=[])['workspace']
    shift = h.detail(w)['shifts'][0]
    person = h.seed['people'][0]
    if blocker == 'overlap':
        w = h.shift(w, people=[person], service=1)['workspace']
    else:
        with transaction(h.app.state.engine, write=True) as db:
            db.get(m.Person, person).active = False
            db.get(m.Organisation, h.seed['organisation_id']).input_revision += 1
    candidate = next(row for row in options(h, w, shift['id'])['candidates'] if row['person_id'] == person)
    assert candidate['status'] == 'blocked' and not candidate['override_allowed']
    assert any(f['code'] == 'TIME_OVERLAP' if blocker == 'overlap' else f['kind'] == 'NOT ELIGIBLE'
               for f in candidate['findings'])
