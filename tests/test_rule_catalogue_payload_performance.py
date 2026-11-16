"""Department-scale summaries, exact freshness, and batched conversion evidence."""
from copy import deepcopy
from datetime import date
import time
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.auth import authenticate
from apps.api.db import transaction
from apps.api.services import rule_catalogue_v2 as conversion
from packages.contracts.schemas import PolicyDefinition


def department_rules(h):
    """QA's expanded department plus its seeded Fair share: 92 sources, 292 lines, 1,045 retired versions."""
    with transaction(h.app.state.engine, write=True) as db:
        sources = list(db.scalars(select(m.Policy.id).where(m.Policy.department_id == h.seed['department_id'],
            m.Policy.status == 'active')))
        for index in range(1136):
            rule = {'kind': 'catalogue', 'id': 'duties_per_week',
                'lines': [{'id': str(uuid4()), 'numbers': {'max': {'value': 5 + n, 'level': 'mandatory'}}}
                          for n in range(201 if index == 0 else 3)]}
            if index > 0:
                rule = {'kind': 'max_duties', 'maximum': 5, 'window': 'week'}
            definition = PolicyDefinition.model_validate({'rule': rule}).model_dump(mode='json')
            row = m.Policy(id=str(uuid4()), department_id=h.seed['department_id'],
                name=f'Synthetic source {index}', owner='Synthetic owner', source='Synthetic scale regression',
                kind=rule['kind'], definition=definition, status='active' if index < 91 else 'retired',
                active=index < 91, starts_on=date(2020, 1, 1),
                created_by=h.seed['identities']['coordinator'], approved_by=h.seed['identities']['approver'])
            db.add(row)
            if index < 91:
                sources.append(row.id)
    h.client('coordinator')
    h.client('approver')
    return sources


def timed_request(h, method, path, *, seconds, **kwargs):
    cpu, wall = time.process_time(), time.perf_counter()
    response = getattr(h.client('coordinator'), method)(path, **kwargs)
    cpu, wall = time.process_time() - cpu, time.perf_counter() - wall
    print(path, {'cpu_seconds': cpu, 'wall_seconds': wall, 'bytes': len(response.content)})
    return response, (cpu, wall, seconds)


@pytest.fixture(scope='module')
def department_summary_measurement(scale_harness):
    h = scale_harness('h')
    sources = department_rules(h)
    h.workspace('Synthetic department proof anchor')
    preview_response, preview_metric = timed_request(h, 'get', '/api/rules/catalogue/migration?version=2&background=true', seconds=1)
    preview = preview_response.json()
    full = h.get('coordinator', '/api/rules/catalogue/migration?version=2&details=true')
    changed = h.command('coordinator', 'rule.migrate_to_catalogue', {
        'version': 2, 'plan_hash': preview['plan_hash'], 'reason': 'Synthetic independent scale review'})['record']
    # Model objective captures can be megabytes. Only this read fixture adds synthetic captured text.
    proof = {'identical': True, 'rotas': [{'workspace_id': str(uuid4()), 'identical': True,
        'model': {'equal': True, **{side: {'ready': True, 'digest': side, 'variables': 1000,
            'constraints': 3000, 'tiers': [{'objective': 'synthetic capture ' * 20_000}]}
            for side in ('before', 'after')}}} for _ in range(15)]}
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.CatalogueConversion, changed['id']).proof = proof
    path = '/api/rules/catalogue/conversions/' + changed['id']
    shown_response, shown_metric = timed_request(h, 'get', path, seconds=1)
    detailed = h.get('auditor', path + '?details=true')['record']
    listed = h.get('coordinator', '/api/rules/catalogue/conversions')['records']
    return sources, preview_response, full, changed, proof, shown_response, detailed, listed, [preview_metric, shown_metric]


