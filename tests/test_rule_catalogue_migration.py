"""Rules in force move into the rule catalogue (docs/design/rule-catalogue.md §4): every rule becomes lines that read
exactly as it did, or is kept as it is and reported with the reason — never silently different."""
import random
import uuid
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts.rule_catalogue import GREEDY_NOT_APPLIED
from packages.contracts.schemas import PolicyDefinition
from packages.domain import rule_groups
from packages.domain.rule_catalogue_migration import held_reasons, id_map, plan
from packages.domain.rule_groups import expanded
from packages.domain.rule_lines import duplicates, outranking
from packages.domain.validation import evaluate
from .test_rule_catalogue_guard import world
from .test_rule_simplification import LABELS, PERSON, PROGRAMME, SERVICE, WEEK_START, production_families, quota, rule
from .test_rule_templates import activate


def after(rules, proposals):
    moved = id_map(proposals)
    return [x['row'] for x in proposals] + [r for r in rules if r['id'] not in moved]


def same(rules, proposals):
    return expanded(rules, id_map(proposals), WEEK_START) == expanded(after(rules, proposals), None, WEEK_START)


def test_every_production_family_moves_into_the_catalogue_unchanged():
    rules = production_families()
    proposals, report = plan(rules, {}, WEEK_START, 'd')
    assert report == [] and sum(len(x['rule_ids']) for x in proposals) == len(rules) and same(rules, proposals)
    assert not [x for x in proposals if duplicates(x['payload']['definition']['rule'])]  # different measures are never the same line
    # One catalogue rule per entry; each copy is a line.
    assert {x['catalogue_id'] for x in proposals} >= {'duties_per_period', 'protected_time', 'keep_off', 'overtime_offers'}
    assert all(x['payload']['definition']['merged_ids'] == x['rule_ids'] for x in proposals)


def test_grouped_rules_move_too_and_overtime_names_the_limits_in_force():
    rules = production_families()
    grouped = [{**g['payload'], 'id': f'00000000-0000-4000-8000-{n:012d}', 'status': 'active', 'active': True, 'kind': g['kind'],
                'department_id': 'd'} for n, g in enumerate(g for g in rule_groups.plan(rules, LABELS, WEEK_START) if 'payload' in g)]
    proposals, report = plan(grouped, {}, WEEK_START, 'd')
    assert report == [] and same(grouped, proposals)
    assert not [x for x in proposals if duplicates(x['payload']['definition']['rule'])]
    cap = next(p for p in grouped if p['name'] == 'UCC overtime cap')
    offer = next(x for x in proposals if x['catalogue_id'] == 'overtime_offers')
    required = {i for line in offer['payload']['definition']['rule']['lines'] for i in line['settings']['allocation_overtime']['required_policy_ids']}
    assert required == {cap['id']}  # the monthly caps grouped into it are known by the rule that holds them


def test_rules_that_cannot_move_unchanged_are_kept_and_reported():
    a, b = PERSON['Kopofiku'], PERSON['Badr']
    everyone = rule('Weekly cap', {'kind': 'max_duties', 'maximum': 5})
    mine = rule('Kopofiku weekly cap', {'kind': 'max_duties', 'maximum': 3}, {'person_id': a})
    tuesday = rule('Cap with a Tuesday line', {'kind': 'max_hours', 'hours': 40}, per_person=[{'person_id': b, 'amount': 30, 'weekdays': [1]}])
    mixed = rule('Mixed', {'kind': 'restriction'}, {'conditions': {'match': 'any', 'conditions': [
        {'field': 'person_ids', 'values': [a]}, {'field': 'service_ids', 'values': [SERVICE['Clinic A']]}]}})
    excepted = rule('Excepted', {'kind': 'min_rest', 'hours': 11}, exception_allowed=True)
    rota_only = rule('Rota only', {'kind': 'min_rest', 'hours': 10}, workspace_id=str(uuid.uuid4()))
    narrower = rule('Narrower', {'kind': 'max_consecutive_days', 'maximum': 4}, {'person_id': b}, overrides=[str(uuid.uuid4())])
    rules = [everyone, mine, tuesday, mixed, excepted, rota_only, narrower]
    held = held_reasons(rules, exceptions=[excepted['id']])
    proposals, report = plan(rules, held, WEEK_START, 'd')
    reasons = {r['name']: r['reason'] for r in report}
    assert set(reasons) == {'Kopofiku weekly cap', 'Cap with a Tuesday line', 'Mixed', 'Excepted', 'Rota only', 'Narrower'}
    assert 'unresolved outranking' in reasons['Kopofiku weekly cap'] and 'person exceptions' in reasons['Excepted']
    assert 'would not check exactly' in reasons['Cap with a Tuesday line'] and 'mix people and sessions' in reasons['Mixed']
    assert 'rota-only' in reasons['Rota only'] and 'replaces another rule' in reasons['Narrower']
    assert all(r['reason'].startswith('Not in the catalogue yet: ') for r in report)
    assert [x['rule_ids'] for x in proposals] == [[everyone['id']]] and same(rules, proposals)


