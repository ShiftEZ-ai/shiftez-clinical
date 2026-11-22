"""rc.13: one visible issue retains every matched rule line; calendar weeks cross New Year."""
import copy
import gc
import time
from datetime import date, timedelta

import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services.issues import issue_key, stored_findings
from packages.domain.conditions import active_rules
from packages.domain import cp_sat, generation
from packages.domain.generation import coverage_findings, deficit_score, introduced_overflow
from packages.domain.quotas import window_blocks
from packages.domain.rota_summary import normalized_check
from packages.domain.rule_groups import same_checks
from packages.domain.rule_lines import attention
from packages.domain.score import schedule_score
from packages.domain.validation import evaluate, finding_observations, grouped_rule_findings
from .test_rule_catalogue_api import approve, line, person, propose
from .test_rule_attention import made as attention_rule
from .test_performance_budget import python_budget


def all_observations(finding):
    return [finding, *(finding.get('detail') or {}).get('derived_findings', [])]


@pytest.mark.parametrize('historical', [False, True])
@pytest.mark.postgres
def test_independent_weekly_quota_lines_are_one_issue_and_every_surface_agrees(pg, historical):
    h = pg
    who = h.seed['people'][0]
    a, b = h.seed['services'][:2]
    dates = {'starts_on': '2026-10-04', 'ends_on': '2026-10-11', 'window_alignment': 'calendar_week'}
    lines = [line(who=person(who), where={'conditions': [{'field': 'service_ids', 'values': services}]},
                  numbers={'min': {'value': 1}}, settings={'block_quota': dates})
             for services in ([a], [b], [a, b])]
    policy = approve(h, propose(h, 'duties_per_week', lines)['record'])
    created = h.shift(h.workspace(), start='2026-10-04T08:00:00+03:00', end='2026-10-04T12:00:00+03:00',
                      people=[h.seed['people'][1]])
    w = created['workspace']
    data = h.snapshot(w)
    validation = evaluate(data)
    findings = [f for f in validation['findings'] if f['rule_id'] == policy['id']]
    assert len(findings) == 1
    assert {f['detail']['line'] for f in all_observations(findings[0])} == {0, 1, 2}
    assert len([r for r in validation['quota_report']['rows'] if r['policy_id'] == policy['id']]) == 3
    assert deficit_score(validation)[1] == 3
    if historical:
        with transaction(h.app.state.engine, write=True) as db:
            stored = db.get(m.Workspace, w['id'])
            old = copy.deepcopy(stored.validation)
            old['findings'] = [{**f, 'detail': {k: v for k, v in f['detail'].items() if k != 'derived_findings'}}
                               for issue in old['findings'] for f in all_observations(issue)]
            old['error_count'] = old['score']['blocking_count'] = 3
            stored.validation = old

    detail = h.detail(w)
    check, health = detail['workspace']['validation'], created['health']
    issue_count = len({issue_key(f) for f in check['findings']})
    assert issue_count == len(check['findings']) == check['error_count'] + check['warning_count']
    assert check['error_count'] == check['score']['blocking_count'] == health['errors'] == 1
    with transaction(h.app.state.engine) as db:
        assert len(stored_findings(db.get(m.Workspace, w['id']))) == issue_count
    summary = h.get('coordinator', f"/api/workspaces/{w['id']}/summary")
    listed = next(row for row in h.get('coordinator', '/api/workspaces')['workspaces'] if row['id'] == w['id'])
    assert summary['issues']['blocking'] == listed['health']['errors'] == check['error_count']
    home = h.get('coordinator', '/api/home')
    assert next(item['count'] for item in home['attention'] if item['key'] == 'blocking') == 1
    submitted = h.lifecycle(w, 'submit', status=422)
    assert submitted['error']['code'] == 'VALIDATION_FAILED'
    assert submitted['error']['details']['error_count'] == 1
    if historical:
        with transaction(h.app.state.engine) as db:
            assert db.get(m.Workspace, w['id']).validation == old

    # Filling one clinic cannot satisfy the separate clinic requirement hidden under the same visible issue.
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[who], service=0)['workspace']
    remaining = [f for f in h.detail(w)['workspace']['validation']['findings'] if f['rule_id'] == policy['id']]
    assert len(remaining) == 1 and remaining[0]['detail']['line'] == 1
    assert not h.detail(w)['workspace']['validation']['valid']


