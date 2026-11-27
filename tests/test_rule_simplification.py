"""One rule for similar rules (docs/design/rule-simplification.md): the planner joins every family of near-duplicate
rules found in production into one rule, and a joined rule checks exactly what the rules it replaces checked —
validator findings, quota rows, the rules the scheduler reads and the CP-SAT model itself."""
import copy
import itertools
import re
from collections import Counter

import pytest
from pydantic import ValidationError

from packages.contracts.schemas import PolicyDefinition
from packages.domain.conditions import active_rules
from packages.domain.rule_groups import canonical_model, expanded, plan, same_model
from packages.domain.validation import evaluate
from .test_rule_templates import activate, propose

ids = (f'00000000-0000-4000-8000-{n:012d}' for n in itertools.count(1))
PERSON = {name: next(ids) for name in ('Kopofiku', 'Badr', 'Carla', 'Quartz', 'Emad')}
PROGRAMME = {name: next(ids) for name in ('Residency', 'Visiting rotation', 'Internship')}
SERVICE = {name: next(ids) for name in ('Physical UCC', 'Virtual UCC', 'Clinic A', 'Clinic B')}
TYPE = {name: next(ids) for name in ('Day shift', 'Evening shift')}
LABELS = {'people': {v: k for k, v in PERSON.items()}, 'programmes': {v: k for k, v in PROGRAMME.items()},
          'services': {v: k for k, v in SERVICE.items()}}
WEEK_START = 6  # Sunday, as the department's calendar


def configure_baseline_rehearsal(monkeypatch, tool):
    """Exercise CP-SAT with the real rota guard and the harness's seeded access."""
    monkeypatch.setenv('ROTA_GENERATION_ENGINE', 'cp_sat')
    baseline_refusals = tool.baseline_refusals
    fixture_access = {'enabled sign-ins', 'roles', 'accounts', 'open invitations'}

    def refusals(db):
        return {reason: count for reason, count in baseline_refusals(db).items() if reason not in fixture_access}

    monkeypatch.setattr(tool, 'baseline_refusals', refusals)


def rule(name, kind_rule, scope=None, starts_on='2026-08-23', ends_on='2027-01-03', severity='mandatory', status='active', **definition):
    return {'id': next(ids), 'name': name, 'kind': kind_rule['kind'], 'status': status, 'active': status == 'active',
            'owner': 'Departmental review pending', 'source': 'Synthetic copy of a production family', 'department_id': 'd',
            'starts_on': starts_on, 'ends_on': ends_on, 'revision': 1,
            'definition': {'rule': kind_rule, 'scope': scope or {}, 'severity': severity, **definition}}


def quota(start, end, amount, comparison='target', metric='session_units', **extra):
    return {'kind': 'block_quota', 'starts_on': start, 'ends_on': end, 'metric': metric, 'comparison': comparison, 'amount': amount, **extra}


def weeks(first, count):
    from datetime import date, timedelta
    start = date.fromisoformat(first)
    return [((start + timedelta(days=7 * i)).isoformat(), (start + timedelta(days=7 * i + 7)).isoformat()) for i in range(count)]


