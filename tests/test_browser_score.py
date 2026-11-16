"""Schedule score in the browser: the planner pill, its "Why this score" popover, and editing the scoring settings."""
import re

import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, navigate, open_config  # noqa: F401
from .test_browser_rule_catalogue_v2 import shot

pytestmark = pytest.mark.browser


def test_score_comparison_uses_displayed_values_for_capped_and_legacy_scores(browser, live_server):
    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(live_server)
        values = page.evaluate("""async () => {
            const {shownScore,scoreDelta}=await window.importBundle('score.js');
            const before={score:59,uncapped_score:71.9},after={score:59,uncapped_score:80.7};
            return [shownScore(after),scoreDelta(before,after),scoreDelta(after,before),
                scoreDelta(before,before),scoreDelta(null,after),scoreDelta({score:50},{score:60}),
                scoreDelta({score:null,uncapped_score:null},after)];
        }""")
        assert values == [80.7, 8.8, -8.8, 0, None, 10, None]
    finally:
        context.close()


@pytest.mark.parametrize('width,theme', [(1440, 'light'), (390, 'dark')])
def test_scoring_dials_save_reload_reset_and_refuse_empty_preference_blend(browser, live_server, h, width, theme):
    context, page = sign_in(browser, live_server, 'admin', viewport={'width': width, 'height': 900})
    page.emulate_media(color_scheme=theme)
    try:
        open_config(page, 'Auto-schedule & scoring')
        pane = page.locator('.config-pane')
        pane.get_by_label('Coverage weight', exact=True).fill('200')
        pane.get_by_role('tab', name='Staff preferences', exact=True).click()
        pane.get_by_label('Wish points per person (0 = wishes off)', exact=True).fill('2000000')
        pane.get_by_label('Catch-up for declined wishes (up to ×)', exact=True).fill('6.25')
        pane.get_by_role('tab', name='Advanced', exact=True).click()
        fields = {'Worst-off fairness emphasis (0–1)': '0.73', 'Desired-cover factor (0–1)': '0.37',
                  'Mean preference weight': '250', 'Worst preference weight': '750', 'Critical strength (×)': '500'}
        for label, value in fields.items():
            pane.get_by_label(label, exact=True).fill(value)
        mean = pane.get_by_label('Mean preference weight', exact=True)
        worst = pane.get_by_label('Worst preference weight', exact=True)
        mean.fill('0')
        worst.fill('0')
        pane.get_by_role('button', name='Save defaults', exact=True).click()
        expect(mean).to_be_focused()
        expect(page.get_by_role('dialog')).to_have_count(0)
        expect(mean).to_have_js_property('validationMessage', 'Give the mean or the worst preference a weight.')
        mean.fill('250')
        worst.fill('750')
        pane.get_by_role('button', name='Save defaults', exact=True).click()
        dialog = page.get_by_role('dialog', name='Save auto-schedule defaults', exact=True)
        dialog.get_by_label('Reason', exact=True).fill('Configure score measurements')
        dialog.get_by_role('button', name='Save defaults', exact=True).click()
        expect(page.get_by_role('button', name='Undo', exact=True)).to_be_visible()
        scoring = h.get('admin', '/api/me')['department']['generation_defaults']['scoring']
        assert scoring['fairness_worst_off_emphasis'] == 0.73 and scoring['desired_cover_factor'] == 0.37
        assert (scoring['preferences_mean_weight'], scoring['preferences_worst_weight']) == (250, 750)
        assert scoring['weights']['coverage'] == 200 and scoring['strengths']['critical'] == 500
        assert scoring['wish_budget_points'] == 2000000 and scoring['wish_debt_cap'] == 6.25
        page.reload()
        expect(pane.get_by_label('Coverage weight', exact=True)).to_have_value('200')
        pane.get_by_role('tab', name='Staff preferences', exact=True).click()
        expect(pane.get_by_label('Wish points per person (0 = wishes off)', exact=True)).to_have_value('2000000')
        expect(pane.get_by_label('Catch-up for declined wishes (up to ×)', exact=True)).to_have_value('6.25')
        pane.get_by_role('tab', name='Advanced', exact=True).click()
        for label, value in fields.items():
            expect(pane.get_by_label(label, exact=True)).to_have_value(value)
        shot(page, 'scoring-configured-advanced', width, theme)
        pane.get_by_role('button', name='Reset to recommended', exact=True).click()
        pane.get_by_role('button', name='Reset all? Reset to recommended', exact=True).click()
        for label, value in (('Worst-off fairness emphasis (0–1)', '0.5'), ('Desired-cover factor (0–1)', '0.25'),
                             ('Mean preference weight', '7'), ('Worst preference weight', '3')):
            expect(pane.get_by_label(label, exact=True)).to_have_value(value)
    finally:
        context.close()


