"""Rule review reads preserved approvals and canonical person counts without changing governance."""
from .clock_support import freeze_clock
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event as sql_event, select

from apps.api import models as m
from apps.api.auth import authenticate
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.serialization import record
from apps.api.services import policies
from packages.domain.time import aware
from packages.domain.rule_lines import fact_policy
from tests.test_bulk_single_commands import SOURCE


def approval(h, row, role='approver'):
    token = h.client(role).cookies['rota_session']
    with transaction(h.app.state.engine) as db:
        return policies.exception_approval(db, authenticate(db, token), row['id'])


def exception(h, policy, first='2026-10-04', last='2026-10-05'):
    return h.command('coordinator', 'exception.create', {'policy_id': policy['id'],
        'person_id': h.seed['people'][0], 'starts_on': first, 'ends_on': last,
        'reason': 'Synthetic bounded workload review'})['record']


@pytest.mark.parametrize('reason', ['Independent synthetic review', 'Source retained before catalogue conversion',
                                  'Original definition retained before private metadata repair'])
def test_policy_version_approval_uses_original_proven_evidence_after_restore(h, monkeypatch, reason):
    proposed = h.command('coordinator', 'policy.create', {'name': 'Synthetic weekly limit', 'owner': 'Test policy owner',
        'source': 'Constructed fixture, not approved hospital policy', 'starts_on': '2020-01-01',
        'definition': {'rule': {'kind': 'max_duties', 'maximum': 3}, 'exception_allowed': True}})['record']
    policy = h.command('approver', 'policy.activate', {'reason': reason}, proposed['id'], proposed['revision'])['record']
    token = h.client('approver').cookies['rota_session']
    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, token)
        original = policies.policy_versions(db, actor, [policy])[0]
        assert original['approved_by_name'] == db.get(m.Identity, policy['approved_by']).display_name
        assert original['approved_at'] is not None
        assert original['reason'] == policy['source']
        history = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.object_id == policy['id'])))
        snapshots = policies.policy_versions(db, actor, [row.snapshot for row in history])
        assert next(row for row in snapshots if row['status'] == 'proposed')['approved_at'] is None
        unknown = policies.policy_versions(db, actor, [{**policy, 'id': str(uuid4())}])[0]
        assert unknown['approved_at'] is None and unknown['reason'] == policy['source']
    later = aware(original['approved_at']) + timedelta(hours=1)
    freeze_clock(monkeypatch, lambda: later)
    retired = h.command('approver', 'policy.retire', {'reason': 'Synthetic retirement'}, policy['id'], policy['revision'])['record']
    restored = h.command('approver', 'policy.restore', {'reason': 'Synthetic restoration'}, retired['id'], retired['revision'])['record']
    with transaction(h.app.state.engine) as db:
        current = policies.policy_versions(db, authenticate(db, token), [restored])[0]
    assert current['approved_at'] == original['approved_at']
    assert current['reason'] == original['reason']
    versions = h.get('approver', '/api/policies?status=active')['policies']
    assert next(row for row in versions if row['id'] == policy['id'])['approved_at'] == original['approved_at']


@pytest.mark.parametrize('version', [1, 2])
def test_catalogue_rule_versions_include_approval_labels(h, version):
    catalogue_id = 'duties_per_week' if version == 1 else 'workload_limit'
    if version == 2:
        from tests.test_rule_catalogue_v2_api import proposed as conversion_proposal
        conversion = conversion_proposal(h)
        h.command('approver', 'rule.catalogue.activate', {'reason': 'Synthetic catalogue conversion review'},
                  conversion['id'], conversion['revision'])
    line = {'id': str(uuid4()), 'numbers': {'max': {'value': 3, 'level': 'mandatory'}}}
    if version == 2:
        line.update(measure='duties', period={'kind': 'calendar_week'})
    proposed = h.command('coordinator', 'rule.propose', {'catalogue_id': catalogue_id, 'catalogue_version': version,
        'lines': [line],
        'reason': 'Synthetic weekly limit review'})['record']
    h.command('approver', 'policy.activate', {'reason': 'Synthetic independent activation'}, proposed['id'], proposed['revision'])
    entries = h.get('approver', f'/api/rules/catalogue?version={version}&include_hidden=true')['entries']
    rule = next(entry['rule'] for entry in entries if entry['id'] == catalogue_id)
    assert rule['approved_by_name'] and rule['approved_at']
    assert rule['reason'] == 'Synthetic weekly limit review'


