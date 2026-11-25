from datetime import date, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event

from apps.api import models as m, workspace_baselines
from apps.api.services import rule_catalogue
from apps.api.snapshots import snapshot_hash
from tests.test_rule_catalogue_api import line
from apps.api.db import transaction


def _actor(h):
    return SimpleNamespace(department_id=h.seed['department_id'], permits=lambda *_roles: True)


def _add_fixture(h, count):
    payloads = []
    with transaction(h.app.state.engine, write=True) as db:
        for index in range(count):
            workspace = m.Workspace(
                department_id=h.seed['department_id'],
                name=f'Source option draft {index:02}',
                starts_on=date(2026, 10, 1) + timedelta(days=index),
                ends_on=date(2026, 10, 8) + timedelta(days=index),
                status='draft',
                created_by=h.seed['identities']['coordinator'],
                last_edited_by=h.seed['identities']['coordinator'],
            )
            db.add(workspace)
            db.flush()
            policies = [{
                'id': f'active-{index}', 'status': 'active',
                'department_id': workspace.department_id,
                'definition': {'rule': {'kind': 'catalogue', 'id': 'duties_per_week',
                                        'lines': [line(numbers={'max': {'value': index + 1}})]}},
            }]
            policies.extend({
                'id': f'retired-{index}-{n}', 'status': 'retired',
                'definition': {'rule': {'kind': 'legacy', 'id': f'legacy-{n}'}},
            } for n in range(300))
            payload = {'policies': policies}
            row = m.WorkspaceBaseline(
                department_id=workspace.department_id, workspace_id=workspace.id,
                workspace_revision=workspace.revision, schema_version=1,
                scope_mode='department', requested_sources=['rules'], included_sources=['rules'],
                omitted_sources=[], dependency_closure={}, source_revisions={},
                provenance={'captured_at': '2026-11-13T00:00:00+00:00'}, canonical_payload=payload,
                payload_hash=snapshot_hash(payload), actor_id=h.seed['identities']['coordinator'],
            )
            db.add(row)
            payloads.append((workspace.id, payload))
    return payloads


def _source_options(h):
    with transaction(h.app.state.engine) as db:
        return rule_catalogue._source_options(db, _actor(h), {
            'duties_per_week': {'kind': 'catalogue', 'id': 'duties_per_week', 'lines': []},
        })


def test_source_options_uses_one_ranked_baseline_read_and_caps_verification(h, monkeypatch):
    _add_fixture(h, 8)
    rule_catalogue._baseline_catalogue_cache.clear()
    from apps.api import workspace_baselines
    calls = []
    original = workspace_baselines.verify
    monkeypatch.setattr(workspace_baselines, 'verify', lambda row: (calls.append(row.id), original(row))[1])
    statements = []
    def sql(_conn, _cursor, statement, _params, _context, _many):
        text = ' '.join(statement.lower().split())
        if text.startswith('select') and 'workspace_baselines' in text:
            statements.append(text)
    event.listen(h.app.state.engine, 'before_cursor_execute', sql)
    try:
        options = _source_options(h)
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', sql)
    assert len(statements) == 1
    assert len(calls) <= 3
    assert len(options) == 9
    assert sum(option['changed'] is not None for option in options[1:]) <= 3


def test_source_options_second_read_verifies_no_baselines_after_small_cache_fill(h, monkeypatch):
    _add_fixture(h, 2)
    rule_catalogue._baseline_catalogue_cache.clear()
    from apps.api import workspace_baselines
    calls = []
    original = workspace_baselines.verify
    monkeypatch.setattr(workspace_baselines, 'verify', lambda row: (calls.append(row.id), original(row))[1])
    first = _source_options(h)
    second = _source_options(h)
    assert [option['changed'] for option in first] == [option['changed'] for option in second]
    assert len(calls) == 2