def production_families():
    """Families of near-duplicate rules, with synthetic ids."""
    clinics = list(SERVICE.values())
    rules = []
    # Weekly clinical target: per person and week, sometimes several people in one copy, for two programmes.
    for n, (s, e) in enumerate(weeks('2026-08-23', 6)):
        rules.append(rule(f"{'Kopofiku'} - Weekly clinical target - {s}", quota(s, e, 7), {'person_id': PERSON['Kopofiku'], 'programme_id': PROGRAMME['Residency'],
                          'service_ids': clinics}, s, e, 'advisory'))
        rules.append(rule('Weekly clinical target', quota(s, e, 6 if n % 2 else 7), {'person_ids': [PERSON['Badr'], PERSON['Carla']],
                          'programme_id': PROGRAMME['Residency'], 'service_ids': clinics}, s, e, 'advisory'))
    rules += [rule('Quartz · Weekly clinical target', quota(s, e, 7), {'person_id': PERSON['Quartz'], 'programme_id': PROGRAMME['Visiting rotation'],
                   'service_ids': clinics}, s, e, 'advisory') for s, e in weeks('2026-12-20', 2)]
    # Residents weekly UCC ceiling: one copy per week for the programme (a series).
    rules += [rule(f'Residents weekly UCC ceiling - {s}', quota(s, e, 1, 'maximum', 'duties'),
                   {'programme_id': PROGRAMME['Residency'], 'service_ids': clinics[:2]}, s, e) for s, e in weeks('2026-08-23', 19)]
    # Internship weekly requirement: a partial first week (starts on a Tuesday), then whole weeks.
    rules.append(rule('Internship weekly urgent-care requirement - 2026-09-01', quota('2026-09-01', '2026-09-06', 1, 'minimum', 'duties'),
                      {'programme_id': PROGRAMME['Internship'], 'service_ids': clinics[:2]}, '2026-09-01', '2026-09-06'))
    rules += [rule(f'Internship weekly urgent-care requirement - {s}', quota(s, e, 1, 'minimum', 'duties'),
                   {'programme_id': PROGRAMME['Internship'], 'service_ids': clinics[:2]}, s, e) for s, e in weeks('2026-09-06', 3)]
    # Residents protected academic day: a weekly copy of one restriction (Thursdays).
    rules += [rule(f'Residents protected academic day - {s}', {'kind': 'restriction'}, {'programme_id': PROGRAMME['Residency'],
                   'service_ids': clinics, 'weekdays': [3]}, s, e) for s, e in weeks('2026-08-23', 5)]
    # First-week UCC restriction: per person, different weeks.
    rules += [rule(f'{who} first-week UCC restriction', {'kind': 'restriction'}, {'person_id': PERSON[who], 'service_ids': clinics[:2],
                   'programme_id': PROGRAMME['Visiting rotation']}, s, e) for who, (s, e) in zip(('Kopofiku', 'Quartz', 'Emad'), weeks('2026-08-23', 3))]
    # No virtual clinics: one copy per clinic for one programme, one per programme for the others.
    rules += [rule(f'Medical student rotation · No virtual clinics · {n}', {'kind': 'restriction'}, {'service_id': s, 'programme_id': PROGRAMME['Internship']},
                   '2026-08-23', '2026-09-18') for n, s in enumerate(clinics[1:3], 1)]
    rules += [rule(f'{p} - Excluded weekdays', {'kind': 'restriction'}, {'programme_id': PROGRAMME[p], 'weekdays': [4, 5]})
              for p in ('Residency', 'Visiting rotation')]
    # UCC overtime slots: per person, duty type and weekday, requiring that person's monthly caps.
    caps = {who: [rule(f'{who} · UCC overtime cap · 2026-{m:02d}', quota(f'2026-{m:02d}-01', f'2026-{m + 1:02d}-01', 3, 'maximum', 'duties', overtime='only'),
                       {'person_id': PERSON[who], 'service_ids': clinics[:2]}, f'2026-{m:02d}-01', f'2026-{m + 1:02d}-01') for m in (10, 11)]
            for who in ('Kopofiku', 'Badr')}
    rules += [c for cs in caps.values() for c in cs]
    for who, slots in (('Kopofiku', ((TYPE['Day shift'], 4), (TYPE['Day shift'], 5), (TYPE['Evening shift'], 4))), ('Badr', ((TYPE['Evening shift'], 1),))):
        rules += [rule(f'{who} · UCC overtime slot · {n}', {'kind': 'allocation_overtime', 'required_policy_ids': [c['id'] for c in caps[who]]},
                       {'person_id': PERSON[who], 'service_ids': clinics[:2], 'assignment_type_id': t, 'weekdays': [d]}, '2026-10-01', '2026-12-01', 'advisory')
                  for n, (t, d) in enumerate(slots, 1)]
    # UCC staffing: the same floor for the same ranks on different sessions.
    rank = [next(ids)]
    rules += [rule(f'UCC {when} {mode} senior floor', {'kind': 'minimum_cover', 'minimum': minimum},
                   {'category_ids': rank, 'service_ids': [SERVICE[mode]], 'assignment_type_id': TYPE[shift], 'weekdays': days})
              for when, mode, shift, days, minimum in (('weekday morning', 'Physical UCC', 'Day shift', [0, 1, 2, 3, 6], 2),
                                                       ('weekday evening', 'Physical UCC', 'Evening shift', [0, 1, 2, 3, 6], 1),
                                                       ('weekend', 'Physical UCC', 'Day shift', [4, 5], 1))]
    # Preferred UCC session: a lower priority per person and duty type.
    rules += [rule(f'{who} · Preferred UCC session', {'kind': 'allocation_preference', 'priority': -1}, {'person_id': PERSON[who],
                   'assignment_type_id': t}, severity='advisory') for who, t in (('Carla', TYPE['Day shift']), ('Quartz', TYPE['Evening shift']))]
    return rules