def test_historical_policy_snapshots_keep_each_approver_and_revision_proof(h):
    policy = h.policy({'kind': 'max_duties', 'maximum': 3})
    token = h.client('approver').cookies['rota_session']
    other = h.seed['identities']['admin']
    with transaction(h.app.state.engine, write=True) as db:
        actor = authenticate(db, token)
        original = policies.policy_versions(db, actor, [policy])[0]
        first_at = aware(original['approved_at'])
        second = {**policy, 'revision': 4, 'approved_by': other}
        third = {**policy, 'revision': 6}
        for snapshot, at in ((second, first_at + timedelta(hours=1)), (third, first_at + timedelta(hours=2))):
            proposed = {**snapshot, 'revision': snapshot['revision'] - 1, 'status': 'proposed', 'approved_by': None}
            for state, by, captured in ((proposed, policy['created_by'], at - timedelta(minutes=1)),
                                        (snapshot, snapshot['approved_by'], at)):
                db.add(m.DefinitionHistory(department_id=actor.department_id, kind='policy', object_id=policy['id'],
                    object_revision=state['revision'], snapshot=state, actor_id=by,
                    created_at=captured, reason='Synthetic approval transition'))
        db.flush()
        versions = policies.policy_versions(db, actor, [policy, second, third, {**second, 'revision': 3}])
        assert [row['approved_by_name'] for row in versions[:3]] == [
            original['approved_by_name'], db.get(m.Identity, other).display_name, original['approved_by_name']]
        assert [aware(row['approved_at']) for row in versions[:3]] == [
            first_at, first_at + timedelta(hours=1), first_at + timedelta(hours=2)]
        assert versions[3]['approved_at'] is None


def test_first_active_snapshot_is_not_an_approval_transition(h):
    approved = h.policy({'kind': 'max_duties', 'maximum': 3})
    token = h.client('approver').cookies['rota_session']
    unproven = {**approved, 'id': str(uuid4()), 'revision': 1}
    with transaction(h.app.state.engine, write=True) as db:
        actor = authenticate(db, token)
        db.add(m.DefinitionHistory(department_id=actor.department_id, kind='policy', object_id=unproven['id'],
            object_revision=1, snapshot=unproven, actor_id=approved['approved_by'], reason='Legacy state captured'))
        db.flush()
        [version] = policies.policy_versions(db, actor, [unproven])
        assert version['approved_by'] == approved['approved_by'] and version['approved_at'] is None


