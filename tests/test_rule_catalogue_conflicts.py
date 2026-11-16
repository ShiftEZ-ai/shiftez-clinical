"""Conflicting lines end to end (docs/design/rule-catalogue.md §1, §5): what the check says about duplicates, overlaps
and warnings, that rule.propose refuses exactly what the check reports (422 the same line twice, 409 overlaps not
acknowledged), that approvers see the same on the waiting version, and that the validator and the scheduler apply the
line the check says wins."""
import copy
import uuid

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from packages.domain import cp_sat
from packages.domain.validation import evaluate


def line(**fields):
    return {'id': str(uuid.uuid4()), **fields}


def person(*people):
    return {'conditions': [{'field': 'person_ids', 'values': list(people)}]}


def rank(category):
    return {'conditions': [{'field': 'category_ids', 'values': [category]}]}


def most(n):
    return {'max': {'value': n}}


def check(h, catalogue_id, lines, role='coordinator'):
    response = h.client(role).post(f'/api/rules/catalogue/{catalogue_id}/check', json={'lines': lines})
    assert response.status_code == 200, response.text
    return response.json()


def propose(h, catalogue_id, lines, status=200, revision=None, acknowledged=(), policy_id=None):
    return h.command('coordinator', 'rule.propose', {'catalogue_id': catalogue_id, 'expected_policy_id': policy_id, 'expected_revision': revision, 'lines': lines,
                                                    'acknowledged_overlaps': list(acknowledged), 'reason': 'Conflict check'}, status=status)


def approve(h, record):
    return h.command('approver', 'policy.activate', {'reason': 'Independent review'}, record['id'], record['revision'])['record']


def tag_people(h, name, people):
    """A department tag labelling these people, as the people form saves it; its id."""
    for pid in people:
        p = next(x for x in h.get('admin', '/api/catalogues')['people'] if x['id'] == pid)
        h.configure('people', {k: p[k] for k in ('display_name', 'active', 'title', 'short_code', 'colour')} | {'tags': [name]}, p['id'], p['revision'])
    with transaction(h.app.state.engine) as db:
        return db.scalar(select(m.Tag.id).where(m.Tag.name == name))


def lines_for(h, case):
    """A case's lines on the demo department: people a, b, c, all of the demo rank; a tag naming a."""
    a, b, c = h.seed['people']
    category, clinic = h.seed['category'], {'conditions': [{'field': 'service_ids', 'values': [h.seed['services'][0]]}]}
    tag = tag_people(h, 'exceptional_trainee', [a]) if 'tag' in case else None
    return {
        'identical': ('duties_per_week', [line(numbers=most(5)), line(numbers=most(4))]),
        'other days': ('duties_per_week', [line(numbers=most(5), weekdays=[0, 1]), line(numbers=most(4), weekdays=[2, 3])]),
        'other hours': ('duties_per_week', [line(numbers=most(5), start_time='08:00', end_time='12:00'), line(numbers=most(4))]),
        'other dates': ('duties_per_week', [line(numbers=most(5), ends_on='2026-11-01'), line(numbers=most(4), starts_on='2026-11-02')]),
        'dates partly overlapping': ('duties_per_week', [line(numbers=most(5), ends_on='2026-11-02'), line(numbers=most(4), starts_on='2026-11-01')]),
        'other measure': ('duties_per_period', [line(numbers=most(5), settings={'max_duties': {}}),
                                                line(who=person(a), numbers=most(1), settings={'max_duties': {'window': 'day'}})]),
        'person over everyone': ('duties_per_week', [line(numbers=most(5)), line(who=person(a), numbers=most(3))]),
        'person over rank over everyone': ('duties_per_week', [line(numbers=most(5)), line(who=rank(category), numbers=most(4)),
                                                               line(who=person(a), numbers=most(3))]),
        'other where': ('duties_per_week', [line(numbers=most(5)), line(who=person(a), where=clinic, numbers=most(2))]),
        'tag and rank': ('duties_per_week', [line(who=rank(category), numbers=most(4)), line(who=person(tag), numbers=most(6))]),
        'person over tag': ('duties_per_week', [line(who=person(tag), numbers=most(4)), line(who=person(a), numbers=most(2))]),
        'migrated settings and a new line': ('duties_per_week', [
            line(numbers=most(5), settings={'max_duties': {'window': 'week', 'window_days': 7, 'include_overtime': True, 'group_by': 'all'}}),
            line(who=person(b), numbers=most(3))]),
        # Blank inherits, read on effective numbers: a "max 0" line takes everyone's min 2 (min above max) unless None.
        'inherits a conflict': ('duties_per_month', [line(numbers={'min': {'value': 2}, 'max': {'value': 5}}), line(who=person(a), numbers=most(0))]),
        'None removes the conflict': ('duties_per_month', [line(numbers={'min': {'value': 2}, 'max': {'value': 5}}),
                                                           line(who=person(a), numbers={'min': {'value': None}, 'max': {'value': 0}})]),
        'safety weaker': ('rest_between_duties', [line(numbers={'min': {'value': 11}}), line(who=rank(category), numbers={'min': {'value': 8}})]),
        'safety stricter': ('rest_between_duties', [line(numbers={'min': {'value': 11}}), line(who=person(a), numbers={'min': {'value': 12}})]),
        'safety cap weaker': ('people_per_session', [line(numbers=most(2)), line(who=rank(category), numbers=most(3))]),
        'safety minimum of a kind': ('people_per_session', [line(numbers={'min': {'value': 2}}), line(who=rank(category), numbers={'min': {'value': 1}})]),
    }[case]


