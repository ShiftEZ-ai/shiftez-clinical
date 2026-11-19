"""Zero ceilings prohibit work even after a draft captures its ordinary rules."""
from copy import deepcopy

import pytest

from apps.api import models as m
from apps.api.db import transaction
from packages.domain.cp_sat import generate as cp_sat_generate
from packages.domain.generation import generate as greedy_generate
from packages.domain.validation import evaluate
from .test_generation_superseded_apply import apply, run


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('change', ['added', 'tightened'])
@pytest.mark.parametrize('path', ['check', 'greedy', 'cp_sat', 'apply', 'submit', 'approve'])
def test_zero_maximum_binds_live_after_draft_capture(request, backend, change, path):
    h = request.getfixturevalue(backend)
    policy = h.policy({'kind': 'max_duties', 'maximum': 1}) if change == 'tightened' else None
    occupied = path in {'check', 'submit', 'approve'}
    draft = h.shift(h.workspace('Synthetic live prohibition'), people=None if occupied else [])['workspace']
    if path == 'approve':
        draft = h.lifecycle(draft, 'submit')['workspace']
    job = run(h, draft) if path == 'apply' else None
    control = h.snapshot(draft)
    if policy:
        with transaction(h.app.state.engine, write=True) as db:
            row = db.get(m.Policy, policy['id'])
            row.definition = {**row.definition, 'rule': {**row.definition['rule'], 'maximum': 0}}
            row.revision += 1
            db.get(m.Organisation, h.seed['organisation_id']).input_revision += 1
    else:
        h.policy({'kind': 'max_duties', 'maximum': 0})
    if path == 'check':
        validation = h.lifecycle(draft, 'validate')['validation']
        assert not validation['valid']
        assert any(f['code'] == 'MAX_DUTIES' and f['severity'] == 'error' for f in validation['findings'])
    elif path in {'greedy', 'cp_sat'}:
        generate = cp_sat_generate if path == 'cp_sat' else greedy_generate
        assert generate(control, 2)['additions']
        assert generate(h.snapshot(draft), 2)['additions'] == []
        if path == 'greedy':
            job = h.command('coordinator', 'job.create', {'workspace_id': draft['id'], 'kind': 'generation'},
                            revision=h.detail(draft)['workspace']['revision'])['job']
            assert h.process_jobs()
            with transaction(h.app.state.engine) as db:
                saved = db.get(m.Job, job['id'])
                assert saved.status == 'succeeded' and saved.result['additions'] == []
                assert any(row['definition']['rule'].get('maximum') == 0
                           for row in saved.input_snapshot['policies'])
    elif path == 'apply':
        before = h.snapshot(draft)
        reply = apply(h, draft, job)
        assert reply['proposal_retained'] and reply['reconciliation']['status'] == 'unresolved'
        assert reply['reconciliation']['addition_indexes'] == reply['reconciliation']['removal_indexes'] == []
        assert h.snapshot(draft) == before
        assert h.get('coordinator', '/api/jobs/' + job['id'])['job']['status'] == 'succeeded'
    else:
        role = 'approver' if path == 'approve' else 'coordinator'
        if path == 'approve':
            # Isolate approval's validation gate with a current submission receipt.
            current = h.snapshot(draft)['fingerprint']
            with transaction(h.app.state.engine, write=True) as db:
                db.get(m.Workspace, draft['id']).submitted_fingerprint = current
        reply = h.lifecycle(draft, path, role, status=422)
        assert reply['error']['code'] == 'VALIDATION_FAILED'
    ids = [row['id'] for row in h.snapshot(draft)['policies']]
    assert len(ids) == len(set(ids))


def policy(rule, *, severity='mandatory', revision=1, **fields):
    return {'id': 'limit', 'revision': revision, 'status': 'active', 'active': True,
            'definition': {'severity': severity, 'scope': {}, 'rule': rule}, **fields}


@pytest.mark.parametrize('rule', [
    {'kind': 'max_duties', 'maximum': 0},
    {'kind': 'max_hours', 'hours': 0},
    {'kind': 'max_consecutive_days', 'maximum': 0},
    {'kind': 'max_consecutive_weekends', 'maximum': 0},
    {'kind': 'block_quota', 'metric': 'duties', 'comparison': 'maximum', 'amount': 0},
    {'kind': 'block_quota', 'metric': 'hours', 'comparison': 'range', 'amount': 0, 'maximum_amount': 0},
])
def test_only_mandatory_zero_ceiling_is_live(rule):
    from apps.api.workspace_baselines import _zero_limit_policy

    row = policy(rule)
    assert _zero_limit_policy(row) == row
    assert _zero_limit_policy(policy(rule, severity='advisory')) is None
    positive = {key: 1 if value == 0 else value for key, value in rule.items()}
    assert _zero_limit_policy(policy(positive)) is None