@pytest.mark.parametrize('legacy_history,reason', [
    (False, 'Synthetic independent conversion'), (True, 'Synthetic independent conversion'),
    pytest.param(False, 'Source retained before catalogue conversion', id='source-preservation-reason'),
    pytest.param(False, 'Original definition retained before private metadata repair', id='repair-preservation-reason'),
])
def test_conversion_preservation_never_becomes_a_legacy_source_approval(h, monkeypatch, legacy_history, reason):
    from tests.test_rule_catalogue_v2_api import proposed as conversion_proposal
    from apps.api.services import rule_catalogue_v2

    with transaction(h.app.state.engine, write=True) as db:
        source = m.Policy(department_id=h.seed['department_id'], name='Synthetic legacy weekly limit',
            kind='max_duties', definition={'rule': {'kind': 'max_duties', 'maximum': 9, 'window': 'week'},
                'scope': {}, 'severity': 'mandatory', 'exception_allowed': False},
            starts_on=m.now().date(), status='active', active=True, source='Synthetic legacy source without approval history',
            owner='Synthetic owner', created_by=h.seed['identities']['coordinator'], approved_by=h.seed['identities']['approver'])
        db.add(source)
        db.flush()
        source_id = source.id
    if legacy_history:
        original_history_many = rule_catalogue_v2.history_many

        def previous_history_format(db, actor, entries, **kwargs):
            return original_history_many(db, actor, entries)

        monkeypatch.setattr(rule_catalogue_v2, 'history_many', previous_history_format)
    actual = h.policy({'kind': 'max_duties', 'maximum': 5, 'window': 'week'})
    actual_at = next(row for row in h.get('approver', '/api/policies?status=active')['policies']
                     if row['id'] == actual['id'])['approved_at']
    conversion = conversion_proposal(h)
    active = h.command('approver', 'rule.catalogue.activate', {'reason': reason},
                       conversion['id'], conversion['revision'])['record']
    retired = {row['id']: row for row in h.get('approver', '/api/policies?status=retired')['policies']}
    assert retired[source_id]['approved_by_name'] and retired[source_id]['approved_at'] is None
    assert retired[actual['id']]['approved_at'] == actual_at
    destinations = {target['row']['id'] for target in active['plan']['targets']}
    current = h.get('approver', '/api/policies?status=active')['policies']
    assert destinations and all(row['approved_at'] and row['approved_by_name'] for row in current if row['id'] in destinations)
    if legacy_history:
        assert all(row['approved_at'] == active['approved_at'] for row in current if row['id'] in destinations)
        destination = next(row for row in current if row['id'] in destinations)
        token = h.client('approver').cookies['rota_session']
        statements = []

        def count(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        with transaction(h.app.state.engine) as db:
            actor = authenticate(db, token)
            sql_event.listen(h.app.state.engine, 'before_cursor_execute', count)
            try:
                policies.policy_versions(db, actor, [destination])
                small = len(statements)
                statements.clear()
                policies.policy_versions(db, actor, [destination, *[
                    {**destination, 'id': str(uuid4())} for _ in range(39)]])
                assert len(statements) == small == 4
            finally:
                sql_event.remove(h.app.state.engine, 'before_cursor_execute', count)
    h.command('approver', 'rule.catalogue.undo', {'reason': 'Synthetic representation restore'},
              active['id'], active['revision'])
    restored = {row['id']: row for row in h.get('approver', '/api/policies?status=active')['policies']}
    assert restored[source_id]['approved_at'] is None
    assert restored[actual['id']]['approved_at'] == actual_at


@pytest.mark.postgres
def test_postgres_legacy_conversion_approval_proof(pg, monkeypatch):
    test_conversion_preservation_never_becomes_a_legacy_source_approval(pg, monkeypatch, True, 'Synthetic independent conversion')


def test_reconstructed_source_cannot_borrow_approval_labels_after_destination_change(h):
    from tests.test_rule_catalogue_v2_api import proposed as conversion_proposal
    from tests.governance_support import identity

    source = h.policy({'kind': 'max_duties', 'maximum': 9, 'window': 'week'})
    identity(h, 'conversion_approver', ['approver'])
    conversion = conversion_proposal(h)
    conversion_active = h.command('conversion_approver', 'rule.catalogue.activate',
                                  {'reason': 'Synthetic department conversion review'},
                                  conversion['id'], conversion['revision'])['record']
    entries = h.get('coordinator', '/api/rules/catalogue?version=2')['entries']
    current = next(entry['rule'] for entry in entries if entry['id'] == 'workload_limit')
    lines = deepcopy(current['definition']['rule']['lines'])
    lines[0]['numbers']['max']['value'] = 1
    changed = h.command('coordinator', 'rule.propose', {'catalogue_id': 'workload_limit', 'catalogue_version': 2,
        'expected_policy_id': current['id'], 'expected_revision': current['revision'], 'lines': lines,
        'reason': 'Synthetic changed destination limit'})['record']
    h.command('approver', 'policy.activate', {'reason': 'Synthetic destination review'}, changed['id'], changed['revision'])
    token = h.client('approver').cookies['rota_session']
    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, token)
        stored = db.get(m.Policy, source['id'])
        effective = policies.effective_source(db, actor, stored)
        assert effective['_v2_source'] and effective['definition']['rule']['maximum'] == 1
        assert effective['revision'] != source['revision']
        [derived, original] = policies.policy_versions(db, actor, [effective, record(stored)])
        assert derived['approved_by_name'] == 'Fictional conversion_approver'
        assert derived['approved_at'] == conversion_active['approved_at']
        assert derived['approved_by'] == source['approved_by'] and derived['source'] == source['source']
        assert original['approved_by_name'] and original['approved_at']


def test_fact_projection_uses_its_fact_approval(h):
    payload = {'kind': 'workload', 'person_id': h.seed['people'][0], 'catalogue_id': 'workload_limit',
        'line': {'id': str(uuid4()), 'measure': 'duties', 'period': {'kind': 'calendar_week'},
                 'numbers': {'max': {'value': 2, 'level': 'mandatory'}}},
        'reason': 'Synthetic personal workload approval'}
    proposed = h.command('coordinator', 'person.workload.propose', payload)['fact']
    active = h.command('approver', 'person.workload.activate', {'reason': 'Independent fact review'},
                       proposed['id'], proposed['revision'])['fact']
    expected_name = h.get('approver', '/api/me')['display_name']
    token = h.client('approver').cookies['rota_session']
    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, token)
        [projected] = policies.policy_versions(db, actor, [fact_policy(record(db.get(m.SchedulingFact, active['id'])))])
    assert projected['approved_by_name'] == expected_name
    assert projected['approved_at'] == active['approved_at']


