"""Maximum-hours observations shared within one validation give the same findings as unshared evaluation."""
import copy
import json
import uuid

from packages.domain import validation


def findings(result):
    return json.dumps(result['findings'], sort_keys=True, default=str)


def test_rules_sharing_a_scope_keep_their_own_findings(h, monkeypatch):
    people = h.seed['people']
    base = h.policy({'kind': 'max_hours', 'hours': 6, 'window': 'day'}, exception=True)
    w = h.workspace()
    for start, end, staff in (('2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00', [people[0], people[1]]),
                              ('2026-10-04T13:00:00+03:00', '2026-10-04T17:00:00+03:00', [people[0], people[1]]),
                              ('2026-10-05T08:00:00+03:00', '2026-10-05T15:00:00+03:00', [people[0]]),
                              ('2026-10-06T08:00:00+03:00', '2026-10-06T16:00:00+03:00', [people[1]])):
        w = h.shift(w, start=start, end=end, people=staff)['workspace']
    data = h.snapshot(w)
    template = next(p for p in data['policies'] if p['id'] == base['id'])
    second = sorted(data['shifts'], key=lambda s: s['start'])[1]
    for participant in second['participants']:
        if participant['person_id'] == people[0]:
            participant['overtime'] = True

    def variant(change):
        policy = copy.deepcopy(template)
        policy['id'] = str(uuid.uuid4())
        change(policy)
        data['policies'].append(policy)
        return policy['id']

    # Each copy shares the scope and differs in one input the observation or its threshold depends on.
    ids = {label: variant(change) for label, change in (
        ('same', lambda p: None),
        ('hours10', lambda p: p['definition']['rule'].update(hours=10)),
        ('no_overtime', lambda p: p['definition']['rule'].update(include_overtime=False)),
        ('week', lambda p: p['definition']['rule'].update(window='week', hours=12)),
        ('rolling2', lambda p: p['definition']['rule'].update(window='rolling', window_days=2, hours=12)),
        ('rolling3', lambda p: p['definition']['rule'].update(window='rolling', window_days=3, hours=12)),
        ('later', lambda p: p.update(starts_on='2026-10-05')),
        ('ends', lambda p: p.update(ends_on='2026-10-05')),
        ('person1', lambda p: p['definition']['scope'].update(person_ids=[people[1]])),
        ('advisory', lambda p: p['definition'].update(severity='advisory')))}
    shared = validation.evaluate(copy.deepcopy(data))
    with monkeypatch.context() as patch:
        patch.setattr(validation, '_policy_scope_key', lambda policy: (id(policy), object()))
        unshared = validation.evaluate(copy.deepcopy(data))
    assert findings(shared) == findings(unshared)
    by_rule = {}
    for finding in shared['findings']:
        if finding['code'] == 'MAX_HOURS':
            by_rule.setdefault(finding['rule_id'], []).append(finding)
    assert len(by_rule[base['id']]) == len(by_rule[ids['same']]) > 0
    assert ids['hours10'] not in by_rule
