"""Resolve issues › Replace ranks the best person per place from indexes worked out once per preview (staffing.people_index)
and checks each candidate against only the rules that can concern them. It must pick exactly the people, in the same
order, with the same findings as the plain implementation it replaced (kept below as the reference), and stay fast."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from random import Random
import pytest
from apps.api.services import issues, staffing
from apps.api.services.issues import best_replacements
from apps.api.services.staffing import QUALIFICATION_TEXT, REASON_KIND
from packages.domain.conditions import active_rules, counting_views
from packages.domain import leadership
from packages.domain.validation import evaluate, finding_observations, named_people, index_person_rows, policy_group_tiers
from tests.scheduler_departments import department
from tests.test_resolve_issues import department_rota
from tests.test_performance_budget import python_budget


# The reference before the indexes, using canonical finding identities and named supervision plans.
def reference_replacements(source, targets, people):
    shifts = {row['id']: row for row in copy.deepcopy(source['shifts']) if row['id'] in set(source['focus_ids'])}
    work = {**source, 'shifts': [shifts.get(row['id'], row) for row in source['shifts']]}
    rows = []
    for shift_id, person_id in sorted(targets, key=lambda t: (shifts[t[0]]['start'] if t[0] in shifts else '', *t)):
        shift = shifts.get(shift_id)
        seat = next((p for p in shift['participants'] if p['person_id'] == person_id), None) if shift else None
        row = {'keys': targets[(shift_id, person_id)], 'shift_id': shift_id, 'person_id': person_id,
               'assignment_id': seat['id'] if seat else None, 'replacement_id': None, 'replacement_name': None,
               'status': 'open', 'why': None, 'findings': []}
        if seat is None:
            row['why'] = 'They are no longer on this session'
        elif shift['locked'] or shift['cancelled']:
            row['why'] = 'The session is locked or cancelled'
        else:
            best, _ = reference_check_people(work, shift, seat, people, best=True)
            if not best:
                row['why'] = 'No one free who no rule stops'
            else:
                pick = best[0]
                row.update(replacement_id=pick['person_id'], replacement_name=pick['display_name'], status=pick['status'],
                           findings=pick['findings'])
                supervisor = ((pick.get('supervision') or {}).get('proposal') or {}).get('supervisor_person_id')
                if supervisor:
                    row['supervisor_person_id'] = supervisor
                seat.update(person_id=pick['person_id'], supervisor_person_id=supervisor, team_lead=False, fixed=False)
        rows.append(row)
    return rows


def reference_check_people(source, shift, replaced, candidates, best=False):
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    from packages.domain.time import aware
    from packages.domain.validation import qualification_failures, categories_for
    kept = [p for p in shift['participants'] if not replaced or p['id'] != replaced['id']]
    slot = {'start': shift['start'], 'end': shift['end'], 'participation': 'staff', 'overtime': False, 'segments': []}
    if replaced:
        slot = {k: replaced.get(k, slot[k]) for k in slot}
    start, end = aware(shift['start']), aware(shift['end'])
    near_lo, near_hi = start, end
    live = [row for row in source['shifts'] if not row['cancelled']]
    duties = {}
    for row in live:
        for p in row['participants']:
            duties.setdefault(p['person_id'], []).append((row, p))
    near = {row['id'] for row in live if aware(row['start']) < near_hi and near_lo < aware(row['end'])}
    zone = ZoneInfo(source['organisation']['timezone'])
    local = start.astimezone(zone)
    week_lo = (local - timedelta(days=(local.weekday() - source['organisation'].get('week_start', 0)) % 7)).replace(hour=0, minute=0, second=0, microsecond=0)
    week_hi = week_lo + timedelta(days=7)
    definition = shift['type_snapshot']['definition']
    services = source.get('services') or {}
    quota_lo = (start.astimezone(zone) - timedelta(days=1)).date().isoformat()
    quota_hi = (end.astimezone(zone) + timedelta(days=1)).date().isoformat()
    assigned = {p['person_id'] for p in kept} | ({replaced['person_id']} if replaced else set())
    ref_index = staffing.people_index(source)
    from packages.domain.validation import PERSON_ROWS
    rows_of = {name: {} for name in PERSON_ROWS}
    for name in PERSON_ROWS:
        for row in source.get(name, []):
            rows_of[name].setdefault(row['person_id'], []).append(row)

    new_findings = staffing._new_candidate_findings

    baseline = evaluate(ref_index['fast'], explain_findings=False)

    def check(pid, mine, week, hours):
        person = source['people'][pid]
        prev = [aware(p['end']) for _, p in mine if aware(p['end']) <= start]
        nxt = [aware(p['start']) for _, p in mine if aware(p['start']) >= end]
        info = {'person_id': pid, 'display_name': person['display_name'], 'rota_hours': round(hours, 1), 'week_hours': round(week, 1),
                'rest_before_hours': round((start - max(prev)).total_seconds() / 3600, 1) if prev else None,
                'rest_after_hours': round((min(nxt) - end).total_seconds() / 3600, 1) if nxt else None,
                'can_lead': leadership.qualified(source, pid, shift, start, end)}
        failures = qualification_failures(source, pid, shift, definition, start, end)
        if failures:
            reasons = [{'code': f['code'], 'kind': 'NOT ELIGIBLE', 'severity': 'error', 'rule_id': None,
                        'message': QUALIFICATION_TEXT.get(f['code'])
                        or 'Not eligible for ' + (services.get(f.get('service_id')) or {}).get('name', 'this service') + '.'}
                       for f in failures]
            return {**info, 'status': 'blocked', 'fills': False, 'findings': reasons}
        before = [d[0]['id'] for d in mine if d[0]['start'] < shift['start']][-1:]
        after = [d[0]['id'] for d in mine if d[0]['start'] > shift['start']][:1]
        own = {d[0]['id'] for d in mine}
        keep = [row for row in live if row['id'] in near or row['id'] in own or row['id'] == shift['id']]
        people = {pid} | {p['person_id'] for row in keep for p in row['participants']}
        candidate = {**slot, 'person_id': pid, 'fixed': False, 'supervisor_person_id': None, 'team_lead': False,
                     'id': 'candidate-' + pid, 'shift_id': shift['id']}
        # Keep ranking and reduced checks naive; named pairing belongs to the shared manual-supervision owner.
        planned = staffing.supervision_plan(ref_index, shift, kept, candidate, info,
                                            require_proposal=best)
        if planned is not None:
            return planned
        reduced = {**ref_index['fast'], 'focus_ids': [shift['id'], *before, *after], 'quota_subjects': {pid}, 'person_rule_subjects': {pid}, 'skip_coverage': True,
                   'quota_dates': (quota_lo, quota_hi),
                   '_person_dates_cache': {}, '_fair_share_eligibility_policies': ref_index['fast']['policies'],
                   **{name: [row for person in sorted(people) for row in rows_of[name].get(person, ())]
                      for name in PERSON_ROWS if name in source}}
        if ref_index['personal'] is not None:
            reduced['policies'] = [rule for rule, named in ref_index['personal'] if named is None or pid in named]
        reduced.update(_active_policies=reduced['policies'], _counted_policies=counting_views(reduced, reduced['policies']),
                       _person_rows=index_person_rows(reduced),
                       _policy_group_tiers=policy_group_tiers(reduced['policies']),
                       _workload_normalization_cache={}, _distribution_cache={}, _allocation_match_cache={}, _policy_applies_cache={},
                       _staffing_selection_cache={})
        trial = evaluate({**reduced, 'shifts': [dict(row, participants=kept + [candidate]) if row['id'] == shift['id'] else row for row in keep]},
                         explain_findings=False)
        new = new_findings(baseline, trial)
        reasons = [{'code': f['code'], 'kind': REASON_KIND.get(f['code'], 'RULE'), 'severity': f['severity'],
                    'rule_id': f['rule_id'], 'message': f['message'], 'detail': f.get('detail'),
                    'start': f['start'], 'end': f['end'], **({'person_id': None, 'shift_id': f.get('shift_id')}
                    if f.get('person_id') is None else {})} for f in new]
        status = 'blocked' if any(f['severity'] == 'error' for f in new) else 'warn' if new else 'ok'
        fills = False
        supervision = definition.get('supervision') or {}
        trainee = bool(supervision) and bool(set(supervision.get('trainee_category_ids', [])) & categories_for(source, pid, start, end))
        if status != 'blocked' or trainee:
            # Session cover and supervision need peers' eligibility rules as well as the candidate's own.
            full = {'policies': ref_index['fast']['policies'], '_active_policies': ref_index['fast']['policies'],
                    '_counted_policies': ref_index['fast']['_counted_policies'],
                    '_policy_group_tiers': ref_index['fast']['_policy_group_tiers']}
            covered = evaluate({**reduced, **full, 'skip_coverage': False, 'focus_ids': [shift['id']],
                                'shifts': [dict(row, participants=kept + [candidate]) if row['id'] == shift['id'] else row for row in keep]},
                               explain_findings=False)
            fills = not any(r['status'] == 'gap' for r in covered['coverage'] if r['shift_id'] == shift['id'])
            extra = [f for f in new_findings(baseline, covered) if f.get('severity') == 'error']
            if extra:
                reasons += [{'code': f['code'], 'kind': REASON_KIND.get(f['code'], 'RULE'), 'severity': f['severity'],
                             'rule_id': f['rule_id'], 'message': f['message'], 'detail': f.get('detail'),
                             'start': f['start'], 'end': f['end'], **({'person_id': None, 'shift_id': f.get('shift_id')}
                             if f.get('person_id') is None else {})} for f in extra]
                status = 'blocked' if any(f['severity'] == 'error' for f in extra) else 'warn'
        return {**info, 'status': status, 'fills': fills, 'findings': reasons[:12]}

    def load(pid):
        mine = sorted(duties.get(pid, []), key=lambda d: d[0]['start'])
        hours = sum((aware(p['end']) - aware(p['start'])).total_seconds() for _, p in mine) / 3600
        week = sum((min(aware(p['end']), week_hi) - max(aware(p['start']), week_lo)).total_seconds()
                   for _, p in mine if aware(p['start']) < week_hi and week_lo < aware(p['end'])) / 3600
        return mine, week, hours

    named = sorted(set(candidates) - assigned, key=lambda pid: (source['people'][pid]['display_name'].casefold(), pid))
    if best:
        from packages.domain.conditions import active_rules
        from packages.domain.validation import person_rows, qualified, rotation_blocks, restriction_blocks
        from packages.domain.time import overlaps
        fast = {**source, 'policies': active_rules(source['policies']), '_person_rows': index_person_rows(source), '_scope_interval_cache': {}}
        loads = {pid: load(pid) for pid in named}
        fallback = None
        for pid in sorted(named, key=lambda pid: staffing.best_order_key(loads[pid][1], loads[pid][2], pid, shift['id'])):
            if any(aware(p['start']) < end and start < aware(p['end']) for _, p in loads[pid][0]):
                continue
            if not (source['people'][pid].get('active', True) and qualified(fast, pid, shift, definition, start, end)
                    and not rotation_blocks(fast, pid, start, end) and not restriction_blocks(fast, pid, shift, start, end)
                    and not any(overlaps(start, end, aware(r['start']), aware(r['end'])) for r in person_rows(fast, 'availability', pid))):
                continue
            row = check(pid, *loads[pid])
            if row.get('supervision') is not None and row['supervision']['proposal'] is None:
                continue
            if row['status'] == 'ok':
                return [row], slot
            fallback = fallback or (row if row['status'] == 'warn' else None)
        return [fallback] if fallback else [], slot
    return [check(pid, *load(pid)) for pid in named], slot


def staffed(data, rng):
    """Every session given one or two people at random (so rules, cover and quotas have something to say)."""
    people = sorted(data['people'])
    for n, shift in enumerate(sorted(data['shifts'], key=lambda s: (s['start'], s['id']))):
        for k, pid in enumerate(rng.sample(people, rng.choice((1, 2)))):
            shift['participants'].append({'id': f'seat-{n}-{k}', 'shift_id': shift['id'], 'person_id': pid, 'start': shift['start'],
                                          'end': shift['end'], 'participation': 'staff', 'overtime': False, 'segments': [],
                                          'fixed': False, 'supervisor_person_id': None, 'team_lead': False})
    return data


def with_person_rules(data, rng):
    """Copies of the department's own rules, each naming one person (as rule lines do): rest, workload, restriction and
    quota rules about someone else must not change anyone's ranking."""
    people = sorted(data['people'], key=lambda pid: data['people'][pid]['display_name'].casefold())
    kinds = {'min_rest', 'max_duties', 'max_hours', 'max_consecutive_days', 'restriction', 'block_quota', 'forbidden_transition'}
    for p in [p for p in data['policies'] if p['definition']['rule']['kind'] in kinds]:
        for n in range(3):
            scope = {**p['definition']['scope'], 'person_id': rng.choice(people)}
            data['policies'].append({**p, 'id': f"{p['id']}-person-{n}", 'definition': {**p['definition'], 'scope': scope,
                                     'severity': rng.choice(('mandatory', 'advisory'))}})
    return data