@pytest.mark.parametrize('version,entry', [
    (1, 'duties_per_week'), (1, 'hours_per_week'), (1, 'duties_per_month'),
    (2, 'workload_limit'), (2, 'days_in_a_row'), (2, 'weekends_in_a_row'),
])
def test_catalogue_zero_reader_keeps_only_mandatory_zero_numbers(version, entry):
    from apps.api.workspace_baselines import _zero_limit_policy
    from packages.contracts.rule_catalogue import catalogue_entry, slot_for_number

    contract = catalogue_entry(entry, version)
    numbers = {'max': {'value': 0}}
    if 'target' in contract['numbers']:
        numbers['target'] = {'value': 0, 'level': 'medium'}
    row = policy({'kind': 'catalogue', 'catalogue_version': version, 'id': entry, 'lines': [
        {'id': 'all', 'numbers': numbers},
        {'id': 'specific', 'who': {'conditions': [{'field': 'person_ids', 'values': ['staff']}]},
         'numbers': {'max': {'value': 2}}},
        {'id': 'advisory', 'numbers': {'max': {'value': 0, 'level': 'high'}}},
        {'id': 'other-time', 'weekdays': [0], 'numbers': {'max': {'value': 1}}},
    ]})
    rule = row['definition']['rule']
    kind = slot_for_number(contract, 'max', rule, rule['lines'][0], numbers)['kind']
    for current in rule['lines'][1:]:
        current['settings'] = {kind: {}}
    before = deepcopy(row)
    zero = _zero_limit_policy(row)
    selected, = zero['definition']['rule']['lines']
    assert selected['id'] == 'all' and selected['numbers'] == {'max': {'value': 0}}
    original = row['definition']['rule']['lines'][0]
    assert slot_for_number(contract, 'max', row['definition']['rule'], original, original['numbers'])['kind'] == (
        slot_for_number(contract, 'max', zero['definition']['rule'], selected, selected['numbers'])['kind'])
    assert row == before


@pytest.mark.parametrize('level', ['mandatory', 'medium'])
def test_live_zero_inherits_its_strength_without_carrying_the_donor_limit(level):
    from apps.api.workspace_baselines import _overlay_zero_limits
    from packages.domain.conditions import active_rules
    from .test_rule_catalogue_api import line, person

    broad = line(numbers={'max': {'value': 2, 'level': level}})
    scoped = line(who=person('00000000-0000-4000-8000-000000000001'),
                  numbers={'max': {'value': 0, 'level': None}})
    row = policy({'kind': 'catalogue', 'id': 'duties_per_week', 'lines': [broad, scoped]})
    expanded = active_rules(_overlay_zero_limits([], [row]))
    assert [copy['definition']['rule']['maximum'] for copy in expanded] == ([0] if level == 'mandatory' else [])


def test_zero_balance_tolerance_is_not_a_work_prohibition():
    from apps.api.workspace_baselines import _zero_limit_policy

    assert _zero_limit_policy(policy({'kind': 'catalogue', 'catalogue_version': 2,
        'id': 'spread_evenly', 'lines': [{'id': 'balance', 'numbers': {'max': {'value': 0}}}]})) is None


