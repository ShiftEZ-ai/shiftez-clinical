"""Workload reads retain the full rota quota evaluation."""
from copy import deepcopy

import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import person_workload
from apps.api.snapshots import domain_snapshot
from packages.domain import quotas
from packages.domain.validation import evaluate
from .test_generation_population import scope
from .test_rule_catalogue_api import approve, line, person, propose


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_workload_quota_snapshot_matches_full_read(h, monkeypatch):
    subject, other = h.seed['people'][:2]
    dates = {'starts_on': '2026-10-04', 'ends_on': '2026-10-11'}
    rate = {'prorate_department_days': True, 'reference_days': 7}
    own_line = line(who=person(subject), measure='duties', period={'kind': 'block', **dates},
                    settings={'block_quota': rate}, numbers={'target': {'value': 12, 'level': 'medium'}})
    catalogue = approve(h, propose(h, 'workload_limit', [own_line], catalogue_version=2)['record'])
    target = {'kind': 'block_quota', 'metric': 'duties', 'comparison': 'target', 'amount': 6, **dates, **rate}
    stale, legacy = [h.policy({**target, 'metric': metric}, {'person_id': subject}, severity='advisory')
                    for metric in ('duties', 'scheduled_hours')]
    maximum = h.policy({**target, 'comparison': 'maximum', 'amount': 3,
                        'prorate_department_days': False}, {'person_id': subject})
    created = h.shift(h.workspace(), people=[])
    unassigned = h.detail(created['workspace'])['shifts'][0]['id']
    workspace = h.shift(created['workspace'], people=[other], start='2026-10-05T08:00:00+03:00',
                        end='2026-10-05T12:00:00+03:00')['workspace']
    for rule in (catalogue, stale, legacy):
        workspace = h.command('coordinator', 'workspace.target_override', {
            'policy_id': rule['id'], 'person_id': subject, 'amount': 4.57, 'reason': 'Agreed workload',
        }, workspace['id'], workspace['revision'])['workspace']
    query = f"/api/people/{subject}/workload?workspace_id={workspace['id']}&on=2026-10-06"

    def read():
        checks = []

        def capture(data, **kwargs):
            checked = evaluate(data, **kwargs)
            report = checked['quota_report']
            checks.append((checked['findings'], {key: value for key, value in report.items() if key != 'fingerprint'}))
            return checked

        with monkeypatch.context() as patch:
            patch.setattr(person_workload, 'evaluate', capture)
            response = h.get('coordinator', query)
        [checked] = checks
        return response, checked

    def compare():
        response, checked = read()
        with monkeypatch.context() as patch:
            def full(db, chosen, **_):
                return domain_snapshot(db, chosen, include_fairness=False, include_objectives=False)

            patch.setattr(person_workload, 'domain_snapshot', full)
            assert (response, checked) == read()
        assert response['rule_basis'] == 'captured_rota_quotas'
        [rota] = [period for period in response['periods'] if period['key'] == 'this_rota']
        return rota, checked[0]

    before, findings = compare()
    assert before['complete']
    assert all(f['code'] != 'TARGET_OVERRIDE_STALE' for f in findings)

    with transaction(h.app.state.engine, write=True) as db:
        stored = db.get(m.Workspace, workspace['id'])
        overrides = deepcopy(stored.target_overrides)
        by_policy = {row['policy_id']: row for row in overrides}
        by_policy[stale['id']]['rule_fingerprints'] = ['0' * 64]
        by_policy[legacy['id']].pop('rule_fingerprints')
        stored.target_overrides = overrides
        stored.content_revision += 1
        # An older receipt has no resolved audience. Its only eligible session has no assignments.
        db.add(m.Job(kind='generation', status='applied', workspace_id=stored.id,
                     department_id=stored.department_id, requested_by=h.seed['identities']['coordinator'],
                     input_fingerprint='0' * 64, input_snapshot={'generation_session_ids': [unassigned],
                     'generation_scope': scope('person_ids', subject)}))
        live = db.get(m.Policy, maximum['id'])
        live.definition = {**live.definition, 'rule': {**live.definition['rule'], 'amount': 1}}
        live.revision += 1
        db.get(m.Organisation, h.seed['organisation_id']).input_revision += 1

    after, findings = compare()
    rows = {row['policy_id']: row for row in after['quotas']}
    assert not after['complete']
    assert rows[maximum['id']]['amount'] == 1
    assert rows[catalogue['id']]['line_id'] == own_line['id']
    assert rows[catalogue['id']]['amount'] == 4.57 and rows[catalogue['id']]['status'] != 'unresolved'
    assert all(rows[rule['id']]['target_calculation']['rule_fingerprint'] for rule in (catalogue, stale, legacy))
    assert {f['rule_id'] for f in findings if f['code'] == 'TARGET_OVERRIDE_STALE'} == {stale['id'], legacy['id']}
    for rule in (stale, legacy):
        assert rows[rule['id']]['status'] == 'unresolved'

    monkeypatch.setattr(quotas, 'MAX_PARTICIPANTS', 0)
    limited, findings = compare()
    assert not limited['complete'] and not limited['quotas']
    assert any(f['code'] == 'QUOTA_SCOPE_LIMIT' for f in findings)
