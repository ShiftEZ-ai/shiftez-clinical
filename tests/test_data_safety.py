"""Production data safety across the release pipeline (docs/release/data-safety.md)."""
from tests.legacy_lead import historical_type

from .clock_support import freeze_clock
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import uuid
from alembic import command
import pytest
from sqlalchemy import MetaData, Table, create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from apps.api import models as m
from apps.api.db import make_engine, transaction
from apps.api.schema import supported_revision
from migrations import safety
from packages.contracts.schemas import ROLE_NAMES
from scripts import backup_job, manage
from apps.api.settings import Settings
from .migration_support import current_seed_columns

ROOT = Path(__file__).resolve().parents[1]
HEAD = '0043_announcements_workers'  # last revision the grandfather inventory was taken at


def test_oidc_department_choice_migration_preserves_existing_flows_and_all_data(session_limits_database):
    from .migration_support import historical_seed
    url = session_limits_database
    cfg = manage.alembic_config(url)
    command.upgrade(cfg, '0122_score_check_gate')
    engine = make_engine(url)
    try:
        with transaction(engine, write=True) as db:
            historical_seed(db)
            old_flow = Table('oidc_flows', MetaData(), autoload_with=db.connection())
            flow_id = str(uuid.uuid4())
            db.execute(old_flow.insert().values(id=flow_id, revision=1, created_at=m.now(),
                state_hash='a' * 64, browser_hash='b' * 64, nonce='retained-nonce', verifier='retained-verifier',
                used=True, force_login=True, remember_me=True, config_revision=7,
                expires_at=m.now() + timedelta(minutes=5)))
        old = MetaData()
        old.reflect(bind=engine)
        def snapshot():
            with engine.connect() as db:
                return {name: list(db.execute(table.select().order_by(*table.primary_key.columns)))
                        for name, table in old.tables.items() if name != 'alembic_version'}
        before = snapshot()
        command.upgrade(cfg, '0123_oidc_department_choice')
        assert snapshot() == before
        with transaction(engine) as db:
            assert db.get(m.OIDCFlow, flow_id).verified_identity_id is None
            assert db.get(m.OIDCFlow, flow_id).used  # old consumed exchanges never become redeemable
        assert next(c for c in inspect(engine).get_columns('oidc_flows') if c['name'] == 'verified_identity_id')['nullable']
    finally:
        engine.dispose()


def test_insertion_order_migration_preserves_rows_guards_and_legacy_ties(session_limits_database):
    """Backfill only new metadata, including with the immutable audit guards enabled."""
    from sqlalchemy import select
    from .migration_support import historical_seed

    url = session_limits_database
    cfg = manage.alembic_config(url)
    command.upgrade(cfg, '0119_backend_hotpath_indexes')
    engine = make_engine(url)
    try:
        with transaction(engine, write=True) as db:
            seed = historical_seed(db)
            ids = [str(uuid.UUID(int=i, version=4)) for i in (900, 100)]
            db.execute(m.AuditEvent.__table__.insert(), [dict(id=ident, department_id=seed['department_id'],
                actor_id=seed['identities']['admin'], action='test.retained', target_id=seed['people'][0],
                created_at=datetime(2026, 10, 3, tzinfo=timezone.utc),
                reason='Preserved evidence', detail={}, correlation_id='historical-tie') for ident in ids])
        old = MetaData()
        old.reflect(bind=engine)

        def snapshot():
            with engine.connect() as db:
                return {name: list(db.execute(table.select().order_by(*table.primary_key.columns)))
                        for name, table in old.tables.items() if name != 'alembic_version'}

        def guards():
            query = ("SELECT tgname, pg_get_triggerdef(oid) FROM pg_trigger WHERE NOT tgisinternal "
                     "AND tgrelid IN (SELECT oid FROM pg_class WHERE relnamespace=current_schema()::regnamespace) ORDER BY tgname"
                     if engine.dialect.name == 'postgresql' else
                     "SELECT name, sql FROM sqlite_master WHERE type='trigger' ORDER BY name")
            with engine.connect() as db:
                return dict(db.execute(text(query)).all())

        before, protected = snapshot(), guards()
        command.upgrade(cfg, 'head')
        command.upgrade(cfg, 'head')
        assert snapshot() == before
        assert all(guards().get(name) == definition for name, definition in protected.items())
        with transaction(engine, write=True) as db:
            rows = list(db.scalars(select(m.AuditEvent).order_by(m.AuditEvent.seq)))
            assert [row.id for row in rows] == sorted(ids)
            last = db.scalar(select(m.RecordInsertion.seq).order_by(m.RecordInsertion.seq.desc()).limit(1))
            fresh = m.AuditEvent(department_id=seed['department_id'], actor_id=seed['identities']['admin'],
                action='test.new', reason='New event after backfill', detail={}, correlation_id='after-migration')
            db.add(fresh)
            db.flush()
            assert fresh.seq > last
        with pytest.raises(DBAPIError, match='immutable evidence'):
            with engine.begin() as db:
                db.execute(text("UPDATE audit_events SET reason='changed'"))
    finally:
        engine.dispose()


def legacy_fair_share(h):
    current = next(p for p in h.get('coordinator', '/api/policies?status=active')['policies']
                   if p['definition']['rule'].get('id') == 'fair_share')
    proposed = h.command('coordinator', 'rule.propose', {'catalogue_id': 'fair_share', 'catalogue_version': 1,
        'expected_policy_id': current['id'], 'expected_revision': current['revision'],
        'lines': current['definition']['rule']['lines'], 'reason': 'Existing catalogue v1 department fixture'})['record']
    h.command('approver', 'policy.activate', {'reason': 'Independent legacy fixture review'}, proposed['id'], proposed['revision'])


def test_fair_share_seed_migration_keeps_definitions_history_and_legacy_lead_results(h):
    """A seeded department keeps the old lead report/counts when built-ins become visible lines."""
    from copy import deepcopy
    import importlib
    from sqlalchemy import select
    from apps.api import models as m
    from apps.api.db import transaction
    from apps.api.serialization import record
    from apps.api.services.scoring_defaults import additions, line_id, seeded_rows
    from packages.domain.conditions import active_rules
    from packages.domain.fairness import goal_report
    from packages.domain.quotas import evaluate_quotas
    from packages.domain.validation import ancestor_ids, exception_for

    legacy_fair_share(h)
    migration = importlib.import_module('migrations.versions.0109_fair_share_dials')
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, h.seed['department_id'])
        department.generation_defaults = {'scoring': {'lead_balance': 'high'}}
        # An existing type is configured before the conversion, preserving its original JSON verbatim.
        typ = db.get(m.AssignmentType, h.seed['type'])
        from packages.contracts.schemas import TypeDefinition
        typ.definition = TypeDefinition.model_validate({**typ.definition,
            'lead': {'minimum': 1, 'maximum': 1, 'most_turns': 1, 'most_turns_per': 'week'}}).model_dump(mode='json')
        original_type = deepcopy(typ.definition)
        policies = [record(p) for p in db.scalars(select(m.Policy).where(m.Policy.department_id == department.id))]
        original_histories = [record(r) for r in db.scalars(select(m.DefinitionHistory))]
        expected_additions = additions(department.id, [record(typ)], policies, 'high', catalogue_version=1)
        assert migration.additions(department.id, [record(typ)], policies, 'high', catalogue_version=1) == expected_additions
    w = h.workspace()
    for day in (4, 5):
        start, end = f'2026-10-{day:02}T08:00:00+03:00', f'2026-10-{day:02}T12:00:00+03:00'
        w = h.shift(w, start=start, end=end,
            people=[{'person_id': h.seed['people'][0], 'start': start, 'end': end, 'team_lead': True}])['workspace']
    before = h.snapshot(w)
    department_id, type_id = h.seed['department_id'], h.seed['type']
    legacy_goal = {'id': 'legacy-lead', 'name': 'Legacy lead balance', 'revision': 1, 'kind': 'balance_goal',
        'department_id': department_id, 'status': 'active', 'active': True, 'starts_on': '2000-01-01', 'ends_on': None,
        'definition': {'severity': 'advisory', 'strength': 'high', 'scope': {}, 'rule': {'kind': 'balance_goal',
            'metric': 'lead_turns', 'aim': 'equal_share', 'share_by': 'available_days', 'period': 'rolling',
            'period_days': 182, 'assignment_type_ids': [type_id]}}}
    legacy_cap = {'id': 'legacy-cap', 'name': 'Legacy lead cap', 'revision': 1, 'kind': 'block_quota',
        'department_id': department_id, 'status': 'active', 'active': True, 'starts_on': '2000-01-01', 'ends_on': None,
        'definition': {'severity': 'advisory', 'strength': 'critical', 'scope': {}, 'rule': {'kind': 'block_quota',
            'metric': 'lead_turns', 'comparison': 'maximum', 'amount': 1, 'assignment_type_ids': [type_id],
            'participation': [], 'overtime': 'include', 'window_days': None, 'window_alignment': 'calendar_week',
            'prorate_department_days': False, 'reference_days': 28, 'rounding': 'nearest',
            'starts_on': (date.fromisoformat(w['starts_on']) - timedelta(days=31)).isoformat(),
            'ends_on': (date.fromisoformat(w['ends_on']) + timedelta(days=31)).isoformat()}}}
    old_goal = goal_report(before, legacy_goal)
    from apps.api.snapshots import domain_snapshot
    with transaction(h.app.state.engine) as db:
        # The old side: the pre-migration rules plus the legacy cap, through the snapshot path so its quota context
        # covers the injected rule.
        old_cap_data = domain_snapshot(db, db.get(m.Workspace, w['id']), policy_overrides=[legacy_cap])
    with h.app.state.engine.begin() as connection:
        migration.backfill(connection)
    # The draft's rule inputs are frozen (425d832da); after the migration a planner refreshes the baseline, the
    # decided path for non-safety rules, so the converted side is read exactly as a draft reads it.
    w = h.refresh_draft_baseline(w)
    with transaction(h.app.state.engine) as db:
        after = domain_snapshot(db, db.get(m.Workspace, w['id']))
    old_quotas = evaluate_quotas(old_cap_data, lambda *args, **kwargs: None, ancestor_ids, exception_for)
    assert old_quotas['complete'] and old_quotas['rows']
    lead = next(p for p in active_rules(after) if p['definition']['rule']['kind'] == 'balance_goal'
                and p['definition']['rule']['metric'] == 'lead_turns')
    new_goal = goal_report(after, lead)
    assert new_goal['people'] == old_goal['people']
    assert new_goal['score'] == old_goal['score']
    new_quotas = evaluate_quotas(after, lambda *args, **kwargs: None, ancestor_ids, exception_for)
    count_rows = lambda report: sorted(tuple(float(r[k]) if k in {'amount', 'actual'} else str(r.get(k)) for k in (
        'person_id', 'starts_on', 'ends_on', 'metric', 'comparison', 'amount', 'actual', 'status'))
        for r in report['rows'])
    assert count_rows(new_quotas) == count_rows(old_quotas)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.AssignmentType, type_id).definition == original_type
        assert db.get(m.Department, department_id).generation_defaults == {'scoring': {'lead_balance': 'high'}}
        for old in policies:
            retained = record(db.get(m.Policy, old['id']))
            assert retained == old
        for old in original_histories:
            assert record(db.get(m.DefinitionHistory, old['id'])) == old
        seeded = list(db.scalars(select(m.Policy).where(m.Policy.department_id == department_id, m.Policy.status == 'active')))
        assert {p.definition['rule']['id'] for p in seeded} == {'fair_share', 'other_counts_per_period'}
        assert all(p.definition['rule']['kind'] == 'catalogue' for p in seeded)
        all_lines = [line for p in seeded_rows(db, [record(p) for p in seeded]) for line in p['definition']['rule']['lines']]
        assert {line_id(department_id, key) for key in ('workload', 'lead:' + type_id, 'lead-cap:' + type_id)} <= {line['id'] for line in all_lines}
        counts = len(list(db.scalars(select(m.Policy)))), len(list(db.scalars(select(m.DefinitionHistory))))
    with h.app.state.engine.begin() as connection:
        migration.backfill(connection)
    with transaction(h.app.state.engine) as db:
        assert counts == (len(list(db.scalars(select(m.Policy)))), len(list(db.scalars(select(m.DefinitionHistory)))))


def test_appended_fair_share_default_can_be_edited_through_normal_approval(h):
    from apps.api.services.scoring_defaults import line_id
    from packages.domain.conditions import active_rules
    from tests.test_rule_catalogue_v2_api import proposed

    conversion = proposed(h)
    h.command('approver', 'rule.catalogue.activate', {'reason': 'Review the default rule representation'},
        conversion['id'], conversion['revision'])

    typ = historical_type(h, {'name': 'New led duty', 'starts_on': '2020-01-01',
        'definition': {'lead': {'minimum': 1, 'maximum': 1}}})
    policies = h.get('coordinator', '/api/policies?status=active')['policies']
    current = next(p for p in policies if p['definition']['rule'].get('id') == 'fair_share')
    rule = current['definition']['rule']
    added_line = next(line for line in rule['lines'] if line['id'] == line_id(h.seed['department_id'], 'lead:' + typ['id']))
    added_line['settings']['balance_goal']['share_adjustment'] = {'mode': 'percent', 'value': 60}
    proposed = h.command('coordinator', 'rule.propose', {'catalogue_id': 'fair_share', 'catalogue_version': 2,
        'expected_policy_id': current['id'], 'expected_revision': current['revision'], 'lines': rule['lines'],
        'reason': 'Review the visible lead-share adjustment'})['record']
    active = h.command('approver', 'policy.activate', {'reason': 'Independent approval of the changed default'},
        proposed['id'], proposed['revision'])['record']
    w = h.workspace()
    lead = next(p for p in active_rules(h.snapshot(w)) if p['id'] == active['id']
        and p['definition']['rule']['metric'] == 'lead_turns')
    assert lead['definition']['rule']['share_adjustment'] == {'mode': 'percent', 'value': 60}
    assert len([line for line in active['definition']['rule']['lines'] if line['id'] == added_line['id']]) == 1