def test_zero_projection_keeps_only_the_remaining_numbers_source_bindings():
    from apps.api.workspace_baselines import _overlay_zero_limits
    from packages.domain.solver_rules import definition_issues

    ident = '00000000-0000-4000-8000-000000000001'
    row = policy({'kind': 'catalogue', 'catalogue_version': 2, 'id': 'workload_limit',
        'settings': {'block_quota': {'metric': 'duties', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11'}},
        'lines': [{'id': ident, 'numbers': {'max': {'value': 0}, 'target': {'value': 0, 'level': 'medium'}},
                   'source_bindings': {key: {'policy_id': ident, 'revision': 1, 'number_key': key,
                       'primitive_kind': 'block_quota', 'precedence_partition': 'synthetic-source'}
                       for key in ('max', 'target')}}]})
    frozen, live = _overlay_zero_limits([row], [deepcopy(row)])
    for projected, key in ((frozen, 'target'), (live, 'max')):
        line, = projected['definition']['rule']['lines']
        assert set(line['numbers']) == set(line['source_bindings']) == {key}
        assert definition_issues(projected['definition'], historical=True) == []


@pytest.mark.parametrize('same_scope', [False, True])
def test_zero_overlay_preserves_scopes_without_duplicate_ids(same_scope):
    from apps.api.workspace_baselines import _overlay_zero_limits, _zero_limit_policy

    frozen = policy({'kind': 'max_duties', 'maximum': 0})
    frozen['definition']['scope'] = {'person_id': 'staff-a'}
    live = deepcopy(frozen)
    live['revision'] = 2
    live['definition']['scope'] = {'person_id': 'staff-a' if same_scope else 'staff-b'}
    before = deepcopy((frozen, live))
    rows = _overlay_zero_limits([frozen], [live, deepcopy(live)])
    assert len(rows) == (1 if same_scope else 2)
    assert len({row['id'] for row in rows}) == len(rows)
    assert {row['definition']['scope']['person_id'] for row in rows} == (
        {'staff-a'} if same_scope else {'staff-a', 'staff-b'})
    if same_scope:
        assert rows[0]['id'] == frozen['id']
        assert _zero_limit_policy(rows[0]) is not None
    assert (frozen, live) == before


def test_zero_overlay_keeps_frozen_numbers_and_excludes_live_nonzero_caps():
    from apps.api.workspace_baselines import _overlay_zero_limits

    frozen = policy({'kind': 'catalogue', 'catalogue_version': 2, 'id': 'workload_limit', 'lines': [
        {'id': 'all', 'numbers': {'max': {'value': 4}, 'target': {'value': 2, 'level': 'medium'}}}]})
    live = deepcopy(frozen)
    live['revision'] = 2
    live['definition']['rule']['lines'][0]['numbers']['max']['value'] = 0
    rows = _overlay_zero_limits([frozen], [live])
    assert len(rows) == len({row['id'] for row in rows}) == 2
    assert rows[0] == frozen
    assert rows[1]['definition']['rule']['lines'][0]['numbers'] == {'max': {'value': 0}}
    live['definition']['rule']['lines'][0]['numbers']['max']['value'] = 1
    assert _overlay_zero_limits([frozen], [live]) == [frozen]
    frozen['definition']['rule']['lines'][0]['numbers'] = {
        'max': {'value': 0}, 'target': {'value': 0, 'level': 'medium'}}
    rows = _overlay_zero_limits([frozen], [deepcopy(frozen)])
    assert len(rows) == len({row['id'] for row in rows}) == 2
    assert rows[0]['definition']['rule']['lines'][0]['numbers'] == {'target': {'value': 0, 'level': 'medium'}}
    assert rows[1]['definition']['rule']['lines'][0]['numbers'] == {'max': {'value': 0}}


@pytest.mark.parametrize('change', ['retired', 'inactive', 'nonzero'])
def test_zero_overlay_uses_only_the_latest_active_zero_revision(change):
    from apps.api.workspace_baselines import _overlay_zero_limits

    zero = policy({'kind': 'max_duties', 'maximum': 0})
    latest = deepcopy(zero)
    latest['revision'] = 2
    if change == 'retired':
        latest['status'] = 'retired'
    elif change == 'inactive':
        latest['active'] = False
    else:
        latest['definition']['rule']['maximum'] = 1
    assert _overlay_zero_limits([], [latest, zero]) == []
    assert _overlay_zero_limits([zero], [latest]) == [zero]


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('scope_kind', ['rank', 'programme', 'tag'])
def test_live_zero_scope_requires_reason_and_keeps_the_blocking_finding(request, backend, scope_kind):
    h = request.getfixturevalue(backend)
    person = h.seed['people'][0]
    if scope_kind == 'rank':
        scope = {'category_id': h.seed['category']}
    elif scope_kind == 'programme':
        programme = h.configure('programmes', {'name': 'Synthetic programme', 'starts_on': '2020-01-01'})['record']
        h.configure('programme_memberships', {'person_id': person, 'programme_id': programme['id'],
                    'starts_on': '2020-01-01', 'ends_on': '2030-01-01'}, role='coordinator')
        scope = {'programme_id': programme['id']}
    else:
        tag = h.configure('tags', {'name': 'Synthetic tag', 'labels': ['people']})['record']
        h.command('admin', 'tag.members', {'add': [{'kind': 'people', 'id': person}],
                  'reason': 'Synthetic membership'}, tag['id'], tag['revision'])
        scope = {'conditions': {'match': 'all', 'conditions': [
            {'field': 'tags', 'op': 'any', 'values': [tag['id']]}]}}
    draft = h.shift(h.workspace('Synthetic scoped prohibition'), people=[])['workspace']
    h.policy({'kind': 'max_duties', 'maximum': 0, 'window': 'week'}, scope)
    start, end = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'
    payload = {'workspace_id': draft['id'], 'assignment_type_id': h.seed['type'],
               'location_id': h.seed['locations'][0], 'service_ids': [h.seed['services'][0]],
               'start': start, 'end': end, 'attributes': {},
               'participants': [{'person_id': person, 'start': start, 'end': end}]}
    refused = h.command('coordinator', 'shift.create', payload, revision=draft['revision'], status=422)
    assert refused['error']['code'] == 'PERSON_UNAVAILABLE'
    admitted = h.shift(draft, people=[person])
    assert any(f['code'] == 'MAX_DUTIES' and f['severity'] == 'error'
               for f in h.detail(admitted['workspace'])['workspace']['validation']['findings'])


def overtime_draft(h):
    from .test_overtime_extra_cover import extra_type

    duty_type = extra_type(h)
    person = h.seed['people'][0]
    h.policy({'kind': 'allocation_overtime'}, {'person_id': person}, severity='advisory')
    draft = h.shift(h.workspace('Synthetic overtime cap'), typ=duty_type['id'], people=[])['workspace']
    draft = h.shift(draft, typ=duty_type['id'], people=[], start='2026-10-05T08:00:00+03:00',
                    end='2026-10-05T12:00:00+03:00')['workspace']
    return draft, person


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_nonzero_live_overtime_cap_stays_out(request, backend):
    from apps.api.snapshots import domain_snapshot

    h = request.getfixturevalue(backend)
    draft, person = overtime_draft(h)
    cap = h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
                    'metric': 'duties', 'comparison': 'maximum', 'amount': 1, 'overtime': 'only'},
                   {'person_id': person})
    with transaction(h.app.state.engine) as db:
        apply_data = domain_snapshot(db, db.get(m.Workspace, draft['id']), mandatory_rules=True)
    data = h.snapshot(draft)
    assert all(row['id'] != cap['id'] for row in data['policies'])
    assert all(row['id'] != cap['id'] for row in apply_data['policies'])
    data['people'] = {person: data['people'][person]}
    for generate in (greedy_generate, cp_sat_generate):
        result = generate(deepcopy(data), 2)
        assert len(result['additions']) == 2 and result['validation']['valid']


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize('metric', ['duties', 'scheduled_hours'])
def test_zero_live_overtime_cap_binds_both_solvers(h, metric):
    draft, person = overtime_draft(h)
    before = h.snapshot(draft)
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
              'metric': metric, 'comparison': 'maximum', 'amount': 0, 'overtime': 'only'}, {'person_id': person})
    after = h.snapshot(draft)
    for generate in (greedy_generate, cp_sat_generate):
        assert len(generate(empty_for_person(before, person), 2)['additions']) == 2
        assert generate(empty_for_person(after, person), 2)['additions'] == []


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_more_specific_nonzero_live_line_cannot_loosen_zero(request, backend):
    from .test_rule_catalogue_api import approve, line, person, propose

    h = request.getfixturevalue(backend)
    draft = h.shift(h.workspace('Synthetic scoped zero line'), people=h.seed['people'][:2])['workspace']
    everyone = line(numbers={'max': {'value': 0}})
    specific = line(who=person(h.seed['people'][0]), numbers={'max': {'value': 2}})
    rule = approve(h, propose(h, 'duties_per_week', [everyone, specific],
                             acknowledged=[everyone['id']])['record'])
    snapshot = h.snapshot(draft)
    carried = next(row for row in snapshot['policies'] if row['id'] == rule['id'])
    assert len(carried['definition']['rule']['lines']) == 1
    assert carried['definition']['rule']['lines'][0]['numbers']['max']['value'] == 0
    validation = h.lifecycle(draft, 'validate')['validation']
    blocked = {f['person_id'] for f in validation['findings'] if f['code'] == 'MAX_DUTIES' and f['severity'] == 'error'}
    assert set(h.seed['people'][:2]) <= blocked
    for generate in (greedy_generate, cp_sat_generate):
        empty = h.snapshot(draft)
        empty['shifts'] = [{**row, 'participants': []} for row in empty['shifts']]
        empty['people'] = {pid: empty['people'][pid] for pid in h.seed['people'][:2]}
        result = generate(empty, 2)
        assert result['additions'] == []
    from .test_assignment_override_reason import payload
    draft = h.detail(draft)['workspace']
    value = payload(h, draft, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00')
    for pid in h.seed['people'][:2]:
        refused = h.command('coordinator', 'shift.create', {**value, 'participants': [
            {**value['participants'][0], 'person_id': pid}]}, revision=draft['revision'], status=422)
        assert refused['error']['code'] == 'PERSON_UNAVAILABLE'


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('rule', [{'kind': 'max_hours', 'hours': 0}, {'kind': 'max_consecutive_days', 'maximum': 0}])
def test_zero_hours_and_consecutive_limits_bind_existing_assignments(request, backend, rule):
    h = request.getfixturevalue(backend)
    draft = h.shift(h.workspace('Synthetic zero ceiling'))['workspace']
    legacy = rule['kind'] == 'max_consecutive_days'
    zero = h.policy({**rule, 'maximum': 1} if legacy else rule)
    if legacy:
        # Seed a stored legacy zero without changing authoring limits.
        with transaction(h.app.state.engine, write=True) as db:
            row = db.get(m.Policy, zero['id'])
            row.definition = {**row.definition, 'rule': rule}
            row.revision += 1
            db.get(m.Organisation, h.seed['organisation_id']).input_revision += 1
    validation = h.lifecycle(draft, 'validate')['validation']
    assert not validation['valid']
    assert any(f['rule_id'] == zero['id'] and f['severity'] == 'error' for f in validation['findings'])


def tighten(h, source, rule, **fields):
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Policy, source['id'])
        prior = row.definition['rule']
        row.definition = {**row.definition, 'severity': 'mandatory',
                          'rule': {**prior, **rule} if prior['kind'] == rule['kind'] else rule}
        for key, value in fields.items():
            setattr(row, key, value)
        row.revision += 1
        db.get(m.Organisation, h.seed['organisation_id']).input_revision += 1


