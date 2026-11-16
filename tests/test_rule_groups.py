"""A rule can aim at a group of people (a tag, or a capability) instead of naming each person."""
from apps.api import models as m
from apps.api.db import transaction


def rota_with_short_rest(h, people):
    w = h.workspace('Group rota', '2026-10-04', '2026-10-11')
    for i, person in enumerate(people):
        w = h.shift(w, f'2026-10-0{4 + i}T08:00:00+03:00', f'2026-10-0{4 + i}T20:00:00+03:00', people=[person])['workspace']
        w = h.shift(w, f'2026-10-0{5 + i}T04:00:00+03:00', f'2026-10-0{5 + i}T10:00:00+03:00', people=[person], location=1)['workspace']
    return w


def hits(h, w, rule):
    return {f.get('person_id') for f in h.lifecycle(w, 'validate')['workspace']['validation']['findings'] if f.get('rule_id') == rule['id']}


def test_tag_rule_applies_to_everyone_with_the_tag_and_follows_tag_changes(h):
    a, b, c = h.seed['people'][:3]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, a).tags = ['Resident']
        db.get(m.Person, c).tags = ['resident', 'Night']
    rest = h.policy({'kind': 'min_rest', 'hours': 12}, scope={'tags': ['Resident']})
    w = rota_with_short_rest(h, (a, b, c))
    assert hits(h, w, rest) == {a, c}


def test_group_matching_nobody_names_nobody(h):
    a, b, c = h.seed['people'][:3]
    rest = h.policy({'kind': 'min_rest', 'hours': 12}, scope={'tags': ['Nobody has this']})
    assert hits(h, rota_with_short_rest(h, (a, b, c)), rest) == set()


def test_rule_added_for_one_rota_applies_to_that_rota_only(h):
    a, b, c = h.seed['people'][:3]
    w = rota_with_short_rest(h, (a,))
    other = h.workspace('Other rota', '2026-10-04', '2026-10-11')
    other = h.shift(other, '2026-10-04T08:00:00+03:00', '2026-10-04T20:00:00+03:00', people=[b])['workspace']
    other = h.shift(other, '2026-10-05T04:00:00+03:00', '2026-10-05T10:00:00+03:00', people=[b], location=1)['workspace']
    rest = h.policy({'kind': 'min_rest', 'hours': 12}, workspace_id=w['id'])
    assert hits(h, w, rest) == {a} and hits(h, other, rest) == set()


def test_lines_of_one_rule_are_approved_together_and_retire_what_they_replace(h):
    a, b = h.seed['people'][:2]
    old = h.policy({'kind': 'min_rest', 'hours': 8}, scope={'person_ids': [a]})
    lines = [h.command('coordinator', 'policy.create', {'name': 'Rest target', 'owner': 'QA', 'source': 'Rule lines test', 'starts_on': '2020-01-01',
             'definition': {'rule': {'kind': 'min_rest', 'hours': hours}, 'scope': {'person_ids': [person]}}})['record'] for person, hours in ((a, 11), (b, 12))]
    result = h.command('approver', 'policy.activate_many', {'reason': 'Approved together', 'replaces_ids': [old['id']],
                       'items': [{'policy_id': p['id'], 'expected_revision': p['revision']} for p in lines]})
    assert [r['status'] for r in result['records']] == ['active', 'active']
    # Retired versions leave the operational catalogue (21e096a0d) and are read through the policy status list.
    retired = {p['id'] for p in h.get('coordinator', '/api/policies?status=retired')['policies']}
    assert old['id'] in retired


def test_approver_can_check_a_rule_but_not_schedule(h):
    w = h.shift(h.workspace('Approver check'), people=[])['workspace']
    proposal = h.command('coordinator', 'policy.create', {'name': 'Rest check', 'owner': 'QA', 'source': 'Approver check test', 'starts_on': '2020-01-01',
                         'definition': {'rule': {'kind': 'min_rest', 'hours': 11}}})['record']
    job = h.command('approver', 'job.create', {'workspace_id': w['id'], 'kind': 'feasibility', 'check_seconds': 10,
                    'rule_changes': [{'policy_id': proposal['id'], 'expected_revision': proposal['revision']}]}, revision=w['revision'])['job']
    assert job['kind'] == 'feasibility'
    h.command('approver', 'job.create', {'workspace_id': w['id'], 'kind': 'generation'}, revision=w['revision'], status=403)


def test_approver_rule_check_runs_to_completion(h):
    from apps.worker.main import process_one
    w = h.shift(h.workspace('Approver check run'), people=[])['workspace']
    proposal = h.command('coordinator', 'policy.create', {'name': 'Rest run', 'owner': 'QA', 'source': 'Approver run test', 'starts_on': '2020-01-01',
                         'definition': {'rule': {'kind': 'min_rest', 'hours': 11}}})['record']
    job = h.command('approver', 'job.create', {'workspace_id': w['id'], 'kind': 'feasibility', 'check_seconds': 10,
                    'rule_changes': [{'policy_id': proposal['id'], 'expected_revision': proposal['revision']}]}, revision=w['revision'])['job']
    process_one(h.app.state.engine)
    done = h.get('approver', '/api/jobs/' + job['id'])['job']
    assert done['status'] == 'succeeded' and done['error_code'] is None
