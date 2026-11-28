"""Published duties or a selected earlier draft can satisfy a shared minimum."""
import copy

import pytest

from packages.domain.validation import evaluate
from .test_finding_causes import cause_data, cause_snapshot, type_definition, uid
from .test_quota_working_day_proration import add_quota, checked_row, working_data


@pytest.fixture
def boundary_data(working_data):
    data, pid = working_data
    data['workspace'].update(starts_on='2026-12-01', ends_on='2026-12-06')
    data['quota_context'].update(start='2026-11-01T00:00:00+03:00', end='2027-01-01T00:00:00+03:00')
    type_definition(data, requirements=[])
    current = data['shifts'][0]
    current.update(start='2026-12-01T08:00:00+03:00', end='2026-12-01T12:00:00+03:00')
    previous = copy.deepcopy(current)
    previous.update(id=uid('boundary-duty'), workspace_id=uid('boundary-november-rota'),
        start='2026-11-29T08:00:00+03:00', end='2026-11-29T12:00:00+03:00')
    previous['participants'] = [{'id': uid('boundary-assignment'), 'person_id': pid,
        'shift_id': previous['id'], 'start': previous['start'], 'end': previous['end'],
        'participation': 'staff', 'overtime': False, 'team_lead': False, 'revision': 1}]
    data['shifts'].append(previous)
    data['quota_context']['publications'] = [{'publication_id': uid('boundary-publication'),
        'sequence': 1, 'workspace_id': previous['workspace_id']}]
    return data, pid


@pytest.mark.parametrize('window', [
    {'window_alignment': 'calendar_week'},
    {'window_alignment': 'fixed', 'window_days': 7},
    {'window_alignment': 'rolling', 'window_days': 7},
    {'window_alignment': 'rolling'},
])
def test_boundary_week_not_double_demanded(boundary_data, window):
    data, pid = boundary_data
    add_quota(data, pid, starts_on='2026-11-29', ends_on='2026-12-06', amount=1, **window)
    frozen = copy.deepcopy(data)
    checked, row = checked_row(data)
    assert data == frozen
    assert (row['actual'], row['amount'], row['status']) == (1, 1, 'met')
    assert (row['proration']['working_days'], row['proration']['full_working_days']) == (3, 5)
    assert not any(f['code'] == 'QUOTA_BELOW_MINIMUM' for f in checked['findings'])


@pytest.mark.parametrize('published', [False, True])
def test_unpublished_neighbor_never_counts_even_inside_candidate_dates(boundary_data, published):
    data, pid = boundary_data
    add_quota(data, pid, starts_on='2026-11-29', ends_on='2026-12-06', amount=1)
    previous = data['shifts'][-1]
    previous.update(start='2026-12-02T08:00:00+03:00', end='2026-12-02T12:00:00+03:00')
    previous['participants'][0].update(start=previous['start'], end=previous['end'])
    if not published:
        data['quota_context']['publications'] = []
    checked, row = checked_row(data)
    assert (row['actual'], row['amount'], row['status']) == (
        (1, 1, 'met') if published else (0, 1, 'below_minimum'))
    assert any(f['code'] == 'QUOTA_BELOW_MINIMUM' for f in checked['findings']) is not published


def test_calendar_month_minimum_counts_publication_across_split_rotas(boundary_data):
    data, pid = boundary_data
    data['workspace'].update(starts_on='2026-11-30', ends_on='2026-12-01')
    data['shifts'][0].update(start='2026-11-30T08:00:00+03:00', end='2026-11-30T12:00:00+03:00')
    add_quota(data, pid, starts_on='2026-11-01', ends_on='2026-12-01', amount=1, window_alignment='calendar_month')
    _, row = checked_row(data)
    assert (row['actual'], row['amount'], row['status']) == (1, 1, 'met')
    assert row['proration']['working_days'] == 1