@pytest.mark.parametrize('severity', ['mandatory', 'advisory'])
@pytest.mark.postgres
def test_scoped_cover_lines_keep_strictest_gate_and_advisory_score(pg, severity):
    h = pg
    rank = {'conditions': [{'field': 'category_ids', 'values': [h.seed['category']]}]}
    level = 'mandatory' if severity == 'mandatory' else 'medium'
    # Safety kinds are always mandatory. For advisory staffing, compare scored targets.
    slot = 'min' if severity == 'mandatory' else 'target'
    lines = [line(numbers={slot: {'value': 2, 'level': level}}),
             line(who=rank, numbers={slot: {'value': 1, 'level': level}})]
    policy = approve(h, propose(h, 'people_per_session', lines)['record'])
    w = h.shift(h.workspace(), people=[])['workspace']
    data = h.snapshot(w)
    validation = evaluate(data)
    findings = [f for f in validation['findings'] if f['rule_id'] == policy['id']]
    assert len(findings) == 1
    assert len(all_observations(findings[0])) == 2
    assert {f['detail']['required'] for f in all_observations(findings[0])} == {1, 2}
    assert findings[0]['detail']['required'] == (2 if severity == 'mandatory' else 1)
    assert len([f for f in coverage_findings(validation) if f['rule_id'] == policy['id']]) == 2
    ungrouped = {**validation, 'findings': [f for issue in validation['findings'] for f in all_observations(issue)]}
    grouped_score = schedule_score(data, validation)
    assert grouped_score['uncapped_score'] == schedule_score(data, ungrouped)['uncapped_score']
    assert normalized_check(validation)['findings'] == validation['findings']
    check = h.detail(w)['workspace']['validation']
    assert check['findings'] == validation['findings']
    h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][2], 'kind': 'recorded_leave',
        'start': '2026-12-01T00:00:00+03:00', 'end': '2026-12-02T00:00:00+03:00', 'private_reason': 'Unrelated period'})
    preview = h.client('coordinator').post('/api/planner/issues/replace-preview', json={
        'workspace_id': w['id'], 'expected_revision': w['revision'], 'keys': [issue_key(findings[0])]})
    assert preview.status_code == 200, preview.text
    if severity == 'mandatory':
        assert not validation['valid']
        assert len(active_rules(data['policies'])) >= 2


@pytest.mark.postgres
def test_fairness_line_issues_keep_all_goal_reports_and_scores(pg):
    h = pg
    lines = [line(numbers={'target': {'value': 100, 'level': 'medium'}},
                  settings={'balance_goal': {'metric': metric, 'aim': 'target'}}) for metric in ('duties', 'hours')]
    policy = approve(h, propose(h, 'fair_share', lines)['record'])
    w = h.shift(h.workspace())['workspace']
    data = h.snapshot(w)
    validation = evaluate(data)
    findings = [f for f in validation['findings'] if f['rule_id'] == policy['id']]
    assert len(findings) == len(h.seed['people']) == len({issue_key(f) for f in findings})
    alex = next(f for f in findings if f['person_id'] == h.seed['people'][0])
    assert len(all_observations(alex)) == 2
    assert sum(len(all_observations(f)) for f in findings) == 4
    assert len([goal for goal in validation['goals'] if goal['policy_id'] == policy['id']]) == 2
    ungrouped = {**validation, 'findings': [f for issue in validation['findings'] for f in all_observations(issue)]}
    assert schedule_score(data, validation)['uncapped_score'] == schedule_score(data, ungrouped)['uncapped_score']


