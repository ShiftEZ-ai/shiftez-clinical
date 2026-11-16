"""Expose built-in fairness and lead-turn limits without rewriting any stored definition or evidence.

Follows dev's single head 0108_password_login; 0109 avoids numbers already used by other open PRs.
Existing catalogue rules gain immutable appended lines; their identity and every reference stay intact.
"""
from datetime import date, datetime, timezone
import uuid

from alembic import op
import sqlalchemy as sa

revision = '0109_fair_share_dials'
down_revision = '0108_password_login'
branch_labels = depends_on = None

SOURCE = 'C2 built-in defaults (owner approved 2026-10-15)'


def line_id(department_id, key):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f'fmuc:{department_id}:scoring-default:{key}'))


def independent_defaults(policy, today):
    # Mirrors scoring_defaults.independent_defaults; migrations never import application code.
    definition = policy['definition']
    return (not policy['active'] or not definition['rule'].get('on', True)
            or policy.get('ends_on') is not None or bool(definition.get('workspace_id'))
            or (policy.get('starts_on') is not None and date.fromisoformat(str(policy['starts_on'])) > today))


def additions(department_id, types, policies, lead_strength, catalogue_version=2):
    # Frozen migration defaults: later authoring defaults must not change upgrades of existing installations.
    existing = {line['id'] for p in policies for line in p['definition']['rule'].get('lines', [])}
    existing.update(p['id'] for p in policies if p.get('source') == SOURCE)
    cap_entry = 'workload_limit' if catalogue_version == 2 else 'other_counts_per_period'
    result = {'fair_share': [], cap_entry: []}
    if line_id(department_id, 'workload') not in existing:
        result['fair_share'].append({'id': line_id(department_id, 'workload'),
            'numbers': {'apply': {'value': None, 'level': 'medium'}}, 'settings': {'balance_goal': {
                'metric': 'workload', 'aim': 'workload_spread', 'period': 'rota', 'share_by': 'equal',
                'tolerance_percent': 0, 'tolerance_units': 0}},
            'note': 'Default even workload spread; previously a built-in scoring and scheduling preference.'})
    goals = []
    for policy in policies:
        if independent_defaults(policy, date(2000, 1, 1)):
            continue
        rule = policy['definition']['rule']
        if rule['kind'] == 'balance_goal':
            goals.append(rule)
        elif rule['kind'] == 'catalogue' and rule['id'] == 'fair_share' and rule.get('on', True):
            for line in rule.get('lines', []):
                # A dated line cannot replace the former built-in outside its own effective dates.
                if line.get('ends_on') or line.get('starts_on') and str(line['starts_on']) > '2000-01-01':
                    continue
                if any(key == 'apply' or number.get('value') is not None for key, number in line['numbers'].items()):
                    goals.append({**rule.get('settings', {}).get('balance_goal', {}),
                                  **line.get('settings', {}).get('balance_goal', {})})
    lead_goals = [goal for goal in goals if goal.get('metric') == 'lead_turns']
    all_leads = any(not goal.get('assignment_type_ids') for goal in lead_goals)
    covered = {tid for goal in lead_goals for tid in goal.get('assignment_type_ids', [])}
    for typ in sorted(types, key=lambda t: t['id']):
        tid, lead = typ['id'], (typ.get('definition') or {}).get('lead')
        if not lead:
            continue
        where = {'match': 'all', 'conditions': [{'field': 'assignment_type_ids', 'op': 'any', 'values': [tid]}]}
        if not all_leads and tid not in covered:
            lead_where = {'match': 'all', 'conditions': [{**where['conditions'][0], 'count': 'each'}]}
            result['fair_share'].append({'id': line_id(department_id, 'lead:' + tid), 'where': lead_where,
                'numbers': {'apply': {'value': None, 'level': lead_strength}}, 'settings': {'balance_goal': {
                    'metric': 'lead_turns', 'aim': 'equal_share', 'share_by': 'available_days',
                    'period': 'rolling', 'period_days': 182, 'assignment_type_ids': [tid]}},
                'note': 'Default fair team-lead turns: ' + typ['name']})
        if lead.get('most_turns') and line_id(department_id, 'lead-cap:' + tid) not in existing:
            result[cap_entry].append({'id': line_id(department_id, 'lead-cap:' + tid), 'where': where,
                'numbers': {'max': {'value': lead['most_turns'], 'level': 'critical'}},
                'settings': {'block_quota': {'metric': 'lead_turns', 'starts_on': '2000-01-01',
                    'repeat': True, 'assignment_type_ids': [tid],
                    'window_alignment': 'calendar_' + lead.get('most_turns_per', 'month')}},
                'note': 'Default lead-turn limit: ' + typ['name']})
    return result