def test_department_preview_and_proposal_are_summaries_with_full_evidence_on_demand(department_summary_measurement):
    sources, preview_response, full, changed, proof, shown_response, detailed, listed, _ = department_summary_measurement
    for response in (preview_response, shown_response):
        assert response.status_code == 200, response.text
        assert len(response.content) <= 300_000
    preview = preview_response.json()
    assert len(preview['bindings']) == len(preview['report']) == len(sources) == 92
    assert not preview['blocked'] and preview['proof'] is None and preview['proof_state'] == 'not_checked'
    assert 'source_manifest' not in preview
    assert 'payload' not in preview['targets'][0] and 'source' not in preview['bindings'][0]
    assert len(full['source_manifest']) == 1137 and full['plan_hash'] == preview['plan_hash']
    assert sum(len(t['payload']['definition']['rule']['lines']) for t in full['targets']) == 292
    assert shown_response.json()['record']['proof'] == conversion._preview_proof(proof)
    assert detailed['proof'] == proof
    assert listed[0]['id'] == changed['id']


@pytest.mark.cpu_budget
def test_department_preview_and_proposal_are_summaries_with_full_evidence_on_demand_cpu_budget(department_summary_measurement):
    import coverage
    *_, metrics = department_summary_measurement
    for cpu, wall, seconds in metrics:
        # Keep the production CPU budget; the shared runner and coverage receive the existing CI allowances.
        budget = seconds * (3 if coverage.Coverage.current() else 1)
        assert cpu <= budget, (cpu, budget)


@pytest.mark.wall_benchmark
def test_department_preview_and_proposal_are_summaries_with_full_evidence_on_demand_wall_benchmark(department_summary_measurement):
    import coverage
    *_, metrics = department_summary_measurement
    for _, wall, seconds in metrics:
        budget = seconds * (3 if coverage.Coverage.current() else 1)
        assert wall <= 4 * budget, (wall, budget)


def batch_lifecycle(h):
    sources = department_rules(h)
    preview = h.get('coordinator', '/api/rules/catalogue/migration?version=2&background=true')
    changed = h.command('coordinator', 'rule.migrate_to_catalogue', {
        'version': 2, 'plan_hash': preview['plan_hash'], 'reason': 'Synthetic scale conversion'})['record']
    metrics = []
    for action in ('activate', 'undo'):
        cpu, wall = time.process_time(), time.perf_counter()
        changed = h.command('approver', 'rule.catalogue.' + action, {'reason': 'Synthetic independent decision'},
                            changed['id'], changed['revision'])['record']
        cpu, wall = time.process_time() - cpu, time.perf_counter() - wall
        print(action, {'cpu_seconds': cpu, 'wall_seconds': wall})
        metrics.append((cpu, wall))
    with transaction(h.app.state.engine) as db:
        statuses = [db.get(m.Policy, ident).status for ident in sources]
        evidence = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.object_id.in_(sources))))
        revisions = {row.object_revision for row in evidence}
    return statuses, len(evidence), revisions, metrics


@pytest.fixture(scope='module')
def department_lifecycle_measurement(scale_harness):
    return batch_lifecycle(scale_harness('h'))


def test_department_conversion_batch_retains_all_lifecycle_evidence(department_lifecycle_measurement):
    statuses, count, revisions, _ = department_lifecycle_measurement
    assert all(status == 'active' for status in statuses)
    assert count == 3 * len(statuses)
    assert revisions == {1, 2, 3}


@pytest.mark.cpu_budget
def test_department_conversion_batch_retains_all_lifecycle_evidence_cpu_budget(department_lifecycle_measurement):
    import coverage
    *_, metrics = department_lifecycle_measurement
    for cpu, wall in metrics:
        assert cpu <= 3 * (3 if coverage.Coverage.current() else 1)


@pytest.mark.wall_benchmark
def test_department_conversion_batch_retains_all_lifecycle_evidence_wall_benchmark(department_lifecycle_measurement):
    import coverage
    *_, metrics = department_lifecycle_measurement
    for _, wall in metrics:
        assert wall <= 12 * (3 if coverage.Coverage.current() else 1)


