"""Schedule adapts to breakpoint changes without losing edits or the phone layout choice."""
import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, planner_tool  # noqa: F401
from .test_browser_rules_in_planner import rota, open_planner
from .test_browser_phone_panels import assert_sheet
from .rules_support import rule_action, rule_row

pytestmark = pytest.mark.browser
PHONE = {"width": 390, "height": 844}
DESKTOP = {"width": 1440, "height": 1000}



@pytest.mark.parametrize("role", ["coordinator", "approver"])
@pytest.mark.parametrize("view", ["week", "month", "day"])
def test_schedule_reflows_when_crossing_phone_breakpoint(browser, live_server, h, role, view):
    w = h.shift(h.workspace("Responsive schedule"))["workspace"]
    h.lifecycle(w, "validate")
    context, page = sign_in(browser, live_server, role, viewport=DESKTOP)
    try:
        navigate(page, f"schedule/{view}/2026-10-04?edit={w['id']}")
        region = page.get_by_role("region", name=f"{view.title()} view", exact=True)
        desktop = region.locator(".sd-day" if view == "day" else ".wk-grid")
        expect(desktop).to_be_visible()
        before = page.url
        page.set_viewport_size(PHONE)
        expect(region.locator(".ag")).to_be_visible()
        expect(page.get_by_role("radio", name="Agenda", exact=True)).to_have_attribute("aria-checked", "true")
        expect(desktop).to_have_count(0)
        page.set_viewport_size(DESKTOP)
        expect(desktop).to_be_visible()
        expect(region.locator(".ag")).to_have_count(0)
        assert page.url == before
        if role == "approver":
            expect(page.get_by_role("button", name="Add session", exact=True)).to_have_count(0)
    finally:
        context.close()


def test_schedule_preserves_explicit_phone_layout_across_breakpoints(browser, live_server, h):
    w = h.shift(h.workspace("Chosen phone layout"))["workspace"]
    h.lifecycle(w, "validate")
    context, page = sign_in(browser, live_server, "coordinator", viewport=PHONE)
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        page.get_by_role("radio", name="Grid", exact=True).click()
        expect(page.locator(".wk-grid")).to_be_visible()
        page.set_viewport_size(DESKTOP)
        expect(page.get_by_role("radiogroup", name="Calendar layout")).to_have_count(0)
        page.set_viewport_size(PHONE)
        expect(page.get_by_role("radio", name="Grid", exact=True)).to_have_attribute("aria-checked", "true")
        expect(page.locator(".wk-grid")).to_be_visible()
        page.get_by_role("radio", name="Agenda", exact=True).click()
        page.set_viewport_size(DESKTOP)
        expect(page.locator(".wk-grid")).to_be_visible()
        page.set_viewport_size(PHONE)
        expect(page.get_by_role("radio", name="Agenda", exact=True)).to_have_attribute("aria-checked", "true")
        expect(page.locator(".ag")).to_be_visible()
    finally:
        context.close()


def test_schedule_resize_preserves_dirty_session_form_and_cached_rota(browser, live_server, h):
    w = h.shift(h.workspace("Resize with unsaved session"))["workspace"]
    h.lifecycle(w, "validate")
    context, page = sign_in(browser, live_server, "coordinator", viewport=DESKTOP)
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        # Any rota check the page starts has landed (its result is read again) before the rota reads are cut below.
        expect(page.locator(".health-pill.checking")).to_have_count(0, timeout=20000)
        page.get_by_role("button", name="Add session", exact=True).click()
        form = page.get_by_role("region", name="Add session", exact=True)
        field = form.get_by_role("spinbutton", name="Session start time", exact=True)
        field.fill("10:00")
        # Resizing an open form must neither replace it nor ask for the whole rota again.
        page.route("**/api/workspaces*", lambda route: route.abort())
        page.set_viewport_size(PHONE)
        expect(page.locator("#schedule-view .ag")).to_have_count(1)
        expect(form).to_be_visible()
        expect(field).to_have_value("10:00")
        page.set_viewport_size(DESKTOP)
        expect(page.locator("#schedule-view .wk-grid")).to_have_count(1)
        expect(form).to_be_visible()
        expect(field).to_have_value("10:00")
        form.get_by_role("button", name="Close panel", exact=True).click()
        expect(form.get_by_role("button", name="Keep editing", exact=True)).to_be_visible()
        form.get_by_role("button", name="Keep editing", exact=True).click()
        expect(field).to_have_value("10:00")
        expect(page.locator("#main [role=alert]")).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize("starts_on_phone", [False, True], ids=["desktop-first", "phone-first"])
