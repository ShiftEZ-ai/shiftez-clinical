"""Exception withdrawal/end retains evidence and never grants an unreviewed waiver."""
from datetime import date, datetime, timedelta
import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from apps.api.snapshots import domain_snapshot
from packages.contracts import schemas as s
from packages.domain.rule_catalogue_migration import lines_of
from packages.domain.time import now, pin_clock
from packages.domain.validation import evaluate


REASON = 'Synthetic exception lifecycle review'


def request(h, policy, starts='2026-10-04', ends='2026-10-11'):
    return h.command('coordinator', 'exception.create', {'policy_id': policy['id'],
        'person_id': h.seed['people'][0], 'starts_on': starts, 'ends_on': ends, 'reason': REASON})['record']


def issued_publication(h, publication_id):
    # `operational.as_of` is the read time of the live absence overlay (d32430440), not issued evidence.
    read = h.get('auditor', '/api/publications/' + publication_id)
    return {**read, 'operational': {k: v for k, v in read['operational'].items() if k != 'as_of'}}


def row(h, role, exception):
    return next(r for r in h.get(role, '/api/catalogues')['exceptions'] if r['id'] == exception['id'])


def test_withdrawal_is_guarded_audited_undoable_and_clears_conversion(h):
    policy = h.policy({'kind': 'max_duties', 'maximum': 3}, exception=True)
    exc = request(h, policy)
    assert row(h, 'coordinator', exc)['can_end'] is True
    assert row(h, 'approver', exc)['can_end'] is True
    assert row(h, 'auditor', exc)['can_end'] is False
    h.command('staff', 'exception.end', {'reason': REASON}, exc['id'], exc['revision'], status=403)
    h.command('coordinator', 'exception.end', {'reason': REASON}, exc['id'], status=428)
    withdrawn = h.command('coordinator', 'exception.end', {'reason': REASON}, exc['id'], exc['revision'])['record']
    assert withdrawn['status'] == 'withdrawn' and withdrawn['revision'] == exc['revision'] + 1
    assert withdrawn['starts_on'] == exc['starts_on'] and withdrawn['ends_on'] == exc['ends_on']
    assert row(h, 'coordinator', withdrawn)['can_undo'] is True
    assert h.get('coordinator', '/api/rules/catalogue/migration?version=2')['blocked'] == []
    h.command('coordinator', 'exception.end', {'reason': REASON}, exc['id'], exc['revision'], status=409)
    restored = h.command('coordinator', 'exception.undo', {'reason': REASON}, exc['id'], withdrawn['revision'])['record']
    assert restored['status'] == 'proposed' and restored['approved_by'] is None
    assert h.get('coordinator', '/api/rules/catalogue/migration?version=2')['blocked']
    actions = {e['action'] for e in h.get('auditor', '/api/audit')['events'] if e['target_id'] == exc['id']}
    assert {'exception.end', 'exception.undo'} <= actions
    with transaction(h.app.state.engine) as db:
        history = list(db.scalars(select(m.DefinitionHistory).where(
            m.DefinitionHistory.kind == 'exception', m.DefinitionHistory.object_id == exc['id'])
            .order_by(m.DefinitionHistory.object_revision)))
        assert [entry.snapshot['status'] for entry in history] == ['proposed', 'withdrawn', 'proposed']


def test_approved_exception_end_restores_constraint_and_undo_keeps_independent_approval(h):
    policy = h.policy({'kind': 'max_hours', 'hours': 1, 'window': 'day'}, exception=True)
    workspace = h.shift(h.workspace())['workspace']
    exc = request(h, policy)
    approved = h.command('approver', 'exception.approve', {'reason': REASON}, exc['id'], exc['revision'])['record']
    assert h.lifecycle(workspace, 'validate')['validation']['valid'] is True
    assert row(h, 'coordinator', approved)['can_end'] is False
    assert row(h, 'approver', approved)['can_end'] is True
    h.command('coordinator', 'exception.end', {'reason': REASON}, exc['id'], approved['revision'], status=403)
    ended = h.command('approver', 'exception.end', {'reason': REASON}, exc['id'], approved['revision'])['record']
    assert ended['status'] == 'ended' and ended['approved_by'] == approved['approved_by']
    current = h.detail(workspace)['workspace']
    assert h.lifecycle(current, 'validate')['validation']['valid'] is False
    with transaction(h.app.state.engine, write=True) as db:
        for role in ('coordinator', 'admin'):
            db.get(m.Identity, h.seed['identities'][role]).person_id = exc['person_id']
    assert row(h, 'admin', ended)['can_undo'] is False
    refused = h.command('admin', 'exception.undo', {'reason': REASON}, exc['id'], ended['revision'], status=403)
    assert refused['error']['code'] == 'SELF_APPROVAL'
    restored = h.command('approver', 'exception.undo', {'reason': REASON}, exc['id'], ended['revision'])['record']
    assert restored['status'] == 'approved' and restored['approved_by'] == approved['approved_by']
    current = h.detail(workspace)['workspace']
    assert h.lifecycle(current, 'validate')['validation']['valid'] is True
    h.command('approver', 'exception.undo', {'reason': REASON}, exc['id'], restored['revision'], status=409)