def test_conversion_activation_reuses_the_plan_manifest_read(h, monkeypatch):
    source = h.policy({'kind': 'max_duties', 'maximum': 5, 'window': 'week'})
    preview = h.get('coordinator', '/api/rules/catalogue/migration?version=2&background=true')
    proposed = h.command('coordinator', 'rule.migrate_to_catalogue', {
        'version': 2, 'plan_hash': preview['plan_hash'], 'reason': 'Synthetic manifest reuse'})['record']
    calls, original = [], conversion._input_rows

    def counted(*args):
        calls.append(True)
        return original(*args)

    monkeypatch.setattr(conversion, '_input_rows', counted)
    active = h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent manifest review'},
                       proposed['id'], proposed['revision'])['record']
    assert active['status'] == 'active' and len(calls) == 2, 'Read once before proof and once for its final freshness guard'
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, source['id']).status == 'retired'


def test_conversion_rejects_same_revision_content_change_before_proof(h, monkeypatch):
    source = h.policy({'kind': 'max_duties', 'maximum': 5, 'window': 'week'})
    preview = h.get('coordinator', '/api/rules/catalogue/migration?version=2&background=true')
    proposed = h.command('coordinator', 'rule.migrate_to_catalogue', {
        'version': 2, 'plan_hash': preview['plan_hash'], 'reason': 'Synthetic exact manifest guard'})['record']
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Policy, source['id'])
        changed = deepcopy(row.definition)
        changed['rule']['maximum'] = 6
        row.definition = changed

    def unexpected(*args, **kwargs):
        pytest.fail('Stale row content must be rejected before any proof')

    monkeypatch.setattr(conversion, '_proof', unexpected)
    error = h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent stale manifest check'},
        proposed['id'], proposed['revision'], status=409)['error']
    assert error['code'] == 'STALE_INPUTS'


@pytest.mark.parametrize('change', ['unchanged', 'value', 'missing', 'null', 'null-missing', 'multiplicity'])
def test_no_rota_proof_retains_complete_shared_provenance(h, monkeypatch, change):
    source_id = h.get('coordinator', '/api/policies?status=active')['policies'][0]['id']
    provenance = {'line': {'numbers': {'max': {'value': 5}}, 'note': 'Complete source evidence'}}
    if change == 'null-missing':
        provenance = None
    before = [{'id': source_id, '_catalogue_sources': provenance} for _ in range(2)]
    after = deepcopy(before)
    if change == 'value':
        after[0]['_catalogue_sources']['line']['note'] = 'Changed evidence'
    elif change in {'missing', 'null-missing'}:
        after[0].pop('_catalogue_sources')
    elif change == 'null':
        after[0]['_catalogue_sources'] = None
    elif change == 'multiplicity':
        after.pop()
    monkeypatch.setattr(conversion, 'active_rules', lambda policies, context: after if context.get('_after') else before)
    monkeypatch.setattr(conversion.migration, 'after_snapshot', lambda data, candidate: {'policies': [], '_after': True})
    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, h.client('approver').cookies['rota_session'])
        result = conversion._proof(db, actor, {'bindings': [{'source_policy_id': source_id}]}, [])
    assert result['identical'] is result['expanded_equal'] is (change == 'unchanged')


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_undo_retires_destination_before_restoring_the_same_catalogue_entry(request, backend):
    h = request.getfixturevalue(backend)
    # The source sorts before its destination: one mixed update batch would violate the active-entry index.
    source_id = str(UUID(int=1))
    definition = PolicyDefinition.model_validate({'rule': {'kind': 'catalogue', 'id': 'days_in_a_row',
        'lines': [{'id': str(uuid4()), 'numbers': {'max': {'value': 5, 'level': 'mandatory'}}}]}},
        context={'historical': True}).model_dump(mode='json')
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.Policy(id=source_id, department_id=h.seed['department_id'], name='Synthetic same-entry source',
            owner='Synthetic owner', source='Synthetic Undo regression', kind='catalogue', definition=definition,
            status='active', active=True, starts_on=date(2020, 1, 1),
            created_by=h.seed['identities']['coordinator'], approved_by=h.seed['identities']['approver']))
    preview = h.get('coordinator', '/api/rules/catalogue/migration?version=2&background=true')
    changed = h.command('coordinator', 'rule.migrate_to_catalogue', {
        'version': 2, 'plan_hash': preview['plan_hash'], 'reason': 'Synthetic same-entry conversion'})['record']
    target_id = changed['plan']['targets'][0]['row']['id']
    assert source_id < target_id
    for action in ('activate', 'undo'):
        changed = h.command('approver', 'rule.catalogue.' + action, {'reason': 'Synthetic independent decision'},
                            changed['id'], changed['revision'])['record']
    with transaction(h.app.state.engine) as db:
        source, target = db.get(m.Policy, source_id), db.get(m.Policy, target_id)
        assert (source.status, source.active, source.revision, source.definition) == ('active', True, 3, definition)
        assert (target.status, target.active, target.revision) == ('retired', False, 2)
        evidence = list(db.scalars(select(m.DefinitionHistory).where(
            m.DefinitionHistory.object_id.in_((source_id, target_id)))))
        assert sorted((row.object_id, row.object_revision, row.snapshot['status']) for row in evidence) == sorted([
            (source_id, 1, 'active'), (source_id, 2, 'retired'), (source_id, 3, 'active'),
            (target_id, 1, 'active'), (target_id, 2, 'retired')])