@pytest.mark.parametrize("action", ["rota", "exception"])
def test_schedule_resize_preserves_pinned_rule_forms(browser, live_server, h, action, starts_on_phone):
    w, rules = rota(h)
    context, page = sign_in(browser, live_server, "coordinator", viewport=DESKTOP)
    try:
        open_planner(page, w)
        page.get_by_role("button", name="Rules for Continuity clinic", exact=True).click()
        panel = page.get_by_role("dialog", name="Continuity clinic", exact=True)
        label = "Adjust for this rota" if action == "rota" else "Make an exception for this clinic"
        rule_action(rule_row(panel, "Weekly duty limit"), label)
        form = panel.get_by_role("form", name=label, exact=True)
        if starts_on_phone:
            page.set_viewport_size(PHONE)
            expect(page.locator("#schedule-view .ag")).to_have_count(1)
        number = form.get_by_role("spinbutton")
        number.fill("6")
        reason = "Keep this clinic exception through resizing"
        if action == "exception":
            form.get_by_role("textbox", name="Reason", exact=True).fill(reason)

        for viewport in ([DESKTOP, PHONE] if starts_on_phone else [PHONE, DESKTOP]):
            page.set_viewport_size(viewport)
            renderer = ".ag" if viewport == PHONE else ".wk-grid"
            expect(page.locator("#schedule-view " + renderer)).to_have_count(1)
            expect(form).to_be_visible()
            if viewport == PHONE:
                assert_sheet(page, panel)
            expect(number).to_have_value("6")
            if action == "exception":
                expect(form.get_by_role("textbox", name="Reason", exact=True)).to_have_value(reason)

        # The original dirty guard and form actions must survive the remount too.
        panel.get_by_role("button", name="Close rules panel (Esc)", exact=True).click()
        expect(panel.get_by_role("button", name="Keep editing", exact=True)).to_be_visible()
        panel.get_by_role("button", name="Keep editing", exact=True).click()
        expect(number).to_have_value("6")
        if action == "rota":
            form.get_by_label('Justification', exact=True).fill('Agreed with the department for this rota')
            form.get_by_role("button", name="Use for this rota", exact=True).click()
            expect(page.locator(".toast").filter(has_text="changed for this rota")).to_be_visible()
            adjustment = next(x for x in h.detail(w)["workspace"]["rule_adjustments"] if x["policy_id"] == rules["duties"]["id"])
            assert adjustment["amount"] == 6
            expect(panel.get_by_role("button", name="Back to the department rule", exact=True)).to_be_visible()
        else:
            form.get_by_role("button", name="Send for approval", exact=True).click()
            expect(page.locator(".toast").filter(has_text="Exception proposed")).to_be_visible()
            proposed = [p for p in h.get("coordinator", "/api/policies?status=proposed")["policies"]
                        if rules["duties"]["id"] in p["definition"]["overrides"]]
            assert len(proposed) == 1
            assert proposed[0]["definition"]["rule"]["maximum"] == 6
            assert proposed[0]["source"] == reason
    finally:
        context.close()