@pytest.mark.postgres
def test_greedy_does_not_hide_new_secondary_breach_behind_a_tolerated_fixed_breach(pg):
    h = pg
    a, b = h.seed['people'][:2]
    clinics = h.seed['services'][:2]
    limits = [line(who=person(a), where={'conditions': [{'field': 'service_ids', 'values': clinics[:1]}]},
                   numbers={'max': {'value': 0}}),
              line(where={'conditions': [{'field': 'service_ids', 'values': clinics[1:]}]}, numbers={'max': {'value': 1}})]
    policy = approve(h, propose(h, 'people_per_session', limits)['record'])
    h.policy({'kind': 'minimum_cover', 'minimum': 2})
    start, end = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'
    w = h.shift(h.workspace(), people=[{'person_id': a, 'start': start, 'end': end, 'fixed': True}])['workspace']
    data = h.snapshot(w)
    data['people'] = {pid: data['people'][pid] for pid in (a, b)}
    shift = data['shifts'][0]
    shift['service_ids'] = clinics
    for definition in (shift['type_snapshot']['definition'], data['types'][shift['assignment_type_id']]['definition']):
        definition.update(allow_shared_services=True, shared_duty_reason='Synthetic shared clinic duty')
    initial = [f for f in evaluate(data)['findings'] if f['rule_id'] == policy['id']]
    assert len(initial) == 1 and initial[0]['detail']['line'] == 0
    result = generation.generate(data, 10)
    assert not result['additions'], result
    assert result['status'] == 'candidate_with_gaps'


def test_new_secondary_overflow_is_detected_per_line_and_count_bucket():
    def overflow(line, scheduled):
        return {'code': 'CAPACITY_OVERFLOW', 'rule_id': 'q', 'shift_id': 's', 'person_id': None, 'severity': 'warning',
                'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00',
                'detail': {'line': line, 'count_bucket': [['category_ids', str(line)]], 'scheduled': scheduled}}
    primary, old_secondary, new_secondary = overflow(0, 4), overflow(1, 2), overflow(1, 3)
    before = {'findings': [{**primary, 'detail': {**primary['detail'], 'derived_findings': [old_secondary]}}]}
    after = {'findings': [{**primary, 'detail': {**primary['detail'], 'derived_findings': [new_secondary]}}]}
    assert introduced_overflow(before, after) == [new_secondary]
    split = {**old_secondary, 'start': '2026-10-04T09:00:00+03:00'}
    assert introduced_overflow(before, {'findings': [split]}) == []


def test_stripping_display_needs_keeps_empty_safety_detail_identity():
    base = {'code': 'UNAVAILABLE', 'rule_id': None, 'shift_id': 's', 'person_id': 'p', 'severity': 'error',
            'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00', 'detail': None}
    grouped = {**base, 'detail': {'derived_findings': [{'code': 'COVERAGE_GAP', 'severity': 'error',
                'detail': {'required': 1, 'available': 0}}]}}
    observation = next(finding_observations({'findings': [grouped]}))
    assert observation['detail'] is None
    assert generation.citation_finding_key(observation) == generation.citation_finding_key(base)


def test_rule_attention_attributes_secondary_lines_without_counting_them_twice():
    row = attention_rule('people_per_session', [{'numbers': {'min': {'value': 2}}},
        {'who': person('p'), 'numbers': {'min': {'value': 1}}}])
    findings = [{'code': 'SCOPED_COVERAGE_GAP', 'rule_id': row['id'], 'shift_id': 's', 'person_id': None,
        'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00', 'severity': 'error',
        'message': 'Synthetic cover gap', 'workspace_id': 'w', 'workspace': 'Synthetic draft',
        'detail': {'line': n, 'required': 2 - n, 'available': 0}} for n in (0, 1)]
    grouped = grouped_rule_findings(findings, [])
    would = {line['id']: {'p'} for line in row['definition']['rule']['lines']}
    blocked, = [issue for issue in attention([row], would, '2026-10-04', grouped) if issue['kind'] == 'blocks']
    assert blocked['lines'] == [0, 1]
    assert blocked['facts']['count'] == blocked['facts']['drafts'][0]['count'] == 1


