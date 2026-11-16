"""Command normalization reuses JSON-ready branches without changing snapshot isolation."""
from copy import deepcopy
from datetime import date, datetime, timezone

import pytest

from apps.api.serialization import json_value


@pytest.mark.parametrize('copy_containers', [False, True])
def test_json_normalization_preserves_values_and_source(copy_containers):
    ready = {'people': [{'id': 'person', 'active': True}], 'flags': [None, 1, 2.5, False]}
    source = {'ready': ready, 'dates': [date(2026, 10, 23), datetime(2026, 10, 23, tzinfo=timezone.utc)],
              'tuple': ({'date': date(2026, 10, 24)}, 'plain')}
    before = deepcopy(source)
    result = json_value(source, copy_containers=copy_containers)
    assert source == before
    assert result == {'ready': ready, 'dates': ['2026-10-23', '2026-10-23T00:00:00+00:00'],
                      'tuple': [{'date': '2026-10-24'}, 'plain']}
    assert result is not source and result['dates'] is not source['dates']
    assert (result['ready'] is ready) is (not copy_containers)
    if copy_containers:
        result['ready']['people'][0]['id'] = 'changed'
        assert source == before


def test_default_json_copy_isolates_all_ready_snapshot_containers():
    source = {'nested': [{'list': [1, 2, 3]}]}
    result = json_value(source)
    result['nested'][0]['list'].append(4)
    assert source == {'nested': [{'list': [1, 2, 3]}]}
    assert json_value(source, copy_containers=False) is source


def test_command_normalization_does_not_mutate_the_handlers_reply(h, monkeypatch):
    from apps.api import commands

    captured, dispatch = [], commands.dispatch

    def recorded(*args, **kwargs):
        reply = dispatch(*args, **kwargs)
        captured.append((reply, deepcopy(reply)))
        return reply

    monkeypatch.setattr(commands, 'dispatch', recorded)
    h.shift(h.workspace())
    assert captured
    assert all(reply == before for reply, before in captured)