# case: (duplicates, [(loser, [winners], people)], warnings by kind, line_overlaps {line: [(other, winner or None)]}, propose status)
CASES = {
    'identical': ([[0, 1]], [], [], {}, 422),
    'other days': ([], [], [], {}, 200),
    'other hours': ([], [], [], {}, 200),
    'other dates': ([], [], [], {}, 200),
    'dates partly overlapping': ([[0, 1]], [], [], {}, 422),
    'other measure': ([], [], [], {}, 200),
    'person over everyone': ([], [(0, [1], 1)], [], {0: [(1, 1)], 1: [(0, 1)]}, 409),
    'person over rank over everyone': ([], [(0, [1, 2], 3), (1, [2], 1)], [],
                                       {0: [(1, 1), (2, 2)], 1: [(0, 1), (2, 2)], 2: [(0, 2), (1, 2)]}, 409),
    'other where': ([], [], [], {}, 200),
    'tag and rank': ([], [], ['both'], {0: [(1, None)], 1: [(0, None)]}, 200),
    'person over tag': ([], [(0, [1], 1)], [], {0: [(1, 1)], 1: [(0, 1)]}, 409),
    'migrated settings and a new line': ([], [(0, [1], 1)], [], {0: [(1, 1)], 1: [(0, 1)]}, 409),
    'inherits a conflict': ([], [(0, [1], 1)], ['inherits'], {0: [(1, 1)], 1: [(0, 1)]}, 409),
    'None removes the conflict': ([], [(0, [1], 1)], [], {0: [(1, 1)], 1: [(0, 1)]}, 409),
    'safety weaker': ([], [], ['weaker'], {}, 200),
    'safety stricter': ([], [], [], {}, 200),
    'safety cap weaker': ([], [], ['weaker'], {}, 200),
    'safety minimum of a kind': ([], [], [], {}, 200),
}


@pytest.mark.parametrize('legacy_op,typed_op', [('any', 'is'), ('none', 'is_not'), ('any', 'any')])
@pytest.mark.parametrize('wrapped', [False, True])
def test_check_and_propose_refuse_mixed_format_duplicate_lines(h, legacy_op, typed_op, wrapped):
    person_id = h.seed['people'][0]
    operand = {'kind': 'absolute', 'value': person_id} if wrapped else person_id
    lines = [line(who={'conditions': [{'field': 'person_ids', 'op': legacy_op, 'values': [person_id]}]}, numbers=most(5)),
             line(who={'conditions': [{'field': 'person_ids', 'op': typed_op, 'value': operand}]}, numbers=most(4))]
    found = check(h, 'duties_per_week', lines)
    assert found['issues'] == []
    assert [duplicate['lines'] for duplicate in found['duplicates']] == [[0, 1]]
    refused = propose(h, 'duties_per_week', lines, status=422)
    assert refused['error']['code'] == 'DUPLICATE_LINES'


