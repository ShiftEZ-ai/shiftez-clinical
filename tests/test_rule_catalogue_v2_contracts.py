"""V2 authoring is additive; canonical settings and source slots retain their meaning."""
import uuid

import pytest
from pydantic import ValidationError

from packages.contracts import schemas as s
from packages.contracts.rule_catalogue import (
    CATALOGUE_BY_ID, CATALOGUE_MAPPING, PRIMITIVE_CONDITION_FIELDS, RULE_CATALOGUE_V2, V2_CATALOGUE_BY_ID, catalogue_counts,
    catalogue_entry, slot_for_number, workload_fields,
)


def uid():
    return str(uuid.uuid4())


def line(**more):
    return {'id': uid(), 'numbers': {'max': {'value': 5}}, **more}


def rule(*lines, id='workload_limit', **more):
    return {'kind': 'catalogue', 'catalogue_version': 2, 'id': id, 'lines': list(lines), **more}


def binding(kind, number='max', **more):
    return {'policy_id': uid(), 'revision': 1, 'number_key': number, 'primitive_kind': kind,
            'precedence_partition': 'original-policy', **more}


def test_embedded_selector_metadata_distinguishes_trees_lists_and_enum_settings():
    assert PRIMITIVE_CONDITION_FIELDS['spread_evenly'] == {}
    assert PRIMITIVE_CONDITION_FIELDS['balance_goal'] == {'within': False, 'partners': False}
    assert PRIMITIVE_CONDITION_FIELDS['minimum_cover'] == {'credit_conditions': False, 'selection_order': True}


def test_registry_retains_all_historical_ids_and_exposes_counts_from_the_registry():
    assert len(CATALOGUE_BY_ID) == len(CATALOGUE_MAPPING) == 28
    assert len(RULE_CATALOGUE_V2) == len(V2_CATALOGUE_BY_ID) == catalogue_counts()['authorable']
    assert catalogue_counts() == {'historical': len(CATALOGUE_BY_ID), 'v2': len(RULE_CATALOGUE_V2), 'authorable': len(RULE_CATALOGUE_V2)}
    assert V2_CATALOGUE_BY_ID['preferences']['name'] == 'Preferred people'
    assert all(e['authorable'] and e['catalogue_version'] == 2 for e in RULE_CATALOGUE_V2)
    for id in CATALOGUE_BY_ID:
        assert s.CatalogueRule.model_validate({'kind': 'catalogue', 'id': id}).catalogue_version == 1
        assert catalogue_entry(id) is CATALOGUE_BY_ID[id]
    assert CATALOGUE_MAPPING['overtime_offers'] is None
    with pytest.raises(ValidationError, match='Not a rule in the catalogue'):
        s.CatalogueRule.model_validate(rule(id='duties_per_week'))