@pytest.mark.parametrize('transition', ['retire', 'conversion'])
def test_seed_lines_keep_restore_history_and_database_immutability(h, transition):
    from sqlalchemy import select, text
    from sqlalchemy.exc import DatabaseError
    from apps.api import models as m
    from apps.api.db import transaction
    from apps.api.serialization import record
    from apps.api.snapshots import scheduling_inputs
    from apps.api.services.scoring_defaults import line_id

    typ = historical_type(h, {'name': 'Historically led duty', 'starts_on': '2020-01-01',
        'definition': {'lead': {'minimum': 1, 'maximum': 1}}})
    with transaction(h.app.state.engine) as db:
        row = db.get(m.PolicySeedLine, line_id(h.seed['department_id'], 'lead:' + typ['id']))
        before = scheduling_inputs(db, [h.seed['department_id']], row.created_at - timedelta(seconds=1))
        current = scheduling_inputs(db, [h.seed['department_id']], row.created_at)
        assert before['policy_seed_lines'] == []
        assert row.id in {r['id'] for r in current['policy_seed_lines']}
        policy_id = row.policy_id
        revision = db.get(m.Policy, policy_id).revision
        original_seed = record(row)
    for action in ('UPDATE policy_seed_lines SET line=line', 'DELETE FROM policy_seed_lines'):
        with pytest.raises(DatabaseError, match='immutable evidence'):
            with h.app.state.engine.begin() as connection:
                connection.execute(text(action))
    if transition == 'retire':
        retired = h.command('approver', 'policy.retire', {'reason': 'Temporarily retire the visible default'},
            policy_id, revision)['record']
        h.command('approver', 'policy.restore', {'reason': 'Restore the unchanged approved lines'},
            policy_id, retired['revision'])
    else:
        from tests.test_rule_catalogue_v2_api import proposed
        conversion = proposed(h)
        activated = h.command('approver', 'rule.catalogue.activate', {'reason': 'Review the seeded rule conversion'},
            conversion['id'], conversion['revision'])['record']
        with transaction(h.app.state.engine) as db:
            retired = record(db.get(m.Policy, policy_id))
        h.command('approver', 'rule.catalogue.undo', {'reason': 'Restore the unchanged seeded predecessor'},
            activated['id'], activated['revision'])
    assert row.id in {line['id'] for line in retired['definition']['rule']['lines']}
    restored = next(p for p in h.get('coordinator', '/api/policies?status=active')['policies'] if p['id'] == policy_id)
    assert line_id(h.seed['department_id'], 'lead:' + typ['id']) in {line['id'] for line in restored['definition']['rule']['lines']}
    with transaction(h.app.state.engine) as db:
        assert record(db.get(m.PolicySeedLine, row.id)) == original_seed
        for state in (retired, restored):
            retained = db.scalar(select(m.DefinitionHistory).where(m.DefinitionHistory.kind == 'policy',
                m.DefinitionHistory.object_id == policy_id, m.DefinitionHistory.object_revision == state['revision']))
            assert retained.snapshot['definition'] == state['definition']


def test_legacy_seed_lines_remain_editable_and_convert_to_workload_limit(h):
    import importlib
    from sqlalchemy import select
    from apps.api import models as m
    from apps.api.db import transaction
    from apps.api.services.scoring_defaults import seeded_rows, line_id
    from apps.api.serialization import record
    from packages.contracts.schemas import TypeDefinition
    from packages.domain.rule_catalogue_migration import plan_v2, after_snapshot
    from packages.domain.conditions import active_rules

    legacy_fair_share(h)
    migration = importlib.import_module('migrations.versions.0109_fair_share_dials')
    h.policy({'kind': 'max_duties', 'maximum': 5, 'window': 'week'})
    with transaction(h.app.state.engine, write=True) as db:
        typ = db.get(m.AssignmentType, h.seed['type'])
        typ.definition = TypeDefinition.model_validate({**typ.definition,
            'lead': {'most_turns': 2, 'most_turns_per': 'month'}}).model_dump(mode='json')
    with h.app.state.engine.begin() as connection:
        migration.backfill(connection)
    policies = h.get('coordinator', '/api/policies?status=active')['policies']
    fair = next(p for p in policies if p['definition']['rule'].get('id') == 'fair_share')
    assert fair['definition']['rule']['catalogue_version'] == 1
    line = next(line for line in fair['definition']['rule']['lines'] if line['id'] == line_id(h.seed['department_id'], 'lead:' + h.seed['type']))
    line['settings']['balance_goal']['share_adjustment'] = {'mode': 'percent', 'value': 75}
    change = h.command('coordinator', 'rule.propose', {'catalogue_id': 'fair_share', 'catalogue_version': 1,
        'expected_policy_id': fair['id'], 'expected_revision': fair['revision'], 'lines': fair['definition']['rule']['lines'],
        'reason': 'Edit the visible legacy default before conversion'})['record']
    h.command('approver', 'policy.activate', {'reason': 'Independent default adjustment review'}, change['id'], change['revision'])
    with transaction(h.app.state.engine) as db:
        rows = seeded_rows(db, [record(p) for p in db.scalars(select(m.Policy).where(m.Policy.department_id == h.seed['department_id']))])
    plan = plan_v2(rows, department_id=h.seed['department_id'])
    assert plan['blocked'] == []
    assert 'workload_limit' in {target['catalogue_id'] for target in plan['targets']}
    converted = active_rules(after_snapshot({'policies': rows}, plan))
    lead = next(p for p in converted if p['definition']['rule']['kind'] == 'balance_goal' and p['definition']['rule']['metric'] == 'lead_turns')
    assert lead['definition']['rule']['share_adjustment'] == {'mode': 'percent', 'value': 75}


@pytest.mark.parametrize('envelope', ['off', 'bounded', 'future', 'rota_only'])
def test_limited_catalogues_preserve_visible_independent_defaults_and_source_rows(h, envelope):
    import importlib
    from copy import deepcopy
    from sqlalchemy import select
    from apps.api import models as m
    from apps.api.db import transaction
    from apps.api.serialization import record
    from packages.contracts.schemas import TypeDefinition, PolicyDefinition
    from packages.domain.conditions import active_rules

    migration = importlib.import_module('migrations.versions.0109_fair_share_dials')
    workspace = h.workspace()
    with transaction(h.app.state.engine, write=True) as db:
        fair = db.scalar(select(m.Policy).where(m.Policy.department_id == h.seed['department_id']))
        fair.definition = {**fair.definition, 'rule': {**fair.definition['rule'], 'on': envelope != 'off', 'lines': []}}
        typ = db.get(m.AssignmentType, h.seed['type'])
        typ.definition = TypeDefinition.model_validate({**typ.definition,
            'lead': {'most_turns': 1, 'most_turns_per': 'week'}}).model_dump(mode='json')
        cap = m.Policy(department_id=h.seed['department_id'], name='Disabled historical lead limit', kind='catalogue',
            definition=PolicyDefinition.model_validate({'rule': {'kind': 'catalogue', 'id': 'other_counts_per_period',
                'catalogue_version': 1, 'on': envelope != 'off', 'lines': []}}).model_dump(mode='json'),
            starts_on=date(2000, 1, 1), active=True, status='active', owner='Department', source='Original configuration',
            created_by=h.seed['identities']['coordinator'], approved_by=h.seed['identities']['approver'])
        db.add(cap)
        db.flush()
        for policy in (fair, cap):
            if envelope == 'bounded':
                policy.ends_on = date(2026, 11, 1)
            elif envelope == 'future':
                policy.starts_on = date(2099, 1, 1)
            elif envelope == 'rota_only':
                policy.definition = {**policy.definition, 'workspace_id': workspace['id']}
        originals = [deepcopy(record(p)) for p in (fair, cap)]
    with h.app.state.engine.begin() as connection:
        migration.backfill(connection)
    with transaction(h.app.state.engine) as db:
        rows = [record(p) for p in db.scalars(select(m.Policy).where(m.Policy.department_id == h.seed['department_id']))]
        for original in originals:
            assert record(db.get(m.Policy, original['id'])) == original
        assert len(rows) == 5  # original off catalogues and three independently editable default rules
    effective = active_rules(rows)
    assert {p['definition']['rule'].get('metric') for p in effective} == {'workload', 'lead_turns'}
    assert any(p['definition']['rule']['kind'] == 'block_quota' for p in effective)
    with h.app.state.engine.begin() as connection:
        migration.backfill(connection)
    with transaction(h.app.state.engine) as db:
        assert len(list(db.scalars(select(m.Policy)))) == 5


def test_initial_seed_template_replaces_visible_default_without_losing_workload(migrated):
    from sqlalchemy import select
    from apps.api import models as m
    from apps.api.db import make_engine, transaction
    from apps.api.services.scoring_defaults import line_id
    from scripts.initial_seed import load

    url, _ = migrated
    engine = make_engine(url)
    try:
        seeded = load(engine, Settings(environment='test', database_url=url), 1,
            'Reviewed initial configuration with visible defaults')
        with transaction(engine) as db:
            fair = [p for p in db.scalars(select(m.Policy)) if p.definition['rule'].get('id') == 'fair_share']
            active = next(p for p in fair if p.status == 'active')
            proposed = next(p for p in fair if p.status == 'proposed')
            assert proposed.definition['merged_ids'] == [active.id]
            assert line_id(seeded['department_id'], 'workload') in {
                line['id'] for line in proposed.definition['rule']['lines']}
            assert active.definition['rule']['catalogue_version'] == proposed.definition['rule']['catalogue_version'] == 1
    finally:
        engine.dispose()


def test_seeded_lead_types_preserve_separate_before_after_fairness_values(h):
    from packages.domain.fairness import comparison_goal, goal_groups, goal_report

    types = [historical_type(h, {'name': name, 'starts_on': '2020-01-01',
        'definition': {'lead': {'minimum': 1, 'maximum': 1}}})['id']
        for name in ('Lead turns at clinic A', 'Lead turns at clinic B')]
    a, b, _ = h.seed['people']
    w = h.workspace()
    for typ, leader, days in ((types[0], a, (4, 5)), (types[1], b, (6, 7, 8))):
        for day in days:
            start, end = f'2026-10-{day:02}T08:00:00+03:00', f'2026-10-{day:02}T12:00:00+03:00'
            w = h.shift(w, start=start, end=end, typ=typ, people=[
                {'person_id': person, 'start': start, 'end': end, 'team_lead': person == leader}
                for person in (a, b)])['workspace']
    data = h.snapshot(w)
    groups = [group for group in goal_groups(data) if group[0]['definition']['rule']['metric'] == 'lead_turns']
    assert len(groups) == 2
    before, after = [], []
    for group in groups:
        goal = comparison_goal(group)
        tid = goal['definition']['rule']['assignment_type_ids'][0]
        reference = {'id': 'legacy-lead:' + tid, 'name': 'Legacy lead balance', 'revision': 1, 'kind': 'balance_goal',
            'department_id': h.seed['department_id'], 'status': 'active', 'active': True,
            'starts_on': '2000-01-01', 'ends_on': None, 'definition': {'severity': 'advisory', 'strength': 'medium',
                'scope': {}, 'rule': {'kind': 'balance_goal', 'metric': 'lead_turns', 'aim': 'equal_share',
                    'share_by': 'available_days', 'period': 'rolling', 'period_days': 182, 'assignment_type_ids': [tid]}}}
        old, new = goal_report(data, reference), goal_report(data, goal)
        assert old['people'] == new['people']
        before.append(old['score'])
        after.append(new['score'])
    assert sorted(before) == sorted(after)
    assert sum(before) == sum(after)


