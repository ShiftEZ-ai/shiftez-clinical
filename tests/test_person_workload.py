"""Person workload: issued sources, shared metric meanings, scoped rules and bounded reads."""
import copy
from datetime import date, datetime, timedelta
from time import process_time
import uuid

import pytest
from sqlalchemy import event

from apps.api import models as m
from apps.api.auth import Actor
from apps.api.db import transaction
from apps.api.services.person_workload import _limits, _totals, read
from packages.contracts.schemas import ROLE_NAMES
from packages.domain.quotas import METRIC_VERSION as QUOTA_METRIC_VERSION
from packages.domain.time import aware, local_midnight
from tests.governance_support import identity
from tests.test_rule_templates import activate, propose
from tests.test_performance_budget import python_budget


def endpoint(person, **query):
    return '/api/people/' + person + '/workload?on=2026-10-06' + ''.join('&' + k + '=' + v for k, v in query.items())


def periods(result):
    return {row['key']: row for row in result['periods']}


@pytest.mark.parametrize('role', ROLE_NAMES)
def test_workload_read_permissions_follow_existing_fairness_roles(h, role):
    if role not in h.seed['identities']:
        identity(h, role, [role])
    person, other, _ = h.seed['people']
    expected = 403 if role in {'credentialer', 'operator'} else 200
    result = h.get(role, endpoint(person), status=expected)
    if expected == 200:
        assert result['person_id'] == person
        assert result['unsupported_metrics'] == ['nights']
    h.get(role, endpoint(other), status=403 if role in {'staff', 'credentialer', 'operator'} else 200)


def test_unlinked_staff_and_people_without_department_affiliation_are_denied(h):
    identity(h, 'unlinked', ['staff'])
    h.get('unlinked', endpoint(h.seed['people'][0]), status=403)
    with transaction(h.app.state.engine, write=True) as db:
        person = m.Person(organisation_id=db.get(m.Person, h.seed['people'][0]).organisation_id,
                          display_name='Unrelated synthetic employee')
        db.add(person)
        db.flush()
        person_id = person.id
    h.get('admin', endpoint(person_id), status=404)


def test_workload_counts_current_publications_once_and_drafts_only_when_selected(h):
    person = h.seed['people'][0]
    w = h.shift(h.workspace(), people=[person])['workspace']
    publication = h.publish(w)['publication_id']
    amendment = h.detail(h.workspace(base=publication))['workspace']
    amendment = h.shift(amendment, '2026-10-06T08:00:00+03:00', '2026-10-06T12:00:00+03:00', people=[person])['workspace']
    published = periods(h.get('staff', endpoint(person)))
    selected = periods(h.get('coordinator', endpoint(person, workspace_id=amendment['id'])))
    assert published['this_rota']['totals']['duties'] == 1
    assert selected['this_rota']['totals']['duties'] == 2
    h.get('staff', endpoint(person, workspace_id=amendment['id']), status=403)
    h.publish(amendment)
    replaced = periods(h.get('coordinator', endpoint(person)))
    assert replaced['this_rota']['totals']['duties'] == 2
    assert replaced['this_month']['totals']['hours'] == 8
    assert replaced['rolling_12_weeks']['totals']['duties'] == 2


def test_workload_uses_effective_until_for_replaced_rotas(h):
    person = h.seed['people'][0]
    w = h.shift(h.workspace(), people=[person])['workspace']
    w = h.shift(w, '2026-10-08T08:00:00+03:00', '2026-10-08T12:00:00+03:00', people=[person])['workspace']
    publication = h.publish(w)['publication_id']
    replacement = h.command('coordinator', 'workspace.create', {
        'name': 'Synthetic replacement', 'starts_on': '2026-10-07', 'ends_on': '2026-10-11',
        'replaces_publication_id': publication})['workspace']
    replacement = h.shift(replacement, '2026-10-09T08:00:00+03:00', '2026-10-09T12:00:00+03:00', people=[person])['workspace']
    h.publish(replacement)
    result = periods(h.get('coordinator', endpoint(person)))
    assert result['this_month']['totals']['duties'] == 2  # October 4 + 9; October 8 is no longer in force.
    assert result['this_rota']['ends_on'] == '2026-10-07'


