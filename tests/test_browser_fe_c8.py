"""maintenance (SWEEP-3 findings SW3-A/B/D): regressions for the planning, governance and configuration journeys."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, condition, pick, wait_held, open_rota, open_scheduling, save_dialog, open_config, restart_app  # noqa: F401
from .test_browser_p2_groups import display, person, group

pytestmark = pytest.mark.browser
DESKTOP = {'width': 1440, 'height': 900}


def rows(main):
    return main.locator('tr[data-row], .dt-card')


def test_a_row_menu_closes_when_its_list_redraws(browser, live_server, h):
    """SW3-D-02 as D-057 (0c4604160) keeps it: a ⋯ menu left open while its list draws its rows again stays open on the
    row's redrawn button, its items built again for the record as it is now (never the previous render's); when the
    row is gone, nothing takes its place and the menu closes, never floating over the new rows."""
    for n, name in enumerate(['Alpha group', 'Bravo group']):
        group(h, name, primary_priority=n + 1)
    context, page = sign_in(browser, live_server, 'admin', DESKTOP)
    try:
        navigate(page, 'configuration/scheduling-groups')
        main = page.locator('#main')
        more = rows(main).filter(has_text='Bravo group').get_by_role('button', name='More actions for Bravo group')
        more.click()
        menu = page.get_by_role('menu', name='Row actions')
        expect(menu.get_by_role('menuitem', name='Switch off')).to_be_visible()
        # The list draws its rows again (a live refresh, a save elsewhere): the old ⋯ button is replaced.
        more.evaluate('b => { window.fixtureOldMore = b; b.replaceWith(b.cloneNode(true)); }')
        expect(menu.get_by_role('menuitem', name='Switch off')).to_be_visible()  # kept, on the row's new button
        expect(more).to_have_attribute('aria-expanded', 'true')
        assert page.evaluate('!window.fixtureOldMore.isConnected')
        # The row itself goes (its record left the list): nothing matches, so the menu closes.
        more.evaluate('b => b.closest("[data-row]").remove()')
        expect(menu).to_have_count(0)
    finally:
        context.close()


def test_a_group_who_names_its_tag(browser, live_server, h):
    """SW3-D-04: a group whose Who is a tag reads "Tag is any of #Night team", never the tag's id."""
    tag = h.configure('tags', {'name': 'Night team', 'labels': ['people']})['record']
    group(h, 'Tagged group', membership={'match': 'all', 'conditions': [{'field': 'tags', 'values': [tag['id']]}]})
    context, page = sign_in(browser, live_server, 'admin', DESKTOP)
    try:
        navigate(page, 'configuration/scheduling-groups')
        row = rows(page.locator('#main')).filter(has_text='Tagged group')
        expect(row).to_contain_text('#Night team')
        expect(row).not_to_contain_text(tag['id'])
    finally:
        context.close()


def test_new_group_says_up_front_that_it_needs_rota_coordinators(browser, live_server, h):
    """SW3-D-03 (D41): with no Rota Coordinators the New group dialog says so before it is filled, and the setting
    opens from it; the form keeps what was typed."""
    coordinator, first = display(h, 'coordinator'), person(h)
    context, page = sign_in(browser, live_server, 'admin', DESKTOP)
    try:
        navigate(page, 'configuration/scheduling-groups')
        main = page.locator('#main')
        main.get_by_role('button', name='New scheduling group').first.click()
        dialog = page.get_by_role('dialog', name='New scheduling group')
        expect(dialog.get_by_text('No Rota Coordinators yet: an active group needs them.')).to_be_visible()
        dialog.get_by_label('Name', exact=True).fill('Early group')
        dialog.get_by_role('button', name='Choose Rota Coordinators').click()
        settings = page.get_by_role('dialog', name='Rota Coordinators and private drafts')
        pick(settings, 'Rota Coordinators', [coordinator])
        settings.get_by_label('Reason', exact=True).fill('Coordinator runs the department rota')
        settings.get_by_role('button', name='Save').click()
        expect(settings).to_have_count(0)
        expect(dialog.get_by_text('No Rota Coordinators yet')).to_have_count(0)
        expect(dialog.get_by_label('Name', exact=True)).to_have_value('Early group')
        condition(dialog.locator('.audience-conditions'), 'Person', [first])
        pick(dialog, 'Responsible', [coordinator])
        dialog.get_by_label('Reason', exact=True).fill('Early team scheduler')
        dialog.get_by_role('button', name='Add group').click()
        expect(dialog).to_have_count(0)
        expect(rows(main).filter(has_text='Early group')).to_contain_text('Active')
    finally:
        context.close()


