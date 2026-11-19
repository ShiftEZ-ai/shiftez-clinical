"""Versioned score settings and the complete advisory-line score review read."""
import pytest
from sqlalchemy import event, func, select

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s


def _version_count(h):
    with transaction(h.app.state.engine) as db:
        return db.scalar(select(func.count()).select_from(m.ScoreSettingsVersion)
                         .where(m.ScoreSettingsVersion.department_id == h.seed['department_id']))

import pytest


def test_score_settings_are_versioned_projected_and_undoable(h):
    current = h.get('admin', '/api/score-settings')
    assert current['definition']['strengths'] == {'low': 1, 'medium': 3, 'high': 10, 'critical': 30}
    changed_reply = h.command('admin', 'score.settings', {
        'definition': {'weights': {'coverage': 35, 'rules': 25, 'fairness': 40, 'preferences': 10, 'stability': 5},
                       'strengths': {'low': 1, 'medium': 4, 'high': 12, 'critical': 30}},
        'reason': 'Calibrate advisory tiers for score review'},
        h.seed['department_id'], current['settings']['revision'])
    changed = changed_reply['settings']
    assert changed['revision'] == current['settings']['revision'] + 1
    assert h.get('admin', '/api/me')['department']['generation_defaults']['scoring']['strengths']['medium'] == 4
    history = h.get('admin', '/api/score-settings?history=true')
    assert [row['revision'] for row in history['versions'][:2]] == [changed['revision'], current['settings']['revision']]
    restored = h.command('admin', 'score.settings.rollback',
        {'version_id': changed_reply['undo']['payload']['version_id'], 'reason': 'Undo score calibration'},
        h.seed['department_id'], changed['revision'])['settings']
    assert restored['definition'] == current['definition']


def test_score_review_lists_zero_and_nonzero_advisory_lines(h):
    workspace = h.shift(h.workspace('Score review read'), people=[h.seed['people'][0]])['workspace']
    h.command('coordinator', 'workspace.validate', {'reason': 'Build score review evidence'},
              workspace['id'], workspace['revision'])
    result = h.get('coordinator', f"/api/workspaces/{workspace['id']}/score-review")
    assert result['score']['score'] is not None
    assert result['total_lost_points'] == round(100 - result['score']['score'], 1)
    assert all({'rule_id', 'level', 'weight', 'lost_points', 'lost_share', 'session_count', 'people_count'} <= set(row)
               for row in result['lines'])


def test_score_review_reuses_current_validation_without_changing_payload(h, monkeypatch):
    from apps.api.services import score_review as service
    from packages.contracts import schemas as s
    from packages.domain.score import score_review_lines, schedule_score
    from packages.domain.validation import evaluate

    workspace = h.shift(h.workspace('Score review reuse'), people=[h.seed['people'][0]])['workspace']
    workspace = h.command('coordinator', 'workspace.validate', {'reason': 'Build reusable score review evidence'},
                          workspace['id'], workspace['revision'])['workspace']
    data = h.snapshot(workspace)
    validation = evaluate(data, explain_findings=False)
    scored = schedule_score(data, validation, data.get('generation_options'))
    expected_lines = score_review_lines(data, validation, data.get('generation_options'), scored)
    expected = s.ScoreReview(workspace_id=workspace['id'], workspace_revision=workspace['revision'],
                             fingerprint=data['fingerprint'], score=expected_lines['score'],
                             total_lost_points=expected_lines['total_lost_points'], lines=expected_lines['lines'])

    monkeypatch.setattr(service, 'evaluate', lambda *args, **kwargs: pytest.fail('reused current validation must not evaluate'))
    monkeypatch.setattr(service, 'schedule_score', lambda *args, **kwargs: pytest.fail('reused current validation must not rescore'))
    assert h.get('coordinator', f"/api/workspaces/{workspace['id']}/score-review") == expected.model_dump(mode='json')


