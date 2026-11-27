"""Staff spreadsheet import (upload → preview → apply, split by role) and bulk clinics through the one Change engine. Real Chromium and API."""
import re
import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, navigate  # noqa: F401
from .test_browser_bulk_changes import bar, panel, submit
from .test_browser_bulk_sheet import no_light_path
from .test_staff_import import EXISTING_PERSON, NEW, csv_of, row, setup_catalogue, import_business_state, apply, preview

pytestmark = pytest.mark.browser


def test_applied_import_is_terminal_until_undone(browser, live_server, h):
    batch = apply(h, "coordinator", preview(h, csv_of(row(display_name="Terminal person")), name="Terminal staff")["batch"])["batch"]
    assert batch["status"] == "applied"
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page, "system/imports")
        page.get_by_role('row').filter(has_text='Terminal staff').get_by_role('button', name=re.compile('^Review')).click()
        review = page.locator(".dock-panel.staff-import")
        expect(review.locator(".import-head")).to_contain_text(re.compile(r"Applied\s*· \d{1,2} [A-Z][a-z]{2} \d{4}"))
        expect(review.get_by_role("button", name="Undo this import", exact=True)).to_be_visible()
        expect(review.get_by_role("button", name=re.compile(r"^Apply"))).to_have_count(0)
        expect(review.locator(".import-summary, .import-parts")).to_have_count(0)

        review.get_by_role("button", name="Undo this import", exact=True).click()
        review.get_by_role("button", name=re.compile(r"^Confirm")).click()
        expect(review.locator(".import-head")).to_contain_text("Undone")
        expect(review.locator(".import-summary")).to_be_visible()
        expect(review.locator(".import-parts")).to_be_visible()
        expect(review.get_by_role("button", name="Apply people & ranks", exact=True)).to_be_enabled()
        assert h.get("coordinator", "/api/staff-imports/" + batch["id"])["batch"]["status"] == "undone"
        review.get_by_role("button", name="Apply people & ranks", exact=True).click()
        expect(review.locator(".import-summary, .import-parts")).to_have_count(0)
        expect(review.locator(".import-head")).to_contain_text("Applied")
    finally:
        context.close()


def test_import_applied_by_another_client_redraws_stale_review_without_reapplying(browser, live_server, h):
    batch = preview(h, csv_of(row(display_name='Stale review person')), name='Stale staff')['batch']
    context, page = sign_in(browser, live_server, 'coordinator')
    sent = applies(page)
    try:
        navigate(page, 'system/imports')
        page.get_by_role('row').filter(has_text='Stale staff').get_by_role('button', name=re.compile('^Review')).click()
        review = page.locator('.dock-panel.staff-import')
        apply_button = review.get_by_role('button', name='Apply people & ranks', exact=True)
        expect(apply_button).to_be_enabled()
        apply(h, 'coordinator', batch)
        applied = h.get('coordinator', '/api/staff-imports/' + batch['id'])['batch']
        application, = applied['applied']['applications']
        assert application['by_name']
        audit = h.get('auditor', '/api/audit')['events']
        before = import_business_state(h)
        with page.expect_response(lambda r: r.url.endswith('/api/commands') and r.request.method == 'POST') as reply:
            apply_button.click()
        assert reply.value.status == 409 and reply.value.json()['error']['code'] == 'IMPORT_ALREADY_APPLIED'
        expect(review.get_by_text('This import was applied since this review opened, so nothing was applied again.',
                                  exact=True)).to_be_visible()
        expect(review.locator('.import-head')).to_contain_text(re.compile(
            r'Applied\s*· \d{1,2} [A-Z][a-z]{2} \d{4} · by ' + re.escape(application['by_name'])))
        expect(review.get_by_role('button', name=re.compile('^Apply'))).to_have_count(0)
        saved = h.get('coordinator', '/api/staff-imports/' + batch['id'])['batch']
        assert saved['status'] == 'applied' and saved['applied']['applications'] == [application]
        assert len(sent) == 1
        assert import_business_state(h) == before
        assert h.get('auditor', '/api/audit')['events'] == audit
    finally:
        context.close()


