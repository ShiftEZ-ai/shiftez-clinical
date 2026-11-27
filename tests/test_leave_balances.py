"""Leave allowances share decision, withdrawal and Undo across single and batch requests."""
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from .test_leave_types import request_leave, selected


SETTINGS = 'department.leave_balance_settings'


def configure(h, days, *, month=1, carry=0, overrides=()):
    leave_type = selected(h)['id']
    previous = h.get('admin', '/api/leave-balance-settings')
    configuration = {'year_start_month': month, 'year_start_day': 1, 'carry_over_days': carry,
                     'allowances': [{'leave_type_id': leave_type, 'days': days},
                                    *({'leave_type_id': leave_type, **line} for line in overrides)]}
    return h.command('admin', SETTINGS, {'configuration': configuration, 'reason': 'Set annual allowances'},
                     revision=previous['revision'])


def request_between(h, start, end, **kwargs):
    return request_leave(h, start=f'{start}T00:00:00+03:00', end=f'{end}T00:00:00+03:00', **kwargs)['record']


def balances(h, row, role='approver', **kwargs):
    return h.get(role, f'/api/requests/{row["id"]}/leave-balances', **kwargs)


def decide(h, row, decision='approve', role='approver', **kwargs):
    confirmation = kwargs.pop('confirmation', None)
    return h.command(role, 'request.decide', {'decision': decision, 'reason': 'Review leave',
                     **({'balance_confirmation': confirmation} if confirmation is not None else {})},
                     row['id'], row['revision'], **kwargs)


def test_allowance_year_override_carry_and_decision_undo(h):
    change = configure(h, 4, month=4, carry=1, overrides=[{
        'person_id': h.seed['people'][0], 'starts_on': '2026-04-01', 'days': 2}])
    row = request_between(h, '2027-03-31', '2027-04-02')
    preview = balances(h, row)
    assert [(x['starts_on'], x['allowance_days'], x['carried_days'], x['projected_days']) for x in preview] == [
        ('2026-04-01', 2, 1, 2), ('2027-04-01', 2, 1, 2)]
    assert [x['remaining_days'] for x in balances(h, row, 'staff')] == [3, 3]
    balances(h, row, 'coordinator', status=403)
    h.get('staff', '/api/leave-balance-settings', status=403)
    approved = decide(h, row)['record']
    assert [x['remaining_days'] for x in balances(h, row)] == [2, 2]
    withdrawn = decide(h, approved, 'withdraw', 'staff')['record']
    assert withdrawn['status'] == 'withdrawn'
    assert [x['remaining_days'] for x in balances(h, row)] == [3, 3]
    restored = h.command('staff', 'request.undo', {'reason': 'Restore approved leave'},
                         withdrawn['id'], withdrawn['revision'])['record']
    assert restored['status'] == 'approved'
    assert [x['used_days'] for x in balances(h, row)] == [1, 1]
    h.command('approver', 'request.undo', {'reason': 'Undo approval'}, restored['id'], restored['revision'])
    assert [x['used_days'] for x in balances(h, row)] == [0, 0]
    undo = change['undo']
    assert undo['payload']['configuration'] is None
    h.command('admin', undo['kind'], undo['payload'], revision=undo['expected_revision'])
    assert balances(h, row) == []


def test_negative_confirmation_rechecks_latest_balance_and_audits_approval(h):
    configure(h, 0)
    first = request_between(h, '2027-03-01', '2027-03-02')
    second = request_between(h, '2027-03-02', '2027-03-03')
    warning = decide(h, second, status=409)['error']
    assert warning['code'] == 'LEAVE_BALANCE_CONFIRM'
    assert warning['details']['balances'][0]['projected_days'] == -1
    proof = warning['details']['balance_confirmation']
    first_proof = decide(h, first, status=409)['error']['details']['balance_confirmation']
    decide(h, first, confirmation=first_proof)
    current = decide(h, second, confirmation=proof, status=409)['error']['details']
    assert current['balances'][0]['projected_days'] == -2 and current['balance_confirmation'] != proof
    assert decide(h, second, confirmation=current['balance_confirmation'])['record']['status'] == 'approved'
    with transaction(h.app.state.engine) as db:
        event = db.scalar(select(m.AuditEvent).where(m.AuditEvent.target_id == second['id'],
                                                     m.AuditEvent.action == 'request.decide'))
        assert event.detail['leave_balance_overdrawn'] is True


