"""The Rules read retains every department policy the checker resolves today."""
from datetime import date
import copy
import uuid

import pytest
from sqlalchemy import event, select

from apps.api import models as m
from apps.api.auth import authenticate
from apps.api.db import transaction
from apps.api.serialization import record
from apps.api.services.policies import people_data
from apps.api.services.scoring_defaults import seeded_rows
from packages.contracts import schemas as s
from packages.domain.conditions import active_rules, with_tags
from packages.domain.validation import within_dates
from .governance_support import retire_rule
from .test_rule_conditions import line, tree


def add_policy(h, rule, *, name='Synthetic rule in force', **fields):
    definition = s.PolicyDefinition.model_validate({'rule': rule, **fields.pop('definition', {})},
                                                  context={'historical': True}).model_dump(mode='json')
    with transaction(h.app.state.engine, write=True) as db:
        policy = m.Policy(id=str(uuid.uuid4()), department_id=h.seed['department_id'],
                          name=name, kind=rule['kind'], owner='Synthetic owner', source='Synthetic regression',
                          definition=definition, starts_on=date(2026, 1, 1), status='active', active=True,
                          created_by=h.seed['identities']['coordinator'])
        for key, value in fields.items():
            setattr(policy, key, value)
        db.add(policy)
        db.flush()
        return record(policy)


def shown_policies(page):
    return [row for entry in page['entries'] for row in
            ([entry['rule']] if entry['rule'] else []) + entry['also_in_force']] + page['not_on_catalogue']


def checker_ids(h, day):
    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, h.client('auditor').cookies['rota_session'])
        data, a, b = people_data(db, actor, day)
        rows = seeded_rows(db, [record(row) for row in db.scalars(select(m.Policy).where(
            m.Policy.department_id == h.seed['department_id']))])
        data = with_tags({**data, 'policies': [p for p in rows if not p['definition'].get('workspace_id')]})
        return {p['id'] for p in active_rules(data) if within_dates(p, a, b, data['organisation']['timezone'])}


def test_counts_only_rules_in_force_matches_catalogue_and_role_scoping(h):
    future = add_policy(h, {'kind': 'catalogue', 'id': 'days_in_a_row', 'lines': [
        {'id': str(uuid.uuid4()), 'numbers': {'max': {'value': 5}}}]},
        name='Future rule', starts_on=date(2026, 10, 4))
    add_policy(h, {'kind': 'catalogue', 'id': 'days_in_a_row', 'lines': [
        {'id': str(uuid.uuid4()), 'numbers': {'max': {'value': 4}}}]},
        name='Waiting rule', status='proposed')
    add_policy(h, {'kind': 'catalogue', 'id': 'weekends_in_a_row', 'lines': [
        {'id': str(uuid.uuid4()), 'numbers': {'max': {'value': 2}}}]},
        name='Retired rule', status='retired', active=False)
    legacy = add_policy(h, {'kind': 'minimum_cover', 'minimum': 1},
                         name='Legacy rule', definition={'scope': {'person_id': h.seed['people'][0]}})
    workspace = h.workspace('Rota-only policy')
    add_policy(h, {'kind': 'minimum_cover', 'minimum': 1}, name='Rota-only rule',
               definition={'workspace_id': workspace['id']})
    tag = h.configure('tags', {'name': 'In-force scope', 'labels': ['people']})['record']
    h.policy({'kind': 'min_rest', 'hours': 11}, {'conditions': tree(line('tags', tag['id']))})

    counts = h.get('auditor', '/api/catalogues?counts_only=true&include=policies')
    catalogue = h.get('auditor', '/api/rules/catalogue')
    assert counts['rules_in_force'] == catalogue['catalogue_counts']['in_force']
    assert counts['rules_in_force'] == len(checker_ids(h, date(2026, 10, 3)))
    assert legacy['id'] in checker_ids(h, date(2026, 10, 3))
    assert future['id'] not in checker_ids(h, date(2026, 10, 3))
    s.CatalogueCounts.model_validate(counts)
    h.get('staff', '/api/catalogues?counts_only=true&include=policies', status=403)


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_in_force_counts_skip_approval_labels_while_full_catalogue_keeps_them(h, monkeypatch):
    from apps.api.services import policies, rule_catalogue

    calls, original = [], policies.policy_versions

    def observed(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(policies, 'policy_versions', observed)
    client = h.client('auditor')
    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, client.cookies['rota_session'])
        compact = rule_catalogue._catalogue_uncached(db, actor, day=date(2026, 10, 3), in_force_only=True)
        assert not calls
        full = rule_catalogue._catalogue_uncached(db, actor, day=date(2026, 10, 3))

    assert calls
    assert compact['catalogue_counts']['in_force'] == full['catalogue_counts']['in_force']
    assert all('approved_by_name' in row for row in shown_policies(full))