def test_upload_preview_and_apply_split_between_coordinator_and_credentialer(browser, live_server, h):
    setup_catalogue(h)
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        navigate(page,"system/imports")
        page.get_by_role("button", name="Upload staff CSV").click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("CSV file").set_input_files({"name": "October staff.csv", "mimeType": "text/csv", "buffer": csv_of(EXISTING_PERSON, NEW).encode()})
        expect(dialog.get_by_label("Name for this import")).to_have_value("October staff")
        dialog.get_by_role("button", name="Preview").click()
        review = page.locator(".dock-panel.staff-import")
        summary = review.locator(".import-summary")
        expect(summary).to_contain_text("1 new person")
        expect(summary).to_contain_text("2 rank changes")
        expect(summary).to_contain_text("2 clinic permissions")
        expect(review.locator(".import-part.waiting")).to_contain_text("needs a credentialer")
        expect(review.get_by_text("Dr Nadia Example")).to_be_visible()
        # F13 A-26: the panel leaves the page its room (820px by default) and does not repeat the matching rules.
        assert review.evaluate("n => n.getBoundingClientRect().width") <= 822
        expect(review).not_to_contain_text("Every row is matched")
        review.get_by_role("button", name="Apply people & ranks").click()
        expect(page.get_by_text(re.compile(r"Applied 1 new and 1 updated people"))).to_be_visible()
        assert any(p["display_name"] == "Dr Nadia Example" for p in h.get("admin", "/api/catalogues")["people"])
    finally:
        context.close()
    context, page = sign_in(browser, live_server, "credentialer")
    try:
        navigate(page,"system/imports")
        page.get_by_role("button", name="Review").first.click()
        review = page.locator(".dock-panel.staff-import")
        expect(review.locator(".import-parts")).to_contain_text("4 changes you can apply")
        review.get_by_role("button", name="Apply clinics & skills").click()
        expect(page.get_by_text("Applied 2 clinic permissions, 2 skills.")).to_be_visible()
        expect(review.locator(".import-head")).to_contain_text("Applied")
        grants = h.get("admin", "/api/catalogues")["grants"]
        assert sum(g["evidence"] == "Credentialing committee 2026-09" for g in grants) == 1
    finally:
        context.close()


def test_bulk_authorise_clinics_opens_the_one_change_engine_then_saves(browser, live_server, h):
    """P-07: the bar's Authorise clinics is a shortcut into Change… (one engine: Preview, Results, one Undo) with its
    change chosen; the page's own matrix dialog is gone."""
    clinic, _, _ = setup_catalogue(h)
    no_light_path(h)
    context, page = sign_in(browser, live_server, "credentialer")
    try:
        navigate(page, "people")
        boxes = page.locator("td .bulk-check")
        boxes.nth(0).check()
        boxes.nth(1).check()
        bar(page).get_by_role("button", name="Authorise clinics", exact=True).click()
        change = page.get_by_role("region", name="Change 2 people")
        expect(change.get_by_role("combobox", name="What to change")).to_contain_text("Authorise clinics")
        expect(page.get_by_role("dialog")).to_have_count(0)  # no modal of its own
        row = change.locator(".bulk-changes .cond-row").last
        row.get_by_label("Clinic", exact=True).select_option(label="Minor injuries")
        start = row.locator(".bulk-value .date-text").first
        start.fill("1 Oct 2026")
        start.press("Tab")
        submit(page)
        expect(panel(page).get_by_role("heading", name="Preview — nothing saved yet")).to_be_visible()
        expect(panel(page).locator("tbody tr", has=page.locator(".badge", has_text="Ready"))).to_have_count(2)
        panel(page).get_by_role("button", name=re.compile(r"^Apply ready rows \(2\)")).click()
        expect(page.locator(".toast", has_text=re.compile("changed|added|authorised", re.I))).to_be_visible()
        assert sum(r["service_id"] == clinic for r in h.get("admin", "/api/catalogues")["eligibilities"]) == 2
        # Overview opens with clinics and skills first for a credentialer: every clinic and skill with its dates.
        page.locator("td .bulk-check").nth(0).locator("xpath=ancestor::tr").locator("td").nth(1).click()
        card = page.locator(".dock-panel.person-panel")
        expect(card.get_by_role("tab", name="Overview", exact=True)).to_have_attribute("aria-selected", "true")
        expect(card.locator(".ph-section h3").nth(1)).to_have_text("Clinics")
        expect(card.locator(".ph-row").filter(has_text="Minor injuries")).to_be_visible()
        expect(card.locator(".ph-row").filter(has_text="Demo supervision")).to_be_visible()
    finally:
        context.close()


