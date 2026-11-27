"""The reviewed golden scenarios; fixtures share executed coverage receipts."""
from contextlib import contextmanager

import pytest

from scripts.golden_scenarios import load_pack
from tests.conftest import sqlite_harness
from .test_autoschedule_budget_api import budget_h  # noqa: F401


@pytest.mark.parametrize('scenario_id', [spec['id'] for spec in load_pack()])
def test_golden_scenario(scenario_id, golden_case):
    receipt = golden_case(scenario_id)
    assert receipt['bindings'] and all(value > 0 for value in receipt['bindings'].values())


@pytest.fixture
def golden_harness(tmp_path_factory, database_template):
    from dataclasses import replace
    from apps.api.main import create_app

    @contextmanager
    def isolated():
        # Department-wide rules must not check drafts seeded by another scenario.
        fixture = sqlite_harness(tmp_path_factory.mktemp('golden-compiler'), database_template)
        h = next(fixture)
        try:
            h.app.state.engine.dispose()
            h.settings = replace(h.settings, generation_engine='cp_sat', rate_commands_per_minute=10_000)
            h.app = create_app(h.settings)
            yield h
        finally:
            next(fixture, None)

    return isolated


def test_golden_compiler_is_idempotent(golden_harness):
    from scripts.golden_scenarios import compile_scenario, command_key
    from apps.worker.main import process_one

    def compile(h, spec):
        return compile_scenario(h.command, spec, read=h.get, committed=h.committed,
            work=lambda: process_one(h.app.state.engine, solver_workers=1))

    specs = load_pack()
    for spec in specs:
        with golden_harness() as h:
            first = compile(h, spec)
            before = h.snapshot(first['workspace'])
            second = compile(h, spec)
            assert first == second
            assert h.snapshot(second['workspace']) == before
    spec = specs[0]
    assert command_key(spec, 'rest') == command_key(spec, 'rest')
    assert command_key(spec, 'rest') != command_key({**spec, 'revision': spec['revision'] + 1}, 'rest')


def test_golden_replayed_activation_still_rejects_changed_content(budget_h):
    import copy
    from apps.worker.main import process_one
    from scripts.golden_scenarios import compile_scenario

    h = budget_h
    spec = load_pack()[0]
    compile_scenario(h.command, spec, read=h.get, committed=h.committed,
                     work=lambda: process_one(h.app.state.engine, solver_workers=1))
    changed = copy.deepcopy(spec)
    next(row for row in changed['commands'] if row['kind'] == 'policy.activate')['payload']['reason'] = 'Changed content'
    def command(role, kind, *args, **kwargs):
        reply = h.command(role, kind, *args, status=409 if kind == 'policy.activate' else 200, **kwargs)
        if kind == 'policy.activate':
            assert reply['error']['code'] == 'IDEMPOTENCY_CONFLICT'
            raise ValueError('Changed activation rejected')
        return reply

    with pytest.raises(ValueError, match='Changed activation rejected'):
        compile_scenario(command, changed, read=h.get, committed=h.committed,
                         work=lambda: pytest.fail('Replayed activation created another check'))


def test_golden_configuration_reaches_api_generation_snapshots(golden_harness):
    import copy
    from scripts.golden_scenarios import compile_scenario
    from tests.test_staff_wishes import department_settings

    from apps.worker.main import process_one

    for spec in load_pack():
        with golden_harness() as h:
            compiled = compile_scenario(h.command, spec, read=h.get, committed=h.committed,
                work=lambda: process_one(h.app.state.engine, solver_workers=1))
            weights = [0, 100] if spec['id'] == 'A-PREF-01' else [None]
            for weight in weights:
                options = copy.deepcopy(compiled['generation_options'])
                if weight is not None:
                    options['scoring']['weights']['preferences'] = weight
                me = h.get('admin', '/api/me')
                h.command('admin', 'department.settings', {
                    **department_settings(h), 'generation_defaults': options},
                    me['department']['id'], me['organisation']['revision'])
                workspace = h.refresh_draft_baseline(h.detail(compiled['workspace'])['workspace'])
                reply = h.command('coordinator', 'job.create', {
                    'workspace_id': workspace['id'], 'kind': 'generation', 'generation_options': options},
                    workspace['id'], workspace['revision'])
                assert reply['job']['effective_options'] == options
                assert h.snapshot(workspace)['generation_options'] == options
                h.command('operator', 'job.cancel', {'reason': 'Synthetic configuration proof; do not solve'},
                          reply['job']['id'], reply['job']['revision'])