def joined(groups):
    return {g['name']: g for g in groups if 'payload' in g}


def test_every_production_family_becomes_one_rule_that_checks_the_same():
    rules = production_families()
    groups = plan(rules, LABELS, WEEK_START)
    made = joined(groups)
    assert not [g for g in groups if 'problem' in g]
    # One rule per family, named without the people, programmes or dates.
    assert sorted(made) == sorted(['Weekly clinical target', 'Residents weekly UCC ceiling', 'Internship weekly urgent-care requirement',
                                   'Residents protected academic day', 'Exclusions',
                                   'UCC overtime cap', 'UCC overtime slot', 'UCC senior floor', 'Preferred UCC session'])
    assert sum(len(g['rule_ids']) for g in made.values()) == len(rules)
    target = made['Weekly clinical target']['payload']['definition']
    # Weekly blocks read as one weekly rule; each person's run of weeks is one line; the programme is on the line.
    assert target['listed_only'] and target['rule']['window_alignment'] == 'calendar_week'
    # Badr and Carla alternate 7 and 6 each week: a line per week; Kopofiku's six weeks at 7 are one line.
    assert Counter(r['person_id'] for r in target['per_person']) == Counter({PERSON['Kopofiku']: 1, PERSON['Carla']: 6, PERSON['Badr']: 6, PERSON['Quartz']: 1})
    assert {r.get('programme_id') for r in target['per_person']} == {None, PROGRAMME['Visiting rotation']} or \
        {r.get('programme_id') for r in target['per_person']} == {PROGRAMME['Residency'], PROGRAMME['Visiting rotation']}
    ceiling = made['Residents weekly UCC ceiling']['payload']
    assert ceiling['definition']['rule']['window_alignment'] == 'calendar_week' and not ceiling['definition']['per_person']
    assert (ceiling['starts_on'], ceiling['ends_on']) == ('2026-08-23', '2027-01-03')
    assert made['Internship weekly urgent-care requirement']['payload']['definition']['rule']['window_alignment'] == 'calendar_week'
    assert made['Residents protected academic day']['shape'] == 'series'
    # Restrictions (not protected time) are one table of exclusions (phase 2): the first-week restrictions, the virtual
    # clinics joined per place, and the excluded weekdays per programme are its lines.
    lines = made['Exclusions']['payload']['definition']['per_person']
    assert {tuple(r['service_ids']) for r in lines if r.get('programme_id') == PROGRAMME['Internship']} == {tuple(list(SERVICE.values())[1:3])}
    assert {r['programme_id'] for r in lines if r.get('weekdays') == [4, 5]} == {PROGRAMME['Residency'], PROGRAMME['Visiting rotation']}
    overtime = made['UCC overtime slot']
    assert overtime['lines'] == 3 and overtime['people'] == 2 and not overtime['switched_off']  # Kopofiku's two Day-shift days are one line
    floor = made['UCC senior floor']['payload']['definition']
    assert [r.get('amount') for r in floor['per_person'] if r.get('amount') is not None] == [2] and floor['rule']['minimum'] == 1
    assert made['Preferred UCC session']['payload']['definition']['rule']['priority'] == -1
    # The proof: the checks read exactly the same rules, every copy, person, clinic and day.
    id_map = {i: n for n, g in enumerate(made.values()) for i in g['rule_ids']}
    after = [{**g['payload'], 'id': n, 'status': 'active', 'active': True, 'kind': g['kind'], 'department_id': 'd'} for n, g in enumerate(made.values())]
    assert expanded(rules, id_map, WEEK_START) == expanded(after, None, WEEK_START)