def test_a_person_line_in_its_own_rule_moves_as_the_most_specific_line():
    a = PERSON['Kopofiku']
    cap = rule('Cap', {'kind': 'max_duties', 'maximum': 5}, per_person=[{'person_id': a, 'amount': 2, 'starts_on': '2026-09-01', 'ends_on': '2026-10-01'}])
    block = rule('Block', quota('2026-08-23', '2026-11-01', 6, 'maximum', 'duties'), per_person=[
        {'person_id': a, 'amount': 2, 'starts_on': '2026-09-06', 'ends_on': '2026-10-04'}])
    proposals, report = plan([cap, block], {}, WEEK_START, 'd')
    assert report == [] and same([cap, block], proposals)


def test_rules_counting_different_windows_are_different_lines_not_duplicates():
    """As on dev's duties per period (10 rules -> 148 lines): a day, a rolling run of days and a block count different
    things, so the same people and places twice is never the same line, and none outranks another."""
    a = PERSON['Kopofiku']
    rules = [rule('Daily cap', {'kind': 'max_duties', 'maximum': 1, 'window': 'day'}),
             rule('Three-day cap', {'kind': 'max_duties', 'maximum': 2, 'window': 'rolling', 'window_days': 3}),
             rule('Block cap', quota('2026-08-23', '2026-11-01', 20, 'maximum', 'duties')),
             rule('Overtime block cap', quota('2026-08-23', '2026-11-01', 4, 'maximum', 'duties', overtime='only')),
             rule('Kopofiku daily cap', {'kind': 'max_duties', 'maximum': 1, 'window': 'day'}, {'person_id': a}, '2026-08-23', '2026-11-01')]
    proposals, report = plan(rules[:4], {}, WEEK_START, 'd')
    [period] = proposals
    assert report == [] and len(period['payload']['definition']['rule']['lines']) == 4 and same(rules[:4], proposals)
    assert duplicates(period['payload']['definition']['rule']) == [] and outranking(period['row']) == {}
    # The same measure twice for the same people is a duplicate: the later old rule stays apart (both apply today).
    twice = rule('Daily cap again', {'kind': 'max_duties', 'maximum': 2, 'window': 'day'})
    proposals, report = plan(rules[:1] + [twice], {}, WEEK_START, 'd')
    assert [r['name'] for r in report] == ['Daily cap again'] and 'same line as one of “Daily cap”' in report[0]['reason']
    # It names the other rule also when that one is already a line of the catalogue rule in force (never itself).
    [daily] = plan(rules[:1], {}, WEEK_START, 'd')[0]
    in_force = {**daily['row'], 'id': str(uuid.uuid4())}
    _, report = plan([in_force, twice], {}, WEEK_START, 'd')
    assert [r['name'] for r in report] == ['Daily cap again'] and 'same line as one of “Daily cap”' in report[0]['reason']
    # A person's own line of the same measure outranks the line for everyone only within its own rule (as before).
    proposals, report = plan([rules[0], rules[4]], {}, WEEK_START, 'd')
    assert [r['name'] for r in report] == ['Kopofiku daily cap'] and 'unresolved outranking' in report[0]['reason']


@pytest.fixture(scope='module')
def department_catalogue_migration():
    """960 per-person weekly targets (160 people x 6 weeks, a line each) all move into one catalogue rule: a rule may
    take over as many rules as it has lines, and planning stays fast (it used to keep 460 of them out, and took minutes
    retrying one rule at a time)."""
    import time
    from datetime import date, timedelta
    people = [str(uuid.uuid4()) for _ in range(160)]
    weeks = [(date(2026, 8, 23) + timedelta(days=7 * n)).isoformat() for n in range(7)]
    rules = [rule(f'Resident {k:03} weekly target {weeks[n]}', quota(weeks[n], weeks[n + 1], 6 + (n + k) % 2), {'person_id': p},
                  weeks[n], weeks[n + 1], 'advisory') for k, p in enumerate(people) for n in range(6)]
    began = time.process_time()
    proposals, report = plan(rules, {}, WEEK_START, 'd')
    elapsed = time.process_time() - began
    return rules, proposals, report, elapsed


