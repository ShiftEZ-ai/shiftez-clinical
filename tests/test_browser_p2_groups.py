"""Phase 2 reviewed in the browser: Configuration › Scheduling groups
(versioned rows, membership through the one condition builder, owners, priority, history and restore), private drafts
(create, share one-way, make the rota), group-scoped planning (Fix list sections, Leave room for <group>), the merge
review (maintenance), the combined submit (maintenance) and the approver's grouped review on a phone (maintenance)."""
import json
import os
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from packages.contracts.schemas import ROLE_NAMES
from .test_browser import auto_setup, browser, live_server, sign_in, navigate, condition, pick, expect_loaded, open_scheduling  # noqa: F401
from .test_browser import fix_pill
from .test_browser_operator_role_coverage import all_roles  # noqa: F401
from .test_browser_cp_sat import cp_server  # noqa: F401 (one fixture: server and in-test worker both on CP-SAT)
from .governance_support import identity

pytestmark = pytest.mark.browser
DESKTOP = {'width': 1440, 'height': 900}
PHONE = {'width': 390, 'height': 844}
SHOTS = os.getenv('P2_FE_SHOTS')  # a folder: every screen built here, for the handoff
REASON = 'Synthetic scheduling responsibility decision'


def shot(page, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f'{name}.png'), full_page=True)


def display(h, role):
    ident = h.seed['identities'][role]
    return next(i['display_name'] for i in h.get('admin', '/api/access')['identities'] if i['id'] == ident)


def person(h, n=0):
    return {p['id']: p['display_name'] for p in h.get('coordinator', '/api/catalogues')['people']}[h.seed['people'][n]]


def group(h, name='Consultants', people=None, owner='coordinator', **changes):
    from .test_planning_groups_drafts import settings
    if not h.get('admin', '/api/planning-settings')['definition']['coordinator_identity_ids']:
        settings(h)  # maintenance: an active group needs Rota Coordinators
    definition = {'name': name, 'starts_on': '2020-01-01', 'primary_priority': 10,
                  'membership': {'match': 'all', 'conditions': [{'field': 'person_ids', 'values': people or [h.seed['people'][0]]}]},
                  'owner_identity_ids': [h.seed['identities'][owner]], **changes}
    return h.command('admin', 'planning_group.create', {'definition': definition, 'reason': REASON})['group']


