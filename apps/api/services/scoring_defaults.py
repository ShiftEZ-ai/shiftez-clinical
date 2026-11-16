"""Visible replacements for the former built-in workload and team-lead preferences."""
from datetime import date

from sqlalchemy import select

from apps.api import models as m
from apps.api.serialization import record
from packages.contracts import schemas as s
from packages.domain.conditions import active_rules
from packages.domain.rule_lines import SCORING_DEFAULT_SOURCE as SOURCE, scoring_default_id as line_id
from scripts.system_command import maintenance_identity
from .catalogue_version import department_version


def workload_line(department_id):
    return {'id': line_id(department_id, 'workload'), 'numbers': {'apply': {'value': None, 'level': 'medium'}},
            'settings': {'balance_goal': {'metric': 'workload', 'aim': 'workload_spread',
                                          'period': 'rota', 'share_by': 'equal',
                                          'tolerance_percent': 0, 'tolerance_units': 0}},
            'note': 'Default even workload spread; previously a built-in scoring and scheduling preference.'}


def independent_defaults(policy, today):
    """An off, dated or rota-only catalogue cannot carry a department-wide former built-in."""
    definition = policy['definition']
    return (not policy['active'] or not definition['rule'].get('on', True)
            or policy.get('ends_on') is not None or bool(definition.get('workspace_id'))
            or (policy.get('starts_on') is not None and date.fromisoformat(str(policy['starts_on'])) > today))


def additions(department_id, types, policies, lead_strength='medium', workload=True, catalogue_version=2):
    """Seed each former built-in once; only permanent department-wide goals replace all default dates."""
    existing = {line['id'] for p in policies for line in p['definition']['rule'].get('lines', [])}
    existing.update(p['id'] for p in policies if p.get('source') == SOURCE)
    cap_entry = 'workload_limit' if catalogue_version == 2 else 'other_counts_per_period'
    result = {'fair_share': [], cap_entry: []}
    if workload and line_id(department_id, 'workload') not in existing:
        result['fair_share'].append(workload_line(department_id))
    goals = [p['definition']['rule'] for p in active_rules(policies)
             if p['definition']['rule']['kind'] == 'balance_goal'
             and p['definition']['rule']['metric'] == 'lead_turns'
             and not independent_defaults(p, date(2000, 1, 1))]
    all_leads = any(not goal.get('assignment_type_ids') for goal in goals)
    covered = {tid for goal in goals for tid in goal.get('assignment_type_ids', [])}
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


def seeded_rows(db, policies):
    from packages.domain.rule_lines import seeded_policies
    ids = [p['id'] for p in policies]
    rows = [record(r) for r in db.scalars(select(m.PolicySeedLine).where(m.PolicySeedLine.policy_id.in_(ids)))] if ids else []
    return seeded_policies(policies, rows)


def seed_line_ids(db, policy_ids):
    """Retain seed identities after lifecycle revisions materialise them into the source definition."""
    return set(db.scalars(select(m.PolicySeedLine.id).join(m.Policy,
        m.PolicySeedLine.policy_id == m.Policy.id).where(m.Policy.id.in_(policy_ids),
        m.PolicySeedLine.policy_revision <= m.Policy.revision))) if policy_ids else set()


def seed(db, department_id, *, types=None, workload=True, catalogue_version=None):
    """Append immutable default lines; existing rule IDs, revisions, definitions and references stay intact."""
    department = db.get(m.Department, department_id)
    retained = list(db.scalars(select(m.Policy).where(m.Policy.department_id == department_id)))
    policies = [p for p in retained if p.status == 'active']
    if catalogue_version is None:
        catalogue_version = department_version(db, department_id, policies)
    effective = seeded_rows(db, [record(p) for p in policies])
    types = list(db.scalars(select(m.AssignmentType).where(m.AssignmentType.department_id == department_id))) if types is None else types
    strength = ((department.generation_defaults or {}).get('scoring') or {}).get('lead_balance', 'medium')
    extra = additions(department_id, [record(t) for t in types], effective, strength, workload, catalogue_version)
    seeded = set(db.scalars(select(m.PolicySeedLine.id).where(m.PolicySeedLine.department_id == department_id)))
    seeded.update(p.id for p in retained)
    seeded.update(line['id'] for p in retained if p.approved_by for line in p.definition['rule'].get('lines', []))
    # Approved removals still retain their immutable seed identities; never create those defaults again.
    extra = {key: [line for line in lines if line['id'] not in seeded] for key, lines in extra.items()}
    if not any(extra.values()):
        return []
    identity = maintenance_identity(db, m, select)
    created = []
    for catalogue_id, lines in extra.items():
        if not lines:
            continue
        old = next((p for p in policies if p.kind == 'catalogue' and p.definition['rule']['id'] == catalogue_id), None)
        if old and independent_defaults(record(old), date(2000, 1, 1)):
            # Keep defaults independent when a catalogue cannot cover their full lifetime.
            for line in lines:
                kind = 'block_quota' if 'max' in line['numbers'] else 'balance_goal'
                body = {'kind': kind, **line['settings'][kind]}
                if kind == 'block_quota':
                    body.update(comparison='maximum', amount=line['numbers']['max']['value'])
                definition = s.PolicyDefinition.model_validate({'severity': 'advisory',
                    'strength': next(iter(line['numbers'].values()))['level'], 'rule': body}).model_dump(mode='json')
                row = m.Policy(id=line['id'], department_id=department_id, name=line['note'][:160], kind=kind,
                    definition=definition, starts_on=date(2000, 1, 1), active=True, status='active',
                    owner='Department', source=SOURCE, created_by=identity.id, approved_by=identity.id)
                db.add(row)
                db.flush()
                db.add(m.DefinitionHistory(department_id=department_id, kind='policy', object_id=row.id,
                    object_revision=row.revision, snapshot=record(row), actor_id=identity.id, reason=SOURCE))
                created.append(row)
            continue
        if old:
            appended = [m.PolicySeedLine(id=line['id'], department_id=department_id,
                policy_id=old.id, policy_revision=old.revision, line=line) for line in lines]
            db.add_all(appended)
            db.flush()
            db.add_all(m.DefinitionHistory(department_id=department_id, kind='policy_seed_line', object_id=row.id,
                object_revision=row.revision, snapshot=record(row), actor_id=identity.id, reason=SOURCE) for row in appended)
            continue
        definition = s.PolicyDefinition.model_validate({'rule': {'kind': 'catalogue', 'id': catalogue_id,
            'catalogue_version': catalogue_version, 'lines': lines}}).model_dump(mode='json')
        row = m.Policy(department_id=department_id, name='Fair share' if catalogue_id == 'fair_share' else 'Workload limit',
            kind='catalogue', definition=definition, starts_on=date(2000, 1, 1), active=True, status='active',
            owner='Department', source=SOURCE, created_by=identity.id, approved_by=identity.id)
        db.add(row)
        db.flush()
        db.add(m.DefinitionHistory(department_id=department_id, kind='policy', object_id=row.id,
            object_revision=row.revision, snapshot=record(row), actor_id=identity.id, reason=SOURCE))
        created.append(row)
    db.flush()
    return created
