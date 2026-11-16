"""Long proof and export transactions opt in before reading application data."""
import sys

import pytest

from apps.api import main
from apps.api.services import rule_catalogue_v2
from scripts import catalogue_rules, group_rules


@pytest.mark.parametrize('path,role', [
    ('/api/rules/catalogue/migration?proof=true', 'coordinator'),
    ('/api/rules/catalogue/migration?version=2&proof=true', 'coordinator'),
    ('/api/audit.csv', 'auditor'),
])
def test_long_api_transactions_raise_idle_limit_before_reading(h, monkeypatch, path, role):
    calls = []
    original = main.allow_long_idle

    def guarded(db, minutes):
        assert not db.in_transaction()
        calls.append(minutes)
        original(db, minutes)

    monkeypatch.setattr(main, 'allow_long_idle', guarded)
    response = h.client(role).get(path)
    assert response.status_code == 200, response.text
    assert calls == [30]


@pytest.mark.parametrize('script,options', [
    (catalogue_rules, []),
    (catalogue_rules, ['--version', '2']),
    (group_rules, []),
])
def test_long_cli_transactions_raise_idle_limit_before_reading(h, monkeypatch, script, options):
    calls = []
    original = script.allow_long_idle

    def guarded(db, minutes):
        assert not db.in_transaction()
        calls.append(minutes)
        original(db, minutes)

    monkeypatch.setattr(script, 'allow_long_idle', guarded)
    monkeypatch.setattr(sys, 'argv', [script.__file__, '--database-url', str(h.app.state.engine.url),
        '--as', h.seed['identities']['coordinator'], *options])
    script.main()
    assert calls == [30]


def test_synchronous_proof_also_guards_callers_outside_the_worker(h, monkeypatch):
    calls = []
    original = rule_catalogue_v2.allow_long_idle

    def guarded(db, minutes):
        calls.append(minutes)
        original(db, minutes)

    monkeypatch.setattr(rule_catalogue_v2, 'allow_long_idle', guarded)
    result = h.get('coordinator', '/api/rules/catalogue/migration?version=2&proof=true')
    assert result['proof']['identical']
    assert calls == [30]
