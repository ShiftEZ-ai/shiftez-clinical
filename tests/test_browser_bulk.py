"""Bulk actions: select several records, apply one change with one reason. Real Chromium and API."""
import re
import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, navigate, toolbar, close_popover, set_layout, filter_by, wait_held  # noqa: F401

pytestmark = pytest.mark.browser


def test_bulk_tag_people_and_switch_off_services(browser, live_server, h):
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "people")
        boxes = page.locator("td .bulk-check")
        boxes.nth(0).check()
        boxes.nth(1).check()
        bar = page.get_by_role("toolbar", name="Bulk actions")
        expect(bar).to_contain_text("2 people selected")
        # One bulk engine (P-07): Add tag opens Change… with Tags › Add chosen (Preview, Results, one Undo).
        bar.get_by_role("button", name="Add tag").click()
        change = page.get_by_role("region", name="Change 2 people")
        expect(change.get_by_role("combobox", name="How")).to_have_value("add")
        change.get_by_role("button", name="Tags: Choose…").click()
        page.get_by_label("Search Tags").fill("Night team")
        page.get_by_role("button", name="Add “Night team”").click()
        close_popover(page)
        change.get_by_label("Reason").fill("New night team")
        change.get_by_role("button", name="Apply 2 changes").click()
        expect(page.locator(".dock-panel.bulk-panel")).to_have_count(0)  # the light path: applied at once, with Undo
        expect(page.locator("tr.dt-changed")).to_have_count(2)
        tagged = [p for p in h.get("admin", "/api/catalogues")["people"] if "Night team" in (p.get("tags") or [])]
        assert len(tagged) == 2
    finally:
        context.close()
    context, page = sign_in(browser, live_server, "admin")
    try:
        navigate(page, "configuration")
        page.get_by_role("button", name=re.compile(r"^Services")).click()
        page.get_by_role("checkbox", name="Select all services shown").check()
        bar = page.get_by_role("toolbar", name="Bulk actions")
        bar.get_by_role("button", name="Set colour").click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_role("radio", name="Purple").check()
        dialog.get_by_label("Reason for this change").fill("Clinic colours")
        dialog.get_by_role("button", name=re.compile(r"^Apply to")).click()
        expect(page.get_by_text(re.compile(r"records? updated\."))).to_be_visible()
        assert all(s["colour"] == "violet" for s in h.get("admin", "/api/catalogues")["services"])
    finally:
        context.close()


def test_admin_removes_a_leavers_personal_details(browser, live_server, h):
    person = next(p for p in h.get("admin", "/api/catalogues")["people"] if p["id"] == h.seed["people"][1])
    value = {k: person[k] for k in ("display_name", "title", "directory_section", "staff_number", "work_phone") if k in person}  # legacy phone field is absent without contacts
    h.configure("people", {**value, "active": False}, person["id"], person["revision"])
    context, page = sign_in(browser, live_server, "admin")
    try:
        navigate(page, "people")
        # People lists active people by default; switched-off people are one filter away.
        filter_by(page, "Status", ["Inactive"])
        row = page.locator(".dt-table tbody tr", has_text=person["display_name"])
        row.locator(".bulk-check").check()
        bar = page.get_by_role("toolbar", name="Bulk actions")
        bar.get_by_role("button", name="Remove personal details").click()
        dialog = page.get_by_role("dialog")
        expect(dialog).to_contain_text("Rotas, publications and the audit log keep what they recorded")
        dialog.get_by_label("Reason", exact=True).fill("Left the department (test)")
        dialog.get_by_role("button", name="Remove details").click()
        # Destructive bulk changes go through Preview: Apply there is the armed same-button confirm.
        panel = page.locator(".dock-panel.bulk-panel")
        apply = panel.locator(".bulk-foot .button.primary")
        # Click only once the preview has settled on its one ready row: an earlier click can land on a button the
        # preview is about to redraw, and the arming is lost with it (the CI flake).
        expect(panel.locator(".bulk-sum")).to_contain_text("1 ready")
        expect(apply).to_have_accessible_name(re.compile(r"^Apply ready rows \(1\)"))
        expect(apply).to_be_enabled()
        apply.click()
        expect(apply).to_have_accessible_name(re.compile(r"^Confirm remove details of 1\?"))
        apply.click()
        expect(page.get_by_text(re.compile(r"Personal details removed"))).to_be_visible()
        # The list redraws with the new name (scoped to the list: the results panel may name the record too).
        expect(page.locator("#main .dt-table tbody tr", has_text="Former staff member")).to_have_count(1)
        after = next(p for p in h.get("admin", "/api/catalogues")["people"] if p["id"] == person["id"])
        assert after["display_name"].startswith("Former staff member") and after["staff_number"] == ""
        # Only for switched-off people, and only admins see it: picking an active person (the list shows Active and
        # Inactive) takes the action away.
        active = next(p for p in h.get("admin", "/api/catalogues")["people"] if p["id"] == h.seed["people"][0])
        assert active["active"] is not False
        page.locator("#main .dt-table tbody tr", has_text=active["display_name"]).locator(".bulk-check").check()
        expect(bar.get_by_role("button", name="Remove personal details")).to_have_count(0)
    finally:
        context.close()