def test_department_scale_one_entry_takes_every_rule_quickly(department_catalogue_migration):
    rules, proposals, report, _ = department_catalogue_migration
    assert report == [] and [len(x['rule_ids']) for x in proposals] == [960] and same(rules, proposals)
    assert len(proposals[0]['payload']['definition']['merged_ids']) == 960


@pytest.mark.cpu_budget
def test_department_scale_one_entry_takes_every_rule_quickly_cpu_budget(department_catalogue_migration):
    import coverage
    _, _, _, elapsed = department_catalogue_migration
    budget = 60 if coverage.Coverage.current() else 15  # ~2 s on a developer machine; it was ~150 s
    assert elapsed < budget, (elapsed, budget)


def test_a_waiting_proposal_holds_its_entry_and_its_rules():
    cap = rule('Cap', {'kind': 'max_duties', 'maximum': 5})
    waiting = {**rule('Duties per week', {'kind': 'max_duties', 'maximum': 4}), 'kind': 'catalogue', 'status': 'proposed',
               'definition': {'rule': {'kind': 'catalogue', 'id': 'duties_per_week', 'lines': []}}}
    _, report = plan([cap, waiting], {}, WEEK_START, 'd')
    assert 'waiting for approval' in report[0]['reason']


# Property: random rule sets (fixed seeds) move unchanged or are reported, never silently different ----------------------

PEOPLE, CLINICS, PROGRAMMES = list(PERSON.values()), list(SERVICE.values()), list(PROGRAMME.values())


def random_rule(rnd):
    def scope():
        s = {}
        if rnd.random() < 0.3:
            s['person_id'] = rnd.choice(PEOPLE)
        elif rnd.random() < 0.3:
            s['programme_id'] = rnd.choice(PROGRAMMES)
        if rnd.random() < 0.4:
            s['service_ids'] = rnd.sample(CLINICS, rnd.randint(1, 3))
        if rnd.random() < 0.2:
            s['weekdays'] = sorted(rnd.sample(range(7), rnd.randint(1, 6)))
        if rnd.random() < 0.15:
            s['conditions'] = {'match': 'all', 'conditions': [{'field': rnd.choice(['tags', 'programme_ids']), 'values': ['vip'] if rnd.random() < .5 else [rnd.choice(PROGRAMMES)]}]}
        return s
    severity = rnd.choice(['mandatory', 'advisory'])
    start = rnd.choice(['2026-08-23', '2026-08-30', '2026-09-06'])
    end = rnd.choice(['2026-11-01', '2026-12-06', None])
    kinds = [
        lambda: {'kind': 'max_duties', 'maximum': rnd.randint(1, 6), 'window': rnd.choice(['week', 'rolling', 'day'])},
        lambda: {'kind': 'max_hours', 'hours': rnd.choice([20, 40, 48])},
        lambda: quota(start, '2026-11-01', rnd.randint(0, 3), rnd.choice(['maximum', 'minimum', 'range']), rnd.choice(['duties', 'session_units', 'scheduled_hours']),
                      **({'maximum_amount': 5} if rnd.random() < 2 else {}), window_alignment=rnd.choice(['calendar_week', 'calendar_month', 'rolling'])),
        lambda: {'kind': 'min_rest', 'hours': rnd.choice([8, 11])},
        lambda: {'kind': 'minimum_cover', 'minimum': rnd.randint(1, 3)},
        lambda: {'kind': 'maximum_participants', 'maximum': rnd.randint(1, 4)},
        lambda: {'kind': 'restriction', 'weekdays': sorted(rnd.sample(range(7), 2))},
        lambda: {'kind': 'allocation_preference', 'priority': rnd.randint(-3, 3), 'session_priority': rnd.choice([0, 2])},
        lambda: {'kind': 'max_consecutive_days', 'maximum': rnd.randint(3, 6)},
        lambda: {'kind': 'run_length', 'minimum': 2, 'maximum': rnd.randint(3, 6)},
    ]
    body = rnd.choice(kinds)()
    if body['kind'] == 'block_quota':
        if body['comparison'] != 'range':
            body.pop('maximum_amount', None)
        if body['window_alignment'] == 'rolling' and rnd.random() < .5:
            body['window_days'] = 7
        if rnd.random() < .3 and body['comparison'] != 'target':
            low = body['amount'] if body['comparison'] != 'maximum' else 0
            high = body.get('maximum_amount', body['amount']) if body['comparison'] != 'minimum' else low + 2
            body.update(target_amount=rnd.randint(int(low), int(high)), target_strength=rnd.choice(['low', 'high']))
    if body['kind'] in ('allocation_preference', 'run_length'):
        severity = 'advisory'
    extra = {}
    if rnd.random() < 0.35 and body['kind'] in ('max_duties', 'max_hours', 'block_quota', 'min_rest', 'restriction', 'allocation_preference', 'max_consecutive_days'):
        rows = []
        for person in rnd.sample(PEOPLE, rnd.randint(1, 3)):
            row = {'person_id': person}
            if body['kind'] not in ('restriction',):
                row['amount'] = rnd.randint(1, 4) if body['kind'] != 'allocation_preference' else rnd.randint(-2, 2)
            if rnd.random() < .3:
                row['starts_on'], row['ends_on'] = '2026-09-06', '2026-10-04'
            if rnd.random() < .15:
                row['weekdays'] = [rnd.randint(0, 6)]
            rows.append(row)
        extra['per_person'] = rows
    s = scope()
    if extra.get('per_person'):
        s.pop('person_id', None)
    return rule(f'Rule {rnd.randint(0, 10**6)}', body, s, start, end, severity, **extra)


