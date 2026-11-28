"""Configured counting blocks keep their phase independently of effective validity."""
from copy import deepcopy
from datetime import date, timedelta
from fractions import Fraction
import time

import pytest
from ortools.sat.python import cp_model
from pydantic import ValidationError

from packages.contracts.rule_catalogue import workload_fields, workload_settings
from packages.contracts.schemas import BlockQuota, CatalogueRule, WorkloadPeriod
from packages.domain.cp_sat_quotas import add_quotas
from packages.domain.quotas import applicable_policies, quota_window_amount
from packages.domain.validation import evaluate
from .test_quota_windows import data
from .test_rule_lines import line, rule as catalogue_rule


def rule(**changes):
    return {'kind': 'block_quota', 'starts_on': '2032-04-05', 'ends_on': '2032-04-25',
            'metric': 'duties', 'comparison': 'minimum', 'amount': 5,
            'window_alignment': 'fixed', 'window_days': 10, 'window_anchor_on': '2032-04-01',
            'prorate_partial_windows': True, **changes}


def blocks(value, first='2032-04-05', last='2032-04-25'):
    return applicable_policies(data(value, first, last))


def observed(policies):
    return [(p['definition']['rule']['starts_on'], p['definition']['rule']['ends_on'], quota_window_amount(p))
            for p in policies]


def test_weekend_buckets_use_local_presence_and_exclude_the_end_instant():
    from packages.domain.patterns import presence_weekends
    from packages.domain.time import aware

    rule = {'weekend_start': 5, 'weekend_days': 2}
    friday = aware('2027-03-05T18:00:00+03:00')
    saturday = aware('2027-03-06T00:00:00+03:00')
    sunday = aware('2027-03-07T12:00:00+03:00')
    monday = aware('2027-03-08T01:00:00+03:00')
    assert not presence_weekends([(friday, saturday)], 'Asia/Riyadh', rule)
    assert presence_weekends([(friday, sunday), (sunday, monday)], 'Asia/Riyadh', rule) == {date(2027, 3, 6)}


def test_anchor_keeps_partial_blocks_on_both_sides_and_before_the_anchor():
    windows = blocks(rule(comparison='target'))
    assert observed(windows) == [
        ('2032-04-05', '2032-04-11', Fraction(5)),
        ('2032-04-11', '2032-04-21', Fraction(5)),
        ('2032-04-21', '2032-04-25', Fraction(5))]
    assert [tuple(p['_quota_full_window']) for p in windows] == [
        ('2032-04-01', '2032-04-11'), ('2032-04-11', '2032-04-21'), ('2032-04-21', '2032-05-01')]
    before_anchor = blocks(rule(starts_on='2032-03-29', ends_on='2032-04-07', comparison='target'),
                           '2032-03-29', '2032-04-07')
    assert observed(before_anchor) == [
        ('2032-03-29', '2032-04-01', Fraction(5)),
        ('2032-04-01', '2032-04-07', Fraction(5))]
    assert [tuple(p['_quota_full_window']) for p in before_anchor] == [
        ('2032-03-22', '2032-04-01'), ('2032-04-01', '2032-04-11')]


def test_workspace_boundaries_do_not_clip_the_nominal_block_during_expansion():
    windows = blocks(rule(comparison='target'), '2032-04-13', '2032-04-14')
    assert observed(windows) == [
        ('2032-04-11', '2032-04-21', Fraction(5))]
    assert tuple(windows[0]['_quota_full_window']) == ('2032-04-11', '2032-04-21')


@pytest.mark.parametrize('comparison,evaluated_amount', [('minimum', 2), ('maximum', 5), ('target', 2)])
def test_expansion_keeps_the_configured_amount_and_evaluated_rows_are_authoritative(comparison, evaluated_amount):
    [policy] = blocks(rule(starts_on='2032-04-01', ends_on='2032-04-02', window_days=3,
                           comparison=comparison), '2032-04-01', '2032-04-02')
    assert quota_window_amount(policy) == 5
    assert tuple(policy['_quota_full_window']) == ('2032-04-01', '2032-04-04')
    assert quota_window_amount(policy, evaluated_amount) == Fraction(str(evaluated_amount))


def test_other_units_use_the_canonical_rounded_amount_for_solver_translation():
    [policy] = blocks(rule(starts_on='2032-04-01', ends_on='2032-04-02', window_days=3,
                           metric='session_units', amount=1), '2032-04-01', '2032-04-02')
    assert quota_window_amount(policy) == 1
    assert quota_window_amount(policy, 1) == 1
    assert tuple(policy['_quota_full_window']) == ('2032-04-01', '2032-04-04')