def test_retry_on_a_slow_home_card_asks_again(browser, live_server, h):
    """SW3-B-01, DL-045: a slow read shows no Retry while it is live; at its limit it is aborted and Retry starts a new
    read, never joining one still on its way (which made the button do nothing until the slow answer came)."""
    held, asked = [], []
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        navigate(page, 'requests')
        # Sign-in can finish the first Home read. Restart on Requests so Home opens without a kept reply.
        restart_app(page)
        page.clock.pause_at(page.evaluate("new Date(Date.now()+1000).toISOString()"))
        # The first read is held for the whole test (unrouting would let it answer, and its answer would replace the
        # card, Retry included, before the press); later reads go through.
        page.route('**/api/home', lambda route: held.append(route) if not held else route.continue_())
        page.on('request', lambda r: asked.append(r.url) if r.url.endswith('/api/home') else None)
        page.evaluate("location.hash='home'")
        wait_held(page, held)
        page.clock.run_for(16000)
        needs = page.get_by_role('region', name='Needs you')
        expect(needs.locator('.sk-slow')).to_contain_text('Still loading · 0:1')  # past 15 s: the one quiet line
        expect(needs.get_by_role('button', name='Retry loading what needs you', exact=True)).to_have_count(0)  # never while the read is live
        page.clock.run_for(45000)  # the read's limit (api.ts READ_TIMEOUT): the held read is aborted
        expect(needs.locator('.read-failed')).to_contain_text('Didn’t load in 1:00.')
        with page.expect_response(lambda r: r.url.endswith('/api/home')):  # Retry's own read, the first still held
            needs.get_by_role('button', name='Retry loading what needs you', exact=True).click()
        page.clock.resume()
        expect(needs.locator('.home-needs-list, .home-clear')).to_be_visible()
        assert len(asked) >= 2 and len(held) == 1
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_new_rota_from_an_empty_month_offers_that_month(browser, live_server, h):
    """SW3-A-02: New rota from an empty month in view prefills that month, not the next free period."""
    from datetime import datetime
    from .test_browser_schedule_views import show, view
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        show(page, 'schedule/month/2028-02-01?edit=none')
        view(page, 'Month').locator('.empty-state').get_by_role('button', name='New rota', exact=True).click()
        dialog = page.get_by_role('dialog', name='New rota', exact=True)
        day = lambda label: datetime.strptime(dialog.get_by_label(label, exact=True).input_value(), '%a %d %b %Y').date().isoformat()
        assert (day('First day'), day('Last day')) == ('2028-02-01', '2028-02-29')
    finally:
        context.close()


def test_a_run_that_did_not_start_says_so_and_offers_try_again(browser, live_server, h):
    """SW3-A-03: a refused auto-schedule start reads "The run didn't start" with Try again and Run offered again; the
    failed read after it is not reported as "Showing the last result" (there was none)."""
    workspace = h.shift(h.workspace('Start failure rota'), people=[])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)

    def refuse_create(route):
        if (route.request.post_data_json or {}).get('kind') == 'job.create':
            route.fulfill(status=503, json={'error': {'code': 'CATALOGUE_BUSY', 'message': 'The service is busy. Try again in a moment.'}})
        else:
            route.continue_()
    try:
        open_rota(page, workspace['id'])
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        page.route('**/api/commands', refuse_create)
        page.route('**/api/jobs?*', lambda route: route.fulfill(status=502, body='Bad gateway'))
        panel.get_by_role('button', name='Run auto-schedule', exact=True).click()
        failed = panel.locator('.auto-start-failed')
        expect(failed).to_contain_text("The run didn't start. The service is busy.")
        expect(panel).not_to_contain_text('Showing the last result')
        expect(panel.get_by_role('button', name='Run auto-schedule', exact=True)).to_be_enabled()
        page.unroute('**/api/commands')
        page.unroute('**/api/jobs?*')
        with page.expect_response(lambda r: r.url.endswith('/api/commands') and r.request.method == 'POST'
                                  and (r.request.post_data_json or {}).get('kind') == 'job.create') as created:
            failed.get_by_role('button', name='Try again').click()
        assert created.value.ok
    finally:
        context.close()