def test_missing_fact_projection_does_not_borrow_copied_approval(h):
    token = h.client('approver').cookies['rota_session']
    fact = {'id': str(uuid4()), 'department_id': h.seed['department_id'], 'person_id': h.seed['people'][0],
            'kind': 'workload', 'status': 'active', 'on': True,
            'data': {'catalogue_id': 'workload_limit', 'catalogue_version': 2,
                     'line': {'id': str(uuid4()), 'measure': 'duties', 'period': {'kind': 'calendar_week'},
                              'numbers': {'max': {'value': 2, 'level': 'mandatory'}}}}}
    projected_fact = fact_policy(fact)
    projected_fact.update(kind='workload', approved_by=h.seed['identities']['approver'],
                          approved_at='2026-10-03T06:00:00+00:00')
    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, token)
        [projected] = policies.policy_versions(db, actor, [projected_fact])
    assert projected['_v2_source']
    assert projected['approved_by_name'] is None
    assert projected['approved_at'] is None


def test_historical_import_reason_projection_preserves_raw_evidence_and_manual_reason(h):
    raw = 'SCHEDULING_RULES.md228: synthetic import log; final verification retained.'
    rows = [h.command('coordinator', 'policy.create', {'name': 'Synthetic review source', 'owner': 'Synthetic owner',
        'source': raw, 'starts_on': '2020-01-01',
        'definition': {'rule': {'kind': 'max_duties', 'maximum': 3}, 'severity': 'mandatory'}})['record'] for _ in range(2)]
    token = h.client('approver').cookies['rota_session']
    with transaction(h.app.state.engine, write=True) as db:
        actor = authenticate(db, token)
        db.add(m.SourceMapping(department_id=actor.department_id, source_name=SOURCE,
            source_key='policy:synthetic-legacy', target_kind='policies', target_id=rows[0]['id']))
        db.flush()
        projected = policies.policy_versions(db, actor, rows)
        assert projected[0]['reason'] == 'Imported scheduling requirement for review.'
        assert projected[1]['reason'] == raw
        assert [db.get(m.Policy, row['id']).source for row in rows] == [raw, raw]
        histories = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.object_id.in_(
            {row['id'] for row in rows}))))
        assert all(history.reason == raw and history.snapshot['source'] == raw for history in histories)


def test_policy_version_metadata_uses_a_fixed_query_count(h):
    policy = h.policy({'kind': 'max_duties', 'maximum': 3})
    token = h.client('approver').cookies['rota_session']
    source = {**policy, 'source': 'Reference synthetic; archive ' + 'd' * 64}
    statements = []

    def count(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, token)
        sql_event.listen(h.app.state.engine, 'before_cursor_execute', count)
        try:
            policies.policy_versions(db, actor, [source])
            small = len(statements)
            statements.clear()
            versions = policies.policy_versions(db, actor, [source, *[
                {**source, 'id': str(uuid4())} for _ in range(39)]])
            assert len(versions) == 40 and len(statements) == small == 4
        finally:
            sql_event.remove(h.app.state.engine, 'before_cursor_execute', count)


def test_exception_review_keeps_alternative_draft_counts_separate(h):
    policy = h.policy({'kind': 'max_hours', 'hours': 6, 'window': 'day'}, exception=True)
    first = h.shift(h.workspace('First alternative'))['workspace']
    second = h.shift(h.workspace('Second alternative'))['workspace']
    second = h.shift(second, '2026-10-04T13:00:00+03:00', '2026-10-04T17:00:00+03:00')['workspace']
    row = exception(h, policy)
    result = approval(h, row)
    assert result['complete'] and result['person']['id'] == row['person_id']
    assert (result['starts_on'], result['ends_on']) == ('2026-10-04', '2026-10-05')
    counts = {context['workspace_id']: [count for line in context['lines'] for count in line['counts']]
              for context in result['current_load']}
    assert [count['actual'] for count in counts[first['id']]] == [4]
    assert [count['actual'] for count in counts[second['id']]] == [8]
    assert all(count['limit'] == 6 for rows in counts.values() for count in rows)


def test_exception_review_loads_issued_duties_with_current_rule_without_a_draft(h):
    published = h.publish(h.shift(h.workspace())['workspace'])
    policy = h.policy({'kind': 'max_hours', 'hours': 1, 'window': 'day'}, exception=True)
    result = approval(h, exception(h, policy))
    [context] = result['current_load']
    assert result['complete'] and context['status'] == 'published'
    assert context['publication_id'] == published['publication_id']
    [count] = context['lines'][0]['counts']
    assert (count['actual'], count['limit']) == (4, 1)