def test_workload_compares_exact_quota_bounds_and_dated_person_line_limits(h):
    person, other, _ = h.seed['people']
    w = h.shift(h.workspace(), people=[person])['workspace']
    w = h.shift(w, '2026-10-06T08:00:00+03:00', '2026-10-06T12:00:00+03:00', people=[person])['workspace']
    h.publish(w)
    quota = h.policy({'kind': 'block_quota', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01',
                      'metric': 'duties', 'comparison': 'target', 'amount': 3},
                     {'person_id': person}, severity='advisory')
    cap = activate(h, propose(h, {'kind': 'max_duties', 'maximum': 5},
                   per_person=[{'person_id': person, 'amount': 2, 'starts_on': '2026-10-05'}]))
    h.policy({'kind': 'max_hours', 'hours': 40}, {'person_id': other})
    result = periods(h.get('staff', endpoint(person)))['this_month']
    row = next(r for r in result['quotas'] if r['policy_id'] == quota['id'])
    assert (row['starts_on'], row['ends_on'], row['actual'], row['amount'], row['status']) == (
        '2026-10-01', '2026-11-01', 2, 3, 'below_target')
    limits = [r for r in result['limits'] if r['policy_id'] == cap['id']]
    week_start = local_midnight(date(2026, 10, 4), 'Asia/Riyadh')
    assert {(r['maximum'], r['actual']) for r in limits if aware(r['start']) == week_start} == {(5, 1), (2, 1)}
    assert not [r for r in result['limits'] if r['kind'] == 'max_hours']


@pytest.mark.parametrize('op', ['any', 'none'])
def test_workload_date_set_limits_and_quotas_match_validator(h, op):
    person = h.seed['people'][0]
    dates = h.configure('date_sets', {'name': 'Workload holidays', 'entries': [
        {'id': str(uuid.uuid4()), 'name': 'Holiday', 'first_day': '2026-10-06',
         'last_day': '2026-10-06', 'repeat': 'once'}]})['record']
    scope = {'person_id': person, 'conditions': {'conditions': [
        {'field': 'date_set_ids', 'op': op, 'values': [dates['id']], 'entry_names': ['Holiday']}]}}
    cap = h.policy({'kind': 'max_hours', 'hours': 1}, scope, severity='advisory')
    quota = h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
                      'metric': 'reserved_hours', 'comparison': 'maximum', 'amount': 1}, scope, severity='advisory')
    workspace = h.workspace()
    for day in ('06', '07'):
        workspace = h.shift(workspace, f'2026-10-{day}T08:00:00+03:00',
                            f'2026-10-{day}T12:00:00+03:00', people=[person])['workspace']
    validation = h.detail(workspace)['workspace']['validation']
    checked_limit = next(row for row in validation['findings']
                         if row['rule_id'] == cap['id'] and row['code'] == 'MAX_HOURS')
    checked_quota = next(row for row in validation['quota_report']['rows'] if row['policy_id'] == quota['id'])
    assert checked_limit['detail']['actual'] == checked_quota['actual'] == 4
    h.publish(workspace)
    result = h.get('staff', endpoint(person))
    rota = periods(result)['this_rota']
    limit = next(row for row in rota['limits'] if row['policy_id'] == cap['id'])
    counted = next(row for row in rota['quotas'] if row['policy_id'] == quota['id'])
    assert result['rule_basis'] == 'current_rules' and rota['complete'] is True
    assert rota['totals']['hours'] == 8
    assert limit['actual'] == checked_limit['detail']['actual'] == 4
    assert counted['actual'] == checked_quota['actual'] == 4
    assert counted['status'] == checked_quota['status'] == 'above_maximum'


def test_totals_use_planned_presence_units_and_reserved_limit_envelopes(h):
    person = h.seed['people'][0]
    w = h.shift(h.workspace(), people=[person])['workspace']
    data = h.snapshot(w)
    shift = data['shifts'][0]
    participant = shift['participants'][0]
    shift['end'] = participant['end'] = '2026-10-04T16:00:00+03:00'
    participant.update(overtime=True, team_lead=True, segments=[
        {'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T10:00:00+03:00'},
        {'start': '2026-10-04T12:00:00+03:00', 'end': '2026-10-04T16:00:00+03:00'}])
    shift['type_snapshot']['definition'].update(session_units=2, session_unit_basis='proportional')
    totals = _totals(data, person, date(2026, 10, 4), date(2026, 10, 11))
    assert totals == {'duties': 1, 'hours': 6, 'reserved_hours': 8, 'session_units': 1.5,
                      'overtime_duties': 1, 'overtime_hours': 6, 'overtime_offered_hours': 0, 'lead_turns': 1, 'nights': None}
    cap = h.policy({'kind': 'max_hours', 'hours': 10})
    data['policies'] = [cap]
    assert next(r for r in _limits(data, person, date(2026, 10, 4), date(2026, 10, 11))
                if r['start'] == '2026-10-03T21:00:00+00:00')['actual'] == 8
    shift['start'] = participant['start'] = '2026-10-31T22:00:00+03:00'
    shift['end'] = participant['end'] = '2026-11-01T02:00:00+03:00'
    participant['segments'] = []
    shift['type_snapshot']['definition']['session_unit_basis'] = 'fixed'
    november = _totals(data, person, date(2026, 11, 1), date(2026, 12, 1))
    assert november == {'duties': 0, 'hours': 2, 'reserved_hours': 2, 'session_units': 0,
                        'overtime_duties': 0, 'overtime_hours': 2, 'overtime_offered_hours': 0, 'lead_turns': 0, 'nights': None}