def empty_for_person(data, person_id):
    data = deepcopy(data)
    data['planning_population'] = {row['id']: [person_id] for row in data['shifts']}
    data['shifts'] = [{**row, 'participants': []} for row in data['shifts']]
    return data


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_live_zero_copy_preserves_approved_exception_identity_and_validity(h):
    source = h.policy({'kind': 'max_duties', 'maximum': 2, 'window': 'day'}, exception=True)
    draft = h.shift(h.workspace('Synthetic exception copy'))['workspace']
    tighten(h, source, {'kind': 'max_duties', 'maximum': 0, 'window': 'day'})
    before = h.snapshot(draft)
    finding, = [f for f in evaluate(before)['findings'] if f['code'] == 'MAX_DUTIES']
    assert finding['rule_id'] == source['id']
    assert 'current department rule' in finding['message'] and source['id'] in finding['message']
    assert finding['detail']['live_zero'] is True
    exc = h.command('coordinator', 'exception.create', {'policy_id': source['id'],
        'person_id': h.seed['people'][0], 'starts_on': '2026-10-04', 'ends_on': '2026-10-05',
        'reason': 'Synthetic approved workload exception'})['record']
    approved = h.command('approver', 'exception.approve', {'reason': 'Synthetic independent decision'},
                         exc['id'], exc['revision'])['record']
    data = h.snapshot(draft)
    live, = [p for p in data['policies'] if p.get('_live_zero')]
    assert live['id'] != source['id'] and live['_source_policy_id'] == source['id']
    assert any(row['policy_id'] == live['id'] and row['policy_revision'] == live['revision']
               and row['id'] == approved['id'] for row in data['exceptions'])
    for changes, allowed in [({}, True), ({'status': 'proposed'}, False),
                             ({'policy_revision': live['revision'] - 1}, False),
                             ({'starts_on': '2026-10-05'}, False), ({'ends_on': '2026-10-04'}, False)]:
        trial = deepcopy(data)
        trial['exceptions'] = [{**row, **changes} for row in trial['exceptions']]
        findings = evaluate(trial)['findings']
        assert any(f['code'] == 'MAX_DUTIES' and f['rule_id'] == source['id'] for f in findings) is not allowed
        if allowed:
            assert any(f['code'] == 'RULE_EXCEPTION' and f['rule_id'] == source['id']
                       and f['detail']['exception_id'] == approved['id'] for f in findings)
        for generate in (greedy_generate, cp_sat_generate):
            assert bool(generate(empty_for_person(trial, h.seed['people'][0]), 2)['additions']) is allowed
    from .test_assignment_override_reason import payload
    draft = h.detail(draft)['workspace']
    shift = h.detail(draft)['shifts'][0]
    draft = h.command('coordinator', 'shift.update', payload(h, draft, shift, people=[]),
                      revision=draft['revision'])['workspace']
    saved = h.command('coordinator', 'shift.update', payload(h, draft, shift), revision=draft['revision'])
    assert not any(f['code'] == 'MAX_DUTIES' for f in saved['validation']['findings'])


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_rota_adjustment_reaches_live_zero_copy(h):
    source = h.policy({'kind': 'max_duties', 'maximum': 2})
    draft = h.shift(h.workspace('Synthetic adjusted zero copy'))['workspace']
    tighten(h, source, {'kind': 'max_duties', 'maximum': 0})
    assert any(f['code'] == 'MAX_DUTIES' for f in evaluate(h.snapshot(draft))['findings'])
    draft = h.command('coordinator', 'workspace.rule_adjust', {'policy_id': source['id'],
        'mode': 'set', 'amount': 2, 'reason': 'Synthetic rota workload adjustment'},
        draft['id'], draft['revision'])['workspace']
    data = h.snapshot(draft)
    live, = [row for row in data['policies'] if row.get('_live_zero')]
    assert live['rota_adjusted'] and live['definition']['rule']['maximum'] == 2
    assert not any(f['code'] == 'MAX_DUTIES' for f in evaluate(data)['findings'])
    for generate in (greedy_generate, cp_sat_generate):
        assert generate(empty_for_person(data, h.seed['people'][0]), 2)['additions']


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_apply_recheck_keeps_approved_adjustment_for_live_replacement(h):
    from apps.api.snapshots import domain_snapshot

    source = h.policy({'kind': 'max_duties', 'maximum': 0})
    draft = h.shift(h.workspace('Synthetic adjusted prohibition'), people=[])['workspace']
    tighten(h, source, {'kind': 'max_duties', 'maximum': 1})
    draft = h.command('coordinator', 'workspace.rule_adjust', {'policy_id': source['id'],
        'mode': 'set', 'amount': 2, 'reason': 'Synthetic adjustment for the tested rota'},
        draft['id'], draft['revision'])['workspace']
    with transaction(h.app.state.engine) as db:
        data = domain_snapshot(db, db.get(m.Workspace, draft['id']), mandatory_rules=True)
    limits = [row for row in data['policies'] if row.get('_source_policy_id', row['id']) == source['id']]
    assert len(limits) == 2
    assert all(row['rota_adjusted'] and row['definition']['rule']['maximum'] == 2 for row in limits)


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_scoped_rota_adjustment_reaches_live_zero_only_for_its_person_and_dates(h):
    from .test_rule_catalogue_api import approve, line, propose

    authored = line(numbers={'max': {'value': 2}})
    source = approve(h, propose(h, 'duties_per_week', [authored])['record'])
    draft = h.shift(h.workspace('Synthetic dated rota adjustment'), people=[])['workspace']
    rule = deepcopy(source['definition']['rule'])
    rule['lines'][0]['numbers']['max']['value'] = 0
    tighten(h, source, rule)
    draft = h.command('coordinator', 'workspace.rule_adjust', {'policy_id': source['id'],
        'line_id': authored['id'], 'number': 'max', 'person_id': h.seed['people'][0],
        'starts_on': '2026-10-05', 'ends_on': '2026-10-06', 'mode': 'set', 'amount': 2,
        'reason': 'Synthetic person and period adjustment'}, draft['id'], draft['revision'])['workspace']
    data = h.snapshot(draft)
    for day in ('04', '05', '06'):
        for pid in h.seed['people'][:2]:
            trial = deepcopy(data)
            shift = trial['shifts'][0]
            start, end = f'2026-10-{day}T08:00:00+03:00', f'2026-10-{day}T12:00:00+03:00'
            shift.update(start=start, end=end, participants=[{'id': 'synthetic-assignment', 'person_id': pid,
                'shift_id': shift['id'], 'start': start, 'end': end, 'participation': 'staff', 'overtime': False}])
            allowed = day == '05' and pid == h.seed['people'][0]
            assert any(f['code'] == 'MAX_DUTIES' and f['rule_id'] == source['id']
                       for f in evaluate(trial)['findings']) is not allowed
            for generate in (greedy_generate, cp_sat_generate):
                assert bool(generate(empty_for_person(trial, pid), 2)['additions']) is allowed