def test_admin_creates_edits_restores_and_switches_off_a_scheduling_group(browser, live_server, h):
    coordinator, first = display(h, 'coordinator'), person(h)
    context, page = sign_in(browser, live_server, 'admin', DESKTOP)
    try:
        navigate(page, 'configuration/scheduling-groups')
        main = page.locator('#main')
        expect(main.get_by_role('heading', name='No scheduling groups yet')).to_be_visible()
        expect(main.get_by_text('None · needed before a group can be active')).to_be_visible()
        shot(page, 'config-groups-empty-desktop-light')
        # Rota Coordinators first (maintenance: an active group needs them): the department's planning settings, versioned.
        settings = main.get_by_role('region', name='Rota Coordinators and private drafts')
        settings.get_by_role('button', name='Edit').click()
        dialog = page.get_by_role('dialog', name='Rota Coordinators and private drafts')
        pick(dialog, 'Rota Coordinators', [coordinator])
        dialog.get_by_label('Private drafts per person').fill('3')
        dialog.get_by_label('Reason', exact=True).fill('Coordinator runs the department rota')
        dialog.get_by_role('button', name='Save').click()
        expect(settings).to_contain_text(coordinator)
        expect(settings).to_contain_text('3')
        assert h.get('admin', '/api/planning-settings')['definition']['coordinator_identity_ids'] == [h.seed['identities']['coordinator']]
        expect(main.get_by_text('None · needed before a group can be active')).to_have_count(0)
        main.get_by_role('button', name='New scheduling group').first.click()
        dialog = page.get_by_role('dialog', name='New scheduling group')
        dialog.get_by_label('Name', exact=True).fill('Consultants')
        who = dialog.locator('.audience-conditions')
        condition(who, 'Person', [first])
        pick(dialog, 'Responsible', [coordinator])
        dialog.get_by_label('Reason', exact=True).fill('Consultant scheduler owns consultants')
        dialog.get_by_role('button', name='Add group').click()
        expect(dialog).to_have_count(0)
        row = main.locator('tr, .dt-card').filter(has_text='Consultants').first
        expect(row).to_contain_text(coordinator)
        expect(row).to_contain_text('Active')
        # Saved for real: a reload reads the same version.
        page.reload()
        expect_loaded(page, 'configuration')
        row = main.locator('tr, .dt-card').filter(has_text='Consultants').first
        expect(row).to_be_visible()
        shot(page, 'config-groups-list-desktop-light')
        row.get_by_role('button', name='Edit Consultants').click()
        dialog = page.get_by_role('dialog', name='Edit Consultants')
        expect(dialog.get_by_label('Priority (lowest wins)')).to_have_value('0')
        dialog.get_by_label('Name', exact=True).fill('Senior consultants')
        dialog.get_by_label('Reason', exact=True).fill('Renamed for the senior team')
        dialog.get_by_role('button', name='Save').click()
        expect(main.locator('tr, .dt-card').filter(has_text='Senior consultants')).to_have_count(1)
        # History lists both versions; restoring the first appends a third with the old name.
        row = main.locator('tr, .dt-card').filter(has_text='Senior consultants').first
        row.get_by_role('button', name='More actions for Senior consultants').click()
        page.get_by_role('menuitem', name='History').click()
        history = page.get_by_role('dialog', name='Senior consultants history')
        expect(history.locator('tbody tr')).to_have_count(2)
        history.get_by_role('button', name='Restore version 1').click()
        restore = page.get_by_role('dialog', name='Restore version 1')
        restore.get_by_label('Reason', exact=True).fill('Back to the first name')
        restore.get_by_role('button', name='Restore').click()
        expect(history).to_have_count(0)
        expect(main.locator('tr, .dt-card').filter(has_text='Consultants').first).not_to_contain_text('Senior')
        assert [v['revision'] for v in h.get('admin', '/api/planning-groups?group_id=' + h.get('admin', '/api/planning-groups')['groups'][0]['group_id'])['groups']] == [3, 2, 1]
        # Switch off: a new version, said on the row.
        row = main.locator('tr, .dt-card').filter(has_text='Consultants').first
        row.get_by_role('button', name='More actions for Consultants').click()
        page.get_by_role('menuitem', name='Switch off').click()
        off = page.get_by_role('dialog', name='Switch off Consultants')
        off.get_by_label('Reason', exact=True).fill('Paused for December')
        off.get_by_role('button', name='Switch off').click()
        expect(main.locator('tr, .dt-card').filter(has_text='Consultants').first).to_contain_text('Inactive')
        shot(page, 'config-groups-settings-desktop-light')
    finally:
        context.close()


@pytest.mark.parametrize('owner', ['coordinator', 'admin'], ids=['owner', 'unrelated'])
def test_a_planner_reads_scheduling_groups_and_a_failed_read_offers_retry(browser, live_server, h, owner):
    group(h, owner=owner, co_editor_identity_ids=[])
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        navigate(page, 'configuration/scheduling-groups')
        main = page.locator('#main')
        row = main.locator('tr, .dt-card').filter(has_text='Consultants').first
        expect(row).to_contain_text(display(h, owner))  # owner names come with the group list (P2-BE)
        expect(main.get_by_role('button', name='New scheduling group')).to_have_count(0)
        edit = row.get_by_role('button', name='Edit Consultants')
        if owner == 'coordinator':
            expect(edit).to_be_disabled()
            holders = page.evaluate("async () => (await window.importBundle('dom.js')).holders('planning.configure')")
            why = f'Only {holders} change scheduling groups.'
            expect(edit).to_have_attribute('aria-describedby', re.compile(r'\S+'))
            reason = page.locator('#' + edit.get_attribute('aria-describedby').split()[-1])
            expect(reason).to_be_visible()
            expect(reason).to_have_text(why)
            expect(edit).to_have_accessible_description(why)
        else:
            expect(edit).to_have_count(0)
        # A failed read says so in its pane, with Retry; the nav stays usable.
        page.route('**/api/planning-groups*', lambda route: route.fulfill(status=503, json={'error': {'code': 'UNAVAILABLE', 'message': 'Service unavailable'}}))
        page.reload()
        expect(main.get_by_text('Couldn’t load scheduling groups.')).to_be_visible()
        page.unroute('**/api/planning-groups*')
        main.get_by_role('button', name='Retry').click()
        expect(main.locator('tr, .dt-card').filter(has_text='Consultants').first).to_be_visible()
    finally:
        context.close()


