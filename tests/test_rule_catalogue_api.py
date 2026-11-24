"""The rule catalogue API (docs/design/rule-catalogue.md §5): the page, checks as you type, proposals with one waiting
version per rule, approval by another person, undo, and one rule in force per entry in the database."""
import time
import uuid
from collections import OrderedDict
from datetime import date

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from apps.api import models as m
from apps.api.db import transaction
from packages.domain.validation import evaluate
from .test_rule_catalogue_guard import world


def line(**fields):
    return {'id': str(uuid.uuid4()), **fields}


def person(p):
    return {'conditions': [{'field': 'person_ids', 'values': [p]}]}


def entry(h, catalogue_id, role='coordinator'):
    return next(e for e in h.get(role, '/api/rules/catalogue')['entries'] if e['id'] == catalogue_id)


def propose(h, catalogue_id, lines, status=200, revision=None, acknowledged=(), policy_id=None, **more):
    if catalogue_id == 'fair_share' and status == 200 and revision is None and policy_id is None:
        current = entry(h, catalogue_id)['rule']
        if current:
            policy_id, revision = current['id'], current['revision']
    return h.command('coordinator', 'rule.propose', {'catalogue_id': catalogue_id, 'expected_policy_id': policy_id, 'expected_revision': revision, 'lines': lines,
                                                    'acknowledged_overlaps': list(acknowledged), 'reason': 'Weekly duty caps', **more}, status=status)


def approve(h, record):
    return h.command('approver', 'policy.activate', {'reason': 'Independent review'}, record['id'], record['revision'])['record']


def test_the_catalogue_lists_every_entry_and_its_rule(h):
    from packages.contracts.rule_catalogue import FAMILIES, RULE_CATALOGUE
    page = h.get('auditor', '/api/rules/catalogue')
    assert [e['id'] for e in page['entries']] == [e['id'] for e in RULE_CATALOGUE]
    assert page['families'] == [{'id': key, 'name': name} for key, name in FAMILIES]
    assert {e['id'] for e in page['entries'] if e['rule']} == {'fair_share'}
    assert all(e['proposed'] is None for e in page['entries'])
    h.get('staff', '/api/rules/catalogue', status=403)