def test_approver_can_clear_and_restore_stale_request_without_granting_waiver(h):
    policy = h.policy({'kind': 'max_duties', 'maximum': 3}, exception=True)
    exc = request(h, policy)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed['identities']['coordinator']).disabled = True
        source = db.get(m.Policy, policy['id'])
        source.revision += 1
    withdrawn = h.command('approver', 'exception.end', {'reason': REASON}, exc['id'], exc['revision'])['record']
    assert withdrawn['status'] == 'withdrawn'
    assert row(h, 'approver', withdrawn)['can_undo'] is True
    restored = h.command('approver', 'exception.undo', {'reason': REASON}, exc['id'], withdrawn['revision'])['record']
    assert restored['status'] == 'proposed' and restored['approved_by'] is None
    refused = h.command('approver', 'exception.approve', {'reason': REASON}, exc['id'], restored['revision'], status=409)
    assert refused['error']['code'] == 'STALE_POLICY'


def test_ended_exception_cannot_be_restored_against_a_changed_policy(h):
    policy = h.policy({'kind': 'max_duties', 'maximum': 3}, exception=True)
    exc = request(h, policy)
    approved = h.command('approver', 'exception.approve', {'reason': REASON}, exc['id'], exc['revision'])['record']
    ended = h.command('approver', 'exception.end', {'reason': REASON}, exc['id'], approved['revision'])['record']
    h.command('approver', 'policy.retire', {'reason': REASON}, policy['id'], policy['revision'])
    assert row(h, 'approver', ended)['can_undo'] is False
    refused = h.command('approver', 'exception.undo', {'reason': REASON}, exc['id'], ended['revision'], status=409)
    assert refused['error']['code'] == 'STALE_POLICY'


def test_ended_exception_retains_undo_to_equivalent_converted_source(h):
    from .test_rule_catalogue_v2_api import proposed

    policy = h.policy({'kind': 'max_duties', 'maximum': 3}, exception=True)
    exc = request(h, policy)
    approved = h.command('approver', 'exception.approve', {'reason': REASON}, exc['id'], exc['revision'])['record']
    ended = h.command('approver', 'exception.end', {'reason': REASON}, exc['id'], approved['revision'])['record']
    change = proposed(h)
    h.command('approver', 'rule.catalogue.activate', {'reason': REASON}, change['id'], change['revision'])
    assert row(h, 'approver', ended)['can_undo'] is True
    restored = h.command('approver', 'exception.undo', {'reason': REASON}, exc['id'], ended['revision'])['record']
    assert restored['status'] == 'approved' and restored['policy_revision'] == policy['revision']


def test_ending_historical_safety_exception_retains_evidence_but_cannot_restore_waiver(h):
    policy = h.policy({'kind': 'min_rest', 'hours': 11})
    with transaction(h.app.state.engine, write=True) as db:
        exc = m.PolicyException(department_id=policy['department_id'], policy_id=policy['id'],
            policy_revision=policy['revision'], person_id=h.seed['people'][0], starts_on=date(2026, 10, 4),
            ends_on=date(2026, 10, 11), reason=REASON, created_by=h.seed['identities']['coordinator'],
            approved_by=h.seed['identities']['approver'], status='approved')
        db.add(exc)
        db.flush()
        ident, revision = exc.id, exc.revision
    ended = h.command('approver', 'exception.end', {'reason': REASON}, ident, revision)['record']
    assert row(h, 'approver', ended)['can_undo'] is False
    refused = h.command('approver', 'exception.undo', {'reason': REASON}, ident, ended['revision'], status=409)
    assert refused['error']['code'] == 'SAFETY_RULE'
    with transaction(h.app.state.engine) as db:
        retained = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.kind == 'exception',
            m.DefinitionHistory.object_id == ident).order_by(m.DefinitionHistory.object_revision)))
        assert [r.snapshot['status'] for r in retained] == ['approved', 'ended']