def test_list_layouts_properties_and_saved_views(browser, live_server, h):
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "people")
        set_layout(page, "Cards")
        expect(page.locator(".dt-card").first).to_be_visible()
        toolbar(page, "View settings").locator(".dt-pop-item", has_text="Skills").locator("input").uncheck()
        close_popover(page)
        toolbar(page, "Saved views").get_by_role("menuitem", name="New view…").click()
        dialog = page.get_by_role("dialog", name="New view")
        dialog.get_by_label("View name").fill("Staff cards")
        dialog.get_by_role("button", name="Save view").click()
        expect(page.get_by_role("button", name="Saved views: Staff cards", exact=True)).to_be_visible()
        saved = h.get("coordinator", "/api/table-views?table=people")["views"]
        assert saved[0]["definition"]["layout"] == "cards" and "skills" in saved[0]["definition"]["hidden"]
        page.reload()
        navigate(page, "people")
        expect(page.get_by_role("button", name="Saved views: Staff cards", exact=True)).to_be_visible()
        expect(page.locator(".dt-card").first).to_be_visible()
        views = toolbar(page, "Saved views")
        views.get_by_role("menuitem", name="Delete “Staff cards”").click()
        expect(page.get_by_role("button", name="Saved views", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="View settings: Cards", exact=True)).to_be_visible()
        assert h.get("coordinator", "/api/table-views?table=people")["views"] == []
    finally:
        context.close()


def test_inline_edit_one_cell_then_many_selected_rows(browser, live_server, h):
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "people")
        rows = page.locator(".dt-table tbody tr")
        column = page.locator(".dt-table thead th").all_inner_texts().index("Tags")
        # One cell: type a new tag and add it.
        cell = rows.nth(0).locator("td").nth(column)
        cell.click()
        cell.get_by_label("Search Tags").fill("Night team")
        cell.get_by_role("button", name="Add “Night team”").click()
        # The confirmation (with Undo) comes before the page refresh; wait for the refresh's new table before the next
        # edit, or the redraw closes an editor opened in between.
        table = page.locator("[data-table]").first
        table.evaluate("n => n.dataset.beforeSave = '1'")
        cell.get_by_role("button", name="Save", exact=True).click()
        expect(page.get_by_text("Tags saved.")).to_be_visible()
        expect(page.locator("[data-table][data-before-save]")).to_have_count(0)
        expect(page.locator("[data-table]").first).to_be_visible()
        # Several selected rows: editing one cell applies to all of them in one audited change.
        for i in range(3):
            rows.nth(i).locator("input.bulk-check").check()
        cell = rows.nth(1).locator("td").nth(column)
        cell.click()
        expect(cell.get_by_text("All 3 selected")).to_be_visible()
        cell.get_by_label("Search Tags").fill("Weekend cover")
        cell.get_by_role("button", name="Add “Weekend cover”").click()
        cell.get_by_role("button", name="Save", exact=True).click()
        expect(page.get_by_text("Tags updated for 3 people.")).to_be_visible()
        tagged = [p for p in h.get("coordinator", "/api/catalogues")["people"] if "Weekend cover" in (p.get("tags") or [])]
        assert len(tagged) == 3
    finally:
        context.close()


