"""Operators receive the same prorated quota in checks, rule previews and issued reports."""
import pytest

from apps.api.services.issues import issue_key
from packages.contracts.schemas import RotaSummary
from .test_department_quota_calendar import settings
from .test_resources_calendars import calendar
from .test_rule_catalogue_api import approve, line, person, propose


def selected_calendar(h):
    chosen = calendar(h, week=[{'weekday': day, 'windows': [{'start_time': '00:00', 'end_time': '24:00'}]}
                               for day in (6, 0, 1, 2, 3)])
    settings(h, operating_calendar_id=chosen['id'], weekend_days=[5, 6])
    return chosen


def partial_rota(h, selected=True):
    if selected:
        calendar_id = selected_calendar(h)['id']
    else:
        settings(h, operating_calendar_id=None, weekend_days=[4, 5])
        calendar_id = None
    person = h.seed['people'][0]
    policy = h.policy({'kind': 'block_quota', 'starts_on': '2026-09-27', 'ends_on': '2026-10-04',
        'window_alignment': 'calendar_week', 'metric': 'duties', 'comparison': 'range',
        'amount': 6, 'maximum_amount': 6}, {'person_id': person})
    workspace = h.workspace('Partial week quota API', '2026-10-01', '2026-10-04')
    proration = {'configured_amount': 6, 'amount': 2, 'working_days': 1, 'full_working_days': 5,
        'starts_on': '2026-09-27', 'ends_on': '2026-10-04', 'calendar_id': calendar_id, 'rounding': 'up'}
    return workspace, policy, person, proration