def test_rules_that_differ_in_meaning_stay_apart():
    clinics = list(SERVICE.values())
    base = lambda name, severity='mandatory', minimum=1, days=(0, 1): rule(name, {'kind': 'minimum_cover', 'minimum': minimum},
                                                                          {'service_ids': clinics[:1], 'weekdays': list(days)}, severity=severity)
    # Different severity, and two floors on the same sessions, never become one rule.
    assert not joined(plan([base('A floor'), base('A target', 'advisory')], LABELS, WEEK_START))
    assert not joined(plan([base('A floor'), base('B floor', minimum=2)], LABELS, WEEK_START))
    # Weekly copies with a gap stay two runs; a quota with reviewed exceptions keeps its own blocks.
    gap = [rule(f'Ceiling - {s}', quota(s, e, 1, 'maximum', 'duties'), {'programme_id': PROGRAMME['Residency']}, s, e)
           for s, e in weeks('2026-08-23', 2) + weeks('2026-09-20', 2)]
    [runs] = [g for g in plan(gap, LABELS, WEEK_START) if 'payload' in g]  # one rule, a line per run of weeks
    assert [(r.get('starts_on'), r.get('ends_on')) for r in runs['payload']['definition']['per_person']] == [(None, '2026-09-06'), ('2026-09-20', None)]
    assert expanded(gap, {r['id']: 'G' for r in gap}, WEEK_START) == expanded([{**runs['payload'], 'id': 'G', 'status': 'active', 'kind': 'block_quota', 'department_id': 'd'}], None, WEEK_START)
    # Quotas that allow reviewed exceptions keep their exact blocks (a line each); a rule with an exception stays apart.
    excepted = [{**r, 'definition': {**r['definition'], 'exception_allowed': True}} for r in gap[:2]]
    [kept] = joined(plan(excepted, LABELS, WEEK_START)).values()
    assert kept['payload']['definition']['rule']['window_alignment'] == 'rolling' and kept['lines'] == 2
    assert not joined(plan(excepted, LABELS, WEEK_START, held={excepted[0]['id']}))
    # A rule waiting in a proposed group is held back; retired rules are never grouped.
    first = plan(gap[:2], LABELS, WEEK_START)[0]
    waiting = {**first['payload'], 'id': next(ids), 'status': 'proposed', 'kind': 'block_quota'}
    assert not joined(plan(gap[:2] + [waiting], LABELS, WEEK_START))
    assert not joined(plan([{**r, 'status': 'retired', 'active': False} for r in gap[:2]], LABELS, WEEK_START))