@pytest.mark.parametrize('kind', ['balance_goal', 'block_quota'])
def test_converted_department_can_independently_edit_its_visible_fallback_defaults(h, kind, monkeypatch):
    from copy import deepcopy
    from sqlalchemy import select
    from apps.api import models as m
    from apps.api.db import transaction
    from packages.contracts.schemas import PolicyDefinition
    from packages.domain.conditions import active_rules
    from tests.test_rule_catalogue_v2_api import proposed
    from apps.api.services.scoring_defaults import SOURCE

    conversion = proposed(h)
    h.command('approver', 'rule.catalogue.activate', {'reason': 'Review the department representation'},
        conversion['id'], conversion['revision'])
    with transaction(h.app.state.engine, write=True) as db:
        fair = db.scalar(select(m.Policy).where(m.Policy.status == 'active', m.Policy.kind == 'catalogue'))
        fair.definition = {**fair.definition, 'rule': {**fair.definition['rule'], 'on': False}}
        db.add(m.Policy(department_id=h.seed['department_id'], name='Disabled lead limit entry', kind='catalogue',
            definition=PolicyDefinition.model_validate({'rule': {'kind': 'catalogue', 'id': 'workload_limit',
                'catalogue_version': 2, 'on': False, 'lines': []}}).model_dump(mode='json'),
            starts_on=date(2000, 1, 1), active=True, status='active', owner='Department', source='Original configuration',
            created_by=h.seed['identities']['coordinator'], approved_by=h.seed['identities']['approver']))
    historical_type(h, {'name': 'New independently led duty', 'starts_on': '2020-01-01',
        'definition': {'lead': {'minimum': 1, 'maximum': 1, 'most_turns': 1}}})
    policies = h.get('coordinator', '/api/policies?status=active')['policies']
    original = next(p for p in policies if p['kind'] == kind and p['source'] == SOURCE)
    definition = deepcopy(original['definition'])
    definition['replaces_id'] = original['id']
    if kind == 'balance_goal':
        definition['rule']['share_adjustment'] = {'mode': 'percent', 'value': 60}
    else:
        definition['rule']['amount'] = 3
    payload = {**{key: original[key] for key in ('name', 'owner', 'source', 'starts_on', 'ends_on', 'active')},
        'definition': definition, 'source': 'Owner adjusted this visible default'}
    unrelated = {**payload, 'definition': {**definition, 'replaces_id': None}}
    assert h.command('coordinator', 'policy.create', unrelated, status=409)['error']['code'] == 'USE_CATALOGUE_V2'
    change = h.command('coordinator', 'policy.create', payload)['record']
    h.command('coordinator', 'policy.activate', {'reason': 'The proposer cannot approve this edit'},
        change['id'], change['revision'], status=403)
    active = h.command('approver', 'policy.activate', {'reason': 'Independent fallback default review'},
        change['id'], change['revision'])['record']
    assert h.get('coordinator', '/api/me')['catalogue_version'] == 2
    again = h.command('coordinator', 'policy.create', {**payload,
        'definition': {**definition, 'replaces_id': active['id']}, 'source': 'A second reviewed default adjustment'})['record']
    active = h.command('approver', 'policy.activate', {'reason': 'Independent second default review'},
        again['id'], again['revision'])['record']
    assert h.get('coordinator', '/api/me')['catalogue_version'] == 2
    import importlib
    migration = importlib.import_module('migrations.versions.0109_fair_share_dials')
    versions = []

    def additions(department_id, types, policies, strength, catalogue_version=2):
        versions.append(catalogue_version)
        return {}

    monkeypatch.setattr(migration, 'additions', additions)
    with h.app.state.engine.begin() as connection:
        migration.backfill(connection)
    assert versions == [2]
    later = h.command('coordinator', 'rule.propose', {'catalogue_id': 'people_per_session',
        'catalogue_version': 2, 'lines': [], 'reason': 'V2 authoring after approved fallback default edits'})['record']
    assert later['definition']['rule']['catalogue_version'] == 2
    with transaction(h.app.state.engine) as db:
        retained = db.get(m.Policy, original['id'])
        assert retained.definition == original['definition'] and retained.status == 'retired'
        assert db.scalar(select(m.DefinitionHistory.id).where(m.DefinitionHistory.object_id == original['id'],
            m.DefinitionHistory.object_revision == original['revision'])) is not None
    copies = active_rules(h.snapshot(h.workspace()))
    assert len([p for p in copies if p['id'] == active['id']]) == 1
    assert not any(p['id'] == original['id'] for p in copies)


@pytest.mark.parametrize('invalid', [None, 'missing', 'label', 'unapproved', 'department', 'kind', 'mandatory', 'merged', 'cycle'])
def test_default_successors_require_verified_replacement_lineage(invalid):
    from copy import deepcopy
    from packages.domain.rule_lines import SCORING_DEFAULT_SOURCE, department_catalogue_version

    seed = {'id': 'seed', 'department_id': 'department', 'kind': 'balance_goal', 'status': 'retired',
        'source': SCORING_DEFAULT_SOURCE, 'created_by': 'maintenance', 'approved_by': 'maintenance',
        'definition': {'severity': 'advisory', 'rule': {'kind': 'balance_goal'}}}
    previous = {**deepcopy(seed), 'id': 'previous', 'source': 'Reviewed adjustment',
        'created_by': 'coordinator', 'approved_by': 'approver'}
    previous['definition']['replaces_id'] = seed['id']
    current = {**deepcopy(previous), 'id': 'current', 'status': 'active'}
    current['definition']['replaces_id'] = previous['id']
    lineage = [seed, previous]
    if invalid == 'missing':
        lineage = [previous]
    elif invalid == 'label':
        seed['created_by'] = 'coordinator'
    elif invalid == 'unapproved':
        previous['approved_by'] = None
    elif invalid == 'department':
        seed['department_id'] = 'another department'
    elif invalid == 'kind':
        previous['kind'] = 'block_quota'
    elif invalid == 'mandatory':
        current['definition']['severity'] = 'mandatory'
    elif invalid == 'merged':
        current['definition']['merged_ids'] = [seed['id']]
    elif invalid == 'cycle':
        previous['definition']['replaces_id'] = current['id']
    assert department_catalogue_version([current], default_actor_id='maintenance', lineage=lineage) == (1 if invalid else 2)


# ---- 1. Migrations: destructive operations need an explicit review ---------------------------------------------

def test_score_check_gate_upgrade_retains_existing_settings_and_defaults_off(swap_upgrade_url):
    url = swap_upgrade_url
    config = manage.alembic_config(url)
    command.upgrade(config, '0121_score_check_period')
    engine = make_engine(url)
    try:
        from .migration_support import historical_seed
        with transaction(engine, write=True) as db:
            seed = historical_seed(db)
        with engine.connect() as db:
            departments = Table('departments', MetaData(), autoload_with=db)
            before = dict(db.execute(departments.select().where(departments.c.id == seed['department_id'])).mappings().one())
        command.upgrade(config, '0122_score_check_gate')
        with engine.connect() as db:
            departments = Table('departments', MetaData(), autoload_with=db)
            after = dict(db.execute(departments.select().where(departments.c.id == seed['department_id'])).mappings().one())
            assert after.pop('score_check_enabled') in (False, 0)
            assert after == before
    finally:
        engine.dispose()


def test_no_migration_has_unreviewed_destructive_operations():
    problems = {p.name: safety.unreviewed(p) for p in safety.VERSIONS.glob('*.py') if safety.unreviewed(p)}
    assert not problems, ('Migrations need a verified SAFETY_CLASS declaration and destructive operations need '
                          'DESTRUCTIVE_REVIEWED = "<reason + backup/rollback plan>" (docs/release/data-safety.md): '
                          f'{problems}')


def test_disposable_confirmation_inventory_tracks_future_reviewed_migrations(tmp_path, monkeypatch):
    monkeypatch.setattr(safety, 'VERSIONS', tmp_path)
    monkeypatch.setattr(safety, 'GRANDFATHERED', frozenset({'0001_fixture_core'}))

    def migration(revision, *, reviewed=True, destructive=True):
        source = ('from alembic import op\n' + f'revision = {revision!r}\n' +
                  f'SAFETY_CLASS = {"destructive" if destructive else "additive"!r}\n')
        if reviewed:
            source += 'DESTRUCTIVE_REVIEWED = "Synthetic reviewed change; restore from the verified backup if needed."\n'
        if destructive:
            source += 'def upgrade():\n    op.execute("DELETE FROM synthetic_rows")\n'
        (tmp_path / (revision + '.py')).write_text(source, encoding='utf-8')

    migration('9000_reviewed')
    migration('9001_unreviewed', reviewed=False)
    migration('9002_additive', destructive=False)
    migration('0001_fixture_core')  # Avoid impersonating a source-bound delivered revision.
    assert safety.reviewed_confirmations() == ('0001_fixture_core', '9000_reviewed')
    migration('9003_future')
    assert safety.reviewed_confirmations() == ('0001_fixture_core', '9000_reviewed', '9003_future')


