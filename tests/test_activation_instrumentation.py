"""Activation phase timings are additive evidence, not lifecycle semantics."""

import logging

from apps.api import models as m
from apps.api.db import transaction
from tests.test_rule_templates import propose


def test_activation_phase_timings_are_ordered_and_persisted(h, caplog):
    h.shift(h.workspace())
    proposal = propose(h, {'kind': 'restriction'})
    caplog.set_level(logging.INFO, logger='apps.api.commands')

    result = h.command('approver', 'policy.activate',
                       {'reason': 'Independent instrumentation review'}, proposal['id'], proposal['revision'])

    expected = ('receipts', 'feasibility_fingerprints', 'policy_impact', 'compile', 'validation', 'serialized_writer')
    timings = result['phase_timings']
    assert tuple(timings) == expected
    assert all(isinstance(value, float) and value >= 0 for value in timings.values())
    assert result['record']['status'] == 'active'
    with transaction(h.app.state.engine) as db:
        receipt = db.get(m.CommandReceipt, result['receipt_id'])
        assert receipt.result['phase_timings'] == timings
        assert receipt.result['record'] == result['record']
        assert receipt.result['impact'] == result['impact']
    events = [record for record in caplog.records if record.msg == 'policy_activation_phases']
    assert len(events) == 1
    assert events[0].phase_timings == timings