def test_overtime_rules_switched_on_and_off_never_share_one_rule():
    cap = rule('Cap', quota('2026-10-01', '2026-11-01', 3, 'maximum', 'duties'), {'person_id': PERSON['Kopofiku']})
    retired = {**rule('Old cap', quota('2026-10-01', '2026-11-01', 3, 'maximum', 'duties'), {'person_id': PERSON['Badr']}), 'status': 'retired', 'active': False}
    slot = lambda who, needs: rule(f'{who} · UCC overtime slot', {'kind': 'allocation_overtime', 'required_policy_ids': [needs['id']]},
                                   {'person_id': PERSON[who]}, severity='advisory')
    on, off, off2 = slot('Kopofiku', cap), slot('Badr', retired), slot('Carla', retired)
    groups = joined(plan([cap, retired, on, off, off2], LABELS, WEEK_START))
    assert [g['rule_ids'] for g in groups.values()] == [[off['id'], off2['id']]] and groups['UCC overtime slot']['switched_off']


def test_a_grouped_limit_keeps_overtime_offered():
    """Overtime needs its required limits in force; a limit grouped into an active rule still is."""
    from packages.domain.generation import allocation_settings
    caps = [rule(f'Kopofiku · UCC overtime cap · {s}', quota(s, e, 3, 'maximum', 'duties'), {'person_id': PERSON['Kopofiku']}, s, e)
            for s, e in (('2026-10-01', '2026-11-01'), ('2026-11-01', '2026-12-01'))]
    slot = rule('Kopofiku · UCC overtime slot', {'kind': 'allocation_overtime', 'required_policy_ids': [c['id'] for c in caps]}, {'person_id': PERSON['Kopofiku']},
                severity='advisory')
    [group] = [g for g in plan(caps, LABELS, WEEK_START) if 'payload' in g]
    grouped = {**group['payload'], 'id': next(ids), 'status': 'active', 'active': True, 'kind': 'block_quota'}
    shift = {'id': 's', 'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00', 'service_ids': [], 'location_id': None,
             'assignment_type_id': TYPE['Day shift'], 'participants': []}
    data = {'organisation': {'timezone': 'Asia/Riyadh'}, 'people': {PERSON['Kopofiku']: {}}, 'memberships': [], 'grants': [], 'policies': [],
            'types': {TYPE['Day shift']: {'department_id': 'd'}}}
    before = {**data, 'policies': active_rules(caps + [slot])}
    after = {**data, 'policies': active_rules([{**c, 'status': 'retired', 'active': False} for c in caps] + [grouped, slot])}
    assert allocation_settings(before, shift, PERSON['Kopofiku'])[1] is True
    assert allocation_settings(after, shift, PERSON['Kopofiku'])[1] is True


def test_line_contract():
    person, programme, service = PERSON['Kopofiku'], PROGRAMME['Residency'], SERVICE['Clinic A']
    cover = {'kind': 'minimum_cover', 'minimum': 2}
    assert PolicyDefinition.model_validate({'rule': cover, 'listed_only': True, 'per_person': [{'service_ids': [service], 'weekdays': [4, 5], 'amount': 1}]})
    # A person on a staffing level's line is who counts there (a clinic placement; phase 2).
    assert PolicyDefinition.model_validate({'rule': cover, 'listed_only': True, 'per_person': [{'person_id': person}]})
    with pytest.raises(ValidationError, match='people only'):  # a rule for everyone its conditions match takes person lines only
        PolicyDefinition.model_validate({'rule': {'kind': 'max_hours', 'hours': 40}, 'per_person': [{'programme_id': programme}]})
    with pytest.raises(ValidationError, match='Add at least one line'):
        PolicyDefinition.model_validate({'rule': {'kind': 'restriction'}, 'listed_only': True})
    with pytest.raises(ValidationError, match='same people and dates'):
        PolicyDefinition.model_validate({'rule': quota('2026-10-01', '2026-11-01', 3, 'maximum'), 'listed_only': True,
                                         'per_person': [{'person_id': person}, {'person_id': person, 'amount': 2}]})
    with pytest.raises(ValidationError, match='already names a programme'):
        PolicyDefinition.model_validate({'rule': {'kind': 'restriction'}, 'scope': {'programme_id': programme}, 'listed_only': True,
                                         'per_person': [{'programme_id': programme}]})
    with pytest.raises(ValidationError, match='both a start and an end'):
        PolicyDefinition.model_validate({'rule': {'kind': 'restriction'}, 'listed_only': True, 'per_person': [{'start_time': '08:00'}]})
    # A person and a programme on one line: that person while in that programme.
    assert PolicyDefinition.model_validate({'rule': quota('2026-10-01', '2026-11-01', 3, 'maximum'), 'listed_only': True,
                                            'per_person': [{'person_id': person, 'programme_id': programme}]})