@pytest.fixture(params=['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)])
def swap_upgrade_url(request, tmp_path):
    if request.param == 'sqlite':
        yield 'sqlite:///' + str(tmp_path / 'swap-settings.db')
        return
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    url = request.getfixturevalue('postgres_worker_url')
    schema = 'swap_upgrade_' + uuid.uuid4().hex
    admin = create_engine(url, isolation_level='AUTOCOMMIT')
    try:
        with admin.connect() as db:
            db.execute(text('CREATE SCHEMA ' + schema))
        yield make_url(url).update_query_dict({'options': '-csearch_path=' + schema}).render_as_string(hide_password=False)
    finally:
        with admin.connect() as db:
            db.execute(text('DROP SCHEMA ' + schema + ' CASCADE'))
        admin.dispose()


def test_swap_settings_migration_preserves_existing_department_and_guards(swap_upgrade_url):
    from alembic import command
    from sqlalchemy import create_engine, inspect, MetaData, Table, text
    from sqlalchemy.exc import DBAPIError
    from alembic.script import ScriptDirectory
    url = swap_upgrade_url
    config = manage.alembic_config(url)
    graph = ScriptDirectory.from_config(config)
    head, = graph.get_heads()
    assert head == supported_revision()
    assert '0145_draft_config' in {r.revision for r in graph.iterate_revisions(head, 'base')}
    assert graph.get_revision('0144_tag_member_dates').down_revision == '0143_merge_heads'
    assert graph.get_revision('0143_merge_heads').down_revision == ('0142_workspace_baselines', '0142_person_window_indexes')
    assert graph.get_revision('0139_duty_short_code').down_revision == '0138_contact_action_off'
    assert graph.get_revision('0138_contact_action_off').down_revision == '0137_display_settings'
    assert graph.get_revision('0137_display_settings').down_revision == '0136_person_contacts'
    assert graph.get_revision('0135_applied_population_index').down_revision == '0134_wie_labels'
    assert graph.get_revision('0134_wie_labels').down_revision == '0133_workspace_merge'
    assert graph.get_revision('0133_workspace_merge').down_revision == '0132_pwa_push'
    assert graph.get_revision('0132_pwa_push').down_revision == '0131_read_projection_cursor'
    assert graph.get_revision('0131_read_projection_cursor').down_revision == '0130_session_lineage'
    assert graph.get_revision('0130_session_lineage').down_revision == '0129_job_worker_owner'
    assert graph.get_revision('0129_job_worker_owner').down_revision == '0128_workspace_read_projections'
    assert '0124_type_lead_ownership' in {r.revision for r in graph.iterate_revisions(head, 'base')}
    assert graph.get_revision('0124_type_lead_ownership').down_revision == '0123_oidc_department_choice'
    assert graph.get_revision('0123_oidc_department_choice').down_revision == '0122_score_check_gate'
    assert graph.get_revision('0122_score_check_gate').down_revision == '0121_score_check_period'
    assert graph.get_revision('0121_score_check_period').down_revision == '0120_score_check'
    assert graph.get_revision('0120_score_check').down_revision == '0120_table_view_defaults'
    assert graph.get_revision('0120_table_view_defaults').down_revision == '0120_record_insertion_order'
    assert graph.get_revision('0120_record_insertion_order').down_revision == '0122_policy_transition_approvals'
    assert graph.get_revision('0122_policy_transition_approvals').down_revision == '0121_identifier_lookup'
    assert graph.get_revision('0121_identifier_lookup').down_revision == '0120_auth_configuration'
    assert graph.get_revision('0120_auth_configuration').down_revision == '0119_backend_hotpath_indexes'
    assert graph.get_revision('0119_backend_hotpath_indexes').down_revision == '0118_shift_occurrence_index'
    assert '0116_password_login_hardening' in {r.revision for r in graph.iterate_revisions(head, 'base')}
    assert graph.get_revision('0118_shift_occurrence_index').down_revision == '0117_backend_read_indexes'
    assert graph.get_revision('0117_backend_read_indexes').down_revision == '0116_password_login_hardening'
    assert graph.get_revision('0116_password_login_hardening').down_revision == '0115_worker_poll_indexes'
    assert graph.get_revision('0115_worker_poll_indexes').down_revision == '0114_request_decision_times'
    assert graph.get_revision('0114_request_decision_times').down_revision == '0113_department_quota_calendar'
    assert graph.get_revision('0113_department_quota_calendar').down_revision == '0112_timeline_leave_policy'
    assert graph.get_revision('0112_timeline_leave_policy').down_revision == '0111_autoschedule_resume'
    assert graph.get_revision('0111_autoschedule_resume').down_revision == '0110_feedback_github_reservation'
    assert graph.get_revision('0110_feedback_github_reservation').down_revision == '0098_bulk_privacy'
    assert graph.get_revision('0098_bulk_privacy').down_revision == '0093_bulk_changes'
    assert graph.get_revision('0093_bulk_changes').down_revision == '0109_fair_share_dials'
    assert head == supported_revision()
    assert '0099_swap_settings' in {r.revision for r in graph.iterate_revisions(head, 'base')}
    assert graph.get_revision('0109_fair_share_dials').down_revision == '0108_password_login'
    assert graph.get_revision('0100_rotation_attachments').down_revision == '0106_feedback_recent_routes'
    assert graph.get_revision('0106_feedback_recent_routes').down_revision == '0099_swap_settings'
    assert graph.get_revision('0107_standing_wishes').down_revision == '0100_rotation_attachments'
    assert graph.get_revision('0099_swap_settings').down_revision == '0097_wish_type_settings'
    command.upgrade(config, '0097_wish_type_settings')
    engine = create_engine(url)
    try:
        metadata = MetaData()
        organisation = Table('organisations', metadata, autoload_with=engine)
        department = Table('departments', metadata, autoload_with=engine)
        identity = Table('identities', metadata, autoload_with=engine)
        audit = Table('audit_events', metadata, autoload_with=engine)
        incident = Table('monitor_incidents', metadata, autoload_with=engine)
        guard_sql = ("SELECT name, sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
                     if engine.dialect.name == 'sqlite' else
                     "SELECT tgname, pg_get_triggerdef(oid) FROM pg_trigger "
                     "WHERE tgrelid IN (SELECT oid FROM pg_class WHERE relnamespace = current_schema()::regnamespace) "
                     "AND NOT tgisinternal ORDER BY tgname")
        with engine.begin() as db:
            db.execute(organisation.insert().values(id='org', revision=1, created_at=m.now(),
                name='Synthetic hospital', timezone='UTC', week_start=0, input_revision=1, availability_revision=1))
            db.execute(department.insert().values(id='dept', revision=1, created_at=m.now(),
                organisation_id='org', name='Synthetic department', generation_defaults={}))
            db.execute(identity.insert().values(id='actor', revision=1, created_at=m.now(),
                issuer='synthetic', subject='migration', display_name='Migration fixture', disabled=True))
            db.execute(audit.insert().values(id='audit', revision=1, created_at=m.now(),
                department_id='dept', actor_id='actor', action='synthetic.evidence', reason='Preserve history',
                detail={'retained': True}, correlation_id='swap-upgrade'))
            db.execute(incident.insert().values(id='incident', department_id='dept', source='synthetic', state='down',
                received_at=m.now(), raw_payload={'retained': True}, event_key='swap-upgrade'))
            before = db.exec_driver_sql('SELECT * FROM departments').mappings().one()
            evidence = {table: db.exec_driver_sql('SELECT * FROM ' + table).all()
                        for table in ('audit_events', 'monitor_incidents')}
            guards = db.exec_driver_sql(guard_sql).all()
        command.upgrade(config, '0099_swap_settings')
        with engine.connect() as db:
            after = db.exec_driver_sql('SELECT * FROM departments').mappings().one()
            assert all(after[key] == value for key, value in before.items())
            assert (after['swap_offer_window_days'], after['swap_open_offer_cap']) == (7, 25)
            assert db.exec_driver_sql(guard_sql).all() == guards
            for table, rows in evidence.items():
                assert db.exec_driver_sql('SELECT * FROM ' + table).all() == rows
            assert db.scalar(text('SELECT version_num FROM alembic_version')) == '0099_swap_settings'
        checks = {row['name'] for row in inspect(engine).get_check_constraints('departments')}
        assert {'ck_departments_swap_offer_window_days', 'ck_departments_swap_open_offer_cap'} <= checks
        for sql in ("UPDATE audit_events SET reason='rewritten' WHERE id='audit'",
                    "DELETE FROM audit_events WHERE id='audit'"):
            with pytest.raises(DBAPIError, match='immutable evidence'), engine.begin() as db:
                db.exec_driver_sql(sql)
        for name in ('swap_offer_window_days', 'swap_open_offer_cap'):
            for value in (0, 2147483648):
                with pytest.raises(DBAPIError), engine.begin() as db:
                    db.exec_driver_sql(f'UPDATE departments SET {name}={value}')
    finally:
        engine.dispose()


def test_grandfather_list_is_closed_and_honest():
    revisions = {safety.revision(p): p for p in safety.VERSIONS.glob('*.py')}
    for rev in safety.GRANDFATHERED:
        assert rev in revisions and rev <= HEAD, f'{rev} is not an existing pre-guard revision; use DESTRUCTIVE_REVIEWED'
        assert safety.findings(revisions[rev]), f'{rev} is no longer destructive; drop it from GRANDFATHERED'
        assert not safety.declaration_errors(revisions[rev]) and safety.needs_confirmation(revisions[rev])


@pytest.mark.parametrize('foreign_keys', [0, 1], ids=['fk-off', 'fk-on'])
@pytest.mark.parametrize('fail_restore', [False, True], ids=['upgrade', 'rollback'])
def test_pattern_wish_expansion_preserves_legacy_rows_constraints_indexes_and_guards(tmp_path, monkeypatch,
                                                                                  foreign_keys, fail_restore):
    from alembic import command
    from alembic import op
    import sqlalchemy as sa
    from apps.api.db import make_engine, transaction

    url = 'sqlite:///' + str(tmp_path / 'pattern-wish-upgrade.db')
    command.upgrade(manage.alembic_config(url), '0107_standing_wishes')
    engine = make_engine(url)
    with transaction(engine, write=True) as db:
        from .migration_support import historical_seed
        seed = historical_seed(db)
    with engine.begin() as db:
        wishes = sa.Table('staff_wishes', sa.MetaData(), autoload_with=db)
        db.execute(wishes.insert(), [
            {'id': 'legacy-' + kind, 'revision': n, 'created_at': datetime(2026, 9, 1, tzinfo=timezone.utc),
             'department_id': seed['department_id'], 'person_id': seed['people'][0],
             'first_day': datetime(2026, 10, 1).date(), 'last_day': datetime(2026, 10, 3).date(),
             'kind': kind, 'points': n, 'note': 'Preserve historical wish text', 'status': status,
             'outcome': outcome, 'created_by': seed['identities']['staff']}
            for n, (kind, status, outcome) in enumerate([
                ('prefer_off', 'open', None), ('prefer_on', 'withdrawn', 'declined')], 1)])
        db.execute(sa.text("CREATE TRIGGER staff_wishes_fixture_guard BEFORE DELETE ON staff_wishes "
                           "BEGIN SELECT RAISE(ABORT, 'wish evidence guard'); END"))
        overrides = sa.Table('wish_overrides', sa.MetaData(), autoload_with=db)
        db.execute(overrides.insert(), {'id': 'legacy-override', 'revision': 1,
            'created_at': datetime(2026, 9, 1, tzinfo=timezone.utc), 'department_id': seed['department_id'],
            'wish_id': 'legacy-prefer_off', 'period_start': datetime(2026, 10, 1).date(), 'skip': False, 'points': 2})
        overrides_before = db.execute(sa.select(overrides)).mappings().all()
        before = db.execute(sa.select(wishes).order_by(wishes.c.id)).mappings().all()
        indexes = sa.inspect(db).get_indexes('staff_wishes')
        checks = [c['sqltext'] for c in sa.inspect(db).get_check_constraints('staff_wishes') if 'kind' not in c['sqltext']]
        keys = {name: sorted(sa.inspect(db).get_foreign_keys(name), key=lambda key: key['constrained_columns'])
                for name in ('staff_wishes', 'wish_overrides')}
        schema = db.exec_driver_sql('SELECT type,name,tbl_name,rootpage,sql FROM sqlite_master ORDER BY type,name').all()
    engine.dispose()
    states = []

    @sa.event.listens_for(engine, 'connect')
    def original_foreign_keys(connection, _record):
        connection.execute(f'PRAGMA foreign_keys={foreign_keys}')

    @sa.event.listens_for(engine, 'after_cursor_execute')
    def foreign_key_state(connection, cursor, statement, *_args):
        if statement.startswith('PRAGMA foreign_keys='):
            with closing(cursor.connection.cursor()) as probe:
                states.append(probe.execute('PRAGMA foreign_keys').fetchone()[0])

    execute = op.execute

    def restore_guard(statement, *args, **kwargs):
        if fail_restore and str(statement).startswith('CREATE TRIGGER staff_wishes_fixture_guard'):
            raise RuntimeError('Synthetic pattern guard restore failure')
        return execute(statement, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr('apps.api.db.make_engine', lambda *_args, **_kwargs: engine)
        patch.setattr(op, 'execute', restore_guard)
        if fail_restore:
            with pytest.raises(RuntimeError, match='Synthetic pattern guard restore failure'):
                command.upgrade(manage.alembic_config(url), '0101_pattern_wishes')
        else:
            command.upgrade(manage.alembic_config(url), '0101_pattern_wishes')
    assert states == [0, foreign_keys]
    if fail_restore:
        with sqlite3.connect(tmp_path / 'pattern-wish-upgrade.db') as db:
            assert db.execute('SELECT type,name,tbl_name,rootpage,sql FROM sqlite_master ORDER BY type,name').fetchall() == [tuple(row) for row in schema]
            assert db.execute('SELECT version_num FROM alembic_version').fetchone() == ('0107_standing_wishes',)
            assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        command.upgrade(manage.alembic_config(url), '0101_pattern_wishes')
    engine = make_engine(url)
    try:
        with engine.connect() as db:
            wishes = sa.Table('staff_wishes', sa.MetaData(), autoload_with=db)
            assert isinstance(wishes.c.limit.type, sa.Integer)
            overrides = sa.Table('wish_overrides', sa.MetaData(), autoload_with=db)
            assert db.execute(sa.select(overrides)).mappings().all() == overrides_before
            after = db.execute(sa.select(wishes).order_by(wishes.c.id)).mappings().all()
            assert [{key: row[key] for key in before[0]} for row in after] == before
            assert all(row['limit'] is row['weekend_start'] is row['weekend_days'] is None for row in after)
            assert sa.inspect(db).get_indexes('staff_wishes') == indexes
            assert {name: sorted(sa.inspect(db).get_foreign_keys(name), key=lambda key: key['constrained_columns'])
                    for name in keys} == keys
            assert db.exec_driver_sql('PRAGMA foreign_keys').scalar_one() == 1
            assert db.exec_driver_sql('PRAGMA foreign_key_check').all() == []
            assert db.exec_driver_sql('PRAGMA integrity_check').all() == [('ok',)]
            assert all(c in [item['sqltext'] for item in sa.inspect(db).get_check_constraints('staff_wishes')] for c in checks)
            assert db.scalar(sa.text("SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name='staff_wishes_fixture_guard'")) == 1
            kind_check = next(c['sqltext'] for c in sa.inspect(db).get_check_constraints('staff_wishes') if 'kind' in c['sqltext'])
            assert all(kind in kind_check for kind in ('max_days_in_row', 'min_days_off_together', 'max_weekends_in_row', 'whole_weekends'))
        with pytest.raises(IntegrityError, match='FOREIGN KEY constraint failed'), engine.begin() as db:
            db.execute(overrides.update().values(wish_id='missing-wish'))
        with pytest.raises(IntegrityError, match='wish evidence guard'), engine.begin() as db:
            db.execute(wishes.delete())
    finally:
        engine.dispose()


@pytest.mark.parametrize('body,destructive', [
    ("op.add_column('people', sa.Column('x', sa.Text()))", False),
    ("op.create_index('ix', 'people', ['x'])", False),
    ("op.drop_constraint('ck_departments_session_idle', 'departments', type_='check')\n"
     "    op.create_check_constraint('ck_departments_session_idle', 'departments', "
     "'session_idle_minutes IS NULL OR session_idle_minutes >= 5')", True),
    ("'''Docstrings may say DROP TABLE people.'''", False),
    ("op.drop_column('people', 'x')", True),
    ("op.drop_table('people')", True),
    ("op.rename_table('people', 'persons')", True),
    ("op.alter_column('people', 'x', type_=sa.Integer())", True),
    ("op.alter_column('people', 'x', new_column_name='y')", True),
    ("with op.batch_alter_table('people') as b:\n        b.drop_column('x')", True),
    ("op.execute('DELETE FROM people')", True),
    ("op.execute('TRUNCATE audit_events')", True),
    ("op.get_bind().execute(sa.text(\"UPDATE people SET colour = ''\"))", True),
    ("op.execute(f'DROP TRIGGER {name}_guard ON people')", True),
    ("op.execute('ALTER TABLE people ALTER COLUMN x TYPE integer')", True),
])
def test_detector(tmp_path, body, destructive):
    path = tmp_path / '9999_x.py'
    path.write_text("from alembic import op\nimport sqlalchemy as sa\n" +
                    f"revision = '9999_x'\ndown_revision = '{HEAD}'\n"
                    f"SAFETY_CLASS = {'destructive' if destructive else 'additive'!r}\n\ndef upgrade():\n    {body}\n\n"
                    "def downgrade():\n    raise RuntimeError('Forward-only')\n")
    assert not safety.declaration_errors(path)
    assert bool(safety.findings(path)) is destructive
    assert bool(safety.unreviewed(path)) is destructive


def test_marker_must_be_a_real_review(tmp_path):
    path = tmp_path / '9999_x.py'
    code = ("from alembic import op\nrevision = '9999_x'\nSAFETY_CLASS = 'destructive'\n{}\n"
            "def upgrade():\n    op.drop_column('people', 'legacy')\n")
    path.write_text(code.format("DESTRUCTIVE_REVIEWED = 'ok'"))
    assert safety.unreviewed(path)
    path.write_text(code.format("DESTRUCTIVE_REVIEWED = 'people.legacy unused since 0.8 (contract step); backup restores it'"))
    assert not safety.unreviewed(path) and safety.needs_confirmation(path)


def test_downgrade_that_drops_data_is_destructive(tmp_path):
    path = tmp_path / '9999_x.py'
    path.write_text("from alembic import op\nimport sqlalchemy as sa\nrevision = '9999_x'\n"
                    "SAFETY_CLASS = 'destructive'\ndef upgrade():\n    op.add_column('people', sa.Column('x', sa.Text()))\n"
                    "def downgrade():\n    op.drop_column('people', 'x')\n")
    assert not safety.declaration_errors(path) and safety.unreviewed(path)


@pytest.fixture
def swap_clock(monkeypatch):
    from apps.api import auth, models as m
    now = datetime(2026, 9, 18, 10, tzinfo=timezone.utc)
    freeze_clock(monkeypatch, lambda: now)
    monkeypatch.setattr(auth, 'now', lambda: now)


@pytest.fixture
def legacy_swap_database(h, tmp_path, swap_clock):
    from alembic import command
    from .test_staff_swaps import prepared
    row, _, _ = prepared(h)
    path = tmp_path / 'legacy-swaps.db'
    url = 'sqlite:///' + str(path)
    command.upgrade(manage.alembic_config(url), '0083_swap_approval_settings')
    # Copy only reflected legacy columns: current ORM defaults must never seed the previous schema.
    with sqlite3.connect(path) as db:
        db.execute('ATTACH DATABASE ? AS fixture', (h.settings.database_url.removeprefix('sqlite:///'),))
        before = {}
        # Insert published workspace owners last, retaining all legacy publication guards while seeding.
        for table, in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='alembic_version' AND name NOT LIKE 'sqlite_%' ORDER BY name='workspaces'").fetchall():
            columns = ','.join('"' + c[1] + '"' for c in db.execute(f'PRAGMA table_info("{table}")'))
            db.execute(f'INSERT INTO "{table}" ({columns}) SELECT {columns} FROM fixture."{table}"')
            before[table] = (columns, db.execute(f'SELECT {columns} FROM "{table}" ORDER BY rowid').fetchall())
        schema = db.execute('SELECT type,name,tbl_name,rootpage,sql FROM sqlite_master ORDER BY type,name').fetchall()
    return url, path, before, schema, row


def test_give_swap_upgrade_preserves_every_legacy_value_and_defaults_exchange(legacy_swap_database):
    url, path, before, schema, row = legacy_swap_database
    # Qualify swap storage pages before later additive migrations rebuild unrelated tables.
    command.upgrade(manage.alembic_config(url), '0084_swap_give')
    with sqlite3.connect(path) as db:
        for table, (columns, rows) in before.items():
            assert db.execute(f'SELECT {columns} FROM "{table}" ORDER BY rowid').fetchall() == rows, table
        assert db.execute('SELECT kind FROM swap_requests WHERE id=?', (row['id'],)).fetchone() == ('exchange',)
        assert all(json.loads(kinds) == ['exchange', 'give'] for kinds, in db.execute('SELECT swap_kinds_allowed FROM departments'))
        # No table rebuild, and the separate retained-evidence delete guard is unchanged.
        after = {name: (rootpage, sql) for kind, name, _, rootpage, sql in
                 db.execute('SELECT type,name,tbl_name,rootpage,sql FROM sqlite_master') if kind == 'table' or name == 'swap_requests_delete_guard'}
        for kind, name, _, rootpage, sql in schema:
            if kind == 'table':
                assert after[name][0] == rootpage, name
            elif name == 'swap_requests_delete_guard':
                assert after[name][1] == sql
    manage.migrate(url)
    with sqlite3.connect(path) as db:
        for table, (columns, rows) in before.items():
            assert db.execute(f'SELECT {columns} FROM "{table}" ORDER BY rowid').fetchall() == rows, table
        assert db.execute('SELECT version_num FROM alembic_version').fetchone() == (supported_revision(),)


def test_failed_give_guard_replacement_rolls_back_schema_and_evidence(legacy_swap_database, monkeypatch):
    from alembic import op
    url, path, before, schema, _ = legacy_swap_database
    execute = op.execute

    def fail_replacement(statement, *args, **kwargs):
        if str(statement).startswith('CREATE TRIGGER swap_requests_material_guard'):
            raise RuntimeError('Synthetic guard creation failure')
        return execute(statement, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(op, 'execute', fail_replacement)
        with pytest.raises(RuntimeError, match='Synthetic guard creation failure'):
            manage.migrate(url)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT type,name,tbl_name,rootpage,sql FROM sqlite_master ORDER BY type,name').fetchall() == schema
        assert db.execute('SELECT version_num FROM alembic_version').fetchone() == ('0083_swap_approval_settings',)
        for table, (columns, rows) in before.items():
            assert db.execute(f'SELECT {columns} FROM "{table}" ORDER BY rowid').fetchall() == rows, table
    manage.migrate(url)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT version_num FROM alembic_version').fetchone() == (supported_revision(),)


@pytest.fixture
def legacy_open_swap_database(legacy_swap_database):
    from alembic import command
    url, path, before, _, row = legacy_swap_database
    command.upgrade(manage.alembic_config(url), '0084_swap_give')
    with sqlite3.connect(path) as db:
        for table in before:
            columns = ','.join('"' + c[1] + '"' for c in db.execute(f'PRAGMA table_info("{table}")'))
            before[table] = (columns, db.execute(f'SELECT {columns} FROM "{table}" ORDER BY rowid').fetchall())
        schema = db.execute('SELECT type,name,tbl_name,rootpage,sql FROM sqlite_master ORDER BY type,name').fetchall()
        version = db.execute('PRAGMA schema_version').fetchone()
    return url, path, before, schema, version, row


def test_open_swap_upgrade_preserves_every_value_index_foreign_key_and_other_guard(legacy_open_swap_database):
    url, path, before, schema, _, row = legacy_open_swap_database
    # Check this revision's exact schema before later additive migrations change other tables.
    command.upgrade(manage.alembic_config(url), '0086_swap_open')
    with sqlite3.connect(path) as db:
        for table, (columns, rows) in before.items():
            assert db.execute(f'SELECT {columns} FROM "{table}" ORDER BY rowid').fetchall() == rows, table
        assert db.execute('SELECT kind,open_offer FROM swap_requests WHERE id=?', (row['id'],)).fetchone() == ('exchange', 0)
        assert all(value == 1 for value, in db.execute('SELECT swap_open_offers FROM departments'))
        assert next(c for c in db.execute('PRAGMA table_info(swap_requests)') if c[1] == 'recipient_person_id')[3] == 0
        after = {name: (kind, table, rootpage, sql) for kind, name, table, rootpage, sql in
                 db.execute('SELECT type,name,tbl_name,rootpage,sql FROM sqlite_master')}
        for kind, name, table, rootpage, sql in schema:
            if kind == 'table':
                assert after[name][2] == rootpage, name
            if name not in {'swap_requests', 'departments', 'swap_requests_material_guard'}:
                assert after[name] == (kind, table, rootpage, sql), name
        # Remove the exact new column wherever SQLite inserted it; every original column/check/FK must remain.
        old_swaps = next(sql for _, name, _, _, sql in schema if name == 'swap_requests')
        expected = ' '.join(old_swaps.replace('recipient_person_id VARCHAR(36) NOT NULL',
                                             'recipient_person_id VARCHAR(36)').split())
        new_swaps = ' '.join(after['swap_requests'][3].split())
        added = ("open_offer BOOLEAN NOT NULL DEFAULT false CHECK (recipient_person_id IS NOT NULL OR "
                 "(open_offer AND status IN ('offered','withdrawn','rejected')))")
        preserved, count = re.subn(r',\s*' + re.escape(added), '', new_swaps)
        assert count == 1 and preserved == expected
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        assert db.execute('PRAGMA integrity_check').fetchall() == [('ok',)]


def test_failed_open_guard_replacement_rolls_back_nullability_defaults_and_all_evidence(legacy_open_swap_database, monkeypatch):
    from alembic import op
    url, path, before, schema, version, _ = legacy_open_swap_database
    execute = op.execute

    def fail_replacement(statement, *args, **kwargs):
        if str(statement).startswith('CREATE TRIGGER swap_requests_material_guard'):
            raise RuntimeError('Synthetic open guard creation failure')
        return execute(statement, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(op, 'execute', fail_replacement)
        with pytest.raises(RuntimeError, match='Synthetic open guard creation failure'):
            manage.migrate(url)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT type,name,tbl_name,rootpage,sql FROM sqlite_master ORDER BY type,name').fetchall() == schema
        assert db.execute('PRAGMA schema_version').fetchone() == version
        assert db.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
        assert db.execute('SELECT version_num FROM alembic_version').fetchone() == ('0084_swap_give',)
        for table, (columns, rows) in before.items():
            assert db.execute(f'SELECT {columns} FROM "{table}" ORDER BY rowid').fetchall() == rows, table
    manage.migrate(url)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT version_num FROM alembic_version').fetchone() == (supported_revision(),)


def test_bulk_upgrade_follows_dev_head_and_preserves_assignment_source_and_existing_values(legacy_open_swap_database):
    from alembic.script import ScriptDirectory
    url, path, _, _, _, row = legacy_open_swap_database
    config = manage.alembic_config(url)
    scripts = ScriptDirectory.from_config(config)
    head, = scripts.get_heads()
    assert scripts.get_revision('0118_shift_occurrence_index').down_revision == '0117_backend_read_indexes'
    assert scripts.get_revision('0117_backend_read_indexes').down_revision == '0116_password_login_hardening'
    assert scripts.get_revision('0116_password_login_hardening').down_revision == '0115_worker_poll_indexes'
    assert scripts.get_revision('0115_worker_poll_indexes').down_revision == '0114_request_decision_times'
    assert scripts.get_revision('0114_request_decision_times').down_revision == '0113_department_quota_calendar'
    assert scripts.get_revision('0113_department_quota_calendar').down_revision == '0112_timeline_leave_policy'
    assert scripts.get_revision('0112_timeline_leave_policy').down_revision == '0111_autoschedule_resume'
    assert scripts.get_revision('0111_autoschedule_resume').down_revision == '0110_feedback_github_reservation'
    assert scripts.get_revision('0110_feedback_github_reservation').down_revision == '0098_bulk_privacy'
    assert scripts.get_revision('0098_bulk_privacy').down_revision == '0093_bulk_changes'
    assert scripts.get_revision('0093_bulk_changes').down_revision == '0109_fair_share_dials'
    assert scripts.get_revision('0070_assignment_source').down_revision == '0086_swap_open'
    command.upgrade(config, '0070_assignment_source')
    with closing(sqlite3.connect(path)) as db, db:
        assignment_id, actor_id = db.execute('SELECT a.id,w.created_by FROM assignments a '
            'JOIN shifts s ON s.id=a.shift_id JOIN workspaces w ON w.id=s.workspace_id '
            'WHERE w.id=? AND w.status=\'draft\' LIMIT 1', (row['workspace_id'],)).fetchone()
        db.execute('UPDATE assignments SET source=?,source_actor_id=?,source_at=? WHERE id=?',
                   ('manual', actor_id, '2026-09-18 10:00:00+00:00', assignment_id))
        provenance = db.execute('SELECT source,source_run_id,source_actor_id,source_at FROM assignments WHERE id=?',
                                (assignment_id,)).fetchone()
        tables = [name for name, in db.execute("SELECT name FROM sqlite_master WHERE type='table' "
            "AND name != 'alembic_version' AND name NOT LIKE 'sqlite_%'")]
        columns = {table: ','.join('"' + col[1] + '"' for col in db.execute(f'PRAGMA table_info("{table}")'))
                   for table in tables}
        before = {table: db.execute(f'SELECT {columns[table]} FROM "{table}" ORDER BY rowid').fetchall()
                  for table in tables}
    command.upgrade(config, 'head')
    command.upgrade(config, 'head')
    with closing(sqlite3.connect(path)) as db:
        assert {table: db.execute(f'SELECT {columns[table]} FROM "{table}" ORDER BY rowid').fetchall()
                for table in tables} == before
        assert db.execute('SELECT kind,open_offer FROM swap_requests WHERE id=?', (row['id'],)).fetchone() == ('exchange', 0)
        assert db.execute('SELECT count(*) FROM bulk_staged').fetchone() == (0,)
        assert db.execute('SELECT DISTINCT bulk_settings FROM departments').fetchall() == [('{}',)]
        assert db.execute('SELECT source,source_run_id,source_actor_id,source_at FROM assignments WHERE id=?',
                          (assignment_id,)).fetchone() == provenance
        assert db.execute('SELECT version_num FROM alembic_version').fetchone() == (head,)
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        assert db.execute('PRAGMA integrity_check').fetchall() == [('ok',)]


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)], ids=['sqlite', 'postgresql'])
@pytest.mark.parametrize('kind', ['exchange', 'give'])
def test_open_swap_database_guards_bind_recipient_once_and_retain_consent(request, backend, kind, swap_clock):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    from apps.api import models as m
    from apps.api.db import transaction
    from .test_staff_swaps import source_pair, offered
    h = request.getfixturevalue(backend)
    pid, first, second, _ = source_pair(h)
    named = offered(h, pid, first, kind=kind)
    with transaction(h.app.state.engine) as db:
        original = db.get(m.SwapRequest, named['id'])
        values = {column.name: getattr(original, column.name) for column in m.SwapRequest.__table__.columns}
    values.update(id=m.uid(), open_offer=True, recipient_person_id=None)
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(m.SwapRequest.__table__.insert().values(**values))
    params = {'id': values['id'], 'person': h.seed['people'][1], 'recipient': h.seed['identities']['staff_two'],
              'at': m.now(), 'second': second if kind == 'exchange' else None}
    claim = ('UPDATE swap_requests SET status=\'counteroffered\',recipient_person_id=:person,'
             'recipient_id=:recipient,counteroffered_at=:at,second_assignment_id=:second WHERE id=:id')

    def refused(statement, values=params, message='immutable authority or consent material'):
        with pytest.raises(DBAPIError, match=message), transaction(h.app.state.engine, write=True) as db:
            db.execute(text(statement), values)

    for overrides in ({'open_offer': False}, {'status': 'counteroffered'}):
        with pytest.raises(DBAPIError, match='(?i)check constraint'), transaction(h.app.state.engine, write=True) as db:
            db.execute(m.SwapRequest.__table__.insert().values(**{**values, 'id': m.uid(), **overrides}))
    refused("UPDATE swap_requests SET status='withdrawn',recipient_person_id=NULL WHERE id=:id",
            {**params, 'id': named['id']})
    refused("UPDATE swap_requests SET status='withdrawn',recipient_person_id=:person WHERE id=:id")
    refused("UPDATE swap_requests SET status='withdrawn',open_offer=false WHERE id=:id")
    refused(claim, {**params, 'person': None})
    refused(claim, {**params, 'recipient': None})
    refused(claim, {**params, 'at': None})
    if kind == 'exchange':
        refused(claim, {**params, 'second': None})
    refused(claim, {**params, 'person': 'missing-person'}, '(?i)foreign key constraint')
    refused(claim, {**params, 'person': h.seed['people'][0]}, '(?i)check constraint')
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(text(claim), params)
    refused("UPDATE swap_requests SET status='withdrawn',recipient_person_id=NULL WHERE id=:id")
    refused("UPDATE swap_requests SET status='withdrawn',recipient_person_id=:person WHERE id=:id",
            {**params, 'person': h.seed['people'][2]})
    refused("UPDATE swap_requests SET status='withdrawn',open_offer=false WHERE id=:id")
    refused("UPDATE swap_requests SET status='accepted' WHERE id=:id")
    refused('DELETE FROM swap_requests WHERE id=:id', message='retained authority or consent evidence')
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(text("UPDATE swap_requests SET status='accepted',confirmed_at=:at WHERE id=:id"), params)
    with transaction(h.app.state.engine) as db:
        saved = db.get(m.SwapRequest, values['id'])
        assert saved.open_offer and saved.recipient_person_id == params['person'] and saved.status == 'accepted'


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)], ids=['sqlite', 'postgresql'])
def test_give_swap_database_guards_retain_consent_and_evidence(request, backend, swap_clock):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    from apps.api import models as m
    from apps.api.db import transaction
    from .governance_support import swap_automation, transition
    from .test_staff_swaps import source_pair, offered
    h = request.getfixturevalue(backend)
    swap_automation(h, prepare=False, publish=False)  # the accepted state is checked before any preparation
    pid, first, second, _ = source_pair(h)
    exchange = offered(h, pid, first)
    values = {'id': exchange['id'], 'recipient': h.seed['identities']['staff_two'], 'at': m.now(), 'second': None}
    consent = "UPDATE swap_requests SET status='counteroffered',recipient_id=:recipient,counteroffered_at=:at,second_assignment_id=:second WHERE id=:id"

    def refused(statement, params, message='immutable authority or consent material'):
        with pytest.raises(DBAPIError, match=message), transaction(h.app.state.engine, write=True) as db:
            db.execute(text(statement), params)

    refused(consent, values)  # An exchange still requires the recipient's exact return duty.
    refused("UPDATE swap_requests SET status='withdrawn',kind='give' WHERE id=:id", values)
    transition(h, exchange, 'withdraw')
    give = offered(h, pid, first, kind='give')
    values['id'] = give['id']
    refused(consent, {**values, 'second': second}, '(?i)check constraint')
    refused(consent, {**values, 'recipient': None})
    refused(consent, {**values, 'at': None})
    refused("UPDATE swap_requests SET status='withdrawn',kind='exchange' WHERE id=:id", values)
    refused("UPDATE swap_requests SET status='withdrawn',reason='Changed consent' WHERE id=:id", values)
    give = transition(h, give, 'accept', 'staff_two')['swap']
    refused("UPDATE swap_requests SET status='accepted' WHERE id=:id", values)  # Proposer confirmation is required.
    give = transition(h, give, 'confirm')['swap']
    refused("UPDATE swap_requests SET status='reviewing' WHERE id=:id", values)
    refused('DELETE FROM swap_requests WHERE id=:id', values, 'retained authority or consent evidence')
    for statement in ("UPDATE swap_events SET reason='Changed evidence' WHERE swap_id=:id",
                      'DELETE FROM swap_events WHERE swap_id=:id'):
        refused(statement, values, 'immutable evidence')
    with transaction(h.app.state.engine) as db:
        saved = db.get(m.SwapRequest, give['id'])
        assert saved.kind == 'give' and saved.status == 'accepted' and saved.second_assignment_id is None


