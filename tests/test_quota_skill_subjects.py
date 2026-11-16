"""Quota membership exists independently of assigned work and keeps complete clinic predicates."""
import copy
import uuid

import pytest

from packages.contracts.schemas import PolicyDefinition
from packages.domain import conditions, quotas
from packages.domain.cp_sat import generate
from packages.domain.generation import generate as greedy_generate
from packages.domain.validation import evaluate
from .test_rule_skill_precedence import _fixture, _line, _who


def _quota(h, version=2, *, service=1, comparison='max'):
    data, policy, person, skill = _fixture(h, version, service)
    for line in policy['definition']['rule']['lines']:
        line['numbers'] = {comparison: {'value': line['numbers']['max']['value'], 'level': 'mandatory'}}
        line['settings'] = {'block_quota': {'metric': 'duties', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11'}}
    policy['definition'] = PolicyDefinition.model_validate(policy['definition']).model_dump(mode='json')
    data['memberships'] = [r for r in data['memberships'] if r['person_id'] == person]
    data['quota_context'] = {'complete': True, 'start': '2026-10-04T00:00:00+03:00',
                             'end': '2026-10-11T00:00:00+03:00', 'publications': []}
    return data, policy, person, skill


def _assigned(data, person):
    witness = copy.deepcopy(data)
    for shift in witness['shifts']:
        shift['participants'] = [{'id': str(uuid.uuid4()), 'shift_id': shift['id'], 'person_id': person,
            'start': shift['start'], 'end': shift['end'], 'participation': 'staff', 'overtime': False, 'revision': 1}]
    return witness


@pytest.mark.parametrize('version', [1, 2])
@pytest.mark.parametrize('service', [0, 1])
def test_clinic_skill_quota_keeps_default_maximum_and_solver_parity(h, version, service):
    data, _, person, _ = _quota(h, version, service=service)
    check = evaluate(_assigned(data, person))
    assert check['quota_report']['complete']
    assert check['valid'] is (service == 0), check
    result = generate(data, 3, feasibility_only=True)
    assert result['status'] == ('candidate_feasible' if service == 0 else 'requirements_conflict'), result


@pytest.mark.parametrize('op', ['any', 'none'])
@pytest.mark.parametrize('matching_provider', [False, True])
def test_clinic_skill_quota_keeps_usual_provider_subjects_and_contributions(h, op, matching_provider):
    data, policy, person, _ = _quota(h, service=0, comparison='min')
    [line] = policy['definition']['rule']['lines'] = policy['definition']['rule']['lines'][1:]
    line['where'] = _who('usual_assignee_ids', person)
    line['where']['conditions'][0]['op'] = op
    witness = _assigned(data, person)
    for shift in witness['shifts']:
        shift['series_snapshot'] = {'definition': {
            'default_person_ids': [person if matching_provider else h.seed['people'][1]]}}
    check = evaluate(witness)
    assert check['quota_report']['complete'], check
    [row] = check['quota_report']['rows']
    assert row['amount'] == 2
    assert row['actual'] == (2 if matching_provider == (op == 'any') else 0)


@pytest.mark.parametrize('variant', ['scoped', 'same_clinic', 'global', 'person', 'complement', 'retired'])
def test_empty_minima_exist_without_work_and_only_where_the_default_can_apply(h, variant):
    data, policy, person, skill = _quota(h, comparison='min')
    data['shifts'], data['focus_ids'] = [], []
    lines = policy['definition']['rule']['lines']
    lines[1]['numbers']['min']['value'] = 0
    if variant == 'same_clinic':
        for line in lines:
            line['where'] = _who('service_ids', h.seed['services'][0])
    elif variant == 'global':
        for grant in data['grants']:
            if grant['person_id'] == person and grant['capability_id'] == skill:
                grant['service_id'] = None
    elif variant == 'person':
        lines[1]['who'] = _who('person_ids', person)
    elif variant == 'complement':
        other = copy.deepcopy(lines[1])
        other['id'] = str(uuid.uuid4())
        other['who']['conditions'][0]['op'] = 'none'
        lines.append(other)
    elif variant == 'retired':
        for kind in ('types', 'services', 'locations'):
            for record in data[kind].values():
                record.update(active=False, ends_on='2026-10-01')
    check = evaluate(data)
    assert check['quota_report']['complete'], check
    default = [r for r in check['quota_report']['rows'] if r['line_id'] == lines[0]['id']]
    required = variant in {'scoped', 'retired'}
    assert bool(default) is required, check
    assert check['valid'] is not required, check
    if required:
        assert default[0]['amount'] == 1 and default[0]['actual'] == 0


@pytest.mark.parametrize('shared', [False, True])
def test_shared_clinic_skill_intersection_is_a_distinct_subject_context(h, shared):
    data, policy, person, skill = _quota(h, comparison='min')
    data['shifts'], data['focus_ids'] = [], []
    second = str(uuid.uuid4())
    data['capabilities'][second] = {'active': True}
    data['grants'].append({'person_id': person, 'department_id': h.seed['department_id'], 'capability_id': second,
                           'service_id': h.seed['services'][1], 'starts_on': '2020-01-01'})
    for typ in data['types'].values():
        typ['definition']['allow_shared_services'] = shared
    lines = policy['definition']['rule']['lines']
    lines[1]['numbers']['min']['value'] = 0
    lines[1]['who']['conditions'][0]['values'] = [skill, second]
    for line in lines:
        line['where'] = {'conditions': [{'field': 'service_ids', 'op': 'any', 'values': h.seed['services']}]}
    check = evaluate(data)
    assert check['quota_report']['complete'], check
    assert check['valid'] is not shared, check
    assert any(r['line_id'] == lines[0]['id'] for r in check['quota_report']['rows']) is shared


def test_negative_skill_subject_and_foreign_department_do_not_disappear(h):
    data, policy, person, skill = _quota(h, comparison='min')
    data['shifts'], data['focus_ids'] = [], []
    policy['definition']['rule']['lines'] = policy['definition']['rule']['lines'][1:]
    policy['definition']['rule']['lines'][0]['who']['conditions'][0]['op'] = 'none'
    check = evaluate(data)
    assert not check['valid'] and check['quota_report']['rows'][0]['actual'] == 0, check
    for grant in data['grants']:
        if grant['person_id'] == person and grant['capability_id'] == skill:
            grant.update(department_id=str(uuid.uuid4()), service_id=None)
    # A skill held in another department cannot remove this local negative-skill obligation either.
    check = evaluate(data)
    assert not check['valid'] and check['quota_report']['rows'], check


def test_legacy_skill_override_keeps_default_quota_at_other_clinics(h):
    data, source, person, skill = _quota(h)
    base = {**source, 'kind': 'block_quota', 'definition': PolicyDefinition.model_validate({'rule': {
        'kind': 'block_quota', 'metric': 'duties', 'comparison': 'maximum', 'amount': 1,
        'starts_on': '2026-10-04', 'ends_on': '2026-10-11'}}).model_dump(mode='json')}
    override = copy.deepcopy(base)
    override.update(id=str(uuid.uuid4()))
    override['definition'].update(scope={'conditions': _who('capability_ids', skill)}, overrides=[base['id']])
    override['definition']['rule']['amount'] = 2
    data['policies'] = [base, override]
    check = evaluate(_assigned(data, person))
    assert check['quota_report']['complete'] and not check['valid'], check
    assert any(f['code'] == 'QUOTA_ABOVE_MAXIMUM' and f['rule_id'] == base['id'] for f in check['findings'])


def test_foreign_global_skill_cannot_remove_a_local_default_quota(h):
    data, _, person, skill = _quota(h)
    for grant in data['grants']:
        if grant['person_id'] == person and grant['capability_id'] == skill:
            grant.update(department_id=str(uuid.uuid4()), service_id=None)
    check = evaluate(_assigned(data, person))
    assert check['quota_report']['complete'] and not check['valid'], check
    assert any(f['code'] == 'QUOTA_ABOVE_MAXIMUM' for f in check['findings'])


@pytest.mark.parametrize('clinic', [None, 0, 1])
def test_inherited_quota_strength_is_one_strongest_applicable_window(h, clinic):
    data, policy, person, skill = _quota(h)
    lines = policy['definition']['rule']['lines']
    lines[1]['numbers']['max']['level'] = 'high'
    own = _line(3, who=_who('person_ids', person), level=None)
    own['settings'] = copy.deepcopy(lines[0]['settings'])
    lines.append(own)
    if clinic is not None:
        for line in lines:
            line['where'] = _who('service_ids', h.seed['services'][clinic])
    check = evaluate(_assigned(data, person))
    assert check['quota_report']['complete'], check
    rows = [r for r in check['quota_report']['rows'] if r['line_id'] == own['id']]
    assert len(rows) == 1 and rows[0]['amount'] == 3, check
    assert (rows[0]['severity'], rows[0]['strength']) == (
        ('advisory', 'high') if clinic == 0 else ('mandatory', 'medium'))


def test_report_context_budget_is_global_and_overflow_never_certifies_a_prefix(h, monkeypatch):
    data, policy, _, _ = _quota(h, comparison='min')
    data['shifts'], data['focus_ids'] = [], []
    data['policies'] = [{**copy.deepcopy(policy), 'id': str(uuid.uuid4())} for _ in range(3)]
    monkeypatch.setattr(quotas, 'MAX_SUBJECT_CONTEXTS', 1)
    original, calls = conditions.scope_applies, []
    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(conditions, 'scope_applies', counted)
    check = evaluate(data)
    assert not check['valid'] and not check['quota_report']['complete'], check
    assert len(calls) <= 1
    issue = next(f for f in check['findings'] if f['code'] == 'QUOTA_SCOPE_LIMIT')
    assert issue['person_id'] is None
    for engine in (generate, greedy_generate):
        result = engine(data, time_budget_seconds=3)
        assert result['status'] == 'invalid_inputs' and not result.get('additions'), result
        assert not result.get('optimality_proven')


def test_missing_context_catalogues_are_unresolved_instead_of_waiving_minima(h):
    data, _, _, _ = _quota(h, comparison='min')
    data['shifts'], data['focus_ids'], data['types'] = [], [], {}
    check = evaluate(data)
    assert not check['valid'] and not check['quota_report']['complete'], check
    assert any(f['code'] == 'QUOTA_SCOPE_LIMIT' for f in check['findings'])


def test_incompatible_context_setup_cannot_erase_an_empty_minimum(h):
    data, policy, _, _ = _quota(h, comparison='min')
    data['shifts'], data['focus_ids'] = [], []
    allowed, excluded = str(uuid.uuid4()), str(uuid.uuid4())
    selected = h.seed['locations'][1]
    for lid, location in data['locations'].items():
        location['location_type_id'] = excluded if lid == selected else allowed
    for typ in data['types'].values():
        typ['definition']['allowed_location_type_ids'] = [allowed]
    for line in policy['definition']['rule']['lines']:
        line['where'] = {'conditions': [{'field': 'service_ids', 'values': [h.seed['services'][1]]},
                                         {'field': 'location_ids', 'values': [selected]}]}
    check = evaluate(data)
    assert not check['valid'] and not check['quota_report']['complete'], check
    assert any(f['code'] == 'QUOTA_SCOPE_LIMIT' for f in check['findings'])
