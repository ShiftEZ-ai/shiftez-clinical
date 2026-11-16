"""Offer options follow department configuration without cancelling existing consent."""
import pytest

from apps.api.services import swaps
from .governance_support import transition
from .test_staff_swaps import fixed_clock, manual_swap_workflow, offered, source_pair, swap_settings


@pytest.mark.parametrize('allowed', [['exchange', 'give'], ['exchange'], ['give'], []])
def test_new_offer_options_return_allowed_kinds_and_skip_disabled_eligibility(h, monkeypatch, allowed):
    _, first, _, _ = source_pair(h)
    swap_settings(h, swap_kinds_allowed=allowed)
    options = h.get('staff', '/api/swaps/options')
    assert options['allowed_kinds'] == allowed
    assert options['kind'] == (allowed[0] if allowed else None)
    if allowed:
        assert options['assignments'] and options['recipients']
        preview = h.get('staff', f'/api/swaps/options?assignment_id={first}&recipient_id={h.seed["people"][1]}')
        assert preview['kind'] == allowed[0] and preview['recipients'][0]['eligible']
    else:
        assert options['assignments'] == options['recipients'] == []
        assert not options['assignments_partial'] and not options['recipients_partial']

    def disabled_preview(*args, **kwargs):
        pytest.fail('Disabled kinds must be refused before evaluating eligibility')

    monkeypatch.setattr(swaps, 'Eligibility', disabled_preview)
    for kind in ('exchange', 'give'):
        if kind not in allowed:
            response = h.get('staff', f'/api/swaps/options?assignment_id={first}'
                             f'&recipient_id={h.seed["people"][1]}&kind={kind}', status=409)
            assert response['error']['code'] == 'SWAP_KIND_DISABLED'
            assert response['error']['message'] == 'This swap kind is not allowed by the department'


def test_unqualified_options_only_reads_the_staff_directory(h, monkeypatch):
    _, first, _, _ = source_pair(h)
    monkeypatch.setattr(swaps, 'Eligibility', lambda *args, **kwargs: pytest.fail(
        'the unqualified options read must not build a validator context'))
    result = h.get('staff', '/api/swaps/options')
    assert result['assignment_id'] == first  # the default duty, named but not checked
    assert result['assignments'] and result['recipients']
    assert all(row['eligible'] is None for row in result['recipients'])


def test_exchange_eligibility_stops_when_the_colleague_cannot_take_the_offered_duty(h, monkeypatch):
    from datetime import datetime
    from apps.api import models as m
    from apps.api.db import transaction
    from .governance_support import identity
    colleague = h.seed['people'][1]
    identity(h, 'staff_two', ['staff'], colleague)
    w = h.shift(h.workspace())['workspace']
    for day in (5, 6, 7):
        w = h.shift(w, f'2026-10-0{day}T08:00:00+03:00', f'2026-10-0{day}T12:00:00+03:00', people=[colleague])['workspace']
    source = h.get('coordinator', '/api/publications/' + h.publish(w)['publication_id'])['snapshot']
    offered = next(p['id'] for shift in source['shifts'] for p in shift['participants']
                   if p['person_id'] == h.seed['people'][0])
    trials, evaluate = [], swaps.evaluate
    def counted(*args, **kwargs):
        trials.append(1)
        return evaluate(*args, **kwargs)
    monkeypatch.setattr(swaps, 'evaluate', counted)
    path = f'/api/swaps/options?assignment_id={offered}&recipient_id={colleague}'
    assert h.get('staff', path)['recipients'][0]['eligible'] is True and len(trials) == 1
    # Unavailable for the offered duty: none of the colleague's three return duties can change that.
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.Availability(person_id=colleague, department_id=h.seed['department_id'],
            start=datetime.fromisoformat('2026-10-04T07:00:00+03:00'), end=datetime.fromisoformat('2026-10-04T13:00:00+03:00'),
            kind='approved_leave', private_reason='Synthetic leave', recorded_by=h.seed['identities']['coordinator']))
        db.get(m.Organisation, h.seed['organisation_id']).availability_revision += 1
    trials.clear()
    result = h.get('staff', path)['recipients'][0]
    assert result['eligible'] is False and [f['code'] for f in result['findings']] == ['UNAVAILABLE']
    assert len(trials) == 1


@pytest.mark.parametrize('kind', ['exchange', 'give'])
def test_existing_offer_options_keep_consented_kind_when_new_offers_are_disabled(h, kind):
    publication, first, second, _ = source_pair(h)
    row = offered(h, publication, first, kind=kind)
    swap_settings(h, swap_kinds_allowed=[])
    options = h.get('staff_two', '/api/swaps/options?swap_id=' + row['id'])
    assert options['kind'] == kind and options['allowed_kinds'] == []
    if kind == 'exchange':
        assert next(a for a in options['assignments'] if a['assignment_id'] == second)['eligible']
        row = transition(h, row, 'counteroffer', 'staff_two', {'assignment_id': second,
            'reason': 'Consent already offered before the setting changed'})['swap']
    else:
        assert options['eligible'] and options['assignments'] == []
        row = transition(h, row, 'accept', 'staff_two')['swap']
    assert transition(h, row, 'confirm')['swap']['status'] == 'accepted'