def variants(data):
    """The data as is; with one rule overriding another (fairness reads rules before overrides); and with the quota
    check over its bounds (it then reports only that), where the faster path must step aside."""
    yield 'plain', data
    first, second = data['policies'][0], data['policies'][-1]
    overriding = {**second, 'definition': {**second['definition'], 'overrides': [first['id']]}}
    yield 'overrides', {**data, 'policies': data['policies'][:-1] + [overriding]}
    yield 'quota bound', {**data, 'quota_policy_limit': 0}


@pytest.mark.parametrize('session_id, expected', [('session-0', ['person-b', 'person-a']),
                                                 ('session-2', ['person-a', 'person-b'])])
def test_best_order_key_uses_a_per_session_stable_shuffle(session_id, expected):
    for candidates in (expected, expected[::-1]):
        assert sorted(candidates, key=lambda pid: staffing.best_order_key(0, 0, pid, session_id)) == expected
    assert staffing.best_order_key(1.04, 2.06, 'person-a', session_id) == (
        1.0, 2.1, hashlib.sha256(f'{session_id}:person-a'.encode()).hexdigest())


@pytest.mark.parametrize('names', [('Zulu', 'Alpha'), ('Alpha', 'Zulu'), ('Same', 'Same')])
def test_replace_equal_loads_use_per_session_shuffle(h, names):
    old, *candidates = h.seed['people']
    workspace = h.shift(h.workspace(), people=[old])['workspace']
    source = h.snapshot(workspace)
    for pid, name in zip(sorted(candidates), names):
        source['people'][pid]['display_name'] = name
    shift = source['shifts'][0]
    targets = {(shift['id'], old): ['replace']}
    rows = best_replacements(source, targets, set(source['people']))
    assert rows == reference_replacements(source, targets, set(source['people']))
    assert rows[0]['replacement_id'] == min(candidates, key=lambda pid: hashlib.sha256(f"{shift['id']}:{pid}".encode()).hexdigest())
    assert rows[0]['status'] == 'ok' and rows[0]['findings'] == []