def test_catalogue_read_reuses_revision_projection_and_invalidates_after_policy_change(h, monkeypatch):
    """A department-scale page is computed once per input revision, with bounded stamp SQL on cache hits."""
    from apps.api.services import rule_catalogue

    # SQLite fixtures copy department IDs and revisions from the same template database.
    monkeypatch.setattr(rule_catalogue, '_catalogue_cache', OrderedDict())
    first = h.get('auditor', '/api/rules/catalogue')
    calls = []
    original = rule_catalogue._catalogue_uncached

    def counted(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(rule_catalogue, '_catalogue_uncached', counted)
    statements = []
    listener = lambda _connection, _cursor, statement, _parameters, _context, _executemany: statements.append(statement)
    event.listen(h.app.state.engine, 'before_cursor_execute', listener)
    try:
        began = time.perf_counter()
        cached = h.get('auditor', '/api/rules/catalogue')
        elapsed = time.perf_counter() - began
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', listener)
    assert cached == first and not calls
    # The source picker now performs one bounded latest-baseline read on cache hits.
    assert len(statements) <= 13, statements

    proposed = propose(h, 'duties_per_week', [line(numbers={'max': {'value': 5}})])['record']
    refreshed = h.get('auditor', '/api/rules/catalogue')
    duties = next(entry for entry in refreshed['entries'] if entry['id'] == 'duties_per_week')
    assert calls == [True] and refreshed != first and duties['proposed']['id'] == proposed['id']
    return locals()


def test_catalogue_response_shape_change_invalidates_cached_body_and_etag(h, monkeypatch):
    from apps.api.services import rule_catalogue

    stamp, build = rule_catalogue._catalogue_stamp, rule_catalogue._catalogue_uncached

    def legacy_body(*args, **kwargs):
        body = build(*args, **kwargs)
        for item in body['entries']:
            item.pop('line_replaces')
        return body

    client = h.client('auditor')
    with monkeypatch.context() as legacy:
        legacy.setattr(rule_catalogue, '_catalogue_stamp', lambda db, actor: ('rules-in-force-v2', *stamp(db, actor)[1:]))
        legacy.setattr(rule_catalogue, '_catalogue_uncached', legacy_body)
        before = client.get('/api/rules/catalogue')
        assert before.status_code == 200, before.text
        assert all('line_replaces' not in item for item in before.json()['entries'])
    after = client.get('/api/rules/catalogue', headers={'If-None-Match': before.headers['etag']})
    assert after.status_code == 200, after.text
    assert all('line_replaces' in item for item in after.json()['entries'])
    assert after.headers['etag'] != before.headers['etag']


def test_catalogue_local_cache_keeps_no_anchor_audiences_separate(h, monkeypatch):
    """The no-workspace fallback must not reuse one reader's history metadata for another."""
    from packages.contracts import schemas as s
    from apps.api.services import rule_catalogue

    class Reader:
        def __init__(self, identity_id, can_read_history):
            self.department_id = h.seed['department_id']
            self.identity_id = identity_id
            self.person_id = None
            self.roles = frozenset({'coordinator'})
            self.can_read_history = can_read_history

        def permits(self, *roles):
            return self.can_read_history if roles == tuple(s.PERMISSIONS['catalogue.read']) else True

    readers = (Reader('history-reader', True), Reader('current-only-reader', False))

    def actor_body(_db, actor, *_args, **_kwargs):
        can_read_history = actor.permits(*s.PERMISSIONS['catalogue.read'])
        return {'reader': actor.identity_id,
                'approved_by_name': 'Fictional approver' if can_read_history else None,
                'approved_at': '2026-11-07T00:00:00+00:00' if can_read_history else None,
                'self_approved': True if can_read_history else None}

    monkeypatch.setattr(rule_catalogue, 'require_catalogue_read', lambda *_args: None)
    monkeypatch.setattr(rule_catalogue, '_catalogue_uncached', actor_body)
    with transaction(h.app.state.engine) as db:
        stamp = rule_catalogue.catalogue_stamp(db, readers[0])
        assert not stamp[6][0], 'this regression must use the no-workspace-anchor path'
        first = rule_catalogue.catalogue(db, readers[0], day=date(2026, 11, 7), _stamp=stamp)
        second = rule_catalogue.catalogue(db, readers[1], day=date(2026, 11, 7), _stamp=stamp)
        first_again = rule_catalogue.catalogue(db, readers[0], day=date(2026, 11, 7), _stamp=stamp)

    assert first == first_again == {
        'reader': 'history-reader', 'approved_by_name': 'Fictional approver',
        'approved_at': '2026-11-07T00:00:00+00:00', 'self_approved': True}
    assert second == {
        'reader': 'current-only-reader', 'approved_by_name': None,
        'approved_at': None, 'self_approved': None}


@pytest.mark.wall_benchmark
def test_catalogue_read_wall_budget(h, monkeypatch):
    state = test_catalogue_read_reuses_revision_projection_and_invalidates_after_policy_change(h, monkeypatch)
    assert state['elapsed'] < 1.0


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_fair_share_capabilities_own_measure_specific_settings(h):
    properties = h.get('coordinator', '/api/policies/capabilities')['definition_schema']['$defs']['BalanceGoal']['properties']
    assert {key: field['x-applies-to'] for key, field in properties.items() if 'x-applies-to' in field} == {
        'partners': {'metric': ['pair_exposure']}, 'pairing': {'metric': ['pair_exposure']},
        'weekend_start': {'metric': ['weekends']}, 'weekend_days': {'metric': ['weekends']},
        'period_months': {'period': ['rolling']},
    }


def test_checks_name_duplicates_overlaps_warnings_and_issues(h):
    a = h.seed['people'][0]
    everyone, mine = line(numbers={'max': {'value': 5}}), line(who=person(a), numbers={'max': {'value': 3}})
    found = h.client('coordinator').post('/api/rules/catalogue/duties_per_week/check', json={'lines': [everyone, mine]}).json()
    assert found['duplicates'] == [] and found['issues'] == [] and found['warnings'] == []
    [overlap] = found['overlaps']
    assert overlap['line_id'] == everyone['id'] and overlap['winner_ids'] == [mine['id']] and overlap['people'] == 1
    twice = h.client('coordinator').post('/api/rules/catalogue/duties_per_week/check',
                                         json={'lines': [everyone, {**everyone, 'id': str(uuid.uuid4()), 'numbers': {'max': {'value': 4}}}]}).json()
    assert twice['duplicates'][0]['lines'] == [0, 1]
    other_days = {**everyone, 'id': str(uuid.uuid4()), 'weekdays': [5, 6]}
    assert h.client('coordinator').post('/api/rules/catalogue/duties_per_week/check',
                                        json={'lines': [{**everyone, 'weekdays': [0, 1]}, other_days]}).json()['duplicates'] == []
    weaker = h.client('coordinator').post('/api/rules/catalogue/people_per_session/check', json={'lines': [
        line(numbers={'max': {'value': 2}}), line(who={'conditions': [{'field': 'category_ids', 'values': [h.seed['category']]}]}, numbers={'max': {'value': 3}})]}).json()
    assert weaker['overlaps'] == [] and 'still binds' in weaker['warnings'][0]['message']  # safety: every line applies
    bad = h.client('coordinator').post('/api/rules/catalogue/not_after/check', json={'lines': [line(numbers={'apply': {}})]}).json()  # no duty types chosen
    assert bad['issues'] and bad['issues'][0]['message'].startswith('Line 1:')
    invalid = h.client('coordinator').post('/api/rules/catalogue/duties_per_week/check', json={'lines': [line(numbers={'apply': {}})]}).json()
    assert invalid['issues'][0]['code'] == 'INVALID_RULE_DEFINITION'
    assert h.client('coordinator').post('/api/rules/catalogue/nonsense/check', json={'lines': []}).status_code == 404
    assert h.client('staff').post('/api/rules/catalogue/duties_per_week/check', json={'lines': []}).status_code == 403


def test_a_line_with_an_empty_list_is_refused_in_plain_words(h):
    for bad, words in (({'weekdays': []}, 'Choose at least one day'), ({'numbers': {}}, 'Give the line a number')):
        issues = h.client('coordinator').post('/api/rules/catalogue/duties_per_week/check', json={'lines': [{**line(numbers={'max': {'value': 5}}), **bad}]}).json()['issues']
        assert [x['message'] for x in issues] == [words], issues
        assert 'lines.0.' in issues[0]['field']  # still the line's own issue, as the editor places it


def test_proposals_wait_for_another_person_and_retiring_brings_back_the_version_before(h):
    w, _, _ = world(h)
    a = h.seed['people'][0]
    everyone, mine = line(numbers={'max': {'value': 2}}), line(who=person(a), numbers={'max': {'value': 1}})
    propose(h, 'duties_per_week', [everyone, everyone], status=422)  # the same line twice
    check = h.client('coordinator').post('/api/rules/catalogue/duties_per_week/check', json={'lines': [everyone, mine]}).json()
    assert check['overlaps'][0]['message'].endswith('for 1 person today')
    first = propose(h, 'duties_per_week', [everyone, mine])['record']
    assert first['status'] == 'proposed' and first['kind'] == 'catalogue'
    propose(h, 'duties_per_week', [everyone], status=409)  # one waiting proposal per rule
    h.command('coordinator', 'policy.activate', {'reason': 'Self approval'}, first['id'], first['revision'], status=403)
    live = approve(h, first)
    w = h.refresh_draft_baseline(w)
    shown = entry(h, 'duties_per_week')
    assert shown['rule']['id'] == live['id'] and shown['overlaps'] == 1 and shown['reach'][mine['id']] == 1
    # The rule reaches each person once: the line for everyone holds for the others, A's own line for A.
    assert shown['people'] == shown['reach'][everyone['id']] + 1 == len(h.seed['people'])
    findings = [f for f in evaluate(h.snapshot(w))['findings'] if f.get('rule_id') == live['id']]
    assert findings and {f['person_id'] for f in findings} >= {a}  # A works more than the one duty of their own line
    propose(h, 'duties_per_week', [everyone], status=409, revision=None)  # stale: a version is in force
    second = propose(h, 'duties_per_week', [{**everyone, 'numbers': {'max': {'value': 6}}}], policy_id=live['id'], revision=live['revision'])['record']
    diff = entry(h, 'duties_per_week')['proposed']['diff']
    assert diff == {'added': [], 'removed': [mine['id']], 'changed': [everyone['id']], 'on': None, 'settings': None}
    newer = approve(h, second)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, live['id']).status == 'retired'
    current = next(p for p in h.get('approver', '/api/policies?status=active')['policies'] if p['id'] == newer['id'])
    restored = h.command('approver', 'policy.retire', {'reason': 'Undo the change'}, current['id'], current['revision'])['restored_ids']
    assert restored == [live['id']] and entry(h, 'duties_per_week')['rule']['id'] == live['id']