def test_catalogue_projection_keeps_full_and_in_force_shapes_separate(h):
    h.workspace('Catalogue projection anchor')
    from apps.api.services import rule_catalogue

    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, h.client('auditor').cookies['rota_session'])
        day = date(2026, 10, 3)
        stamp = ('same-department-and-day',)
        full = rule_catalogue.catalogue(db, actor, day=day, _stamp=stamp)
        compact = rule_catalogue.catalogue(db, actor, day=day, _stamp=stamp, in_force_only=True)
        full_again = rule_catalogue.catalogue(db, actor, day=day, _stamp=stamp)
        compact_again = rule_catalogue.catalogue(db, actor, day=day, _stamp=stamp, in_force_only=True)

    assert 'entries' in full and 'catalogue_counts' in full
    assert compact == compact_again and set(compact) == {'catalogue_counts'}
    assert full == full_again and 'entries' in full_again


@pytest.mark.parametrize('role', s.ROLE_NAMES)
def test_counts_only_requires_catalogue_read_and_returns_exact_shape(h, role):
    if role not in h.seed['identities']:
        from .governance_support import identity
        identity(h, role, [role])

    response = h.client(role).get('/api/catalogues?counts_only=true&include=policies')
    if role not in s.PERMISSIONS['catalogue.read']:
        assert response.status_code == 403, response.text
        return
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {'counts', 'rules_in_force'}
    assert set(body['counts']) == {'policies'}
    assert isinstance(body['rules_in_force'], int)


def test_counts_only_rules_in_force_invalidates_after_activate_and_retire(h):
    before = h.get('auditor', '/api/catalogues?counts_only=true')['rules_in_force']
    proposal = h.command('coordinator', 'policy.create', {
        'name': 'Stamp invalidation rule', 'owner': 'Synthetic owner', 'source': 'Synthetic regression',
        'starts_on': '2020-01-01',
        'definition': {'rule': {'kind': 'minimum_cover', 'minimum': 1},
                       'scope': {'person_id': h.seed['people'][0]}, 'severity': 'mandatory',
                       'exception_allowed': False}})['record']
    assert h.get('auditor', '/api/catalogues?counts_only=true')['rules_in_force'] == before
    active = h.command('approver', 'policy.activate', {'reason': 'Independent synthetic review'},
                       proposal['id'], proposal['revision'])['record']
    assert h.get('auditor', '/api/catalogues?counts_only=true')['rules_in_force'] == before + 1
    retire_rule(h, active, 'Synthetic rule retired')
    assert h.get('auditor', '/api/catalogues?counts_only=true')['rules_in_force'] == before