def backfill(bind):
    metadata = sa.MetaData()
    departments, policies, types, identities, histories, seed_lines = [sa.Table(name, metadata, autoload_with=bind)
        for name in ('departments', 'policies', 'assignment_types', 'identities', 'definition_history', 'policy_seed_lines')]
    now = datetime.now(timezone.utc)
    actor_row = bind.execute(sa.select(identities.c.id, identities.c.disabled).where(identities.c.issuer == 'urn:fmuc:maintenance',
        identities.c.subject == 'system-maintenance')).first()
    actor = actor_row.id if actor_row else None
    default_actor_id = actor if actor_row and actor_row.disabled else None
    for department in bind.execute(sa.select(departments)).mappings():
        current = [dict(p) for p in bind.execute(sa.select(policies).where(policies.c.department_id == department['id'],
            policies.c.status == 'active')).mappings()]
        # Mirrors rule_lines.department_catalogue_version; migrations never import application code.
        lineage = bind.execute(sa.select(histories.c.snapshot).where(
            histories.c.department_id == department['id'], histories.c.kind == 'policy')
            .order_by(histories.c.object_revision)).scalars()
        by_id = {p['id']: p for p in [*lineage, *current]}

        def seeded_default(policy):
            seen = set()
            while policy['id'] not in seen:
                seen.add(policy['id'])
                if (policy.get('source') == SOURCE and default_actor_id
                        and policy.get('created_by') == default_actor_id == policy.get('approved_by')):
                    return True
                definition = policy['definition']
                previous = by_id.get(definition.get('replaces_id'))
                if (policy['kind'] not in {'balance_goal', 'block_quota'} or not policy.get('approved_by')
                        or definition.get('severity') != 'advisory' or definition.get('merged_ids')
                        or not previous or previous['kind'] != policy['kind']
                        or previous.get('department_id') != policy.get('department_id')
                        or previous['definition'].get('severity') != 'advisory'):
                    return False
                policy = previous
            return False

        catalogue_version = 1 if any(
            (p['kind'] != 'catalogue' or p['definition']['rule'].get('catalogue_version', 1) == 1)
            and not (p['kind'] != 'catalogue' and seeded_default(p))
            for p in current) else 2
        # A repeated backfill sees previously appended lines, without editing the source rows.
        appended = list(bind.execute(sa.select(seed_lines).where(seed_lines.c.department_id == department['id'])).mappings())
        effective = []
        for policy in current:
            rule = policy['definition']['rule']
            lines = [r['line'] for r in appended if r['policy_id'] == policy['id'] and r['policy_revision'] <= policy['revision']]
            effective.append({**policy, 'definition': {**policy['definition'], 'rule': {**rule,
                'lines': [*rule.get('lines', []), *lines]}}} if lines else policy)
        duty_types = [dict(t) for t in bind.execute(sa.select(types).where(types.c.department_id == department['id'])).mappings()]
        strength = ((department['generation_defaults'] or {}).get('scoring') or {}).get('lead_balance', 'medium')
        extra = additions(department['id'], duty_types, effective, strength, catalogue_version)
        if not any(extra.values()):
            continue
        if actor is None:
            actor = str(uuid.uuid4())
            bind.execute(identities.insert().values(id=actor, revision=1, created_at=now, issuer='urn:fmuc:maintenance',
                subject='system-maintenance', display_name='System maintenance (not a user)', disabled=True))
            default_actor_id = actor
        for catalogue_id, lines in extra.items():
            if not lines:
                continue
            old = next((p for p in current if p['kind'] == 'catalogue' and p['definition']['rule']['id'] == catalogue_id), None)
            if old and independent_defaults(old, date(2000, 1, 1)):
                for line in lines:
                    kind = 'block_quota' if 'max' in line['numbers'] else 'balance_goal'
                    body = {'kind': kind, **line['settings'][kind]}
                    if kind == 'block_quota':
                        body = {'starts_on': None, 'ends_on': None, 'maximum_amount': None,
                            'target_amount': None, 'target_strength': None, 'participation': [], 'overtime': 'include',
                            'window_days': None, 'prorate_department_days': False, 'reference_days': 28,
                            'rounding': 'nearest', **body, 'comparison': 'maximum', 'amount': line['numbers']['max']['value']}
                    row = dict(id=line['id'], revision=1, created_at=now, department_id=department['id'],
                        name=line['note'][:160], kind=kind, definition={'scope': {}, 'severity': 'advisory',
                            'strength': next(iter(line['numbers'].values()))['level'], 'rule': body},
                        starts_on=date(2000, 1, 1), ends_on=None, active=True, status='active', owner='Department',
                        source=SOURCE, created_by=actor, approved_by=actor)
                    bind.execute(policies.insert().values(**row))
                    snapshot = {key: value.isoformat() if isinstance(value, (date, datetime)) else value for key, value in row.items()}
                    bind.execute(histories.insert().values(id=str(uuid.uuid4()), revision=1, created_at=now,
                        department_id=department['id'], kind='policy', object_id=row['id'], object_revision=1,
                        snapshot=snapshot, actor_id=actor, reason=SOURCE))
                continue
            if old:
                appended_rows = [dict(id=line['id'], revision=1, created_at=now,
                    department_id=department['id'], policy_id=old['id'], policy_revision=old['revision'], line=line)
                    for line in lines]
                bind.execute(seed_lines.insert(), appended_rows)
                bind.execute(histories.insert(), [dict(id=str(uuid.uuid4()), revision=1, created_at=now,
                    department_id=department['id'], kind='policy_seed_line', object_id=row['id'], object_revision=1,
                    snapshot={**row, 'created_at': now.isoformat()}, actor_id=actor, reason=SOURCE) for row in appended_rows])
                continue
            row = dict(id=str(uuid.uuid4()), revision=1, created_at=now, department_id=department['id'],
                name='Fair share' if catalogue_id == 'fair_share' else 'Workload limit',
                kind='catalogue', definition={'schema_version': 1, 'scope': {}, 'severity': 'mandatory',
                    'strength': 'medium', 'balance_between_people': True, 'exception_allowed': False,
                    'rule': {'kind': 'catalogue', 'id': catalogue_id,
                    'catalogue_version': catalogue_version, 'lines': lines}}, starts_on=date(2000, 1, 1), ends_on=None, active=True,
                status='active', owner='Department', source=SOURCE, created_by=actor, approved_by=actor)
            bind.execute(policies.insert().values(**row))
            snapshot = {key: value.isoformat() if isinstance(value, (date, datetime)) else value for key, value in row.items()}
            bind.execute(histories.insert().values(id=str(uuid.uuid4()), revision=1, created_at=now,
                department_id=department['id'], kind='policy', object_id=row['id'], object_revision=row['revision'],
                snapshot=snapshot, actor_id=actor, reason=SOURCE))