@pytest.mark.parametrize('new_secondary', [False, True], ids=['retained-scope', 'new-guard-breach'])
@pytest.mark.postgres
def test_cp_sat_checks_secondary_clipped_window_inside_selected_scope(pg, monkeypatch, new_secondary):
    h = pg
    w = h.shift(h.workspace(), people=[h.seed['people'][0]])['workspace']
    added = h.shift(w, start='2026-10-06T08:00:00+03:00', end='2026-10-06T12:00:00+03:00', people=[])
    data = h.snapshot(added['workspace'])
    selected = added['shift_id']
    data['generation_session_ids'] = [selected]
    original_evaluate = cp_sat.evaluate

    def checked(candidate, **kwargs):
        result = original_evaluate(candidate, **kwargs)
        primary = {'code': 'QUOTA_ABOVE_MAXIMUM', 'rule_id': 'synthetic-validator-guard', 'shift_id': None,
                   'person_id': h.seed['people'][0], 'severity': 'error', 'message': 'Synthetic retained quota breach',
                   'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-05T00:00:00+03:00',
                   'detail': {'line': 0, 'actual': 3, 'amount': 1, 'difference': 2}}
        observations = [primary]
        has_new_work = any(s['id'] == selected and s['participants'] for s in candidate['shifts'])
        if not new_secondary or has_new_work:
            observations.append({**primary, 'end': '2026-10-07T00:00:00+03:00',
                'message': 'Synthetic secondary quota breach', 'detail': {'line': 1, 'actual': 2, 'amount': 1, 'difference': 1}})
        findings = [*result['findings'], *grouped_rule_findings(observations, [])]
        return {**result, 'findings': findings, 'valid': False,
                'error_count': sum(f['severity'] == 'error' for f in findings)}

    monkeypatch.setattr(cp_sat, 'evaluate', checked)
    progress = []
    result = cp_sat.generate(data, 5, deterministic=True, progress=progress.append)
    if new_secondary:
        assert result['status'] == 'translation_error' and result['additions'] == []
        assert 'summary' not in result
    else:
        assert result['status'] == 'candidate_with_gaps' and result['additions']
        assert len(result['issues']) == len(result['outside_scope_issues']) == 1
        assert progress[-1]['selected_scope_validation_passed'] is False
        assert len(all_observations(result['issues'][0])) == 2


def test_conversion_proof_compares_every_grouped_constraint_and_rejects_missing_secondary():
    def gap(rule, required):
        return {'code': 'SCOPED_COVERAGE_GAP', 'rule_id': rule, 'shift_id': 's', 'person_id': None, 'severity': 'error',
                'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00',
                'detail': {'required': required, 'available': 0}}
    before = {'findings': [gap('old-a', 2), gap('old-b', 3)]}
    after = {'findings': grouped_rule_findings([gap('q', 2), gap('q', 3)], [])}
    after['findings'][0]['detail']['rule_tier'] = 2
    mapping = {'old-a': 'q', 'old-b': 'q'}
    proof = same_checks(before, after, mapping)
    assert not proof['findings_only_before'] and not proof['findings_only_after'] and proof['quota_equal']
    missing = copy.deepcopy(after)
    missing['findings'][0]['detail'].pop('derived_findings')
    assert len(same_checks(before, missing, mapping)['findings_only_before']) == 1


@pytest.mark.parametrize('week_start,first', [(0, '2026-12-28'), (6, '2026-12-27')])
def test_calendar_week_crosses_new_year_without_restarting(week_start, first):
    start = date.fromisoformat(first)
    windows = list(window_blocks(start, date(2027, 1, 11), 'calendar_week', week_start=week_start))
    assert windows[0] == (start, start + timedelta(days=7))
    assert all(end != date(2027, 1, 1) for _, end in windows)