@pytest.mark.parametrize('case', list(CASES))
def test_the_check_and_propose_agree_case_by_case(h, case):
    entry, lines = lines_for(h, case)
    same, overlaps, warnings, line_overlaps, status = CASES[case]
    found = check(h, entry, lines)
    ids = [x['id'] for x in lines]
    assert found['issues'] == []
    assert [d['lines'] for d in found['duplicates']] == same
    assert [(o['line'], o['winners'], o['people']) for o in found['overlaps']] == overlaps
    assert [o['winner_ids'] for o in found['overlaps']] == [[ids[w] for w in o[1]] for o in overlaps]
    assert sorted(w['kind'] for w in found['warnings']) == warnings
    assert {ids.index(k): [(ids.index(o['line_id']), o['winner'] and ids.index(o['winner'])) for o in v] for k, v in found['line_overlaps'].items()} == {
        k: [(o, w) for o, w in v] for k, v in line_overlaps.items()}
    # rule.propose refuses exactly what the check reports, and the overlaps it names are the check's.
    refused = propose(h, entry, lines, status=status)
    if status == 422:
        assert refused['error']['code'] == 'DUPLICATE_LINES'
    if status == 409:
        assert refused['error']['code'] == 'OVERLAPS_UNACKNOWLEDGED'
        assert [o['line_id'] for o in refused['error']['details']] == [o['line_id'] for o in found['overlaps']]
        assert propose(h, entry, lines, acknowledged=[o['line_id'] for o in found['overlaps']][:-1], status=409)  # all of them, not some
        record = propose(h, entry, lines, acknowledged=[o['line_id'] for o in found['overlaps']])['record']
        assert record['status'] == 'proposed'


def test_approvers_see_the_check_of_the_waiting_version_and_the_page_reads_what_the_check_said(h):
    a, b, _ = h.seed['people']
    everyone, mine = line(numbers=most(5)), line(who=person(a), numbers=most(3))
    found = check(h, 'duties_per_week', [everyone, mine])
    first = propose(h, 'duties_per_week', [everyone, mine], acknowledged=[everyone['id']])['record']
    waiting = next(e for e in h.get('approver', '/api/rules/catalogue')['entries'] if e['id'] == 'duties_per_week')['proposed']
    assert waiting['check'] == {k: found[k] for k in ('duplicates', 'overlaps', 'warnings', 'issues', 'counting_attention')}
    live = approve(h, first)
    shown = next(e for e in h.get('auditor', '/api/rules/catalogue')['entries'] if e['id'] == 'duties_per_week')
    assert shown['line_overlaps'] == found['line_overlaps'] and shown['overlaps'] == 1
    # A version removing the person line has nothing to acknowledge; one adding a rank line names only the new loser.
    removed = propose(h, 'duties_per_week', [everyone], policy_id=live['id'], revision=live['revision'])['record']
    assert [x['id'] for x in removed['definition']['rule']['lines']] == [everyone['id']]
    h.command('coordinator', 'policy.decline', {'reason': 'Withdrawn with Undo by the person who proposed it'}, removed['id'], removed['revision'])
    ranked = line(who=rank(h.seed['category']), numbers=most(4))
    refused = propose(h, 'duties_per_week', [everyone, mine, ranked], policy_id=live['id'], revision=live['revision'], acknowledged=[everyone['id']], status=409)
    assert [o['line_id'] for o in refused['error']['details']] == [ranked['id']]
    shown = next(e for e in h.get('coordinator', '/api/rules/catalogue')['entries'] if e['id'] == 'duties_per_week')
    assert shown['line_overlaps'] == found['line_overlaps']  # the rule in force is unchanged