@pytest.fixture(scope='module')
def postgres_lifecycle_measurement(scale_harness):
    h = scale_harness('pg')
    return h, batch_lifecycle(h)


@pytest.mark.postgres
def test_postgres_department_batch_and_exact_row_content_guard(postgres_lifecycle_measurement):
    pg, (statuses, count, revisions, _) = postgres_lifecycle_measurement
    assert all(status == 'active' for status in statuses)
    assert count == 3 * len(statuses)
    assert revisions == {1, 2, 3}
    with transaction(pg.app.state.engine) as db:
        actor = authenticate(db, pg.client('coordinator').cookies['rota_session'])
        before = conversion._inputs(db, actor)
        source = db.scalar(select(m.Policy).where(m.Policy.status == 'retired'))
        with transaction(pg.app.state.engine, write=True) as other:
            # Even an out-of-band historical edit without a revision bump must invalidate the proof.
            changed = other.get(m.Policy, source.id)
            definition = deepcopy(changed.definition)
            definition['rule']['maximum'] = definition['rule'].get('maximum', 5) + 1
            changed.definition = definition
        after = conversion._inputs(db, actor)
        assert before['input_revision'] == after['input_revision']
        assert before['records']['policies'] != after['records']['policies']


@pytest.mark.postgres
@pytest.mark.cpu_budget
def test_postgres_department_batch_and_exact_row_content_guard_cpu_budget(postgres_lifecycle_measurement):
    import coverage
    _, (_, _, _, metrics) = postgres_lifecycle_measurement
    for cpu, wall in metrics:
        assert cpu <= 3 * (3 if coverage.Coverage.current() else 1)


@pytest.mark.postgres
@pytest.mark.wall_benchmark
def test_postgres_department_batch_and_exact_row_content_guard_wall_benchmark(postgres_lifecycle_measurement):
    import coverage
    _, (_, _, _, metrics) = postgres_lifecycle_measurement
    for _, wall in metrics:
        assert wall <= 12 * (3 if coverage.Coverage.current() else 1)


@pytest.mark.postgres
def test_postgres_preview_rejects_concurrent_input_edit(pg, monkeypatch):
    from .test_rule_preview_rc12 import test_preview_rejects_concurrent_edit_even_with_cached_source_identity
    test_preview_rejects_concurrent_edit_even_with_cached_source_identity(pg, monkeypatch)
