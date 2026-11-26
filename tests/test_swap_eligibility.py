"""Staff swap previews use the preparation validator before saving consent."""
import ast
import copy
import inspect
import json
import time
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import swaps
from packages.domain.validation import evaluate
from packages.domain import quotas, resources, validation
from .governance_support import identity, transition
from .test_resources_calendars import attach, calendar
from .test_staff_swaps import accepted, fixed_clock, manual_swap_workflow, offered, ordered_clock, source_pair
from .test_performance_budget import python_budget


def recipient(options, person_id):
    return next(row for row in options['recipients'] if row['id'] == person_id)


def counts(h):
    with transaction(h.app.state.engine) as db:
        return [db.scalar(select(func.count()).select_from(model)) for model in
                (m.SwapRequest, m.SwapEvent, m.Outbox, m.Workspace, m.Shift, m.Assignment)]


def offer_payload(h, publication_id, assignment_id, kind='exchange'):
    return {'kind': kind, 'publication_id': publication_id, 'assignment_id': assignment_id,
            'recipient_person_id': h.seed['people'][1], 'expires_at': (m.now() + timedelta(days=1)).isoformat(),
            'reason': 'Synthetic eligibility check'}


def publish_pair(h, workspace):
    publication_id = h.publish(workspace)['publication_id']
    source = h.get('coordinator', '/api/publications/' + publication_id)['snapshot']
    ids = [next(p['id'] for shift in source['shifts'] for p in shift['participants']
                if p['person_id'] == person_id) for person_id in h.seed['people'][:2]]
    return publication_id, *ids, source


@pytest.mark.parametrize('rule_kind,code', [('min_rest', 'MIN_REST'),
    ('forbidden_transition', 'FORBIDDEN_TRANSITION'), ('quota_maximum', 'QUOTA_ABOVE_MAXIMUM'),
    ('quota_minimum', 'QUOTA_BELOW_MINIMUM'), ('max_duties', 'MAX_DUTIES'), ('peer_unavailable', 'UNAVAILABLE'),
    ('location_capacity', 'RESOURCE_CAPACITY')])
def test_exchange_audience_matches_full_validation(h, monkeypatch, rule_kind, code):
    proposer, blocked, eligible = h.seed['people']
    identity(h, 'staff_two', ['staff'], blocked)
    identity(h, 'staff_three', ['staff'], eligible)
    evening, morning = [h.configure('assignment_types', {'name': name, 'starts_on': '2020-01-01',
        'definition': {}})['record']['id'] for name in ('Synthetic offered evening', 'Synthetic next morning')]
    w = h.shift(h.workspace(), '2026-10-04T16:00:00+03:00', '2026-10-04T20:00:00+03:00', typ=evening)['workspace']
    w = h.shift(w, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', people=[blocked], typ=morning)['workspace']
    w = h.shift(w, '2026-10-05T16:00:00+03:00', '2026-10-05T20:00:00+03:00', people=[eligible])['workspace']
    dirty = rule_kind in {'peer_unavailable', 'location_capacity'}
    if dirty:
        w = h.shift(w, '2026-10-04T16:00:00+03:00', '2026-10-04T20:00:00+03:00', people=[blocked],
                    location=1 if rule_kind == 'peer_unavailable' else 0)['workspace']
    for day in (8, 9):
        w = h.shift(w, f'2026-10-{day:02}T08:00:00+03:00', f'2026-10-{day:02}T12:00:00+03:00', people=[blocked])['workspace']
    with transaction(h.app.state.engine, write=True) as db:
        neighbour = db.scalar(select(m.Shift).where(m.Shift.workspace_id == w['id'], m.Shift.assignment_type_id == morning))
        neighbour.locked = True  # Remains a rule target but cannot itself be offered in return.
        if dirty:
            peer = db.scalar(select(m.Shift).where(m.Shift.workspace_id == w['id'],
                m.Shift.assignment_type_id == h.seed['type'], m.Shift.start == swaps.aware('2026-10-04T16:00:00+03:00')))
            peer.locked = True
        if rule_kind == 'location_capacity':
            db.get(m.Location, h.seed['locations'][0]).capacity = 2
    publication_id, first, _, _ = publish_pair(h, w)
    scope = {'person_id': blocked}
    if rule_kind == 'peer_unavailable':
        h.command('coordinator', 'availability.record', {'person_id': blocked, 'kind': 'recorded_leave',
            'start': '2026-10-04T16:00:00+03:00', 'end': '2026-10-04T20:00:00+03:00',
            'private_reason': 'Synthetic unchanged peer blocker'})
    elif rule_kind == 'location_capacity':
        with transaction(h.app.state.engine, write=True) as db:
            db.get(m.Location, h.seed['locations'][0]).capacity = 1
    elif rule_kind == 'min_rest':
        rule = {'kind': rule_kind, 'hours': 16}
        scope['assignment_type_id'] = morning
    elif rule_kind == 'forbidden_transition':
        rule = {'kind': rule_kind, 'from_type_ids': [evening], 'to_type_ids': [morning]}
        scope['assignment_type_id'] = morning
    elif rule_kind.startswith('quota_'):
        rule = {'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-06',
                'metric': 'duties', 'comparison': rule_kind.removeprefix('quota_'), 'amount': 1}
        if rule_kind == 'quota_minimum':
            scope = {'person_id': proposer}
    else:
        rule = {'kind': 'max_duties', 'maximum': 1, 'window': 'week'}
        scope['conditions'] = {'conditions': [{'field': 'assignment_type_ids', 'values': [evening, morning]}]}
    if not dirty:
        h.policy(rule, scope)
    snapshots, original_snapshot = [], swaps.domain_snapshot

    def capture_snapshot(*args, **kwargs):
        source = original_snapshot(*args, **kwargs)
        snapshots.append(copy.deepcopy(source))
        return source

    monkeypatch.setattr(swaps, 'domain_snapshot', capture_snapshot)
    expires = m.now() + timedelta(days=1)
    with transaction(h.app.state.engine) as db:
        publication = db.get(m.Publication, publication_id)
        published_source = copy.deepcopy(publication.snapshot)
        swaps.Eligibility(db, publication)
        # The oracle uses raw current inputs and checks the complete rota, independently of trial preparation.
        source = snapshots[0]
        assert all(shift['workspace_id'] == source['workspace']['id'] for shift in source['shifts']
                   if shift['id'] in source['focus_ids'])
        checked = evaluate(source)
        assert checked['valid'] == (not dirty), checked['findings']
        expected, rejected_codes = set(), set()
        for person_id in (blocked, eligible):
            for shift in source['shifts']:
                if shift['locked'] or shift['cancelled']:
                    continue
                for participant in shift['participants']:
                    if participant['person_id'] != person_id or swaps.aware(participant['start']) < expires:
                        continue
                    trial = copy.deepcopy(source)
                    for duty in trial['shifts']:
                        for p in duty['participants']:
                            if p['id'] == first:
                                p['person_id'] = person_id
                            elif p['id'] == participant['id']:
                                p['person_id'] = proposer
                    result = evaluate(trial)
                    if result['valid']:
                        expected.add(person_id)
                    elif person_id == blocked:
                        rejected_codes.update(f['code'] for f in result['findings'] if f['severity'] == 'error')
        assert expected == (set() if dirty else {eligible}) and code in rejected_codes
        assert swaps.eligible_staff(db, publication, first, expires, 'exchange') == expected
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Publication, publication_id).snapshot == published_source


