"""Rules retain their full evidence window while identifying history outside the rota; the client words a window by
where it lies against the rota (RUL-E01 / LIVE-18) and verbs agree with their count (FD-07), in Node."""
from packages.domain.validation import evaluate
from .test_block_quotas import quota
from .test_client_dates_roles import run


def test_lookback_quota_findings_expose_complete_window_in_domain_and_api(h):
    policy = quota(h, starts_on='2026-10-01', ends_on='2026-11-26', window_days=28, amount=0)
    w = h.workspace(start='2026-10-28', end='2026-10-29')
    saved = h.shift(w, start='2026-10-28T08:00:00+03:00', end='2026-10-28T12:00:00+03:00')
    w = saved['workspace']
    domain = [f for f in evaluate(h.snapshot(w))['findings'] if f['rule_id'] == policy['id']]
    api = [f for f in h.detail(w)['workspace']['validation']['findings'] if f['rule_id'] == policy['id']]
    assert len(domain) == 28 and api == domain
    assert all(f['outside_rota'] for f in domain)
    first = min(domain, key=lambda f: f['start'])
    assert first['window'] == {'start': '2026-09-30T21:00:00+00:00', 'end': '2026-10-28T21:00:00+00:00'}
    assert all(f['window'] == {'start': f['start'], 'end': f['end']} for f in domain)
    assert not saved['validation']['valid']


def test_rule_window_inside_rota_is_not_labelled_lookback(h):
    policy = quota(h, starts_on='2026-10-01', ends_on='2026-11-01', amount=0)
    w = h.workspace(start='2026-10-01', end='2026-11-01')
    result = h.shift(w)['validation']
    findings = [f for f in result['findings'] if f['rule_id'] == policy['id']]
    assert len(findings) == 1 and not findings[0]['outside_rota']
    assert 'window' not in findings[0]


def test_week_limit_before_rota_carries_its_week_window(h):
    policy = h.policy({'kind': 'max_duties', 'window': 'week', 'maximum': 1},
                      scope={'person_id': h.seed['people'][0]})
    w = h.workspace(start='2026-10-05', end='2026-10-12')
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00')['workspace']
    result = h.shift(w, start='2026-10-06T08:00:00+03:00', end='2026-10-06T12:00:00+03:00')['validation']
    finding = next(f for f in result['findings'] if f['rule_id'] == policy['id'])
    assert finding['outside_rota']
    assert finding['window'] == {'start': '2026-10-03T21:00:00+00:00', 'end': '2026-10-10T21:00:00+00:00'}


ROTA = {'workspace': {'starts_on': '2026-12-20', 'ends_on': '2026-12-23', 'validation': {}}}


def words(client_modules, start, end):
    finding = {'code': 'MIN_DUTIES', 'message': 'x', 'severity': 'error', 'outside_rota': True, 'window': {'start': start, 'end': end}}
    data = {**ROTA, 'findings': [finding]}
    return run(client_modules, 'findings.js', 'm.outsideRota(m.plainFindings(input).findings[0])', data)


def test_straddling_window_label(client_modules):
    # 20–27 Dec starts inside the 3-day rota: not a look-ahead "after" it.
    text = words(client_modules, '2026-12-19T21:00:00Z', '2026-12-26T21:00:00Z')
    assert text.startswith('Counted over') and text.endswith('partly outside this rota'), text


def test_wholly_outside_windows_keep_look_back_and_look_ahead(client_modules):
    assert 'look-ahead after' in words(client_modules, '2026-12-22T21:00:00Z', '2026-12-29T21:00:00Z')
    assert 'look-back before' in words(client_modules, '2026-12-12T21:00:00Z', '2026-12-19T21:00:00Z')
    # A week starting before the rota and ending inside it is still its look-back.
    assert 'look-back before' in words(client_modules, '2026-12-16T21:00:00Z', '2026-12-23T21:00:00Z')


def test_verb_agrees_with_its_count(client_modules):
    assert run(client_modules, 'dom.js', '[m.agree(1),m.agree(2),m.agree(0)]') == ['needs', 'need', 'need']
