"""Score review (owner, 7 Nov 2026; docs/design/score-review.md): Configuration › Auto-schedule & scoring › Score review
lists every soft rule line of a draft with its tier, weight and points lost (the server's read), Must lines last and not
editable; a tier is changed through the governed rule proposal (another person approves; Undo withdraws); the score's
weights and tiers have a History with Restore; the footer's "Why this score" links to it. 1440 and 390, light and dark."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, open_rota, sign_in  # noqa: F401
from .test_browser_rule_catalogue import flag
from .test_browser_rule_catalogue_v2 import line, v2_rule

pytestmark = pytest.mark.browser
VIEWS = [pytest.param(1440, 'light', id='1440-light'), pytest.param(1440, 'dark', id='1440-dark'),
         pytest.param(390, 'light', id='390-light'), pytest.param(390, 'dark', id='390-dark')]


def _world(h):
    """An advisory weekly cap (medium) the draft breaks, and a Must rest rule it breaks too (so it has must-fix)."""
    flag(h, legacy=False)
    cap = v2_rule(h, 'workload_limit', [line({'max': {'value': 1, 'level': 'medium'}}, measure='duties', period={'kind': 'calendar_week'})])
    v2_rule(h, 'duty_spacing', [line({'min': {'value': 11, 'level': 'mandatory'}})])
    a = h.seed['people'][0]
    w = h.shift(h.workspace('Score review draft'), start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[a])['workspace']
    w = h.shift(w, start='2026-10-05T13:00:00+03:00', end='2026-10-05T17:00:00+03:00', people=[a])['workspace']
    w = h.command('coordinator', 'workspace.validate', {'reason': 'Routine rota check'}, w['id'], w['revision'])['workspace']
    review = h.get('coordinator', f"/api/workspaces/{w['id']}/score-review")
    soft = next(l for l in review['lines'] if l['rule_id'] == cap['id'])
    return w, review, soft


def _open(browser, origin, role, w, width, theme):
    context, page = sign_in(browser, origin, role, viewport={'width': width, 'height': 900 if width > 400 else 844})
    page.emulate_media(color_scheme=theme)
    navigate(page, f"configuration/auto_schedule?tab=score&draft={w['id']}")
    return context, page


@pytest.mark.parametrize('width,theme', VIEWS)
def test_every_soft_line_with_its_tier_weight_and_points_lost(browser, live_server, h, width, theme):
    w, review, soft = _world(h)
    context, page = _open(browser, live_server, 'admin', w, width, theme)
    try:
        expect(page.get_by_role('tab', name='Score review')).to_have_attribute('aria-selected', 'true')
        pane = page.locator('.score-review')
        # The score as the footer says it, and the parts that lost points (no zero parts).
        shown = review['score']['score']
        expect(pane.locator('.sr-score b')).to_have_text(str(int(shown)) if float(shown).is_integer() else str(shown))
        expect(pane.get_by_role('button', name='About the score')).to_have_attribute('data-tip', re.compile('fills required places first'))
        for part, c in review['score']['components'].items():
            if not c['lost']:
                expect(pane.locator('.sr-parts')).not_to_contain_text(part.capitalize())
        # The soft line: its tier is a choice, its points and reach the server's.
        tier = pane.get_by_role('combobox', name=re.compile(f"^Tier of {re.escape(soft['name'])}"))
        expect(tier).to_have_value(soft['level'])
        lost = round(soft['lost_points'], 1)
        said = str(int(lost)) if float(lost).is_integer() else str(lost)
        expect(pane.locator('.sr-lost b').filter(has_text=re.compile(f'^{re.escape(said)}$')).first).to_be_visible()
        expect(pane).to_contain_text(f"{soft['session_count']} session")
        # The Must line: listed, never a choice.
        expect(pane.get_by_text('Must', exact=True).first).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    finally:
        context.close()


def test_a_tier_change_is_a_governed_proposal_with_undo(browser, live_server, h):
    w, review, soft = _world(h)
    context, page = _open(browser, live_server, 'coordinator', w, 1440, 'light')
    try:
        # A coordinator's section is the Score review only: no department settings tabs.
        expect(page.get_by_role('tab', name='How it decides')).to_have_count(0)
        nav = page.get_by_role('navigation', name='Configuration sections')
        expect(nav.get_by_role('button', name='Auto-schedule & scoring', exact=True)).to_be_visible()
        tier = page.get_by_role('combobox', name=re.compile(f"^Tier of {re.escape(soft['name'])}"))
        tier.select_option('high')
        dialog = page.get_by_role('dialog', name=re.compile(f"^Propose High for {re.escape(soft['name'])}"))
        dialog.get_by_label('Reason', exact=True).fill('This cap matters more than other goals')
        dialog.get_by_role('button', name='Propose', exact=True).click()
        toast = page.locator('.toast').filter(has_text='Proposed')
        expect(toast).to_be_visible()
        expect(toast.get_by_role('button', name='Undo')).to_be_visible()
        # Nothing applies until another person approves: the line shows the proposal and is not editable meanwhile.
        pane = page.locator('.score-review')
        expect(pane.get_by_text('Proposed', exact=True).first).to_be_visible()
        expect(pane.get_by_role('combobox', name=re.compile(f"^Tier of {re.escape(soft['name'])}"))).to_have_count(0)
        entries = h.get('coordinator', '/api/rules/catalogue?version=2')['entries']
        entry = next(e for e in entries if (e.get('rule') or {}).get('id') == soft['rule_id'])
        assert entry['proposed'] and h.get('coordinator', f"/api/workspaces/{w['id']}/score-review")['lines']  # still in force
    finally:
        context.close()


def test_score_settings_history_restores_an_earlier_version(browser, live_server, h):
    w, _, _ = _world(h)
    current = h.get('admin', '/api/score-settings')
    h.command('admin', 'score.settings', {'definition': {**current['definition'], 'strengths': {'low': 1, 'medium': 4, 'high': 12, 'critical': 30}},
                                          'reason': 'Calibrate tiers'}, h.seed['department_id'], current['settings']['revision'])
    context, page = sign_in(browser, live_server, 'admin', viewport={'width': 1440, 'height': 900})
    try:
        navigate(page, 'configuration/auto_schedule')
        page.get_by_role('tab', name='How it decides').click()
        page.get_by_role('button', name='History', exact=True).click()
        dialog = page.get_by_role('dialog', name='Score settings history')
        versions = h.get('admin', '/api/score-settings?history=true')['versions']
        expect(dialog.get_by_role('row')).to_have_count(len(versions) + 1)  # header + every version, newest first
        expect(dialog.get_by_role('row').nth(1)).to_contain_text('tiers 1 / 4 / 12 / 30')
        expect(dialog.get_by_role('row').nth(1).get_by_role('button', name=re.compile('^Restore'))).to_have_count(0)  # current
        dialog.get_by_role('button', name=f"Restore version {current['settings']['revision']}").click()
        confirm = page.get_by_role('dialog', name=f"Restore version {current['settings']['revision']}")
        confirm.get_by_label('Reason', exact=True).fill('Back to the agreed tiers')
        confirm.get_by_role('button', name='Restore', exact=True).click()
        expect(page.locator('.toast').filter(has_text='Score settings restored.')).to_be_visible()
        assert h.get('admin', '/api/score-settings')['definition'] == current['definition']
    finally:
        context.close()


def test_why_this_score_opens_the_drafts_score_review(browser, live_server, h):
    w, _, _ = _world(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        open_rota(page, w['id'])
        page.locator('.app-footer .score-pill').first.click()
        page.get_by_role('button', name='Review rule scores', exact=True).click()
        expect(page).to_have_url(re.compile(rf"#configuration/auto_schedule\?.*draft={w['id']}"))
        expect(page.locator('.score-review .sr-tier').first).to_be_visible()
    finally:
        context.close()