def test_pair_reuses_person_rows_across_repeated_scopes_without_changing_snapshot(h, monkeypatch):
    proposer, blocked, eligible = h.seed['people']
    w = h.shift(h.workspace())['workspace']
    w = h.shift(w, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', people=[blocked])['workspace']
    w = h.shift(w, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', people=[eligible], location=1)['workspace']
    publication_id, first, second, published = publish_pair(h, w)
    h.command('coordinator', 'availability.record', {'person_id': blocked, 'kind': 'actual_absence',
        'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00',
        'private_reason': 'Synthetic recipient-specific blocker'})
    snapshots, contexts = [], []
    original_snapshot, original_evaluate = swaps.domain_snapshot, swaps.evaluate

    def snapshot(*args, **kwargs):
        data = original_snapshot(*args, **kwargs)
        snapshots.append(copy.deepcopy(data))
        return data

    def check(data, **kwargs):
        contexts.append({name: data[name] for name in validation.PERSON_ROWS if name in data})
        return original_evaluate(data, **kwargs)

    monkeypatch.setattr(swaps, 'domain_snapshot', snapshot)
    monkeypatch.setattr(swaps, 'evaluate', check)
    with transaction(h.app.state.engine) as db:
        preview = swaps.Eligibility(db, db.get(m.Publication, publication_id))
        unchanged = {name: copy.deepcopy(preview.data[name]) for name in ('shifts', *validation.PERSON_ROWS)
                     if name in preview.data}
        outcomes, rows = [], []
        for return_id, recipient_id in ((None, blocked), (second, blocked), (None, eligible), (None, blocked)):
            actual = preview.pair(first, return_id, recipient_id=recipient_id)
            rows.append(contexts[-1])
            trial = copy.deepcopy(snapshots[0])
            replacements = {first: recipient_id, **({return_id: proposer} if return_id else {})}
            for shift in trial['shifts']:
                for participant in shift['participants']:
                    participant['person_id'] = replacements.get(participant['id'], participant['person_id'])
            oracle = evaluate(trial)
            assert actual['eligible'] is oracle['valid']
            assert {f['code'] for f in actual['findings']} == {f['code'] for f in oracle['findings'] if f['severity'] == 'error'}
            outcomes.append(actual['eligible'])
        assert outcomes == [False, False, True, False]
        assert [{r['person_id'] for r in row['memberships']} for row in rows] == [
            {proposer, blocked}, {proposer, blocked, eligible}, {proposer, eligible}, {proposer, blocked}]
        assert all(rows[0][name] is rows[-1][name] for name in rows[0])
        assert unchanged == {name: preview.data[name] for name in unchanged}
    assert h.get('coordinator', '/api/publications/' + publication_id)['snapshot'] == published


def test_prepared_swap_rule_priorities_preserve_all_grouped_findings(monkeypatch):
    sources = {'everyone': {'line': 0, 'tier': 1}, 'person': {'line': 1, 'tier': 3}}
    policies = [{'id': 'q', 'template_line': {'line': line}, '_catalogue_sources': sources,
                 'definition': {'scope': {}}} for line in range(1000)]
    findings = [{'code': 'QUOTA_BELOW_MINIMUM', 'rule_id': 'q', 'shift_id': 's', 'person_id': 'p',
                 'severity': 'error', 'start': '2026-10-04', 'end': '2026-10-11',
                 'message': f'Synthetic quota line {line}', 'detail': {'line': line, 'difference': -1}}
                for line in (0, 1)]
    expected = validation.grouped_rule_findings(findings, policies)
    tiers = validation.policy_group_tiers(policies)

    def no_preparation(_policies):
        pytest.fail('A swap trial rebuilt the immutable rule priorities')

    monkeypatch.setattr(validation, 'policy_group_tiers', no_preparation)
    for _ in range(2):
        assert validation.grouped_rule_findings(findings, policies, tiers=tiers) == expected
    assert expected[0]['detail']['line'] == 1
    assert expected[0]['detail']['derived_findings'][0]['detail']['line'] == 0


def test_options_marks_recipients_without_disclosing_return_duties(h):
    pid, first, second, _ = source_pair(h)
    before = counts(h)
    path = '/api/swaps/options?assignment_id=' + first
    directory = h.get('staff', path)
    assert directory['assignment_id'] == first and directory['recipient_id'] is None
    assert all(set(row) == {'id', 'display_name', 'eligible', 'message', 'findings'} for row in directory['recipients'])
    options = h.get('staff', path + '&recipient_id=' + h.seed['people'][1])
    assert options['assignment_id'] == first
    assert options['recipient_id'] == h.seed['people'][1]
    assert recipient(options, h.seed['people'][1]) == {
        'id': h.seed['people'][1], 'display_name': recipient(options, h.seed['people'][1])['display_name'],
        'eligible': True, 'message': None, 'findings': []}
    assert recipient(options, h.seed['people'][2])['eligible'] is False
    unavailable = recipient(h.get('staff', path + '&recipient_id=' + h.seed['people'][2]), h.seed['people'][2])
    assert unavailable['eligible'] is False and unavailable['message']
    assert unavailable['findings'][0]['code'] == 'SWAP_RETURN_REQUIRED'
    assert second not in str(options) and 'private_reason' not in str(options) and 'type_snapshot' not in str(options)
    assert h.get('staff', '/api/swaps/options')['assignment_id'] == first
    h.get('staff', '/api/swaps/options?assignment_id=' + second, status=404)
    h.get('staff', path + '&recipient_id=' + h.seed['people'][0], status=404)
    h.get('staff', path + '&recipient_id=' + m.uid(), status=404)
    h.get('staff', '/api/swaps/options?recipient_id=' + h.seed['people'][1], status=422)
    unbound = m.uid()
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.Person(id=unbound, organisation_id=db.get(m.Person, h.seed['people'][0]).organisation_id,
                        display_name='Synthetic staff name without participation'))
    h.get('staff', path + '&recipient_id=' + unbound, status=404)
    failed = h.command('staff', 'swap.offer', {**offer_payload(h, pid, first),
                       'recipient_person_id': h.seed['people'][2]}, status=422)['error']
    assert failed['code'] == 'SWAP_INELIGIBLE'
    assert failed['details']['findings'] == unavailable['findings']
    assert counts(h) == before


def assert_private_time_hidden(body):
    serialized = json.dumps(body)
    for private in ('UNAVAILABLE', 'RESTRICTION', 'is unavailable at that time.', 'has protected time then.'):
        assert private not in serialized


def test_staff_options_and_refusals_make_leave_and_protection_indistinguishable(h):
    publication, first, _, _ = source_pair(h, third=True)
    proposer, on_leave, protected = h.seed['people']
    rows = []
    for person, login in ((on_leave, 'staff_two'), (protected, 'staff_three')):
        offer = h.command('staff', 'swap.offer', {**offer_payload(h, publication, first, 'give'),
            'recipient_person_id': person})['swap']
        rows.append(transition(h, offer, 'accept', login)['swap'])
    h.command('coordinator', 'availability.record', {
        'person_id': on_leave, 'kind': 'recorded_leave', 'private_reason': 'Synthetic leave note',
        'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00'})
    h.policy({'kind': 'restriction', 'weekdays': [6], 'start_time': '08:00', 'end_time': '12:00'},
             {'person_id': protected})

    path = f'/api/swaps/options?assignment_id={first}&kind=give'
    options = h.get('staff', path)
    assert_private_time_hidden(options)
    safe_finding = {'code': 'COLLEAGUE_NOT_FREE', 'message': 'This colleague is not free at that time.'}
    for person in (on_leave, protected):
        assert {key: value for key, value in recipient(options, person).items() if key not in {'id', 'display_name'}} == {
            'eligible': False, 'message': safe_finding['message'], 'findings': [safe_finding]}
    assert options['no_eligible_recipients_reason'] == 'Colleagues are not free at that time.'

    for role in ('coordinator', 'approver'):
        login = role + '_staff'
        identity(h, login, ['staff', role], proposer)
        privileged = h.get(login, path)
        for person, code, suffix in ((on_leave, 'UNAVAILABLE', 'is unavailable at that time.'),
                                     (protected, 'RESTRICTION', 'has protected time then.')):
            choice = recipient(privileged, person)
            assert choice['eligible'] is False and choice['message'].endswith(suffix)
            assert choice['findings'] == [{'code': code, 'message': choice['message']}]
        assert privileged['no_eligible_recipients_reason'] == options['no_eligible_recipients_reason']

    for row in rows:
        thread = '/api/swaps/' + row['id']
        before = h.get('staff', thread)
        result = transition(h, row, 'confirm', status=422)
        assert_private_time_hidden(result)
        assert result['error'] == {'code': 'SWAP_INELIGIBLE', 'message': safe_finding['message'],
                                   'details': {'findings': [safe_finding]}}
        after = h.get('staff', thread)
        assert_private_time_hidden(after)
        assert after == before
        assert_private_time_hidden(h.get('staff', '/api/requests/all?id=' + row['id']))


@pytest.mark.parametrize('code,suffix', [('UNAVAILABLE', 'is unavailable at that time.'),
                                         ('RESTRICTION', 'has protected time then.')])
def test_stored_swap_refusal_is_projected_for_the_current_reader(h, code, suffix):
    row, _ = accepted(h, 'give')
    message = 'The person taking this duty ' + suffix
    detail = {'code': 'SWAP_INELIGIBLE', 'message': message,
              'details': {'findings': [{'code': code, 'message': message, 'person_id': row['recipient_person_id']}]}}
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.SwapEvent(swap_id=row['id'], actor_id=h.seed['identities']['coordinator'],
                           action='prepare_refused', pair_hash=swaps.pair_hash(db.get(m.SwapRequest, row['id'])),
                           reason=message, detail=detail))
    path = '/api/swaps/' + row['id']
    safe = 'This colleague is not free at that time.'
    staff = h.get('staff', path)
    assert_private_time_hidden(staff)
    refusal = next(event for event in staff['events'] if event['action'] == 'prepare_refused')
    assert refusal['reason'] == safe
    assert refusal['detail'] == staff['swap']['prepare_refusal'] == {
        'code': 'SWAP_INELIGIBLE', 'message': safe,
        'details': {'findings': [{'code': 'COLLEAGUE_NOT_FREE', 'message': safe}]}}
    assert_private_time_hidden(h.get('staff', '/api/swaps'))
    history = h.get('staff', '/api/requests/all?id=' + row['id'])
    assert_private_time_hidden(history)
    assert next(event for event in history['history'] if event['action'] == 'swap.prepare_refused')['reason'] == safe
    reviewer = h.get('approver', path)
    assert reviewer['swap']['prepare_refusal'] == detail
    assert next(event for event in reviewer['events'] if event['action'] == 'prepare_refused')['reason'] == message
    with transaction(h.app.state.engine) as db:
        saved = db.scalar(select(m.SwapEvent).where(m.SwapEvent.swap_id == row['id'], m.SwapEvent.action == 'prepare_refused'))
        assert saved.reason == message and saved.detail == detail


