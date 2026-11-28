"""Draft Snapshot UI coverage: choose a dependency-closed scope, reload it, and review refresh."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, new_draft, save_dialog, sign_in  # noqa: F401
from .test_publish_revised_mandatory import revise

pytestmark = pytest.mark.browser


def test_custom_draft_baseline_survives_reload_and_opens_refresh_review(browser, live_server, h):
    context, page = sign_in(browser, live_server, "coordinator", viewport={"width": 1440, "height": 900}, color_scheme="light")
    try:
        # D-075: another draft is open first; after Create draft the address names the new draft, not this one.
        shown = h.workspace("Draft open before New rota")
        navigate(page, f"schedule/week/{shown['starts_on']}?edit={shown['id']}")
        expect(page).to_have_url(re.compile(r"edit=" + shown["id"]))
        before = {row["id"] for row in h.get("coordinator", "/api/workspaces?limit=100")["workspaces"]}
        new_draft(page)
        dialog = page.get_by_role("dialog", name="New rota", exact=True)
        # Department baseline by default: the custom scope waits, unticked, under a quiet Advanced after the routine fields.
        expect(dialog.get_by_role("checkbox", name="Score and fairness", exact=True)).to_be_hidden()
        dialog.get_by_text("Advanced", exact=True).click()
        custom = dialog.get_by_role("checkbox", name="Custom scope", exact=True)
        expect(custom).not_to_be_checked()
        custom.check()
        expect(dialog.get_by_role("group", name="Baseline groups")).to_be_visible()
        dialog.get_by_role("checkbox", name="Score and fairness", exact=True).check()
        expect(dialog.get_by_text("1 group, plus what it depends on", exact=True)).to_be_visible()
        save_dialog(dialog, "Create draft")

        created = next(row for row in h.get("coordinator", "/api/workspaces?limit=100")["workspaces"] if row["id"] not in before)
        detail = h.get("coordinator", f"/api/workspaces/{created['id']}")
        baseline = detail["workspace"]["baseline"]
        assert baseline["scope"] == "custom"
        assert {"score", "rules", "people", "receipts"} <= set(baseline["included_sources"])
        snapshot_hash = baseline["snapshot_hash"]
        expect(page.locator(".rota-switcher")).to_have_accessible_name(re.compile(re.escape(created["name"])))
        expect(page).to_have_url(re.compile(r"[?&]edit=" + created["id"] + r"(&|$)"))

        page.reload()
        navigate(page, f"planner/{created['id']}")
        page.get_by_role("button", name="Rota tools", exact=True).click()
        page.get_by_role("menuitem", name="Refresh draft baseline", exact=True).click()
        refresh = page.get_by_role("dialog", name="Refresh draft baseline", exact=True)
        expect(refresh).to_be_visible()
        expect(refresh).not_to_contain_text(snapshot_hash)  # never the raw hash
        rota = h.get("coordinator", f"/api/workspaces/{created['id']}")["workspace"]
        preview = h.command("coordinator", "workspace.baseline.refresh.preview", {}, rota["id"], rota["revision"])
        apply = refresh.get_by_role("button", name="Apply refresh", exact=True)
        if all(c["status"] == "draft" for c in preview["changes"]):  # no department change: nothing to apply
            expect(refresh).to_contain_text("No department changes since this draft was created")
            expect(apply).to_have_count(0)
        else:
            expect(refresh.get_by_role("table")).to_be_visible()
            expect(refresh.get_by_role("columnheader", name="Decision")).to_have_count(1 if preview["conflicts"] else 0)
            expect(refresh.get_by_label("Reason", exact=True)).to_have_value("")
            undecided = [c for c in preview["changes"] if c["status"] == "conflict" and not c["safety"]]
            if undecided:  # Apply waits, saying why, until each conflict is decided
                expect(apply).to_be_disabled()
                expect(refresh.get_by_role("status")).to_have_text("Choose an outcome for each conflict")
        for words in ("frozen snapshot", "The preview is read-only", "No source groups", "source group"):
            expect(refresh).not_to_contain_text(words)
    finally:
        context.close()


def test_submit_refusal_offers_refresh_for_a_revised_department_limit(browser, live_server, h):
    policy = h.policy({'kind': 'max_duties', 'maximum': 2, 'window': 'week'})
    draft = h.shift(h.workspace('Review after a rule change'))['workspace']
    draft = h.shift(draft, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00')['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        navigate(page, f"planner/{draft['id']}")
        page.get_by_role('button', name='Submit for review…', exact=True).click()
        dialog = page.get_by_role('dialog', name=f"Submit {draft['name']} for review", exact=True)
        submit = dialog.get_by_role('button', name='Submit for review', exact=True)
        expect(submit).to_be_enabled()

        revise(h, policy, 1, field='maximum')
        submit.click()
        expect(dialog).to_contain_text('A department rule changed')
        dialog.get_by_role('button', name='Refresh', exact=True).click()
        expect(page.get_by_role('dialog', name='Refresh draft baseline', exact=True)).to_be_visible()
    finally:
        context.close()


def test_earlier_draft_for_checks_can_be_saved_and_cleared(browser, live_server, h):
    earlier = h.workspace('Earlier planning rota', '2026-10-04', '2026-10-11')
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        page.clock.set_fixed_time('2026-10-03T09:00:00+03:00')
        navigate(page, f"planner/{earlier['id']}")
        new_draft(page)
        dialog = page.get_by_role('dialog', name='New rota', exact=True)
        fields = dialog.get_by_label('Earlier draft for checks', exact=True)
        expect(fields.locator(f"option[value='{earlier['id']}']")).to_have_count(1)
        fields.select_option(earlier['id'])
        save_dialog(dialog, 'Create draft')
        later = next(row for row in h.get('coordinator', '/api/workspaces')['workspaces']
                     if row['context_draft_id'] == earlier['id'])

        page.reload()
        navigate(page, f"planner/{later['id']}")
        page.get_by_role('button', name='Rota tools', exact=True).click()
        page.get_by_role('menuitem', name='Earlier draft for checks…', exact=True).click()
        edit = page.get_by_role('dialog', name='Earlier draft for checks', exact=True)
        field = edit.get_by_label('Earlier draft for checks', exact=True)
        expect(field).to_have_value(earlier['id'])
        field.select_option('')
        save_dialog(edit, 'Save')
        assert h.detail(later)['workspace']['context_draft_id'] is None
    finally:
        context.close()