def test_weekend_run_identity_keeps_the_window_and_count_when_its_cited_session_changes():
    old = {'code': 'CONSECUTIVE_WEEKENDS', 'rule_id': 'weekends', 'person_id': 'person', 'shift_id': 'old-duty',
           'severity': 'error', 'start': '2026-10-09T00:00:00+03:00', 'end': '2026-10-18T00:00:00+03:00',
           'detail': {'weekends': 2, 'maximum': 1}}
    recited = {**old, 'shift_id': 'new-duty'}
    assert staffing._new_candidate_findings({'findings': [old]}, {'findings': [recited]}) == []
    worse = {**recited, 'detail': {'weekends': 3, 'maximum': 1}}
    assert staffing._new_candidate_findings({'findings': [old]}, {'findings': [worse]}) == [worse]


@pytest.mark.parametrize('existing_pairing', [False, True])
@pytest.mark.parametrize('require_pairing', [False, True])
def test_best_supervisor_checks_new_pairing_obligations_before_candidate_trials(h, monkeypatch, existing_pairing, require_pairing):
    from tests.test_manual_supervision_staffing import world
    w, _, _, trainee, candidate, old = world(h)
    h.policy({'kind': 'supervision_limit', 'mode': 'capacity', 'maximum': 2})
    source = h.snapshot(w)
    shift = source['shifts'][0]
    [seat] = shift['participants']
    shift['type_snapshot']['definition']['supervision']['require_pairing'] = require_pairing
    shift['participants'] = [{**seat, 'id': 'unpaired-trainee', 'person_id': trainee, 'participation': 'trainee'},
                             {**seat, 'id': 'replaced-supervisor', 'person_id': old}]
    if not existing_pairing:
        source['grants'] = [grant for grant in source['grants'] if grant['person_id'] != old]
    before = evaluate(source)
    trial = copy.deepcopy(source)
    trial['shifts'][0]['participants'][1]['person_id'] = candidate
    new = staffing._new_candidate_findings(before, evaluate(trial))
    assert any(f['code'] == 'SUPERVISION_PAIRING' for f in new) is not existing_pairing
    calls, real = [], staffing.evaluate
    monkeypatch.setattr(staffing, 'evaluate', lambda data, **kwargs: calls.append(data.get('_staffing_baseline')) or real(data, **kwargs))
    rows, _ = staffing.check_people(source, shift, shift['participants'][1], {candidate}, best=True)
    assert [(row['person_id'], row['status']) for row in rows] == ([(candidate, 'ok')] if existing_pairing else [])
    if not existing_pairing:
        assert None not in calls, 'A proven new pairing blocker needs no candidate trial'