def rows_for(check, policy):
    report = check['quota_report']
    assert report['complete'], report
    rows = [row for row in report['rows'] if row['policy_id'] == policy['id']]
    assert len(rows) == 2
    return {row['comparison']: row for row in rows}


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('selected_calendar', [True, False])
def test_prorated_amount_display_and_calculation_agree_across_check_preview_and_publication(request, backend, selected_calendar):
    h = request.getfixturevalue(backend)
    workspace, policy, person, proration = partial_rota(h, selected_calendar)
    for start, end in [('08:00', '10:00'), ('13:00', '15:00')]:
        workspace = h.shift(workspace, start=f'2026-10-01T{start}:00+03:00',
                            end=f'2026-10-01T{end}:00+03:00', people=[person])['workspace']
    checked = h.lifecycle(workspace, 'validate')['workspace']
    check = h.detail(checked)['workspace']['validation']
    rows = rows_for(check, policy)
    minimum, maximum = rows['minimum'], rows['maximum']
    assert (minimum['actual'], minimum['amount'], minimum['difference'], minimum['status']) == (2, 2, 0, 'met')
    assert minimum['display'] == '2 of 2 (prorated: 1 of 5 days)'
    assert minimum['proration'] == proration
    assert (maximum['actual'], maximum['amount'], maximum['status']) == (2, 6, 'met')
    assert not maximum.get('proration') and maximum['display'] == '2 of 6'
    assert not [finding for finding in check['findings'] if finding['rule_id'] == policy['id']]

    preview = h.get('coordinator', f"/api/policies/reach?workspace_id={workspace['id']}&policy_id={policy['id']}&details=true")
    counts = {count['comparison']: count for line in preview['details'][policy['id']]['lines']
              for who in line['people'] if who['person_id'] == person for count in who['counts']}
    assert set(counts) == {'minimum', 'maximum'}
    for comparison, row in rows.items():
        assert counts[comparison] == {key: row[key] for key in (
            'starts_on', 'ends_on', 'actual', 'amount', 'comparison', 'metric', 'status', 'proration', 'display') if key in row}

    published = h.publish(checked)
    issued = h.get('coordinator', f"/api/reports/{published['publication_id']}")['quotas']
    issued_rows = {row['comparison']: row for row in issued['rows'] if row['policy_id'] == policy['id']}
    assert issued_rows == rows


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('selected_calendar', [True, False])
def test_empty_partial_rota_retains_proration_cause_in_typed_summary_and_never_scales_maximum(request, backend, selected_calendar):
    h = request.getfixturevalue(backend)
    workspace, policy, person, proration = partial_rota(h, selected_calendar)
    workspace = h.lifecycle(workspace, 'validate')['workspace']
    check = h.detail(workspace)['workspace']['validation']
    rows = rows_for(check, policy)
    assert (rows['minimum']['actual'], rows['minimum']['amount'], rows['minimum']['status']) == (0, 2, 'below_minimum')
    assert rows['minimum']['proration'] == proration
    assert rows['minimum']['display'] == '0 of 2 (prorated: 1 of 5 days)'
    assert rows['maximum']['amount'] == 6 and rows['maximum']['status'] == 'met'
    assert not rows['maximum'].get('proration')
    [finding] = [row for row in check['findings'] if row['rule_id'] == policy['id']]
    assert finding['code'] == 'QUOTA_BELOW_MINIMUM' and finding['person_id'] == person
    assert finding['detail']['display'] == rows['minimum']['display']
    assert finding['detail']['proration'] == proration
    assert (finding['cause']['code'], finding['cause']['required'], finding['cause']['sessions_found']) == ('partial_window', 2, 0)
    assert finding['cause']['proration'] == proration and finding['fix_hints']

    summary = h.get('coordinator', f"/api/workspaces/{workspace['id']}/summary")
    serializable = RotaSummary.model_validate(summary).model_dump(mode='json')
    [projected] = [row for row in serializable['findings'] if row['key'] == issue_key(finding)]
    assert projected['cause']['proration'] == proration and projected['cause']['code'] == 'partial_window'
    assert projected['person_id'] == person and projected['rule_id'] == policy['id']
    [group] = serializable['cause_groups']
    assert (group['code'], group['count']) == ('partial_window', 1)
    assert group['finding_keys'] == [issue_key(finding)] and group['person_ids'] == [person]


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_explicit_rate_target_uses_original_block_when_the_rota_clips_it(request, backend):
    h = request.getfixturevalue(backend)
    chosen = selected_calendar(h)
    who = h.seed['people'][0]
    policy = h.policy({'kind': 'block_quota', 'starts_on': '2026-09-27', 'ends_on': '2026-10-04',
        'metric': 'duties', 'comparison': 'target', 'amount': 6,
        'prorate_department_days': True, 'reference_days': 7, 'rounding': 'nearest'},
        {'person_id': who}, severity='advisory')
    workspace = h.lifecycle(h.workspace('Explicit target on partial rota', '2026-10-01', '2026-10-04'), 'validate')['workspace']
    report = h.detail(workspace)['workspace']['validation']['quota_report']
    assert report['complete'], report
    [row] = [row for row in report['rows'] if row['policy_id'] == policy['id']]
    assert row['amount'] == 2 and row['actual'] == 0
    assert row['proration'] == {'configured_amount': 6, 'amount': 2, 'working_days': 1, 'full_working_days': 5,
        'starts_on': '2026-09-27', 'ends_on': '2026-10-04', 'calendar_id': chosen['id'], 'rounding': 'up'}
    assert row['target_calculation']['department_days'] == 7
    assert row['target_calculation']['calculated_amount'] == 6
    assert row['display'] == '0 of 2 (prorated: 1 of 5 days)'


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('repeat', [False, True], ids=['finite', 'open_repeating'])
def test_catalogue_rolling_quota_keeps_each_complete_window_instead_of_the_rule_horizon(request, backend, repeat):
    h = request.getfixturevalue(backend)
    selected_calendar(h)
    who = h.seed['people'][0]
    own = line(who=person(who), measure='duties',
        numbers={'min': {'value': 6, 'level': 'mandatory'}},
        period={'kind': 'rolling', 'days': 7, 'starts_on': '2026-10-04',
                'ends_on': None if repeat else '2026-11-01', 'repeat': repeat})
    policy = approve(h, propose(h, 'workload_limit', [own], catalogue_version=2)['record'])
    workspace = h.lifecycle(h.workspace(), 'validate')['workspace']
    report = h.detail(workspace)['workspace']['validation']['quota_report']
    assert report['complete'], report
    rows = [row for row in report['rows'] if row['policy_id'] == policy['id']]
    assert len(rows) == 7
    [complete] = [row for row in rows if row['starts_on'] == '2026-10-04' and row['ends_on'] == '2026-10-11']
    assert complete['amount'] == 6 and complete['actual'] == 0
    assert complete['comparison'] == 'minimum' and complete['status'] == 'below_minimum'
    assert not complete.get('proration')


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_neighbour_publication_satisfies_the_partial_rota_minimum_and_counts_towards_maximum(request, backend):
    h = request.getfixturevalue(backend)
    selected_calendar(h)
    who = h.seed['people'][0]
    prior = h.workspace('Earlier part of the same week', '2026-09-27', '2026-10-01')
    for start, end in [('08:00', '10:00'), ('13:00', '15:00')]:
        prior = h.shift(prior, start=f'2026-09-27T{start}:00+03:00',
                        end=f'2026-09-27T{end}:00+03:00', people=[who])['workspace']
    issued = h.publish(prior)
    policy = h.policy({'kind': 'block_quota', 'starts_on': '2026-09-27', 'ends_on': '2026-10-04',
        'window_alignment': 'calendar_week', 'metric': 'duties', 'comparison': 'range',
        'amount': 6, 'maximum_amount': 6}, {'person_id': who})
    workspace = h.lifecycle(h.workspace('Unfilled later part of the week', '2026-10-01', '2026-10-04'), 'validate')['workspace']
    check = h.detail(workspace)['workspace']['validation']
    assert issued['publication_id'] in {source['publication_id'] for source in check['quota_report']['source_publications']}
    rows = rows_for(check, policy)
    assert (rows['minimum']['actual'], rows['minimum']['amount'], rows['minimum']['status']) == (2, 2, 'met')
    assert (rows['maximum']['actual'], rows['maximum']['amount'], rows['maximum']['status']) == (2, 6, 'met')
    assert not [row for row in check['findings'] if row['rule_id'] == policy['id']]


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_selected_department_calendar_creation_undo_preserves_its_reference(request, backend):
    h = request.getfixturevalue(backend)
    chosen = selected_calendar(h)
    payload = {'catalogue': 'operating_calendars', 'reason': 'Undo synthetic calendar creation'}
    rejected = h.command('admin', 'catalogue.undo_create', payload, chosen['id'], chosen['revision'], status=409)
    assert rejected['error']['code'] == 'INTEGRITY_CONFLICT'
    assert h.get('admin', '/api/me')['department']['operating_calendar_id'] == chosen['id']
    assert chosen['id'] in {row['id'] for row in h.get('admin', '/api/catalogues')['operating_calendars']}
    settings(h, operating_calendar_id=None)
    undone = h.command('admin', 'catalogue.undo_create', payload, chosen['id'], chosen['revision'])
    assert undone['removed'] == 1 and undone['removed_ids'] == [chosen['id']]
    assert chosen['id'] not in {row['id'] for row in h.get('admin', '/api/catalogues')['operating_calendars']}