def valid(r):
    try:
        PolicyDefinition.model_validate(r['definition'])
        return r['ends_on'] is not None or r['definition']['rule']['kind'] != 'block_quota'
    except ValidationError:
        return False


@pytest.mark.parametrize('seed', range(10))
def test_random_rule_sets_move_unchanged_or_are_reported(seed):
    rnd = random.Random(seed)
    moved = reported = 0
    for _ in range(50):  # 10 seeds x 50 sets = 500 cases
        rules = [r for r in (random_rule(rnd) for _ in range(rnd.randint(1, 5))) if valid(r)]
        if not rules:
            continue
        proposals, report = plan(rules, {}, WEEK_START, 'd')
        assert {i for x in proposals for i in x['rule_ids']} | {r['rule_id'] for r in report} == {r['id'] for r in rules}
        assert same(rules, proposals), (seed, rules, report)
        assert not [x for x in proposals if duplicates(x['payload']['definition']['rule'])], (seed, rules)  # never refused as duplicates
        moved += sum(len(x['rule_ids']) for x in proposals)
        reported += len(report)
    assert moved > reported  # most rules move; the rest are named


# Through the API: preview, propose, approve, the same checks, undo ------------------------------------------------------

def test_catalogue_solve_proof_keeps_each_greedy_run_within_ninety_seconds(h, monkeypatch):
    from apps.api.auth import Actor
    from apps.api.services.rule_catalogue import _solve_proof
    from packages.domain import cp_sat, generation

    workspace = h.workspace()
    calls = []
    def greedy(snapshot, time_budget_seconds=None):
        calls.append(time_budget_seconds)
        assert time_budget_seconds == 90
        return {'status': 'candidate_feasible', 'not_applied': GREEDY_NOT_APPLIED}
    monkeypatch.setattr(generation, 'generate', greedy)
    monkeypatch.setattr(cp_sat, 'generate', lambda *args, **kwargs: {'status': 'candidate_feasible'})
    actor = Actor(h.seed['identities']['coordinator'], h.seed['department_id'], h.seed['organisation_id'],
                  'coordinator', frozenset(['coordinator']), None, 'test')
    proof = {'identical': True, 'rotas': [{'workspace_id': workspace['id']}]}
    with transaction(h.app.state.engine) as db:
        _solve_proof(db, actor, [], proof)
    assert calls == [90] * 4 and proof['identical']