@pytest.mark.parametrize('entry', ['cell', 'bulk'])
def test_inline_field_confirmation_keeps_undo_before_refresh(browser, live_server, h, entry):
    context, page = sign_in(browser, live_server, 'coordinator')
    held = []
    try:
        navigate(page, 'people')
        rows = page.locator('.dt-table tbody tr[data-row]')
        ids = [rows.nth(i).get_attribute('data-row') for i in range(2 if entry == 'bulk' else 1)]
        before = {p['id']: p.get('tags') or [] for p in h.get('coordinator', '/api/catalogues')['people'] if p['id'] in ids}
        if entry == 'bulk':
            for i in range(2):
                rows.nth(i).locator('input.bulk-check').check()
            # People is in the bulk registry: the universal Change\u2026 (Tags \u203a Add), applied at once (clean and small).
            page.get_by_role('toolbar', name='Bulk actions').get_by_role('button', name='Change\u2026', exact=True).click()
            editor = page.locator('.dock-panel.bulk-panel')
            row = editor.locator('.bulk-changes .cond-row').last
            row.get_by_label('What to change', exact=True).select_option(label='Tags')
            row.get_by_label('How', exact=True).select_option(label='Add')
            row.locator('.pick-button').click()
            message, action = '2 people changed', 'Apply 2 changes'
        else:
            editor = rows.first.locator('td[data-col=tags]')
            editor.click()
            message, action = 'Tags saved.', 'Save'
        editor.get_by_label('Search Tags').fill('Controlled refresh')
        editor.get_by_role('button', name='Add \u201cControlled refresh\u201d', exact=True).click()
        editor.get_by_label('Search Tags').press('Escape')
        expect(editor.get_by_label('Search Tags')).not_to_be_visible()
        page.route('**/api/me', lambda route: held.append(route) if not held else route.continue_())
        editor.get_by_role('button', name=action, exact=True).click()
        confirmation = page.locator('.toast').filter(has_text=message)
        expect(confirmation.get_by_role('button', name='Undo', exact=True)).to_be_visible()
        if entry == 'cell':
            # An inline edit patches its rows in place (C-34) and asks for no refresh; the next one would come with the
            # 10 s pulse, after this 10 s message may have gone. Start that refresh now, as reconnecting does.
            page.evaluate("window.dispatchEvent(new Event('online'))")
        wait_held(page, held)
        expect(confirmation.get_by_role('button', name='Undo', exact=True)).to_be_visible()
        expect(page.locator('.toast').filter(has_text=re.compile(r'^Saved\.'))).to_have_count(0)
        held[0].continue_()
        for row_id in ids:
            expect(page.locator(f'tr[data-row="{row_id}"] td[data-col=tags]')).to_contain_text('Controlled refresh')
        confirmation.get_by_role('button', name='Undo', exact=True).click()
        expect(page.get_by_text('Undone. The previous values are back.', exact=True)).to_be_visible()
        after = {p['id']: p.get('tags') or [] for p in h.get('coordinator', '/api/catalogues')['people'] if p['id'] in ids}
        assert after == before
    finally:
        context.close()


