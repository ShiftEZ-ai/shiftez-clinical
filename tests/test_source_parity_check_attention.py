"""Unsaved rule checks expose the stored-rule counting warning without another comparison."""
import pytest
from sqlalchemy import func, select

from apps.api import models as m
from apps.api.db import transaction
from packages.domain.rule_lines import counting_attention
from .test_rule_catalogue_api import approve, entry, line, propose


@pytest.mark.parametrize('validity_end,counting_end,warns', [
    (None, '2027-01-01', True),
    ('2028-01-01', '2027-01-01', True),
    ('2027-01-01', '2027-01-01', False),
    ('2026-12-01', '2027-01-01', False),
    (None, None, False),
])
def test_check_returns_the_shared_warning_for_the_edited_line(h, validity_end, counting_end, warns):
    own = line(starts_on='2026-10-03', ends_on=validity_end, numbers={'max': {'value': 3}},
               settings={'block_quota': {'starts_on': '2026-10-03', 'ends_on': counting_end,
                                         'repeat': True, 'window_alignment': 'calendar_month'}})
    client = h.client('coordinator')
    with transaction(h.app.state.engine) as db:
        before = [db.scalar(select(func.count()).select_from(model)) for model in (m.Policy, m.AuditEvent)]
    response = client.post('/api/rules/catalogue/duties_per_month/check', json={'lines': [own]})
    assert response.status_code == 200, response.text
    found = response.json()
    assert found['issues'] == []
    expected = ([{'kind': 'counting_ends', 'line': 0, 'line_id': own['id'],
                  'counting_end': counting_end, 'validity_end': validity_end}] if warns else [])
    assert found['counting_attention'] == expected
    with transaction(h.app.state.engine) as db:
        assert before == [db.scalar(select(func.count()).select_from(model)) for model in (m.Policy, m.AuditEvent)]


def test_check_uses_inherited_counting_settings_and_explicit_typed_clear(h):
    own = line(starts_on='2026-10-03', numbers={'max': {'value': 3}}, measure='duties',
               period={'kind': 'fixed', 'days': 7, 'repeat': True})
    body = {'catalogue_version': 2, 'lines': [own], 'settings': {'block_quota': {
        'starts_on': '2026-10-03', 'ends_on': '2027-01-01', 'repeat': True,
        'window_alignment': 'fixed', 'window_days': 7}}}
    check = lambda: h.client('coordinator').post('/api/rules/catalogue/workload_limit/check', json=body).json()
    found = check()
    assert found['issues'] == []
    assert found['counting_attention'] == [{'kind': 'counting_ends', 'line': 0, 'line_id': own['id'],
                                           'counting_end': '2027-01-01', 'validity_end': None}]
    own['period']['ends_on'] = None
    cleared = check()
    assert cleared['issues'] == [] and cleared['counting_attention'] == []


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_pending_and_detail_checks_match_the_shared_stored_rule_warning(request, backend):
    h = request.getfixturevalue(backend)
    own = line(starts_on='2026-10-03', numbers={'max': {'value': 3}}, settings={'block_quota': {
        'starts_on': '2026-10-03', 'ends_on': '2027-01-01', 'window_alignment': 'calendar_month'}})
    pending = propose(h, 'duties_per_month', [own])['record']
    expected = counting_attention(pending)
    assert expected and entry(h, 'duties_per_month')['proposed']['check']['counting_attention'] == expected
    approve(h, pending)
    detail = h.get('coordinator', '/api/rules/catalogue/duties_per_month/detail')
    assert detail['check']['counting_attention'] == expected
    # The persisted body remains lossless; checking does not silently extend a finite counting period.
    assert detail['entry']['rule']['definition']['rule']['lines'][0]['settings'] == own['settings']


def test_check_always_returns_the_warning_array_including_invalid_and_nonquota_rules(h):
    invalid = h.client('coordinator').post('/api/rules/catalogue/duties_per_month/check',
                                          json={'lines': [line(numbers={'apply': {}})]}).json()
    assert invalid['issues'] and invalid['counting_attention'] == []
    other = h.client('coordinator').post('/api/rules/catalogue/duties_per_week/check',
                                        json={'lines': [line(numbers={'max': {'value': 3}})]}).json()
    assert other['issues'] == [] and other['counting_attention'] == []


def test_saved_checks_include_the_policy_validity_envelope(h):
    own = line(starts_on='2026-10-03', numbers={'max': {'value': 3}}, settings={'block_quota': {
        'starts_on': '2026-10-03', 'ends_on': '2027-01-01', 'window_alignment': 'calendar_month'}})
    pending = propose(h, 'duties_per_month', [own])['record']
    # Historical catalogue envelopes may end before an otherwise open line does.
    with transaction(h.app.state.engine, write=True) as db:
        from datetime import date
        db.get(m.Policy, pending['id']).ends_on = date(2027, 1, 1)
    assert entry(h, 'duties_per_month')['proposed']['check']['counting_attention'] == []
    approve(h, pending)
    detail = h.get('coordinator', '/api/rules/catalogue/duties_per_month/detail')
    assert detail['check']['counting_attention'] == counting_attention(detail['entry']['rule']) == []