@pytest.fixture
def check_spec():
    return {'id': 'check-guard', 'revision': 1, 'inputs': {'generation_options': {}}, 'commands': [
        {'key': 'workspace', 'role': 'coordinator', 'kind': 'workspace.create'},
        {'key': 'rule', 'role': 'coordinator', 'kind': 'policy.create'},
        {'key': 'activate', 'role': 'approver', 'kind': 'policy.activate',
         'target': '$rule', 'revision': '$rule.revision'}]}


@pytest.mark.parametrize('status', ['failed', 'queued', 'succeeded'])
def test_golden_compiler_never_activates_after_failed_or_timed_out_check(monkeypatch, check_spec, status):
    from scripts import golden_scenarios as golden

    posted = []
    def command(role, kind, payload=None, **kwargs):
        posted.append(kind)
        return {'workspace.create': {'workspace': {'id': 'workspace', 'revision': 1}},
                'policy.create': {'record': {'id': 'rule', 'revision': 1}},
                'job.create': {'job': {'id': 'check'}}}[kind]
    clock = [0]
    monkeypatch.setattr(golden.time, 'monotonic', lambda: clock[0])
    def read(role, path):
        if path.startswith('/api/jobs?'):
            return {'jobs': [], 'has_more': False}
        if path.startswith('/api/policies/'):
            return {'effect': [{'workspace_id': 'workspace', 'revision': 1}]}
        clock[0] = 121 if status != 'failed' else 0
        return {'job': {'id': 'check', 'status': status}}
    with pytest.raises(ValueError, match='failed or timed out'):
        golden.compile_scenario(command, check_spec, read=read, committed=lambda *_: None)
    assert posted == ['workspace.create', 'policy.create', 'job.create']


@pytest.mark.parametrize('historical_status', ['failed', 'cancelled'])
def test_golden_check_resume_reads_all_history_before_replacing_a_terminal_job(monkeypatch, check_spec, historical_status):
    from scripts import golden_scenarios as golden

    posted, offsets, serviced = [], [], []
    original_key = golden.command_key(check_spec, 'activate:check')
    replacement_key = golden.command_key(check_spec, 'activate:check:after:original')
    def command(role, kind, payload=None, **kwargs):
        posted.append((kind, payload, kwargs['key']))
        if kind == 'workspace.create':
            return {'workspace': {'id': 'workspace', 'revision': 1}}
        if kind.startswith('policy.'):
            return {'record': {'id': 'rule', 'revision': 1}}
        assert kind == 'job.create'
        assert kwargs['key'] in {original_key, replacement_key}
        return {'job': {'id': 'original' if kwargs['key'] == original_key else 'replacement'}}

    def read(role, path):
        if path.startswith('/api/jobs?'):
            offsets.append(path.rsplit('=', 1)[-1])
            if offsets[-1] == '0':
                return {'jobs': [{'id': f'other-{i}', 'status': 'succeeded'} for i in range(100)], 'has_more': True, 'limit': 100}
            assert offsets[-1] == '100'
            return {'jobs': [{'id': 'original', 'status': historical_status}], 'has_more': False}
        if path.startswith('/api/policies/'):
            return {'effect': [{'workspace_id': 'workspace', 'revision': 1}]}
        ident = path.rsplit('/', 1)[-1]
        return {'job': {'id': ident, 'status': historical_status if ident == 'original' else 'succeeded'}}

    monkeypatch.setattr(golden.time, 'monotonic', lambda: 0)
    golden.compile_scenario(command, check_spec, read=read, committed=lambda *_: None, work=lambda: serviced.append(True))
    assert offsets == ['0', '100']
    assert [key for kind, _, key in posted if kind == 'job.create'] == [original_key, replacement_key]
    assert posted[-1][1] == {'check_job_ids': ['replacement']}
    assert not serviced  # Replayed terminal checks never run another worker job.


def test_golden_incomplete_check_history_cannot_authorize_a_replacement(monkeypatch, check_spec):
    from scripts import golden_scenarios as golden

    posted, clock = [], [0]
    def command(role, kind, payload=None, **kwargs):
        posted.append(kind)
        return {'workspace.create': {'workspace': {'id': 'workspace', 'revision': 1}},
                'policy.create': {'record': {'id': 'rule', 'revision': 1}}}[kind]

    def read(role, path):
        assert path.startswith('/api/jobs?')
        clock[0] += 61
        return {'jobs': [{'id': 'original', 'status': 'failed'}], 'has_more': True, 'limit': 100}

    monkeypatch.setattr(golden.time, 'monotonic', lambda: clock[0])
    with pytest.raises(ValueError, match='failed or timed out'):
        golden.compile_scenario(command, check_spec, read=read, committed=lambda *_: None)
    assert posted == ['workspace.create', 'policy.create']


