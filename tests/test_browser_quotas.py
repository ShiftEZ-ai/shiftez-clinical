"""Real browser quota proposal/review/publish/report. No API route mocking."""
import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, navigate, save_dialog, decide, wait_held

pytestmark = pytest.mark.browser


@pytest.mark.parametrize('mode', ['fixed', 'rolling', 'prorated'])
def test_quota_proposal_activation_publication_and_personal_report(browser, live_server, h, mode):
    # Rows are stored end-exclusive; the report shows the included final day.
    periods = ([f"{day} Oct 2026 – {day + 27} Oct 2026" for day in range(1, 5)] if mode == "rolling"
               else ["4 Oct 2026 – 10 Oct 2026" if mode == "prorated" else "1 Oct 2026 – 31 Oct 2026"])

    def assert_periods(report, person):
        expect(report.locator('th[data-col="block"]')).to_contain_text("Period")
        cells = report.locator("tr[data-row]").filter(has_text=person).locator('td[data-col="block"]')
        expect(cells).to_have_count(len(periods))
        assert sorted(cells.all_text_contents()) == sorted(periods)
        # Every date is one intact unit; wrapping may occur between the two dates.
        expect(cells.locator(".date-units > span")).to_have_count(2 * len(periods))

    w =h.shift(h.workspace("Quota browser journey"))["workspace"]
    contexts = []; errors = []
    try:
        context, page = sign_in(browser, live_server, "coordinator"); contexts.append(context)
        page.on("pageerror", lambda error: errors.append(str(error)))
        navigate(page, "rules"); page.get_by_role("button", name="Add rule", exact=True).click()
        form = page.get_by_role("dialog")
        form.get_by_label("Rule title", exact=True).fill("Browser target")
        form.get_by_label("Policy owner", exact=True).fill("Fictional owner")
        form.get_by_label("Source or departmental decision", exact=True).fill("Constructed browser test, not institutional policy")
        form.get_by_label("Effective from", exact=True).fill("2026-10-01")
        form.get_by_role('radio', name='Quota or commitment', exact=True).check()
        period = form.locator(".quota-period")
        period.get_by_label("From", exact=True).fill("2026-10-01")
        period.get_by_label("Until (included)", exact=True).fill("2026-10-31")
        if mode == 'rolling':
            period.get_by_label("Repeat", exact=True).select_option("rolling")
            period.get_by_label("Days in each block", exact=True).fill("28")
        # Only a target (no minimum or maximum): an advisory target rule.
        form.locator(".quota-limits").get_by_label("Target", exact=True).fill("2")
        if mode == "prorated":
            period.get_by_label("Scale the target to each person’s days in the department", exact=True).check()
            period.get_by_label("Target is for this many days", exact=True).fill("28")
            expect(period.get_by_label("Repeat", exact=True)).to_be_disabled()
        expect(form.get_by_label("Classification", exact=True)).to_have_value("advisory")
        expect(form.get_by_label("Classification", exact=True)).to_be_disabled()
        # A pending rule check/save is not a closed dialog yet; wait for this command before checking its UI outcome.
        with page.expect_response(lambda response: response.url.endswith('/api/commands')
                and response.request.method == 'POST'
                and response.request.post_data_json.get('kind') == 'policy.create') as proposed:
            form.get_by_role('button', name='Propose rule', exact=True).click()
        assert proposed.value.ok, proposed.value.text()
        assert proposed.value.json()['record']['status'] == 'proposed'
        expect(form).not_to_be_visible(timeout=10000)
        context, reviewer = sign_in(browser, live_server, "approver"); contexts.append(context)
        navigate(reviewer, "rules"); reviewer.get_by_role("button", name="Review", exact=True).click()
        form = reviewer.get_by_role("dialog"); form.get_by_label("Reason", exact=True).fill("Independent quota review")
        save_dialog(form, "Activate")
        # The draft froze its inputs when it was created; an advisory target (not a safety rule) reaches it once its
        # baseline is refreshed, as a coordinator does from the draft's "baseline change · review".
        h.refresh_draft_baseline(h.detail(w)["workspace"])
        # Load the other browser's activation before testing a check arriving while the rules sheet has focus.
        page.reload()
        navigate(page, "rules")
        # The automatic check finishes only after the targets dock is open (CI's order): the targets still arrive.
        held = []
        if mode == "prorated":
            page.route("**/api/commands", lambda route: held.append(route) if "Automatic rota check" in (route.request.post_data or "") else route.continue_())
        navigate(page, "planner")
        if mode == "prorated":
            page.get_by_role("button", name="Rota tools").click()  # workload targets have their own dock
            page.get_by_role("menuitem", name="Workload targets for this rota").click()
            expect(page.get_by_role("region", name="Workload targets for this rota")).to_be_visible()
            wait_held(page, held)
            assert len(held) == 1
            held.pop().continue_()
            # The planner checks the changed draft automatically; targets become adjustable once it has.
            expect(page.get_by_role("button", name="Adjust workload target", exact=True)).to_be_visible(timeout=20000)
            page.get_by_role("button", name="Adjust workload target", exact=True).click()
            adjustment = page.get_by_role("dialog", name="Adjust workload target")
            adjustment.get_by_label("Override target", exact=True).fill("3")
            adjustment.get_by_label("Adjustment reason", exact=True).fill("Synthetic rota-specific target")
            save_dialog(adjustment, "Save target adjustment")
            expect(page.get_by_role("heading", name="Saved target adjustments", exact=True)).to_be_visible()
            page.get_by_role("button", name="Remove adjustment", exact=True).click()
            removal = page.get_by_role("dialog", name="Remove target adjustment")
            removal.get_by_label("Reason", exact=True).fill("Reset synthetic adjustment")
            save_dialog(removal, "Remove adjustment")
            expect(page.get_by_role("heading", name="Saved target adjustments", exact=True)).to_have_count(0)
            page.get_by_role("button", name="Adjust workload target", exact=True).click()
            adjustment = page.get_by_role("dialog", name="Adjust workload target")
            adjustment.get_by_label("Override target", exact=True).fill("3")
            adjustment.get_by_label("Adjustment reason", exact=True).fill("Synthetic replacement target")
            save_dialog(adjustment, "Save target adjustment")
            sheet = page.get_by_role("region", name="Workload targets for this rota")
            expect(sheet.get_by_role("heading", name="Saved target adjustments", exact=True)).to_be_visible()
            sheet.get_by_role("button", name="Close panel").click()
            expect(sheet).to_have_count(0)
        decide(page, "Submit for review")
        navigate(reviewer, "planner")
        if mode == 'prorated':
            reviewer.get_by_role('button', name='Approve rota', exact=True).click()
            approval = reviewer.get_by_role('dialog', name='Approve rota', exact=True)
            items = approval.get_by_role('region', name='Changes for this rota')
            expect(items.locator('[data-review-item]')).to_have_count(1)
            expect(approval.locator('button[type=submit]')).to_be_disabled()
            items.get_by_role('button', name='Accept', exact=True).click()
            approval.get_by_label('Note', exact=True).fill('Independently reviewed the prorated target adjustment')
            save_dialog(approval, 'Approve rota')
        else:
            decide(reviewer, "Approve rota")
        page.reload(); navigate(page, "planner"); decide(page, "Publish rota")
        navigate(page,"insights/workload")
        panel = page.get_by_role("heading", name="Targets and limits", exact=True).locator("..")
        expect(panel.get_by_role("cell", name="Jordan Lee", exact=True)).to_have_count(4 if mode == 'rolling' else 1)
        assert_periods(panel, "Jordan Lee")
        # One collection table: search by person or rule, and a Result filter in the shared Filter popover.
        panel.get_by_label("Search commitments", exact=True).fill("no such person")
        expect(panel.get_by_role("heading", name="No matching commitments", exact=True)).to_be_visible()
        panel.get_by_label("Search commitments", exact=True).fill("Jordan")
        expect(panel.get_by_role("cell", name="Jordan Lee", exact=True)).to_have_count(4 if mode == 'rolling' else 1)
        panel.get_by_label("Search commitments", exact=True).fill("")
        panel.get_by_role("button", name="Filter", exact=True).click()
        expect(page.locator(".dt-pop").get_by_label("Filter on", exact=True).locator("option", has_text="Result")).to_have_count(1)
        page.keyboard.press("Escape")
        context, staff = sign_in(browser, live_server, "staff"); contexts.append(context)
        navigate(staff,"insights/workload")
        personal = staff.get_by_role("heading", name="Targets and limits", exact=True).locator("..")
        expect(personal.get_by_role("cell", name="Alex Morgan", exact=True)).to_have_count(4 if mode == 'rolling' else 1)
        expect(personal.get_by_role("cell", name="Jordan Lee", exact=True)).to_have_count(0)
        assert_periods(personal, "Alex Morgan")
        if mode == "prorated":
            expect(personal.get_by_role("cell", name="2 per 28 days × 31 departmental days = 2 → adjusted to 3", exact=True)).to_be_visible()
        # Downloads moved to Insights › Exports.
        # I-20: staff get only their own calendar; the department's Commitments CSV is the planners'.
        navigate(staff,"insights/exports")
        expect(staff.get_by_role("button", name="Download Personal calendar", exact=True)).to_be_visible()
        expect(staff.get_by_role("button", name="Download Commitments CSV", exact=True)).to_have_count(0)
        navigate(page,"insights/exports")
        expect(page.get_by_role("button", name="Download Commitments CSV", exact=True)).to_be_visible()
        assert not errors and h.detail(w)["workspace"]["status"] == "published"
    finally:
        for context in contexts: context.close()