@pytest.mark.parametrize('entry,kind', [('days_in_a_row', 'max_consecutive_days'),
                                       ('weekends_in_a_row', 'max_consecutive_weekends')])
def test_stored_zero_consecutive_catalogue_does_not_change_authoring_limits(entry, kind):
    from pydantic import ValidationError
    from packages.contracts.schemas import PolicyDefinition
    from packages.domain.conditions import active_rules
    from packages.domain.solver_rules import definition_issues

    with pytest.raises(ValidationError):
        PolicyDefinition.model_validate({'rule': {'kind': kind, 'maximum': 0}})
    rule = {'kind': 'catalogue', 'catalogue_version': 2, 'id': entry, 'lines': [
        {'id': '00000000-0000-4000-8000-000000000001', 'numbers': {'max': {'value': 0}}}]}
    with pytest.raises(ValidationError):
        PolicyDefinition.model_validate({'rule': rule})
    expanded, = active_rules([policy(rule)])
    assert expanded['definition']['rule']['kind'] == kind
    assert expanded['definition']['rule']['maximum'] == 0
    assert definition_issues({'rule': rule})
    assert definition_issues({'rule': rule}, historical=True) == []
    assert definition_issues(expanded['definition'])
    assert definition_issues(expanded['definition'], historical=True) == []


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_target_override_changes_captured_target_but_cannot_waive_live_zero(h):
    rule = {'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
            'metric': 'duties', 'comparison': 'target', 'amount': 2, 'prorate_department_days': True,
            'reference_days': 7}
    source = h.policy(rule, severity='advisory')
    draft = h.shift(h.workspace('Synthetic target and ceiling'))['workspace']
    draft = h.command('coordinator', 'workspace.target_override', {'policy_id': source['id'],
        'person_id': h.seed['people'][0], 'amount': 1, 'reason': 'Synthetic target adjustment'},
        draft['id'], draft['revision'])['workspace']
    tighten(h, source, {**rule, 'comparison': 'maximum', 'amount': 0})
    data = h.snapshot(draft)
    live, = [row for row in data['policies'] if row.get('_live_zero')]
    report = evaluate(data)['quota_report']
    assert any(row['comparison'] == 'target' and row['person_id'] == h.seed['people'][0]
               and row['amount'] == 1 for row in report['rows'])
    # Even a stored row keyed to the transient revision cannot turn a target override into a limit waiver.
    data['workspace']['target_overrides'].append({**data['workspace']['target_overrides'][0],
        'policy_id': live['id'], 'policy_revision': live['revision'], 'amount': 2})
    assert any(f['code'] == 'QUOTA_ABOVE_MAXIMUM' and f['rule_id'] == source['id']
               and 'current department rule' in f['message'] for f in evaluate(data)['findings'])
    for generate in (greedy_generate, cp_sat_generate):
        assert generate(empty_for_person(data, h.seed['people'][0]), 2)['additions'] == []


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize('kind', ['max_duties', 'max_hours', 'max_consecutive_days',
                                 'max_consecutive_weekends', 'range', 'catalogue', 'days_in_a_row'])
