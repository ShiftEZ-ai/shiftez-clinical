"""Focused contracts for the department-wide Why projection."""

from datetime import date, timedelta

import pytest

from packages.domain.rule_explainer import explain_rules
from packages.contracts.schemas import CAPABILITIES, PERMISSIONS, ROLE_NAMES
from .governance_support import identity
from .test_rule_catalogue_api import approve, line, propose


def test_rules_page_capability_is_within_rules_read():
    assert set(CAPABILITIES['ui.rules']) <= set(PERMISSIONS['rules.read'])
    assert set(CAPABILITIES['ui.rules']) <= set(ROLE_NAMES)
    assert set(PERMISSIONS['rules.read']) <= set(ROLE_NAMES)


def test_department_explained_reads_use_rules_read_permission(h):
    for role in ROLE_NAMES:
        if role not in h.seed['identities']:
            identity(h, role, [role])
        h.get(role, '/api/rules-explained?group_by=question',
              status=200 if role in PERMISSIONS['rules.read'] else 403)


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_department_explained_groups_legacy_rule_by_its_workload_question(h):
    policy = h.policy({'kind': 'max_duties', 'maximum': 5, 'window': 'week'})
    result = h.get('coordinator', '/api/rules-explained?group_by=question')
    row = next(rule for rule in result['rules'] if rule['rule_id'] == policy['id'])

    assert row['question_group'] == 'workload_group'
    assert row['unexplained'] is False
    assert 'at most 5 duties a week' in row['summary_sentence']
    assert 'a week' in row['summary_short']


def test_department_explained_conflicts_are_server_bound_and_reached(h):
    day = h.get('coordinator', '/api/rules/catalogue')['day']
    person = h.seed['people'][0]
    lines = [
        line(numbers={'max': {'value': 4, 'level': 'mandatory'}}),
        line(numbers={'max': {'value': 3, 'level': 'mandatory'}}, who={'conditions': [{'field': 'person_ids', 'values': [person]}]}),
    ]
    policy = propose(h, 'duties_per_month', lines, acknowledged=[lines[0]['id']])
    policy = approve(h, policy['record'])
    end_day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    workspace = h.shift(h.workspace('Why department sessions', day, end_day), start=f'{day}T08:00:00+03:00',
                        end=f'{day}T10:00:00+03:00', people=[person])['workspace']
    h.shift(workspace, start=f'{day}T11:00:00+03:00', end=f'{day}T13:00:00+03:00', people=[person])

    first = h.get('coordinator', f'/api/rules-explained?group_by=question&at={day}')
    second = h.get('coordinator', f'/api/rules-explained?group_by=question&at={day}')
    assert first == second
    assert first['status'] is None
    conflict = next(item for item in first['conflicts'] if policy['id'] in item['rule_ids'])
    assert len(conflict['line_ids']) == len(conflict['rule_ids']) == len(conflict['line_refs']) == 2
    assert conflict['line_refs'] == [1, 2]
    assert conflict['reach']['sessions_count'] == 2
    assert all(isinstance(value, int) and value >= 1 for value in conflict['line_refs'])


def test_department_people_table_is_null_without_person_workload_rules_and_is_private(h):
    day = h.get('coordinator', '/api/rules/catalogue')['day']
    empty = h.get('coordinator', f'/api/rules-explained?at={day}')
    assert empty['people_table'] is None
    assert h.get('staff', f'/api/rules-explained?at={day}', status=403)['error']['code'] == 'FORBIDDEN'


def test_department_people_table_uses_server_line_identity_and_omits_inherited_cells(h):
    day = h.get('coordinator', '/api/rules/catalogue')['day']
    person = h.seed['people'][0]
    policy = propose(h, 'duties_per_week', [
        line(numbers={'max': {'value': 5, 'level': 'mandatory'}}, who={'conditions': [{'field': 'person_ids', 'values': [person]}]}),
    ])
    approve(h, policy['record'])
    result = h.get('coordinator', f'/api/rules-explained?at={day}')
    table = result['people_table']
    assert table and table['columns']
    assert {row['kind'] for row in table['rows']} >= {'everyone', 'rank'}
    person_row = next(row for row in table['rows'] if row['kind'] == 'person' and row['id'] == person)
    assert person_row['cells']
    assert all('line_id' in cell for cell in person_row['cells'].values())
    assert len([row for row in table['rows'] if row['kind'] == 'person']) == 1
    assert all(row['kind'] != 'person' or row['cells'] for row in table['rows'])