def test_neutral_defaults_keep_start_alignment_and_full_amount_at_a_partial_end():
    old = rule(window_anchor_on=None, prorate_partial_windows=False)
    old.pop('window_anchor_on'), old.pop('prorate_partial_windows')
    neutral = {**old, 'window_anchor_on': None, 'prorate_partial_windows': False}
    assert observed(blocks(old)) == observed(blocks(neutral)) == [
        ('2032-04-05', '2032-04-15', Fraction(5)), ('2032-04-15', '2032-04-25', Fraction(5))]
    assert observed(blocks({**neutral, 'ends_on': '2032-04-22'}))[-1][-1] == 5


def test_catalogue_line_validity_clips_without_restarting_the_counting_phase():
    settings = {'block_quota': {k: v for k, v in rule(comparison='target').items()
                               if k not in ('kind', 'comparison', 'amount')}}
    own = line(numbers={'target': {'value': 5, 'level': 'medium'}}, starts_on='2032-04-08', ends_on='2032-04-13')
    policy = catalogue_rule('duties_per_period', own, settings=settings)
    source = data(rule(), '2032-04-08', '2032-04-13')
    source['policies'] = [policy]
    windows = applicable_policies(source)
    assert observed(windows) == [
        ('2032-04-08', '2032-04-11', Fraction(5)), ('2032-04-11', '2032-04-13', Fraction(5))]
    assert [tuple(p['_quota_full_window']) for p in windows] == [
        ('2032-04-01', '2032-04-11'), ('2032-04-11', '2032-04-21')]


@pytest.mark.parametrize('changes', [
    {'window_alignment': 'rolling'}, {'window_alignment': 'calendar_month', 'window_days': None},
    {'prorate_department_days': True, 'comparison': 'target'},
])
def test_counting_contract_rejects_incompatible_settings(changes):
    with pytest.raises(ValidationError):
        BlockQuota.model_validate(rule(**changes))


def test_fixed_period_accepts_partial_validity_without_the_legacy_opt_in():
    assert BlockQuota.model_validate(rule(window_days=30, prorate_partial_windows=False)).window_days == 30


def test_typed_editor_period_round_trips_separately_from_line_dates():
    period = WorkloadPeriod.model_validate({'kind': 'fixed', 'days': 10, 'anchor_on': '2032-04-01',
                                           'prorate_partial_windows': True}).model_dump(mode='json', exclude_unset=True)
    settings = workload_settings('duties', period, {}, {'target': {'value': 5}})
    assert settings == {'block_quota': {'metric': 'duties', 'window_alignment': 'fixed', 'window_days': 10,
                                      'window_anchor_on': '2032-04-01', 'prorate_partial_windows': True}}
    fields = workload_fields({'id': 'workload_limit', 'catalogue_version': 2, 'settings': settings},
                             {'numbers': {'target': {'value': 5}}})
    assert fields['period'] == {'kind': 'fixed', 'days': 10, 'anchor_on': '2032-04-01',
                                'prorate_partial_windows': True}
    for kind in ('block', 'calendar_week', 'rolling'):
        with pytest.raises(ValidationError):
            WorkloadPeriod.model_validate({**period, 'kind': kind})


@pytest.mark.parametrize('field,value,canonical_field', [
    ('anchor_on', None, 'window_anchor_on'),
    ('prorate_partial_windows', False, 'prorate_partial_windows'),
])
def test_read_edit_period_clears_are_explicit_and_cannot_silently_keep_stale_settings(field, value, canonical_field):
    raw = {'kind': 'catalogue', 'catalogue_version': 2, 'id': 'workload_limit', 'lines': [line(
        numbers={'target': {'value': 5, 'level': 'medium'}}, measure='duties',
        period={'kind': 'fixed', 'days': 10, 'starts_on': '2032-04-01', 'ends_on': '2032-04-21',
                'anchor_on': '2032-04-02', 'prorate_partial_windows': True})]}
    stored = CatalogueRule.model_validate(raw).model_dump(mode='json')
    decorated = {**stored['lines'][0], **workload_fields(stored, stored['lines'][0])}
    assert CatalogueRule.model_validate({**stored, 'lines': [decorated]}).model_dump(mode='json') == stored
    decorated['period'][field] = value
    with pytest.raises(ValidationError, match='conflict with the canonical workload settings'):
        CatalogueRule.model_validate({**stored, 'lines': [decorated]})
    # A typed edit becomes authoritative only after removing the obsolete dual specification.
    decorated['settings']['block_quota'].pop(canonical_field)
    updated = CatalogueRule.model_validate({**stored, 'lines': [decorated]}).model_dump(mode='json')
    assert updated['lines'][0]['settings']['block_quota'][canonical_field] is value
    read = workload_fields(updated, updated['lines'][0])['period']
    assert read.get(field, value) is value