@pytest.mark.parametrize('code', sorted(swaps.NOT_FREE_CODES - {swaps.COLLEAGUE_NOT_FREE}))
def test_every_not_free_code_reads_the_same_to_staff(h, monkeypatch, code):
    publication, first, _, _ = source_pair(h, third=True)
    proposer, colleague, _ = h.seed['people']
    evaluate = swaps.evaluate

    def away(*args, **kwargs):
        result = evaluate(*args, **kwargs)
        return {**result, 'session_findings': result['session_findings'] + [
            {'code': code, 'severity': 'error', 'message': 'Private validator detail', 'person_id': colleague}]}
    monkeypatch.setattr(swaps, 'evaluate', away)

    path = f'/api/swaps/options?assignment_id={first}&kind=give'
    safe = {'code': 'COLLEAGUE_NOT_FREE', 'message': 'This colleague is not free at that time.'}
    options = h.get('staff', path)
    assert code not in json.dumps(options) and 'Private validator detail' not in json.dumps(options)
    for row in options['recipients']:
        assert {key: value for key, value in row.items() if key not in {'id', 'display_name'}} == {
            'eligible': False, 'message': safe['message'], 'findings': [safe]}
    assert options['no_eligible_recipients_reason'] == 'Colleagues are not free at that time.'
    refusal = h.command('staff', 'swap.offer', offer_payload(h, publication, first, 'give'), status=422)
    assert refusal['error'] == {'code': 'SWAP_INELIGIBLE', 'message': safe['message'], 'details': {'findings': [safe]}}

    identity(h, 'coordinator_staff', ['staff', 'coordinator'], proposer)
    assert [f['code'] for f in recipient(h.get('coordinator_staff', path), colleague)['findings']] == [code]
    # Only a coordinator or approver reads the specific finding; other team readers who are staff read what staff read.
    identity(h, 'lead_staff', ['staff', 'programme_lead', 'auditor'], proposer)
    assert h.get('lead_staff', path)['recipients'] == options['recipients']


def test_audience_words_cover_codes_from_the_swap_validator():
    assert {'UNAVAILABLE', 'RESTRICTION', 'EXTERNAL_ROTATION', 'ROTATION_LEAVE_NEEDS_APPROVAL',
            'ROTATION_LEAVE_NOT_ALLOWED'} <= swaps.NOT_FREE_CODES
    # Read the producers used by partial validation. Some warnings share these producers with errors.
    def codes(expression):
        if isinstance(expression, ast.IfExp):
            return codes(expression.body) | codes(expression.orelse)
        if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
            return {expression.value}
        return set()

    known = set(quotas.QUOTA_DEVIATION_CODES.values()) | {swaps.COLLEAGUE_NOT_FREE}
    for fn in (validation.evaluate, validation.validate_session_setup, validation.location_validity_findings,
               validation.rotation_leave_findings, quotas.evaluate_quotas, resources.evaluate_resources,
               resources.check_calendar, swaps.Eligibility):
        for node in ast.walk(ast.parse(inspect.getsource(fn))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in {'add', 'violation', 'unresolved'} and node.args):
                known.update(codes(node.args[0]))
            if isinstance(node, ast.Yield) and isinstance(node.value, ast.Tuple):
                known.update(codes(node.value.elts[0]))
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and key.value == 'code':
                        known.update(codes(value))

    assert known <= swaps.AUDIENCE_WORDS.keys()
    for code in known:
        assert swaps.nobody_reason([{'code': code, 'message': 'Private validator detail'}]) == swaps.AUDIENCE_WORDS[code]
    assert swaps.nobody_reason([{'code': 'FUTURE_CHECK', 'message': 'Private validator detail'}]) == (
        'Nobody can take this duty under the current rules.')