def test_the_validator_and_the_scheduler_apply_the_line_the_check_says_wins(h):
    a, b, c = h.seed['people']
    everyone, mine = line(numbers=most(1)), line(who=person(a), numbers=most(3))
    [overlap] = check(h, 'duties_per_week', [everyone, mine])['overlaps']
    assert overlap['line_id'] == everyone['id'] and overlap['winner_ids'] == [mine['id']]
    live = approve(h, propose(h, 'duties_per_week', [everyone, mine], acknowledged=[everyone['id']])['record'])
    w = h.workspace(start='2026-10-04', end='2026-10-11')
    days = ('05', '06', '07', '08', '09')
    for day, people in zip(days, ([a], [a], [a], [b], [b])):
        w = h.shift(w, f'2026-10-{day}T08:00:00+03:00', f'2026-10-{day}T12:00:00+03:00', people=people)['workspace']
    data = h.snapshot(w)
    flagged = {f.get('person_id') for f in evaluate(data)['findings'] if f.get('rule_id') == live['id']}
    assert flagged == {b}  # a's own line (3) holds for a; b works 2 against the line for everyone (1)
    # Five sessions, one person each: a may take 3 and everyone else 1, so a schedule exists only with a's own line.
    for s in data['shifts']:
        s['participants'] = []
    run = cp_sat.generate(copy.deepcopy(data), 10, 1, deterministic=True)
    added = [(x.get('participant') or x)['person_id'] for x in run['additions']]
    assert run['status'] == 'candidate_feasible' and len(added) == 5
    assert added.count(a) == 3 and added.count(b) == 1 and added.count(c) == 1


def test_needs_attention_reads_the_rules_in_force_and_the_drafts_they_block(h):
    a, b, c = h.seed['people']
    everyone, mine = line(numbers=most(1)), line(who=person(a), numbers=most(3))
    ending = line(where={'conditions': [{'field': 'service_ids', 'values': [h.seed['services'][0]]}]}, numbers=most(4), ends_on='2026-10-20')
    assert h.get('auditor', '/api/rules/catalogue')['attention'] == []
    live = approve(h, propose(h, 'duties_per_week', [everyone, mine, ending], acknowledged=[everyone['id']])['record'])
    found = h.get('auditor', '/api/rules/catalogue')['attention']
    assert [(x['kind'], x['line_ids'], x['fixes'][0]['kind']) for x in found] == [
        ('overlap', [everyone['id'], mine['id']], 'acknowledge'), ('ending', [ending['id']], 'acknowledge')]
    assert all(x['rule_id'] == live['id'] for x in found)
    # b works twice in a week against the line for everyone (1): the draft is blocked by that mandatory line.
    w = h.workspace(start='2026-10-04', end='2026-10-11')
    for day in ('05', '06'):
        w = h.shift(w, f'2026-10-{day}T08:00:00+03:00', f'2026-10-{day}T12:00:00+03:00', people=[b])['workspace']
    blocks = [x for x in h.get('coordinator', '/api/rules/catalogue')['attention'] if x['kind'] == 'blocks']
    assert [(x['facts']['workspace_ids'], x['fixes'][0]) for x in blocks] == [
        ([w['id']], {'kind': 'resolve', 'recommended': True, 'ops': [], 'workspace_id': w['id'], 'workspace': w['name']})]
    assert blocks[0]['facts']['count'] >= 1 and everyone['id'] in blocks[0]['line_ids'] and mine['id'] not in blocks[0]['line_ids']
    assert [f['kind'] for f in blocks[0]['fixes']] == ['resolve', 'advisory']
    # Only rotas being planned count: one already over (2026-09) and a discarded one do not. A second draft blocked
    # by the same line joins the same issue.
    def blocked(start, end, days):
        other = h.workspace(name=f'Rota from {start}', start=start, end=end)
        for day in days:
            other = h.shift(other, f'{day}T08:00:00+03:00', f'{day}T12:00:00+03:00', people=[b])['workspace']
        return other
    blocked('2026-09-20', '2026-09-27', ('2026-09-21', '2026-09-22'))
    second = blocked('2026-10-11', '2026-10-18', ('2026-10-12', '2026-10-13'))
    discarded = blocked('2026-10-18', '2026-10-25', ('2026-10-19', '2026-10-20'))
    h.command('coordinator', 'workspace.archive', {'reason': 'Not needed'}, discarded['id'], discarded['revision'])
    [blocks] = [x for x in h.get('coordinator', '/api/rules/catalogue')['attention'] if x['kind'] == 'blocks']
    assert sorted(blocks['facts']['workspace_ids']) == sorted([w['id'], second['id']])
    assert [f['workspace_id'] for f in blocks['fixes'] if f['kind'] == 'resolve'] == [d['workspace_id'] for d in blocks['facts']['drafts']]