@pytest.mark.parametrize('snapshot_kind', ['dict', 'domain_snapshot'])
def test_retired_score_review_context_cache_preserves_payload_and_bounds_calls(monkeypatch, snapshot_kind):
    from copy import deepcopy
    from apps.api.snapshots import DomainSnapshot
    from packages.domain import fairness
    from packages.domain.score import score_review_lines, schedule_score
    from packages.domain.validation import evaluate
    from tests.test_schedule_score_scale import department

    data = department()
    if snapshot_kind == 'domain_snapshot':
        data = DomainSnapshot(data)
    template = deepcopy(data['policies'][0])
    for number in range(1300):
        retired = deepcopy(template)
        retired.update(id=f'retired-score-review-{number}', name=f'Retired score rule {number}',
                       revision=number + 1, status='retired', active=False)
        data['policies'].append(retired)
    validation = evaluate(data, explain_findings=False)
    scored = schedule_score(data, validation, data.get('generation_options'))
    baseline = score_review_lines(deepcopy(data), deepcopy(validation), data.get('generation_options'), deepcopy(scored))

    real_goal_applies = fairness.goal_applies
    def uncached_goal_applies(value, *args, **kwargs):
        if hasattr(value, '_fair_share_measurement_cache'):
            del value._fair_share_measurement_cache
        cache = value.get('_line_index_cache')
        if hasattr(cache, 'context'):
            cache.context = cache.key = None
        return real_goal_applies(value, *args, **kwargs)

    with monkeypatch.context() as uncached:
        uncached.setattr(fairness, 'goal_applies', uncached_goal_applies)
        reference = score_review_lines(deepcopy(data), deepcopy(validation), data.get('generation_options'), deepcopy(scored))
    assert baseline == reference

    calls = []
    contexts = []
    real_measurement_context = fairness.measurement_context
    def counted_measurement_context(value):
        calls.append(id(value))
        context = real_measurement_context(value)
        contexts.append(context)
        return context

    monkeypatch.setattr(fairness, 'measurement_context', counted_measurement_context)
    score_review_lines(deepcopy(data), deepcopy(validation), data.get('generation_options'), deepcopy(scored))
    context_counts = {}
    for context in contexts:
        context_counts[id(context)] = context_counts.get(id(context), 0) + 1
    assert calls and len(context_counts) <= 2 and max(context_counts.values()) > 1, (len(calls), len(context_counts))


@pytest.mark.parametrize('snapshot_kind', ['dict', 'domain_snapshot'])
@pytest.mark.parametrize('opt_in', [False, True])
def test_measurement_context_runtime_cache_stays_out_of_snapshot_payload(snapshot_kind, opt_in):
    from copy import deepcopy
    import pickle
    from apps.api.snapshots import DomainSnapshot
    from packages.domain.fairness import measurement_context
    from packages.domain.input_manifest import manifest
    from packages.domain.serialization import json_value
    from tests.test_schedule_score_scale import department

    snapshot = deepcopy(department())
    if snapshot_kind == 'domain_snapshot':
        snapshot = DomainSnapshot(snapshot, read_quality={'diagnostics': []})
    if opt_in:
        snapshot['_line_index_cache'] = {}
    before = json_value(snapshot)
    token = manifest(snapshot)['token']
    context = measurement_context(snapshot)

    assert manifest(snapshot)['token'] == token
    assert json_value(snapshot) == before
    assert '_fair_share_measurement_context' not in snapshot
    assert '_fair_share_measurement_context_key' not in snapshot
    if snapshot_kind == 'domain_snapshot':
        assert context.read_quality is snapshot.read_quality
    if snapshot_kind == 'domain_snapshot' or opt_in:
        assert measurement_context(snapshot) is context
        restored = pickle.loads(pickle.dumps(snapshot))
        cache = (restored._fair_share_measurement_cache if snapshot_kind == 'domain_snapshot'
                 else restored['_line_index_cache'])
        assert cache.context is None and cache.key is None
        assert json_value(restored) == before
        assert measurement_context(restored) is not context


def test_measurement_context_cache_keeps_independent_input_views():
    from packages.domain.fairness import measurement_context
    from tests.test_schedule_score_scale import department

    snapshot = {**department(), '_line_index_cache': {}}
    first = measurement_context(snapshot)
    other = {**snapshot, 'generation_options': {'balance_measure': 'duties'}}
    second = measurement_context(other)
    assert id(second) != id(first)
    assert id(measurement_context(snapshot)) == id(first)
    assert id(measurement_context(other)) == id(second)
    assert second['generation_options'] is other['generation_options']