def test_published_credit_reduces_the_locally_prorated_larger_minimum(boundary_data):
    data, pid = boundary_data
    add_quota(data, pid, starts_on='2026-11-29', ends_on='2026-12-06', amount=6)
    previous = copy.deepcopy(data['shifts'][-1])
    previous.update(id=uid('second-boundary-duty'), start='2026-11-30T08:00:00+03:00', end='2026-11-30T12:00:00+03:00')
    previous['participants'][0].update(id=uid('second-boundary-assignment'), shift_id=previous['id'],
                                     start=previous['start'], end=previous['end'])
    data['shifts'].append(previous)
    _, row = checked_row(data)
    assert (row['actual'], row['amount'], row['difference'], row['status']) == (2, 4, -2, 'below_minimum')
    assert (row['proration']['working_days'], row['proration']['full_working_days']) == (3, 5)
    for day in ('2026-12-01', '2026-12-02'):
        current = copy.deepcopy(data['shifts'][0])
        current.update(id=uid(day), start=day+'T08:00:00+03:00', end=day+'T12:00:00+03:00')
        current['participants'] = [{**previous['participants'][0], 'id': uid(day+'-assignment'),
            'shift_id': current['id'], 'start': current['start'], 'end': current['end']}]
        data['shifts'].append(current)
        data['focus_ids'].append(current['id'])
    _, row = checked_row(data)
    assert (row['actual'], row['amount'], row['status']) == (4, 4, 'met')


def test_neighbor_does_not_change_target_or_count_outside_rule_validity(boundary_data):
    data, pid = boundary_data
    add_quota(data, pid, starts_on='2026-11-29', ends_on='2026-12-06', amount=1, comparison='target')
    _, row = checked_row(data)
    assert (row['actual'], row['amount'], row['status']) == (0, 1, 'below_target')
    policy = add_quota(data, pid, starts_on='2026-11-29', ends_on='2026-12-06', amount=1)
    policy['starts_on'] = '2026-12-01'
    _, row = checked_row(data)
    assert (row['actual'], row['amount'], row['status']) == (0, 1, 'below_minimum')


@pytest.mark.parametrize('engine', ['greedy', 'cp_sat'])
@pytest.mark.parametrize('published', [False, True])
def test_schedulers_share_published_boundary_minimum(boundary_data, engine, published):
    from packages.domain import cp_sat, generation

    data, pid = boundary_data
    add_quota(data, pid, starts_on='2026-11-29', ends_on='2026-12-06', amount=1)
    if not published:
        data['shifts'] = data['shifts'][:1]
        data['quota_context']['publications'] = []
    result = (generation if engine == 'greedy' else cp_sat).generate(data, time_budget_seconds=10)
    assert result['status'] == 'candidate_feasible', result
    [row] = result['validation']['quota_report']['rows']
    assert (row['actual'], row['amount'], row['status']) == (1, 1, 'met')
    assert len(result['additions']) == (0 if published else 1)


@pytest.mark.parametrize('published', [False, True])
def test_snapshot_check_preview_and_publication_agree_at_month_boundary(h, published):
    who = h.seed['people'][0]
    earlier = h.shift(h.workspace('November boundary', '2026-11-29', '2026-12-01'),
        start='2026-11-29T08:00:00+03:00', end='2026-11-29T12:00:00+03:00', people=[who])['workspace']
    issued = h.publish(earlier) if published else None
    policy = h.policy({'kind': 'block_quota', 'starts_on': '2026-11-29', 'ends_on': '2026-12-06',
        'window_alignment': 'calendar_week', 'metric': 'duties', 'comparison': 'minimum', 'amount': 1}, {'person_id': who})
    optional = h.configure('assignment_types', {'name': 'Optional boundary session', 'starts_on': '2020-01-01',
        'definition': {'requirements': []}})['record']
    later = h.shift(h.workspace('December boundary', '2026-12-01', '2026-12-06'),
        start='2026-12-01T08:00:00+03:00', end='2026-12-01T12:00:00+03:00', people=[], typ=optional['id'])['workspace']
    data = h.snapshot(later)
    assert {s['workspace_id'] for s in data['shifts']} == ({earlier['id'], later['id']} if published else {later['id']})
    checked = h.lifecycle(later, 'validate')['workspace']
    report = h.detail(checked)['workspace']['validation']['quota_report']
    assert report['rows'] == evaluate(data)['quota_report']['rows']
    [row] = report['rows']
    assert (row['actual'], row['amount'], row['status']) == (
        (1, 1, 'met') if published else (0, 1, 'below_minimum'))
    preview = h.get('coordinator', f"/api/policies/reach?workspace_id={later['id']}&policy_id={policy['id']}&details=true")
    [count] = preview['details'][policy['id']]['lines'][0]['people'][0]['counts']
    assert (count['actual'], count['amount'], count['status']) == (row['actual'], row['amount'], row['status'])
    workload = h.get('coordinator', f"/api/people/{who}/workload?on=2026-12-01&workspace_id={later['id']}")
    [comparison] = next(period for period in workload['periods'] if period['key'] == 'this_rota')['quotas']
    assert (comparison['actual'], comparison['amount'], comparison['status']) == (row['actual'], row['amount'], row['status'])
    if published:
        assert report['source_publications'] == [{'publication_id': issued['publication_id'],
            'sequence': 1, 'workspace_id': earlier['id']}]
        later_issued = h.publish(checked)
        frozen = h.get('coordinator', f"/api/reports/{later_issued['publication_id']}")['quotas']
        assert frozen['rows'] == report['rows'] and frozen['source_publications'] == report['source_publications']
        workload = h.get('staff', f"/api/people/{who}/workload?on=2026-12-01")
        [comparison] = next(period for period in workload['periods'] if period['key'] == 'this_week')['quotas']
        assert (comparison['actual'], comparison['amount'], comparison['status']) == (1, 1, 'met')
    else:
        assert report['source_publications'] == []