def test_reach_counts_people_by_the_clinics_they_can_work_at_and_an_empty_group_is_kept(h):
    """A who of "can work at" clinics (as Fair share lines migrated) reaches the people allowed there today, never 0;
    a rotation nobody is on today reaches nobody but holds again when someone rotates in: keep it, not remove it."""
    clinic = h.seed['services'][0]
    allowed = {e['person_id'] for e in h.get('admin', '/api/catalogues')['eligibilities'] if e['service_id'] == clinic}
    visiting = h.configure('rotation_types', {'name': 'Visiting rotation', 'starts_on': '2026-01-01'})['record']
    works_there = line(who={'conditions': [{'field': 'eligible_service_ids', 'values': [clinic]}]}, numbers=most(4))
    approve(h, propose(h, 'duties_per_week', [works_there])['record'])
    off = line(who={'conditions': [{'field': 'rotation_type_ids', 'values': [visiting['id']]}]}, numbers={'apply': {}})
    approve(h, propose(h, 'keep_off', [off])['record'])
    page = h.get('auditor', '/api/rules/catalogue')
    week = next(e for e in page['entries'] if e['id'] == 'duties_per_week')
    assert allowed and week['reach'] == {works_there['id']: len(allowed)} and week['people'] == len(allowed)
    [nobody] = [x for x in page['attention'] if x['kind'] == 'nobody']
    assert nobody['line_ids'] == [off['id']] and nobody['facts'] == {'group': True}
    assert [(f['kind'], f['recommended']) for f in nobody['fixes']] == [('acknowledge', True), ('remove', False)]