def assert_only_scoring_defaults_added(before, after, columns):
    """Retain every original column/row and account explicitly for 0109's three additive row families."""
    from apps.api.services.scoring_defaults import SOURCE, line_id

    assert after.keys() == before.keys()
    additions = {}
    for table, rows in before.items():
        remaining = list(after[table])
        for row in rows:
            assert row in remaining, (table, row)
            remaining.remove(row)
        additions[table] = [dict(zip(columns[table], row)) for row in remaining]
    assert not {table: rows for table, rows in additions.items()
                if rows and table not in {'identities', 'policies', 'definition_history'}}
    departments = {dict(zip(columns['departments'], row))['id'] for row in before['departments']}
    if not departments:
        assert not any(additions.values())
        return
    [actor] = additions['identities']
    assert actor['issuer'] == 'urn:fmuc:maintenance' and actor['subject'] == 'system-maintenance'
    assert actor['disabled'] == True and actor['password_hash'] is None and actor['person_id'] is None
    policies = additions['policies']
    assert len(policies) == len(departments) and {row['department_id'] for row in policies} == departments
    by_id = {row['id']: row for row in policies}
    for policy in policies:
        definition = json.loads(policy['definition']) if isinstance(policy['definition'], str) else policy['definition']
        assert policy['source'] == SOURCE and policy['kind'] == 'catalogue' and policy['status'] == 'active'
        assert policy['created_by'] == policy['approved_by'] == actor['id'] and policy['revision'] == 1
        assert definition['rule']['id'] == 'fair_share' and definition['rule']['catalogue_version'] == 2
        [line] = definition['rule']['lines']
        assert line['id'] == line_id(policy['department_id'], 'workload')
        assert line['settings']['balance_goal']['metric'] == 'workload'
        policy['definition'] = definition
    histories = additions['definition_history']
    assert len(histories) == len(policies) and {row['object_id'] for row in histories} == by_id.keys()
    for row in histories:
        policy = by_id[row['object_id']]
        snapshot = json.loads(row['snapshot']) if isinstance(row['snapshot'], str) else row['snapshot']
        assert row['kind'] == 'policy' and row['object_revision'] == 1 and row['actor_id'] == actor['id']
        assert row['reason'] == SOURCE and row['department_id'] == policy['department_id']
        assert snapshot['id'] == policy['id'] and snapshot['definition'] == policy['definition']
        assert snapshot['source'] == SOURCE and snapshot['created_by'] == snapshot['approved_by'] == actor['id']


