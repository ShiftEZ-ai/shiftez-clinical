"""Cause contracts reach operator APIs without changing checks, privacy or Undo."""
import copy
import json

from sqlalchemy import func, select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services.issues import issue_key
from apps.api.services.leave_disclosure import staff_overlap_labels
from packages.contracts.schemas import FindingCause
from .test_series_reconciliation import template
from .test_timeline_layers import stored_publication


def minimum(h, people, *, severity='mandatory', scoped=False):
    scope = {'person_ids': people, 'service_id': h.seed['services'][0]}
    if scoped:
        scope.update(location_id=h.seed['locations'][0], weekdays=[6], start_time='08:00', end_time='12:00')
    return h.policy({'kind': 'block_quota', 'metric': 'duties', 'comparison': 'minimum', 'amount': 1,
        'starts_on': '2026-10-04', 'ends_on': '2026-10-11', 'assignment_type_ids': [h.seed['type']]},
        scope, severity=severity)


def stored_state(h, workspace):
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Workspace, workspace['id'])
        return (row.revision, copy.deepcopy(row.validation),
                db.scalar(select(func.count()).select_from(m.AuditEvent)),
                db.scalar(select(func.count()).select_from(m.CommandReceipt)))


def test_missing_sessions_contract_groups_people_and_suggests_existing_templates_without_writes(h):
    people = h.seed['people'][:2]
    active = template(h, days=(6,))
    inactive = template(h, days=(6,), active=False)
    expired = template(h, days=(6,), starts_on='2026-09-01', ends_on='2026-10-01')
    unrelated = template(h, days=(0,))
    policy = minimum(h, people, scoped=True)
    workspace = h.lifecycle(h.workspace(), 'validate')['workspace']
    check = h.detail(workspace)['workspace']['validation']
    findings = [f for f in check['findings'] if f['rule_id'] == policy['id']]
    assert {f['person_id'] for f in findings} == set(people)
    for finding in findings:
        cause = finding['cause']
        assert cause['code'] == 'no_candidate_sessions'
        assert (cause['sessions_found'], cause['eligible_people'], cause['places_available'], cause['days_in_window']) == (0, 0, 0, 7)
        assert cause['required'] == 1 and cause['possible'] == 0
        [hint] = finding['fix_hints']
        assert hint['action'] == 'add_sessions'
        prefill = hint['prefill']
        assert (prefill['starts_on'], prefill['ends_on']) == ('2026-10-04', '2026-10-11')
        assert prefill['assignment_type_ids'] == [h.seed['type']]
        assert prefill['service_ids'] == [h.seed['services'][0]]
        assert prefill['location_id'] == h.seed['locations'][0]
        assert (prefill['weekdays'], prefill['start_time'], prefill['end_time']) == ([6], '08:00', '12:00')
        matches = {item['id']: item for item in hint['matching_series']}
        assert {identity: item['action'] for identity, item in matches.items()} == {
            active['id']: 'expand', inactive['id']: 'reactivate', expired['id']: 'extend'}
        assert unrelated['id'] not in matches
        assert matches[inactive['id']]['active'] is False
        assert matches[expired['id']]['ends_on'] == '2026-10-01'
        assert all(item['revision'] >= 1 for item in matches.values())

    before = stored_state(h, workspace)
    summary = h.get('coordinator', f"/api/workspaces/{workspace['id']}/summary")
    assert stored_state(h, workspace) == before
    assert summary['issues']['blocking'] == check['error_count'] == 2
    [group] = summary['cause_groups']
    assert (group['code'], group['severity'], group['count']) == ('no_candidate_sessions', 'error', 2)
    assert set(group['person_ids']) == set(people) and group['rule_ids'] == [policy['id']]
    assert set(group['finding_keys']) == {issue_key(f) for f in findings}
    projected = {f['key']: f for f in summary['findings']}
    for finding in findings:
        projected_finding = projected[issue_key(finding)]
        assert projected_finding['person_id'] == finding['person_id']
        assert projected_finding['cause'] == FindingCause.model_validate(finding['cause']).model_dump(mode='json')
        assert projected_finding['fix_hints'] == finding['fix_hints']
        assert projected_finding['accepted'] is False
    h.get('staff', f"/api/workspaces/{workspace['id']}/summary", status=403)