def test_department_people_table_cell_keeps_overtime_place_and_window_qualifiers(h):
    day = h.get('coordinator', '/api/rules/catalogue')['day']
    person = h.seed['people'][0]
    service = h.seed['services'][0]
    end_day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    h.shift(h.workspace('Qualified workload cell', day, end_day), start=f'{day}T08:00:00+03:00',
            end=f'{day}T10:00:00+03:00', service=0, people=[person])
    source = line(where={'conditions': [{'field': 'service_ids', 'values': [service]}]},
                  numbers={'max': {'value': 2, 'level': 'mandatory'}},
                  settings={'block_quota': {'metric': 'duties', 'overtime': 'only',
                                             'window_alignment': 'calendar_month',
                                             'starts_on': day, 'ends_on': '2027-01-01'}})
    policy = approve(h, propose(h, 'duties_per_month', [source])['record'])
    result = h.get('coordinator', f'/api/rules-explained?at={day}')
    cells = [cell for row in result['people_table']['rows'] for cell in row['cells'].values()
             if cell.get('line_id') == source['id']]
    assert cells
    cell = cells[0]
    assert cell['rule_id'] == policy['id'] and cell['kind'] == 'line' and cell['qualified']
    assert h.seed['services'][0] and 'overtime' in cell['cell'].lower() and 'until 31 Dec' in cell['cell']
    assert any(action['action'] == 'open_rule' and action['enabled'] for action in cell['actions'])
    assert any('overtime' in column['label'].lower() for column in result['people_table']['columns'])


def test_department_explained_uses_rule_dates_and_own_weekday(h):
    day = '2026-11-11'  # Wednesday in the department's Asia/Riyadh timezone.
    undated = approve(h, propose(h, 'duties_per_week', [line(numbers={'max': {'value': 5}})])['record'])
    dated = approve(h, propose(h, 'duties_per_month', [line(
        starts_on='2026-11-10', ends_on='2026-11-15', weekdays=[2], start_time='23:30', end_time='01:30',
        numbers={'max': {'value': 4}})])['record'])

    result = h.get('coordinator', f'/api/rules-explained?group_by=question&at={day}')
    undated_sentence = next(rule['summary_sentence'] for rule in result['rules'] if rule['rule_id'] == undated['id'])
    dated_sentence = next(rule['summary_sentence'] for rule in result['rules'] if rule['rule_id'] == dated['id'])
    assert '2026' not in undated_sentence
    assert 'Wednesday' not in undated_sentence
    assert '10 November 2026–14 November 2026, Wednesday, 23:30–01:30' in dated_sentence


def test_rota_explainer_keeps_clipped_workspace_dates(h):
    policy = approve(h, propose(h, 'duties_per_week', [line(
        starts_on='2026-11-10', ends_on='2026-11-15', numbers={'max': {'value': 4}})])['record'])
    workspace = h.shift(h.workspace('Clipped rule dates', '2026-11-11', '2026-11-13'),
                        start='2026-11-11T08:00:00+03:00', end='2026-11-11T12:00:00+03:00')['workspace']
    result = explain_rules(h.snapshot(workspace))
    sentence = next(rule['summary_sentence'] for rule in result['rules'] if rule['rule_id'] == policy['id'])
    assert '11 November 2026–12 November 2026, every day' in sentence


def test_existing_why_stale_revision_still_fails_closed(h):
    created = h.shift(h.workspace('Why stale input'), people=[h.seed['people'][0]])
    stale = dict(created['workspace'])
    shift_id = h.detail(stale)['shifts'][0]['id']
    current = h.detail(stale)['workspace']
    h.shift(current, people=[h.seed['people'][0]])
    response = h.client('coordinator').post('/api/planner/why', json={
        'workspace_id': stale['id'], 'expected_revision': stale['revision'], 'shift_ids': [shift_id]})
    assert response.status_code == 409