def test_invalid_import_recovers_to_corrected_preview_without_applying(browser, live_server, h):
    setup_catalogue(h)
    before = h.get('admin', '/api/catalogues')
    batches_before = h.get('coordinator', '/api/staff-imports')['batches']
    values = dict(display_name='Import recovery person (test)', short_code='qarec', rank='Demo practitioners',
                  rank_from='2026-10-01', clinics='Minor injuries', skills='Suturing', skill_evidence='Fictional evidence')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'system/imports')
        page.get_by_role('button', name='Upload staff CSV', exact=True).click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_label('CSV file', exact=True).set_input_files({
            'name': 'Invalid header.csv', 'mimeType': 'text/csv', 'buffer': b'display_name,unknown_column\r\nExample,value\r\n'})
        dialog.get_by_role('button', name='Preview', exact=True).click()
        expect(dialog.get_by_text('Unknown columns:', exact=False)).to_be_visible()
        assert h.get('coordinator', '/api/staff-imports')['batches'] == batches_before
        dialog.get_by_label('Name for this import', exact=True).fill('Audit invalid email')
        dialog.get_by_label('CSV file', exact=True).set_input_files({
            'name': 'Invalid email.csv', 'mimeType': 'text/csv', 'buffer': csv_of(row(**values, email='not-an-email')).encode()})
        dialog.get_by_role('button', name='Preview', exact=True).click()
        review = page.locator('.dock-panel.staff-import')
        expect(review.locator('.cell-error')).to_have_count(1)
        expect(review.locator('.cell-error')).to_contain_text('email')
        expect(review.get_by_role('button', name='Apply people & ranks', exact=True)).to_be_disabled()
        expect(review).to_contain_text('Nothing is applied while a row has a problem.')
        page.reload()
        navigate(page, 'system/imports')
        page.get_by_role('button', name='Upload staff CSV', exact=True).click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_label('CSV file', exact=True).set_input_files({
            'name': 'Audit corrected email.csv', 'mimeType': 'text/csv',
            'buffer': csv_of(row(**values, email='audit.recovery@example.test')).encode()})
        dialog.get_by_role('button', name='Preview', exact=True).click()
        expect(review.locator('.cell-error')).to_have_count(0)
        expect(review.get_by_role('button', name='Apply people & ranks', exact=True)).to_be_enabled()
        expect(review.locator('.import-part.waiting')).to_contain_text('needs a credentialer')
        page.reload()
        navigate(page, 'system/imports')
        page.get_by_role('row').filter(has_text='Audit corrected email').get_by_role('button', name='Review', exact=True).click()
        expect(review).to_contain_text(values['display_name'])
        expect(review.locator('.cell-error')).to_have_count(0)
        expect(review.get_by_role('button', name='Apply people & ranks', exact=True)).to_be_enabled()
        batch = next(b for b in h.get('coordinator', '/api/staff-imports')['batches'] if b['source_name'] == 'Audit corrected email')
        assert batch['status'] == 'preview'
        after = h.get('admin', '/api/catalogues')
        for key in ('people', 'memberships', 'programme_memberships', 'eligibilities', 'grants'):
            assert after[key] == before[key], key
    finally:
        context.close()


def applies(page):
    """Every staff_import.apply the page sends, with the fingerprint it names (Command.expected_fingerprint)."""
    sent = []
    page.on('request', lambda r: sent.append(r.post_data_json) if r.url.endswith('/api/commands') and r.method == 'POST'
            and (r.post_data_json or {}).get('kind') == 'staff_import.apply' else None)
    return sent


def upload_sheet(page, name, sheet):
    navigate(page, 'system/imports')
    page.get_by_role('button', name='Upload staff CSV', exact=True).click()
    dialog = page.get_by_role('dialog')
    dialog.get_by_label('CSV file', exact=True).set_input_files({'name': name + '.csv', 'mimeType': 'text/csv', 'buffer': sheet.encode()})
    dialog.get_by_role('button', name='Preview', exact=True).click()
    return page.locator('.dock-panel.staff-import')