@pytest.mark.parametrize('measure, period, kind', [
    ('duties', {'kind': 'day'}, 'max_duties'),
    ('duties', {'kind': 'calendar_week'}, 'max_duties'),
    ('scheduled_hours', {'kind': 'rolling', 'days': 7}, 'max_hours'),
    ('session_units', {'kind': 'calendar_month'}, 'block_quota'),
    ('training_credit', {'kind': 'fixed', 'days': 14, 'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}, 'block_quota'),
    ('reserved_hours', {'kind': 'block', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}, 'block_quota'),
    ('weekends', {'kind': 'fixed', 'days': 28, 'starts_on': '2026-10-01', 'ends_on': '2026-11-01',
                  'anchor_on': '2026-10-01'}, 'block_quota'),
])
def test_typed_workload_write_and_read_share_one_canonical_storage(measure, period, kind):
    parsed = s.CatalogueRule.model_validate(rule(line(measure=measure, period=period)))
    canonical = parsed.model_dump(mode='json')
    stored = canonical['lines'][0]
    assert 'measure' not in stored and 'period' not in stored
    assert set(stored['settings']) == {kind}
    fields = workload_fields(canonical, stored)
    expected_period = {**period, **({'repeat': False} if kind == 'block_quota' and period.get('ends_on') else {})}
    assert fields['measure'] == measure and fields['period'] == expected_period
    assert s.CatalogueRule.model_validate({**canonical, 'lines': [{**stored, **fields}]}).model_dump(mode='json') == canonical
    assert s.CatalogueRule.model_validate(canonical).model_dump(mode='json') == canonical


@pytest.mark.parametrize('measure, period, kind, options', [
    ('scheduled_hours', {'kind': 'calendar_week'}, 'max_hours', {'include_overtime': False, 'window': 'week'}),
    ('duties', {'kind': 'calendar_month'}, 'block_quota',
     {'overtime': 'exclude', 'metric': 'duties', 'window_alignment': 'calendar_month'}),
    ('duties', {'kind': 'rolling', 'days': 40}, 'block_quota',
     {'overtime': 'exclude', 'metric': 'duties', 'window_alignment': 'rolling', 'window_days': 40}),
])
def test_native_measure_or_period_edit_moves_options_to_one_count_kind(measure, period, kind, options):
    parsed = s.CatalogueRule.model_validate(rule(line(measure=measure, period=period,
        settings={'max_duties': {'include_overtime': False, 'group_by': 'all'}}))).model_dump(mode='json')
    stored = parsed['lines'][0]
    assert stored['settings'] == {kind: options}
    assert slot_for_number(V2_CATALOGUE_BY_ID['workload_limit'], 'max', parsed, stored, stored['numbers'])['kind'] == kind
    assert workload_fields(parsed, stored)['period'] == period


def test_typed_quota_edit_retains_quota_only_options_and_zero_count():
    options = {'overtime': 'only', 'participation': ['supervisor'], 'assignment_type_ids': [uid()]}
    parsed = s.CatalogueRule.model_validate(rule(line(numbers={'max': {'value': 0}}, measure='duties',
        period={'kind': 'calendar_week'}, settings={'block_quota': options}))).model_dump(mode='json')
    stored = parsed['lines'][0]
    assert stored['settings'] == {'block_quota': {**options, 'metric': 'duties', 'window_alignment': 'calendar_week'}}
    assert slot_for_number(V2_CATALOGUE_BY_ID['workload_limit'], 'max', parsed, stored, stored['numbers'])['kind'] == 'block_quota'
    assert stored['numbers']['max']['value'] == 0


@pytest.mark.parametrize('measure, period', [
    ('scheduled_hours', {'kind': 'calendar_week'}), ('duties', {'kind': 'calendar_month'}),
])
def test_typed_edit_refuses_to_drop_grouped_duty_count(measure, period):
    with pytest.raises(ValidationError, match='cannot preserve max_duties group_by'):
        s.CatalogueRule.model_validate(rule(line(measure=measure, period=period,
            settings={'max_duties': {'group_by': 'weekday'}})))


def test_typed_edit_cannot_retarget_a_bound_primitive_or_overwrite_explicit_settings():
    with pytest.raises(ValidationError, match='max_duties cannot represent'):
        s.CatalogueRule.model_validate(rule(line(measure='scheduled_hours', period={'kind': 'calendar_week'},
            source_bindings={'max': binding('max_duties')})))
    with pytest.raises(ValidationError, match='conflict with the canonical'):
        s.CatalogueRule.model_validate(rule(line(measure='scheduled_hours', period={'kind': 'calendar_week'},
            settings={'max_duties': {'window': 'week', 'include_overtime': False}})))
    with pytest.raises(ValidationError, match='conflict with the canonical'):
        s.CatalogueRule.model_validate(rule(line(measure='duties', period={'kind': 'calendar_month'},
            settings={'max_duties': {'include_overtime': True}, 'block_quota': {'overtime': 'only'}})))


def test_combined_minimum_does_not_retarget_a_pinned_maximum_to_quota():
    raw = line(numbers={'min': {'value': 2}, 'max': {'value': 5}},
               settings={'block_quota': {'metric': 'duties'}, 'max_duties': {'window': 'week'}},
               source_bindings={'max': binding('max_duties')})
    entry = V2_CATALOGUE_BY_ID['workload_limit']
    assert slot_for_number(entry, 'max', rule(), raw, raw['numbers'])['kind'] == 'max_duties'
    s.CatalogueRule.model_validate(rule(raw))
    raw['source_bindings']['max']['primitive_kind'] = 'min_rest'
    with pytest.raises(ValidationError, match='cannot retain'):
        s.CatalogueRule.model_validate(rule(raw))


def test_read_edit_roundtrip_keeps_separate_pinned_quota_and_limit_periods():
    raw = line(numbers={'min': {'value': 2}, 'max': {'value': 5}},
               settings={'block_quota': {'metric': 'duties', 'window_alignment': 'calendar_week',
                                        'starts_on': '2026-10-01', 'ends_on': '2026-11-01'},
                         'max_duties': {'window': 'week'}},
               source_bindings={'min': binding('block_quota', 'min'), 'max': binding('max_duties')})
    parsed = s.CatalogueRule.model_validate(rule(raw)).model_dump(mode='json')
    decorated = {**parsed['lines'][0], **workload_fields(parsed, parsed['lines'][0])}
    assert s.CatalogueRule.model_validate({**parsed, 'lines': [decorated]}).model_dump(mode='json') == parsed


@pytest.mark.parametrize('raw, message', [
    (line(measure='duties'), 'both measure and period'),
    (line(measure='duties', period={'kind': 'fixed'}), 'required number of days'),
    (line(measure='duties', period={'kind': 'calendar_week', 'days': 7}), 'required number of days'),
    (line(measure='duties', period={'kind': 'calendar_week'}, settings={'max_duties': {'window': 'day'}}), 'conflict'),
    (line(settings={'max_duties': {'unknown': 1}}), 'no line settings'),
    (line(settings={'max_duties': {'window_days': 100}}), 'less than or equal to 31'),
    (line(numbers={'max': {'value': -1}}, settings={'max_duties': {}}), 'greater than or equal to 0'),
    (line(numbers={'max': {'value': 1.5}}, settings={'max_duties': {}}), 'valid integer'),
    (line(numbers={'apply': {'value': None}}, measure='duties', period={'kind': 'block'}), 'No-duties'),
])
def test_invalid_or_conflicting_workload_inputs_are_refused(raw, message):
    with pytest.raises(ValidationError, match=message):
        s.CatalogueRule.model_validate(rule(raw))


@pytest.mark.parametrize('kind, field, measure', [
    ('max_duties', 'maximum', 'duties'), ('max_hours', 'hours', 'scheduled_hours'),
])
def test_zero_maximum_is_retained_in_primitive_and_catalogue_workload_limits(kind, field, measure):
    parsed = s.PolicyDefinition.model_validate({'rule': {'kind': kind, field: 0}})
    assert getattr(parsed.rule, field) == 0
    catalogue = s.CatalogueRule.model_validate(rule(line(numbers={'max': {'value': 0}},
        measure=measure, period={'kind': 'calendar_week'}, settings={kind: {}})))
    assert catalogue.lines[0].numbers['max'].value == 0
    with pytest.raises(ValidationError, match='greater than or equal to 0'):
        s.PolicyDefinition.model_validate({'rule': {'kind': kind, field: -1}})


def test_safety_uses_the_affected_primitive_in_mixed_entries():
    count = rule(line(settings={'block_quota': {'metric': 'duties'}}))
    restriction = rule(line(numbers={'apply': {'value': None}}, settings={'restriction': {}}))
    assert not s.safety_rule({'rule': count})
    assert s.safety_rule({'rule': restriction})
    bad = line(numbers={'apply': {'value': None}}, source_bindings={
        'apply': binding('restriction', 'apply', exception_allowed=True)})
    with pytest.raises(ValidationError, match='Safety source bindings'):
        s.CatalogueRule.model_validate(rule(bad))


def test_duty_spacing_keeps_advisory_avoidance_distinct_from_hard_transitions():
    avoid = line(numbers={'apply': {'value': None, 'level': 'high'}},
                 settings={'shift_succession': {'mode': 'avoid', 'from_type_ids': [uid()], 'to_type_ids': [uid()]}})
    parsed = s.CatalogueRule.model_validate(rule(avoid, id='duty_spacing'))
    assert not s.safety_rule({'rule': parsed.model_dump(mode='json')})
    avoid['numbers']['apply']['level'] = 'mandatory'
    with pytest.raises(ValidationError, match='preference, not mandatory'):
        s.CatalogueRule.model_validate(rule(avoid, id='duty_spacing'))


def test_none_and_inherited_strength_are_not_filled_by_typed_authoring():
    parsed = s.CatalogueRule.model_validate(rule(line(numbers={'max': {'value': None, 'level': None}},
                                                    measure='duties', period={'kind': 'calendar_week'})))
    assert parsed.lines[0].numbers['max'].value is None and parsed.lines[0].numbers['max'].level is None


def test_department_proposal_cannot_be_partial_or_unbound():
    assert s.RuleMigrationInput.model_validate({'reason': 'review conversion'}).version == 1
    with pytest.raises(ValidationError, match='complete department proposal'):
        s.RuleMigrationInput.model_validate({'version': 2, 'reason': 'review conversion', 'catalogue_ids': ['workload_limit']})
    with pytest.raises(ValidationError, match='preview plan hash'):
        s.RuleMigrationInput.model_validate({'version': 2, 'reason': 'review conversion'})
    assert s.RuleMigrationInput.model_validate({'version': 2, 'reason': 'review conversion', 'plan_hash': 'a' * 64}).version == 2


def test_person_facts_require_subject_and_exact_fair_share_source():
    workload = {'kind': 'workload', 'catalogue_id': 'workload_limit', 'line': line(), 'reason': 'review quota'}
    with pytest.raises(ValidationError, match='need a person'):
        s.SchedulingFactInput.model_validate(workload)
    assert s.SchedulingFactInput.model_validate({**workload, 'person_id': uid()}).kind == 'workload'
    target = {'kind': 'target', 'catalogue_id': 'fair_share', 'person_id': uid(), 'reason': 'review target',
              'line': line(numbers={'target': {'value': 7, 'level': 'high'}})}
    with pytest.raises(ValidationError, match='exact source goal'):
        s.SchedulingFactInput.model_validate(target)
    assert s.SchedulingFactInput.model_validate({**target, 'goal_ref': {'policy_id': uid(), 'revision': 2}}).goal_ref.revision == 2


def test_overtime_patterns_and_required_limit_ids_are_typed_person_data():
    offer = {'kind': 'overtime_offer', 'catalogue_id': 'overtime_offers', 'person_id': uid(), 'reason': 'review offer',
             'line': line(numbers={'apply': {'value': None, 'level': 'medium'}}, weekdays=[0],
                          settings={'allocation_overtime': {'required_policy_ids': [uid()]}})}
    assert s.SchedulingFactInput.model_validate(offer).line.weekdays == [0]
    offer['line']['settings']['allocation_overtime']['required_policy_ids'] = ['invalid-source']
    with pytest.raises(ValidationError):
        s.SchedulingFactInput.model_validate(offer)


def test_fact_checks_are_version_bound_feasibility_jobs_only():
    change = {'fact_id': uid(), 'expected_revision': 2}
    job = {'workspace_id': uid(), 'kind': 'feasibility', 'fact_changes': [change]}
    assert s.JobInput.model_validate(job).fact_changes[0].expected_revision == 2
    with pytest.raises(ValidationError, match='feasibility job'):
        s.JobInput.model_validate({**job, 'kind': 'generation'})
    with pytest.raises(ValidationError, match='each scheduling fact once'):
        s.JobInput.model_validate({**job, 'fact_changes': [change, change]})
    decision = s.FactDecisionInput.model_validate({'fact_id': change['fact_id'], 'expected_revision': 2,
                                                   'reason': 'approve quota', 'check_job_ids': [uid()],
                                                   'acknowledge_unresolved': True})
    assert decision.check_job_ids and decision.acknowledge_unresolved


def test_raw_source_defaults_remain_one_canonical_shared_line():
    default = line(numbers={'max': {'value': 5}})
    parsed = s.RuleLine.model_validate(line(source_default=default))
    assert parsed.source_default.numbers['max'].value == 5
    with pytest.raises(ValidationError, match='another source default'):
        s.RuleLine.model_validate(line(source_default=line(source_default=default)))
    with pytest.raises(ValidationError, match='no line settings'):
        s.CatalogueRule.model_validate(rule(line(source_default=line(settings={'max_duties': {'unknown': 1}}))))
    zero = s.CatalogueRule.model_validate(rule(line(source_default=line(numbers={'max': {'value': 0}}))))
    assert zero.lines[0].source_default.numbers['max'].value == 0
    with pytest.raises(ValidationError, match='greater than or equal to 0'):
        s.CatalogueRule.model_validate(rule(line(source_default=line(numbers={'max': {'value': -1}}))))


@pytest.mark.parametrize('entry_id', ['allocation_order', 'preferences'])
@pytest.mark.parametrize('selected, opposite', [('priority', 'session_priority'), ('session_priority', 'priority')])
def test_allocation_secondary_priority_remains_a_canonical_fallback(entry_id, selected, opposite):
    raw = line(numbers={selected: {'value': 7, 'level': 'high'}},
               settings={'allocation_preference': {selected: 7, opposite: -3}})
    canonical = s.CatalogueRule.model_validate(rule(raw, id=entry_id)).model_dump(mode='json')
    stored = canonical['lines'][0]
    assert set(stored['numbers']) == {selected}
    assert stored['settings']['allocation_preference'] == {selected: 7, opposite: -3}
    assert slot_for_number(V2_CATALOGUE_BY_ID[entry_id], selected, canonical, stored, stored['numbers'])['field'] == selected
    assert selected not in CATALOGUE_BY_ID[entry_id]['line_settings']['allocation_preference']
    raw['settings']['allocation_preference'][selected] = 8
    with pytest.raises(ValidationError, match='conflict with the selected number'):
        s.CatalogueRule.model_validate(rule(raw, id=entry_id))


@pytest.mark.parametrize('bad', [s.MAX_SCORE_INPUT + 1, 1.5, '3'])
def test_allocation_fallback_priority_uses_primitive_bounds_and_type(bad):
    raw = line(numbers={'priority': {'value': 7, 'level': 'high'}},
               settings={'allocation_preference': {'session_priority': bad}})
    with pytest.raises(ValidationError):
        s.CatalogueRule.model_validate(rule(raw, id='preferences'))


def test_every_folded_v1_id_redirects_to_an_existing_v2_rule():
    """Old #rules/<v1 id> links follow replacement_id (rule_catalogue.ts folded()); each must name a v2 rule or None."""
    folded = {old: new for old, new in CATALOGUE_MAPPING.items() if old not in V2_CATALOGUE_BY_ID}
    assert folded['keep_off'] == 'workload_limit' and folded['not_after'] == 'duty_spacing'
    for old, new in folded.items():
        assert CATALOGUE_BY_ID[old]['replacement_id'] == new
        assert new is None or new in V2_CATALOGUE_BY_ID, f'{old} folds into {new}, which is not a v2 rule'