def test_batch_confirmation_includes_earlier_rows_and_rolls_back_on_warning(h):
    configure(h, 1)
    first = request_between(h, '2027-03-01', '2027-03-02')
    second = request_between(h, '2027-03-02', '2027-03-03')
    items = [{'id': x['id'], 'expected_revision': x['revision']} for x in (first, second)]
    payload = {'items': items, 'reason': 'Review both requests', 'decision': 'approve'}
    refused = h.command('approver', 'request.decide_many', payload, status=409)['error']
    assert refused['code'] == 'LEAVE_BALANCE_CONFIRM'
    details = refused['details']
    assert details['id'] == second['id'] and details['completed'] == 0
    assert details['balances'][0]['projected_days'] == -1
    assert balances(h, second)[0]['used_days'] == 0
    items[1]['balance_confirmation'] = details['balance_confirmation']
    reply = h.command('approver', 'request.decide_many', payload)
    assert [x['status'] for x in reply['records']] == ['approved', 'approved']
    assert balances(h, second)[0]['remaining_days'] == -1
    h.command('approver', 'request.undo', {'batch_id': reply['batch_id'], 'reason': 'Undo both approvals'})
    assert balances(h, second)[0]['remaining_days'] == 1


def test_hidden_leave_type_stays_hidden_in_balance_and_warning(h):
    from .test_leave_disclosure import setting

    configure(h, 0)
    setting(h, 'none')
    row = request_between(h, '2027-03-01', '2027-03-02')
    balance = balances(h, row)[0]
    assert balance['leave_type_id'] is None and balance['leave_type_name'] == 'Leave'
    warning = decide(h, row, status=409)['error']['details']['balances'][0]
    assert warning == balance
    own = balances(h, row, 'staff')[0]
    assert own['leave_type_id'] == selected(h)['id'] and own['leave_type_name'] == 'Annual'


def test_approval_accounts_for_carry_lost_in_the_next_year(h):
    configure(h, 1, carry=1, overrides=[{
        'person_id': h.seed['people'][0], 'starts_on': '2028-01-01', 'days': 0}])
    next_year = request_between(h, '2028-01-02', '2028-01-03')
    assert decide(h, next_year)['record']['status'] == 'approved'
    previous = request_between(h, '2027-12-31', '2028-01-01')
    preview = balances(h, previous)
    assert [(x['starts_on'], x['projected_days']) for x in preview] == [
        ('2027-01-01', 1), ('2028-01-01', -1)]
    error = decide(h, previous, status=409)['error']
    assert error['code'] == 'LEAVE_BALANCE_CONFIRM'
    assert error['details']['balances'] == [preview[1]]


def test_anonymising_person_scrubs_current_and_undo_allowance(h):
    person_id = h.seed['people'][0]
    override = {'person_id': person_id, 'starts_on': '2027-01-01', 'days': 2}
    configure(h, 4, overrides=[override])
    change = configure(h, 5, overrides=[override])
    person = next(row for row in h.get('admin', '/api/catalogues?include=people')['people'] if row['id'] == person_id)
    inactive = h.configure('people', {'display_name': person['display_name'], 'active': False},
                           person_id, person['revision'])['record']
    h.command('admin', 'person.anonymise', {'reason': 'Remove synthetic details'}, person_id, inactive['revision'])
    live = h.get('admin', '/api/leave-balance-settings')
    assert live['revision'] != change['record']['revision']
    assert all(line['person_id'] is None for line in live['configuration']['allowances'])
    undo = change['undo']
    h.command('admin', undo['kind'], undo['payload'], revision=live['revision'])
    restored = h.get('admin', '/api/leave-balance-settings')['configuration']['allowances']
    assert len(restored) == 1 and restored[0]['days'] == 4 and restored[0]['person_id'] is None


def test_withdrawal_undo_cannot_overdraw_without_an_approver(h):
    configure(h, 1)
    first = request_between(h, '2027-03-01', '2027-03-02')
    second = request_between(h, '2027-03-02', '2027-03-03')
    approved = decide(h, first)['record']
    withdrawn = decide(h, approved, 'withdraw', 'staff')['record']
    assert decide(h, second)['record']['status'] == 'approved'
    undo = h.command('staff', 'request.undo', {'reason': 'Restore approved leave'},
                     withdrawn['id'], withdrawn['revision'], status=409)['error']
    assert undo['code'] == 'LEAVE_BALANCE_UNDO' and undo['details'] is None
    assert balances(h, first)[0]['remaining_days'] == 0
    requests = h.get('staff', '/api/requests')['requests']
    assert next(row for row in requests if row['id'] == first['id'])['status'] == 'withdrawn'


def test_recorded_leave_counts_and_started_approval_cannot_be_withdrawn(h):
    from .test_leave_types import absence

    configure(h, 4)
    row = request_between(h, '2026-10-01', '2026-10-02')
    approved = decide(h, row)['record']
    assert decide(h, approved, 'withdraw', 'staff', status=409)['error']['code'] == 'LEAVE_ALREADY_STARTED'
    absence(h, start='2026-10-05T00:00:00+03:00', end='2026-10-06T00:00:00+03:00')
    assert balances(h, row)[0]['used_days'] == 2