def test_received_options_checks_only_own_return_duties_and_current_availability(h):
    pid, first, second, _ = source_pair(h)
    row = offered(h, pid, first)
    path = '/api/swaps/options?swap_id=' + row['id']
    options = h.get('staff_two', path)
    assert not options['recipients']
    assert [(a['assignment_id'], a['eligible']) for a in options['assignments']] == [(second, True)]
    h.get('staff', path, status=403)
    h.get('staff_three', path, status=404)
    h.get('coordinator', path, status=403)
    h.get('staff_two', path + '&assignment_id=' + second, status=422)
    h.get('staff_two', path + '&recipient_id=' + h.seed['people'][0], status=422)
    h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][1],
        'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00',
        'kind': 'actual_absence', 'private_reason': 'PRIVATE synthetic absence note'})
    blocked = h.get('staff_two', path)['assignments'][0]
    assert blocked['eligible'] is False and blocked['message']
    assert 'COLLEAGUE_NOT_FREE' in {f['code'] for f in blocked['findings']}
    assert all(set(f) == {'code', 'message'} for f in blocked['findings'])
    assert 'PRIVATE' not in str(blocked) and 'private_reason' not in str(blocked)


def test_give_away_recipient_without_return_duty_is_eligible_and_received_options_need_no_assignment(h):
    pid, first, _, _ = source_pair(h)
    path = '/api/swaps/options?assignment_id=' + first + '&recipient_id=' + h.seed['people'][2]
    exchange = recipient(h.get('staff', path), h.seed['people'][2])
    assert exchange['eligible'] is False and exchange['findings'][0]['code'] == 'SWAP_RETURN_REQUIRED'
    give = h.get('staff', path + '&kind=give')
    assert give['kind'] == 'give' and recipient(give, h.seed['people'][2])['eligible'] is True
    before = counts(h)
    with transaction(h.app.state.engine) as db:
        preview = swaps.Eligibility(db, db.get(m.Publication, pid))
        assert preview.pair(first, None, recipient_id=h.seed['people'][2]) == preview.recipient(
            first, h.seed['people'][2], kind='give')
        assert preview.recipient(first, h.seed['people'][2], kind='give')['eligible'] is True
        assert preview.recipient(first, h.seed['people'][2])['eligible'] is False
    assert counts(h) == before
    row = h.command('staff', 'swap.offer', {**offer_payload(h, pid, first, 'give'),
        'recipient_person_id': h.seed['people'][2]})['swap']
    received = '/api/swaps/options?swap_id=' + row['id']
    options = h.get('staff_three', received)
    assert options['kind'] == 'give' and options['assignments'] == [] and options['recipients'] == []
    assert options['eligible'] is True and options['message'] is None and options['findings'] == []
    assert options['assignments_partial'] is False
    h.get('staff_three', received + '&kind=exchange', status=422)
    h.get('staff_two', received, status=404)
    row = transition(h, row, 'accept', 'staff_three')['swap']
    assert transition(h, row, 'confirm')['swap']['status'] == 'accepted'


def test_received_give_away_options_recheck_single_transfer_and_do_not_disclose_recipient_duties(h):
    pid, first, second, _ = source_pair(h)
    row = offered(h, pid, first, kind='give')
    path = '/api/swaps/options?swap_id=' + row['id']
    assert h.get('staff_two', path)['assignments'] == []
    h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][1],
        'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00',
        'kind': 'actual_absence', 'private_reason': 'PRIVATE synthetic give-away blocker'})
    before = counts(h)
    options = h.get('staff_two', path)
    assert options['assignments'] == [] and options['eligible'] is False and options['message']
    assert 'COLLEAGUE_NOT_FREE' in {f['code'] for f in options['findings']}
    assert all(set(f) == {'code', 'message'} for f in options['findings'])
    assert second not in str(options) and 'PRIVATE' not in str(options) and 'private_reason' not in str(options)
    assert transition(h, row, 'accept', 'staff_two', status=422)['error']['details']['findings'] == options['findings']
    assert counts(h) == before


def test_return_before_consent_expiry_is_ineligible_while_a_later_return_allows_the_offer(h):
    identity(h, 'staff_two', ['staff'], h.seed['people'][1])
    w = h.shift(h.workspace(start='2026-09-19'))['workspace']
    w = h.shift(w, '2026-09-19T08:00:00+03:00', '2026-09-19T12:00:00+03:00', people=[h.seed['people'][1]])['workspace']
    w = h.shift(w, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', people=[h.seed['people'][1]])['workspace']
    pid, first, _, _ = publish_pair(h, w)
    row = offered(h, pid, first)
    choices = h.get('staff_two', '/api/swaps/options?swap_id=' + row['id'])['assignments']
    early = next(a for a in choices if a['start'].startswith('2026-09-19'))
    later = next(a for a in choices if a['start'].startswith('2026-10-05'))
    assert early['eligible'] is False and early['message']
    assert 'SWAP_EXPIRY' in {f['code'] for f in early['findings']}
    assert later['eligible'] is True
    before = counts(h)
    assert transition(h, row, 'counteroffer', 'staff_two', {'assignment_id': early['assignment_id'],
        'reason': 'This duty starts before consent expires'}, status=422)['error']['code'] == 'SWAP_EXPIRY'
    assert counts(h) == before
    assert transition(h, row, 'counteroffer', 'staff_two', {'assignment_id': later['assignment_id'],
        'reason': 'This later duty remains consentable'})['swap']['status'] == 'counteroffered'


@pytest.mark.parametrize('kind,stage', [('exchange', 'offer'), ('exchange', 'counteroffer'), ('exchange', 'confirm'),
    ('give', 'offer'), ('give', 'accept'), ('give', 'confirm')])
@pytest.mark.postgres
def test_changed_inputs_refuse_consent_atomically_on_postgres(pg, kind, stage):
    h = pg
    pid, first, second, _ = source_pair(h)
    row = offered(h, pid, first, kind=kind) if stage != 'offer' else None
    if stage == 'confirm':
        row = (transition(h, row, 'accept', 'staff_two') if kind == 'give' else
               transition(h, row, 'counteroffer', 'staff_two',
                          {'assignment_id': second, 'reason': 'Exact synthetic return'}))['swap']
    h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][1],
        'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00',
        'kind': 'actual_absence', 'private_reason': 'PRIVATE synthetic consent blocker'})
    before = counts(h)
    if stage == 'offer':
        result = h.command('staff', 'swap.offer', offer_payload(h, pid, first, kind), status=422)
    else:
        result = transition(h, row, stage, 'staff_two' if stage in {'counteroffer', 'accept'} else 'staff',
            {'assignment_id': second, 'reason': 'Current eligibility must be rechecked'} if stage == 'counteroffer'
            else None, status=422)
    error = result['error']
    assert error['code'] == 'SWAP_INELIGIBLE'
    assert 'COLLEAGUE_NOT_FREE' in {f['code'] for f in error['details']['findings']}
    assert all(set(f) == {'code', 'message'} for f in error['details']['findings'])
    assert 'PRIVATE' not in str(error)
    assert counts(h) == before
    if row:
        assert h.get('staff', '/api/swaps/' + row['id'])['swap'] == row


