"""PC13 published-route failures: typed cache dates and scope-free stored rule envelopes."""
from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import json

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import json_value
from packages.domain.input_manifest import digest
from packages.domain.validation import coverage_boundary_inputs
from tests.test_governed_leadership import activate, line, proposal


def test_manifest_digest_uses_the_same_date_encoding_as_snapshots():
    source = {'until': date(2026, 10, 11), 'nested': [datetime(2026, 10, 4, tzinfo=timezone.utc),
                                                   datetime(2026, 10, 5)]}
    frozen = deepcopy(source)
    encoded = json_value(source)
    expected = hashlib.sha256(json.dumps(encoded, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    assert digest(source) == digest(encoded) == expected
    assert digest({**source, 'until': date(2026, 10, 12)}) != expected
    assert source == frozen
    with pytest.raises(TypeError):
        digest({'unsupported': object()})


def test_coverage_boundary_clocks_accept_missing_scope_without_changing_stored_rules():
    data = {'organisation': {'timezone': 'UTC'}, 'memberships': [], 'eligibilities': [], 'grants': [],
            'categories': {}, 'capabilities': {}, 'policies': [
                {'definition': {'rule': {'kind': 'catalogue'}}, 'starts_on': '2026-10-05'},
                {'definition': {'scope': {'weekdays': [1], 'start_time': '09:00', 'end_time': '11:00'}},
                 'ends_on': '2026-10-07'}]}
    before = deepcopy(data)
    dates, clocks = coverage_boundary_inputs(data)
    assert dates == {datetime(2026, 10, day, tzinfo=timezone.utc) for day in (5, 7)}
    assert clocks == {(tuple(range(7)), '00:00', '24:00'), ((1,), '09:00', '11:00')}
    assert data == before


@pytest.mark.postgres
def test_publication_coverage_cache_tracks_typed_effective_end(pg, monkeypatch):
    from apps.api import main
    from tests.test_schedule_window import published_rota

    publication_id = published_rota(pg)
    calls, evaluate = [], main.evaluate
    def counted(*args, **kwargs):
        calls.append(True)
        return evaluate(*args, **kwargs)
    monkeypatch.setattr(main, 'evaluate', counted)
    with transaction(pg.app.state.engine, write=True) as db:
        publication = db.get(m.Publication, publication_id)
        publication.effective_until = date(2026, 10, 10)
        frozen = deepcopy(publication.snapshot)
    path = f'/api/publications/{publication_id}/coverage'
    first = pg.get('coordinator', path)['evaluation']
    assert len(first['coverage']) == 2
    assert pg.get('coordinator', path)['evaluation'] == first and len(calls) == 1
    with transaction(pg.app.state.engine, write=True) as db:
        # Publications may be trimmed further, never extended by rewriting their issued window.
        db.get(m.Publication, publication_id).effective_until = date(2026, 10, 5)
    second = pg.get('coordinator', path)['evaluation']
    assert len(second['coverage']) == 1 and len(calls) == 2
    with transaction(pg.app.state.engine) as db:
        assert db.get(m.Publication, publication_id).snapshot == frozen


@pytest.mark.postgres
def test_approved_rota_publishes_with_scope_free_catalogue_history_and_coverage(pg):
    a, b, _ = pg.seed['people']
    typ = pg.configure('assignment_types', {'name': 'Published leadership regression', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 2}]}})['record']['id']
    requirement = line(typ, [a, b])
    requirement['settings']['team_lead_requirement']['count_scope'] = 'concurrent_duties'
    leadership = activate(pg, proposal(pg, [requirement]))
    goal = pg.policy({'kind': 'balance_goal', 'metric': 'lead_turns', 'period': 'month', 'aim': 'equal_share'},
                     {'person_ids': [a, b]}, severity='advisory')

    def duty(workspace, day, person):
        start, end = f'2026-10-{day:02}T08:30:00+03:00', f'2026-10-{day:02}T12:00:00+03:00'
        return pg.shift(workspace, start, end, typ=typ, people=[{
            'person_id': person, 'start': start, 'end': end, 'team_lead': True}])['workspace']

    history = pg.publish(duty(pg.workspace('Published fairness history'), 4, a))
    # Older stored catalogue envelopes (and fact_policy) omit the parent scope; their lines still own Who/Where.
    with transaction(pg.app.state.engine, write=True) as db:
        policy = db.get(m.Policy, leadership['id'])
        policy.definition = {key: value for key, value in policy.definition.items() if key != 'scope'}
        frozen_definition = deepcopy(policy.definition)
    workspace = pg.workspace('Two approved consultant-led duties', '2026-10-11', '2026-10-18')
    workspace = duty(duty(workspace, 11, a), 12, b)
    workspace = pg.lifecycle(workspace, 'submit')['workspace']
    workspace = pg.lifecycle(workspace, 'approve', 'approver')['workspace']
    assert workspace['status'] == 'approved' and workspace['validation']['error_count'] == 0
    reply = pg.lifecycle(workspace, 'publish')
    assert reply['workspace']['status'] == 'published'
    assert pg.detail(workspace)['workspace']['publication_id'] == reply['publication_id']
    with transaction(pg.app.state.engine) as db:
        facts = list(db.execute(select(m.FairnessLedger.person_id, m.FairnessLedger.value).where(
            m.FairnessLedger.publication_id == reply['publication_id'], m.FairnessLedger.metric_key == 'lead_turns')))
        assert dict(facts) == {a: 1, b: 1}
        assert db.get(m.Policy, leadership['id']).definition == frozen_definition
    coverage = pg.get('coordinator', f'/api/publications/{reply["publication_id"]}/coverage')['evaluation']
    assert coverage['error_count'] == 0 and len(coverage['coverage']) == 2
    goals = pg.get('coordinator', '/api/fairness/goals?workspace_id=' + workspace['id'])['goals']
    report, = [row for row in goals if row['policy_id'] == goal['id']]
    assert {row['person_id']: (row['prior'], row['planned']) for row in report['people']} == {a: (1, 1), b: (0, 1)}
    assert {row['person_id']: row['value'] for row in pg.get('coordinator',
        '/api/fairness/distribution?metric=lead_turns&starts_on=2026-10-01&ends_on=2026-11-01')['people']}[a] == 2
    assert pg.get('coordinator', f'/api/publications/{history["publication_id"]}/coverage')['evaluation']['error_count'] == 0
