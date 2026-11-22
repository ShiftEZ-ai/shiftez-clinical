"""Synthetic conversion regressions for safety, protected data and amendable history."""
import copy
from datetime import date

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts.rule_catalogue import ADVISORY, NEVER_OVERRIDDEN, PERSON_CALENDAR_FACT_MAPPING, RULE_CATALOGUE
from packages.domain.rule_catalogue_migration import after_snapshot, census, plan_v2
from packages.domain.rule_lines import fact_policy, tier
from packages.domain.rule_migration_proof import strict_expanded, strict_proof
from packages.domain.validation import evaluate
from .test_conversion_safety_kinds import SAFETY_REVIEW_REASON
from .test_rule_catalogue_v2_domain import catalogue, line, who
from .test_rule_catalogue_v2_proof import source, uid


SAFETY_SLOTS = [(entry, key, slot) for entry in RULE_CATALOGUE
                if entry.get('safety') and entry['id'] not in PERSON_CALENDAR_FACT_MAPPING
                for key, slots in entry['numbers'].items() for slot in slots
                if slot['kind'] in NEVER_OVERRIDDEN and not slot['advisory']]


@pytest.mark.parametrize('entry,key,slot', SAFETY_SLOTS,
                         ids=[f"{entry['id']}-{key}-{slot['kind']}" for entry, key, slot in SAFETY_SLOTS])
@pytest.mark.parametrize('level', ['mandatory', *ADVISORY])
def test_safety_slot_requires_mandatory_level_to_convert(entry, key, slot, level):
    settings = copy.deepcopy(entry['default_settings'])
    for kind, fields in entry['needs'].items():
        settings.setdefault(kind, {}).update({field: [uid(600), uid(601)] if field.endswith('_ids') else uid(600)
                                             for field in fields})
    settings.setdefault(slot['kind'], {}).update(slot['fixed'])
    if slot['fixed'].get('mode') == 'allowed_ranks':
        settings[slot['kind']].update(trainee_category_ids=[uid(600)])
    number = {'value': 2 if slot['field'] else None, 'level': 'mandatory'}
    original = source(1, {'kind': 'catalogue', 'id': entry['id'], 'settings': settings, 'lines': [
        line(uid(200), {key: number}, who(uid(100)), settings={slot['kind']: settings[slot['kind']]},
             calendar_entry_id=uid(300))]})
    # Historical storage can predate today's authoring-level restrictions.
    original['definition']['rule']['lines'][0]['numbers'][key]['level'] = level
    assert census([original])['lines'][0]['classification'] != 'person_fact'
    plan = plan_v2([original], department_id=uid(10))
    assert not plan['scheduling_facts']
    # Issue #144 refuses advisory safety numbers before target validation or rule-line conversion.
    if level != 'mandatory':
        [blocked] = plan['blocked']
        assert blocked['rule_id'] == original['id']
        assert blocked['reason'] == SAFETY_REVIEW_REASON
        assert not plan['targets'] and not plan['bindings']
        return
    assert not plan['blocked'], plan['blocked']
    target = plan['targets'][0]['payload']['definition']['rule']
    [converted] = target['lines']
    assert tier(converted) == 3
    assert converted['numbers'][key]['level'] == 'mandatory'
    before = {'policies': [original]}
    assert strict_expanded(before, after_snapshot(before, plan))['equal']


@pytest.mark.parametrize('level', ['mandatory', *ADVISORY])
@pytest.mark.parametrize('named_person', [False, True])
def test_calendar_linked_keep_off_requires_mandatory_level_to_convert(h, level, named_person):
    workspace = h.workspace()
    h.shift(workspace)
    before = h.snapshot(workspace)
    original = catalogue('keep_off', [line(uid(200), {'apply': {'value': None, 'level': level}},
        who(h.seed['people'][0]) if named_person else None, calendar_entry_id=uid(300))])
    original['department_id'] = workspace['department_id']
    before.update(policies=[original], source_bindings=[], scheduling_facts=[])
    assert census([original])['lines'][0]['classification'] == 'v2_workload_limit'
    plan = plan_v2(before['policies'], department_id=workspace['department_id'])
    assert not plan['scheduling_facts']
    # Issue #144 refuses advisory keep-off restrictions, so findings parity applies to mandatory conversions.
    if level != 'mandatory':
        [blocked] = plan['blocked']
        assert blocked['rule_id'] == original['id']
        assert blocked['reason'] == SAFETY_REVIEW_REASON
        assert not plan['targets'] and not plan['bindings']
        return
    assert not plan['blocked'], plan['blocked']
    target = plan['targets'][0]['payload']['definition']['rule']
    assert target['id'] == 'workload_limit'
    [converted] = target['lines']
    assert converted['numbers'] == {'apply': {'value': None, 'level': level}}
    assert converted['calendar_entry_id'] == uid(300)
    after = after_snapshot(before, plan)
    proof = strict_proof(before, after)
    assert proof['identical'], {key: value for key, value in proof.items()
                              if isinstance(value, dict) and value.get('equal') is False}
    findings = [finding for finding in evaluate(before)['findings'] if finding.get('rule_id') == original['id']]
    assert findings
    assert findings == [finding for finding in evaluate(after)['findings'] if finding.get('rule_id') == original['id']]


