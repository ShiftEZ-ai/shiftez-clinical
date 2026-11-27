"""Schedule overlap journeys end to end (docs/design/schedule-unification.md §6, §7): New draft over a published rota
offers Start after it / Replace from a date / Change the published rota, and each path reaches a published rota
(Submit for review… → submit, an approver approves, Publish… → Publish with its same-button confirm); a draft overtaken by a
later publication is flagged in the footer and in Publish…; competing drafts open side by side."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, save_dialog, decide, wait_held, press_escape  # noqa: F401
from .test_browser_operator_governance_actions import issued_evidence
from .test_browser_schedule import at, checked, october, overlapping, show, settled, sign_in, watch, hash_of

pytestmark = pytest.mark.browser


def published_october(h):
    """Published October rota, 4–13 Oct (Alex on 4 Oct; Alex and Jordan on 12 Oct). Returns its publication id."""
    a, b, _ = h.seed['people']
    w = h.shift(h.workspace('October rota', '2026-10-04', '2026-10-14'), *at(4), people=[a])['workspace']
    w = h.shift(w, *at(12), people=[a, b])['workspace']
    return h.publish(w)['publication_id']


def new_draft(page, name, first, last):
    """New draft (published only, so New draft is the primary) with dates that touch the published rota."""
    show(page, 'schedule/week/2026-10-11?edit=none')
    page.get_by_role('button', name='New rota', exact=True).click()
    dialog = page.get_by_role('dialog', name='New rota')
    rota_name = dialog.get_by_label('Rota name', exact=True)
    # The initial date-based suggestion must finish before Playwright selects text to replace.
    expect(rota_name).not_to_have_value('')
    rota_name.fill(name)
    expect(rota_name).to_have_value(name)
    dialog.get_by_label('First day', exact=True).fill(first)
    dialog.get_by_label('Last day', exact=True).fill(last)
    fit = dialog.get_by_role('radiogroup', name='How should the draft fit?')
    expect(fit).to_be_visible()
    return dialog, fit


def created(h, name):
    return next(w for w in h.get('coordinator', '/api/workspaces?limit=50&offset=0')['workspaces'] if w['name'] == name)


def submit_approve_publish(browser, live_server, h, page, w, day, approve_in_ui=False):
    """Submit for review… submits the draft; the approver approves; Publish… publishes it after the same-button confirm."""
    w = checked(h, h.shift(h.detail(w)['workspace'], *at(day), people=[h.seed['people'][0]])['workspace'])
    # The open page answers a repeated read from the replies it holds (api.ts keptReads, c6d9edec1): the session and check
    # added through the API above are read by a fresh page, so Submit sends the draft's current revision.
    page.reload()
    show(page, f"schedule/week/2026-10-{day:02}?edit={w['id']}")
    page.get_by_role('button', name='Submit for review…', exact=True).click()
    dialog = page.get_by_role('dialog', name=f"Submit {w['name']} for review")
    # D9: session/place facts (and SW-21 the Fix list's could-improve and score) and the server's cause-based gate replace
    # the old five recounted tiles.
    expect(dialog.locator('.publish-summary .summary-item').first).to_contain_text('Sessions')
    expect(dialog.locator('.publish-summary')).to_contain_text('Places filled')
    assert h.get('coordinator', f"/api/workspaces/{w['id']}/fix-list")['summary']['must'] == 0
    expect(dialog.get_by_role('list', name='Must fix', exact=True)).to_have_count(0)
    expect(dialog.locator('button[type=submit]')).to_be_enabled()
    save_dialog(dialog, 'Submit for review')
    expect(page.locator('.toast').filter(has_text='Submitted for review')).to_be_visible()
    assert h.detail(w)['workspace']['status'] == 'submitted'
    if approve_in_ui:
        context, approver = sign_in(browser, live_server, 'approver')
        try:
            show(approver, f"schedule/week/2026-10-{day:02}?edit={w['id']}")
            expect(approver.get_by_role('button', name='Add session', exact=True)).to_have_count(0)
            decide(approver, 'Approve rota')
        finally:
            context.close()
    else:
        h.lifecycle(h.detail(w)['workspace'], 'approve', 'approver')
    assert h.detail(w)['workspace']['status'] == 'approved'
    if not approve_in_ui:
        # A fixture write in another process has no live notification on SQLite. Read it in a fresh page.
        page.reload()
    show(page, f"schedule/day/2026-10-{day:02}?edit={w['id']}")
    page.get_by_role('button', name='Publish…', exact=True).click()
    dialog = page.get_by_role('dialog', name=f"Publish {w['name']}")
    expect(dialog).to_contain_text('Staff are told as soon as you publish.')
    publish = dialog.get_by_role('button', name='Publish', exact=True)
    publish.click()
    expect(dialog.locator('button.armed')).to_have_text('Confirm publish?')  # same-button confirm, nothing sent yet
    assert h.detail(w)['workspace']['status'] == 'approved'
    dialog.locator('button.armed').click()
    expect(dialog).to_have_count(0, timeout=15000)
    expect(page.locator('.toast').filter(has_text='Published · staff are told.')).to_be_visible()
    assert h.detail(w)['workspace']['status'] == 'published'
    return {p['workspace_id']: p for p in h.get('coordinator', '/api/publications')['publications']}


def test_start_after_it_is_the_default_and_reaches_a_published_rota(browser, live_server, h):
    pid = published_october(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    errors = watch(page)
    try:
        dialog, fit = new_draft(page, 'After October', '2026-10-10', '2026-10-20')
        expect(dialog).to_contain_text('October rota · Published')
        start_after = fit.get_by_role('radio', name=re.compile('^Start after it'))
        expect(start_after).to_be_checked()
        expect(fit.get_by_role('radio', name=re.compile('^Replace from a date'))).not_to_be_checked()
        expect(fit.get_by_role('radio', name=re.compile('^Change the published rota'))).not_to_be_checked()
        submit = dialog.get_by_role('button', name='Create draft from 14 Oct', exact=True)
        expect(submit).to_be_visible()
        submit.click()
        expect(dialog).to_have_count(0, timeout=15000)
        settled(page)
        w = created(h, 'After October')
        assert (w['starts_on'], w['ends_on'], w.get('replaces_publication_id'), w.get('base_publication_id')) == ('2026-10-14', '2026-10-21', None, None)
        expect(page.locator('.topbar h1')).to_have_text('After October')
        pubs = submit_approve_publish(browser, live_server, h, page, w, 15, approve_in_ui=True)
        old = next(p for p in pubs.values() if p['id'] == pid)
        assert old['current'] and old['effective_until'] is None  # untouched: the new rota starts after it
        assert pubs[w['id']]['current'] and pubs[w['id']]['starts_on'] == '2026-10-14'
        assert not errors, errors
    finally:
        context.close()


def test_replace_from_a_date_trims_the_published_rota_on_publish(browser, live_server, h):
    pid = published_october(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    errors = watch(page)
    try:
        dialog, fit = new_draft(page, 'Replacement rota', '2026-10-09', '2026-10-21')
        expect(fit).to_contain_text('The draft runs 14 Oct 2026 – 21 Oct 2026.')
        fit.get_by_role('radio', name=re.compile('^Replace from a date')).check()
        expect(fit).to_contain_text('this draft takes over from 9 Oct 2026.')
        # A takeover date not chosen by the person is the suggestion, which follows the first day.
        dialog.get_by_label('First day', exact=True).fill('2026-10-10')
        expect(fit).to_contain_text('this draft takes over from 10 Oct 2026.')
        # A chosen one stays when the answer for the dates changed before it is drawn after it (it went back to 10 Oct).
        held = []
        overlaps = re.compile(r'.*/api/schedule/overlaps\?starts_on=2026-10-10&ends_on=2026-10-21$')
        page.route(overlaps, lambda route: held.append(route))
        dialog.get_by_label('Last day', exact=True).fill('2026-10-20')
        wait_held(page, held)
        takes_over = fit.get_by_label('Takes over from', exact=True)
        expect(takes_over).to_have_attribute('min', '2026-10-05')
        expect(takes_over).to_have_attribute('max', '2026-10-13')
        takes_over.fill('2026-10-12')
        words = 'October rota stays in force until 11 Oct 2026; this draft takes over from 12 Oct 2026.'
        expect(fit).to_contain_text(words)
        held.pop().continue_()
        page.unroute(overlaps)
        expect(fit).to_contain_text('The draft runs 14 Oct 2026 – 20 Oct 2026.')
        expect(fit).to_contain_text(words)
        dialog.get_by_role('button', name='Create draft from 12 Oct', exact=True).click()
        expect(dialog).to_have_count(0, timeout=15000)
        settled(page)
        w = created(h, 'Replacement rota')
        assert (w['starts_on'], w['replaces_publication_id']) == ('2026-10-12', pid)
        # Publish… names the replacement before the decision.
        show(page, f"schedule/week/2026-10-12?edit={w['id']}")
        page.get_by_role('button', name='Submit for review…', exact=True).click()
        summary = page.get_by_role('dialog', name='Submit Replacement rota for review')
        expect(summary).to_contain_text('Replaces October rota from 12 Oct 2026. October rota stays in force until then.')
        press_escape(page)
        expect(summary).to_have_count(0)
        pubs = submit_approve_publish(browser, live_server, h, page, w, 15)
        old = next(p for p in pubs.values() if p['id'] == pid)
        assert old['current'] and old['effective_until'] == '2026-10-12'
        # The schedule now shows the new rota from 12 Oct and the old one before it.
        show(page, 'schedule/month/2026-10-12?edit=none')
        month = page.get_by_role('region', name='Month view')
        cell = lambda day: month.get_by_role('gridcell', name=re.compile(rf'^{day} Oct 2026 · ')).locator('.pchip')
        expect(cell(4)).not_to_have_count(0)  # October rota
        expect(cell(12)).to_have_count(0)  # replaced day
        expect(cell(15)).not_to_have_count(0)  # Replacement rota
        assert not errors, errors
    finally:
        context.close()


def test_change_the_published_rota_opens_an_amendment_that_supersedes_it(browser, live_server, h):
    pid = published_october(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    errors = watch(page)
    try:
        dialog, fit = new_draft(page, 'Ignored name', '2026-10-10', '2026-10-20')
        fit.get_by_role('radio', name=re.compile('^Change the published rota')).check()
        dialog.get_by_role('button', name='Change the published rota', exact=True).click()
        amendment = page.get_by_role('dialog', name='Create amendment')
        expect(amendment.get_by_label('Rota name', exact=True)).to_have_value('Amendment · October rota')
        expect(amendment.get_by_label('First day', exact=True)).to_be_disabled()
        expect(amendment.get_by_label('First day', exact=True)).to_have_value('Sun 4 Oct 2026')
        expect(amendment.get_by_label('Last day', exact=True)).to_have_value('Tue 13 Oct 2026')
        amendment.get_by_role('button', name='Create draft', exact=True).click()
        expect(amendment).to_have_count(0, timeout=15000)
        settled(page)
        w = created(h, 'Amendment · October rota')
        assert (w['starts_on'], w['ends_on'], w['base_publication_id']) == ('2026-10-04', '2026-10-14', pid)
        assert not any(x['name'] == 'Ignored name' for x in h.get('coordinator', '/api/workspaces?limit=50&offset=0')['workspaces'])
        pubs = submit_approve_publish(browser, live_server, h, page, w, 6)
        old = next(p for p in h.get('coordinator', '/api/publications')['publications'] if p['id'] == pid)
        assert not old['current'] and pubs[w['id']]['current'] and pubs[w['id']]['starts_on'] == '2026-10-04'
        assert not errors, errors
    finally:
        context.close()


def test_the_fit_choice_stays_when_the_dates_change(browser, live_server, h):
    """The fit is drawn for the dates as they are typed: a choice made on one answer (first day typed) survives the
    answer for the final dates (the merge-queue flake of the amendment journey, where it fell back to the default)."""
    published_october(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    held = []
    try:
        dialog, fit = new_draft(page, 'Kept choice', '2026-10-10', '2026-10-22')
        fit.get_by_role('radio', name=re.compile('^Change the published rota')).check()
        page.route(re.compile(r'.*/api/schedule/overlaps\?starts_on=2026-10-10&ends_on=2026-10-21$'), lambda route: held.append(route))
        dialog.get_by_label('Last day', exact=True).fill('2026-10-20')
        wait_held(page, held)
        with page.expect_event('requestfinished', lambda r: '/api/schedule/overlaps' in r.url):
            held.pop().continue_()
        expect(fit.get_by_role('radio', name=re.compile('^Change the published rota'))).to_be_checked()
        expect(dialog.get_by_role('button', name='Change the published rota', exact=True)).to_be_visible()
    finally:
        context.close()


def test_a_draft_overtaken_by_a_publication_is_flagged_and_publish_offers_the_fit(browser, live_server, h):
    a = h.seed['people'][0]
    d = h.shift(h.workspace('Planned first', '2026-10-14', '2026-10-21'), *at(18), people=[a])['workspace']
    d = checked(h, d)
    # Meanwhile another rota is published over the first days of this draft.
    later = h.shift(h.workspace('Published meanwhile', '2026-10-14', '2026-10-17'), *at(15), people=[a])['workspace']
    h.publish(later)
    context, page = sign_in(browser, live_server, 'coordinator')
    errors = watch(page)
    try:
        show(page, f"schedule/week/2026-10-18?edit={d['id']}")
        pill = page.locator('footer').get_by_role('button', name='1 overlap with a published rota')
        expect(pill).to_be_visible()
        pill.click()
        panel = page.get_by_role('dialog', name='These days already have a published rota')
        expect(panel.get_by_role('radiogroup', name='How should the draft fit?')).to_be_visible()
        expect(panel.get_by_role('button', name='Use these dates from 17 Oct', exact=True)).to_be_visible()
        page.keyboard.press('Escape')
        expect(panel).to_have_count(0)
        page.get_by_role('button', name='Submit for review…', exact=True).click()
        dialog = page.get_by_role('dialog', name='Submit Planned first for review')
        changed = dialog.get_by_role('group', name='Changed since you started')
        expect(changed.get_by_role('radio', name=re.compile('^Start after it'))).to_be_checked()
        expect(changed.get_by_role('radio', name=re.compile('^Replace from a date'))).to_be_visible()
        expect(changed.get_by_role('radio', name=re.compile('^Change the published rota'))).to_be_visible()
        dialog.get_by_role('button', name='Submit from 17 Oct', exact=True).click()
        expect(dialog).to_have_count(0, timeout=15000)
        w = h.detail(d)['workspace']
        assert (w['starts_on'], w['status']) == ('2026-10-17', 'submitted'), (w['starts_on'], w['status'])
        assert not errors, errors
    finally:
        context.close()


def test_competing_drafts_are_compared_side_by_side_from_publish(browser, live_server, h):
    _, d = october(h)
    d2 = overlapping(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    errors = watch(page)
    try:
        show(page, f"schedule/week/2026-10-15?edit={d['id']}")
        expect(page.locator('footer').get_by_role('button', name='1 other draft for these days')).to_be_visible()
        page.get_by_role('button', name='Submit for review…', exact=True).click()
        dialog = page.get_by_role('dialog', name='Submit Mid-October draft for review')
        changed = dialog.get_by_role('group', name='Changed since you started')
        expect(changed).to_contain_text('Another draft covers these days')
        expect(changed).to_contain_text('Overlap draft · Draft')
        expect(changed.get_by_role('radiogroup')).to_have_count(0)  # no published overlap: nothing to choose
        changed.get_by_role('button', name='Compare', exact=True).click()
        expect(dialog).to_have_count(0)
        settled(page)
        assert hash_of(page).endswith(f"&mode=side&compare={d2['id']}"), hash_of(page)
        expect(page.get_by_role('button', name=re.compile(r'^View settings: '))).to_have_accessible_name('View settings: Week · Side by side')
        expect(page.locator('#schedule-view').get_by_label('Compare with', exact=True)).to_have_value(d2['id'])
        assert not errors, errors
    finally:
        context.close()


def test_reopen_reviewed_draft_invalidates_review_and_preserves_publication(browser, live_server, h):
    publication_id = published_october(h)
    issued = issued_evidence(h, publication_id)
    w = h.shift(h.workspace('Reopen reviewed draft', '2026-10-15', '2026-10-16'), *at(15))['workspace']
    w = h.lifecycle(w, 'submit')['workspace']
    w = h.lifecycle(w, 'approve', 'approver')['workspace']
    before = h.detail(w)
    assert w['status'] == 'approved' and w['submitted_fingerprint'] and w['review_snapshot']
    assert before['review_progress']['complete']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        show(page, 'schedule/week/2026-10-15?edit=' + w['id'])
        decide(page, 'Reopen as draft')
        page.reload()
        show(page, 'schedule/week/2026-10-15?edit=' + w['id'])
        expect(page.get_by_role('button', name='Add session', exact=True)).to_be_visible()
        expect(page.get_by_role('button', name='Reopen as draft', exact=True)).to_have_count(0)
        after = h.detail(w)
        assert after['workspace']['status'] == 'draft'
        assert after['workspace']['revision'] > before['workspace']['revision']
        assert after['workspace']['submitted_fingerprint'] is None
        assert after['workspace']['submitted_at'] is None and after['workspace']['review_snapshot'] is None
        assert after['review_progress']['complete'] is False
        assert after['shifts'] == before['shifts']
        assert issued_evidence(h, publication_id) == issued
    finally:
        context.close()


def test_competing_draft_dialog_closes_with_close_and_discards_a_duplicate(browser, live_server, h):
    """G-47: with only competing drafts (nothing published overlaps), the dialog's one way out reads Close, and a
    duplicate draft can be discarded from it: reversible, so at once with Undo in its message (D42)."""
    _, d = october(h)
    d2 = overlapping(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    errors = watch(page)
    try:
        show(page, f"schedule/week/2026-10-15?edit={d['id']}")
        page.locator('footer').get_by_role('button', name='1 other draft for these days').click()
        dialog = page.get_by_role('dialog', name='Other drafts cover these days')
        expect(dialog.get_by_role('button', name='Close', exact=True)).to_be_visible()
        expect(dialog.get_by_role('button', name='Cancel', exact=True)).to_have_count(0)
        discard = dialog.locator('.rc-issue', has_text='Overlap draft').get_by_role('button', name=re.compile('^Discard'))
        discard.click()
        expect(page.locator('.toast').filter(has_text='Overlap draft discarded.').get_by_role('button', name='Undo')).to_be_visible()
        assert h.detail(d2)['workspace']['archived_at']
        assert not errors, errors
    finally:
        context.close()


def test_a_late_name_suggestion_never_joins_or_replaces_the_typed_name(browser, live_server, h):
    """A rota-name suggestion (from the dates) that lands while the name is selected stays selected, so the typing
    that follows replaces it (Playwright's fill selects, then inserts: the race seen on CI); once typed, a later
    suggestion leaves the name alone."""
    published_october(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    held = []
    try:
        page.route('**/api/schedule/overlaps*', lambda route: held.append(route))
        show(page, 'schedule/week/2026-10-11?edit=none')
        page.get_by_role('button', name='New rota', exact=True).click()
        dialog = page.get_by_role('dialog', name='New rota')
        name = dialog.get_by_label('Rota name', exact=True)
        wait_held(page, held)
        name.focus()
        name.select_text()
        while held:
            held.pop().continue_()
        page.unroute('**/api/schedule/overlaps*')
        expect(name).not_to_have_value('')  # the suggestion landed between the selection and the typing
        page.keyboard.insert_text('Typed rota')
        expect(name).to_have_value('Typed rota')
        with page.expect_response('**/api/schedule/overlaps*'):
            dialog.get_by_label('Last day', exact=True).fill('2026-10-25')
        expect(name).to_have_value('Typed rota')
    finally:
        context.close()


def test_amendment_draft_is_named_as_such(browser, live_server, h):
    """SR-02: a draft that amends a published rota says so on its status pill and in the switcher, and "Change a
    published rota…" sits under Edit in Rota tools."""
    pid = published_october(h)
    amend = h.workspace('October amendment', '2026-10-04', '2026-10-14', base=pid)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        show(page, f"schedule/week/2026-10-05?edit={amend['id']}")
        expect(page.locator('.rota-status')).to_have_text('Amendment · Draft')
        page.get_by_role('button', name=re.compile(r'^(Switch draft · |Choose a draft)')).click()
        expect(page.get_by_role('dialog', name='Drafts', exact=True).get_by_role('option').filter(has_text='October amendment')).to_contain_text('Amends the published rota')
        press_escape(page)
        page.get_by_role('button', name='Rota tools', exact=True).click()
        menu = page.get_by_role('menu', name='Rota tools', exact=True)
        expect(menu.get_by_role('group', name='Edit').get_by_role('menuitem', name='Change a published rota…')).to_be_visible()
        expect(menu.get_by_role('group', name='Share').get_by_role('menuitem', name='Change a published rota…')).to_have_count(0)
        # SR-05: one door to the comparison (View settings › Layers), not a second in Rota tools.
        expect(menu.get_by_role('menuitem', name='Compare side by side')).to_have_count(0)
        # SR-11: one entry for paper.
        menu.get_by_role('menuitem', name='Print or export…').click()
        choice = page.get_by_role('dialog', name='Print or export')
        expect(choice.get_by_role('button', name=re.compile('^Preset: '))).to_be_visible()
        expect(choice.get_by_role('button', name='Published sheet…', exact=True)).to_be_visible()
    finally:
        context.close()


def test_published_versions_say_what_changed(browser, live_server, h):
    """SR-35: a version that replaced another offers "What changed in version N", day by day."""
    from .test_browser import planner_tool
    a, b, _ = h.seed['people']
    pid = published_october(h)
    amend = h.workspace('October amendment', '2026-10-04', '2026-10-14', base=pid)
    amend = h.shift(amend, *at(6), people=[b])['workspace']
    h.publish(h.detail(amend)['workspace'])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        show(page, 'schedule/week/2026-10-05?edit=none')
        planner_tool(page, 'Published versions…')
        page.get_by_role('region', name='Published versions').get_by_role('button', name=re.compile(r'^What changed in version 2')).click()
        dialog = page.get_by_role('region', name=re.compile('^What changed'))  # opened within the versions panel (C26)
        expect(dialog.get_by_role('button', name='Back to Published versions')).to_be_visible()
        expect(dialog.locator('tbody tr')).to_have_count(1)
        expect(dialog).to_contain_text('6 Oct')
    finally:
        context.close()