@pytest.mark.parametrize('viewport', [PHONE, DESKTOP], ids=['phone', 'desktop'])
@pytest.mark.parametrize('replacement', ['auto', 'rules', 'help'])
def test_panel_replacement_keeps_or_discards_the_rule_form(browser, live_server, h, viewport, replacement):
    w, _ = rota(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        open_planner(page, w)
        page.get_by_role('button', name='Rules for Continuity clinic', exact=True).click()
        panel = page.get_by_role('dialog', name='Continuity clinic', exact=True)
        rule_action(rule_row(panel, 'Weekly duty limit'), 'Adjust for this rota')
        form = panel.get_by_role('form', name='Adjust for this rota', exact=True)
        number = form.get_by_role('spinbutton')
        number.fill('6')
        if viewport == PHONE:
            page.set_viewport_size(PHONE)
            expect(page.locator('#schedule-view .ag')).to_be_visible()
            assert_sheet(page, panel)
        before = h.detail(w)['workspace']['rule_adjustments']
        auto = page.get_by_role('button', name='Auto-schedule', exact=True)
        help_button = page.get_by_role('button', name='How to read this schedule', exact=True)
        next_panel = (page.locator('#auto-panel') if replacement == 'auto' else
                      page.get_by_role('dialog', name='Rules in this rota', exact=True) if replacement == 'rules' else
                      page.locator('.wk-panel').filter(has=page.get_by_role('heading', name='How to read this schedule', exact=True)))

        def replace_panel():
            if replacement == 'rules':
                planner_tool(page, 'Rules in this rota')
            else:
                (auto if replacement == 'auto' else help_button).click()

        replace_panel()
        expect(panel.get_by_role('alert')).to_contain_text('Not saved.')
        opposite = DESKTOP if viewport == PHONE else PHONE
        page.set_viewport_size(opposite)
        expect(page.locator('#schedule-view ' + ('.ag' if opposite == PHONE else '.wk-grid'))).to_be_visible()
        expect(panel.get_by_role('alert')).to_contain_text('Not saved.')
        expect(next_panel).not_to_be_visible()
        expect(auto).to_have_attribute('aria-expanded', 'false')
        expect(help_button).to_have_attribute('aria-pressed', 'false')
        panel.get_by_role('button', name='Keep editing', exact=True).click()
        expect(number).to_have_value('6')
        # Keeping edits cancels the earlier replacement; an ordinary Close still asks to close only Rules.
        panel.get_by_role('button', name='Close rules panel (Esc)', exact=True).click()
        panel.get_by_role('button', name='Keep editing', exact=True).click()
        expect(next_panel).not_to_be_visible()
        expect(number).to_have_value('6')
        replace_panel()
        page.set_viewport_size(viewport)
        expect(page.locator('#schedule-view ' + ('.ag' if viewport == PHONE else '.wk-grid'))).to_be_visible()
        panel.get_by_role('button', name='Discard changes', exact=True).click()
        expect(panel).to_have_count(0)
        expect(page.locator('.side-panel:visible')).to_have_count(1)
        expect(next_panel).to_be_visible()
        expect(auto).to_have_attribute('aria-expanded', str(replacement == 'auto').lower())
        expect(help_button).to_have_attribute('aria-pressed', str(replacement == 'help').lower())
        if viewport == PHONE and replacement != 'help':
            assert_sheet(page, next_panel)
        assert h.detail(w)['workspace']['rule_adjustments'] == before
    finally:
        context.close()



def test_session_replacement_returns_focus_to_its_original_chip(browser, live_server, h):
    w, _ = rota(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        open_planner(page, w)
        page.get_by_role('button', name='Rules for Continuity clinic', exact=True).click()
        rules = page.get_by_role('dialog', name='Continuity clinic', exact=True)
        rule_action(rule_row(rules, 'Weekly duty limit'), 'Adjust for this rota')
        rules.get_by_role('form', name='Adjust for this rota', exact=True).get_by_role('spinbutton').fill('6')
        chip = page.locator('button.pchip:not(.gap)').first
        label = chip.get_attribute('aria-label')
        chip.click()
        expect(rules.get_by_role('alert')).to_contain_text('Not saved.')
        expect(page.locator('.wk-panel')).to_have_count(0)
        rules.get_by_role('button', name='Discard changes', exact=True).click()
        expect(rules).to_have_count(0)
        session = page.locator('.wk-panel')
        expect(session).to_be_visible()
        expect(page.locator('.side-panel:visible')).to_have_count(1)
        # Moving within the session panel keeps the original schedule chip as its return target.
        session.get_by_role('button', name='Replace', exact=True).click()
        session.get_by_role('button', name='Close panel', exact=True).click()
        expect(session).to_have_count(0)
        expect(page.get_by_role('button', name=label, exact=True)).to_be_focused()
        assert h.detail(w)['workspace']['rule_adjustments'] == []
    finally:
        context.close()


@pytest.mark.parametrize('dirty', [False, True], ids=['clean', 'discard-first'])
def test_rule_replacement_reports_a_failed_async_form_read(browser, live_server, h, dirty):
    from .test_browser import planner_tool
    w, _ = rota(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        open_planner(page, w)
        planner_tool(page, 'Rules in this rota')
        rules = page.get_by_role('dialog', name='Rules in this rota', exact=True)
        if dirty:
            rule_action(rule_row(rules, 'Weekly duty limit'), 'Adjust for this rota')
            rules.get_by_role('form', name='Adjust for this rota', exact=True).get_by_role('spinbutton').fill('6')
        page.route('**/api/policies/capabilities', lambda route: route.fulfill(status=503,
            json={'error': {'code': 'SERVER_ERROR', 'message': 'Rule form temporarily unavailable'}}))
        rules.get_by_role('button', name='Add a rule for this rota', exact=True).click()
        if dirty:
            expect(rules.get_by_role('alert')).to_contain_text('Not saved.')
            rules.get_by_role('button', name='Discard changes', exact=True).click()
        expect(page.locator('.toast').filter(has_text='Rule form temporarily unavailable')).to_be_visible()
        assert not errors
        assert h.detail(w)['workspace']['rule_adjustments'] == []
    finally:
        context.close()