@pytest.mark.parametrize('audience', [None, who(uid(100)),
    {'match': 'all', 'conditions': [{'field': 'programme_ids', 'op': 'any', 'values': [uid(101)]}]}])
@pytest.mark.parametrize('calendar', [None, uid(300)])
@pytest.mark.parametrize('level', ['mandatory', *ADVISORY])
def test_protected_time_requires_mandatory_level_to_convert(audience, calendar, level):
    original = catalogue('protected_time', [line(uid(200), {'apply': {'value': None, 'level': level}},
        audience, settings={'restriction': {'leave_buffer_days': 1}}, calendar_entry_id=calendar)])
    plan = plan_v2([original], department_id=uid(10))
    # Issue #144 refuses advisory protected-time restrictions before converting them to rules or facts.
    if level != 'mandatory':
        [blocked] = plan['blocked']
        assert blocked['rule_id'] == original['id']
        assert blocked['reason'] == SAFETY_REVIEW_REASON
        assert not plan['targets'] and not plan['bindings'] and not plan['scheduling_facts']
        return
    if calendar is None and (audience is None or tier({'who': audience}) != 3):
        assert not plan['blocked'], plan['blocked']
        assert not plan['scheduling_facts']
        target = plan['targets'][0]['payload']['definition']['rule']
        assert target['id'] == 'workload_limit'
        assert set(target['lines'][0]['numbers']) == {'apply'}
        assert target['lines'][0]['numbers']['apply']['level'] == level
        before = {'policies': [original]}
        assert strict_expanded(before, after_snapshot(before, plan))['equal']
        return
    assert not plan['blocked'], plan['blocked']
    assert not plan['targets']
    [fact] = plan['scheduling_facts']
    assert fact['kind'] == 'protected_time'
    assert fact['data']['catalogue_id'] == 'protected_time'
    assert fact['calendar_entry_id'] == calendar
    rule = fact_policy(fact)['definition']['rule']
    assert rule['lines'][0]['numbers']['apply']['level'] == level
    before = {'policies': [original]}
    assert strict_expanded(before, after_snapshot(before, plan))['equal']


@pytest.mark.parametrize('entry,key,value', [('duties_per_week', 'max', 5),
    ('keep_off', 'apply', None), ('protected_time', 'apply', None)])
def test_only_lines_ended_by_earliest_current_publication_are_left_in_source_history(entry, key, value):
    ends = ['2026-09-30', '2026-10-01', '2026-10-02', '2026-11-11', '2026-11-12', None]
    original = catalogue(entry, [line(uid(200 + index), {key: {'value': value, 'level': 'mandatory'}},
        who(uid(100)), weekdays=[index], ends_on=end) for index, end in enumerate(ends)])
    untouched = copy.deepcopy(original)
    plan = plan_v2([original], department_id=uid(10), today='2026-11-11', publication_start='2026-10-01')
    assert not plan['blocked'], plan['blocked']
    assert plan['retirement_cutoff'] == '2026-10-01'
    destinations = plan['bindings'][0]['destinations']
    assert [destination['source_line_index'] for destination in destinations] == [2, 3, 4, 5]
    assert {destination['source_line_id'] for destination in destinations} == {uid(202), uid(203), uid(204), uid(205)}
    assert original == untouched
    before = {'policies': [original]}
    after = after_snapshot(before, plan)
    assert strict_expanded(before, after, retirement_cutoff=plan['retirement_cutoff'])['equal']
    assert not strict_expanded(before, after)['equal']
    # A draft before the cutoff still fails proof if it needs an omitted line.
    assert not strict_expanded({**before, 'workspace': {'starts_on': '2026-09-01'}},
        {**after, 'workspace': {'starts_on': '2026-09-01'}}, retirement_cutoff=plan['retirement_cutoff'])['equal']