def test_explicit_neutral_period_options_override_inherited_counting_configuration():
    shared = {'metric': 'duties', 'window_alignment': 'fixed', 'window_days': 10,
              'window_anchor_on': '2032-04-02', 'prorate_partial_windows': True,
              'starts_on': '2032-04-01', 'ends_on': '2032-04-21'}
    raw = {'kind': 'catalogue', 'catalogue_version': 2, 'id': 'workload_limit',
           'settings': {'block_quota': shared}, 'lines': [line(
               numbers={'target': {'value': 5, 'level': 'medium'}}, measure='duties',
               period={'kind': 'fixed', 'days': 10, 'anchor_on': None, 'prorate_partial_windows': False})]}
    stored = CatalogueRule.model_validate(raw).model_dump(mode='json')
    own = stored['lines'][0]['settings']['block_quota']
    assert own['window_anchor_on'] is None and own['prorate_partial_windows'] is False
    period = workload_fields(stored, stored['lines'][0])['period']
    assert 'anchor_on' not in period and 'prorate_partial_windows' not in period


@pytest.mark.parametrize('engine', ['greedy', 'cp_sat'])
def test_both_engines_honour_anchored_partial_minimums_and_maximums(h, engine):
    from packages.domain import cp_sat, generation

    person = h.seed['people'][0]
    quota = h.policy(rule(starts_on='2026-10-04', ends_on='2026-10-11', window_anchor_on='2026-10-03',
                          window_days=3, comparison='range', amount=2, maximum_amount=3), {'person_id': person})
    typ = h.configure('assignment_types', {'name': 'Optional configured counting duty', 'starts_on': '2020-01-01',
                      'definition': {'requirements': []}})['record']['id']
    workspace = h.workspace()
    for offset in range(7):
        day = date(2026, 10, 4) + timedelta(days=offset)
        workspace = h.shift(workspace, typ=typ, people=[], start=f'{day}T08:00:00+03:00',
                            end=f'{day}T12:00:00+03:00')['workspace']
    snapshot = h.snapshot(workspace)
    result = (generation if engine == 'greedy' else cp_sat).generate(snapshot, 20)
    assert result['status'] == 'candidate_feasible', result.get('issues')
    rows = [row for row in result['validation']['quota_report']['rows'] if row['policy_id'] == quota['id']]
    assert len(rows) == 6 and all(row['status'] == 'met' for row in rows)
    assert {(row['starts_on'], row['ends_on'], row['comparison'], row['amount']) for row in rows} == {
        ('2026-10-04', '2026-10-06', 'minimum', 2), ('2026-10-04', '2026-10-06', 'maximum', 3),
        ('2026-10-06', '2026-10-09', 'minimum', 2), ('2026-10-06', '2026-10-09', 'maximum', 3),
        ('2026-10-09', '2026-10-11', 'minimum', 0), ('2026-10-09', '2026-10-11', 'maximum', 3)}


def test_cp_sat_uses_rounded_partial_non_count_amount_without_false_precision_rejection(h):
    person = h.seed['people'][0]
    h.policy(rule(starts_on='2026-10-04', ends_on='2026-10-05', window_anchor_on='2026-10-03',
                  window_days=3, metric='session_units', amount=1), {'person_id': person})
    snapshot = h.snapshot(h.shift(h.workspace(), people=[person])['workspace'])
    model, penalties = cp_model.CpModel(), []
    variable = model.new_bool_var('counted_duty')
    shift = next(s for s in snapshot['shifts'] if s['participants'])
    by_person = {person: [(shift, shift['participants'][0], None, None, variable)]}
    assert add_quotas(model, snapshot, by_person, lambda expression, *_: penalties.append(expression)) == []
    model.minimize(variable)
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.max_time_in_seconds = 5
    assert solver.solve(model) == cp_model.OPTIMAL
    assert solver.value(variable) == 1
    row = evaluate(snapshot)['quota_report']['rows'][0]
    assert row['amount'] == 1


@pytest.fixture(scope='module')
def department_year_counting_windows():
    source = data(rule(starts_on='2032-01-01', ends_on='2033-01-01', window_days=7,
                       window_anchor_on='2031-12-30'), '2032-01-01', '2033-01-01')
    original = source['policies'][0]
    source['policies'] = [{**deepcopy(original), 'id': str(n)} for n in range(150)]
    started = time.process_time()
    policies = applicable_policies(source)
    elapsed = time.process_time() - started
    return policies, elapsed


def test_counting_window_expansion_cpu_is_bounded_for_a_department_year(department_year_counting_windows):
    policies, _ = department_year_counting_windows
    assert len(policies) == 150 * 53, len(policies)


@pytest.mark.cpu_budget
def test_counting_window_expansion_cpu_is_bounded_for_a_department_year_cpu_budget(department_year_counting_windows):
    _, elapsed = department_year_counting_windows
    assert elapsed < 2, elapsed