@pytest.fixture(params=['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)])
def session_limits_database(request, tmp_path):
    if request.param == 'sqlite':
        yield 'sqlite:///' + str(tmp_path / 'session-limits.db')
        return
    url = os.getenv('TEST_POSTGRES_URL', '')
    if (os.getenv('ROTA_ENV') == 'production' or os.getenv('ROTA_ALLOW_DISPOSABLE_TEST_DB') != 'yes'
            or not url.startswith('postgresql+psycopg://')):
        pytest.fail('Session migration requires an authorised disposable PostgreSQL database')
    schema = 'rota_test_' + uuid.uuid4().hex
    admin = create_engine(url, isolation_level='AUTOCOMMIT', hide_parameters=True)
    try:
        with admin.connect() as db:
            db.execute(text('CREATE SCHEMA ' + schema))
        yield make_url(url).update_query_dict({'options': '-csearch_path=' + schema}).render_as_string(hide_password=False)
    finally:
        with admin.connect() as db:
            db.execute(text('DROP SCHEMA ' + schema + ' CASCADE'))
        admin.dispose()


def test_session_limits_migration_preserves_data_and_other_guards(session_limits_database):
    url = session_limits_database
    command.upgrade(manage.alembic_config(url), '0073_integration_credentials')
    engine = make_engine(url)
    try:
        # Reflect the OLD schema: the current ORM deliberately includes new additive columns.
        old = MetaData()
        old.reflect(bind=engine)
        moment = m.now()
        with engine.begin() as db:
            def add(table, **values):
                common = {'id': str(uuid.uuid4()), 'revision': 1, 'created_at': moment}
                data = {key: value for key, value in common.items() if key in old.tables[table].c}
                data.update(values)
                db.execute(old.tables[table].insert().values(**data))
                return data['id']

            organisation_id = add('organisations', name='Synthetic existing institution', timezone='Asia/Riyadh',
                                  week_start=6, input_revision=1, availability_revision=1)
            departments = []
            for name, idle, hours in (('Configured', 480, 24), ('Inherited', None, None)):
                departments.append(add('departments', organisation_id=organisation_id, name=name,
                                       generation_defaults={}, display_settings={}, weekend_days=[4, 5],
                                       leave_reason_display='type', session_idle_minutes=idle, session_max_hours=hours))
            identity_id = add('identities', issuer='synthetic', subject='existing', display_name='Synthetic existing account',
                              disabled=False)
            add('integration_credentials', department_id=departments[0], kind='github_issues', repo='synthetic/existing',
                token_ciphertext=b'synthetic-existing-ciphertext', token_hint='test', connected_by=identity_id,
                connected_at=moment)
            session_id = add('sessions', identity_id=identity_id, department_id=departments[0], token_hash='s' * 64,
                             csrf_hash='c' * 64, expires_at=moment + timedelta(hours=1), revoked=False,
                             last_active_at=moment)
            add('audit_events', department_id=departments[0], actor_id=identity_id, action='test.session_limits',
                target_id=session_id, reason='Synthetic existing evidence', detail={'before': [480, 24]},
                correlation_id='session-limits-migration')
        columns = {table: [column['name'] for column in inspect(engine).get_columns(table)]
                   for table in inspect(engine).get_table_names() if table != 'alembic_version'}

        def stored_data():
            with engine.connect() as db:
                return {table: sorted(db.execute(text('SELECT ' + ', '.join(f'"{column}"' for column in names)
                                                     + f' FROM "{table}"')).fetchall(), key=repr)
                        for table, names in columns.items()}

        def triggers():
            query = ("SELECT pg_get_triggerdef(oid) FROM pg_trigger WHERE NOT tgisinternal "
                     "AND tgrelid IN (SELECT oid FROM pg_class WHERE relnamespace=current_schema()::regnamespace)"
                     if engine.dialect.name == 'postgresql' else "SELECT sql FROM sqlite_master WHERE type='trigger'")
            with engine.connect() as db:
                return sorted(db.execute(text(query)).scalars())

        before, guards = stored_data(), triggers()
        inspector = inspect(engine)
        indexes = inspector.get_indexes('departments')
        foreign_keys = inspector.get_foreign_keys('departments')
        other_checks = [check for check in inspector.get_check_constraints('departments')
                        if check['name'] not in {'ck_departments_session_idle', 'ck_departments_session_max'}]
        # Keep 0077's exact preservation checks at its boundary; later revisions add their own guards and columns.
        command.upgrade(manage.alembic_config(url), '0077_session_limits')
        assert stored_data() == before and triggers() == guards
        inspector = inspect(engine)
        assert inspector.get_indexes('departments') == indexes
        assert inspector.get_foreign_keys('departments') == foreign_keys
        checks = inspector.get_check_constraints('departments')
        assert [check for check in checks if check['name'] not in {
            'ck_departments_session_idle', 'ck_departments_session_max'}] == other_checks
        # Current ORM reads need the complete additive schema, including the swap settings.
        manage.migrate(url)
        assert_only_scoring_defaults_added(before, stored_data(), columns)
        with engine.connect() as db:
            assert db.scalar(text('SELECT count(*) FROM policy_seed_lines')) == 0
        with transaction(engine) as db:
            for department_id in departments:
                department = db.get(m.Department, department_id)
                assert department.session_idle_unlimited is False and department.session_max_unlimited is False
            assert db.get(m.Department, departments[1]).session_idle_minutes is None
            assert db.get(m.Department, departments[1]).session_max_hours is None
            assert db.get(m.Session, session_id).expiry_unlimited is False
        if engine.dialect.name == 'postgresql':
            assert {check['name'] for check in checks} == {check['name'] for check in other_checks} | {
                'ck_departments_session_idle', 'ck_departments_session_max'}
            for changes in ({'session_idle_minutes': 4}, {'session_max_hours': 0}, {'leave_reason_display': 'invalid'}):
                with pytest.raises(IntegrityError), transaction(engine, write=True) as db:
                    for field, value in changes.items():
                        setattr(db.get(m.Department, departments[0]), field, value)
                    db.flush()
        with transaction(engine, write=True) as db:
            department = db.get(m.Department, departments[0])
            department.session_idle_minutes, department.session_max_hours = 1440, 720
        with transaction(engine) as db:
            department = db.get(m.Department, departments[0])
            assert (department.session_idle_minutes, department.session_max_hours) == (1440, 720)
        migration = safety.VERSIONS / '0077_session_limits.py'
        assert safety.classification(migration) == 'destructive' and not safety.unreviewed(migration)
        assert safety.needs_confirmation(migration) and not safety.declaration_errors(migration)
    finally:
        engine.dispose()


def test_password_login_migration_preserves_sessions_and_evidence(session_limits_database, monkeypatch):
    from alembic.script import ScriptDirectory
    from sqlalchemy.exc import DBAPIError
    from apps.worker import main as worker
    from packages.domain import time as domain_time

    # Retention guards use the database wall clock, outside the pinned rota calendar.
    monkeypatch.setattr(domain_time, '_offset', timedelta(0))

    url = session_limits_database
    cfg = manage.alembic_config(url)
    scripts = ScriptDirectory.from_config(cfg)
    head, = scripts.get_heads()
    migration = scripts.get_revision('0108_password_login')
    assert migration.down_revision == '0102_wish_outcome_reasons'
    assert migration.revision in {r.revision for r in scripts.iterate_revisions(head, 'base')}
    assert not (safety.VERSIONS / '0100_password_login.py').exists()
    assert not (safety.VERSIONS / '0107_password_login.py').exists()
    path = safety.VERSIONS / '0108_password_login.py'
    assert safety.classification(path) == 'destructive' and not safety.declaration_errors(path)
    assert safety.findings(path) and safety.needs_confirmation(path) and not safety.unreviewed(path)
    assert 'authentication-event retention only' in safety.marker(path)
    command.upgrade(cfg, migration.down_revision)
    confirmation = safety.reviewed_confirmations()
    assert migration.revision in confirmation
    with monkeypatch.context() as production:
        production.setenv('ROTA_ENV', 'production')
        pending = [r.revision for r in reversed(list(scripts.iterate_revisions(head, migration.down_revision)))]
        assert pending[0] == migration.revision
        with pytest.raises(SystemExit, match='--confirm-destructive ' + migration.revision + ' '):
            manage.check_migrations(url, tuple(revision for revision in confirmation if revision != migration.revision))
        assert manage.check_migrations(url, confirmation) == pending
    engine = make_engine(url)
    try:
        old = MetaData()
        old.reflect(bind=engine)
        moment = m.now()
        with engine.begin() as db:
            def add(table, **values):
                values = {'id': str(uuid.uuid4()), 'revision': 1, 'created_at': moment, **values}
                db.execute(old.tables[table].insert().values(**values))
                return values['id']

            organisation = add('organisations', name='Synthetic password migration institution', timezone='UTC',
                               week_start=0, input_revision=1, availability_revision=1)
            department = add('departments', organisation_id=organisation, name='Synthetic password migration department',
                             generation_defaults={})
            identity = add('identities', issuer='synthetic', subject='retained-password-migration',
                           display_name='Synthetic retained account', disabled=False)
            session_values = {'identity_id': identity, 'department_id': department, 'csrf_hash': 'c' * 64,
                              'expires_at': moment + timedelta(hours=1), 'revoked': False, 'last_active_at': moment}
            session = add('sessions', token_hash='s' * 64, **session_values)
            add('audit_events', department_id=department, actor_id=identity, action='test.password_migration',
                target_id=session, reason='Synthetic retained evidence', detail={'session': session},
                correlation_id='password-login-migration')
        assert 'auth_method' not in old.tables['sessions'].c

        def snapshot():
            with engine.connect() as db:
                return {table.name: sorted(db.execute(table.select()).fetchall(), key=repr)
                        for table in old.sorted_tables if table.name != 'alembic_version'}

        before = snapshot()
        command.upgrade(cfg, migration.revision)
        assert snapshot() == before
        inspector = inspect(engine)
        columns = {column['name']: column for column in inspector.get_columns('auth_login_events')}
        assert set(columns) == {'id', 'revision', 'created_at', 'account_hash', 'peer_hash', 'outcome',
                                'correlation_id', 'session_id'}
        assert {name for name, column in columns.items() if column['nullable']} == {'session_id'}
        assert {name: columns[name]['type'].length for name in ('id', 'account_hash', 'peer_hash', 'outcome',
                                                               'correlation_id', 'session_id')} == {
            'id': 36, 'account_hash': 64, 'peer_hash': 64, 'outcome': 32, 'correlation_id': 64, 'session_id': 36}
        assert inspector.get_foreign_keys('auth_login_events') == []  # Pruning sessions cannot erase login evidence.
        assert any(index['column_names'] == ['created_at']
                   for index in inspector.get_indexes('auth_login_events'))
        with transaction(engine, write=True) as db:
            event = m.AuthLoginEvent(account_hash='a' * 64, peer_hash='b' * 64, outcome='success',
                                    correlation_id='synthetic-login-migration', session_id=session,
                                    created_at=moment - timedelta(days=8))
            db.add(event)
            db.flush()
            event_id = event.id
            recent = m.AuthLoginEvent(account_hash='a' * 64, peer_hash='b' * 64, outcome='credentials_rejected',
                                      correlation_id='synthetic-recent-login')
            expired = m.AuthLoginEvent(account_hash='a' * 64, peer_hash='b' * 64, outcome='credentials_rejected',
                                       correlation_id='synthetic-expired-login', created_at=moment - timedelta(days=31))
            db.add_all([recent, expired])
            db.flush()
            recent_id, expired_id = recent.id, expired.id
        with transaction(engine) as db:
            event = db.get(m.AuthLoginEvent, event_id)
            assert (event.account_hash, event.peer_hash, event.outcome, event.correlation_id, event.session_id) == (
                'a' * 64, 'b' * 64, 'success', 'synthetic-login-migration', session)
        with engine.begin() as db:
            sessions = MetaData()
            sessions.reflect(bind=db, only=['sessions'])
            table = sessions.tables['sessions']
            assert db.execute(table.select().where(table.c.id == session)).one().auth_method is None
            for method in (None, 'password', 'oidc'):
                key = str(uuid.uuid4())
                db.execute(table.insert().values(id=key, revision=1, created_at=moment,
                           token_hash=uuid.uuid4().hex * 2, auth_method=method, **session_values))
                assert db.execute(table.select().where(table.c.id == key)).one().auth_method == method
        for statement, target in (("UPDATE auth_login_events SET outcome='rewritten' WHERE id=:id", event_id),
                                  ("UPDATE auth_login_events SET outcome='rewritten' WHERE id=:id", expired_id),
                                  ('DELETE FROM auth_login_events WHERE id=:id', recent_id)):
            with pytest.raises(DBAPIError, match='immutable evidence'), transaction(engine, write=True) as db:
                db.execute(text(statement), {'id': target})
        with transaction(engine, write=True) as db:
            db.execute(table.update().where(table.c.id == session).values(expires_at=moment - timedelta(days=8)))
            attempt = m.LoginAttempt(key_hash='d' * 64, succeeded=False, created_at=moment - timedelta(days=8))
            db.add(attempt)
            db.flush()
            attempt_id = attempt.id
        monkeypatch.setattr(worker, '_pruned', {})
        assert worker.prune_security_state(engine) == 3
        with transaction(engine) as db:
            assert db.execute(table.select().where(table.c.id == session)).first() is None
            assert db.get(m.LoginAttempt, attempt_id) is None
            assert db.get(m.AuthLoginEvent, expired_id) is None
            assert db.get(m.AuthLoginEvent, recent_id).outcome == 'credentials_rejected'
            event = db.get(m.AuthLoginEvent, event_id)
            assert event.session_id == session and event.outcome == 'success'
    finally:
        engine.dispose()


# ---- 1b/3. Deploy gate: downgrade protection, destructive refusal and confirmation --------------------------------

@pytest.fixture
def migrated(tmp_path):
    url = 'sqlite:///' + str(tmp_path / 'gate.db')
    manage.migrate(url)
    return url, tmp_path / 'gate.db'


def set_head(path, revision):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('UPDATE alembic_version SET version_num=?', (revision,))


def test_gate_passes_current_and_fresh_databases(tmp_path, migrated, capsys):
    from apps.api.schema import supported_revision
    url, _ = migrated
    assert manage.check_migrations(url) == []
    fresh = manage.check_migrations('sqlite:///' + str(tmp_path / 'fresh.db'))
    assert fresh[0] == '0001_core' and fresh[-1] == supported_revision()
    assert 'pending: 0' in capsys.readouterr().out


@pytest.mark.parametrize('version_table', [False, True])
def test_production_fresh_database_needs_no_destructive_confirmations(tmp_path, monkeypatch, capsys, version_table):
    path = tmp_path / 'fresh.db'
    with closing(sqlite3.connect(path)) as db, db:
        if version_table:
            db.execute('CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)')
    monkeypatch.setenv('ROTA_ENV', 'production')
    pending = manage.check_migrations('sqlite:///' + str(path))
    assert pending[0] == '0001_core' and pending[-1] == supported_revision()
    assert 'fresh database: destructive confirmations not required' in capsys.readouterr().out
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == (
            [('alembic_version',)] if version_table else [])


@pytest.mark.parametrize('version_table', [False, True])
def test_production_unversioned_tables_still_need_confirmation(tmp_path, monkeypatch, capsys, version_table):
    path = tmp_path / 'partial.db'
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('CREATE TABLE retained_evidence (id TEXT)')
        if version_table:
            db.execute('CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)')
    monkeypatch.setenv('ROTA_ENV', 'production')
    with pytest.raises(SystemExit, match='--confirm-destructive '):
        manage.check_migrations('sqlite:///' + str(path))
    assert 'fresh database' not in capsys.readouterr().out


def test_fresh_database_inspection_error_blocks_migrations(tmp_path, monkeypatch, capsys):
    def fail_inspection(self, *args, **kwargs):
        raise RuntimeError('schema inspection failed')

    monkeypatch.setattr('sqlalchemy.engine.reflection.Inspector.get_table_names', fail_inspection)
    monkeypatch.setenv('ROTA_ENV', 'production')
    with pytest.raises(RuntimeError, match='schema inspection failed'):
        manage.check_migrations('sqlite:///' + str(tmp_path / 'unknown.db'))
    assert 'fresh database' not in capsys.readouterr().out


def test_gate_refuses_a_database_newer_than_the_image(migrated):
    url, path = migrated
    set_head(path, '9999_from_a_newer_release')
    with pytest.raises(SystemExit, match='downgrade'):
        manage.check_migrations(url)


def fake_destructive(monkeypatch, reviewed):
    monkeypatch.setattr(manage.safety, 'findings', lambda p: ['line 1: drop_column'] if HEAD in str(p) else [])
    monkeypatch.setattr(manage.safety, 'declaration_errors', lambda p: [])
    monkeypatch.setattr(manage.safety, 'classification', lambda p: 'destructive' if HEAD in str(p) else 'additive')
    monkeypatch.setattr(manage.safety, 'unreviewed', lambda p: safety.findings(p) if not reviewed else [])
    monkeypatch.setattr(manage.safety, 'marker', lambda p: 'x' * 40 if reviewed else None)


def test_gate_refuses_unreviewed_destructive_pending_revision(migrated, monkeypatch):
    url, path = migrated
    set_head(path, '0042_rate_limits')
    fake_destructive(monkeypatch, reviewed=False)
    with pytest.raises(SystemExit, match='without DESTRUCTIVE_REVIEWED'):
        manage.check_migrations(url)


def test_production_needs_confirmation_of_the_exact_reviewed_revision(migrated, monkeypatch):
    url, path = migrated
    set_head(path, '0042_rate_limits')
    fake_destructive(monkeypatch, reviewed=True)
    monkeypatch.setenv('ROTA_ENV', 'production')
    for confirmed in ((), ('0042_rate_limits',)):
        with pytest.raises(SystemExit, match='--confirm-destructive ' + HEAD):
            manage.check_migrations(url, confirmed)
    assert manage.check_migrations(url, {HEAD})[0] == HEAD  # pending ids, oldest first (later expand-only ones follow)


def test_migrate_cli_runs_the_gate_before_touching_the_schema(migrated):
    url, path = migrated
    set_head(path, '9999_from_a_newer_release')
    env = {k: v for k, v in os.environ.items() if not k.startswith(('DATABASE_URL', 'ROTA_'))}
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/manage.py'), 'migrate', '--database-url', url],
                            env={**env, 'ROTA_ENV': 'test'}, capture_output=True, text=True, timeout=90)
    assert result.returncode != 0 and 'downgrade' in result.stderr
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('SELECT version_num FROM alembic_version').fetchall() == [('9999_from_a_newer_release',)]


# ---- 2. Startup and operator commands --------------------------------------------------------------------------

@pytest.mark.parametrize('flag', ['ROTA_ALLOW_DISPOSABLE_TEST_DB', 'ROTA_ALLOW_TEST_ROLES'])
def test_test_only_flags_hard_fail_in_production(monkeypatch, flag):
    monkeypatch.setenv(flag, 'yes')
    with pytest.raises(RuntimeError, match=flag):
        Settings(environment='production').check()
    env = {k: v for k, v in os.environ.items() if not k.startswith('DATABASE_URL')}
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/manage.py'), 'migrate', '--database-url',
                             'postgresql+psycopg://x@db.invalid/rota'], env={**env, 'ROTA_ENV': 'production'},
                            capture_output=True, text=True, timeout=60)
    assert result.returncode != 0 and flag in result.stderr


