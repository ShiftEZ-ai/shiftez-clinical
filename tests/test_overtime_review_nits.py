"""Defensive overtime source resolution and the shared request-kind contract."""
from typing import get_args

from packages.contracts import schemas as s
from packages.domain.rule_lines import resolved_overtime_offers
from apps.api.services import overtime_availability
from .test_overtime_volunteer import offer, offer_rule


def test_leave_input_kinds_are_request_kinds():
    assert set(get_args(s.LeaveInput.model_fields['kind'].annotation)) <= set(get_args(s.RequestKind))


def test_missing_overtime_source_line_refuses_request_clearly(h, monkeypatch):
    offer_rule(h)
    context = overtime_availability._context

    def missing_source(*args):
        data = context(*args)
        resolved = resolved_overtime_offers(data, h.seed['people'][0],
            '2026-10-05T00:00:00+03:00', '2026-10-06T00:00:00+03:00')
        assert resolved
        source = data['_overtime_resolved']['_overtime_originals'][resolved[0]['id']]
        source['definition']['rule']['lines'] = []
        return data

    monkeypatch.setattr(overtime_availability, '_context', missing_source)
    refusal = offer(h, status=400)
    assert refusal['error']['code'] == 'OVERTIME_NOT_ELIGIBLE'
    assert refusal['error']['message'] == (
        'Overtime is not available on 5 Oct 2026 at the selected times. Choose another date or time.')