def test_submit_says_when_its_checks_could_not_be_read(browser, live_server, h):
    """SW3-B-02: Submit for review whose pre-submit read fails says so with Retry (it used to do nothing at all)."""
    from .test_browser_schedule import show
    w = h.shift(h.workspace('Submit read failure'), people=[h.seed['people'][0]])['workspace']
    h.command('coordinator', 'workspace.validate', {'reason': 'Routine rota check'}, w['id'], w['revision'])
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        show(page, f"schedule/week/{w['starts_on']}?edit={w['id']}")
        page.route('**/fix-list*', lambda route: route.fulfill(status=503, json={'error': {'code': 'UNAVAILABLE', 'message': 'The service is busy.'}}))
        page.get_by_role('button', name='Submit for review…', exact=True).click()
        toast = page.locator('.toast').filter(has_text='Couldn’t load the rota’s checks.')
        expect(toast).to_contain_text('The service is busy.')
        page.unroute('**/fix-list*')
        toast.get_by_role('button', name='Retry').click()
        expect(page.get_by_role('dialog', name=f"Submit {w['name']} for review")).to_be_visible()
    finally:
        context.close()


def test_an_approval_shows_approved_even_when_the_rota_list_is_slow_to_read(browser, live_server, h):
    """SW3-B-03: after Approve the page shows the approved rota (Publish…, no Approve) and says so at once; it no longer
    waits on (or falls back to the pre-approval state over) a failed read of the whole rota list."""
    w = h.lifecycle(h.shift(h.workspace('Approval reply'))['workspace'], 'submit')['workspace']
    context, page = sign_in(browser, live_server, 'approver', DESKTOP)
    try:
        navigate(page, 'planner/' + w['id'])
        page.get_by_role('button', name='Approve rota', exact=True).click()
        form = page.get_by_role('dialog', name='Approve rota', exact=True)
        form.get_by_label('Note', exact=True).fill('Independent review')
        page.route('**/api/workspaces?*', lambda route: route.fulfill(status=502, body='Bad gateway'))
        save_dialog(form, 'Approve rota')
        expect(page.locator('.toast').filter(has_text='Approved · ready to publish')).to_be_visible()
        expect(page.get_by_role('button', name='Publish…').first).to_be_visible()
        expect(page.get_by_role('button', name='Approve rota', exact=True)).to_have_count(0)
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_a_line_for_one_person_names_them_whatever_its_reach_count(browser, live_server, h):
    """SW3-D-16: a person line whose reach count is 0 (counted over its dates) still reads "<name>: at most …", never
    "No one" (which read as a line for nobody)."""
    from urllib.parse import urlparse, parse_qs
    from .test_browser_rule_catalogue import catalogue_rule, flag, line
    from .test_browser_rule_filter_builder import open_line
    flag(h)
    who = {'match': 'all', 'conditions': [{'field': 'person_ids', 'op': 'any', 'values': [h.seed['people'][0]]}]}
    mine = line({'max': {'value': 3, 'level': 'medium'}}, who=who, starts_on='2027-05-01', ends_on='2027-06-01', note='One person')
    rule = catalogue_rule(h, 'duties_per_week', [line({'max': {'value': 5, 'level': 'mandatory'}}, note='Department default'), mine])
    line_id = next(l['id'] for l in rule['definition']['rule']['lines'] if l.get('note') == 'One person')

    def nobody(route):
        rule = parse_qs(urlparse(route.request.url).query).get('policy_id', [''])[0]
        route.fulfill(json={'details': {rule: {'lines': [{'line_id': line_id, 'people': [], 'clinics': []}]}}})
    context, page = sign_in(browser, live_server, 'admin', DESKTOP)
    try:
        page.route(lambda url: '/api/policies/reach' in url and 'line_id=' in url, nobody)
        panel = open_line(page, 'duties_per_week', line_id)
        says = panel.locator('.rc-says')
        expect(says).to_contain_text(person(h) + ': ')
        expect(says).not_to_contain_text('No one')
    finally:
        context.close()


