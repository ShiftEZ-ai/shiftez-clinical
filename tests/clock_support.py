"""Freeze API time for counter assertions, including imported authentication clocks."""
from apps.api import models as m


def freeze_clock(monkeypatch, moment=None):
    moment = m.now() if moment is None else moment
    clock = moment if callable(moment) else lambda: moment
    monkeypatch.setattr(m, "clock_now", clock)
    return clock()