def test_best_replacement_cannot_remove_a_kept_trainees_named_supervisor(h, monkeypatch):
    from tests.test_manual_supervision_staffing import world
    _, source, shift, trainee, supervisor, candidate = world(h)
    [seat] = shift['participants']
    shift['participants'].append({**seat, 'id': 'paired-trainee', 'person_id': trainee,
                                  'participation': 'trainee', 'supervisor_person_id': supervisor})
    before = evaluate(source)
    trial = copy.deepcopy(source)
    trial['shifts'][0]['participants'][0]['person_id'] = candidate
    assert any(f['code'] == 'SUPERVISION_PAIRING' for f in staffing._new_candidate_findings(before, evaluate(trial)))
    calls, real = [], staffing.evaluate
    monkeypatch.setattr(staffing, 'evaluate', lambda data, **kwargs: calls.append(data.get('_staffing_baseline')) or real(data, **kwargs))
    rows, _ = staffing.check_people(source, shift, seat, {candidate}, best=True)
    assert rows == []
    assert None not in calls, 'A known lost pairing must be refused before candidate trials'


def test_best_trainee_without_a_supervisor_needs_no_validator_trial(h, monkeypatch):
    from tests.test_manual_supervision_staffing import world
    _, source, shift, trainee, _, _ = world(h)
    monkeypatch.setattr(staffing, 'evaluate', lambda *args, **kwargs: pytest.fail('No supervisor can be proposed'))
    rows, _ = staffing.check_people(source, shift, shift['participants'][0], {trainee}, best=True)
    assert rows == []


@pytest.mark.parametrize('blocked', [False, True])
def test_best_unique_pairing_uses_one_complete_trial_and_still_checks_capacity(h, monkeypatch, blocked):
    from tests.test_manual_supervision_staffing import world
    w, _, _, trainee, supervisor, _ = world(h)
    if blocked:
        h.policy({'kind': 'supervision_limit', 'mode': 'capacity', 'maximum': 0})
    source = h.snapshot(w)
    shift = source['shifts'][0]
    calls, real = [], staffing.evaluate
    monkeypatch.setattr(staffing, 'evaluate', lambda data, **kwargs: calls.append(data.get('_partial')) or real(data, **kwargs))
    rows, _ = staffing.check_people(source, shift, None, {trainee}, best=True)
    assert len(calls) == 2 and calls == [None, None], 'One whole-rota baseline and one complete named-pair trial'
    if blocked:
        assert rows == []
    else:
        assert rows[0]['status'] == 'ok'
        assert rows[0]['supervision']['proposal']['supervisor_person_id'] == supervisor