def test_live_zero_dates_and_synchronous_edit_refusal_use_the_shared_reader(h, kind):
    from datetime import date
    from .test_assignment_override_reason import payload
    from .test_perf5_edit_reply import worker_up

    draft = h.shift(h.workspace('Synthetic dated ceiling'), people=[])['workspace']
    if kind == 'range':
        rule = {'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
                'metric': 'duties', 'comparison': 'range', 'amount': 0, 'maximum_amount': 0}
    elif kind in {'catalogue', 'days_in_a_row'}:
        from .test_rule_catalogue_api import approve, line, propose
        source = approve(h, propose(h, 'duties_per_week' if kind == 'catalogue' else kind,
            [line(numbers={'max': {'value': 1}})], catalogue_version=1 if kind == 'catalogue' else 2)['record'])
        rule = deepcopy(source['definition']['rule'])
        rule['lines'][0]['numbers']['max']['value'] = 0
    else:
        rule = {'kind': kind, 'hours' if kind == 'max_hours' else 'maximum': 0}
        if kind == 'max_consecutive_weekends':
            rule.update(weekend_start=0, weekend_days=1)
    if kind not in {'catalogue', 'days_in_a_row'}:
        source = h.policy({**rule, 'maximum': 1} if kind.startswith('max_consecutive') else rule)
    # Consecutive zeros remain stored legacy input; authoring still requires a positive maximum.
    tighten(h, source, rule, starts_on=date(2026, 10, 5), ends_on=date(2026, 10, 6))
    # The synchronous gate must refuse before any deferred full validation can run.
    worker_up(h)
    value = payload(h, draft, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00')
    before = h.detail(draft)
    refused = h.command('coordinator', 'shift.create', value, revision=draft['revision'], status=422)
    assert refused['error']['code'] == 'PERSON_UNAVAILABLE'
    assert 'current department rule' in refused['error']['message']
    assert any(f['rule_id'] == source['id'] and 'current department rule' in f['message']
               for f in refused['error']['details']['findings'])
    assert h.detail(draft) == before
    data = h.snapshot(draft)
    pid = h.seed['people'][0]
    shift = data['shifts'][0]
    for day, blocked in [('04', False), ('05', True), ('06', False)]:
        trial = deepcopy(data)
        start, end = f'2026-10-{day}T08:00:00+03:00', f'2026-10-{day}T12:00:00+03:00'
        trial['shifts'] = [{**shift, 'start': start, 'end': end, 'participants': [
            {'id': 'synthetic-assignment', 'person_id': pid, 'shift_id': shift['id'],
             'start': start, 'end': end, 'participation': 'staff', 'overtime': False}]}]
        assert any(f['rule_id'] == source['id'] and f['severity'] == 'error'
                   for f in evaluate(trial)['findings']) is blocked
        for generate in (greedy_generate, cp_sat_generate):
            result = generate(empty_for_person(trial, pid), 2)
            assert bool(result['additions']) is not blocked, (kind, day, generate.__module__, result)


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize('timing', ['before_capture', 'after_capture'])
def test_seeded_zero_default_binds_without_duplicate_obligations(h, timing):
    from .test_rule_catalogue_api import approve, line, propose
    from packages.domain.conditions import active_rules

    source = approve(h, propose(h, 'duties_per_week', [line(numbers={'max': {'value': 4}})])['record'])

    def seed_zero():
        with transaction(h.app.state.engine, write=True) as db:
            db.add(m.PolicySeedLine(department_id=h.seed['department_id'], policy_id=source['id'],
                policy_revision=source['revision'], line=line(numbers={'max': {'value': 0}})))
            db.get(m.Organisation, h.seed['organisation_id']).input_revision += 1

    if timing == 'before_capture':
        seed_zero()
    draft = h.shift(h.workspace('Synthetic seeded zero rule'))['workspace']
    if timing == 'after_capture':
        seed_zero()
    data = h.snapshot(draft)
    zeros = [row for row in active_rules(data['policies'], context=data)
             if row['definition']['rule'].get('maximum') == 0]
    assert len(zeros) == 1
    findings = evaluate(data)['findings']
    assert any(f['code'] == 'MAX_DUTIES' and f['rule_id'] == source['id'] for f in findings)
    assert h.lifecycle(draft, 'submit', status=422)['error']['code'] == 'VALIDATION_FAILED'
    for generate in (greedy_generate, cp_sat_generate):
        assert generate(empty_for_person(data, h.seed['people'][0]), 2)['additions'] == []


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_apply_checks_both_the_positive_and_zero_limits_in_a_live_revision(request, backend):
    from .test_generation_superseded_apply import group
    from .test_rule_catalogue_api import approve, line, person, propose

    h = request.getfixturevalue(backend)
    positive_person, zero_person = h.seed['people'][:2]
    general = line(numbers={'max': {'value': 5}})
    source = approve(h, propose(h, 'duties_per_week', [general])['record'])
    draft = h.shift(h.workspace('Synthetic mixed duty limits'), people=[positive_person])['workspace']
    draft = h.shift(draft, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00',
                    people=[positive_person])['workspace']
    positive_slot = h.shift(draft, '2026-10-06T08:00:00+03:00', '2026-10-06T12:00:00+03:00', people=[])
    zero_slot = h.shift(positive_slot['workspace'], '2026-10-07T08:00:00+03:00', '2026-10-07T12:00:00+03:00', people=[])
    draft = zero_slot['workspace']
    zero_proposal = run(h, draft, sessions=[zero_slot['shift_id']], population=group(zero_person))
    tighten(h, source, {**source['definition']['rule'], 'lines': [
        {**general, 'numbers': {'max': {'value': 2}}},
        line(who=person(zero_person), numbers={'max': {'value': 0}}),
    ]})
    positive_proposal = run(h, draft, sessions=[positive_slot['shift_id']], population=group(positive_person))
    before = h.snapshot(draft)
    for job, pid, maximum in ((positive_proposal, positive_person, 2), (zero_proposal, zero_person, 0)):
        receipt = apply(h, draft, job)['reconciliation']
        assert receipt['status'] == 'unresolved'
        assert receipt['addition_indexes'] == []
        assert any(f['code'] == 'MAX_DUTIES' and f['rule_id'] == source['id']
                   and f['person_id'] == pid and f['detail']['maximum'] == maximum
                   for placement in receipt['conflicting_placements'] for f in placement['findings'])
        assert h.snapshot(draft) == before