def test_policy_withdrawal_requires_a_reason(h):
    proposal = propose(h, 'duties_per_week', [line(numbers={'max': {'value': 5}})])['record']
    refused = h.command('coordinator', 'policy.decline', {}, proposal['id'], proposal['revision'], status=422)
    assert refused['error']['code'] == 'SCHEMA_INVALID'


def test_partial_rule_amendment_keeps_omissions_and_accepts_explicit_clears(pg):
    catalogue_id = 'duties_per_week'
    settings = {'max_duties': {'window': 'week'}}
    first = propose(pg, catalogue_id, [line(numbers={'max': {'value': 3}})],
                    on=False, settings=settings, balance_between_people=False)['record']
    live = approve(pg, first)

    def amend(base, **fields):
        return pg.command('coordinator', 'rule.propose', {
            'catalogue_id': catalogue_id, 'expected_policy_id': base['id'], 'expected_revision': base['revision'],
            'reason': 'Edit synthetic rule', **fields,
        })['record']

    original = live['definition']['rule']
    amended = amend(live, balance_between_people=True)
    assert amended['definition']['rule'] == original
    assert amended['definition']['balance_between_people'] is True
    assert entry(pg, catalogue_id)['rule']['id'] == live['id']
    current = approve(pg, amended)
    cleared = amend(current, on=True, settings={}, lines=[])
    assert cleared['definition']['rule'] == {**original, 'on': True, 'settings': {}, 'lines': []}
    pg.command('coordinator', 'policy.decline', {'reason': 'Undo proposal'}, cleared['id'], cleared['revision'])
    latest = entry(pg, catalogue_id)['rule']
    assert latest['id'] == current['id']
    restored = pg.command('approver', 'policy.retire', {'reason': 'Undo activation'}, latest['id'], latest['revision'])
    assert restored['restored_ids'] == [live['id']]
    assert entry(pg, catalogue_id)['rule']['definition']['rule'] == original
    with transaction(pg.app.state.engine) as db:
        for saved in (live, amended, cleared):
            history = db.query(m.DefinitionHistory).filter_by(
                kind='policy', object_id=saved['id'], object_revision=saved['revision']).one()
            assert history.snapshot['definition']['rule'] == saved['definition']['rule']
        assert db.query(m.AuditEvent).filter_by(action='rule.propose', target_id=amended['id']).count() == 1