def test_counts_only_rules_in_force_reuses_stamp_projection(h, monkeypatch):
    h.workspace('Projection anchor')
    from apps.api.services import rule_catalogue
    calls = []
    original = rule_catalogue._in_force

    def counted(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(rule_catalogue, '_in_force', counted)
    first = h.get('auditor', '/api/catalogues?counts_only=true')['rules_in_force']
    second = h.get('auditor', '/api/catalogues?counts_only=true')['rules_in_force']
    assert first == second and len(calls) == 1


def test_counts_only_rules_in_force_has_a_conditional_etag(h):
    client = h.client('auditor')
    first = client.get('/api/catalogues?counts_only=true&include=policies')
    assert first.status_code == 200 and first.headers.get('etag')
    assert first.headers['cache-control'] == 'private, no-cache'
    repeat = client.get('/api/catalogues?counts_only=true&include=policies',
                        headers={'If-None-Match': first.headers['etag']})
    assert repeat.status_code == 304 and not repeat.content
    h.policy({'kind': 'min_rest', 'hours': 12})
    changed = client.get('/api/catalogues?counts_only=true&include=policies',
                         headers={'If-None-Match': first.headers['etag']})
    assert changed.status_code == 200 and changed.headers['etag'] != first.headers['etag']


def test_in_force_dependency_stamp_tracks_each_scheduling_input_family():
    from apps.api.services.rule_catalogue import in_force_dependency_stamp

    families = {name: 1 for name in ('scheduling_facts', 'catalogue_conversions', 'source_bindings',
                                     'policy_seed_lines', 'type_lead_ownership')}
    versions = {'policies': 'p', 'tags': 't', 'tag_members': 'm'}
    source_revision = lambda value: tuple(sorted(value.items()))
    baseline = in_force_dependency_stamp('2026-10-03', 'Asia/Riyadh', source_revision(families), versions)
    for family in families:
        changed = dict(families)
        changed[family] += 1
        assert in_force_dependency_stamp('2026-10-03', 'Asia/Riyadh', source_revision(changed), versions) != baseline


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_future_active_catalogue_rule_remains_editable_before_it_is_in_force(h):
    starts_on = date(2026, 10, 4)
    future = add_policy(h, {'kind': 'catalogue', 'id': 'days_in_a_row', 'lines': [
        {'id': str(uuid.uuid4()), 'numbers': {'max': {'value': 5}}}]}, starts_on=starts_on)
    for version in (1, 2):
        for day in (date(2026, 10, 3), starts_on):
            page = h.get('auditor', f'/api/rules/catalogue?version={version}&day={day}')
            entry = next(entry for entry in page['entries'] if entry['id'] == 'days_in_a_row')
            assert entry['rule']['id'] == future['id']
            assert entry['rule']['starts_on'] == str(starts_on)
            assert all(row['id'] != future['id'] for row in entry['also_in_force'] + page['not_on_catalogue'])
            expected = checker_ids(h, day)
            assert (future['id'] in expected) is (day >= starts_on)
            assert page['catalogue_counts']['in_force'] == len(expected)
            assert page['catalogue_counts']['lines'] == len(expected)
        detail = h.get('coordinator', f'/api/rules/catalogue/days_in_a_row/detail?version={version}&day=2026-10-03')
        assert detail['entry']['rule']['id'] == future['id']


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_catalogue_policy_ids_equal_checker_with_duplicate_safety_and_unconverted_rule(h):
    day = date(2026, 10, 3)
    person_id = h.seed['people'][0]
    canonical = add_policy(h, {'kind': 'catalogue', 'id': 'kind_per_session', 'lines': [{
        'id': str(uuid.uuid4()), 'who': {'conditions': [{'field': 'person_ids', 'values': [person_id]}]},
        'numbers': {'min': {'value': 1}}}]})
    duplicate = add_policy(h, {'kind': 'minimum_cover', 'minimum': 2},
                           definition={'scope': {'person_id': person_id}})
    unreached = add_policy(h, {'kind': 'minimum_cover', 'minimum': 1},
                           definition={'scope': {'person_id': str(uuid.uuid4())}})
    # A valid legacy any-of mixes person and session selectors: it cannot be expressed as who AND where.
    unconverted = add_policy(h, {'kind': 'block_quota', 'metric': 'duties', 'comparison': 'maximum',
                                'amount': 5, 'starts_on': '2026-01-01', 'ends_on': '2027-01-01'},
                            definition={'listed_only': True,
                                'per_person': [{'person_id': pid, 'amount': 5} for pid in
                                               [*h.seed['people'], *[str(uuid.uuid4()) for _ in range(35)]]],
                                'scope': {'conditions': {'match': 'any', 'conditions': [
                                {'field': 'person_ids', 'values': [person_id]},
                                {'field': 'service_ids', 'values': [h.seed['services'][0]]}]}}})
    unconverted_safety = add_policy(h, {'kind': 'minimum_cover', 'minimum': 1},
                                   definition={'scope': unconverted['definition']['scope']})
    excluded = [add_policy(h, {'kind': 'min_rest', 'hours': 11}, active=False),
                add_policy(h, {'kind': 'min_rest', 'hours': 12}, starts_on=date(2026, 10, 4)),
                add_policy(h, {'kind': 'min_rest', 'hours': 13}, ends_on=day),
                add_policy(h, {'kind': 'min_rest', 'hours': 14}, status='proposed'),
                add_policy(h, {'kind': 'min_rest', 'hours': 15}, status='retired')]
    expected = checker_ids(h, day)
    assert {canonical['id'], duplicate['id'], unconverted['id']} <= expected
    assert expected.isdisjoint(p['id'] for p in excluded)
    for version, entry_id in [(1, 'kind_per_session'), (2, 'people_per_session')]:
        page = h.get('auditor', f'/api/rules/catalogue?version={version}')
        shown = shown_policies(page)
        assert {row['id'] for row in shown} == expected
        assert len(shown) == len(expected) == page['catalogue_counts']['in_force']
        s.RuleCatalogueInForce.model_validate({key: page[key] for key in ('not_on_catalogue', 'catalogue_counts')})
        entry = next(entry for entry in page['entries'] if entry['id'] == entry_id)
        safety = next(row for row in entry['also_in_force'] if row['id'] == duplicate['id'])
        assert set(safety) == set(s.RuleInForce.model_fields)
        assert safety['safety'] is True and safety['kind'] == 'minimum_cover'
        assert safety['lines'] == 1 and safety['reach'] == 1
        assert {row['id'] for row in page['not_on_catalogue']} == {unconverted['id'], unconverted_safety['id']}
        assert next(row for row in page['not_on_catalogue'] if row['id'] == unconverted['id'])['lines'] == 38
        assert next(row for row in page['not_on_catalogue'] if row['id'] == unconverted_safety['id'])['safety'] is True
        assert next(row for row in shown if row['id'] == canonical['id'])['safety'] is True
        detail = h.get('coordinator', f'/api/rules/catalogue/{entry_id}/detail?version={version}&policy_id={duplicate["id"]}')
        assert any(row['id'] == duplicate['id'] and row['safety'] for row in detail['entry']['also_in_force'])
        assert detail['reach']['reach'][duplicate['id']] == 1
        pending = [issue for issue in page['attention'] if not issue['acknowledged']]
        assert any(issue['rule_id'] == unreached['id'] and issue['safety'] for issue in pending)
        assert page['attention_counts'] == {
            'rules': len({issue['rule_id'] for issue in pending}),
            'lines': len({(issue['rule_id'], line) for issue in pending for line in issue['line_ids']})}
        assert all(issue['safety'] for issue in page['attention'] if issue['rule_id'] == duplicate['id'])
    h.get('staff', '/api/rules/catalogue', status=403)


def test_catalogue_uses_checker_source_ids_after_v2_conversion(h):
    source = h.policy({'kind': 'max_duties', 'maximum': 3, 'window': 'week'})
    preview = h.get('coordinator', '/api/rules/catalogue/migration?version=2&background=true')
    conversion = h.command('coordinator', 'rule.migrate_to_catalogue', {
        'version': 2, 'plan_hash': preview['plan_hash'], 'reason': 'Synthetic source identity regression'})['record']
    h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent source identity review'},
              conversion['id'], conversion['revision'])
    for maximum in (4, 5):
        current = h.get('coordinator', '/api/rules/catalogue/workload_limit/detail?version=2')['entry']['rule']
        lines = copy.deepcopy(current['definition']['rule']['lines'])
        lines[0]['numbers']['max']['value'] = maximum
        proposal = h.command('coordinator', 'rule.propose', {
            'catalogue_id': 'workload_limit', 'catalogue_version': 2, 'expected_policy_id': current['id'],
            'expected_revision': current['revision'], 'lines': lines, 'reason': 'Synthetic replacement chain'})['record']
        h.command('approver', 'policy.activate', {'reason': 'Independent replacement review'},
                  proposal['id'], proposal['revision'])
    expected = checker_ids(h, date(2026, 10, 3))
    assert source['id'] in expected
    for version in (1, 2):
        page = h.get('auditor', f'/api/rules/catalogue?version={version}')
        carriers = {entry['rule']['id'] for entry in page['entries'] if entry['rule'] and
                    any(line.get('source_bindings') for line in entry['rule']['definition']['rule']['lines'])}
        assert {row['id'] for row in shown_policies(page)} == expected | carriers
        assert page['catalogue_counts']['in_force'] == len(expected)