@pytest.mark.parametrize('kind', ['exchange', 'give'])
def test_undo_cannot_reopen_withdrawn_accepted_pair_after_inputs_make_it_ineligible(h, kind):
    row, _ = accepted(h, kind)
    withdrawn = transition(h, row, 'withdraw')['swap']
    h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][1],
        'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00',
        'kind': 'actual_absence', 'private_reason': 'PRIVATE synthetic reopening blocker'})
    before = counts(h)
    error = transition(h, withdrawn, 'undo', status=422)['error']
    assert error['code'] == 'SWAP_INELIGIBLE'
    assert 'COLLEAGUE_NOT_FREE' in {f['code'] for f in error['details']['findings']}
    assert counts(h) == before
    assert h.get('staff', '/api/swaps/' + withdrawn['id'])['swap'] == withdrawn


@pytest.mark.parametrize('severity', ['mandatory', 'advisory', 'advisory_workload'])
@pytest.mark.parametrize('kind', ['exchange', 'give'])
def test_current_restrictions_block_mandatory_but_allow_advisory_pairs(h, severity, kind):
    pid, first, second, _ = source_pair(h)
    scope = {'person_id': h.seed['people'][1]}
    if severity == 'advisory_workload':
        scope['weekdays'] = [6]
        h.policy({'kind': 'max_duties', 'maximum': 0, 'window': 'day'}, scope, severity='advisory')
    else:
        rule = {'kind': 'restriction', 'weekdays': [6], 'start_time': '08:00', 'end_time': '12:00'}
        create = h.stored_advisory_safety if severity == 'advisory' else h.policy
        create(rule, scope)
    choice = recipient(h.get('staff', '/api/swaps/options?assignment_id=' + first + '&recipient_id=' + scope['person_id']
        + '&kind=' + kind), scope['person_id'])
    if severity == 'advisory_workload':
        assert choice['eligible'] is True
        row = offered(h, pid, first, kind=kind)
        row = (transition(h, row, 'accept', 'staff_two') if kind == 'give' else
               transition(h, row, 'counteroffer', 'staff_two', {'assignment_id': second, 'reason': 'Advisory is nonblocking'}))['swap']
        row = transition(h, row, 'confirm')['swap']
        prepared = transition(h, row, 'prepare', 'coordinator')
        assert prepared['validation']['valid']
        assert any(f['code'] == 'MAX_DUTIES' and f['severity'] == 'warning' for f in prepared['validation']['findings'])
        return

    # Safety kinds are always mandatory. A stored advisory restriction still blocks the swap.
    assert choice['eligible'] is False and choice['message'] == 'This colleague is not free at that time.'
    assert {'code': 'COLLEAGUE_NOT_FREE', 'message': choice['message']} in choice['findings']
    assert h.command('staff', 'swap.offer', offer_payload(h, pid, first, kind), status=422)['error']['code'] == 'SWAP_INELIGIBLE'


@pytest.mark.parametrize('changed_input,expected_code', [('eligibility', 'NOT_ELIGIBLE'), ('calendar', 'CALENDAR_CLOSED')])
@pytest.mark.parametrize('kind', ['exchange', 'give'])
def test_early_check_and_full_preparation_refuse_the_same_current_input(h, changed_input, expected_code, kind):
    pid, first, second, _ = source_pair(h)
    row = offered(h, pid, first, kind=kind)
    row = (transition(h, row, 'accept', 'staff_two') if kind == 'give' else
           transition(h, row, 'counteroffer', 'staff_two', {'assignment_id': second, 'reason': 'Exact synthetic return'}))['swap']
    row = transition(h, row, 'confirm')['swap']
    if changed_input == 'eligibility':
        with transaction(h.app.state.engine, write=True) as db:
            for eligibility in db.scalars(select(m.Eligibility).where(m.Eligibility.person_id == h.seed['people'][1])):
                eligibility.ends_on = date(2026, 10, 4)
    else:
        attach(h, calendar(h, week=[]))
    with transaction(h.app.state.engine) as db:
        result = swaps.Eligibility(db, db.get(m.Publication, pid)).pair(first, second if kind == 'exchange' else None,
            recipient_id=h.seed['people'][1])
    assert result['eligible'] is False and expected_code in {f['code'] for f in result['findings']}
    before = counts(h)
    failed = transition(h, row, 'prepare', 'coordinator', status=422)['error']
    assert failed['code'] == 'SWAP_INVALID'
    assert expected_code in {f['code'] for f in failed['details']['findings']}
    assert counts(h) == before