def test_ending_a_permission_shows_ended_at_once(browser, live_server, h):
    """SW3-D-17: End permission marks the row Ended from the command's reply; it no longer waits for (or depends on)
    the page reading the list again."""
    from .governance_support import identity
    identity(h, 'both', ['coordinator', 'approver'])
    h.command('admin', 'self_approval.grant', {'identity_id': h.seed['identities']['both'], 'actions': ['rule.activate'],
              'starts_on': '2030-01-01', 'ends_on': '2030-02-01', 'reason': 'Cover in January'})
    context, page = sign_in(browser, live_server, 'admin', DESKTOP)
    try:
        navigate(page, 'access/routes')
        row = page.get_by_role('row').filter(has_text='Cover in January')
        expect(row).to_contain_text('Starts later')
        page.route('**/api/self-approval', lambda route: None)  # the list read after the change stays unanswered
        page.get_by_role('button', name='More actions for Fictional both', exact=True).first.click()
        page.get_by_role('menuitem', name='End permission', exact=True).click()
        form = page.get_by_role('dialog')
        form.get_by_label('Reason', exact=True).fill('Second approver joined')
        save_dialog(form, 'End permission')
        expect(row).to_contain_text('Ended')
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_audit_rows_name_their_subject_and_do_not_fold_different_ones(browser, live_server, h):
    """SW3-D-22: an audit row names whose permission (the server's target_label); two changes to different subjects
    stay two rows, so neither subject is hidden in "added 2 …"."""
    from .governance_support import identity
    for name in ('first', 'second'):
        identity(h, name, ['approver'])
        # An action approvers can do as their own work (maintenance refuses a grant they could never use).
        h.command('admin', 'self_approval.grant', {'identity_id': h.seed['identities'][name], 'actions': ['request.decide'],
                  'starts_on': '2030-01-01', 'ends_on': '2030-02-01', 'reason': 'Too few approvers'})
    labels = [e['target_label'] for e in h.get('auditor', '/api/audit?action=self_approval')['events'] if e['action'] == 'self_approval.grant']
    assert len(labels) == 2 and all(labels) and labels[0] != labels[1], labels
    context, page = sign_in(browser, live_server, 'auditor', DESKTOP)
    try:
        navigate(page, 'system/audit')
        for label in labels:
            expect(page.locator('.audit-row').filter(has_text=label)).to_have_count(1)
    finally:
        context.close()


def test_ending_an_upcoming_rank_starts_from_its_first_day(browser, live_server, h):
    """SW3-D-06: End on a rank that has not started offers its first day (today was refused: "End must follow start"),
    and ending it on that day saves."""
    from .test_browser_person_profile import person as staff, open_person, section
    p = staff(h)
    rank = next(c for c in h.get('coordinator', '/api/catalogues')['categories'] if c.get('active') is not False)
    h.command('coordinator', 'membership.create', {'person_id': p['id'], 'category_id': rank['id'], 'starts_on': '2030-07-01', 'ends_on': '2030-08-01'})
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        panel = open_person(page, p)
        section(panel, 'Rank').get_by_role('button', name=f"End {rank['name']}").first.click()
        form = page.get_by_role('dialog', name=f"End {rank['name']}")
        expect(form.get_by_label('Last day')).to_have_value('Mon 1 Jul 2030')
        form.get_by_label('Reason').fill('Added by mistake')
        expect(form).to_contain_text('No duties need cover.')
        form.get_by_role('button', name='End', exact=True).click()
        expect(form).to_have_count(0)
        ended = [r for r in h.get('coordinator', '/api/catalogues')['memberships'] if r['person_id'] == p['id'] and r['starts_on'] == '2030-07-01']
        assert any(r['ends_on'] == '2030-07-02' for r in ended), ended
    finally:
        context.close()