def upgrade():
    op.create_table('policy_seed_lines',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('department_id', sa.String(36), sa.ForeignKey('departments.id'), nullable=False, index=True),
        sa.Column('policy_id', sa.String(36), sa.ForeignKey('policies.id'), nullable=False, index=True),
        sa.Column('policy_revision', sa.Integer(), nullable=False),
        sa.Column('line', sa.JSON(), nullable=False))
    if op.get_bind().dialect.name == 'postgresql':
        op.execute("""DO $$ BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'rota_app') THEN
                REVOKE UPDATE, DELETE ON policy_seed_lines FROM rota_app;
            END IF;
        END $$""")
    backfill(op.get_bind())
    if op.get_bind().dialect.name == 'sqlite':
        for action in ('UPDATE', 'DELETE'):
            op.execute(f"CREATE TRIGGER policy_seed_lines_{action.lower()}_guard BEFORE {action} ON policy_seed_lines BEGIN SELECT RAISE(ABORT, 'immutable evidence'); END")
    else:
        op.execute('CREATE TRIGGER policy_seed_lines_guard BEFORE UPDATE OR DELETE ON policy_seed_lines '
                   'FOR EACH ROW EXECUTE FUNCTION rota_immutable_evidence()')


def downgrade():
    raise RuntimeError('Forward-only: retain every rule version and its approval evidence')
