"""Issued evidence is stable while operational reads retain their observation time."""
from datetime import timedelta

from packages.domain.time import now, pin_clock
from .test_published_operational import frozen, issue


def test_observation_time_can_change_without_changing_issued_evidence(h):
    _, pub, _ = issue(h, days=(4,))
    evidence = frozen(h, pub)
    first = h.get('coordinator', '/api/publications/' + pub)
    moment = now()
    try:
        pin_clock(moment + timedelta(minutes=1))
        service = h.get('admin', '/api/catalogues?include=services')['services'][0]
        h.configure('services', {'name': 'Live label after publication', 'starts_on': service['starts_on']},
                    target=service['id'], revision=service['revision'])
        second = h.get('coordinator', '/api/publications/' + pub)
    finally:
        pin_clock(moment)
    assert second['operational'].pop('as_of') > first['operational'].pop('as_of')
    assert second == first
    assert frozen(h, pub) == evidence
