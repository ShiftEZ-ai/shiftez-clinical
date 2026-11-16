"""SW3-B-04: a rota the approver returned waits on the coordinator's Home with who returned it and why."""


def test_returned_rota_is_on_the_coordinators_home(h):
    w = h.shift(h.workspace('Returned rota'))['workspace']
    w = h.lifecycle(w, 'submit')['workspace']
    h.command('approver', 'workspace.reopen', {'reason': 'Clinic room 2 is double booked'}, w['id'], w['revision'])
    item = next(i for i in h.get('coordinator', '/api/home')['attention'] if i['key'] == 'returned')
    sample, = item['sample']
    assert item['count'] == 1 and sample['rota_id'] == w['id'] and sample['rota_name'] == 'Returned rota'
    assert sample['reason'] == 'Clinic room 2 is double booked' and sample['by'] and sample['returned_at']
    assert not [i for i in h.get('approver', '/api/home')['attention'] if i['key'] == 'returned']
    assert not [i for i in h.get('staff', '/api/home')['attention'] if i['key'] == 'returned']
