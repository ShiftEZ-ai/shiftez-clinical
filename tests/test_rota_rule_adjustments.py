"""Department rules are the defaults for every rota; one rota may switch a rule off or use another number."""


def findings(h, w, rule_id):
    return [f for f in h.lifecycle(w, 'validate')['workspace']['validation']['findings'] if f.get('rule_id') == rule_id]


def test_rota_can_switch_off_or_change_a_department_rule(h):
    rest = h.policy({'kind': 'max_hours', 'hours': 12, 'window_days': 7})
    person = h.seed['people'][0]
    w = h.workspace('Adjusted rota', '2026-10-04', '2026-10-11')
    w = h.shift(w, '2026-10-04T08:00:00+03:00', '2026-10-04T20:00:00+03:00', people=[person])['workspace']
    w = h.shift(w, '2026-10-05T04:00:00+03:00', '2026-10-05T10:00:00+03:00', people=[person], location=1)['workspace']
    assert findings(h, w, rest['id']), '18 h work breaks the 12 h workload default'
    w = h.detail(w)['workspace']
    w = h.command('coordinator', 'workspace.rule_adjust', {'policy_id': rest['id'], 'mode': 'set', 'amount': 20, 'reason': 'Workload agreed for this rota'}, w['id'], w['revision'])['workspace']
    assert not findings(h, w, rest['id']), 'this rota uses 20 h'
    w = h.detail(w)['workspace']
    assert w['rule_adjustments'][0]['mode'] == 'set'
    w = h.command('coordinator', 'workspace.rule_adjust', {'policy_id': rest['id'], 'mode': 'off', 'reason': 'Rule paused for this rota'}, w['id'], w['revision'])['workspace']
    assert not findings(h, w, rest['id'])
    w = h.detail(w)['workspace']
    w = h.command('coordinator', 'workspace.rule_adjust', {'policy_id': rest['id'], 'mode': 'clear', 'reason': 'Back to the department rule'}, w['id'], w['revision'])['workspace']
    assert findings(h, w, rest['id'])
    other = h.workspace('Untouched rota', '2026-10-04', '2026-10-11')
    assert h.detail(other)['workspace']['rule_adjustments'] == [], 'adjustments belong to one rota'


def test_adjustments_are_checked(h):
    rest = h.policy({'kind': 'min_rest', 'hours': 12})
    w = h.workspace('Checked adjustments', '2026-10-04', '2026-10-11')
    h.command('coordinator', 'workspace.rule_adjust', {'policy_id': rest['id'], 'mode': 'set', 'amount': 500, 'reason': 'Too long'}, w['id'], w['revision'], status=422)
    h.command('coordinator', 'workspace.rule_adjust', {'policy_id': rest['id'], 'mode': 'set', 'reason': 'Missing number'}, w['id'], w['revision'], status=422)
    h.command('staff', 'workspace.rule_adjust', {'policy_id': rest['id'], 'mode': 'off', 'reason': 'Not mine'}, w['id'], w['revision'], status=403)