def test_api_and_worker_never_migrate_seed_or_reset_on_start():
    banned = re.compile(r'command\.(upgrade|downgrade|stamp)|scripts\.manage|demo_data|create_all|drop_all|TRUNCATE|DROP (TABLE|SCHEMA)')
    hits = [f'{p.relative_to(ROOT)}: {m.group(0)}' for base in ('apps/api', 'apps/worker', 'packages')
            for p in (ROOT / base).rglob('*.py') for m in banned.finditer(p.read_text(encoding='utf-8'))]
    assert not hits, hits
    dockerfile = (ROOT / 'infra/Dockerfile').read_text()
    assert re.search(r'^CMD \["python", "-m", "uvicorn"', dockerfile, re.M)


def test_runtime_image_copies_every_module_and_operator_script_the_app_needs():
    # DEPLOY-PC4: apps/process_memory.py, the projection warmer, the VAPID generator and the emblem fallback were missing.
    runtime = (ROOT / 'infra/Dockerfile').read_text().split(' AS runtime', 1)[1]  # the web build stage is not shipped
    copied = {src for line in runtime.splitlines() if line.startswith('COPY ') and '--from=' not in line for src in line.split()[1:-1]}
    covered = lambda path: any(path == c or path.startswith(c.rstrip('/') + '/') for c in copied)
    imported = {m.group(1) for base in ('apps/api', 'apps/worker', 'packages') for p in (ROOT / base).rglob('*.py')
                for m in re.finditer(r'^\s*(?:from|import) apps\.(\w+)', p.read_text(encoding='utf-8'), re.M)}
    needed = {f'apps/{name}.py' if (ROOT / f'apps/{name}.py').exists() else f'apps/{name}' for name in imported}
    needed |= {'scripts/warm_read_projections.py', 'scripts/generate_vapid_key.py', 'apps/web/public/assets/brand/emblem.png'}
    assert not sorted(p for p in needed if not covered(p))


