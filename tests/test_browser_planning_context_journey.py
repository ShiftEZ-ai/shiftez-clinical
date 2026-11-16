"""Small real wire-consumption probes; records are never a fillability proof."""
import re

import pytest
from playwright.sync_api import expect
from sqlalchemy import select
from apps.api import models as m
from apps.api.db import transaction
from .test_browser import auto_setup, browser, live_server, sign_in, navigate, open_rota, open_scheduling  # noqa: F401
from .browser_acceptance_support import shot

pytestmark = pytest.mark.browser


def test_recorded_facts_are_dated_incomplete_and_readonly(browser, live_server, h):
    w = h.shift(h.workspace('Fictional December context', '2026-12-01', '2027-01-01'),
        start='2026-12-06T08:00:00+03:00', end='2026-12-06T12:00:00+03:00', people=[])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_rota(page, w['id'])
        open_scheduling(page)
        before = h.detail(w)
        auto_setup(page.locator('#auto-panel'))  # D76: the setup's fields open under Change
        with page.expect_response(lambda r: r.url.endswith('/readiness')) as answer:
            page.get_by_role('button', name='Check the people already assigned', exact=True).click()
        assert answer.value.ok, answer.value.text()
        value = answer.value.json()
        assert value['scope_window'] == {'start': '2026-12-01', 'end': '2027-01-01'}
        assert value['checked'] and value['complete'] is False
        expect(page.locator('.auto-readiness')).to_contain_text('whether every place can be filled, are not checked here')
        assert h.detail(w) == before
        shot(page, 'december-recorded-facts-1440-light', full_page=False)
    finally:
        context.close()


def test_people_evidence_uses_observation_and_fingerprint_without_writing(browser, live_server, h):
    h.policy({'kind': 'minimum_cover', 'minimum': 2})
    w = h.shift(h.workspace('Fictional evidence'), people=[])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', color_scheme='dark')
    try:
        open_rota(page, w['id'])
        before = h.detail(w)
        navigate(page, f"schedule/issues?draft={w['id']}")
        row = page.locator('#main section[data-level=must] .fix-list > li[data-cause]').first
        row.get_by_role('button', name='Show evidence', exact=True).click()
        with page.expect_response(lambda r: '/evidence' in r.url) as answer:
            row.get_by_role('button', name='People for this duty', exact=True).click()
        assert answer.value.ok, answer.value.text()
        value = answer.value.json()
        payload = answer.value.request.post_data_json
        assert payload['expected_fingerprint'] == before['current_fingerprint']
        assert payload['session_ids'] == [value['observation']['shift_id']]
        assert value['complete'] is False and value['requirement_scope_complete'] is False
        dialog = page.get_by_role('region', name='People recorded for this duty', exact=True)  # a list: a docked panel (C26)
        expect(dialog).to_contain_text('it does not show who would resolve the whole requirement')
        expect(dialog.locator('.why-list li')).to_have_count(len(value['candidates']))
        assert h.detail(w) == before
        shot(page, 'duty-evidence-1440-dark', full_page=False)
    finally:
        context.close()


def test_linked_exception_has_independent_decline_and_keeps_history(browser, live_server, h):
    from .test_browser_rule_catalogue import flag
    flag(h)
    rule = h.policy({'kind': 'max_duties', 'maximum': 2, 'window': 'week'}, exception=True)
    pending = h.command('coordinator', 'exception.create', {'policy_id': rule['id'],
        'person_id': h.seed['people'][0], 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
        'reason': 'Fictional linked exception'})['record']
    from .test_browser import navigate
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page, 'rules?exception=' + pending['id'])
        row = page.locator('.dt-linked[aria-current=true]')
        expect(row).to_contain_text('Fictional linked exception')
        expect(row.get_by_role('button', name='Approve exception', exact=True)).to_be_visible()
        row.get_by_role('button', name='Decline exception', exact=True).click()
        dialog = page.get_by_role('dialog', name='Decline exception request', exact=True)
        dialog.get_by_label('Reason', exact=True).fill('Independent fictional refusal')
        with page.expect_response(lambda r: r.url.endswith('/api/commands')
                and r.request.post_data_json.get('kind') == 'exception.end') as answer:
            dialog.get_by_role('button', name='Decline', exact=True).click()
        assert answer.value.ok, answer.value.text()
        assert answer.value.request.post_data_json['target_id'] == pending['id']
        expect(dialog).to_have_count(0)
        page.reload()
        expect(page.locator('#main[data-loaded-view]')).to_have_count(1)
        ended = next(r for r in h.get('approver', '/api/catalogues')['exceptions'] if r['id'] == pending['id'])
        assert ended['status'] == 'withdrawn'
        assert ended['policy_id'] == rule['id'] and ended['person_id'] == pending['person_id']
        with transaction(h.app.state.engine) as db:
            versions = list(db.scalars(select(m.DefinitionHistory).where(
                m.DefinitionHistory.kind == 'exception', m.DefinitionHistory.object_id == pending['id'])
                .order_by(m.DefinitionHistory.object_revision)))
            assert [v.snapshot['status'] for v in versions] == ['proposed', 'withdrawn']
            assert versions[-1].reason == 'Independent fictional refusal'
        assert next(p for p in h.get('approver', '/api/catalogues')['policies'] if p['id'] == rule['id'])['status'] == 'active'
        shot(page, 'linked-exception-declined-1440-light', full_page=False)
    finally:
        context.close()


def test_rule_review_exposes_decline_and_retains_current_rule(browser, live_server, h):
    from .test_browser_rule_catalogue import flag, weekly
    from .test_browser_rule_catalogue_details import propose_off, entry
    from .test_browser import navigate
    flag(h)
    weekly(h)
    current = entry(h, 'duties_per_week')['rule']
    proposed = propose_off(h)
    context, page = sign_in(browser, live_server, 'approver', color_scheme='dark')
    try:
        navigate(page, 'rules/duties_per_week')
        waiting = page.locator('.rc-waiting')
        waiting.get_by_role('button', name='Decline', exact=True).click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_label('Reason', exact=True).fill('Keep the independently approved current rule')
        with page.expect_response(lambda r: r.url.endswith('/api/commands')
                and r.request.post_data_json.get('kind') == 'policy.decline') as answer:
            dialog.get_by_role('button', name='Decline', exact=True).click()
        assert answer.value.ok, answer.value.text()
        assert answer.value.request.post_data_json['target_id'] == proposed['id']
        expect(dialog).to_have_count(0)
        page.reload()
        expect(page.locator('.rc-waiting')).to_have_count(0)
        assert entry(h, 'duties_per_week')['rule'] == current
        history = h.get('approver', '/api/rules/catalogue/duties_per_week/history')['policies']
        retained = next(p for p in history if p['id'] == proposed['id'])
        assert retained['status'] == 'retired' and retained['definition'] == proposed['definition']
        with transaction(h.app.state.engine) as db:
            decision = db.scalar(select(m.DefinitionHistory).where(m.DefinitionHistory.kind == 'policy',
                m.DefinitionHistory.object_id == proposed['id'], m.DefinitionHistory.object_revision == retained['revision']))
            assert decision.snapshot['status'] == 'retired'
            assert decision.reason == 'Keep the independently approved current rule'
    finally:
        context.close()
