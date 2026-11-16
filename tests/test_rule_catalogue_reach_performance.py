"""Reach semantics and bounded scope evaluation for the Rules read."""

from datetime import date
import json
import uuid

from apps.api.auth import authenticate
from apps.api.db import transaction
from apps.api.services import rule_catalogue
from apps.api.services.policies import people_data
from packages.domain import conditions
from packages.domain.rule_lines import scope_of
from packages.domain.validation import named_people


def _line(index, **scope):
    return {'id': f'g10-line-{index}', **scope, 'numbers': {'max': {'value': 3}}}


def _fixture(h):
    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, h.client('auditor').cookies['rota_session'])
        data, first, last = people_data(db, actor, date(2026, 10, 3))

    people = sorted(data['people'])
    services = sorted(data['services'])
    locations = sorted(data['locations'])
    assignment_types = sorted(data['types'])
    ident = lambda name: str(uuid.uuid5(uuid.NAMESPACE_URL, 'rota:g10:' + name))
    category = next(iter(data['categories']), ident('rank'))
    if category not in data['categories']:
        data['categories'][category] = {'id': category, 'active': True, 'starts_on': '2020-01-01', 'ends_on': None}
        data['memberships'].append({'id': 'g10-membership', 'person_id': people[0], 'category_id': category,
                                    'department_id': h.seed['department_id'], 'starts_on': '2020-01-01', 'ends_on': None})
    programme = next(iter(data['programmes']), ident('programme'))
    if programme not in data['programmes']:
        data['programmes'][programme] = {'id': programme, 'active': True, 'starts_on': '2020-01-01', 'ends_on': '2030-01-01'}
        data['programme_memberships'].append({'id': 'g10-programme-membership', 'person_id': people[1],
                                              'programme_id': programme, 'starts_on': '2020-01-01', 'ends_on': '2030-01-01'})
    data['people'][people[2]] = {**data['people'][people[2]], 'tags': ['g10-tag']}

    # A service-specific capability grant exercises the retained coherent-duty
    # witness path; a different clinic must not inherit that capability.
    capability = next(iter(data['capabilities']), ident('capability'))
    if capability not in data['capabilities']:
        data['capabilities'][capability] = {'id': capability, 'active': True, 'starts_on': '2020-01-01', 'ends_on': None}
    data['grants'].append({'id': 'g10-grant', 'person_id': people[0], 'capability_id': capability,
                           'department_id': h.seed['department_id'], 'service_id': services[0], 'revoked': False,
                           'starts_on': '2020-01-01', 'ends_on': None})
    other_service = services[1] if len(services) > 1 else services[0]
    rows = [{
        'id': 'g10-policy', 'department_id': h.seed['department_id'], 'status': 'active', 'active': True,
        'starts_on': '2020-01-01', 'ends_on': None,
        'definition': {'rule': {'kind': 'catalogue', 'id': 'duties_per_week', 'lines': [
            _line(0, who={'conditions': [{'field': 'person_ids', 'values': [people[0]]}]}),
            _line(1, who={'conditions': [{'field': 'category_ids', 'values': [category]}]}),
            _line(2, who={'conditions': [{'field': 'programme_ids', 'values': [programme]}]}),
            _line(3, who={'conditions': [{'field': 'tags', 'values': ['g10-tag']}]}),
            _line(4, who={'conditions': [{'field': 'eligible_service_ids', 'values': [services[0]]}]}),
            _line(5, where={'conditions': [{'field': 'service_ids', 'values': [other_service]}]}),
            _line(6, where={'conditions': [{'field': 'location_ids', 'values': [locations[0]]}]}),
            _line(7, where={'conditions': [{'field': 'assignment_type_ids', 'values': [assignment_types[0]]}]}),
            _line(8, who={'conditions': [{'field': 'capability_ids', 'values': [capability]}]},
                  where={'conditions': [{'field': 'service_ids', 'values': [other_service]}]}),
        ]}},
    }]
    return data, rows, first, last


def test_catalogue_reach_matches_witness_reference_and_stays_within_scope_budget(h, monkeypatch):
    data, rows, first, last = _fixture(h)
    with monkeypatch.context() as reference_patch:
        reference_patch.setattr(rule_catalogue, '_reach_scope_needs_duty_witness', lambda scope: True)
        reference = rule_catalogue._reach(data, rows, first, last)

    calls = []
    original_scope_applies = conditions.scope_applies

    def counted(*args, **kwargs):
        calls.append((json.dumps(args[1], sort_keys=True, default=str), args[2]))
        return original_scope_applies(*args, **kwargs)

    monkeypatch.setattr(rule_catalogue, 'scope_applies', counted)
    monkeypatch.setattr(conditions, 'scope_applies', counted)
    optimized = rule_catalogue._reach(data, rows, first, last)
    assert optimized == reference

    canonical = {}
    for line in rows[0]['definition']['rule']['lines']:
        scope = scope_of(line)
        key = json.dumps(scope, sort_keys=True, default=str)
        named = named_people(scope)
        canonical[key] = len(data['people']) if named is None else len(named & set(data['people']))
    distinct_sides = sum(canonical.values())
    assert len(calls) <= distinct_sides * 2, (len(calls), distinct_sides)