@pytest.mark.parametrize('publication_start', [None, '2026-12-01'])
def test_no_publication_or_a_future_publication_never_discards_a_live_line(publication_start):
    original = catalogue('keep_off', [line(uid(200 + index), {'apply': {'value': None, 'level': 'mandatory'}},
        who(uid(100)), ends_on=end) for index, end in enumerate(['2026-11-11', '2026-11-12'])])
    plan = plan_v2([original], department_id=uid(10), today='2026-11-11', publication_start=publication_start)
    assert not plan['blocked'], plan['blocked']
    assert [destination['source_line_index'] for destination in plan['bindings'][0]['destinations']] == [1]


def test_ended_legacy_rows_and_exclusive_quota_bounds_do_not_become_facts():
    original = source(1, {'kind': 'block_quota', 'metric': 'duties', 'comparison': 'maximum', 'amount': 5,
        'starts_on': '2026-09-01', 'ends_on': '2026-10-01'}, who=uid(100))
    plan = plan_v2([original], department_id=uid(10), today='2026-11-11', publication_start='2026-10-01')
    assert not plan['blocked'], plan['blocked']
    assert not plan['scheduling_facts'] and not plan['targets']
    assert plan['bindings'][0]['destinations'] == []
    before = {'policies': [original]}
    assert strict_expanded(before, after_snapshot(before, plan), retirement_cutoff=plan['retirement_cutoff'])['equal']


def test_retiring_a_legacy_carrier_keeps_the_default_on_a_retained_group_line():
    original = source(1, {'kind': 'max_duties', 'maximum': 5, 'window': 'week'}, listed_only=True,
        per_person=[{'category_ids': [uid(100)], 'amount': None, 'ends_on': '2026-10-01'},
                    {'category_ids': [uid(101)], 'amount': None}])
    plan = plan_v2([original], department_id=uid(10), today='2026-11-11', publication_start='2026-10-01')
    assert not plan['blocked'], plan['blocked']
    [converted] = plan['targets'][0]['payload']['definition']['rule']['lines']
    assert converted['source_default']['numbers']['max']['value'] == 5
    assert plan['bindings'][0]['destinations'][0]['source_line_index'] == 1
    before = {'policies': [original]}
    assert strict_expanded(before, after_snapshot(before, plan), retirement_cutoff=plan['retirement_cutoff'])['equal']


def test_cutoff_proof_keeps_existing_native_leadership_without_a_legacy_projection():
    original = source(1, {'kind': 'catalogue', 'id': 'team_leads', 'catalogue_version': 2,
        'lines': [line(uid(200), {'min': {'value': 1, 'level': 'mandatory'}})]})
    plan = plan_v2([original], department_id=uid(10), today='2026-11-11', publication_start='2026-10-01')
    assert not plan['blocked'] and not plan['bindings']
    before = {'policies': [original]}
    assert strict_expanded(before, after_snapshot(before, plan), retirement_cutoff=plan['retirement_cutoff'])['equal']


def test_strict_proof_retains_findings_score_and_full_model_after_historical_line_retirement(h):
    workspace = h.workspace(start='2026-10-01', end='2026-11-01')
    h.shift(workspace)
    before = h.snapshot(workspace)
    person = h.seed['people'][0]
    original = catalogue('duties_per_week', [line(uid(200 + index), {'max': {'value': 5, 'level': 'mandatory'}},
        who(person), ends_on=end) for index, end in enumerate(['2026-10-01', None])])
    original['department_id'] = workspace['department_id']
    before.update(policies=[original], source_bindings=[], scheduling_facts=[])
    plan = plan_v2(before['policies'], department_id=workspace['department_id'],
        today='2026-11-11', publication_start='2026-10-01')
    assert not plan['blocked'], plan['blocked']
    proof = strict_proof(before, after_snapshot(before, plan), retirement_cutoff=plan['retirement_cutoff'])
    assert proof['identical'], {key: value for key, value in proof.items()
                              if isinstance(value, dict) and value.get('equal') is False}