def test_apply_names_the_review_on_screen_and_each_part_reads_the_review_again(browser, live_server, h):
    """PCF-S02: Apply sends the fingerprint of the review shown; after the coordinator's part (and its Undo) the review is
    read again, so the credentialer's part names the current plan, never the one first shown."""
    setup_catalogue(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    sent = applies(page)
    try:
        review = upload_sheet(page, 'Reviewed fingerprint', csv_of(EXISTING_PERSON, NEW))
        expect(review.get_by_text('Dr Nadia Example')).to_be_visible()
        batch = h.get('coordinator', '/api/staff-imports')['batches'][0]
        shown = h.get('coordinator', '/api/staff-imports/' + batch['id'])['source_fingerprint']
        review.get_by_role('button', name='Apply people & ranks', exact=True).click()
        expect(page.get_by_text(re.compile(r'Applied 1 new and 1 updated people'))).to_be_visible()
        assert [x['expected_fingerprint'] for x in sent] == [shown], sent
        assert re.fullmatch(r'[0-9a-f]{64}', shown)
        expect(review.locator('.import-part').filter(has_text='People, ranks & programmes')).to_contain_text('Applied')
        expect(review.get_by_role('button', name='Apply people & ranks', exact=True)).to_have_count(0)
        review.get_by_role('button', name='Undo this import', exact=True).click()
        review.get_by_role('button', name=re.compile('^Confirm')).click()
        expect(review.get_by_role('button', name='Apply people & ranks', exact=True)).to_be_enabled()
        again = h.get('coordinator', '/api/staff-imports/' + batch['id'])['source_fingerprint']
        review.get_by_role('button', name='Apply people & ranks', exact=True).click()
        expect(page.get_by_text(re.compile(r'Applied 1 new and 1 updated people'))).to_be_visible()
        assert sent[-1]['expected_fingerprint'] == again and len(sent) == 2, sent
        expect(review.locator('.import-part').filter(has_text='People, ranks & programmes')).to_contain_text('Applied')
        expect(review.get_by_role('button', name='Apply people & ranks', exact=True)).to_have_count(0)
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'credentialer')
    sent = applies(page)
    try:
        navigate(page, 'system/imports')
        page.get_by_role('button', name='Review').first.click()
        review = page.locator('.dock-panel.staff-import')
        expect(review.get_by_role('button', name='Apply clinics & skills', exact=True)).to_be_enabled()
        current = h.get('credentialer', '/api/staff-imports/' + batch['id'])['source_fingerprint']
        review.get_by_role('button', name='Apply clinics & skills', exact=True).click()
        expect(page.get_by_text('Applied 2 clinic permissions, 2 skills.')).to_be_visible()
        assert [x['expected_fingerprint'] for x in sent] == [current], sent
    finally:
        context.close()


@pytest.mark.parametrize('drift', ['target', 'reference'])
def test_a_review_changed_by_another_session_is_never_applied_and_is_shown_again(browser, live_server, h, drift):
    """PCF-S02: another session changes the reviewed person (target) or renames its clinic (reference) after the review is
    shown. Apply is refused STALE_PREVIEW with nothing written; the page says so and shows the new review, and sends no
    second Apply by itself. A target drift can then be applied explicitly from the new review."""
    from apps.api import models as m
    from apps.api.db import transaction
    from sqlalchemy import select
    setup_catalogue(h)
    person = next(p for p in h.get('admin', '/api/catalogues')['people'] if p['id'] == h.seed['people'][0])
    context, page = sign_in(browser, live_server, 'coordinator')
    sent = applies(page)
    try:
        review = upload_sheet(page, 'Drifting review', csv_of(row(display_name=person['display_name'], title='Reviewed title',
                                                                    clinics='Minor injuries')))
        apply = review.get_by_role('button', name='Apply people & ranks', exact=True)
        expect(apply).to_be_enabled()
        with transaction(h.app.state.engine, write=True) as db:  # a second session, after the review is on screen
            if drift == 'target':
                record = db.get(m.Person, person['id'])
                record.title = 'Another operator changed this'
            else:
                record = db.scalar(select(m.Service).where(m.Service.name == 'Minor injuries',
                                                          m.Service.department_id == h.seed['department_id']))
                record.name = 'Renamed clinic after review'
            record.revision += 1
        before = import_business_state(h)
        apply.click()
        expect(review).to_contain_text('People, clinics or dates changed since this review, so nothing was applied.')
        assert len(sent) == 1, sent  # refused once; nothing re-sent by itself
        assert import_business_state(h) == before
        batch = h.get('coordinator', '/api/staff-imports')['batches'][0]
        fresh = h.get('coordinator', '/api/staff-imports/' + batch['id'])['source_fingerprint']
        assert fresh != sent[0]['expected_fingerprint']
        if drift == 'target':
            review.get_by_role('button', name='Apply people & ranks', exact=True).click()
            expect(page.get_by_text(re.compile(r'Applied 0 new and 1 updated people'))).to_be_visible()
            assert len(sent) == 2 and sent[1]['expected_fingerprint'] == fresh, sent
    finally:
        context.close()