@pytest.mark.parametrize('legacy', [False, True])
def test_ending_exception_preserves_published_findings_summary_and_coverage_but_changes_drafts(h, legacy):
    policy = h.policy({'kind': 'max_hours', 'hours': 1, 'window': 'day'}, exception=True)
    if legacy:
        with transaction(h.app.state.engine, write=True) as db:
            exc = m.PolicyException(department_id=policy['department_id'], policy_id=policy['id'],
                policy_revision=policy['revision'], person_id=h.seed['people'][0], starts_on=date(2026, 10, 4),
                ends_on=date(2026, 10, 11), reason=REASON, created_by=h.seed['identities']['coordinator'],
                approved_by=h.seed['identities']['approver'], status='approved')
            db.add(exc)
            db.flush()
            approved = record(exc)
    else:
        exc = request(h, policy)
        approved = h.command('approver', 'exception.approve', {'reason': REASON}, exc['id'], exc['revision'])['record']
    published = h.publish(h.shift(h.workspace())['workspace'])
    workspace, publication_id = published['workspace'], published['publication_id']
    issued = issued_publication(h, publication_id)
    summary = h.get('coordinator', '/api/workspaces/' + workspace['id'] + '/summary')
    coverage = h.get('coordinator', '/api/publications/' + publication_id + '/coverage')
    assert any(f['code'] == 'RULE_EXCEPTION' and f['detail']['exception_id'] == approved['id']
               for f in issued['original_validation']['findings'])
    draft = h.workspace('Reviewed amendment after exception ended', base=publication_id)
    present = now()
    try:
        pin_clock(present + timedelta(minutes=1))
        h.command('approver', 'exception.end', {'reason': REASON}, approved['id'], approved['revision'])
        assert issued_publication(h, publication_id) == issued
        after = h.get('coordinator', '/api/workspaces/' + workspace['id'] + '/summary')
        for key in ('valid', 'issues', 'findings', 'score', 'places_needed', 'places_filled', 'places_open'):
            assert after[key] == summary[key]
        checked = h.get('coordinator', '/api/publications/' + publication_id + '/coverage')
        assert checked['evaluation']['findings'] == coverage['evaluation']['findings']
        assert checked['evaluation']['valid'] is True
        with transaction(h.app.state.engine) as db:
            source = db.get(m.Workspace, workspace['id'])
            historical = domain_snapshot(db, source)
            current = domain_snapshot(db, source, current_scheduling=True)
            retained = next(e for e in historical['exceptions'] if e['id'] == approved['id'])
            assert retained['status'] == 'approved' and retained['policy_revision'] == approved['policy_revision']
            assert retained['starts_on'] == approved['starts_on'] and retained['ends_on'] == approved['ends_on']
            assert any(f['code'] == 'MAX_HOURS' for f in evaluate(current)['findings'])
            if legacy:
                histories = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.kind == 'exception',
                    m.DefinitionHistory.object_id == approved['id'])))
                assert all(e.created_at > db.get(m.Publication, publication_id).created_at for e in histories)
        # D11: an amendment checks only changed duties; the untouched duty keeps its issued evidence (the waiver).
        check = h.lifecycle(draft, 'validate')['validation']
        assert check['scope'] == 'published_changes'
        assert not any(f['code'] == 'MAX_HOURS' for f in check['findings'])
        # Once the draft changes that duty, it is checked against current inputs: the ended exception no longer waives it.
        [shift] = h.detail(draft)['shifts']
        end = (datetime.fromisoformat(shift['end']) - timedelta(minutes=30)).isoformat()
        draft = h.command('coordinator', 'shift.update', {
            'workspace_id': draft['id'], 'shift_id': shift['id'],
            **{key: shift[key] for key in s.ShiftInput.model_fields if key not in {'participants', 'end'}}, 'end': end,
            'participants': [{'person_id': p['person_id'], 'start': p['start'], 'end': end} for p in shift['participants']]},
            revision=draft['revision'])['workspace']
        check = h.lifecycle(draft, 'validate')['validation']
        assert any(f['code'] == 'MAX_HOURS' for f in check['findings'])
    finally:
        pin_clock(present)