@pytest.mark.parametrize('with_publications', [False, True])
def test_api_uses_department_today_and_only_current_department_publications(h, monkeypatch, with_publications):
    from apps.api.services import independence
    monkeypatch.setattr(independence, 'today', lambda db, department_id: date(2026, 11, 11))
    original = h.policy({'kind': 'max_duties', 'maximum': 5, 'window': 'week'})
    with transaction(h.app.state.engine) as db:
        db.get(m.Policy, original['id']).ends_on = date(2026, 10, 2)
    if with_publications:
        for index, (start, end, current) in enumerate([
                ('2026-09-01', '2026-10-01', False), ('2026-10-01', '2026-11-01', True),
                ('2026-11-01', '2026-12-01', True)]):
            workspace = h.workspace(start=start, end=end)
            snapshot = h.snapshot(workspace)
            with transaction(h.app.state.engine) as db:
                actor = db.get(m.Workspace, workspace['id']).created_by
                db.add(m.Publication(id=uid(300 + index), department_id=workspace['department_id'],
                    workspace_id=workspace['id'], sequence=index + 1, starts_on=date.fromisoformat(start),
                    ends_on=date.fromisoformat(end), current=current, published_by=actor,
                    snapshot=snapshot, validation=evaluate(snapshot), fingerprint='0' * 64))
    plan = h.get('coordinator', '/api/rules/catalogue/migration?version=2&proof=false')
    assert not plan['blocked'], plan['blocked']
    assert plan['retirement_cutoff'] == ('2026-10-01' if with_publications else '2026-11-11')
    binding = next(binding for binding in plan['bindings'] if binding['source_policy_id'] == original['id'])
    assert bool(binding['destinations']) == with_publications
    with transaction(h.app.state.engine) as db:
        stored = db.scalar(select(m.Policy).where(m.Policy.id == original['id']))
        assert stored.status == 'active' and stored.ends_on == date(2026, 10, 2)


def test_group_protection_remains_a_safety_rule_and_uses_membership_at_each_duty(h):
    person = h.seed['people'][0]
    programme = h.configure('programmes', {'name': 'Synthetic programme', 'starts_on': '2026-01-01'})['record']
    membership = h.configure('programme_memberships', {'person_id': person, 'programme_id': programme['id'],
        'starts_on': '2026-10-04', 'ends_on': '2026-10-05'})['record']
    workspace = h.workspace(start='2026-10-04', end='2026-10-06')
    for day in ('04', '05'):
        workspace = h.shift(workspace, people=[person], start=f'2026-10-{day}T08:00:00+03:00',
            end=f'2026-10-{day}T12:00:00+03:00')['workspace']
    before = h.snapshot(workspace)
    original = catalogue('protected_time', [line(uid(200), {'apply': {'value': None, 'level': 'mandatory'}},
        {'match': 'all', 'conditions': [{'field': 'programme_ids', 'op': 'any', 'values': [programme['id']]}]},
        start_time='08:00', end_time='12:00')])
    original['department_id'] = workspace['department_id']
    before.update(policies=[original], source_bindings=[], scheduling_facts=[])
    plan = plan_v2(before['policies'], department_id=workspace['department_id'])
    assert not plan['blocked'], plan['blocked']
    assert not plan['scheduling_facts']
    target = plan['targets'][0]['payload']['definition']['rule']
    assert target['id'] == 'workload_limit'
    assert target['lines'][0]['numbers'] == {'apply': {'value': None, 'level': 'mandatory'}}
    assert target['lines'][0]['source_bindings']['apply']['primitive_kind'] == 'restriction'
    after = after_snapshot(before, plan)
    proof = strict_proof(before, after)
    assert proof['identical'], {key: value for key, value in proof.items()
                              if isinstance(value, dict) and value.get('equal') is False}
    def refused(data):
        return {finding['shift_id'] for finding in evaluate(data)['findings']
                if finding.get('rule_id') == original['id'] and finding['code'] == 'RESTRICTION'
                and finding['severity'] == 'error'}
    assert refused(before) == refused(after) == {before['shifts'][0]['id']}
    current = next(row for row in after['programme_memberships'] if row['id'] == membership['id'])
    current['ends_on'] = '2026-10-06'
    assert refused(after) == {shift['id'] for shift in before['shifts']}