@pytest.mark.parametrize('new_breach', [False, True])
@pytest.mark.parametrize('carry_baseline', [False, True])
def test_supervision_scan_keeps_existing_rest_findings_in_published_context(h, new_breach, carry_baseline):
    from tests.test_manual_supervision_staffing import world
    candidate = h.seed['people'][2]
    published = h.workspace(start='2026-10-03', end='2026-10-04')
    for start, end in [('08', '12'), ('16', '20')]:
        published = h.shift(published, start=f'2026-10-03T{start}:00:00+03:00',
                            end=f'2026-10-03T{end}:00:00+03:00', people=[candidate])['workspace']
    h.publish(published)
    w, _, _, trainee, _, _ = world(h)
    h.policy({'kind': 'min_rest', 'hours': 12})
    h.policy({'kind': 'supervision_limit', 'mode': 'capacity', 'maximum': 0})
    source = h.snapshot(w)
    shift = next(row for row in source['shifts'] if row['id'] in source['focus_ids'])
    [supervisor] = shift['participants']
    old = 'replaced-person'
    source['people'][old] = {**source['people'][candidate], 'id': old}
    seat = {**supervisor, 'id': 'replaced-seat', 'person_id': old}
    shift['participants'].append(seat)
    if new_breach:
        shift['start'], shift['end'] = '2026-10-04T04:00:00+03:00', '2026-10-04T08:00:00+03:00'
        for participant in shift['participants']:
            participant.update(start=shift['start'], end=shift['end'])
    external = [row for row in source['shifts'] if row['id'] not in source['focus_ids']]
    assert len(external) == 2 and all(row['workspace_id'] == published['id'] for row in external)
    whole = evaluate(source)
    assert not any(f['code'] == 'MIN_REST' for f in finding_observations(whole))
    person_baseline = evaluate({**source, 'focus_ids': [row['id'] for row in external]})
    assert any(f['code'] == 'MIN_REST' for f in finding_observations(person_baseline))
    trial = copy.deepcopy(source)
    next(row for row in trial['shifts'] if row['id'] == shift['id'])['participants'][-1]['person_id'] = candidate
    assert any(f['code'] == 'MIN_REST' for f in staffing._new_candidate_findings(
        person_baseline, evaluate({**trial, 'focus_ids': [row['id'] for row in trial['shifts']]}))) is new_breach
    index = staffing.people_index(source)
    if carry_baseline:
        rows, _ = staffing.check_people(source, shift, seat, {trainee}, best=True, index=index)
        assert rows == [] and 'supervision_baseline' in index
    rows, _ = staffing.check_people(source, shift, seat, {candidate} if carry_baseline else {trainee, candidate},
                                    best=True, index=index)
    assert 'supervision_baseline' in index, 'The first trainee trial must populate the workspace-only baseline'
    assert [(row['person_id'], row['status']) for row in rows] == ([] if new_breach else [(candidate, 'ok')])