@pytest.mark.parametrize('omitted', ['on', 'settings', 'lines'])
def test_rule_amendment_keeps_each_omitted_field(h, omitted):
    catalogue_id = 'duties_per_week'
    original = {'on': False, 'settings': {'max_duties': {'window': 'week'}},
                'lines': [line(numbers={'max': {'value': 3}})]}
    live = approve(h, propose(h, catalogue_id, **original)['record'])
    if omitted == 'lines':
        seed = line(who=person(h.seed['people'][0]), numbers={'max': {'value': 2}})
        with transaction(h.app.state.engine, write=True) as db:
            db.add(m.PolicySeedLine(id=seed['id'], department_id=h.seed['department_id'],
                                    policy_id=live['id'], policy_revision=live['revision'], line=seed))
    changes = {'on': True, 'settings': {}, 'lines': []}
    del changes[omitted]
    payload = {'catalogue_id': catalogue_id, 'expected_policy_id': live['id'],
               'expected_revision': live['revision'], 'reason': 'Review amended rule settings', **changes}
    if omitted == 'lines':
        stale = h.command('coordinator', 'rule.propose', {**payload, 'expected_line_ids': []}, status=409)
        assert stale['error']['code'] == 'STALE_REVISION'
    amended = h.command('coordinator', 'rule.propose', payload)['record']['definition']['rule']
    assert {field: amended[field] for field in changes} == changes
    if omitted == 'lines':
        assert [item['id'] for item in amended['lines']] == [original['lines'][0]['id'], seed['id']]
    else:
        assert amended[omitted] == original[omitted]