def test_people_confirmed_tags_and_code_stay_current_when_refresh_fails(browser, live_server, h):
    person = h.get('coordinator', '/api/catalogues')['people'][0]
    h.configure('tags', {'name': 'Existing membership', 'labels': ['people']})
    h.command('coordinator', 'catalogue.bulk_update', {'catalogue': 'people',
        'items': [{'id': person['id'], 'revision': person['revision']}],
        'set': {'tags': ['existing membership'], 'short_code': 'OLDQA'}, 'reason': 'Failed refresh fixture'})
    context, page = sign_in(browser, live_server, 'coordinator')
    writes = []
    try:
        navigate(page, 'people')
        toolbar(page, 'View settings').locator('.dt-pop-item', has_text='Short code').locator('input').check()
        close_popover(page)
        row = page.locator(f'.dt-table tr[data-row="{person["id"]}"]')
        expect(row.locator('td[data-col=tags] .tag-ref')).to_contain_text('Existing membership')
        expect(row.locator('td[data-col=name] .code-chip')).to_have_text('OLDQA')
        page.on('request', lambda request: writes.append(request) if request.method == 'POST' and request.url.endswith('/api/commands') else None)
        def fail_refresh(route):
            route.fulfill(status=503, content_type='application/json', body='{"error":{"code":"REFRESH_UNAVAILABLE","message":"Synthetic catalogue refresh unavailable"}}')
        page.route('**/api/me', fail_refresh)
        cell = row.locator('td[data-col=tags]')
        cell.focus()
        cell.press('Enter')  # Edit the cell, rather than activate its existing tag link.
        cell.get_by_label('Search Tags').fill('Confirmed membership')
        cell.get_by_role('button', name='Add \u201cConfirmed membership\u201d', exact=True).click()
        cell.get_by_role('button', name='Save', exact=True).click()
        # rc.16 (maintenance): a page showing data says so in one stale banner (no toast); the confirmed save stays below.
        expect(page.locator('#main > .stale-banner')).to_contain_text('Couldn’t refresh')
        # The server's reason is text on the line (touch and keyboard read it), not a hover-only tip.
        expect(page.locator('#main > .stale-banner')).to_contain_text('Couldn’t refresh · showing data from')
        expect(page.locator('#main > .stale-banner').get_by_text('Synthetic catalogue refresh unavailable')).to_be_visible()
        expect(page.locator('#main > .stale-banner')).not_to_have_attribute('data-tip', re.compile('.'))
        expect(cell).to_contain_text('Confirmed membership')
        expect(cell).to_contain_text('existing membership')
        expect(cell.locator('.tag-ref')).to_have_count(0)  # Stale membership counts are not invented.
        expect(row.locator('.editing,.saving')).to_have_count(0)
        # The second save must use the returned revision and invalidate the person-name code cache too.
        row.locator('td[data-col=code]').click()
        editor = row.get_by_label('Short code', exact=True)
        editor.fill('NEWQA')
        editor.press('Shift+Tab')
        expect(row.locator('td[data-col=name] .code-chip')).to_have_text('NEWQA')
        expect(row.locator('td[data-col=code]')).to_have_text('NEWQA')
        expect(row.get_by_label('Search Tags')).to_be_focused()
        row.get_by_label('Search Tags').press('Escape')
        expect(row.get_by_label('Search Tags')).not_to_be_visible()
        page.keyboard.press('Escape')  # The first Escape closes the picker; the second cancels the cell editor.
        expect(row.locator('.editing,.saving')).to_have_count(0)
        assert len(writes) == 2
        saved = next(p for p in h.get('coordinator', '/api/catalogues')['people'] if p['id'] == person['id'])
        assert saved['short_code'] == 'NEWQA' and set(saved['tags']) == {'existing membership', 'Confirmed membership'}
        page.unroute('**/api/me', fail_refresh)
        page.locator('.toast').filter(has_text='Short code saved.').get_by_role('button', name='Undo', exact=True).click()
        expect(row.locator('td[data-col=name] .code-chip')).to_have_text('OLDQA')
        expect(cell).to_contain_text('Confirmed membership')
        expect(cell.locator('.tag-ref')).to_have_count(2)
    finally:
        context.close()


def test_inline_shift_tab_saves_in_place_and_opens_the_live_previous_cell(browser, live_server, h):
    # An inline edit patches the row from the command's reply (C-34): no session re-read, and Shift+Tab opens the
    # previous cell of the connected table.
    context, page = sign_in(browser, live_server, 'coordinator')
    reads = []
    try:
        navigate(page, 'people')
        toolbar(page, 'View settings').locator('.dt-pop-item', has_text='Short code').locator('input').check()
        close_popover(page)
        row_id = page.locator('.dt-table tbody tr[data-row]').first.get_attribute('data-row')
        row = page.locator(f'.dt-table tr[data-row="{row_id}"]')
        row.locator('td[data-col="code"]').click()
        editor = row.get_by_label('Short code', exact=True)
        editor.fill('TABQA')
        page.on('request', lambda r: reads.append(r.url) if r.method == 'GET' and ('/api/me' in r.url or '/api/catalogues' in r.url) else None)
        editor.press('Shift+Tab')
        expect(page.locator('.toast').filter(has_text='Short code saved.').get_by_role('button', name='Undo', exact=True)).to_be_visible()
        # Locate through the connected document, never through a detached old table handle.
        expect(row.get_by_label('Search Tags')).to_be_focused()
        assert row.get_by_label('Search Tags').evaluate('node => node.isConnected')
        assert reads == [], reads
        assert next(p for p in h.get('coordinator', '/api/catalogues')['people'] if p['id'] == row_id)['short_code'] == 'TABQA'
    finally:
        context.close()