def test_return_options_keep_nearby_commitments_for_rest_without_rejecting_a_different_valid_return(h):
    identity(h, 'staff_two', ['staff'], h.seed['people'][1])
    w = h.shift(h.workspace())['workspace']
    w = h.shift(w, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', people=[h.seed['people'][1]])['workspace']
    w = h.shift(w, '2026-10-04T13:00:00+03:00', '2026-10-04T17:00:00+03:00', people=[h.seed['people'][1]])['workspace']
    pid, first, _, _ = publish_pair(h, w)
    h.policy({'kind': 'min_rest', 'hours': 12})
    give = recipient(h.get('staff', '/api/swaps/options?kind=give&assignment_id=' + first
        + '&recipient_id=' + h.seed['people'][1]), h.seed['people'][1])
    assert give['eligible'] is False and 'MIN_REST' in {f['code'] for f in give['findings']}
    assert h.command('staff', 'swap.offer', offer_payload(h, pid, first, 'give'), status=422)['error']['code'] == 'SWAP_INELIGIBLE'
    row = offered(h, pid, first)
    choices = h.get('staff_two', '/api/swaps/options?swap_id=' + row['id'])['assignments']
    bad = next(a for a in choices if a['start'].startswith('2026-10-05'))
    good = next(a for a in choices if a['start'].startswith('2026-10-04'))
    assert bad['eligible'] is False and 'MIN_REST' in {f['code'] for f in bad['findings']}
    assert good['eligible'] is True
    assert transition(h, row, 'counteroffer', 'staff_two', {'assignment_id': bad['assignment_id'],
        'reason': 'This return leaves only one hour rest'}, status=422)['error']['code'] == 'SWAP_INELIGIBLE'
    row = transition(h, row, 'counteroffer', 'staff_two', {'assignment_id': good['assignment_id'], 'reason': 'Valid alternative return'})['swap']
    row = transition(h, row, 'confirm')['swap']
    assert transition(h, row, 'prepare', 'coordinator')['validation']['valid']


@pytest.mark.parametrize('neighbor', ['before', 'after'])
@pytest.mark.parametrize('rest_hours', [(12,), (1, 12)])
def test_rest_rule_scoped_to_unchanged_neighbor_still_blocks_exact_pair(h, monkeypatch, neighbor, rest_hours):
    identity(h, 'staff_two', ['staff'], h.seed['people'][1])
    typ = h.configure('assignment_types', {'name': 'Synthetic neighboring duty', 'starts_on': '2020-01-01',
                                          'definition': {}})['record']
    w = h.shift(h.workspace())['workspace']
    w = h.shift(w, '2026-10-06T08:00:00+03:00', '2026-10-06T12:00:00+03:00', people=[h.seed['people'][1]])['workspace']
    start, end = ('02:00', '06:00') if neighbor == 'before' else ('13:00', '17:00')
    w = h.shift(w, '2026-10-04T' + start + ':00+03:00', '2026-10-04T' + end + ':00+03:00',
                people=[h.seed['people'][1]], typ=typ['id'])['workspace']
    pid, first, _, source = publish_pair(h, w)
    second = next(p['id'] for shift in source['shifts'] if shift['start'].startswith('2026-10-06')
                  for p in shift['participants'])
    lines = [{'id': m.uid(), 'who': {'conditions': [{'field': 'person_ids', 'values': [h.seed['people'][1]]}]},
              'where': {'conditions': [{'field': 'assignment_type_ids',
                         'values': [typ['id'] if hours == 12 else h.seed['type']]}]},
              'numbers': {'min': {'value': hours}}} for hours in rest_hours]
    rule = h.command('coordinator', 'rule.propose', {'catalogue_id': 'rest_between_duties',
        'lines': lines, 'reason': 'Synthetic rest lines; the second line requires the longer gap'})['record']
    h.command('approver', 'policy.activate', {'reason': 'Independent synthetic review'}, rule['id'], rule['revision'])
    selected_lines = set()
    original_selected = validation.workload_selected

    def selected(data, policy, *args):
        selected_lines.add(policy['template_line']['id'])
        return original_selected(data, policy, *args)

    monkeypatch.setattr(validation, 'workload_selected', selected)
    row = offered(h, pid, first)
    choices = h.get('staff_two', '/api/swaps/options?swap_id=' + row['id'])['assignments']
    bad = next(a for a in choices if a['assignment_id'] == second)
    assert bad['eligible'] is False and 'MIN_REST' in {f['code'] for f in bad['findings']}
    assert selected_lines == {line['id'] for line in lines}
    with transaction(h.app.state.engine) as db:
        preview = swaps.Eligibility(db, db.get(m.Publication, pid))
        assert [p['definition']['rule']['hours'] for p in preview.index['fast']['policies']
                if p['definition']['rule']['kind'] == 'min_rest'] == list(rest_hours)
        data = preview.data
    data = copy.deepcopy(data)
    for shift in data['shifts']:
        for participant in shift['participants']:
            if participant['id'] == first:
                participant['person_id'] = h.seed['people'][1]
            elif participant['id'] == second:
                participant['person_id'] = h.seed['people'][0]
    assert 'MIN_REST' in {f['code'] for f in evaluate(data)['findings']}
    assert transition(h, row, 'counteroffer', 'staff_two', {'assignment_id': second,
        'reason': 'The unchanged neighboring duty requires rest'}, status=422)['error']['code'] == 'SWAP_INELIGIBLE'


def test_next_day_rule_scoped_to_unchanged_target_is_not_hidden_by_intervening_duty(h):
    identity(h, 'staff_two', ['staff'], h.seed['people'][1])
    first_type, target_type = [h.configure('assignment_types', {'name': name, 'starts_on': '2020-01-01',
        'definition': {}})['record'] for name in ('Synthetic evening', 'Synthetic next morning')]
    w = h.shift(h.workspace(), '2026-10-04T16:00:00+03:00', '2026-10-04T20:00:00+03:00', typ=first_type['id'])['workspace']
    w = h.shift(w, '2026-10-04T21:00:00+03:00', '2026-10-04T23:00:00+03:00', people=[h.seed['people'][1]])['workspace']
    w = h.shift(w, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', people=[h.seed['people'][1]], typ=target_type['id'])['workspace']
    w = h.shift(w, '2026-10-06T08:00:00+03:00', '2026-10-06T12:00:00+03:00', people=[h.seed['people'][1]])['workspace']
    pid, first, _, source = publish_pair(h, w)
    second = next(p['id'] for shift in source['shifts'] if shift['start'].startswith('2026-10-06')
                  for p in shift['participants'])
    h.policy({'kind': 'forbidden_transition', 'from_type_ids': [first_type['id']], 'to_type_ids': [target_type['id']]},
             {'person_id': h.seed['people'][1], 'assignment_type_id': target_type['id']})
    row = offered(h, pid, first)
    choices = h.get('staff_two', '/api/swaps/options?swap_id=' + row['id'])['assignments']
    bad = next(a for a in choices if a['assignment_id'] == second)
    assert bad['eligible'] is False and 'FORBIDDEN_TRANSITION' in {f['code'] for f in bad['findings']}
    assert any(a['eligible'] for a in choices)
    assert transition(h, row, 'counteroffer', 'staff_two', {'assignment_id': second,
        'reason': 'Intervening work cannot erase a next-day restriction'}, status=422)['error']['code'] == 'SWAP_INELIGIBLE'


@pytest.mark.parametrize('requirement,expected_code', [('capability', 'CAPABILITY_GAP'), ('supervision', 'SUPERVISION_PAIRING')])
@pytest.mark.parametrize('kind', ['exchange', 'give'])
def test_cover_and_other_participants_supervision_are_checked_before_offer(h, requirement, expected_code, kind):
    identity(h, 'staff_two', ['staff'], h.seed['people'][1])
    definition = {'requirements': [{'minimum': 1, 'capabilities': [{'capability_id': h.seed['capability'], 'minimum': 1}]}]}
    start, end = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'
    people = [h.seed['people'][0]]
    if requirement == 'supervision':
        definition = {'supervision': {'trainee_category_ids': [h.seed['trainee_category']],
            'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 1, 'require_pairing': True}}
        people = [{'person_id': h.seed['people'][0], 'start': start, 'end': end, 'participation': 'supervisor'},
                  {'person_id': h.seed['people'][2], 'start': start, 'end': end, 'participation': 'trainee',
                   'supervisor_person_id': h.seed['people'][0]}]
    typ = h.configure('assignment_types', {'name': 'Synthetic swap cover', 'starts_on': '2020-01-01', 'definition': definition})['record']
    w = h.shift(h.workspace(), typ=typ['id'], people=people)['workspace']
    w = h.shift(w, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', people=[h.seed['people'][1]])['workspace']
    pid, first, _, _ = publish_pair(h, w)
    if requirement in {'capability', 'supervision'}:
        capability_id = h.seed['capability'] if requirement == 'capability' else h.seed['supervisor_capability']
        with transaction(h.app.state.engine, write=True) as db:
            for grant in db.scalars(select(m.CapabilityGrant).where(m.CapabilityGrant.person_id == h.seed['people'][1],
                    m.CapabilityGrant.capability_id == capability_id)):
                grant.revoked = True
    choice = recipient(h.get('staff', '/api/swaps/options?assignment_id=' + first + '&recipient_id=' + h.seed['people'][1]
        + '&kind=' + kind), h.seed['people'][1])
    assert choice['eligible'] is False and expected_code in {f['code'] for f in choice['findings']}
    assert h.command('staff', 'swap.offer', offer_payload(h, pid, first, kind), status=422)['error']['code'] == 'SWAP_INELIGIBLE'


@pytest.mark.parametrize('kind', ['exchange', 'give'])
def test_qualified_supervision_recipient_keeps_pairing_for_offer_and_consent(h, monkeypatch, kind):
    identity(h, 'staff_two', ['staff'], h.seed['people'][1])
    start, end = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'
    definition = {'supervision': {'trainee_category_ids': [h.seed['trainee_category']],
        'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 1, 'require_pairing': True}}
    typ = h.configure('assignment_types', {'name': 'Synthetic qualified swap', 'starts_on': '2020-01-01',
        'definition': definition})['record']
    people = [{'person_id': h.seed['people'][0], 'start': start, 'end': end, 'participation': 'supervisor'},
              {'person_id': h.seed['people'][2], 'start': start, 'end': end, 'participation': 'trainee',
               'supervisor_person_id': h.seed['people'][0]}]
    w = h.shift(h.workspace(), typ=typ['id'], people=people)['workspace']
    return_people = [h.seed['people'][1]]
    return_typ = None
    if kind == 'exchange':
        return_typ = typ['id']
        return_people = [{'person_id': h.seed['people'][1], 'start': '2026-10-05T08:00:00+03:00',
                          'end': '2026-10-05T12:00:00+03:00', 'participation': 'supervisor'},
                         {'person_id': h.seed['people'][2], 'start': '2026-10-05T08:00:00+03:00',
                          'end': '2026-10-05T12:00:00+03:00', 'participation': 'trainee',
                          'supervisor_person_id': h.seed['people'][1]}]
    w = h.shift(w, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', typ=return_typ,
                people=return_people)['workspace']
    pid, first, second, _ = publish_pair(h, w)
    trials = []
    original_evaluate = swaps.evaluate

    def capture(data, *args, **kwargs):
        trials.append(copy.deepcopy(data))
        return original_evaluate(data, *args, **kwargs)

    monkeypatch.setattr(swaps, 'evaluate', capture)
    choice = recipient(h.get('staff', '/api/swaps/options?assignment_id=' + first + '&recipient_id=' + h.seed['people'][1]
        + '&kind=' + kind), h.seed['people'][1])
    assert choice['eligible'] is True
    paired = [p for data in trials for shift in data['shifts'] for p in shift['participants']
              if p['person_id'] == h.seed['people'][2] and p.get('supervisor_person_id') == h.seed['people'][1]]
    assert paired
    row = h.command('staff', 'swap.offer', offer_payload(h, pid, first, kind))['swap']
    if kind == 'give':
        assert transition(h, row, 'accept', 'staff_two')['swap']['status'] == 'counteroffered'
    else:
        row = transition(h, row, 'counteroffer', 'staff_two',
                         {'assignment_id': second, 'reason': 'Qualified supervisor exchange'})['swap']
        assert transition(h, row, 'confirm')['swap']['status'] == 'accepted'


def test_exact_pair_can_remove_single_leg_overlap_and_restore_mandatory_quota(h):
    identity(h, 'staff_two', ['staff'], h.seed['people'][1])
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
              'metric': 'duties', 'comparison': 'maximum', 'amount': 1}, {'person_ids': h.seed['people'][:2]})
    w = h.shift(h.workspace())['workspace']
    w = h.shift(w, people=[h.seed['people'][1]], location=1)['workspace']
    pid, first, second, _ = publish_pair(h, w)
    data = h.snapshot(w)
    next(p for shift in data['shifts'] for p in shift['participants'] if p['id'] == first)['person_id'] = h.seed['people'][1]
    codes = {f['code'] for f in evaluate(data)['findings']}
    assert {'TIME_OVERLAP', 'QUOTA_ABOVE_MAXIMUM'} <= codes
    give = recipient(h.get('staff', '/api/swaps/options?kind=give&assignment_id=' + first
        + '&recipient_id=' + h.seed['people'][1]), h.seed['people'][1])
    assert give['eligible'] is False
    assert {'TIME_OVERLAP', 'QUOTA_ABOVE_MAXIMUM'} <= {f['code'] for f in give['findings']}
    assert h.command('staff', 'swap.offer', offer_payload(h, pid, first, 'give'), status=422)['error']['code'] == 'SWAP_INELIGIBLE'
    assert recipient(h.get('staff', '/api/swaps/options?assignment_id=' + first + '&recipient_id=' + h.seed['people'][1]), h.seed['people'][1])['eligible']
    row = offered(h, pid, first)
    row = transition(h, row, 'counteroffer', 'staff_two', {'assignment_id': second, 'reason': 'Both simultaneous substitutions are valid'})['swap']
    row = transition(h, row, 'confirm')['swap']
    assert transition(h, row, 'prepare', 'coordinator')['validation']['valid']


def test_give_away_checks_the_proposer_losing_a_mandatory_minimum(h):
    pid, first, _, _ = source_pair(h)
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
        'metric': 'duties', 'comparison': 'minimum', 'amount': 1}, {'person_id': h.seed['people'][0]})
    choice = recipient(h.get('staff', '/api/swaps/options?kind=give&assignment_id=' + first
        + '&recipient_id=' + h.seed['people'][1]), h.seed['people'][1])
    assert choice['eligible'] is False and 'QUOTA_BELOW_MINIMUM' in {f['code'] for f in choice['findings']}
    before = counts(h)
    error = h.command('staff', 'swap.offer', offer_payload(h, pid, first, 'give'), status=422)['error']
    assert error['code'] == 'SWAP_INELIGIBLE' and error['details']['findings'] == choice['findings']
    assert counts(h) == before


@pytest.mark.parametrize('kind', ['exchange', 'give'])
def test_swap_cannot_duplicate_an_existing_participant_in_the_offered_session(h, kind):
    identity(h, 'staff_two', ['staff'], h.seed['people'][1])
    w = h.shift(h.workspace(), people=h.seed['people'][:2])['workspace']
    w = h.shift(w, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', people=[h.seed['people'][1]])['workspace']
    pid, first, _, _ = publish_pair(h, w)
    choice = recipient(h.get('staff', '/api/swaps/options?assignment_id=' + first + '&recipient_id=' + h.seed['people'][1]
        + '&kind=' + kind), h.seed['people'][1])
    assert choice['eligible'] is False
    assert 'DUPLICATE_PARTICIPATION' in {f['code'] for f in choice['findings']}
    assert h.command('staff', 'swap.offer', offer_payload(h, pid, first, kind), status=422)['error']['code'] == 'SWAP_INELIGIBLE'


@pytest.mark.parametrize('kind', ['exchange', 'give'])
def test_options_reuses_one_snapshot_and_bounds_validation_work_with_department_scale_context(h, monkeypatch, kind):
    _, first, _, _ = source_pair(h)
    path = '/api/swaps/options?assignment_id=' + first + '&recipient_id=' + h.seed['people'][1] + '&kind=' + kind
    expected = h.get('staff', path)
    original_snapshot, original_evaluate = swaps.domain_snapshot, swaps.evaluate
    snapshots, trials = [], []

    def large_snapshot(*args, **kwargs):
        data = original_snapshot(*args, **kwargs)
        template = copy.deepcopy(data['shifts'][0])
        original_people = len(data['people'])
        for i in range(150 - original_people):
            pid = 'unrelated-person-' + str(i)
            data['people'][pid] = {**data['people'][h.seed['people'][2]], 'id': pid}
        for i in range(500):
            shift = copy.deepcopy(template)
            shift.update(id='unrelated-shift-' + str(i), start='2026-10-08T05:00:00+00:00', end='2026-10-08T09:00:00+00:00')
            shift['participants'] = [{**shift['participants'][0], 'id': 'unrelated-assignment-' + str(i),
                'shift_id': shift['id'], 'person_id': 'unrelated-person-' + str(i % (150 - original_people)),
                'start': shift['start'], 'end': shift['end']}]
            data['shifts'].append(shift)
            data['focus_ids'].append(shift['id'])
        snapshots.append((len(data['people']), len(data['shifts'])))
        return data

    def bounded_evaluate(data, **kwargs):
        people = data['_partial']['people']
        assert len(people) == 2 and h.seed['people'][0] in people
        assert people <= set(h.seed['people'])
        assert data['quota_subjects'] == people
        assert len(data['shifts']) <= 2 and len(data['focus_ids']) <= 2
        assert all(not shift['id'].startswith('unrelated-') for shift in data['shifts'])
        trials.append(len(data['shifts']))
        return original_evaluate(data, **kwargs)

    monkeypatch.setattr(swaps, 'domain_snapshot', large_snapshot)
    monkeypatch.setattr(swaps, 'evaluate', bounded_evaluate)
    assert h.get('staff', path) == expected
    assert snapshots == [(150, 502)]
    assert 1 <= len(trials) <= len(expected['recipients'])


@pytest.fixture(scope="module")
def department_swap_options(scale_harness):
    """150 real staff lookups; ineligible selected staff must exhaust ten real validator trials."""
    h = scale_harness()
    with pytest.MonkeyPatch.context() as monkeypatch, ordered_clock(monkeypatch):
        with transaction(h.app.state.engine, write=True) as db:
            department = db.get(m.Department, h.seed['department_id'])
            department.swap_auto_prepare = department.swap_auto_publish = False
        _, first, _, _ = source_pair(h)
        people = list(h.seed['people'][1:])
        with transaction(h.app.state.engine, write=True) as db:
            organisation_id = db.get(m.Person, h.seed['people'][0]).organisation_id
            for i in range(148):
                person_id, identity_id = m.uid(), m.uid()
                db.add(m.Person(id=person_id, organisation_id=organisation_id, display_name=f'Synthetic recipient {i:03}'))
                db.flush()
                db.add(m.Identity(id=identity_id, issuer='local-development', subject=person_id,
                                  display_name=f'Synthetic recipient {i:03}', person_id=person_id))
                db.flush()
                db.add(m.RoleBinding(identity_id=identity_id, department_id=h.seed['department_id'],
                                     role='staff', starts_on=date(2020, 1, 1)))
                people.append(person_id)
        h.policy({'kind': 'restriction', 'weekdays': [6], 'start_time': '08:00', 'end_time': '12:00'},
                 {'person_ids': people[:-1]})
        original_snapshot = swaps.domain_snapshot

        def department_snapshot(*args, **kwargs):
            data = original_snapshot(*args, **kwargs)
            template = data['shifts'][0]
            source = h.seed['people'][0]
            for person_id in people:
                data['people'][person_id] = {**data['people'][source], 'id': person_id}
                for name in ('memberships', 'eligibilities', 'grants'):
                    data[name].extend({**row, 'id': m.uid(), 'person_id': person_id}
                                      for row in list(data[name]) if row['person_id'] == source)
            data['shifts'] = [template]
            data['workspace']['ends_on'] = '2026-11-01'
            for location in data['locations'].values():
                location['capacity'] = 1000
            for person_id in people:
                for day in range(10):
                    on = date(2026, 10, 5) + timedelta(days=day)
                    shift = copy.deepcopy(template)
                    shift.update(id=m.uid(), start=f'{on}T08:00:00+03:00', end=f'{on}T12:00:00+03:00')
                    shift['participants'] = [{**shift['participants'][0], 'id': m.uid(), 'shift_id': shift['id'],
                        'person_id': person_id, 'start': shift['start'], 'end': shift['end']}]
                    data['shifts'].append(shift)
            data['focus_ids'] = [shift['id'] for shift in data['shifts']]
            assert len(data['people']) == 151 and len(data['shifts']) == 1501
            return data

        monkeypatch.setattr(swaps, 'domain_snapshot', department_snapshot)
        h.client('staff')  # Exclude sign-in from the endpoint measurement.
        path = '/api/swaps/options?assignment_id=' + first
        timings, responses = {}, {}
        for name, query in (('directory', ''), ('ineligible', '&recipient_id=' + people[0]),
                            ('eligible', '&recipient_id=' + people[-1])):
            began, cpu = time.perf_counter(), time.process_time()
            responses[name] = h.get('staff', path + query)
            timings[name] = {'wall_seconds': time.perf_counter() - began, 'cpu_seconds': time.process_time() - cpu}
        print('swap options department performance', timings)
        return people, responses, timings


def test_options_with_ten_return_duties_per_recipient_stays_within_department_budget(department_swap_options, request):
    people, responses, timings = department_swap_options
    request.node.user_properties.append(('swap_options_performance', json.dumps(timings)))
    assert all(len(options['recipients']) == 150 for options in responses.values())
    assert all(set(row) == {'id', 'display_name', 'eligible', 'message', 'findings'} for row in responses['directory']['recipients'])
    assert recipient(responses['eligible'], people[-1])['eligible'] is True
    blocked = recipient(responses['ineligible'], people[0])
    assert blocked['eligible'] is False and 'COLLEAGUE_NOT_FREE' in {f['code'] for f in blocked['findings']}
    assert all('eligible' in row for row in responses['ineligible']['recipients'])


@pytest.mark.cpu_budget
def test_options_with_ten_return_duties_per_recipient_stays_within_department_budget_cpu_budget(department_swap_options):
    _, _, timings = department_swap_options
    assert all(timing['cpu_seconds'] < python_budget(2) for timing in timings.values()), timings


def test_options_say_why_nobody_can_take_a_duty_from_the_same_check(h):
    colleague = h.seed['people'][1]
    identity(h, 'staff_two', ['staff'], colleague)
    w = h.shift(h.workspace())['workspace']
    w = h.shift(w, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', people=[colleague])['workspace']
    w = h.shift(w, '2026-10-04T13:00:00+03:00', '2026-10-04T17:00:00+03:00', people=[colleague])['workspace']
    _, first, _, _ = publish_pair(h, w)
    path = '/api/swaps/options?assignment_id=' + first
    h.policy({'kind': 'min_rest', 'hours': 12})
    give = h.get('staff', path + '&kind=give')
    assert recipient(give, colleague)['eligible'] is False
    assert recipient(give, colleague)['message'] == 'This swap leaves too little rest between duties.'
    assert give['no_eligible_recipients_reason'] == 'Taking this duty would leave too little rest between duties.'
    exchange = h.get('staff', path + '&kind=exchange')  # the colleague still has a valid duty to return
    assert recipient(exchange, colleague)['eligible'] is True and exchange['no_eligible_recipients_reason'] is None


@pytest.mark.parametrize('kind', ['exchange', 'give'])
def test_the_reason_nobody_can_take_a_duty_never_names_a_colleagues_protected_time(h, kind):
    colleague = h.seed['people'][1]
    identity(h, 'staff_two', ['staff'], colleague)
    w = h.shift(h.workspace())['workspace']
    w = h.shift(w, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', people=[colleague])['workspace']
    _, first, _, _ = publish_pair(h, w)
    h.policy({'kind': 'restriction', 'weekdays': [6], 'start_time': '08:00', 'end_time': '12:00'}, {'person_id': colleague})
    options = h.get('staff', '/api/swaps/options?assignment_id=' + first + '&kind=' + kind)
    assert recipient(options, colleague)['message'] == 'This colleague is not free at that time.'
    assert_private_time_hidden(options)
    assert options['no_eligible_recipients_reason'] == 'Colleagues are not free at that time.'