@pytest.fixture(scope='module')
def person_workload_scale(scale_harness):
    h = scale_harness('h')
    w = h.workspace('Synthetic department scale', '2026-10-01', '2026-11-01')
    with transaction(h.app.state.engine, write=True) as db:
        person = db.get(m.Person, h.seed['people'][0])
        department = db.get(m.Department, h.seed['department_id'])
        organisation = db.get(m.Organisation, department.organisation_id)
        peers = [m.Person(organisation_id=person.organisation_id, display_name='Synthetic peer ' + str(i)) for i in range(147)]
        db.add_all(peers)
        db.flush()
        people = h.seed['people'] + [p.id for p in peers]
        typ = db.get(m.AssignmentType, h.seed['type'])
        shifts = []
        for index in range(500):
            start = datetime.fromisoformat('2026-10-01T08:00:00+03:00') + timedelta(days=index % 30)
            shifts.append({'id': str(uuid.uuid4()), 'workspace_id': w['id'], 'assignment_type_id': typ.id,
                           'type_snapshot': {'name': typ.name, 'definition': copy.deepcopy(typ.definition)},
                           'service_ids': [], 'location_id': None, 'cancelled': False,
                           'start': start.isoformat(), 'end': (start + timedelta(hours=4)).isoformat(),
                           'participants': [{'id': str(uuid.uuid4()), 'person_id': people[index % len(people)],
                                             'start': start.isoformat(), 'end': (start + timedelta(hours=4)).isoformat(),
                                             'participation': 'staff', 'overtime': False}]})
        db.add(m.Publication(department_id=department.id, workspace_id=w['id'], sequence=1,
                             starts_on=date(2026, 10, 1), ends_on=date(2026, 11, 1), current=True,
                             published_by=h.seed['identities']['coordinator'],
                             validation={'contracts': {'block_quota': QUOTA_METRIC_VERSION, 'programmes': 'programmes-v1'}},
                             fingerprint='synthetic',
                             snapshot={'workspace': {**w, 'status': 'published'}, 'shifts': shifts,
                                       'timezone': organisation.timezone, 'metric_version': 'scheduled-v2'}))
        actor = Actor(h.seed['identities']['coordinator'], department.id, organisation.id, 'Synthetic reader',
                      frozenset({'coordinator'}), None, str(uuid.uuid4()))
    statements = []
    def count_statement(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(h.app.state.engine, 'before_cursor_execute', count_statement)
    try:
        started = process_time()
        with transaction(h.app.state.engine) as db:
            result = read(db, actor, people[0], on=date(2026, 10, 6))
        elapsed = process_time() - started
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', count_statement)
    return result, statements, elapsed


def test_department_scale_workload_read_has_a_fixed_query_and_runtime_budget(person_workload_scale):
    result, statements, _ = person_workload_scale
    assert periods(result)['this_month']['totals']['duties'] == 4
    assert len(statements) <= 32, 'Workload must batch department records rather than query per person or duty'
    assert sum('publications.snapshot' in statement for statement in statements) == 1, (
        'Select the workload context without fetching large publication payloads twice')


@pytest.mark.cpu_budget
def test_department_scale_workload_read_has_a_fixed_query_and_runtime_budget_cpu_budget(person_workload_scale):
    _, _, elapsed = person_workload_scale
    assert elapsed < python_budget(1), f'150 people and 500 duties must remain a bounded read: {elapsed:.3f}s'
# Include this file in hosted supervision and team-lead qualification.