def test_exception_review_uses_full_canonical_quota_window(h):
    policy = h.policy({'kind': 'block_quota', 'metric': 'scheduled_hours', 'comparison': 'maximum', 'amount': 2,
        'starts_on': '2026-10-04', 'ends_on': '2026-10-11'}, exception=True)
    h.shift(h.workspace())
    result = approval(h, exception(h, policy, last='2026-10-11'))
    assert result['complete']
    [count] = result['current_load'][0]['lines'][0]['counts']
    assert (count['actual'], count['amount']) == (4, 2)
    assert (count['starts_on'], count['ends_on']) == ('2026-10-04', '2026-10-11')


def test_published_quota_load_uses_the_full_exception_window_across_publications(h):
    h.publish(h.shift(h.workspace())['workspace'])
    later = h.workspace('Later issued rota', start='2026-10-11', end='2026-10-18')
    h.publish(h.shift(later, '2026-10-11T08:00:00+03:00', '2026-10-11T12:00:00+03:00')['workspace'])
    policy = h.policy({'kind': 'block_quota', 'metric': 'scheduled_hours', 'comparison': 'minimum', 'amount': 6,
        'starts_on': '2026-10-04', 'ends_on': '2026-10-18'}, exception=True)
    result = approval(h, exception(h, policy, last='2026-10-18'))
    assert result['complete'] and len(result['current_load']) == 2
    for context in result['current_load']:
        assert context['status'] == 'published'
        [count] = context['lines'][0]['counts']
        assert (count['actual'], count['amount']) == (8, 6)
        assert (count['starts_on'], count['ends_on']) == ('2026-10-04', '2026-10-18')


def test_exception_review_without_schedule_is_unknown_and_staff_cannot_read_it(h):
    policy = h.policy({'kind': 'max_duties', 'maximum': 3}, exception=True)
    row = exception(h, policy)
    result = approval(h, row)
    assert result['current_load'] == [] and result['complete'] is False
    with pytest.raises(Problem) as refused:
        approval(h, row, 'staff')
    assert refused.value.status == 403


def test_exception_approval_endpoint_returns_typed_counts_and_scopes_its_target(h):
    policy = h.policy({'kind': 'max_hours', 'hours': 6, 'window': 'day'}, exception=True)
    h.shift(h.workspace())
    row = exception(h, policy)
    result = h.get('approver', f"/api/exceptions/{row['id']}/approval")
    assert result['complete'] and result['starts_on'] == row['starts_on'] and result['ends_on'] == row['ends_on']
    [count] = result['current_load'][0]['lines'][0]['counts']
    assert (count['actual'], count['limit']) == (4, 6)
    h.get('staff', f"/api/exceptions/{row['id']}/approval", status=403)
    with transaction(h.app.state.engine, write=True) as db:
        other = m.Department(organisation_id=h.seed['organisation_id'], name='Other synthetic department')
        db.add(other)
        db.flush()
        foreign = m.PolicyException(department_id=other.id, policy_id=policy['id'], policy_revision=policy['revision'],
            person_id=row['person_id'], starts_on=m.now().date(), ends_on=m.now().date() + timedelta(days=1),
            reason='Synthetic foreign request', created_by=h.seed['identities']['coordinator'])
        db.add(foreign)
        db.flush()
        foreign_id = foreign.id
    h.get('approver', f'/api/exceptions/{foreign_id}/approval', status=404)


def test_future_import_reason_is_short_while_source_and_existing_evidence_are_preserved(h):
    checksum = 'c' * 64
    raw = 'Synthetic imported scheduling requirement; archive ' + checksum
    row = h.command('coordinator', 'policy.create', {'name': 'Synthetic imported duty limit',
        'owner': 'Synthetic reviewer', 'source': raw, 'starts_on': '2026-10-04',
        'definition': {'rule': {'kind': 'max_duties', 'maximum': 1}}})['record']
    token = h.client('approver').cookies['rota_session']
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.SourceMapping(department_id=h.seed['department_id'], source_name=SOURCE,
            source_key='policy:synthetic-future', target_kind='policies', target_id=row['id']))
        db.flush()
        policy = db.get(m.Policy, row['id'])
        entry = db.scalar(select(m.DefinitionHistory).where(m.DefinitionHistory.object_id == policy.id))
        old = (record(policy), entry.snapshot.copy(), entry.reason)
        actor = authenticate(db, token)
        for _ in range(2):
            [projected] = policies.policy_versions(db, actor, [row])
            assert projected['reason'] == 'Imported scheduling requirement for review.'
            assert entry.snapshot['source'] == policy.source == raw and checksum in policy.source
            assert (record(policy), entry.snapshot, entry.reason) == old
