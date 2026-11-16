"""A session over its maximum has its places filled: the maximum is a blocking issue, not an open place."""


def test_too_many_people_is_an_error_but_not_an_open_place(h):
    typ = h.configure('assignment_types', {'name': 'Single night cover', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 1}]}})['record']['id']
    w = h.shift(h.workspace(), typ=typ, people=h.seed['people'][:2])['workspace']
    short = h.shift(w, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', typ=typ, people=[])
    v = short['validation']
    over, empty = (s['id'] for s in sorted(h.detail(w)['shifts'], key=lambda s: s['start']))
    assert {r['status'] for r in v['coverage'] if r['shift_id'] == over} == {'covered'}
    assert {r['status'] for r in v['coverage'] if r['shift_id'] == empty} == {'gap'}
    assert any(f['code'] == 'MAXIMUM_PARTICIPANTS' and f['shift_id'] == over for f in v['findings'])