def test_catalogue_keeps_stored_tag_lines_and_acknowledgement_identity(h):
    from packages.domain.rule_lines import fingerprint
    from .test_rule_catalogue_conflicts import tag_people
    tag = tag_people(h, 'catalogue_group', [h.seed['people'][0]])
    policy = add_policy(h, {'kind': 'catalogue', 'id': 'duties_per_week', 'lines': [
        {'id': str(uuid.uuid4()), 'who': {'conditions': [{'field': 'category_ids', 'values': [h.seed['category']]}]},
         'numbers': {'max': {'value': 3}}},
        {'id': str(uuid.uuid4()), 'who': {'conditions': [{'field': 'person_ids', 'values': [tag]}]},
         'numbers': {'max': {'value': 2}}}]})
    stored = {line['id']: line for line in policy['definition']['rule']['lines']}
    def read():
        page = h.get('auditor', '/api/rules/catalogue')
        shown = next(entry['rule'] for entry in page['entries'] if entry['id'] == 'duties_per_week')
        assert shown['definition'] == policy['definition']
        issue = next(issue for issue in page['attention'] if issue['rule_id'] == policy['id'] and issue['kind'] == 'both')
        assert issue['fingerprint'] == fingerprint(issue, stored)
        return issue['fingerprint']
    before = read()
    tag_people(h, 'catalogue_group', [h.seed['people'][1]])
    assert read() == before