def test_context_draft_eligibility_and_undo(h):
    first, cut, last = '2026-11-29', '2026-12-01', '2026-12-06'
    earlier = h.workspace('Earlier shared rota', first, cut)
    private = h.command('coordinator', 'workspace.create', {
        'name': 'Private planning rota', 'starts_on': first, 'ends_on': cut,
        'visibility': 'private', 'purpose': 'sandbox'})['workspace']
    later = h.workspace('Later planning rota', cut, last)
    choices = h.get('coordinator', f'/api/workspaces?context_before={cut}')['workspaces']
    assert [row['id'] for row in choices] == [earlier['id']]
    for source in (private, later):
        refused = h.command('coordinator', 'workspace.context', {
            'context_draft_id': source['id'], 'reason': 'Set context draft'},
            later['id'], later['revision'], status=422)
        assert refused['error']['code'] == 'INVALID_CONTEXT_DRAFT'
    linked = h.command('coordinator', 'workspace.context', {
        'context_draft_id': earlier['id'], 'reason': 'Set context draft'}, later['id'], later['revision'])['workspace']
    assert linked['context_draft_id'] == earlier['id']
    assert h.snapshot(linked)['context_draft']['id'] == earlier['id']
    assert h.lifecycle(linked, 'submit', status=409)['error']['code'] == 'CONTEXT_DRAFT_UNPUBLISHED'
    unlinked = h.lifecycle(linked, 'undo')['workspace']
    assert unlinked['context_draft_id'] is None
    assert 'context_draft' not in h.snapshot(unlinked)


def test_selected_draft_replaces_only_its_days_in_published_context(h):
    first, start, cut, last = '2026-11-28', '2026-11-29', '2026-12-01', '2026-12-06'
    official = h.shift(h.workspace('Earlier publication', first, cut),
        start=first+'T08:00:00+03:00', end=first+'T12:00:00+03:00')['workspace']
    official = h.shift(official, start=start+'T08:00:00+03:00', end=start+'T12:00:00+03:00')['workspace']
    h.publish(official)
    source = h.shift(h.workspace('Earlier alternative', start, cut),
        start=start+'T14:00:00+03:00', end=start+'T18:00:00+03:00')['workspace']
    later = h.command('coordinator', 'workspace.create', {
        'name': 'Later planning rota', 'starts_on': cut, 'ends_on': last, 'context_draft_id': source['id']})['workspace']

    def external():
        return {(row['workspace_id'], row['start'][:10]) for row in h.snapshot(later)['shifts']}

    assert external() == {(official['id'], first), (source['id'], start)}
    h.lifecycle(source, 'archive')
    assert external() == {(official['id'], first), (official['id'], start)}