def test_withdrawing_an_upcoming_rank_previews_published_and_draft_duties(browser, live_server, h):
    from .test_browser_person_profile import person, open_person, section
    from .test_catalogue_preview import rank_cover_fixture

    ident, _, _ = rank_cover_fixture(h)
    p = person(h)
    cat = h.get('coordinator', '/api/catalogues')
    rank = next(r for r in cat['memberships'] if r['id'] == ident)
    name = next(r['name'] for r in cat['categories'] if r['id'] == rank['category_id'])
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        panel = open_person(page, p)
        section(panel, 'Rank').get_by_role('button', name=f'More for {name}').click()
        page.get_by_role('menuitem', name='Withdraw', exact=True).click()
        dialog = page.get_by_role('dialog', name=f'Withdraw {name}?')
        dialog.get_by_label('Reason').fill('Review the rank change')
        expect(dialog).to_contain_text('1 published duty would need cover')
        expect(dialog).to_contain_text('1 duty in a draft would need cover')
        expect(dialog.locator('.impact-list li')).to_have_count(2)
        before = h.get('coordinator', '/api/catalogues')['memberships']
        assert any(r['id'] == ident for r in before)
        dialog.get_by_role('button', name='Withdraw', exact=True).click()
        expect(dialog).to_have_count(0)
        after = h.get('coordinator', '/api/catalogues')['memberships']
        assert all(r['id'] != ident for r in after)
    finally:
        context.close()


def test_ending_an_eligibility_with_nothing_affected_says_so(browser, live_server, h):
    """SW3-D-27: the End preview says "No duties need cover." when nothing is affected (it showed nothing)."""
    cat = h.get('credentialer', '/api/catalogues')
    e = next(x for x in cat['eligibilities'] if not x.get('ends_on') and not x.get('revoked'))
    context, page = sign_in(browser, live_server, 'credentialer', DESKTOP)
    try:
        page.route('**/api/eligibilities/*/published-impact', lambda route: route.fulfill(json={
            'published_impact': {'count': 0, 'sessions': []}, 'draft_impact': {'count': 0, 'sessions': []}}))
        navigate(page, f"people?open={e['person_id']}")
        page.locator('.dock-panel').get_by_role('button', name=re.compile('^End ')).first.click()
        expect(page.get_by_role('dialog', name=re.compile('^End '))).to_contain_text('No duties need cover.')
    finally:
        context.close()


def test_configuration_histories_share_one_view_with_by_and_restore(browser, live_server, h):
    """SW3-D-07: Notifications › History reads like every configuration history (Version · Saved · By · … · Reason),
    names who saved each version and restores an earlier one as a new version."""
    from .test_browser_pwa import policy
    policy(h)
    policy(h, revision=2, push_enabled=False, reason='Quiet for the pilot')
    admin = h.get('admin', '/api/me')['display_name']
    context, page = sign_in(browser, live_server, 'admin', DESKTOP)
    try:
        navigate(page, 'configuration/notifications')
        page.locator('#main').get_by_role('button', name='History', exact=True).click()
        history = page.get_by_role('dialog', name='Notifications history')
        expect(history.locator('thead')).to_contain_text('VersionSavedBySettingsReason')
        latest = history.locator('tbody tr').first
        expect(latest).to_contain_text(admin)
        expect(latest).to_contain_text('Alerts off')
        on = history.locator('tbody tr').filter(has_text='Department alerts for the test')
        expect(on).to_contain_text('Alerts on')
        on.get_by_role('button', name=re.compile('^Restore version ')).click()
        confirm = page.get_by_role('dialog', name=re.compile('^Restore version '))
        confirm.get_by_label('Reason').fill('Back to alerts on')
        confirm.get_by_role('button', name='Restore', exact=True).click()
        expect(page.locator('.toast').filter(has_text='Earlier notifications restored.')).to_be_visible()
        assert h.get('admin', '/api/admin/notifications/policy')['policy']['push_enabled'] is not False
    finally:
        context.close()