def test_rule_amendment_stores_the_normalised_dates_of_omitted_lines(h):
    catalogue_id = 'other_counts_per_period'
    live = approve(h, propose(h, catalogue_id, [line(numbers={'min': {'value': 2}})])['record'])
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Policy, live['id'])
        rule = row.definition['rule']
        undated = {key: value for key, value in rule['lines'][0].items() if key not in ('starts_on', 'ends_on')}
        row.definition = {**row.definition, 'rule': {**rule, 'lines': [undated]}}
    reply = h.command('coordinator', 'rule.propose', {
        'catalogue_id': catalogue_id, 'expected_policy_id': live['id'], 'expected_revision': live['revision'],
        'reason': 'Keep the counting block', 'settings': {},
    })
    [saved] = reply['record']['definition']['rule']['lines']
    [checked] = reply['check']['lines']
    assert saved['starts_on'] == checked['starts_on'] == live['starts_on']
    assert saved['ends_on'] == checked['ends_on'] > saved['starts_on']


def test_catalogue_rules_are_not_proposed_as_plain_rules(h):
    h.command('coordinator', 'policy.create', {'name': 'Sneaky', 'owner': 'Test', 'source': 'Constructed fixture', 'starts_on': '2026-01-01',
                                               'definition': {'rule': {'kind': 'catalogue', 'id': 'keep_off', 'lines': []}}}, status=422)


def test_a_line_naming_another_departments_records_is_refused(h):
    stranger = str(uuid.uuid4())
    propose(h, 'keep_off', [line(who=person(stranger), numbers={'apply': {'value': None}})], status=404)


def test_one_catalogue_rule_in_force_per_entry_in_the_database(h):
    def add(status):
        with transaction(h.app.state.engine, write=True) as db:
            db.add(m.Policy(department_id=h.seed['department_id'], name='Keep off', kind='catalogue', status=status, owner='Test', source='Test',
                            starts_on=m.now().date(), created_by=h.seed['identities']['coordinator'],
                            definition={'rule': {'kind': 'catalogue', 'id': 'keep_off', 'lines': []}}))
    add('active')
    add('proposed')
    add('retired')
    with pytest.raises(IntegrityError):
        add('active')


def test_the_migration_preview_route(h):
    world(h)
    plan = h.get('coordinator', '/api/rules/catalogue/migration?proof=true')
    assert plan['moving'] == 6 and plan['proof']['identical']
    h.get('staff', '/api/rules/catalogue/migration', status=403)