def test_best_warning_fallback_skips_a_missing_supervision_proposal(h, monkeypatch):
    from tests.test_manual_supervision_staffing import world
    _, source, shift, trainee, _, _ = world(h)
    calls, real = [], staffing.supervision_plan

    def changed_plan(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return {**real(*args, **kwargs), 'status': 'warn'}
        return {'supervision': {'proposal': None}}

    monkeypatch.setattr(staffing, 'supervision_plan', changed_plan)
    rows, _ = staffing.check_people(source, shift, None, {trainee}, best=True)
    assert len(calls) == 2 and rows == []


def test_replace_quota_baseline_follows_each_pairs_eligible_subjects(h):
    old, first, second = h.seed['people']
    h.policy({'kind': 'block_quota', 'metric': 'duties', 'comparison': 'maximum', 'amount': 0,
              'starts_on': '2026-10-04', 'ends_on': '2026-10-11'},
             scope={'person_id': second, 'conditions': {'match': 'all', 'conditions': [
                 {'field': 'service_ids', 'op': 'any', 'values': h.seed['services'][:1]}]}})
    w = h.shift(h.workspace(), service=0, people=[second])['workspace']
    for day in (5, 6):
        w = h.shift(w, service=1, start=f'2026-10-{day:02}T08:00:00+03:00',
                    end=f'2026-10-{day:02}T12:00:00+03:00', people=[old])['workspace']
    source = h.snapshot(w)
    assert any(f['code'] == 'QUOTA_ABOVE_MAXIMUM' and f['person_id'] == second
               for f in finding_observations(evaluate(source)))
    index = staffing.people_index(source)
    shifts = sorted(source['shifts'], key=lambda row: row['start'])[1:]
    for shift, candidate in zip(shifts, (first, second)):
        rows, _ = staffing.check_people(source, shift, shift['participants'][0], {candidate}, best=True, index=index)
        assert [(row['person_id'], row['status'], row['findings']) for row in rows] == [(candidate, 'ok', [])]


def test_move_seat_invalidates_quota_baseline_before_a_breach_is_reintroduced(h):
    old, other, candidate = h.seed['people']
    h.policy({'kind': 'block_quota', 'metric': 'duties', 'comparison': 'maximum', 'amount': 0,
              'starts_on': '2026-10-04', 'ends_on': '2026-10-11'},
             scope={'person_id': candidate, 'conditions': {'match': 'all', 'conditions': [
                 {'field': 'service_ids', 'op': 'any', 'values': h.seed['services'][:1]}]}})
    w = h.shift(h.workspace(), service=0, people=[candidate])['workspace']
    for day, service in ((5, 1), (6, 0)):
        w = h.shift(w, service=service, start=f'2026-10-{day:02}T08:00:00+03:00',
                    end=f'2026-10-{day:02}T12:00:00+03:00', people=[old])['workspace']
    source = h.snapshot(w)
    original, warm, target = sorted(source['shifts'], key=lambda row: row['start'])
    index = staffing.people_index(source)
    rows, _ = staffing.check_people(source, warm, warm['participants'][0], {candidate}, best=True, index=index)
    assert rows[0]['status'] == 'ok', 'An unchanged existing quota breach is not new'
    staffing.move_seat(index, original, original['participants'][0], other)
    assert not [f for f in finding_observations(evaluate(source)) if f['code'] == 'QUOTA_ABOVE_MAXIMUM']
    rows, _ = staffing.check_people(source, target, target['participants'][0], {candidate}, best=True, index=index)
    assert rows == [], 'Reintroducing the removed mandatory quota breach must block the candidate'


@pytest.mark.parametrize('new_breach', [None, 'min_rest', 'max_duties', 'worse_max_duties'])
def test_best_replacement_ignores_old_other_session_findings_but_blocks_new_breaches(h, new_breach):
    old, candidate = h.seed['people'][:2]
    if new_breach == 'min_rest':
        h.policy({'kind': 'min_rest', 'hours': 12})
    elif new_breach in {'max_duties', 'worse_max_duties'}:
        h.policy({'kind': 'max_duties', 'maximum': 1, 'window': 'week'})
    w = h.shift(h.workspace(), people=[candidate])['workspace']
    if new_breach == 'min_rest':
        w = h.shift(w, start='2026-10-05T22:00:00+03:00', end='2026-10-06T04:00:00+03:00',
                    people=[candidate])['workspace']
    if new_breach == 'worse_max_duties':
        w = h.shift(w, start='2026-10-06T08:00:00+03:00', end='2026-10-06T12:00:00+03:00', people=[candidate])['workspace']
    day = '05' if new_breach == 'worse_max_duties' else '06'
    created = h.shift(w, start=f'2026-10-{day}T08:00:00+03:00', end=f'2026-10-{day}T12:00:00+03:00', people=[old])
    source = h.snapshot(created['workspace'])
    source['availability'].append({'id': 'existing-other-session-leave', 'person_id': candidate,
        'kind': 'recorded_leave', 'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00'})
    shift = next(row for row in source['shifts'] if row['id'] == created['shift_id'])
    before = evaluate(source)
    assert any(f['code'] == 'UNAVAILABLE' and f['shift_id'] != shift['id'] for f in finding_observations(before))
    trial = copy.deepcopy(source)
    next(row for row in trial['shifts'] if row['id'] == shift['id'])['participants'][0]['person_id'] = candidate
    errors = [f for f in staffing._new_candidate_findings(before, evaluate(trial)) if f['severity'] == 'error']
    assert bool(errors) is bool(new_breach)
    if new_breach:
        assert new_breach.upper().removeprefix('WORSE_') in {f['code'] for f in errors}
    rows, _ = staffing.check_people(source, shift, shift['participants'][0], {candidate}, best=True)
    assert [(row['person_id'], row['status']) for row in rows] == ([] if new_breach else [(candidate, 'ok')])


@pytest.mark.parametrize('code', ['MAX_DUTIES', 'MIN_REST'])
def test_existing_window_or_duty_pair_survives_a_changed_focused_citation(h, code):
    old, candidate = h.seed['people'][:2]
    if code == 'MAX_DUTIES':
        h.policy({'kind': 'max_duties', 'maximum': 1, 'window': 'day'})
    w = h.workspace()
    periods = [(6, '08', '10'), (6, '10', '12'), (6, '12', '14')] if code == 'MAX_DUTIES' else [(6, '08', '12'), (6, '16', '20')]
    for day, start, end in periods:
        w = h.shift(w, service=0, start=f'2026-10-{day:02}T{start}:00:00+03:00',
                    end=f'2026-10-{day:02}T{end}:00:00+03:00', people=[candidate])['workspace']
    day = 7 if code == 'MAX_DUTIES' else 4
    created = h.shift(w, service=1, start=f'2026-10-{day:02}T08:00:00+03:00',
                      end=f'2026-10-{day:02}T12:00:00+03:00', people=[old])
    if code == 'MIN_REST':
        h.policy({'kind': 'min_rest', 'hours': 12})
    source = h.snapshot(created['workspace'])
    shift = next(row for row in source['shifts'] if row['id'] == created['shift_id'])
    before = evaluate(source)
    assert any(f['code'] == code for f in finding_observations(before))
    trial = copy.deepcopy(source)
    next(row for row in trial['shifts'] if row['id'] == shift['id'])['participants'][0]['person_id'] = candidate
    assert not [f for f in staffing._new_candidate_findings(before, evaluate(trial)) if f['severity'] == 'error']
    rows, _ = staffing.check_people(source, shift, shift['participants'][0], {candidate}, best=True)
    assert [(row['person_id'], row['status']) for row in rows] == [(candidate, 'ok')]


@pytest.mark.parametrize('day, blocked', [(10, False), (23, True)])
def test_best_weekend_precheck_ignores_existing_windows_but_blocks_a_new_one(h, day, blocked):
    old, candidate = h.seed['people'][:2]
    h.policy({'kind': 'max_consecutive_weekends', 'maximum': 1, 'weekend_start': 4, 'weekend_days': 2})
    w = h.workspace(end='2026-11-01')
    for existing in (9, 16):
        w = h.shift(w, start=f'2026-10-{existing:02}T08:00:00+03:00',
                    end=f'2026-10-{existing:02}T12:00:00+03:00', people=[candidate])['workspace']
    created = h.shift(w, start=f'2026-10-{day:02}T08:00:00+03:00',
                      end=f'2026-10-{day:02}T12:00:00+03:00', people=[old])
    source = h.snapshot(created['workspace'])
    shift = next(row for row in source['shifts'] if row['id'] == created['shift_id'])
    before = evaluate(source)
    assert any(f['code'] == 'CONSECUTIVE_WEEKENDS' for f in finding_observations(before))
    trial = copy.deepcopy(source)
    next(row for row in trial['shifts'] if row['id'] == shift['id'])['participants'][0]['person_id'] = candidate
    assert any(f['code'] == 'CONSECUTIVE_WEEKENDS' for f in staffing._new_candidate_findings(before, evaluate(trial))) is blocked
    rows, _ = staffing.check_people(source, shift, shift['participants'][0], {candidate}, best=True)
    assert [(row['person_id'], row['status']) for row in rows] == ([] if blocked else [(candidate, 'ok')])


def test_replace_selection_is_identical_across_hash_seeds(h):
    old, *candidates = h.seed['people']
    workspace = h.shift(h.workspace(), people=[old])['workspace']
    source = h.snapshot(workspace)
    for pid, name in zip(sorted(candidates), ('Zulu', 'Alpha')):
        source['people'][pid]['display_name'] = name
    payload = json.dumps({'source': source, 'old': old})
    script = """
import json, sys
from apps.api.services.issues import best_replacements
value = json.load(sys.stdin)
source = value['source']
targets = {(source['shifts'][0]['id'], value['old']): ['replace']}
print(json.dumps(best_replacements(source, targets, set(source['people'])), sort_keys=True))
"""
    results = []
    for seed in (0, 1, 7):
        result = subprocess.run([sys.executable, '-c', script], input=payload, capture_output=True,
                                text=True, check=True, timeout=30, cwd=Path(__file__).resolve().parents[1],
                                env={**os.environ, 'PYTHONHASHSEED': str(seed)})
        results.append(json.loads(result.stdout))
    assert results[0] == results[1] == results[2]
    session_id = source['shifts'][0]['id']
    assert results[0][0]['replacement_id'] == min(candidates, key=lambda pid: hashlib.sha256(f'{session_id}:{pid}'.encode()).hexdigest())


@pytest.mark.parametrize('seed', range(4))
def test_replace_picks_exactly_what_the_reference_picks(h, seed):
    """Random replacement batches preserve the reference's people, order, status, findings and reasons."""
    rng = Random(seed)
    h.policy({'kind': 'balance_goal', 'metric': 'duties', 'aim': 'equal_share', 'period': 'rota'}, {}, severity='advisory')
    data, _ = department(h, 40 + seed, people=24, days=10, rules='full' if seed % 2 else 'random', leave_rate=0.12, clinics=2)
    data = with_person_rules(staffed(data, rng), rng)
    people = set(data['people'])
    seats = [(s['id'], p['person_id']) for s in data['shifts'] for p in s['participants']]
    compared = 0
    for name, source in variants(data):
        before = json.dumps(source, sort_keys=True, default=str)
        for trial in range(3):
            chosen = rng.sample(seats, rng.randint(1, 30)) + [(seats[0][0], 'nobody')]  # one place they already left
            targets = {seat: [f'{name}-{trial}-{n}'] for n, seat in enumerate(chosen)}
            expected = reference_replacements(source, targets, people)
            actual = best_replacements(source, targets, people)
            assert actual == expected, (name, trial)
            compared += sum(r['replacement_id'] is not None for r in actual)
        assert json.dumps(source, sort_keys=True, default=str) == before, 'the rota given is not changed'
    assert compared > 0, 'at least one safe replacement remains to compare'


def test_peer_restrictions_still_determine_fair_share_audience_in_reduced_checks(h):
    alex, jordan, taylor = h.seed['people']
    h.policy({'kind': 'balance_goal', 'metric': 'duties', 'aim': 'equal_share', 'period': 'rota',
              'tolerance_percent': 0, 'tolerance_units': 0}, severity='advisory')
    h.policy({'kind': 'restriction', 'weekdays': list(range(7)), 'start_time': '00:00', 'end_time': '24:00'},
             {'person_id': alex})
    w = h.shift(h.workspace(), people=[alex], seed_restricted=True)['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[jordan])['workspace']
    data = h.snapshot(w)
    shift = next(s for s in data['shifts'] if s['start'].startswith('2026-10-04'))
    targets = {(shift['id'], alex): ['leave']}
    rows = best_replacements(data, targets, set(data['people']))
    assert rows == reference_replacements(data, targets, set(data['people']))
    assert rows[0]['replacement_id'] == taylor and rows[0]['status'] == 'ok'


def test_reference_keeps_peer_restrictions_in_session_supervision_checks(h):
    from tests.test_manual_supervision_staffing import world
    w, _, _, trainee, supervisor, candidate = world(h)
    h.policy({'kind': 'supervision_limit', 'mode': 'capacity', 'maximum': 2})
    h.policy({'kind': 'restriction', 'weekdays': list(range(7)), 'start_time': '00:00', 'end_time': '24:00'},
             {'person_id': trainee})
    source = h.snapshot(w)
    shift = source['shifts'][0]
    shift['participants'].append({**shift['participants'][0], 'id': 'kept-trainee', 'person_id': trainee})
    source['grants'] = [g for g in source['grants'] if g['person_id'] != candidate] + [
        {**g, 'id': 'candidate-' + g['id'], 'person_id': candidate}
        for g in source['grants'] if g['person_id'] == supervisor]
    targets = {(shift['id'], supervisor): ['replace']}
    before = evaluate(source)
    trial = copy.deepcopy(source)
    trial['shifts'][0]['participants'][0]['person_id'] = candidate
    after = evaluate(trial)
    assert not [f for f in staffing._new_candidate_findings(before, after) if f['severity'] == 'error']
    rows = best_replacements(source, targets, {candidate})
    assert rows[0]['replacement_id'] == candidate and rows[0]['status'] == 'ok'
    assert reference_replacements(source, targets, {candidate}) == rows


def ranking_rota(h):
    data, targets, people = department_rota(h)
    data = with_person_rules(data, Random(7))
    selected = sorted(targets.items(), key=lambda item: (item[0][0], data['people'][item[0][1]]['display_name'].casefold()))
    return data, dict(selected[::10]), people


def test_replace_scale_workload_is_independent_of_person_ids(h, monkeypatch):
    from tests import scheduler_departments

    data, _ = scheduler_departments.department(h, 11, people=150, days=28, rules='full', leave_rate=0.05, clinics=6)
    people = sorted(data['people'], key=lambda pid: data['people'][pid]['display_name'])
    assert len({p['display_name'].casefold() for p in data['people'].values()}) == len(people)
    signatures = []
    for ordering in (people, people[::-1]):
        encoded = json.dumps(data, default=str)
        for n, pid in enumerate(ordering):
            encoded = encoded.replace(pid, f'relabel-person-{n:03}')
        renamed = json.loads(encoded)
        monkeypatch.setattr(scheduler_departments, 'department', lambda *args, **kwargs: (copy.deepcopy(renamed), None))
        source, targets, _ = ranking_rota(h)
        names = {pid: person['display_name'] for pid, person in source['people'].items()}
        workload = {'seats': [(s['id'], [names[p['person_id']] for p in s['participants']]) for s in source['shifts']],
                    'scopes': [p['definition']['scope'] for p in source['policies']],
                    'targets': [(sid, names[pid], keys) for (sid, pid), keys in targets.items()]}
        encoded = json.dumps(workload, sort_keys=True)
        for n, pid in enumerate(sorted(names, key=names.get)):
            encoded = encoded.replace(pid, f'semantic-person-{n:03}')
        signatures.append(hashlib.sha256(encoded.encode()).hexdigest())
    assert signatures[0] == signatures[1], 'Changing identity IDs must preserve the seeded staffing, rules and selected places'


def ranking_measurement(h, monkeypatch):
    """Department-scale ranking: one index, lazy pair baselines and current-state quota/person baselines,
    then candidate-local trials against the shared validator."""
    data, targets, people = ranking_rota(h)
    indexes, sizes = [], []
    real_index, real_evaluate = staffing.people_index, staffing.evaluate
    monkeypatch.setattr(issues, 'people_index', lambda source: indexes.append(1) or real_index(source))
    monkeypatch.setattr(staffing, 'evaluate', lambda d, **kwargs: (sizes.append(d.get('_staffing_baseline'))
        if d.get('_staffing_baseline') else None) or real_evaluate(d, **kwargs))
    expected = reference_replacements(data, targets, people)
    rules = active_rules(data['policies'])
    shared = sum(named_people(p['definition']['scope']) is None for p in rules)
    samples, observations = [], []
    # Isolate the computation thread and sample three stateless previews.
    for _ in range(3):
        indexes.clear()
        sizes.clear()
        started = time.thread_time()
        rows = best_replacements(data, targets, people)
        samples.append(time.thread_time() - started)
        print('replace ranking', {'cpu_seconds': samples[-1], 'places': len(targets), 'indexes': len(indexes)})
        observations.append((rows, indexes[:], sizes[:]))
    return len(targets), expected, observations, shared, len(rules), min(samples)


@pytest.fixture(scope='module')
def replace_ranking_measurement(scale_harness):
    with pytest.MonkeyPatch.context() as monkeypatch:
        return ranking_measurement(scale_harness('h'), monkeypatch)


def test_a_50_issue_replace_ranks_once_per_preview_within_budget(replace_ranking_measurement):
    targets, expected, observations, shared, rules, _ = replace_ranking_measurement
    assert targets == 50
    for rows, indexes, sizes in observations:
        assert rows == expected
        filled = sum(r['replacement_id'] is not None for r in rows)
        assert filled == sum(r['replacement_id'] is not None for r in expected) > 40
        assert indexes == [1], 'the rota is indexed once per preview, not once per place'
        assert 0 < sizes.count('baseline') <= targets, \
            'the reduced safety baseline runs at most once per pair that reaches a trial'
        assert sizes.count('covered_baseline') <= targets, \
            'the covered safety baseline never runs more than once per pair'
        assert 0 < sizes.count('quota_baseline') <= sizes.count('baseline'), \
            'quota baselines are current for each assignment state and candidate set'


@pytest.mark.cpu_budget
def test_a_50_issue_replace_ranking_time(replace_ranking_measurement):
    *_, elapsed = replace_ranking_measurement
    assert elapsed < python_budget(1), f'{elapsed:.2f} CPU s'
# Include this file in hosted supervision and team-lead qualification.