def test_weekly_quota_honours_explicit_new_year_expiry():
    assert list(window_blocks(date(2026, 12, 27), date(2027, 1, 1), 'calendar_week', week_start=6)) == [
        (date(2026, 12, 27), date(2027, 1, 1))]


def test_grouping_matches_issue_keys_and_preserves_clipped_ends_and_rule_bounds():
    def finding(code, end, amount):
        return {'code': code, 'rule_id': 'q', 'shift_id': None, 'person_id': 'p', 'start': '2026-10-04',
                'end': end, 'severity': 'error', 'detail': {'amount': amount, 'difference': -amount}}
    rows = [finding('QUOTA_BELOW_MINIMUM', '2026-10-11', 1),
            finding('QUOTA_BELOW_MINIMUM', '2026-10-12', 2), finding('QUOTA_ABOVE_MAXIMUM', '2026-10-11', 3)]
    grouped = grouped_rule_findings(rows, [])
    assert len(grouped) == len({issue_key(f) for f in rows}) == 2
    assert {f['end'] for f in all_observations(grouped[0])} == {'2026-10-11', '2026-10-12'}
    assert grouped[1] == rows[2]


def test_grouping_is_idempotent_and_flattens_previous_line_groups():
    base = {'code': 'QUOTA_BELOW_MINIMUM', 'rule_id': 'q', 'person_id': 'p', 'severity': 'error',
            'start': '2026-10-04', 'end': '2026-10-11', 'detail': {'difference': -1, 'line': 0}}
    third = {**base, 'detail': {'difference': -1, 'line': 2}}
    second = {**base, 'detail': {'difference': -1, 'line': 1, 'derived_findings': [third]}}
    nested = {**base, 'detail': {**base['detail'], 'derived_findings': [second]}}
    grouped = grouped_rule_findings([nested], [])
    assert {f['detail']['line'] for f in all_observations(grouped[0])} == {0, 1, 2}
    assert grouped_rule_findings(grouped, []) == grouped


@pytest.fixture(scope="module")
def department_grouping():
    source_reads = []

    class Sources(dict):
        def values(self):
            source_reads.append(1)
            return super().values()

    sources = Sources({str(n): {'line': n, 'tier': 2} for n in range(1000)})
    policies = [{'id': 'q', 'template_line': {'line': n, 'slot': 'min'}, '_catalogue_sources': sources,
                 'definition': {'scope': {}}} for n in range(1000)]
    findings = [{'code': 'QUOTA_BELOW_MINIMUM', 'rule_id': 'q', 'person_id': str(n), 'severity': 'error',
                 'start': '2026-10-04', 'end': '2026-10-11', 'detail': {'difference': -1, 'line': line, 'slot': 'min'}}
                for n in range(2304) for line in range(3)]
    samples, results = [], []
    for _ in range(3):
        gc.collect()  # Exclude earlier fixtures' garbage; collection stays enabled during grouping.
        began = time.process_time()
        grouped = grouped_rule_findings(findings, policies)
        samples.append(time.process_time() - began)
        results.append((grouped, len(source_reads)))
    return results, samples


def test_grouping_stays_within_ci_budget_at_department_scale(department_grouping):
    results, _ = department_grouping
    for count, (grouped, source_reads) in enumerate(results, 1):
        assert len(grouped) == 2304 and sum(len(all_observations(f)) for f in grouped) == 6912
        assert source_reads == count, 'Shared source tiers are read once per grouping, not once per policy'


@pytest.mark.cpu_budget
def test_grouping_stays_within_ci_budget_at_department_scale_cpu_budget(department_grouping):
    _, samples = department_grouping
    # Measured .047/.141 CPU s untraced/traced locally, .588 s traced in CI; sample tiny work without one-off noise.
    assert min(samples) < python_budget(.15), samples
    print('rule finding grouping CPU seconds', samples)