@pytest.mark.parametrize('source', ['policies', 'rotations', 'shifts', 'memberships', 'people',
                                   'generation_options', 'workspace'])
def test_measurement_context_cache_rebuilds_after_input_replacement(source):
    from copy import deepcopy
    from apps.api.snapshots import DomainSnapshot
    from packages.domain.fairness import measurement_context
    from tests.test_schedule_score_scale import department

    snapshot = DomainSnapshot(department())
    first = measurement_context(snapshot)
    assert measurement_context(snapshot) is first
    snapshot[source] = deepcopy(snapshot.get(source, {}))
    if source == 'workspace':
        snapshot[source]['ends_on'] = '2026-11-01'
    second = measurement_context(snapshot)
    assert second is not first
    assert measurement_context(snapshot) is second
    if source != 'policies':
        assert second[source] is snapshot[source]


@pytest.mark.parametrize('invalid', ['fingerprint', 'score', 'scope'])
def test_score_review_recomputes_unusable_validation(h, monkeypatch, invalid):
    from copy import deepcopy
    from apps.api import models as m
    from apps.api.db import transaction
    from apps.api.services import score_review as service

    workspace = h.shift(h.workspace('Score review fallback'), people=[h.seed['people'][0]])['workspace']
    workspace = h.command('coordinator', 'workspace.validate', {'reason': 'Build score review evidence'},
                          workspace['id'], workspace['revision'])['workspace']
    url = f"/api/workspaces/{workspace['id']}/score-review"
    expected = h.get('coordinator', url)
    with transaction(h.app.state.engine, write=True) as db:
        stored = db.get(m.Workspace, workspace['id'])
        check = deepcopy(stored.validation)
        if invalid == 'fingerprint':
            check['fingerprint'] = 'stale'
        elif invalid == 'score':
            check['score'].pop('rule_weights')
        else:
            check['scope'] = 'command_changes'
        stored.validation = check
    calls = []
    for name in ('evaluate', 'schedule_score'):
        original = getattr(service, name)

        def counted(*args, _name=name, _original=original, **kwargs):
            calls.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(service, name, counted)
    assert h.get('coordinator', url) == expected
    assert calls == ['evaluate', 'schedule_score']