def test_publishing_says_published_before_the_page_reads_again(browser, live_server, h):
    """SW3-B-07: "Published" is said as soon as the server confirms, not after the reads that redraw the page (which
    took minutes under load and left "Approved · Publish…" with no message)."""
    w = h.shift(h.workspace('Publish reply'))['workspace']
    w = h.lifecycle(w, 'submit')['workspace']
    w = h.lifecycle(w, 'approve', 'approver')['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        navigate(page, f"schedule/week/{w['starts_on']}?edit={w['id']}")
        page.get_by_role('button', name='Publish…', exact=True).click()
        dialog = page.get_by_role('dialog', name='Publish Publish reply', exact=True)
        page.route('**/api/workspaces?*', lambda route: None)  # the reads after publishing stay unanswered
        save_dialog(dialog, 'Publish')
        expect(page.locator('.toast').filter(has_text='Published · staff are told.')).to_be_visible()
        assert h.detail(w)['workspace']['status'] == 'published'
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_leaving_unsaved_settings_for_another_section_names_that_section(browser, live_server, h):
    """SW3-D-09: going from unsaved Department settings to Tags, the guard says "…if you go to Tags", not
    "…to Configuration" (the page you are already on)."""
    context, page = sign_in(browser, live_server, 'admin', DESKTOP)
    try:
        open_config(page, 'Department settings')
        page.get_by_label('Department name', exact=True).fill('Fictional unsaved department')
        page.get_by_role('navigation', name='Configuration sections').get_by_role('button', name='Tags', exact=True).click()
        expect(page.get_by_role('dialog', name='Leave this page?')).to_contain_text('Your changes on this page are lost if you go to Tags.')
    finally:
        context.close()


def test_a_date_range_refuses_a_last_day_before_the_first(browser, live_server, h):
    """SW3-D-15: every date range (here New scheduling group's) says on Last day that it cannot be before the first day,
    before anything is sent (Holidays accepted 10 Mar → 5 Mar and went on to its preview)."""
    from .test_planning_groups_drafts import settings
    settings(h)
    sent = []
    context, page = sign_in(browser, live_server, 'admin', DESKTOP)
    try:
        page.on('request', lambda r: sent.append(r) if r.method == 'POST' and r.url.endswith('/api/commands') else None)
        navigate(page, 'configuration/scheduling-groups')
        page.locator('#main').get_by_role('button', name='New scheduling group').first.click()
        dialog = page.get_by_role('dialog', name='New scheduling group')
        dialog.get_by_label('From', exact=True).fill('2027-03-10')
        until = dialog.get_by_label('Until (included)', exact=True)
        until.fill('2027-03-05')
        dialog.get_by_role('button', name='Add group').click()
        assert until.evaluate('i => i.validationMessage').startswith('Choose ')
        assert not sent
    finally:
        context.close()


def test_delegate_reviewers_read_with_role_names(browser, live_server, h):
    """SW3-D-18: the delegate list says "Approver", never the role key ("approver, programme_lead")."""
    from .governance_support import identity, route
    identity(h, 'programme', ['programme_lead'])
    identity(h, 'delegate', ['approver', 'programme_lead'])
    route(h, [{'key': 'programme', 'label': 'Programme review', 'role': 'programme_lead'}])
    context, page = sign_in(browser, live_server, 'programme', DESKTOP)
    try:
        navigate(page, 'access/routes')
        page.get_by_role('button', name='Delegate review step', exact=True).click()
        options = page.get_by_role('dialog', name='Delegate a review step').get_by_label('Delegate reviewer').locator('option')
        expect(options.filter(has_text='Fictional delegate')).to_contain_text('Approver')
        expect(options.filter(has_text='programme_lead')).to_have_count(0)
    finally:
        context.close()


def test_a_failed_save_is_not_carried_to_another_page(browser, live_server, h):
    """SW3-D-19: the footer's "Not saved: …" from a refused change on one page is gone on the next page."""
    context, page = sign_in(browser, live_server, 'admin', DESKTOP)
    try:
        navigate(page, 'access/routes')
        page.route('**/api/commands', lambda route: route.fulfill(status=422, json={'error': {'code': 'SCHEMA_INVALID', 'message': 'Refused for the test.'}}))
        # A change refused on this page (as the delegation's 14-day refusal was): the footer says so here.
        page.evaluate("async()=>{const a=await window.importBundle('api.js');await a.command('review_delegation.create',{}).catch(()=>null);}")
        footer = page.locator('.app-footer')
        expect(footer).to_contain_text('Not saved')
        page.unroute('**/api/commands')
        navigate(page, 'rules')
        expect(footer).not_to_contain_text('Not saved')
    finally:
        context.close()


def test_a_person_row_menu_is_named_by_the_person_alone(browser, live_server, h):
    """SW3-D-25: a person row's ⋯ is "More actions for <name>", not the name followed by every alias and code."""
    p = next(x for x in h.get('admin', '/api/catalogues')['people'] if x['id'] == h.seed['people'][0])
    context, page = sign_in(browser, live_server, 'admin', DESKTOP)
    try:
        navigate(page, 'people')
        expect(page.get_by_role('button', name=f"More actions for {p['display_name']}", exact=True)).to_have_count(1)
    finally:
        context.close()