@pytest.fixture(scope='module')
def catalogue_check_measurement(scale_harness):
    h = scale_harness('h')
    from apps.api.auth import Actor
    from apps.api.services import policies
    from apps.api.services.rule_catalogue import check
    real = policies.people_data

    def department(db, actor, day, *, last=None):  # the department at scale: 150 people
        data, a, b = real(db, actor, day, last=last)
        return {**data, 'people': {**data['people'], **{str(uuid.uuid4()): {'active': True} for _ in range(147)}}}, a, b
    from datetime import date, timedelta
    people = h.seed['people']
    week = lambda n: (date(2026, 1, 4) + timedelta(days=7 * (n // 4))).isoformat()  # noqa: E731
    # 50 weeks of a line for everyone and one per person (each person's line outranks the one for everyone).
    lines = [line(who=person(people[n % 4 - 1]) if n % 4 else None, numbers={'max': {'value': 3 + n % 3}},
                  starts_on=week(n), ends_on=week(n + 4)) for n in range(200)]
    actor = Actor(h.seed['identities']['coordinator'], h.seed['department_id'], h.seed['organisation_id'], 'coordinator',
                  frozenset(['coordinator']), None, 'test')
    with pytest.MonkeyPatch.context() as monkeypatch, transaction(h.app.state.engine) as db:
        monkeypatch.setattr(policies, 'people_data', department)
        check(db, actor, 'duties_per_week', {'lines': lines})
        elapsed = []
        for _ in range(3):  # the best of three: a collector pause is not the check
            began = time.process_time()
            found = check(db, actor, 'duties_per_week', {'lines': lines})
            elapsed.append(time.process_time() - began)
        elapsed = min(elapsed)
    return found, elapsed


def test_the_check_stays_within_budget_on_a_200_line_rule(catalogue_check_measurement):
    found, _ = catalogue_check_measurement
    assert not found['issues'] and found['overlaps']


@pytest.mark.cpu_budget
def test_the_check_stays_within_budget_on_a_200_line_rule_cpu_budget(catalogue_check_measurement):
    import coverage
    _, elapsed = catalogue_check_measurement
    # About 0.035 s on a developer machine at department scale; coverage tracing (CI) is several times slower.
    assert elapsed < (0.9 if coverage.Coverage.current() else 0.15), elapsed


def test_every_entry_turns_on_with_its_first_line(h):
    from packages.contracts.rule_catalogue import RULE_CATALOGUE
    from packages.domain.conditions import active_rules
    second = h.configure('assignment_types', {'name': 'Late clinic (catalogue test)', 'starts_on': '2020-01-01',
                                              'definition': {'requirements': [{'minimum': 1}]}})['record']['id']
    types, people = [h.seed['type'], second], h.seed['people']
    chosen = {'type_order': types, 'from_type_ids': types[:1], 'to_type_ids': types[1:], 'from_location_id': h.seed['locations'][0],
              'to_location_id': h.seed['locations'][1], 'mode': 'together', 'person_ids': people[:2], 'when_person_ids': people[:1]}
    for item in RULE_CATALOGUE:
        settings = {kind: {**values} for kind, values in item['default_settings'].items()}
        for kind, fields in item['needs'].items():  # what the department must choose: its duty types, places, people
            settings.setdefault(kind, {}).update({f: chosen[f] for f in fields})
        first = {'id': str(uuid.uuid4()), **item['default_line']}
        record = propose(h, item['id'], [first], settings=settings)['record']
        copies = active_rules([{**record, 'status': 'active'}])
        assert copies and {c['template_line']['id'] for c in copies} == {first['id']}, item['id']
        stored = record['definition']['rule']['lines'][0]
        quotas = [c for c in copies if c['kind'] == 'block_quota']
        if quotas:
            assert all(c['definition']['rule'].get('repeat') or c['definition']['rule'].get('ends_on') for c in quotas), item['id']
            assert all(c['definition']['rule'].get('ends_on') is None for c in quotas if c['definition']['rule'].get('repeat'))
        else:
            assert not stored.get('ends_on'), item['id']
    weekly = entry(h, 'duties_per_week')
    assert weekly['default_line'] == {'numbers': {'max': {'value': 5, 'level': 'mandatory'}}}


def test_overlaps_are_listed_per_line(h):
    a = h.seed['people'][0]
    everyone, mine = line(numbers={'max': {'value': 5}}), line(who=person(a), numbers={'max': {'value': 3}})
    other = line(where={'conditions': [{'field': 'service_ids', 'values': [h.seed['services'][0]]}]}, numbers={'max': {'value': 2}})
    found = h.client('coordinator').post('/api/rules/catalogue/duties_per_week/check', json={'lines': [everyone, mine, other]}).json()
    assert found['line_overlaps'] == {everyone['id']: [{'line_id': mine['id'], 'people': 1, 'winner': mine['id']}],
                                      mine['id']: [{'line_id': everyone['id'], 'people': 1, 'winner': mine['id']}]}
    assert found['line_replaces'] == {mine['id']: [{'replaced_line_id': everyone['id'], 'numbers': ['max']}]}
    waiting = propose(h, 'duties_per_week', [everyone, mine, other])['record']
    assert entry(h, 'duties_per_week')['proposed']['line_replaces'] == found['line_replaces']
    approve(h, waiting)
    shown = entry(h, 'duties_per_week')
    assert shown['line_overlaps'] == found['line_overlaps'] and shown['overlaps'] == 1
    assert shown['line_replaces'] == found['line_replaces']
    from packages.contracts.schemas import RuleCatalogueInForceEntry
    assert RuleCatalogueInForceEntry.model_validate({k: shown[k] for k in ('also_in_force', 'line_replaces')}).model_dump(mode='json')['line_replaces'] == found['line_replaces']


def test_a_repeating_quota_line_without_dates_has_no_hidden_end(h):
    found = h.client('coordinator').post('/api/rules/catalogue/duties_per_month/check', json={'lines': [line(numbers={'max': {'value': 20}})]}).json()
    assert found['issues'] == [] and len(found['dated']) == 1
    [(start, end)] = found['dated'].values()
    assert start and end is None