def test_score_pill_explains_lost_points_and_moves_after_an_edit(browser, live_server, h):
    typ = h.configure('assignment_types', {'name': 'Pair clinic', 'starts_on': '2020-01-01',
                                           'definition': {'requirements': [{'minimum': 2}]}})['record']
    w = h.shift(h.workspace('Scored rota'), typ=typ['id'])['workspace']
    score = h.detail(w)['workspace']['validation']['score']
    # Half the seat-hours open; the gap is also a mandatory breach. The total also counts the fixed-audience workload.
    assert score['components']['coverage']['value'] == 0.5 and score['score'] < 60 and not score['publishable']
    shown = f"{score.get('uncapped_score', score['score']):g}"  # the pill shows the uncapped score, like the footer
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner')
        # Owner, 7 Nov 2026 ("I don't see the draft score"): while the gap must be fixed the footer still shows the score,
        # capped, beside the must-fix count.
        footer = page.locator('.app-footer')
        must = footer.get_by_role('button', name=re.compile(r'^1 must fix'))
        expect(must).to_be_visible()
        pill = footer.locator('.score-pill')
        expect(pill).to_contain_text(shown)
        # Owner: one count on screen, the must-fix pill's; the score says only that it is capped, and its name says why.
        expect(pill.locator('.score-blocking')).to_have_text('· capped')
        expect(pill).to_have_attribute('aria-label', f'Schedule score {shown} of 100, capped while anything must be fixed: 1 must fix')
        must.click()
        expect(page.locator('.popover.fix-pop')).not_to_contain_text('Score shows once')
        page.keyboard.press('Escape')
        # Filling the place lifts the score; the footer shows it, and its breakdown explains the points lost.
        shift = h.snapshot(w)['shifts'][0]
        payload = {key: shift[key] for key in ('assignment_type_id', 'location_id', 'service_ids', 'start', 'end', 'attributes')}
        payload.update(workspace_id=w['id'], shift_id=shift['id'], participants=[
            {'person_id': p, 'start': shift['start'], 'end': shift['end']} for p in h.seed['people'][:2]])
        h.command('coordinator', 'shift.update', payload, revision=h.detail(w)['workspace']['revision'])
        after = h.detail(w)['workspace']['validation']['score']
        assert after['publishable'] and after['score'] > score['score']
        page.reload()
        ready = footer.get_by_role('button', name=f"Ready to submit · score {after['score']:g}", exact=True)
        expect(ready).to_be_visible(timeout=20000)
        ready.click()
        page.locator('.popover.fix-pop').get_by_role('button', name='Score breakdown', exact=True).click()
        why = page.get_by_role('dialog', name='Why this score')
        expect(why.get_by_text(f"Schedule score {after['score']:g} / 100", exact=True)).to_be_visible()
        expect(why.get_by_role('meter', name='Coverage 100%')).to_be_visible()
        page.keyboard.press('Escape')
        expect(why).to_have_count(0)
    finally:
        context.close()