def test_migration_through_governance_checks_the_same_and_undoes(h):
    import json
    from apps.api.services.rule_catalogue import migration_plan, migration_proof
    from apps.api.auth import Actor
    from .test_staff_wishes import wish
    w, old, _ = world(h)
    a, b, c = h.seed['people']
    # Six dated wishes fill the 1 + 2 + 3 duty caps without interchangeable dates in the refill.
    for person, day in ((a, 6), (b, 5), (b, 7), (c, 4), (c, 5), (c, 8)):
        wish(h, f'2026-10-{day:02}', kind='prefer_on', points=1, person=person, role='coordinator')
    before = evaluate(h.snapshot(w))
    with transaction(h.app.state.engine) as db:
        actor = Actor(h.seed['identities']['coordinator'], h.seed['department_id'], h.seed['organisation_id'], 'coordinator',
                      frozenset(['coordinator']), None, 'test')
        preview = migration_plan(db, actor)
        proof = migration_proof(db, actor, preview, model=True, solve=True)
    assert preview['moving'] == len(old) and preview['report'] == []
    assert proof['identical'] and proof['rotas'] and all(
        not r['findings_only_before'] and not r['findings_only_after'] and r['quota_equal']
        and r['score_before'] == r['score_after'] and r['model_equal']
        and r['cp_sat_equal'] and r['greedy_equal'] for r in proof['rotas']), json.dumps(proof, indent=2, sort_keys=True)
    # A real schedule is compared: the rota scheduled again from empty fills places, the same under both sets of rules.
    [rota] = proof['rotas']
    assert rota['greedy_not_applied'] == GREEDY_NOT_APPLIED
    assert rota['refill_greedy_not_applied'] == GREEDY_NOT_APPLIED
    assert rota['refill_status'] in ('candidate_feasible', 'candidate_with_gaps') and rota['refill_assignments'] > 0 and rota['refill_equal']
    assert rota['refill_greedy_equal'] and rota['refill_greedy_assignments'] > 0
    result = h.command('coordinator', 'rule.migrate_to_catalogue', {'reason': 'Rules as catalogue lines'})
    assert len(result['records']) == len(preview['proposals']) and all(r['status'] == 'proposed' for r in result['records'])
    h.command('coordinator', 'rule.migrate_to_catalogue', {'reason': 'Twice'}, status=409)  # one waiting proposal per rule
    assert evaluate(h.snapshot(w))['findings'] == before['findings']  # nothing applies until approved
    for record in result['records']:
        activate(h, record)
    # TEST-STALE: catalogue migration is a reviewed non-safety rule change. Refresh the existing
    # draft through the freeze-matrix path before comparing its post-activation inputs
    # (425d832da/6ef713812); the strict proof above already covers findings, quota,
    # score and model equivalence, including the BALANCE_GOAL diagnostic-id fix.
    w = h.refresh_draft_baseline(h.detail(w)['workspace'])
    now = evaluate(h.snapshot(w))
    moved = {i: r['id'] for r in result['records'] for i in r['definition']['merged_ids']}
    assert sorted(rule_groups.mapped_citation_finding_key(f, moved) for f in before['findings']) == sorted(rule_groups.mapped_citation_finding_key(f, {}) for f in now['findings'])
    with transaction(h.app.state.engine) as db:
        assert {p.id for p in db.scalars(select(m.Policy).where(m.Policy.status == 'active'))} == {r['id'] for r in result['records']}
    for record in result['records']:  # undo: retiring a catalogue rule brings back the rules it took over
        live = next(p for p in h.get('approver', '/api/policies?status=active')['policies'] if p['id'] == record['id'])
        restored = h.command('approver', 'policy.retire', {'reason': 'Undo the catalogue'}, live['id'], live['revision'])['restored_ids']
        assert sorted(restored) == sorted(record['definition']['merged_ids'])
    # TEST-STALE: undo retires the converted non-safety rules; review that retirement into the draft too.
    w = h.refresh_draft_baseline(h.detail(w)['workspace'])
    assert evaluate(h.snapshot(w))['findings'] == before['findings']


def test_the_script_dry_run_and_baseline(h, monkeypatch, capsys):
    import scripts.catalogue_rules as tool
    world(h)
    args = SimpleNamespace(who='coordinator', department=None, propose=False, activate=False, undo=False, baseline=False, only=[],
                           reason='', only_reason=None, solve=False, no_model=False, acknowledge_unresolved=False, json=False)
    with transaction(h.app.state.engine) as db:
        original = {p.id for p in db.scalars(select(m.Policy).where(m.Policy.status == 'active'))}
        tool.preview(db, tool.actor_for(db, h.seed['identities']['coordinator']), args)
    out = capsys.readouterr().out
    assert '6 rules in force: 6 move into' in out and 'IDENTICAL' in out
    monkeypatch.setattr(tool, 'baseline_refusals', lambda db: {})
    tool.baseline(h.app.state.engine, SimpleNamespace(**{**vars(args), 'baseline': True, 'propose': True, 'reason': 'Baseline rehearsal'}))
    with transaction(h.app.state.engine) as db:
        made = [p for p in db.scalars(select(m.Policy).where(m.Policy.status == 'active'))]
        assert made and all(p.kind == 'catalogue' for p in made)
        assert all(db.get(m.Identity, p.created_by).disabled and db.get(m.Identity, p.approved_by).disabled for p in made)
    tool.baseline(h.app.state.engine, SimpleNamespace(**{**vars(args), 'baseline': True, 'undo': True, 'reason': 'Undo the rehearsal',
                                                         'only_reason': 'Baseline rehearsal'}))
    with transaction(h.app.state.engine) as db:
        assert {p.id for p in db.scalars(select(m.Policy).where(m.Policy.status == 'active'))} == original