@pytest.mark.parametrize('role', [r for r in ROLE_NAMES if r not in ('admin', 'coordinator')])
def test_other_roles_have_no_scheduling_groups_section(browser, live_server, all_roles, role):
    context, page = sign_in(browser, live_server, role, DESKTOP)
    try:
        page.evaluate("location.hash='configuration/scheduling-groups'")
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', re.compile(r'^(?!home$).+'))
        expect(page.get_by_role('heading', name='Scheduling groups')).to_have_count(0)
        expect(page.locator('#main')).not_to_contain_text('Rota Coordinators and private drafts')
    finally:
        context.close()


# ---- maintenance · private drafts (maintenance, live) ----

def switcher(page):
    page.get_by_role('button', name=re.compile(r'^(Switch draft · |Choose a draft)')).click()
    picker = page.get_by_role('dialog', name='Drafts', exact=True)
    expect(picker).to_be_visible()
    return picker


def rota_tools(page, item):
    page.get_by_role('button', name='Rota tools').click()
    page.get_by_role('menu', name='Rota tools').get_by_role('menuitem', name=item).click()


def test_a_private_draft_is_created_from_the_rota_shared_one_way_and_view_only_for_others(browser, live_server, h):
    from .test_browser import open_rota
    w = h.shift(h.workspace('December rota'))['workspace']
    identity(h, 'coordinator_two', ['coordinator'])
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        open_rota(page, w['id'])
        picker = switcher(page)
        expect(picker.get_by_role('button', name=re.compile('^New private draft'))).to_contain_text('Copy of December rota')
        shot(page, 'g1-drafts-picker-desktop-light')
        picker.get_by_role('button', name=re.compile('^New private draft')).click()
        bar = page.locator('.planner-bar')
        expect(bar.get_by_role('button', name='Switch draft · December rota · draft 1', exact=True)).to_be_visible()
        expect(bar.locator('.rota-status')).to_have_text('Private draft')
        # A sandbox is never submitted; with no rota designated for the period there is nothing to merge into.
        expect(bar.get_by_role('button', name='Submit for review…')).to_have_count(0)
        mine = next(x for x in h.get('coordinator', '/api/workspaces')['workspaces'] if x['name'] == 'December rota · draft 1')
        assert (mine['visibility'], mine['purpose'], mine['owner_identity_id']) == ('private', 'sandbox', h.seed['identities']['coordinator'])
        page.reload()
        expect(page.locator('.planner-bar .rota-status')).to_have_text('Private draft')
        picker = switcher(page)
        expect(picker.get_by_role('group', name=re.compile(r'^Your private drafts\s*1$')).get_by_role('option', name=re.compile('December rota · draft 1'))).to_contain_text('Private')
        page.keyboard.press('Escape')
        # Share: the audience is said first; one-way, so the button is armed.
        rota_tools(page, 'Share with schedulers…')
        dialog = page.get_by_role('dialog', name='Share December rota · draft 1')
        expect(dialog).to_contain_text('can view it and copy it. Only you can change it.')
        expect(dialog).to_contain_text('can’t be made private again')
        dialog.get_by_role('button', name='Share').click()
        dialog.get_by_role('button', name='Confirm share?').click()
        expect(page.locator('.planner-bar .rota-status')).to_have_text('Shared draft')
        shot(page, 'g1-shared-draft-desktop-light')
        expect(page.get_by_role('button', name='Rota tools')).to_be_visible()
        page.get_by_role('button', name='Rota tools').click()
        expect(page.get_by_role('menuitem', name='Share with schedulers…')).to_have_count(0)
        page.keyboard.press('Escape')
    finally:
        context.close()
    # Another scheduler sees it as a shared draft: view only (no editing tools), copy to change.
    context, page = sign_in(browser, live_server, 'coordinator_two', DESKTOP)
    try:
        open_rota(page, mine['id'])
        bar = page.locator('.planner-bar')
        expect(bar.locator('.rota-status')).to_have_text('Shared draft')
        expect(bar.get_by_role('button', name='Auto-schedule')).to_have_count(0)
        expect(bar.get_by_role('button', name='Undo last edit')).to_have_count(0)
        picker = switcher(page)
        expect(picker.get_by_role('option', name=re.compile('December rota · draft 1'))).to_contain_text('View only')
        expect(picker.get_by_role('button', name=re.compile('^New private draft'))).to_be_visible()
    finally:
        context.close()


