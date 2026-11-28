"""QA stream B2 (Rules & configuration): exact duplicate rules are retired rather than grouped (with the proof that the
rule kept checks the same), a rule activation is undone by retiring it and restoring what it replaced, and the lists the
app keeps in memory are re-sent only when they changed."""
from packages.domain.rule_groups import duplicates, expanded
from .governance_support import retire_rule
from .test_rule_simplification import PERSON, SERVICE, WEEK_START, quota, rule
from .test_rule_templates import activate, propose


# Domain -------------------------------------------------------------------------------------------------------------

def test_exact_copies_are_retired_and_the_rule_kept_checks_the_same():
    scope = {'person_ids': [PERSON['Kopofiku']], 'service_ids': [SERVICE['Clinic A']]}
    first = {**rule('Weekly clinic ceiling', quota('2026-09-01', '2026-12-01', 4, 'maximum', 'duties', window_alignment='calendar_week'), scope,
                    starts_on='2026-09-01', ends_on='2026-12-01'), 'created_at': '2026-01-01'}
    copy = {**rule('Clinic A ceiling (copy)', first['definition']['rule'], scope, starts_on='2026-09-01', ends_on='2026-12-01'), 'created_at': '2026-02-01'}
    other = rule('Another ceiling', {**first['definition']['rule'], 'amount': 5}, scope, starts_on='2026-09-01', ends_on='2026-12-01')
    [change] = duplicates([first, copy, other], week_start=WEEK_START)
    assert (change['shape'], change['retire'], change['rule_ids'], change['kept_id']) == ('duplicate', True, [copy['id']], first['id'])
    assert 'reported twice' in change['was'] and 'score' not in change['was'] and 'Weekly clinic ceiling' in change['now']
    # The proof: with the copy read as the kept rule, both check exactly what the kept rule checks.
    assert expanded([first, copy], {copy['id']: first['id']}, WEEK_START) == expanded([first], None, WEEK_START)
    # A different number, other dates, a waiting replacement or a person exception: not a copy to retire.
    assert duplicates([first, other], week_start=WEEK_START) == []
    later = rule('Weekly clinic ceiling', first['definition']['rule'], scope, starts_on='2026-10-01', ends_on='2026-12-01')
    assert duplicates([first, later], week_start=WEEK_START) == []
    assert duplicates([first, copy], held={copy['id']}, week_start=WEEK_START) == []
    waiting = rule('Grouped', first['definition']['rule'], scope, status='proposed', merged_ids=[copy['id']])
    assert duplicates([first, copy, waiting], week_start=WEEK_START) == []


def test_advisory_copies_say_the_score_counted_them_twice_and_grouped_rules_are_kept():
    pref = {'kind': 'allocation_preference', 'priority': 3, 'session_priority': 0}
    grouped = {**rule('Grouped preference', pref, severity='advisory', merged_ids=[PERSON['Badr'], PERSON['Carla']]), 'created_at': '2026-05-01'}
    plain = {**rule('Preference', pref, severity='advisory'), 'starts_on': grouped['starts_on'], 'ends_on': grouped['ends_on'], 'created_at': '2026-01-01'}
    [change] = duplicates([plain, grouped], week_start=WEEK_START)
    # Retiring a grouped rule would bring back the rules it grouped: the grouped one is the one kept.
    assert change['rule_ids'] == [plain['id']] and change['kept_id'] == grouped['id'] and 'counted twice in the score' in change['was']
    assert duplicates([grouped, {**grouped, 'id': 'z'}], week_start=WEEK_START) == []  # only grouped copies: nothing safe to retire


# API ----------------------------------------------------------------------------------------------------------------

def test_group_preview_offers_copies_to_retire_and_keeps_them_out_of_groups(h):
    first = activate(h, propose(h, {'kind': 'min_rest', 'hours': 11}, name='Rest between duties'))
    copy = activate(h, propose(h, {'kind': 'min_rest', 'hours': 11}, name='Rest (imported copy)'))
    preview = h.get('coordinator', '/api/policies/groups')
    [change] = [c for c in preview['changes'] if c['shape'] == 'duplicate']
    assert change['retire'] and change['rule_ids'] == [copy['id']] and change['kept_id'] == first['id']
    assert not any(copy['id'] in g['rule_ids'] for g in preview['groups'])
    # Proposing it is refused (an approver retires it); retiring and restoring are the usual audited decisions.
    refused = h.command('coordinator', 'policy.group', {'groups': [], 'changes': [[copy['id']]], 'reason': 'Tidy up'}, status=422)
    assert 'retired by an approver' in refused['error']['message']
    retired = retire_rule(h, copy, f"Duplicate of {first['name']}: tidy up")
    assert retired['record']['status'] == 'retired' and retired['restored_ids'] == []
    assert not [c for c in h.get('coordinator', '/api/policies/groups')['changes'] if c['shape'] == 'duplicate']
    h.command('approver', 'policy.restore', {'reason': 'Undo'}, copy['id'], retired['record']['revision'])


def test_activation_reports_the_versions_it_replaced_so_undo_can_restore_them(h):
    old = activate(h, propose(h, {'kind': 'min_rest', 'hours': 11}, name='Rest'))
    new = propose(h, {'kind': 'min_rest', 'hours': 12}, name='Rest', replaces_id=old['id'])
    done = h.command('approver', 'policy.activate', {'reason': 'Independent synthetic review'}, new['id'], new['revision'])
    assert [r['id'] for r in done['replaced']] == [old['id']] and done['replaced'][0]['status'] == 'retired'
    # The Undo the app runs (api.ts UNDO 'policy.activate'): retire the new rule, then restore what it replaced.
    retired = retire_rule(h, done['record'], 'Undo activation')
    h.command('approver', 'policy.restore', {'reason': 'Undo activation'}, old['id'], done['replaced'][0]['revision'])
    status = {p['id']: p['status'] for p in [p for state in ('active', 'proposed', 'retired')
        for p in h.get('coordinator', '/api/policies?status=' + state)['policies']]}
    assert status[old['id']] == 'active' and status[new['id']] == 'retired' and retired['record']['status'] == 'retired'
    # A proposal's Undo withdraws it (declined by the person who proposed it, kept in History).
    again = propose(h, {'kind': 'min_rest', 'hours': 10}, name='Shorter rest')
    h.command('coordinator', 'policy.decline', {'reason': 'Withdrawn with Undo by the person who proposed it'}, again['id'], again['revision'])


def test_catalogues_send_only_the_lists_that_changed(h):
    full = h.get('coordinator', '/api/catalogues')
    versions = full['_versions']
    assert set(versions) == {k for k in full if k != '_versions'} and 'policies' in versions and 'people' in versions
    known = ','.join(versions.values())
    same = h.get('coordinator', '/api/catalogues?known=' + known)
    assert set(same) == {'_versions'} and same['_versions'] == versions
    made = propose(h, {'kind': 'min_rest', 'hours': 11}, name='Fresh rest rule')
    after = h.get('coordinator', '/api/catalogues?known=' + known)
    assert set(after) == {'policies', '_versions'} and any(p['id'] == made['id'] for p in after['policies'])
    assert after['_versions']['policies'] != versions['policies'] and after['_versions']['people'] == versions['people']
    whole = h.get('coordinator', '/api/catalogues')
    assert [k for k in whole if k != '_versions'] == [k for k in full if k != '_versions']  # same lists, same order
    assert whole['policies'] == after['policies']