def test_acknowledgements_are_shared_audited_undone_and_expire_when_the_issue_changes(h):
    """rule.acknowledge / rule.unacknowledge: coordinators and admins acknowledge Needs attention issues for everyone
    (who, when, note); the overlaps acknowledged when proposing are the same acknowledgement; a changed line expires it."""
    a = h.seed['people'][0]
    everyone, mine = line(numbers=most(1)), line(who=person(a), numbers=most(3))
    ending = line(where={'conditions': [{'field': 'service_ids', 'values': [h.seed['services'][0]]}]}, numbers=most(4), ends_on='2026-10-20')
    live = approve(h, propose(h, 'duties_per_week', [everyone, mine, ending], acknowledged=[everyone['id']])['record'])
    attention = {item['kind']: item for item in h.get('auditor', '/api/rules/catalogue')['attention']}
    overlap, soon = attention['overlap'], attention['ending']
    assert overlap['acknowledged']['note'] == 'When proposed: Conflict check' and overlap['acknowledged']['by']
    assert soon['kind'] == 'ending' and soon['acknowledged'] is None
    for role in ('auditor', 'approver', 'staff'):
        h.command(role, 'rule.acknowledge', {'fingerprints': [soon['fingerprint']]}, status=403)
    assert h.command('admin', 'rule.acknowledge', {'fingerprints': [soon['fingerprint']], 'note': 'Winter only, ends as planned'})['acknowledged'] == 1
    seen = {x['kind']: x for x in h.get('coordinator', '/api/rules/catalogue')['attention']}
    assert seen['ending']['acknowledged']['note'] == 'Winter only, ends as planned' and seen['ending']['acknowledged']['at']
    assert h.command('coordinator', 'rule.acknowledge', {'fingerprints': [soon['fingerprint']]})['acknowledged'] == 0  # who and when stay
    stale = h.command('coordinator', 'rule.acknowledge', {'fingerprints': ['0' * 64]}, status=409)
    assert stale['error']['code'] == 'ISSUE_CHANGED'
    with transaction(h.app.state.engine) as db:
        assert [e.action for e in db.scalars(select(m.AuditEvent).where(m.AuditEvent.action.like('rule.%acknowledge')))] == ['rule.acknowledge'] * 2
    # Undo takes it back for everyone; acknowledging again restores it.
    assert h.command('coordinator', 'rule.unacknowledge', {'fingerprints': [soon['fingerprint']]})['unacknowledged'] == 1
    assert next(x for x in h.get('auditor', '/api/rules/catalogue')['attention'] if x['kind'] == 'ending')['acknowledged'] is None
    with transaction(h.app.state.engine) as db:  # who acknowledged it, and when, stay in the history
        kept = [(x.reason, x.snapshot['note']) for x in db.scalars(select(m.DefinitionHistory).where(
            m.DefinitionHistory.kind == 'rule_acknowledgement', m.DefinitionHistory.object_id.in_(
                select(m.DefinitionHistory.object_id).where(m.DefinitionHistory.reason == 'Taken back'))).order_by(m.DefinitionHistory.object_revision))]
        assert kept == [('Winter only, ends as planned', 'Winter only, ends as planned'), ('Taken back', 'Winter only, ends as planned')]
    h.command('coordinator', 'rule.acknowledge', {'fingerprints': [soon['fingerprint']], 'note': 'Ends as planned'})
    # A new version changing only the ending line: its acknowledgement expires; the overlap's holds (same lines).
    approve(h, propose(h, 'duties_per_week', [everyone, mine, {**ending, 'ends_on': '2026-10-25'}], policy_id=live['id'], revision=live['revision'],
                       acknowledged=[everyone['id']])['record'])
    after = {x['kind']: x for x in h.get('auditor', '/api/rules/catalogue')['attention']}
    assert after['ending']['acknowledged'] is None and after['ending']['fingerprint'] != soon['fingerprint']
    assert after['overlap']['fingerprint'] == overlap['fingerprint'] and after['overlap']['acknowledged'] == overlap['acknowledged']


def test_a_blank_number_is_inherited_and_shown_and_none_removes_it(h):
    """Blank inherits (docs/design/rule-catalogue.md §2): the check and the page name each blank number's value and the
    line it comes from; the overlap names only the numbers replaced; an explicit None is proposed and removes it."""
    a = h.seed['people'][0]
    everyone = line(numbers={'min': {'value': 1}, 'max': {'value': 20}})
    alex = line(who=person(a), numbers={'max': {'value': 10}})
    found = check(h, 'duties_per_month', [everyone, alex])
    assert found['inherited'] == {alex['id']: {'min': {'value': 1.0, 'level': 'mandatory', 'from': everyone['id']}}}
    [overlap] = found['overlaps']
    assert overlap['numbers'] == ['max'] and overlap['people'] == 1 and 'on its max' in overlap['message']
    live = approve(h, propose(h, 'duties_per_month', [everyone, alex], acknowledged=[everyone['id']])['record'])
    shown = next(e for e in h.get('auditor', '/api/rules/catalogue')['entries'] if e['id'] == 'duties_per_month')
    assert shown['inherited'] == {alex['id']: {'min': {'value': 1.0, 'level': 'mandatory', 'from': everyone['id']}}}
    none = {**alex, 'numbers': {'min': {'value': None}, 'max': {'value': 10}}}
    found = check(h, 'duties_per_month', [everyone, none])
    assert found['inherited'] == {} and found['overlaps'][0]['numbers'] == ['max', 'min']
    record = propose(h, 'duties_per_month', [everyone, none], policy_id=live['id'], revision=live['revision'], acknowledged=[everyone['id']])['record']
    assert record['definition']['rule']['lines'][1]['numbers']['min']['value'] is None