@pytest.mark.parametrize('status', ['succeeded', 'failed'])
@pytest.mark.parametrize('failed_rota', ['other', 'workspace'])
def test_golden_compiler_requires_checks_for_every_affected_rota(check_spec, status, failed_rota):
    from scripts import golden_scenarios as golden

    posted = []
    def command(role, kind, payload=None, **kwargs):
        posted.append((kind, payload, kwargs))
        if kind == 'workspace.create':
            return {'workspace': {'id': 'workspace', 'revision': 1}}
        if kind == 'job.create':
            assert payload['rule_changes'] == [{'policy_id': 'rule', 'expected_revision': 1}]
            assert kwargs['revision'] == {'workspace': 1, 'other': 7}[payload['workspace_id']]
            return {'job': {'id': 'check-' + payload['workspace_id']}}
        return {'record': {'id': 'rule', 'revision': 1}}

    def read(role, path):
        if path == '/api/policies/rule/review':
            return {'effect': [{'workspace_id': 'workspace', 'revision': 1},
                               {'workspace_id': 'other', 'revision': 7}]}
        if path.startswith('/api/jobs?'):
            return {'jobs': [], 'has_more': False}
        return {'job': {'id': path.rsplit('/', 1)[-1],
                        'status': status if path.endswith('check-' + failed_rota) else 'succeeded'}}

    if status == 'failed':
        with pytest.raises(ValueError, match='failed or timed out'):
            golden.compile_scenario(command, check_spec, read=read, committed=lambda *_: None)
        assert 'policy.activate' not in [kind for kind, _, _ in posted]
    else:
        golden.compile_scenario(command, check_spec, read=read, committed=lambda *_: None)
        assert posted[-1][1] == {'check_job_ids': ['check-other', 'check-workspace']}
    keys = [kwargs['key'] for kind, _, kwargs in posted if kind == 'job.create']
    expected = [golden.command_key(check_spec, 'activate:check:other')]
    if status == 'succeeded' or failed_rota == 'workspace':
        expected.append(golden.command_key(check_spec, 'activate:check:workspace'))
    assert keys == expected


def test_golden_compiler_replays_committed_approval_before_creating_checks(check_spec):
    from scripts import golden_scenarios as golden

    posted = []
    approved = {'id': 'rule', 'revision': 2, 'status': 'active'}
    receipt = {'record': approved, 'context_check_evidence': [
        {'workspace_id': 'workspace', 'check_job_id': 'original-check'}]}
    operation_key = golden.command_key(check_spec, 'activate')
    def command(role, kind, payload=None, **kwargs):
        posted.append(kind)
        if kind == 'policy.activate':
            assert role == 'approver'
            assert payload == {'check_job_ids': ['original-check']}
            assert kwargs == {'target': 'rule', 'revision': 1, 'key': operation_key}
            return receipt
        return {'workspace.create': {'workspace': {'id': 'workspace', 'revision': 1}},
                'policy.create': {'record': {'id': 'rule', 'revision': 1}}}[kind]

    def committed(role, key):
        assert (role, key) == ('approver', operation_key)
        return receipt

    compiled = golden.compile_scenario(command, check_spec, committed=committed,
        read=lambda *_: pytest.fail('Committed activation must not read new review or check state'))
    assert posted == ['workspace.create', 'policy.create', 'policy.activate']
    assert compiled['records']['rule'] == compiled['records']['activate'] == approved


@pytest.mark.parametrize('seeds,workers', [([11, 29], 1), ([11, 11, 47], 1), ([11, 29, 47], 2)])
def test_golden_micro_pack_requires_three_distinct_seeds_and_one_worker(tmp_path, monkeypatch, seeds, workers):
    import json
    from scripts import golden_scenarios as golden

    spec = load_pack()[0]
    spec['expect']['seed_robustness'].update(seeds=seeds, workers=workers)
    (tmp_path / 'entry.json').write_text(json.dumps(spec), encoding='utf-8')
    (tmp_path / 'manifest.json').write_text(json.dumps({
        'version': 1, 'seeds': [11, 29, 47], 'scenarios': ['entry.json']}), encoding='utf-8')
    monkeypatch.setattr(golden, 'PACK', tmp_path)
    with pytest.raises(ValueError, match='three distinct seeds and one worker'):
        golden.load_pack()