def test_private_drafts_are_hidden_from_everyone_else(browser, live_server, h):
    from .test_browser import open_rota
    w = h.shift(h.workspace('December rota'))['workspace']
    private = h.command('coordinator', 'workspace.create', {'name': 'Secret plan', 'starts_on': w['starts_on'], 'ends_on': w['ends_on'],
        'visibility': 'private', 'purpose': 'sandbox', 'source_workspace_id': w['id'], 'source_revision': w['revision'], 'copy_people': True})['workspace']
    identity(h, 'coordinator_two', ['coordinator'])
    for role in ('coordinator_two', 'admin', 'approver', 'auditor'):
        assert 'Secret plan' not in json.dumps(h.get(role, '/api/workspaces'))
    context, page = sign_in(browser, live_server, 'coordinator_two', DESKTOP)
    try:
        open_rota(page, w['id'])
        picker = switcher(page)
        expect(picker.get_by_role('option', name=re.compile('December rota'))).to_be_visible()
        expect(picker.get_by_role('option', name=re.compile('Secret plan'))).to_have_count(0)
        page.keyboard.press('Escape')
        page.evaluate("id => location.hash = 'planner/' + id", private['id'])
        expect(page.get_by_text('That rota is not available. Showing the schedule without it.')).to_be_visible()
    finally:
        context.close()