def test_admin_edits_scoring_settings_with_live_explanation_and_reset(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        open_config(page, 'Auto-schedule & scoring')
        pane = page.locator('.config-pane')
        save = pane.get_by_role('button', name='Save defaults', exact=True)
        expect(pane.get_by_role('region', name='Unsaved changes')).to_be_hidden()  # nothing changed yet
        # How it decides is the everyday tab (with the score weights); strengths and search tuning are under Advanced.
        for legend in ('Score weights', 'Workload', 'Existing assignments'):
            expect(pane.locator('legend', has_text=legend)).to_be_visible()
        expect(pane.locator('details')).to_have_count(0)
        expect(pane.get_by_label('First priority', exact=True)).to_have_count(0)  # one weighted score, no ordering
        explain = pane.locator('.score-explain')
        coverage = pane.get_by_label('Coverage weight', exact=True)
        expect(coverage).to_have_accessible_description('35%')  # its share, shown beside it
        expect(pane.locator('.weights-total')).to_be_hidden()  # a total of 100 needs no words
        coverage.fill('60')
        expect(coverage).to_have_accessible_description('48%')  # 60 of 125
        expect(pane.locator('.weights-total')).to_be_hidden()  # weights are relative: any total is fine, no warning (L10)
        pane.get_by_role('tab', name='Advanced', exact=True).click()
        for legend in ('Advisory rule strength', 'Search'):
            expect(pane.locator('legend', has_text=legend)).to_be_visible()
        expect(pane.locator('legend', has_text='Give-and-take')).to_have_count(0)
        expect(pane.locator('legend', has_text='Staff wishes')).to_be_hidden()
        pane.get_by_role('tab', name='Staff preferences', exact=True).click()  # the wish points and the kinds of wish
        for legend in ('Staff wishes', 'Kinds of wish'):
            expect(pane.locator('legend', has_text=legend)).to_be_visible()
        pane.get_by_role('tab', name='Advanced', exact=True).click()
        pane.get_by_label('Critical strength (×)', exact=True).fill('50')
        expect(explain).to_contain_text('counts 50× a low one')
        # How long a run searches is on Timing; how widely, under Advanced › Search.
        pane.get_by_role('tab', name='Timing', exact=True).click()
        # One unit for every time (L10): written and read as people read it.
        expect(pane.get_by_label('Stop after no improvement', exact=True)).to_have_value('10 s')
        pane.get_by_label('Stop after no improvement', exact=True).fill('30 s')
        pane.get_by_role('tab', name='Advanced', exact=True).click()
        expect(pane.get_by_label('People tried first for each place', exact=True)).to_have_value('3')
        pane.get_by_label('People tried first for each place', exact=True).fill('5')
        save.click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_label('Reason', exact=True).fill('Weigh coverage more')
        dialog.get_by_role('button', name='Save defaults', exact=True).click()
        undo = page.get_by_role('button', name='Undo', exact=True)
        expect(undo).to_be_visible()
        defaults = h.get('admin', '/api/me')['department']['generation_defaults']
        scoring = defaults['scoring']
        assert scoring['weights']['coverage'] == 60 and scoring['strengths']['critical'] == 50
        assert defaults['stop_without_improvement_seconds'] == 30 and defaults['shortlist_width'] == 5
        undo.click()
        expect(page.get_by_text('Undone', exact=False)).to_be_visible()
        assert h.get('admin', '/api/me')['department']['generation_defaults']['scoring']['weights']['coverage'] == 35
        open_config(page, 'Auto-schedule & scoring')
        pane.get_by_role('tab', name='How it decides', exact=True).click()  # the pane remembers the last tab shown
        expect(pane.get_by_label('Coverage weight', exact=True)).to_have_value('35')
        pane.get_by_label('Coverage weight', exact=True).fill('70')
        expect(pane.get_by_label('Coverage weight', exact=True)).to_have_accessible_description('52%')  # its share (70 of 135), not a hint per weight
        expect(pane.locator('.score-weights legend .score-help')).to_have_attribute('data-tip', re.compile('Work shared evenly'))  # what each part measures: the group's help
        pane.get_by_role('tab', name='Advanced', exact=True).click()
        pane.get_by_label('Critical strength (×)', exact=True).fill('40')
        reset = pane.get_by_role('button', name='Reset to recommended', exact=True)
        reset.click()  # same-button confirm: the first click only arms it
        expect(pane.get_by_label('Critical strength (×)', exact=True)).to_have_value('40')
        pane.get_by_role('button', name='Reset all? Reset to recommended', exact=True).click()
        expect(pane.get_by_label('Critical strength (×)', exact=True)).to_have_value('30')
        pane.get_by_role('tab', name='How it decides', exact=True).click()
        expect(pane.get_by_label('Coverage weight', exact=True)).to_have_value('35')
        # All weights zero is refused in the browser before it reaches the server.
        for label in ('Coverage', 'Rules & targets', 'Fairness', 'Preferences', 'Stability'):
            pane.get_by_label(label + ' weight', exact=True).fill('0')
        expect(pane.locator('.weights-total')).to_have_text('Give at least one part a weight.')  # said on the weights' total line
        save.click()
        expect(pane.get_by_label('Coverage weight', exact=True)).to_be_focused()
        expect(page.get_by_role('dialog')).to_have_count(0)
    finally:
        context.close()