# ---- 3. Deploy script order and backup retention ---------------------------------------------------------------

def test_deploy_script_gates_backs_up_then_migrates_once():
    script = '\n'.join(l for l in (ROOT / 'tests/fixtures/guards/deploy.sh').read_text().splitlines() if not l.lstrip().startswith('#'))
    order = ['manage.py check-migrations', 'dc stop api worker', 'if ! $ROTA_BACKUP_CMD;', 'manage.py migrate', '\ndc up -d api worker']
    positions = [script.index(step) for step in order]
    assert positions == sorted(positions)
    assert script.count('manage.py migrate') == 1 and 'downgrade' not in script
    assert '"${ROTA_SKIP_BACKUP:-no}" = yes ] && [ "$pending" = 0 ]' in script  # never skip a backup before a migration
    # Even the first installation takes a verified backup before its initial migrations.
    assert "backup skipped: new installation" not in script
    # Both host layouts back up through a verified backup_job.py run.
    assert 'ROTA_BACKUP_CMD:=sudo systemctl start rota-backup.service' in script
    assert 'ROTA_BACKUP_CMD:-infra/production/ops.sh backup' in script
    # Automatic rollback to the previous image only when no migration ran.
    assert 'if [ "$pending" = 0 ] && [ -n "$previous" ]; then' in script




def test_both_deploy_paths_rebuild_the_fairness_ledger_only_after_a_successful_migration():
    script = '\n'.join(l for l in (ROOT / 'tests/fixtures/guards/deploy.sh').read_text().splitlines() if not l.lstrip().startswith('#'))
    rebuild = script.index('manage.py rebuild-fairness')
    assert script.index('manage.py migrate') < script.index('infra/database-roles.sql') < rebuild < script.index('\ndc up -d api worker')
    assert script.count('manage.py rebuild-fairness') == 2  # the step and the operator hint in its failure message
    guard = script.rindex('if [ "$pending" != 0 ]; then', 0, rebuild)
    assert script.index('\nfi\n', guard) > rebuild and 'exit 1' in script[rebuild:script.index('\nfi\n', guard)]
    local = (ROOT / 'tests/fixtures/guards/local-deploy.py').read_text()
    branch = local[local.index('if args.migrate and head != current:'):local.index('for role in ("api", "worker"):')]
    assert branch.index('"scripts/manage.py", "migrate"') < branch.index('database-roles.sql') < branch.index('"rebuild-fairness"')


def touch(directory, stamp):
    path = directory / f"rota-{stamp.strftime(backup_job.STAMP)}.dump"
    for item in backup_job.companions(path):
        item.write_text('x')
    return path


def test_pre_deploy_backup_survives_later_backups_for_keep_all_days(tmp_path):
    deploy = datetime(2026, 9, 26, 10, tzinfo=timezone.utc)
    before = touch(tmp_path, deploy)                          # pre-deploy backup
    touch(tmp_path, deploy + timedelta(hours=2))              # a second deploy the same day
    backup_job.prune(tmp_path, 7, 4, keep_all_days=7)
    assert before.exists()
    touch(tmp_path, deploy + timedelta(days=8))               # past the window: newest-per-day rules again
    backup_job.prune(tmp_path, 7, 4, keep_all_days=7)
    assert not before.exists()


# ---- 4. Data retention: every hard delete is a reviewed, intended one ------------------------------------------

# Evidence (publications, audit_events, review_decisions, definition_history, command_receipts, swap_events, source
# evidence) is additionally protected by database triggers (migrations 0002/0004/0005/0019) and never deleted in code.
ALLOWED_BULK = {'Assignment', 'ShiftService', 'Shift', 'PublicationChangeList', 'RateLimit', 'OIDCFlow',  # draft-workspace rows (triggers block published), expired counters and expired one-time sign-in flows
                'WishLedger',  # derived wish points per publication, rebuilt when an amendment supersedes it (wishes and outcomes stay)
                'FairnessLedger',  # derived fairness counts, rebuilt from the immutable publications they are computed from
                'MailboxConnection', 'MailboxOAuthFlow',  # owner-approved M4: revoke encrypted mailbox credentials; expire single-use consent flows, never email or receipt evidence
                'Session', 'LoginAttempt',  # worker pruning of expired security state: it authorises nothing
                'AuthLoginEvent'}  # explicit owner-approved security review: bounded authentication-only retention, at least one day; other audit evidence never expires
ALLOWED_ROW = {'apps/api/services/catalogue.py': 2,      # Audited unchanged creations/rotation attachments, and their provenance links; definition history is retained
               'apps/api/services/jobs.py': 1,           # applying a draft replacement suggestion
               'apps/api/services/github_integrations.py': 1,  # owner-requested credential revocation; receipts/audit remain
               'apps/api/services/planner_tools.py': 1,  # owner deletes a saved planner view
               'apps/api/services/planning.py': 1,       # series reconcile in a draft; selected draft removal uses the reviewed bulk Shift/Assignment/ShiftService path above
               'apps/api/services/mailboxes.py': 2,      # owner-approved M4: erase revoked encrypted tokens and expired/used security flows only
               'apps/api/services/rule_catalogue.py': 1,  # an acknowledgement taken back (its snapshot stays in definition_history)
               'apps/api/services/table_views.py': 1,    # owner deletes a saved table view
               'apps/api/services/tags.py': 1}           # a record leaves a tag (a reference row; its snapshot stays in definition_history)


def test_hard_delete_paths_are_inventoried():
    bulk, rows = set(), {}
    for base in ('apps', 'packages'):
        for p in (ROOT / base).rglob('*.py'):
            text = p.read_text(encoding='utf-8')
            deleted = set(re.findall(r'\bdelete\(m\.(\w+)\)', text))
            if 'AuthLoginEvent' in deleted:
                assert p == ROOT / 'apps/worker/main.py'  # The owner approved worker retention only.
            bulk |= deleted
            if n := len(re.findall(r'\bdb\.delete\(', text)):
                rows[p.relative_to(ROOT).as_posix()] = n
    assert bulk <= ALLOWED_BULK, f'New hard delete of {bulk - ALLOWED_BULK}: soft-delete/archive instead or review it here'
    assert rows == ALLOWED_ROW, 'A hard-delete call site changed: confirm it never removes operational evidence, then update this list'


def test_integration_credentials_migration_is_expand_only():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    path = safety.VERSIONS / '0073_integration_credentials.py'
    assert safety.revision(path) == '0073_integration_credentials'
    assert not safety.findings(path) and not safety.needs_confirmation(path)
    scripts = ScriptDirectory.from_config(Config(str(ROOT / 'alembic.ini')))
    assert scripts.get_revision('0073_integration_credentials').down_revision == '0076_monitor_events'
    head, = scripts.get_heads()  # exactly one head, including future additive descendants
    assert '0073_integration_credentials' in {r.revision for r in scripts.iterate_revisions(head, 'base')}


def test_source_rota_configuration_follows_residency_with_one_additive_head():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    path = safety.VERSIONS / '0074_source_rota_configuration.py'
    assert not safety.findings(path) and not safety.needs_confirmation(path)
    scripts = ScriptDirectory.from_config(Config(str(ROOT / 'alembic.ini')))
    assert scripts.get_revision('0074_source_rota_configuration').down_revision == '0091_programme_ownership'
    head, = scripts.get_heads()
    assert '0074_source_rota_configuration' in {r.revision for r in scripts.iterate_revisions(head, 'base')}


@pytest.mark.parametrize('previous', ['0069_schedule_view_scope', '0076_monitor_events', '0083_swap_approval_settings',
                                      '0086_swap_open', '0070_assignment_source', '0131_read_projection_cursor'])
def test_integration_migration_preserves_existing_rows(tmp_path, previous):
    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from apps.api import models as m
    from apps.api.db import make_engine, transaction
    path = tmp_path / 'integration-upgrade.db'
    cfg = Config(str(ROOT / 'alembic.ini'))
    cfg.set_main_option('sqlalchemy.url', 'sqlite:///' + str(path))
    scripts = ScriptDirectory.from_config(cfg)
    head, = scripts.get_heads()
    command.upgrade(cfg, previous)
    if previous == '0076_monitor_events':
        engine = make_engine('sqlite:///' + str(path))
        try:
            with current_seed_columns(engine), transaction(engine, write=True) as db:
                organisation = m.Organisation(name='Monitor evidence organisation')
                db.add(organisation); db.flush()
                department = m.Department(organisation_id=organisation.id, name='Monitor evidence department')
                db.add(department); db.flush()
                db.add(m.MonitorIntegration(department_id=department.id, token_hash='a' * 64))
                db.add(m.MonitorIncident(id='retained-incident', department_id=department.id, source='synthetic',
                       state='down', received_at=datetime(2026, 10, 3, tzinfo=timezone.utc),
                       raw_payload={'message': 'Retained monitor evidence'}, event_key='b' * 64))
        finally:
            engine.dispose()
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("INSERT INTO organisations (id, revision, created_at, name, timezone, week_start, input_revision, availability_revision) "
                   "VALUES ('retained', 1, '2026-10-03', 'Retained organisation', 'UTC', 6, 1, 1)")
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name != 'alembic_version'")]
        columns = {table: [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')] for table in tables}
        # Compare every pre-upgrade column and row while permitting additive columns.
        def snapshot(connection):
            return {table: connection.execute('SELECT ' + ', '.join('"' + c + '"' for c in columns[table])
                    + f' FROM "{table}"').fetchall() for table in tables}
        before = snapshot(db)
    if previous == '0076_monitor_events':
        expected = [r.revision for r in reversed(list(scripts.iterate_revisions(head, previous)))]
        assert expected[0] == '0073_integration_credentials'
        assert manage.check_migrations('sqlite:///' + str(path)) == expected
    command.upgrade(cfg, 'head')
    command.upgrade(cfg, 'head')  # A repeated deploy has no additional work.
    with closing(sqlite3.connect(path)) as db:
        assert_only_scoring_defaults_added(before, snapshot(db), columns)
        assert db.execute('SELECT count(*) FROM policy_seed_lines').fetchone() == (0,)
        assert db.execute('SELECT version_num FROM alembic_version').fetchall() == [(head,)]
        assert db.execute('SELECT count(*) FROM integration_credentials').fetchone() == (0,)
        assert db.execute('SELECT count(*) FROM bulk_staged').fetchone() == (0,)
        assert 'manual_type_override' in {r[1] for r in db.execute('PRAGMA table_info(availability)')}
        assert 'batch_id' in {r[1] for r in db.execute('PRAGMA table_info(definition_history)')}
        assert 'bulk_settings' in {r[1] for r in db.execute('PRAGMA table_info(departments)')}
# Include this file in hosted supervision and team-lead qualification.