def test_a_rota_coordinator_makes_a_shared_draft_the_rota(browser, live_server, h):
    from .test_browser import open_rota
    h.command('admin', 'planning.settings', {'definition': {'coordinator_identity_ids': [h.seed['identities']['coordinator']]}, 'reason': REASON})
    w = h.shift(h.workspace('December rota'))['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        open_rota(page, w['id'])
        rota_tools(page, 'Make this the rota…')
        dialog = page.get_by_role('dialog', name='Make December rota the rota')
        dialog.get_by_label('Reason', exact=True).fill('December is planned together')
        dialog.get_by_role('button', name='Make it the rota').click()
        expect(dialog).to_have_count(0)
        picker = switcher(page)
        expect(picker.get_by_role('group', name=re.compile(r'^Rota\s*1$')).get_by_role('option', name=re.compile('^December rota'))).to_be_visible()
        assert h.detail(w)['workspace']['purpose'] == 'integrated'
    finally:
        context.close()


# ---- maintenance · merge a private draft (P2-BE, real API) ----

def real_rota(h, people=None):
    """P2-BE's integrated fixture: a scheduling group owned by the coordinator (the first seed person), the admin as
    configured Rota Coordinator, and one designated session (people as given)."""
    from .test_planning_groups_drafts import integrated
    return integrated(h, people=people if people is not None else [])


def fork(h, rota, role='coordinator'):
    return h.command(role, 'workspace.create', {'name': 'Rota · draft 1', 'starts_on': rota['starts_on'], 'ends_on': rota['ends_on'],
        'visibility': 'private', 'purpose': 'sandbox', 'fork_from': {'workspace_id': rota['id'], 'revision': rota['revision']}})['workspace']


def put(h, workspace, shift, people, role='coordinator'):
    from .test_planning_groups_drafts import edit
    return edit(h, workspace, shift, [p if isinstance(p, dict) else {'person_id': p, 'start': shift['start'], 'end': shift['end']}
                                      for p in people], role=role)['workspace']


def rota_and_private_draft(h):
    rota, _, _ = real_rota(h)
    return rota, fork(h, rota)


def run_jobs(h):
    """The worker takes every queued job: the merge the panel queued (PERF-5) and any check an edit queued."""
    from apps.worker.main import process_one
    while process_one(h.app.state.engine):
        pass


def worked(where, h):
    """A merge request is a worker job: the panel waits for it (Waiting to start), then the worker runs it."""
    expect(where).to_contain_text('Waiting to start')
    run_jobs(h)


def open_merge(page, draft, h):
    from .test_browser import open_rota
    run_jobs(h)  # the checks the fixture's edits queued, so the merge is the job the panel waits for
    open_rota(page, draft['id'])
    bar = page.locator('.planner-bar')
    expect(bar.locator('.rota-status')).to_have_text('Private draft')
    expect(bar.get_by_role('button', name='Submit for review…')).to_have_count(0)
    bar.get_by_role('button', name='Merge into rota…').click()
    panel = page.locator('.dock-panel.merge-panel')
    worked(panel, h)
    return panel


def merge(panel, h, name):
    panel.get_by_role('button', name=name).click()
    worked(panel.locator('.merge-foot'), h)


def test_merging_a_private_draft_brings_back_my_groups_change_and_leaves_anothers(browser, live_server, h):
    mine, other = h.seed['people'][:2]
    rota, _, _ = real_rota(h)
    draft = fork(h, rota)
    draft = put(h, draft, h.detail(draft)['shifts'][0], [mine, other])
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        panel = open_merge(page, draft, h)
        expect(panel).to_contain_text(f'{draft["name"]} → {rota["name"]}')
        expect(panel.get_by_role('status').first).to_contain_text('1 of 2 changes fit · the rest need a look')
        look = panel.get_by_role('region', name='Need a look')
        expect(look.locator('li')).to_have_count(1)
        expect(look.locator('li .badge.neutral')).to_have_text('Not merged')  # another group's person stays in the draft
        expect(panel.get_by_role('region', name='Changes that fit').locator('li')).to_have_count(1)
        expect(panel.get_by_role('region', name='After merging')).to_contain_text('Must fix')
        expect(panel).to_contain_text('Merging shares this draft and its history with department planners')
        shot(page, 'g3-merge-real-desktop-light')
        merge(panel, h, 'Merge 1 change')
        expect(page.get_by_text(f'1 change merged into {rota["name"]}.')).to_be_visible()
        assert [p['person_id'] for p in h.detail(rota)['shifts'][0]['participants']] == [mine]
        archived = h.get('coordinator', '/api/workspaces/' + draft['id'])['workspace']
        assert archived['visibility'] == 'shared' and archived['archived_at']
        expect(page.locator('.planner-bar').get_by_role('button', name=f'Switch draft · {rota["name"]}', exact=True)).to_be_visible()
    finally:
        context.close()


def test_a_both_changed_conflict_takes_yours_only_when_chosen(browser, live_server, h):
    mine = h.seed['people'][0]
    rota, shift, _ = real_rota(h)
    draft = fork(h, rota)
    draft = put(h, draft, h.detail(draft)['shifts'][0], [mine])
    rota = put(h, rota, shift, [{'person_id': mine, 'start': shift['start'], 'end': shift['end'], 'team_lead': True}], role='admin')
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        panel = open_merge(page, draft, h)
        choice = panel.get_by_role('region', name='Need a look').get_by_role('radiogroup')
        expect(choice.get_by_role('radio', name='Rota’s')).to_have_attribute('aria-checked', 'true')
        expect(panel.get_by_role('button', name='Merge 0 changes')).to_be_disabled()
        choice.get_by_role('radio', name='Yours').click()
        merge(panel, h, 'Merge 1 change')
        expect(page.get_by_text(f'1 change merged into {rota["name"]}.')).to_be_visible()
        assert not h.detail(rota)['shifts'][0]['participants'][0].get('team_lead')
    finally:
        context.close()


def test_a_merge_whose_rota_changed_while_it_waited_fails_closed_and_keeps_the_draft_and_choices(browser, live_server, h):
    """The rota changes after the merge is queued: the worker refuses it, nothing is merged, and a new preview keeps
    the conflict's choice."""
    mine = h.seed['people'][0]
    rota, shift, _ = real_rota(h)
    draft = fork(h, rota)
    draft = put(h, draft, h.detail(draft)['shifts'][0], [mine])
    rota = put(h, rota, shift, [{'person_id': mine, 'start': shift['start'], 'end': shift['end'], 'team_lead': True}], role='admin')
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        panel = open_merge(page, draft, h)
        yours = panel.get_by_role('region', name='Need a look').get_by_role('radio', name='Yours')
        yours.click()
        panel.get_by_role('button', name='Merge 1 change').click()
        expect(panel.locator('.merge-foot')).to_contain_text('Waiting to start')
        h.shift(rota, start=shift['start'], end=shift['end'], people=[], role='admin')  # the target moves on meanwhile
        run_jobs(h)
        failed = panel.locator('.merge-error')
        expect(failed).to_contain_text('The draft or the rota changed meanwhile. Preview the merge again.')
        expect(yours).to_have_attribute('aria-checked', 'true')
        expect(panel.get_by_role('button', name='Merge 1 change')).to_be_enabled()
        shot(page, 'g3-merge-failed-desktop-light')
        kept_shift = next(x for x in h.detail(rota)['shifts'] if x['id'] == shift['id'])
        assert [(p['person_id'], p.get('team_lead')) for p in kept_shift['participants']] == [(mine, True)]  # nothing merged
        kept = h.get('coordinator', '/api/workspaces/' + draft['id'])['workspace']
        assert kept['visibility'] == 'private' and not kept['archived_at']
        failed.get_by_role('button', name='Preview again').click()
        worked(panel, h)
        expect(panel.get_by_role('region', name='Need a look').get_by_role('radio', name='Yours')).to_have_attribute('aria-checked', 'true')
        expect(panel.get_by_role('button', name='Merge 1 change')).to_be_enabled()
    finally:
        context.close()


def test_a_waiting_merge_preview_can_be_cancelled(browser, live_server, h):
    rota, draft = rota_and_private_draft(h)
    draft = put(h, draft, h.detail(draft)['shifts'][0], [h.seed['people'][0]])
    run_jobs(h)
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        from .test_browser import open_rota
        open_rota(page, draft['id'])
        page.locator('.planner-bar').get_by_role('button', name='Merge into rota…').click()
        panel = page.locator('.dock-panel.merge-panel')
        expect(panel.get_by_role('status')).to_contain_text('Waiting to start')
        panel.get_by_role('button', name='Cancel: waiting to start').click()
        expect(panel.locator('.notice')).to_contain_text('The merge was cancelled. Nothing changed.')
        assert not h.detail(rota)['shifts'][0]['participants']
        panel.get_by_role('button', name='Try again').click()
        worked(panel, h)
        expect(panel.get_by_role('button', name='Merge 1 change')).to_be_enabled()
    finally:
        context.close()


def test_a_merge_whose_scope_changed_says_so_and_keeps_the_review(browser, live_server, h):
    from .test_planning_groups_drafts import group as add_group
    rota, _, _ = real_rota(h)
    draft = fork(h, rota)
    draft = put(h, draft, h.detail(draft)['shifts'][0], [h.seed['people'][0]])
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        panel = open_merge(page, draft, h)
        merge = panel.get_by_role('button', name='Merge 1 change')
        expect(merge).to_be_enabled()
        add_group(h, people=[h.seed['people'][1]], primary_priority=20)  # group versions changed: the preview is stale
        merge.click()
        # Refused when queued, or failed closed by the worker: said where the button is either way.
        expect(panel.locator('.merge-foot .long-work, .merge-error')).to_be_visible()
        run_jobs(h)
        expect(panel.locator('.merge-error .notice')).to_be_visible()
        expect(merge).to_be_enabled()
        assert h.detail(rota)['shifts'][0]['participants'] == []
    finally:
        context.close()


def test_a_merge_that_would_add_a_must_fix_cannot_be_applied(browser, live_server, h):
    """The one merge state still answered in the contract's shape: a merge that would add or worsen a must-fix
    (can_apply false) needs a contrived worsening; the server's own check is tests/test_p2_groups.py."""
    from .test_browser import route_fetch
    rota, draft = rota_and_private_draft(h)
    draft = put(h, draft, h.detail(draft)['shifts'][0], [h.seed['people'][0]])
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        def worse(route):  # the preview job's result (PERF-5), read once it succeeded
            body = route_fetch(route).json()
            if body['job']['kind'] != 'merge_preview':
                return route.fulfill(json=body)
            merge = body['job']['result']['merge']
            body['job']['result']['merge'] = {**merge, 'can_apply': False, 'new_or_worse_findings': [{'message': 'Staff 01(test) goes over the weekend cap'}]}
            route.fulfill(json=body)
        page.route(re.compile(r'/api/jobs/[^/?]+$'), worse)
        panel = open_merge(page, draft, h)
        expect(panel.get_by_role('status').first).to_contain_text('Merging would add or worsen a must-fix cause')
        expect(panel).to_contain_text('Staff 01(test) goes over the weekend cap')
        expect(panel.get_by_role('button', name='Merge 1 change')).to_be_disabled()
    finally:
        context.close()


# ---- maintenance · my group on the rota; maintenance submit; maintenance approve (P2-BE, real API) ----

def two_groups(h):
    """The coordinator's group (first seed person) and the admin's (second), with the admin's change attributed."""
    from .test_planning_groups_drafts import group as add_group
    rota, shift, mine = real_rota(h)
    other = add_group(h, people=[h.seed['people'][1]], owner='admin', name='Residents')
    rota = put(h, rota, shift, [h.seed['people'][1]], role='admin')  # their attributed change…
    rota = put(h, rota, h.detail(rota)['shifts'][0], [], role='admin')  # …which leaves the place open again (a cause)
    assert h.get('coordinator', f'/api/workspaces/{rota["id"]}/fix-list')['causes']
    return rota, mine, other


def test_my_group_scopes_the_bar_footer_and_fix_list_by_the_servers_attribution(browser, live_server, h):
    from .test_browser import open_rota
    rota, mine, other = two_groups(h)
    context_ = h.get('coordinator', f'/api/workspaces/{rota["id"]}/fix-list')
    planning, causes = context_['planning_context'], context_['causes']
    own = next(g for g in planning['groups'] if g['group_id'] == mine['group_id'])
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        open_rota(page, rota['id'])
        bar = page.locator('.planner-bar')
        expect(bar.get_by_role('button', name=f'Scheduling group: {mine["definition"]["name"]}')).to_be_visible()
        expect(page.locator('.footer-group')).to_have_text(mine['definition']['name'])
        if own['must']:
            expect(fix_pill(page, 'err')).to_contain_text(f'{own["must"]} must fix')
        shot(page, 'g2-group-planner-real-desktop-light')
        # The Fix list reads the server's attribution: one section per classification it sent; other groups read only.
        navigate(page, f'schedule/issues?draft={rota["id"]}')
        main = page.locator('#main')
        expect(main.get_by_role('button', name=re.compile('^Group: Scheduling group'))).to_be_visible()
        names = {'mine': 'Mine to fix', 'unassigned': 'No group'}
        for c in causes:
            k = c['attribution']['classification']
            if k in names:
                expect(main.get_by_role('region', name=names[k])).to_contain_text(c['sentence'])
        others = main.get_by_role('region', name='Other groups')
        residents = others.locator(f'[data-group="{other["group_id"]}"]')
        expect(residents).to_contain_text('Residents')
        theirs = next(g for g in planning['groups'] if g['group_id'] == other['group_id'])
        expect(residents).to_contain_text(theirs['owner_name'])
        expect(residents).to_contain_text(f'by {theirs["last_change"]["by_name"]}')  # their last change, quietly
        expect(others.locator('.fix-row > button.small')).to_have_count(0)
        shot(page, 'g2-fix-list-real-desktop-light')
        # The basic scheduler runs a group alone: no Leave room, and the run says so (lookahead none).
        open_rota(page, rota['id'])
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        auto_setup(panel)  # D76: the setup's fields open under Change
        expect(panel.get_by_label(re.compile('^Leave room for'))).to_have_count(0)
        payload = run_auto(page, panel)
        assert payload['planning_group_version_id'] == own['group_version_id'] and payload['lookahead'] == 'none'
        cancel_runs(h, rota)
    finally:
        context.close()


def run_auto(page, panel):
    with page.expect_request(lambda r: r.url.endswith('/api/commands') and '"job.create"' in (r.post_data or '')) as request:
        panel.get_by_role('button', name='Run auto-schedule', exact=True).click()
    expect(panel.get_by_role('progressbar', name='Auto-schedule progress')).to_be_visible()  # the server took it
    return json.loads(request.value.post_data)['payload']


def cancel_runs(h, rota):
    for job in h.get('coordinator', '/api/jobs?workspace_id=' + rota['id'])['jobs']:
        if job['status'] in ('queued', 'running'):
            h.command('coordinator', 'job.cancel', {'reason': REASON}, job['id'], job['revision'])


@pytest.mark.parametrize('default', ['coupled', 'none'])
def test_leave_room_follows_the_department_default_and_is_sent_with_the_group_run(browser, cp_server, h, default):
    from .test_browser import open_rota
    rota, mine, _ = two_groups(h)
    setting = h.get('admin', '/api/planning-settings')['settings']
    h.command('admin', 'planning.settings', {'definition': {**setting['definition'], 'group_lookahead': default}, 'reason': REASON}, revision=setting['revision'])
    own = next(g for g in h.get('coordinator', f'/api/workspaces/{rota["id"]}/fix-list')['planning_context']['groups'] if g['group_id'] == mine['group_id'])
    context, page = sign_in(browser, cp_server, 'coordinator', DESKTOP)
    try:
        open_rota(page, rota['id'])
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        auto_setup(panel)  # D76: the setup's fields open under Change
        leave = panel.get_by_label('Leave room for Residents')
        if default == 'coupled':
            expect(leave).to_be_checked()
        else:
            expect(leave).not_to_be_checked()
            leave.check()
        shot(page, f'g2-autoschedule-real-{default}-desktop-light')
        payload = run_auto(page, panel)
        assert payload['planning_group_version_id'] == own['group_version_id'] and payload['lookahead'] == 'coupled'
        cancel_runs(h, rota)
    finally:
        context.close()


def adjusted_rota(h):
    """A designated rota whose coordinator group has a rota-only adjustment with its justification (maintenance + P2-BE)."""
    from .test_be5_governance import binding_fixture
    from .test_be5_rota_adjustments import scoped_payload
    from .test_planning_groups_drafts import group as add_group, settings as configure
    binding, policy, rota = binding_fixture(h)
    add_group(h)
    configure(h)
    rota = h.command('admin', 'workspace.designate', {'reason': REASON}, rota['id'], rota['revision'])['workspace']
    return h.command('coordinator', 'workspace.rule_adjust', scoped_payload(policy, binding, h.seed['people'][0]), rota['id'], rota['revision'])['workspace']


def test_submit_lists_each_groups_work_with_its_justification(browser, live_server, h):
    from .test_browser import open_rota
    rota = adjusted_rota(h)
    planning = h.get('coordinator', f'/api/workspaces/{rota["id"]}/fix-list')['planning_context']
    group = planning['groups'][0]
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        open_rota(page, rota['id'])
        page.locator('.planner-bar').get_by_role('button', name='Submit for review…').click()
        dialog = page.get_by_role('dialog', name=re.compile('^Submit .* for review$'))
        section = dialog.get_by_role('region', name=group['name'])
        expect(section.locator(f'[data-group="{group["group_id"]}"]')).to_contain_text(f'{group["filled"]} of {group["total"]} places filled')  # SW3-A-25: labelled
        expect(section).to_contain_text('“Agreed workload for this rota only”')
        expect(section.locator('.fix-quoted figcaption')).to_contain_text(re.compile(r'\S'))  # its author and date
        shot(page, 'g4-submit-real-desktop-light')
        dialog.get_by_role('button', name='Submit for review').click()
        expect(dialog).to_have_count(0)
        assert h.detail(rota)['workspace']['status'] == 'submitted'
    finally:
        context.close()


@pytest.mark.parametrize('viewport', [PHONE, DESKTOP], ids=['phone', 'desktop'])
def test_the_approver_reviews_by_group_on_a_phone_and_a_rejection_returns_the_rota(browser, live_server, h, viewport):
    rota = h.lifecycle(adjusted_rota(h), 'submit')['workspace']
    context, page = sign_in(browser, live_server, 'approver', viewport)
    try:
        navigate(page, 'home')
        page.locator('#main').get_by_role('button', name=re.compile('^Review')).first.click()
        dialog = page.get_by_role('dialog', name='Approve rota')
        expect(dialog).to_be_visible()
        group = h.get('approver', f'/api/workspaces/{rota["id"]}')['planning_context']['groups'][0]
        section = dialog.get_by_role('region', name=group['name'])
        expect(section).to_contain_text('“Agreed workload for this rota only”')
        section.get_by_role('group', name=re.compile('^Decision')).get_by_role('button', name='Reject').click()
        section.get_by_label('Why reject').fill('Keep the weekly maximum for this rota')
        submit = dialog.get_by_role('button', name='Return to coordinator')
        expect(submit).to_be_enabled()
        shot(page, f'g5-approver-real-{"phone" if viewport is PHONE else "desktop"}-light')
        submit.click()
        dialog.get_by_role('button', name='Confirm decision?').click()
        expect(dialog).to_have_count(0)
        returned = h.detail(rota)['workspace']
        assert returned['status'] == 'draft' and not returned['rule_adjustments']  # the whole rota comes back (D24)
    finally:
        context.close()