def test_a_line_is_a_narrower_copy_of_the_rule():
    person, programme, service = PERSON['Kopofiku'], PROGRAMME['Residency'], SERVICE['Clinic A']
    template = rule('Senior floor', {'kind': 'minimum_cover', 'minimum': 2}, {'category_ids': [PERSON['Emad']]}, listed_only=True, per_person=[
        {'service_ids': [service], 'weekdays': [4, 5], 'start_time': '08:00', 'end_time': '16:00', 'amount': 1},
        {'programme_id': programme, 'assignment_type_ids': [TYPE['Day shift']]}])
    weekend, day = active_rules([template])
    assert weekend['id'] == day['id'] == template['id']  # findings still name the one rule
    assert (weekend['definition']['rule']['minimum'], weekend['definition']['scope']['service_ids'], weekend['definition']['scope']['weekdays'],
            weekend['definition']['scope']['start_time']) == (1, [service], [4, 5], '08:00')
    assert day['definition']['rule']['minimum'] == 2 and day['definition']['scope']['programme_id'] == programme
    assert active_rules([weekend, day]) == [weekend, day]  # idempotent
    with_person = rule('Cap', quota('2026-10-01', '2026-11-01', 3, 'maximum'), listed_only=True, per_person=[{'person_id': person, 'amount': 4}])
    [only] = active_rules([with_person])  # nobody else: the rule is its lines
    assert only['template_line']['person_id'] == person and only['definition']['rule']['amount'] == 4


# End to end through the API: preview, propose, approve, same checks, undo -------------------------------------------------