def test_catalogue_supplement_queries_do_not_grow_per_policy_or_cross_departments(h):
    client = h.client('auditor')
    def read_queries():
        statements = []
        listener = lambda _connection, _cursor, sql, *_: statements.append(sql)
        event.listen(h.app.state.engine, 'before_cursor_execute', listener)
        try:
            response = client.get('/api/rules/catalogue')
            assert response.status_code == 200, response.text
            return response.json(), len(statements)
        finally:
            event.remove(h.app.state.engine, 'before_cursor_execute', listener)

    first = add_policy(h, {'kind': 'minimum_cover', 'minimum': 1})
    _, before = read_queries()
    policies = [add_policy(h, {'kind': 'minimum_cover', 'minimum': 1}) for _ in range(30)]
    with transaction(h.app.state.engine, write=True) as db:
        department = m.Department(organisation_id=h.seed['organisation_id'], name='Other synthetic department')
        db.add(department)
        db.flush()
        foreign_department = department.id
    foreign = add_policy(h, {'kind': 'minimum_cover', 'minimum': 1}, department_id=foreign_department)
    page, after = read_queries()
    ids = {row['id'] for row in shown_policies(page)}
    assert {first['id'], *(row['id'] for row in policies)} <= ids
    assert foreign['id'] not in ids
    assert after <= before + 1, (before, after)
    h.get('coordinator', '/api/rules/catalogue/people_per_session/detail?policy_id=' + foreign['id'], status=404)