@pytest.mark.parametrize('requested_before', [False, True])
def test_later_exception_approval_never_changes_a_published_snapshot(h, requested_before):
    policy = h.policy({'kind': 'max_hours', 'hours': 10, 'window': 'day'}, exception=True)
    exc = request(h, policy) if requested_before else None
    published = h.publish(h.shift(h.workspace())['workspace'])
    present = now()
    try:
        pin_clock(present + timedelta(minutes=1))
        exc = exc or request(h, policy)
        approved = h.command('approver', 'exception.approve', {'reason': REASON}, exc['id'], exc['revision'])['record']
        with transaction(h.app.state.engine) as db:
            workspace = db.get(m.Workspace, published['workspace']['id'])
            historical = domain_snapshot(db, workspace)
            prior = next((e for e in historical['exceptions'] if e['id'] == exc['id']), None)
            assert (prior['status'] if prior else None) == ('proposed' if requested_before else None)
            current = domain_snapshot(db, workspace, current_scheduling=True)
            assert next(e for e in current['exceptions'] if e['id'] == exc['id'])['status'] == approved['status']
    finally:
        pin_clock(present)


@pytest.mark.parametrize('status,starts,ends,blocks', [
    ('approved', '2026-09-01', '2026-10-03', False),
    ('proposed', '2026-09-01', '2026-10-03', False),
    ('ended', '2026-10-04', '2026-10-11', False),
    ('withdrawn', '2026-10-04', '2026-10-11', False),
    ('declined', '2026-10-04', '2026-10-11', False),
    ('approved', '2026-10-02', '2026-10-04', True),
    ('proposed', '2026-10-04', '2026-10-11', True),
    ('approved', '2026-11-01', '2026-11-08', True),
])
def test_only_current_or_future_operative_exceptions_hold_conversion(h, status, starts, ends, blocks):
    policy = h.policy({'kind': 'max_duties', 'maximum': 3}, exception=True)
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.PolicyException(department_id=policy['department_id'], policy_id=policy['id'],
            policy_revision=policy['revision'], person_id=h.seed['people'][0], starts_on=date.fromisoformat(starts),
            ends_on=date.fromisoformat(ends), reason=REASON, created_by=h.seed['identities']['coordinator'],
            approved_by=h.seed['identities']['approver'] if status in {'approved', 'ended'} else None, status=status))
    v1 = h.get('coordinator', '/api/rules/catalogue/migration?version=1')
    assert any(r['rule_id'] == policy['id'] and 'person exceptions' in r['reason'] for r in v1['report']) is blocks
    v2 = h.get('coordinator', '/api/rules/catalogue/migration?version=2')
    assert any(r['rule_id'] == policy['id'] and 'person exceptions' in r['reason'] for r in v2['blocked']) is blocks


def test_blocker_contract_validates_precise_link_targets():
    ident = str(uuid.uuid4())
    blocker = s.RuleMigrationBlocker.model_validate({'kind': 'proposal', 'reason': REASON, 'proposal_ids': [ident]})
    assert blocker.model_dump()['proposal_ids'] == [ident]
    assert blocker.rule_id == '' and blocker.person_ids == [] and blocker.line_ids == []
    for field in ('proposal_ids', 'person_ids', 'line_ids'):
        with pytest.raises(ValidationError):
            s.RuleMigrationBlocker.model_validate({'kind': 'proposal', 'reason': REASON, field: ['not-an-id']})


def test_conversion_blockers_identify_pending_proposal_and_inheriting_person_line(h):
    person = h.seed['people'][0]
    inherited = h.policy({'kind': 'max_duties', 'maximum': 3})
    with transaction(h.app.state.engine, write=True) as db:
        policy = db.get(m.Policy, inherited['id'])
        policy.definition = {**policy.definition, 'listed_only': True, 'per_person': [{'person_id': person, 'amount': None}]}
        inherited['definition'] = policy.definition
    pending = h.command('coordinator', 'policy.create', {'name': 'Pending synthetic rule', 'owner': 'QA owner',
        'source': REASON, 'starts_on': '2020-01-01',
        'definition': {'rule': {'kind': 'max_hours', 'hours': 40}, 'severity': 'mandatory'}})['record']
    preview = h.get('coordinator', '/api/rules/catalogue/migration?version=2')
    parsed = [s.RuleMigrationBlocker.model_validate(b) for b in preview['blocked']]
    assert next(b for b in parsed if b.kind == 'proposal').proposal_ids == [pending['id']]
    own = next(b for b in parsed if b.rule_id == inherited['id'])
    assert own.person_ids == [person]
    assert own.line_ids == [lines_of(inherited)[1][0]['id']]
    assert own.reason.startswith('Not in the catalogue yet: ')