def test_grouping_through_governance_checks_the_same_and_undoes(h):
    a, b, c = h.seed['people']
    names = {p['id']: p['display_name'] for p in h.get('coordinator', '/api/catalogues')['people']}
    residency = h.configure('programmes', {'name': 'Synthetic residency', 'starts_on': '2026-01-01'})['record']
    for person in (a, b):
        h.configure('programme_memberships', {'person_id': person, 'programme_id': residency['id'], 'starts_on': '2026-09-01',
                                              'ends_on': '2026-12-01'}, role='coordinator')
    service = h.seed['services'][0]
    made = []
    for who, amount in ((a, 1), (b, 2)):  # a weekly per-person quota: one copy per person and week
        for s, e in weeks('2026-09-27', 3):
            made.append(activate(h, propose(h, quota(s, e, amount, 'maximum', 'duties'), {'person_id': who}, name=f'{names[who]} - Weekly duty cap - {s}',
                                            starts_on=s, ends_on=e)))
    for s, e in weeks('2026-09-27', 3):  # a weekly programme restriction: one copy per week
        made.append(activate(h, propose(h, {'kind': 'restriction'}, {'programme_id': residency['id'], 'weekdays': [0]},
                                        name=f'Residents protected academic day - {s}', starts_on=s, ends_on=e)))
    for who, weekday in ((a, 1), (c, 2)):  # per-person restriction on one weekday
        made.append(activate(h, propose(h, {'kind': 'restriction'}, {'person_id': who, 'weekdays': [weekday]}, name=f'{names[who]} · No clinic day')))
    w = h.workspace(start='2026-10-04', end='2026-10-11')
    for day in ('04', '05', '06', '07'):
        w = h.shift(w, f'2026-10-{day}T08:00:00+03:00', f'2026-10-{day}T12:00:00+03:00', people=[a, b, c],
                    seed_restricted=day != '04')['workspace']
    before = h.snapshot(w)
    checked = evaluate(before)
    assert checked['findings'] and checked['quota_report']['rows']

    preview = h.get('coordinator', '/api/policies/groups')
    assert h.get('coordinator', '/api/policies/groups') == preview  # a dry run saves nothing
    groups = [g for g in preview['groups'] if 'payload' in g]
    assert {g['name'] for g in groups} == {'Weekly duty cap', 'Residents protected academic day', 'No clinic day'}
    assert preview['active'] == len(made) and preview['after'] == 3
    academic = next(g for g in groups if g['name'] == 'Residents protected academic day')
    assert academic['calendar'] == {'entry_id': None, 'name': 'Residents protected academic day', 'programme_id': residency['id'],
                                    'starts_on': '2026-09-27', 'ends_on': '2026-10-18'}
    result = h.command('coordinator', 'policy.group', {'groups': [g['rule_ids'] for g in groups], 'reason': 'One rule for similar rules'})
    assert len(result['records']) == 3 and result['calendar_entries'] == 1
    entry = next(e for e in h.get('coordinator', '/api/catalogues')['programme_calendar_entries'] if e['name'] == 'Residents protected academic day')
    assert next(r for r in result['records'] if r['name'] == 'Residents protected academic day')['definition']['calendar_entry_id'] == entry['id']
    # Nothing changes until an approver activates.
    assert evaluate(h.snapshot(w))['findings'] == checked['findings']
    for record in result['records']:
        activate(h, record)
    # Draft rules stay captured until their baseline is explicitly refreshed.
    w = h.refresh_draft_baseline(h.detail(w)['workspace'])
    after = h.snapshot(w)
    id_map = {i: r['id'] for r in result['records'] for i in r['definition']['merged_ids']}
    assert len([p for p in after['policies'] if p['status'] == 'active']) == 4  # three grouped rules plus seeded Fair share
    from packages.domain.rule_groups import same_checks
    diff = same_checks(checked, evaluate(after), id_map)
    assert not diff['findings_only_before'] and not diff['findings_only_after'] and diff['quota_equal']
    assert expanded(before['policies'], id_map, WEEK_START) == expanded(after['policies'], None, WEEK_START)
    assert same_model(canonical_model(before), canonical_model(after), id_map)
    assert h.get('coordinator', '/api/policies/groups')['groups'] == []

    # Undo: retiring a grouped rule brings back exactly the rules it replaced.
    for record in result['records']:
        live = next(p for p in h.get('approver', '/api/policies?status=active')['policies'] if p['id'] == record['id'])
        restored = h.command('approver', 'policy.retire', {'reason': 'Undo the grouping'}, live['id'], live['revision'])['restored_ids']
        assert sorted(restored) == sorted(record['definition']['merged_ids'])
    active = {p['id']: p for p in h.get('approver', '/api/policies?status=active')['policies']}
    assert all(active[record['id']]['active'] for record in made)
    assert not {record['id'] for record in result['records']} & active.keys()
    w = h.refresh_draft_baseline(h.detail(w)['workspace'])
    assert evaluate(h.snapshot(w))['findings'] == checked['findings']


def test_grouping_refuses_a_stale_or_partial_selection(h):
    a, b, _ = h.seed['people']
    rules = [activate(h, propose(h, {'kind': 'max_hours', 'hours': hours}, {'person_id': who}, name='Weekly hours'))['id'] for who, hours in ((a, 40), (b, 48))]
    h.command('coordinator', 'policy.group', {'groups': [rules[:1]], 'reason': 'Only half of the group'}, status=409)
    h.command('coordinator', 'policy.group', {'groups': [rules, rules[:1]], 'reason': 'A rule twice'}, status=422)
