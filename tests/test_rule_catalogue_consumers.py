"""Consumers of catalogue rules (docs/design/rule-catalogue.md §3): the rule map, the session's rules, which rule
applies to a person, quota rows, clinic permission gaps and Fairness read a catalogue rule through its lines."""
from datetime import date

from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts.schemas import PolicyDefinition
from packages.domain.rule_map import rule_map
from packages.domain.validation import evaluate
from .test_rule_catalogue_guard import swapped, world


def stored(h, old, made):
    """The catalogue rules in force in place of the old ones (as activating them would leave the database)."""
    with transaction(h.app.state.engine, write=True) as db:
        for p in old.values():
            row = db.get(m.Policy, p['id'])
            row.status, row.active = 'retired', False
        for row in db.scalars(select(m.Policy).where(m.Policy.department_id == h.seed['department_id'],
                m.Policy.kind == 'catalogue', m.Policy.status == 'active')):
            replacement = next((p for p in made.values()
                if p['definition']['rule']['id'] == row.definition['rule']['id']), None)
            if replacement:
                replacement['definition']['rule']['lines'] += row.definition['rule']['lines']
                row.status, row.active = 'retired', False
        db.flush()
        for p in made.values():
            db.add(m.Policy(id=p['id'], department_id=h.seed['department_id'], name=p['name'], kind='catalogue', status='active',
                            definition=PolicyDefinition.model_validate(p['definition']).model_dump(mode='json'), owner=p['owner'], source=p['source'], starts_on=date(2020, 1, 1),
                            created_by=h.seed['identities']['coordinator'], approved_by=h.seed['identities']['approver']))


def test_the_rule_map_reads_a_catalogue_rule_through_its_lines(h):
    w, old, made = world(h)
    before = h.snapshot(w)
    after = swapped(before, old, made)
    was, now = rule_map(before), rule_map(after)
    by = lambda result: {r['id']: r for r in result['rules']}  # noqa: E731
    was, now_rules = by(was), by(now)
    for key in ('cover', 'off', 'cap'):
        entry = now_rules[made[key]['id']]
        assert entry['kind'] == 'catalogue' and entry['catalogue_id'] == made[key]['definition']['rule']['id']
        assert entry['sessions'] == was[old[key]['id']]['sessions'] and entry['people'] == was[old[key]['id']]['people']
        assert len(entry['lines']) == len(made[key]['definition']['rule']['lines'])
    assert now_rules[made['cover']['id']]['safety'] and not now_rules[made['cap']['id']]['safety']
    a = h.seed['people'][0]
    narrower = [x for x in now['markers'] if x['kind'] == 'narrower_line' and x['rule_id'] == made['cap']['id']]
    assert narrower and narrower[0]['people'] == [a]
    assert set(now_rules[made['cap']['id']]['line_people'][a][0][:2]) and now_rules[made['cap']['id']]['line_people'][a][0][2] == 1


def test_quota_rows_name_their_catalogue_line(h):
    w, old, made = world(h)
    after = swapped(h.snapshot(w), old, made)
    rows = [r for r in evaluate(after)['quota_report']['rows'] if r['policy_id'] == made['quota']['id']]
    assert rows and {r['line_id'] for r in rows} <= {line['id'] for line in made['quota']['definition']['rule']['lines']}


def test_session_rules_and_who_a_rule_applies_to_read_catalogue_lines(h):
    w, old, made = world(h)
    stored(h, old, made)
    # TEST-STALE: this reviewed catalogue replacement is non-safety; refresh per freeze-matrix records 425d832da/6ef713812.
    w = h.refresh_draft_baseline(h.detail(w)['workspace'])
    a, b, c = h.seed['people']
    w = h.detail(w)['workspace']
    shift = next(s for s in h.detail(w)['shifts'] if any(p['person_id'] == a for p in s['participants']))
    value = h.get('coordinator', f"/api/workspaces/{w['id']}/shifts/{shift['id']}/rules")
    mine = {r['id']: r for r in next(p for p in value['people'] if p['person_id'] == a)['rules']}
    cap = mine[made['cap']['id']]
    assert cap['kind'] == 'max_duties' and cap['number']['value'] == 1  # A's own line, not the line for everyone
    assert cap['own_line']['id'] == made['cap']['definition']['rule']['lines'][1]['id']
    applies = {r['policy_id']: r for r in h.get('coordinator', f'/api/policies/applies?person_id={a}&day=2026-10-05')['rules']}
    assert applies[made['cap']['id']]['rule']['maximum'] == 1 and applies[made['cap']['id']]['why'].startswith('Line 2 of this rule')
    groups = h.get('coordinator', '/api/policies/groups')  # grouping reads the older rules only
    assert not {i for g in groups['groups'] for i in g['rule_ids']} & {p['id'] for p in made.values()}


def test_clinic_permission_gaps_count_catalogue_cover_lines(h):
    from apps.api.services.catalogue import eligibility_gaps
    w, old, made = world(h)
    stored(h, old, made)
    with transaction(h.app.state.engine) as db:
        assert eligibility_gaps(db, h.seed['department_id']) == []  # open-ended permissions: no gap, and no error reading lines