def test_source_options_changed_matches_previous_comparison_after_memo_fill(h):
    payloads = _add_fixture(h, 2)
    rule_catalogue._baseline_catalogue_cache.clear()
    live = {'duties_per_week': {'kind': 'catalogue', 'id': 'duties_per_week', 'lines': []}}
    expected = {}
    for workspace_id, payload in payloads:
        snapshot = rule_catalogue._catalogue_definitions(payload['policies'])
        expected[workspace_id] = sum(snapshot.get(key) != value for key, value in live.items()) + sum(
            key not in live for key in snapshot)
    options = {option['workspace_id']: option for option in _source_options(h)[1:]}
    assert {workspace_id: options[workspace_id]['changed'] for workspace_id in expected} == expected


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_rules_source_reads_real_catalogue_v2_and_legacy_rules(h):
    legacy = h.policy({'kind': 'max_duties', 'maximum': 5, 'window': 'week'})
    draft = h.workspace('Captured rules')
    later = h.policy({'kind': 'max_hours', 'hours': 6, 'window': 'day'})
    catalogue = '/api/rules/catalogue?version=2&include_hidden=true'
    live = h.get('coordinator', catalogue)
    captured = h.get('coordinator', f'{catalogue}&workspace_id={draft["id"]}')
    assert captured['source']['kind'] == 'snapshot'
    assert captured['source']['workspace_id'] == draft['id']
    assert captured['source']['captured_at']
    assert 'source' not in live
    for body in (live, captured):
        fair_share = next(entry['rule'] for entry in body['entries'] if entry['id'] == 'fair_share')
        assert fair_share['definition']['rule']['catalogue_version'] == 2
        assert fair_share['definition']['rule']['lines']

    api = '/api/rules-explained'
    live_ids = {row['rule_id'] for row in h.get('coordinator', api)['rules']}
    selected = f'{api}?workspace_id={draft["id"]}'
    explained = h.get('coordinator', f'{selected}&baseline_id={captured["source"]["baseline_id"]}')
    captured_ids = {row['rule_id'] for row in explained['rules']}
    assert {legacy['id'], later['id']} <= live_ids
    assert legacy['id'] in captured_ids
    assert later['id'] not in captured_ids
    stale = h.get('coordinator', f'{selected}&baseline_id={uuid4()}', status=409)
    assert stale['error']['code'] == 'RULES_SNAPSHOT_UNAVAILABLE'


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize('corruption', [
    'missing-definition', 'scalar-number', 'binding', 'destination-kind', 'fact',
    'exception-id', 'exception-policy', 'exception-date', 'exception-date-format',
])
def test_captured_rules_refuse_corrupt_rows_without_breaking_live(h, monkeypatch, corruption):
    draft = h.workspace('Unusable capture')
    verify = workspace_baselines.verify

    def corrupt(row):
        payload = verify(row)
        if row.workspace_id == draft['id']:
            policy = next(policy for policy in payload['policies'] if policy['status'] == 'active')
            definition = policy['definition']
            if corruption == 'missing-definition':
                return {**payload, 'policies': [{**policy, 'definition': None}]}
            if corruption == 'scalar-number':
                definition = {**definition, 'rule': {**definition['rule'], 'lines': [line(numbers={'target': 3})]}}
                return {**payload, 'policies': [{**policy, 'definition': definition}]}
            if corruption == 'fact':
                fact = {'id': str(uuid4()), 'department_id': policy['department_id'], 'data': {'catalogue_id': 'fair_share'}}
                return {**payload, 'scheduling_facts': [fact]}
            if corruption.startswith('exception'):
                exception = {'department_id': policy['department_id'], 'policy_id': policy['id'],
                             'person_id': h.seed['people'][0], 'status': 'approved', 'policy_revision': policy['revision'],
                             'starts_on': '2026-10-04', 'ends_on': '2026-10-06'}
                if corruption != 'exception-id':
                    exception['id'] = str(uuid4())
                if corruption == 'exception-policy':
                    exception['policy_id'] = []
                if corruption == 'exception-date':
                    exception['ends_on'] = '2026-10-03'
                if corruption == 'exception-date-format':
                    exception.update(starts_on='20261004', ends_on='20261006')
                return {**payload, 'exceptions': [exception]}
            destination = {'id': policy['id'], 'kind': 'fact' if corruption == 'destination-kind' else 'policy'}
            binding = {'source': policy, 'source_policy_id': policy['id'], 'destinations': [destination]}
            if corruption == 'destination-kind':
                binding['source_order'] = 1
            return {**payload, 'source_bindings': [binding]}
        return payload

    monkeypatch.setattr(workspace_baselines, 'verify', corrupt)
    for path in ('/api/rules/catalogue', '/api/rules-explained'):
        h.get('coordinator', path)
        refused = h.get('coordinator', f'{path}?workspace_id={draft["id"]}', status=409)
        assert refused['error']['code'] == 'RULES_SNAPSHOT_UNAVAILABLE'


def test_captured_catalogue_ignores_other_department_and_rota_rules(h, monkeypatch):
    draft = h.workspace('Department capture')
    verify = workspace_baselines.verify
    other_id, private_id, foreign_seed = (str(uuid4()) for _ in range(3))

    def with_unrelated_policies(row):
        payload = verify(row)
        policy = next(policy for policy in payload['policies'] if policy['status'] == 'active')
        other = {**policy, 'id': other_id, 'department_id': str(uuid4())}
        private = {**policy, 'id': private_id, 'definition': {**policy['definition'], 'workspace_id': str(uuid4())}}
        seed = {'department_id': other['department_id'], 'policy_id': policy['id'],
                'policy_revision': policy['revision'], 'line': {'id': foreign_seed, 'numbers': {'target': 3}}}
        binding = {'source': other, 'destinations': [{'id': policy['id'], 'kind': 'policy'}]}
        return {**payload, 'policies': [other, private, *payload['policies']],
                'policy_seed_lines': [*payload['policy_seed_lines'], seed],
                'source_bindings': [*payload['source_bindings'], binding]}

    monkeypatch.setattr(workspace_baselines, 'verify', with_unrelated_policies)
    for path in ('/api/rules/catalogue', '/api/rules-explained'):
        result = repr(h.get('coordinator', f'{path}?workspace_id={draft["id"]}'))
        assert other_id not in result
        assert private_id not in result
        assert foreign_seed not in result