def test_score_review_attributes_grouped_fair_share_loss_to_the_source_line(monkeypatch):
    from packages.domain import score
    from packages.domain import fairness

    monkeypatch.setattr(score, 'active_rules', lambda policies, context=None: policies)
    monkeypatch.setattr(score, 'counting_views', lambda data, rules: rules)
    monkeypatch.setattr(fairness, 'goal_people', lambda data, policy: {'who': [], 'partners': []})

    source = {
        'id': 'source-policy', 'name': 'Fair share line', 'status': 'active', 'active': True,
        'department_id': 'd', 'starts_on': '2020-01-01', 'ends_on': None,
        'template_line': {'id': 'source-line', 'line': 2, 'slot': 'apply'},
        'definition': {'severity': 'advisory', 'strength': 'high', 'scope': {},
                       'rule': {'kind': 'balance_goal', 'metric': 'duties'}},
    }
    data = {'policies': [source], 'focus_ids': [], 'shifts': [], 'people': {}, 'memberships': [],
            'workspace': {'department_id': 'd', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01'},
            'organisation': {'timezone': 'UTC'}, 'generation_options': {}}
    scored = {'contributions': [{'component': 'fairness', 'points': 4.0, 'rule_id': 'comparison-policy',
                                 'rule_source_id': 'source-policy', 'line_id': 'source-line', 'line': 2,
                                 'slot': 'apply', 'rule_strength': 'high', 'rule_weight': 10}]}

    result = score.score_review_lines(data, {'findings': []}, scored=scored)

    assert len(result['lines']) == 1
    assert result['lines'][0]['rule_id'] == 'source-policy'
    assert result['lines'][0]['lost_points'] == 4.0
    assert result['lines'][0]['component'] == 'fairness'


def test_score_review_counts_quota_audience_without_a_current_assignment(monkeypatch):
    from packages.domain import score

    monkeypatch.setattr(score, 'active_rules', lambda policies, context=None: policies)
    monkeypatch.setattr(score, 'counting_views', lambda data, rules: rules)

    quota = {
        'id': 'quota-policy', 'name': 'Monthly quota', 'status': 'active', 'active': True,
        'department_id': 'd', 'starts_on': '2020-01-01', 'ends_on': None,
        'template_line': {'id': 'quota-line', 'line': 0, 'slot': 'target'},
        'definition': {'severity': 'advisory', 'strength': 'medium', 'scope': {},
                       'rule': {'kind': 'block_quota'}},
    }
    data = {'policies': [quota], 'focus_ids': [], 'shifts': [], 'people': {}, 'memberships': [],
            'workspace': {'department_id': 'd', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01'},
            'organisation': {'timezone': 'UTC'}, 'generation_options': {}}
    validation = {'findings': [], 'quota_report': {'rows': [{'policy_id': 'quota-policy', 'line_id': 'quota-line',
                   'line': 0, 'slot': 'target', 'person_id': 'person-without-assignment'}]}}

    result = score.score_review_lines(data, validation, scored={'contributions': []})

    assert result['lines'][0]['people_count'] == 1


def test_an_admin_only_account_reads_the_score_settings_it_changes(h):
    from .governance_support import identity
    identity(h, 'score_admin_only', ['admin'])
    assert h.get('score_admin_only', '/api/score-settings')['definition']['strengths']['medium'] >= 1
    assert h.get('score_admin_only', '/api/score-settings?history=true')['versions']


@pytest.mark.parametrize('h', ['sqlite', 'postgresql'], indirect=True)
@pytest.mark.parametrize('revision', [None, 1])
def test_post_migration_department_projects_then_stores_and_undoes_v1(h, revision):
    assert _version_count(h) == 0
    current = h.get('admin', '/api/score-settings')
    assert _version_count(h) == 0
    assert current['settings']['revision'] == 1
    assert current['settings']['stored'] is False
    assert current['settings']['id'] is None
    assert current['settings']['definition'] == current['definition']
    assert h.get('admin', '/api/score-settings?history=true')['versions'][0]['stored'] is False
    assert h.get('admin', '/api/score-settings?history=true&offset=1')['versions'] == []
    assert _version_count(h) == 0

    changed_reply = h.command('admin', 'score.settings', {
        'definition': {**current['definition'],
                       'strengths': {'low': 1, 'medium': 4, 'high': 12, 'critical': 30}},
        'reason': 'Calibrate tiers'}, h.seed['department_id'], revision)
    assert changed_reply['settings']['revision'] == 2
    assert _version_count(h) == 2
    baseline = h.get('admin', '/api/score-settings?history=true')['versions'][1]
    assert baseline['definition'] == current['definition']
    assert baseline['id'] == changed_reply['undo']['payload']['version_id']
    assert h.get('admin', '/api/score-settings?history=true&limit=1')['has_more'] is True
    assert h.get('admin', '/api/score-settings?history=true&offset=2')['versions'] == []

    restored = h.command('admin', 'score.settings.rollback',
                         {'version_id': changed_reply['undo']['payload']['version_id'],
                          'reason': 'Undo score calibration'},
                         h.seed['department_id'], changed_reply['settings']['revision'])['settings']
    assert restored['definition'] == current['definition']
    assert _version_count(h) == 3


@pytest.mark.parametrize('role', s.ROLE_NAMES)
def test_score_settings_read_roles_keep_authorization_and_read_is_write_free(h, role):
    from .governance_support import identity
    username = f'score_role_{role}'
    identity(h, username, [role])
    h.client(username)
    before = h.get('admin', '/api/score-settings?history=true')['versions']
    allowed = role in s.PERMISSIONS['planning.read'] or role in s.PERMISSIONS['planning.configure']
    writes = []

    def record_write(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().split()[0].upper() in {'INSERT', 'UPDATE', 'DELETE'}:
            writes.append(statement)

    event.listen(h.app.state.engine, 'before_cursor_execute', record_write)
    try:
        h.get(username, '/api/score-settings', status=200 if allowed else 403)
        h.get(username, '/api/score-settings?history=true', status=200 if allowed else 403)
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', record_write)
    after = h.get('admin', '/api/score-settings?history=true')['versions']
    assert [row['revision'] for row in after] == [row['revision'] for row in before]
    assert _version_count(h) == 0
    assert writes == []


@pytest.mark.parametrize('h', ['sqlite', 'postgresql'], indirect=True)
@pytest.mark.parametrize(('revision', 'status', 'code'), [
    (0, 422, 'SCHEMA_INVALID'), (2, 409, 'STALE_REVISION'),
])
def test_first_score_change_rejects_invalid_or_stale_revisions_without_storing_v1(h, revision, status, code):
    reply = h.command('admin', 'score.settings', {'definition': {}, 'reason': 'Use stale revision'},
                      h.seed['department_id'], revision, status=status)
    assert reply['error']['code'] == code
    if revision == 0:
        assert [detail['field'] for detail in reply['error']['details']] == ['body.expected_revision']
    else:
        assert reply['error']['details']['current_revision'] == 1
    assert _version_count(h) == 0


@pytest.mark.parametrize('h', ['sqlite', 'postgresql'], indirect=True)
def test_stored_score_settings_still_require_the_current_revision(h):
    first = h.command('admin', 'score.settings', {
        'definition': {'strengths': {'low': 1, 'medium': 4, 'high': 12, 'critical': 30}},
        'reason': 'First score change'}, h.seed['department_id'])
    payload = {'definition': first['settings']['definition'], 'reason': 'Keep current score settings'}
    missing = h.command('admin', 'score.settings', payload, h.seed['department_id'], status=428)
    assert missing['error']['code'] == 'REVISION_REQUIRED'
    stale = h.command('admin', 'score.settings', payload, h.seed['department_id'], 1, status=409)
    assert stale['error']['code'] == 'STALE_REVISION'
    unchanged = h.command('admin', 'score.settings', payload, h.seed['department_id'], 2)
    assert unchanged['settings']['revision'] == 2
    assert unchanged['undo'] is None
    assert _version_count(h) == 2


@pytest.mark.parametrize('h', ['sqlite', 'postgresql'], indirect=True)
def test_first_unchanged_score_settings_store_only_the_baseline(h):
    current = h.get('admin', '/api/score-settings')
    reply = h.command('admin', 'score.settings', {
        'definition': current['definition'], 'reason': 'Keep initial score settings'},
        h.seed['department_id'], 1)
    assert reply['settings']['revision'] == 1
    assert reply['settings']['definition'] == current['definition']
    assert reply['settings']['id'] is not None
    assert reply['undo'] is None
    assert _version_count(h) == 1


@pytest.mark.parametrize('h', ['sqlite', 'postgresql'], indirect=True)
def test_general_settings_preserve_the_first_score_version(h):
    me = h.get('admin', '/api/me')
    org, department = me['organisation'], me['department']
    initial = h.get('admin', '/api/score-settings')['definition']
    h.command('admin', 'department.settings', {
        'department_name': department['name'], 'department_revision': department['revision'],
        'organisation_name': org['name'], 'timezone': org['timezone'], 'week_start': org['week_start'],
        'generation_defaults': {'scoring': {'strengths': {'low': 1, 'medium': 4, 'high': 12, 'critical': 30}}},
        'reason': 'Calibrate score settings'}, department['id'], org['revision'])
    versions = h.get('admin', '/api/score-settings?history=true')['versions']
    assert [row['revision'] for row in versions] == [2, 1]
    assert versions[1]['definition'] == initial
    restored = h.command('admin', 'score.settings.rollback', {
        'version_id': versions[1]['id'], 'reason': 'Undo score calibration'}, department['id'], 2)
    assert restored['settings']['definition'] == initial
    assert _version_count(h) == 3