def test_missing_session_fix_uses_existing_audited_session_command_and_undo(h):
    people = h.seed['people'][:2]
    policy = minimum(h, people)
    workspace = h.lifecycle(h.workspace(), 'validate')['workspace']
    before = stored_state(h, workspace)
    [hint] = next(f for f in before[1]['findings'] if f['rule_id'] == policy['id'])['fix_hints']
    prefill = hint['prefill']
    created = h.shift(workspace, people=people, typ=prefill['assignment_type_ids'][0])
    workspace = created['workspace']
    checked = h.lifecycle(workspace, 'validate')['workspace']
    assert not [f for f in checked['validation']['findings'] if f['rule_id'] == policy['id']]
    assert not h.get('coordinator', f"/api/workspaces/{workspace['id']}/summary")['cause_groups']
    assert stored_state(h, workspace)[2] > before[2]
    audit_count = stored_state(h, checked)[2]
    undone = h.command('coordinator', 'workspace.undo', {'reason': 'Restore missing-session test case'},
                       checked['id'], checked['revision'])['workspace']
    assert h.detail(undone)['shifts'] == []
    restored = [f for f in undone['validation']['findings'] if f['rule_id'] == policy['id']]
    assert len(restored) == 2 and all(f['cause']['code'] == 'no_candidate_sessions' for f in restored)
    assert stored_state(h, undone)[2] > audit_count


def test_leave_fix_hints_do_not_disclose_private_reasons_or_other_people_to_staff(h):
    absent = h.seed['people'][1]
    private_reason = 'Synthetic confidential reason for leave'
    policy = minimum(h, [absent], severity='advisory')
    h.command('coordinator', 'availability.record', {'person_id': absent, 'kind': 'recorded_leave',
        'leave_type': 'Annual leave', 'start': '2026-10-04T00:00:00+03:00',
        # leave four of five working days so the rounded minimum remains one and the cause stays visible.
        'end': '2026-10-08T00:00:00+03:00', 'private_reason': private_reason})
    workspace = h.shift(h.workspace(), people=[h.seed['people'][0]])['workspace']
    check = h.detail(workspace)['workspace']['validation']
    finding = next(f for f in check['findings'] if f['rule_id'] == policy['id'])
    assert finding['cause']['code'] == 'person_unavailable'
    assert [hint['action'] for hint in finding['fix_hints']] == ['open_leave']
    assert finding['fix_hints'][0]['prefill']['person_id'] == absent
    summary = h.get('coordinator', f"/api/workspaces/{workspace['id']}/summary")
    assert private_reason not in json.dumps({'check': check, 'summary': summary})
    assert 'Annual leave' not in json.dumps(finding['fix_hints'])
    published = h.publish(workspace)
    operator = h.get('coordinator', f"/api/publications/{published['publication_id']}/coverage")['evaluation']
    assert any(f.get('cause', {}).get('code') == 'person_unavailable' for f in operator['findings'])
    personal = h.get('staff', f"/api/publications/{published['publication_id']}/coverage")['evaluation']
    assert personal['findings'] == [] and not personal.get('cause_groups')
    assert private_reason not in json.dumps(personal)
    assert absent not in json.dumps(personal)


def test_staff_timeline_hides_recurring_template_fix_hints_without_changing_operator_evidence(h):
    own = h.seed['people'][0]
    private_name = 'Synthetic private recurring template'
    series = template(h, days=(6,), name=private_name)
    policy = minimum(h, [own], severity='advisory', scoped=True)
    url = '/api/timeline?start=2026-10-01&end=2026-11-01&layers=conflict'
    assert all(not person['conflicts'] for person in h.get('coordinator', url)['people'])
    checked = h.lifecycle(h.workspace(), 'validate')['workspace']
    stored_publication(h, findings=checked['validation']['findings'])

    def own_finding(body):
        person = next(person for person in body['people'] if person['id'] == own)
        return next(finding for finding in person['conflicts'] if finding['rule_id'] == policy['id'])

    operator = own_finding(h.get('coordinator', url))
    matches = [item for hint in operator['fix_hints'] for item in hint.get('matching_series', [])]
    assert any(item['id'] == series['id'] and item['name'] == private_name for item in matches)
    staff = h.get('staff', url)
    finding = own_finding(staff)
    assert finding['cause'] == operator['cause']
    assert finding['fix_hints'] and all(not hint.get('matching_series') for hint in finding['fix_hints'])
    assert private_name not in json.dumps(staff) and series['id'] not in json.dumps(staff)
    assert own_finding(h.get('coordinator', url)) == operator


def test_staff_template_projection_recurses_without_mutating_source():
    source = {'derived_findings': [{'fix_hints': [{'matching_series': [{'name': 'Private template'}]}]}]}
    original = copy.deepcopy(source)
    assert staff_overlap_labels(source) == {'derived_findings': [{'fix_hints': [{'matching_series': []}]}]}
    assert source == original