def test_captured_catalogue_retains_folded_lines_and_ended_legacy_rules(h):
    from .test_rule_catalogue_api import approve, person, propose
    from .test_rule_templates import activate, propose as propose_legacy

    people = h.seed['people']
    legacy = activate(h, propose_legacy(h, {'kind': 'max_hours', 'hours': 7, 'window': 'day'}, ends_on='2029-12-31'))
    safety = h.stored_advisory_safety({'kind': 'min_rest', 'hours': 10})
    folded = approve(h, propose(h, 'duties_per_week', [
        line(numbers={'max': {'value': 3, 'level': 'critical'}}),
        line(who=person(people[1]), numbers={'max': {'value': 5, 'level': None}}),
    ])['record'])
    switched_off = approve(h, propose(h, 'duties_per_month', [
        line(numbers={'max': {'value': 2}}),
    ], on=False)['record'])
    draft = h.workspace('Historical rules source')
    verify_date = '2030-01-01'
    url = f'/api/rules/catalogue?version=2&day={verify_date}&workspace_id={draft["id"]}'
    records = {row['id']: row for row in h.get('coordinator', url)['captured_other_rules']}
    numbers = [row['numbers']['max'] for row in records[folded['id']]['definition']['rule']['lines']]
    assert [(number['value'], number['level']) for number in numbers] == [(3, 'critical'), (5, None)]
    assert records[legacy['id']]['definition']['rule']['hours'] == 7
    assert records[safety['id']]['effective_severity'] == 'mandatory'
    off_rule = records[switched_off['id']]['definition']['rule']
    assert off_rule['on'] is False
    assert off_rule['lines'][0]['numbers']['max']['value'] == 2


def test_captured_catalogue_retains_independently_approved_workload_fact(h):
    from .test_scheduling_facts_api import decide, replace, workload

    proposed = h.command('coordinator', 'person.workload.propose', workload(h))['fact']
    original = decide(h, proposed)['fact']
    draft = h.workspace('Captured personal workload rule')
    successor = decide(h, replace(h, original, 4))['fact']
    url = '/api/rules/catalogue?version=2'
    live = h.get('coordinator', url)
    assert any(row['id'] == successor['id'] for entry in live['entries'] for row in entry['also_in_force'])
    captured = h.get('coordinator', f'{url}&workspace_id={draft["id"]}')
    rows = {row['id']: row for row in captured['captured_other_rules']}
    assert successor['id'] not in rows
    saved_line = rows[original['id']]['definition']['rule']['lines'][0]
    assert saved_line['numbers']['max']['value'] == 2
    assert saved_line['measure'] == 'duties'
    assert saved_line['period']['kind'] == 'calendar_week'


@pytest.mark.parametrize('approval', ['current-week', 'current-day', 'stale-week'])
def test_captured_people_table_keeps_limit_with_dated_approval(h, monkeypatch, approval):
    from .test_rule_catalogue_api import approve, person, propose

    who = h.seed['people'][0]
    policy = approve(h, propose(h, 'duties_per_week', [
        line(who=person(who), numbers={'max': {'value': 5}}),
    ])['record'])
    draft = h.workspace('Captured exception rules')
    day = h.get('coordinator', '/api/rules/catalogue')['day']
    week_start = h.get('coordinator', '/api/me')['organisation']['week_start']
    selected = date.fromisoformat(day)
    first = selected if approval == 'current-day' else selected - timedelta(days=(selected.weekday() - week_start) % 7)
    last = selected if approval == 'current-day' else first + timedelta(days=6)
    exception_id = str(uuid4())
    verify = workspace_baselines.verify

    def with_exceptions(row):
        payload = verify(row)
        policies = [{**p, 'definition': {**p['definition'], 'exception_allowed': True}}
                    if p['id'] == policy['id'] else p for p in payload['policies']]
        exception = {'id': exception_id, 'department_id': policy['department_id'], 'policy_id': policy['id'],
                     'policy_revision': policy['revision'] - int(approval == 'stale-week'), 'person_id': who,
                     'status': 'approved', 'starts_on': first.isoformat(),
                     'ends_on': (last + timedelta(days=1)).isoformat()}
        return {**payload, 'policies': policies, 'exceptions': [exception]}

    monkeypatch.setattr(workspace_baselines, 'verify', with_exceptions)
    body = h.get('coordinator', f'/api/rules-explained?at={day}&workspace_id={draft["id"]}')
    person_row = next(row for row in body['people_table']['rows'] if row['kind'] == 'person' and row['id'] == who)
    cell = next(cell for cell in person_row['cells'].values() if cell['rule_id'] == policy['id'])
    assert cell['kind'] == 'line'
    assert '5' in cell['cell'].partition(' · Approved exception ')[0]
    assert cell['actions'] == []
    if approval == 'stale-week':
        assert cell['exception_id'] is None
        assert 'Approved exception' not in cell['display']
    else:
        assert cell['exception_id'] == exception_id
        assert f'Approved exception {first} through {last}' in cell['display']
