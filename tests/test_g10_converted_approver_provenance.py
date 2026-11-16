"""Converted catalogue reads retain source approval provenance."""

from uuid import uuid4

from apps.api import models as m
from apps.api.auth import authenticate
from apps.api.db import transaction
from apps.api.services import policies
from .governance_support import identity
from .test_rule_catalogue_v2_api import proposed as conversion_proposal


def _catalogue_policies(page):
    for entry in page['entries']:
        for policy in (entry.get('rule'), entry.get('proposed')):
            if policy:
                yield policy
        yield from entry.get('also_in_force') or ()


def test_converted_catalogue_rule_keeps_source_approver(h):
    identity(h, 'approver_x', ['approver'])
    identity(h, 'approver_y', ['approver'])
    source = h.command('coordinator', 'rule.propose', {
        'catalogue_id': 'duties_per_week', 'catalogue_version': 1,
        'lines': [{'id': str(uuid4()), 'numbers': {'max': {'value': 3, 'level': 'mandatory'}}}],
        'reason': 'Synthetic v1 source approval'})['record']
    h.command('approver_x', 'policy.activate', {'reason': 'Approve the v1 source'}, source['id'], source['revision'])

    conversion = conversion_proposal(h)
    active = h.command('approver_y', 'rule.catalogue.activate',
                       {'reason': 'Approve the department conversion'},
                       conversion['id'], conversion['revision'])['record']
    target_ids = {target['row']['id'] for target in active['plan']['targets']}

    retired = h.get('approver_y', '/api/policies?status=retired')['policies']
    source_read = next(row for row in retired if row['id'] == source['id'])
    assert source_read['approved_by_name'] == 'Fictional approver_x' and source_read['approved_at']

    page = h.get('approver_y', '/api/rules/catalogue?version=2&include_hidden=true')
    converted = next(policy for policy in _catalogue_policies(page) if policy['id'] in target_ids)
    assert converted['approved_by_name'] == 'Fictional approver_y'
    assert converted['approved_at']

    token = h.client('approver_y').cookies['rota_session']
    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, token)
        stored = db.get(m.Policy, source['id'])
        effective = policies.effective_source(db, actor, stored)
        [projected] = policies.policy_versions(db, actor, [effective])
    assert projected['_v2_source']
    assert projected['approved_by_name'] == 'Fictional approver_y'
    assert projected['approved_at'] == active['approved_at']
    assert projected['approved_by_name'] != source_read['approved_by_name']
