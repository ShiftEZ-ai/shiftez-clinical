"""Exact manual plans through checked fill-options and real fingerprint-bound writes.
No test in this module substitutes a validation result or command acknowledgement.
"""
import re

import pytest
from playwright.sync_api import expect

from packages.contracts.schemas import ParticipantInput
from .browser_acceptance_support import shot
from .test_browser import browser, live_server, sign_in, navigate, pick_view, route_fetch, wait_held, planner_filter, filter_pick, close_popover  # noqa: F401
from .test_fill_options import options
from .test_manual_supervision_staffing import world, command_for, START, END

pytestmark = pytest.mark.browser


@pytest.fixture
def manual_supervision_case(h, request):
    """A real open place with retained metadata and an unrelated, off-day assignment.

    Indirect parameters also prepare none/multiple/restricted cases for later picker
    and blocker journeys. Setup always sends the full canonical ParticipantInput roster.
    """
    scenario = getattr(request, 'param', 'single')
    assert scenario in ('single', 'multiple', 'none', 'restricted')
    w, _, shift, trainee, supervisor, other = world(h)
    shift['participants'][0].update(fixed=True, team_lead=True, participation='supervisor')
    if scenario in ('single', 'restricted'):
        retained = {'person_id': other, 'start': START, 'end': END, 'fixed': True,
            'participation': 'trainee', 'overtime': True, 'supervisor_person_id': supervisor,
            'segments': [{'start': START, 'end': '2026-10-04T09:00:00+03:00'},
                         {'start': '2026-10-04T10:00:00+03:00', 'end': END}]}
    else:
        retained = {'person_id': other, 'start': START, 'end': END, 'fixed': True}
    command = command_for(w, shift, retained)
    if scenario == 'none':
        command['payload']['participants'] = []
    w = h.command('coordinator', 'shift.update', command['payload'], revision=w['revision'])['workspace']
    if scenario == 'multiple':
        h.policy({'kind': 'minimum_cover', 'minimum': 3}, {'assignment_type_id': shift['assignment_type_id']})
    rule = h.policy({'kind': 'restriction'}, {'person_id': trainee}) if scenario == 'restricted' else None
    w = h.shift(w, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', people=[other])['workspace']
    w = h.lifecycle(w, 'validate')['workspace']
    before = h.detail(w)
    shift = next(s for s in before['shifts'] if s['id'] == shift['id'])
    row = next(r for r in options(h, w, shift['id'])['candidates'] if r['person_id'] == trainee)
    plan = row['supervision']
    if scenario == 'multiple':
        assert {c['supervisor_person_id'] for c in plan['choices']} == {supervisor, other}
        assert plan['choice_required'] and plan['proposal'] is None and not row['fills']
    elif scenario == 'none':
        assert not plan['choices'] and plan['proposal'] is None and row['status'] == 'blocked'
    else:
        assert plan['proposal']['supervisor_person_id'] == supervisor
        assert (row['status'] == 'blocked') == (scenario == 'restricted')
        assert row['fills'] == (scenario == 'single')
        if rule:
            assert any(f['rule_id'] == rule['id'] for f in row['findings'])
    assert h.detail(w) == before, 'An offered plan must not write any assignment or provenance'
    return {'workspace': w, 'shift': shift, 'trainee': trainee, 'supervisor': supervisor,
            'other': other, 'candidate': row, 'before': before, 'rule': rule}


def open_manual_week(page, w):
    """Choose the calendar Week grid explicitly, including after reload and saved view restoration."""
    navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
    pick_view(page, 'Week')


def open_manual_place(page, w, shift):
    """Fill the target session on the Week grid; the other duty remains independently retained."""
    open_manual_week(page, w)
    with page.expect_response(lambda r: r.url.endswith('/api/planner/fill-options')) as offered:
        enter_manual_fill(page, shift)
    assert offered.value.ok, offered.value.text()
    return page.locator('.wk-panel'), offered.value.json()


def enter_manual_fill(page, shift):
    """A full session's lead/skill gap opens staffing details before the ordinary add-person action."""
    gap = page.locator(f"button.pchip.gap[data-shift='{shift['id']}']")
    expect(gap).to_be_visible()
    role_gap = 'role-gap' in (gap.get_attribute('class') or '').split()
    gap.click()
    if role_gap:
        page.locator('.wk-panel').get_by_role('button', name='Add another person anyway', exact=True).click()


def assert_retained_roster(h, case):
    """Compare entire records: IDs, attendance, pairing, fixed/lead/OT and server provenance."""
    after = h.detail(case['workspace'])
    for original in case['before']['shifts']:
        current = next(s for s in after['shifts'] if s['id'] == original['id'])
        if original['id'] != case['shift']['id']:
            assert current == original
        else:
            assert [p for p in current['participants'] if p['person_id'] != case['trainee']] == original['participants']
    return after


def participant_input(row):
    return ParticipantInput.model_validate({k: v for k, v in row.items() if k in ParticipantInput.model_fields})


@pytest.mark.parametrize('role,scheme', [('coordinator', 'light'), ('admin', 'dark')])
def test_single_named_plan_assign_preview_save_reload_preserves_every_other_record(
        browser, live_server, h, manual_supervision_case, role, scheme):
    case = manual_supervision_case
    w, shift = case['workspace'], case['shift']
    context, page = sign_in(browser, live_server, role,
                            viewport={'width': 1440, 'height': 1000}, color_scheme=scheme)
    try:
        panel, result = open_manual_place(page, w, shift)
        row = next(r for r in result['candidates'] if r['person_id'] == case['trainee'])
        proposal = participant_input(row['supervision']['proposal'])
        assert proposal == participant_input(case['candidate']['supervision']['proposal'])
        choice, = [c for c in row['supervision']['choices'] if c['supervisor_person_id'] == case['supervisor']]
        card = panel.locator('.wk-cand').filter(has=page.get_by_text(row['display_name'], exact=True))
        expect(card.get_by_text('With ' + choice['display_name'], exact=True)).to_be_visible()
        assert h.detail(w) == case['before']
        shot(page, f'manual-supervision-offered-1440-{scheme}', full_page=False)
        panel.get_by_role('tab', name='Why', exact=True).click()
        why_row = panel.locator('.why-row').filter(has_text=row['display_name'])
        expect(why_row).to_contain_text('With ' + choice['display_name'])
        panel.get_by_role('tab', name='Session', exact=True).click()
        with page.expect_response(lambda r: r.url.endswith('/api/commands')
                and r.request.post_data_json.get('kind') == 'shift.update') as saved:
            card.get_by_role('button', name=re.compile(r'^Assign (?!.* with a warning$)')).click()
        command = saved.value.request.post_data_json
        assert command['payload']['shift_id'] == shift['id']
        assert command['payload']['workspace_id'] == w['id']
        assert command['expected_revision'] == w['revision']
        assert command['expected_fingerprint'] == result['source_fingerprint']
        inputs = command['payload']['participants']
        assert {p['person_id'] for p in inputs} == {case['trainee'], case['supervisor'], case['other']}
        assert participant_input(next(p for p in inputs if p['person_id'] == case['trainee'])) == proposal
        for original in shift['participants']:
            actual = next(p for p in inputs if p['person_id'] == original['person_id'])
            assert participant_input(actual) == participant_input(original)
        assert saved.value.ok, saved.value.text()
        assert saved.value.json()['validation']['valid']
        after = assert_retained_roster(h, case)
        target = next(s for s in after['shifts'] if s['id'] == shift['id'])
        assigned, = [p for p in target['participants'] if p['person_id'] == case['trainee']]
        assert participant_input(assigned) == proposal
        page.reload()
        open_manual_week(page, w)
        expect(page.locator(f"button.pchip[data-shift='{shift['id']}'][data-person='{case['trainee']}']")).to_have_attribute(
            'aria-label', re.compile('with ' + re.escape(choice['display_name'])))
        assert h.detail(w) == after
        shot(page, f'manual-supervision-saved-1440-{scheme}', full_page=False)
    finally:
        context.close()


@pytest.mark.parametrize('change', ['absence', 'revision'])
def test_named_plan_ordinary_save_rejects_real_input_or_revision_race(
        browser, live_server, h, manual_supervision_case, change):
    case = manual_supervision_case
    w = case['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 1000})
    intercepted = []

    def change_before_write(route):
        if route.request.post_data_json.get('kind') == 'shift.update':
            intercepted.append(route.request.post_data_json)
            if change == 'absence':
                h.command('coordinator', 'availability.record', {'person_id': case['trainee'],
                    'kind': 'actual_absence', 'start': START, 'end': END,
                    'private_reason': 'Fictional change after the real preview'})
            else:
                h.command('coordinator', 'shift.action', {'workspace_id': w['id'],
                    'shift_ids': [case['shift']['id']], 'action': 'lock', 'reason': 'Fictional concurrent edit'},
                    revision=w['revision'])
        route.continue_()  # The real server decides; never fabricate a conflict or acknowledgement.

    try:
        panel, result = open_manual_place(page, w, case['shift'])
        row = next(r for r in result['candidates'] if r['person_id'] == case['trainee'])
        card = panel.locator('.wk-cand').filter(has=page.get_by_text(row['display_name'], exact=True))
        page.route('**/api/commands', change_before_write)
        with page.expect_response(lambda r: r.url.endswith('/api/commands')
                and r.request.post_data_json.get('kind') == 'shift.update') as refused:
            card.get_by_role('button', name=re.compile(r'^Assign (?!.* with a warning$)')).click()
        assert intercepted and intercepted[0]['expected_fingerprint'] == result['source_fingerprint']
        assert refused.value.status == 409, refused.value.text()
        # The fingerprint guard runs before the revision guard for a fingerprint-bound write.
        assert refused.value.json()['error']['code'] == 'STALE_PREVIEW'
        after = assert_retained_roster(h, case)
        assert all(p['person_id'] != case['trainee'] for s in after['shifts'] for p in s['participants'])
        expect(page.locator('.toast').filter(has_text=refused.value.json()['error']['message'])).to_be_visible()
        if change == 'revision':
            rejected = h.command('coordinator', 'shift.update', intercepted[0]['payload'],
                revision=w['revision'], expected_fingerprint=after['current_fingerprint'], status=409)
            assert rejected['error']['code'] == 'STALE_REVISION'
            assert h.detail(w) == after
    finally:
        page.unroute('**/api/commands', change_before_write)
        context.close()


def trainee_card(page, panel, row):
    panel.get_by_role('searchbox', name='Find a person', exact=True).fill(row['display_name'])
    return panel.locator('.wk-cand, .wk-blocked li').filter(has=page.get_by_text(row['display_name'], exact=True))


def save_checked(page, h, case, card, result, before_reload=None):
    row = next(r for r in result['candidates'] if r['person_id'] == case['trainee'])
    with page.expect_response(lambda r: r.url.endswith('/api/commands')
            and r.request.post_data_json.get('kind') == 'shift.update') as saved:
        card.get_by_role('button', name=re.compile(r'^Assign (?!.* with a warning$)')).click()
    assert saved.value.ok, saved.value.text()
    cmd = saved.value.request.post_data_json
    assert cmd['expected_revision'] == case['workspace']['revision']
    assert cmd['expected_fingerprint'] == result['source_fingerprint']
    assert participant_input(next(p for p in cmd['payload']['participants'] if p['person_id'] == case['trainee'])) == participant_input(row['supervision']['proposal'])
    for original in case['shift']['participants']:
        assert participant_input(next(p for p in cmd['payload']['participants'] if p['person_id'] == original['person_id'])) == participant_input(original)
    after = assert_retained_roster(h, case)
    if before_reload:
        before_reload()
    page.reload()
    open_manual_week(page, case['workspace'])
    assert h.detail(case['workspace']) == after
    return after


@pytest.mark.parametrize('manual_supervision_case', ['multiple'], indirect=True)
def test_multiple_supervisors_require_choice_then_save_exact_plan(browser, live_server, h, manual_supervision_case):
    case = manual_supervision_case
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        panel, result = open_manual_place(page, case['workspace'], case['shift'])
        row = next(r for r in result['candidates'] if r['person_id'] == case['trainee'])
        card = trainee_card(page, panel, row)
        expect(card.get_by_role('button', name=re.compile('Assign|Break rule'))).to_have_count(0)
        choice = row['supervision']['choices'][-1]
        with page.expect_response(lambda r: r.url.endswith('/api/planner/fill-options')) as fresh:
            card.get_by_label('Who supervises ' + row['display_name'], exact=True).select_option(choice['supervisor_person_id'])
        assert fresh.value.ok, fresh.value.text()
        assert fresh.value.request.post_data_json['supervisor_person_id'] == choice['supervisor_person_id']
        assert fresh.value.request.post_data_json['expected_revision'] == case['workspace']['revision']
        expect(card.get_by_text('With ' + choice['display_name'], exact=True)).to_be_visible()
        save_checked(page, h, case, card, fresh.value.json())
        shot(page, 'manual-multiple-saved-1440-light', full_page=False)
    finally:
        context.close()


@pytest.mark.parametrize('manual_supervision_case', ['multiple'], indirect=True)
def test_late_supervisor_reply_cannot_replace_newer_selection(browser, live_server, h, manual_supervision_case):
    case = manual_supervision_case
    context, page = sign_in(browser, live_server, 'coordinator', color_scheme='dark')
    held = []
    def hold(route):
        if route.request.post_data_json.get('supervisor_person_id') == case['supervisor']:
            held.append((route, route_fetch(route)))
        else:
            route.continue_()
    try:
        panel, result = open_manual_place(page, case['workspace'], case['shift'])
        row = next(r for r in result['candidates'] if r['person_id'] == case['trainee'])
        page.route('**/api/planner/fill-options', hold)
        trainee_card(page, panel, row).get_by_label('Who supervises ' + row['display_name'], exact=True).select_option(case['supervisor'])
        wait_held(page, held)
        page.mouse.move(0, 0)
        expect(page.get_by_role('tooltip')).to_have_count(0)
        page.keyboard.press('Escape')
        expect(panel).to_have_count(0)
        enter_manual_fill(page, case['shift'])
        card = trainee_card(page, panel, row)
        with page.expect_response(lambda r: r.url.endswith('/api/planner/fill-options')
                and r.request.post_data_json.get('supervisor_person_id') == case['other']) as fresh:
            card.get_by_label('Who supervises ' + row['display_name'], exact=True).select_option(case['other'])
        assert fresh.value.ok
        chosen = next(c for c in row['supervision']['choices'] if c['supervisor_person_id'] == case['other'])
        expect(card.get_by_text('With ' + chosen['display_name'], exact=True)).to_be_visible()
        route, response = held.pop()
        with page.expect_response(lambda r: r.url.endswith('/api/planner/fill-options')
                and r.request.post_data_json.get('supervisor_person_id') == case['supervisor']):
            route.fulfill(response=response)
        expect(card.get_by_text('With ' + chosen['display_name'], exact=True)).to_be_visible()
        save_checked(page, h, case, card, fresh.value.json())
    finally:
        for route, _ in held:
            route.abort()
        context.close()


@pytest.mark.parametrize('manual_supervision_case', ['none'], indirect=True)
def test_no_qualified_supervisor_cannot_assign_or_bypass(browser, live_server, h, manual_supervision_case):
    case = manual_supervision_case
    context, page = sign_in(browser, live_server, 'coordinator', color_scheme='dark')
    try:
        panel, result = open_manual_place(page, case['workspace'], case['shift'])
        row = next(r for r in result['candidates'] if r['person_id'] == case['trainee'])
        card = trainee_card(page, panel, row)
        expect(card).to_contain_text(re.compile('supervis', re.I))
        expect(card.get_by_role('button', name=re.compile('Assign|Break rule', re.I))).to_have_count(0)
        assert h.detail(case['workspace']) == case['before']
        shot(page, 'manual-no-supervisor-1440-dark', full_page=False)
    finally:
        context.close()


@pytest.mark.parametrize('manual_supervision_case', ['restricted'], indirect=True)
def test_named_supervisor_keeps_independent_rule_blocker(browser, live_server, h, manual_supervision_case):
    case = manual_supervision_case
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        panel, result = open_manual_place(page, case['workspace'], case['shift'])
        row = next(r for r in result['candidates'] if r['person_id'] == case['trainee'])
        card = trainee_card(page, panel, row)
        choice = next(c for c in row['supervision']['choices'] if c['supervisor_person_id'] == case['supervisor'])
        # ME-08 (d1beaa224): blocked people are listed under the reason that keeps them out, which labels their list.
        expect(panel.get_by_role('list', name=re.compile('^' + re.escape(case['rule']['name']) + ' · '))
               .get_by_text(row['display_name'], exact=True)).to_be_visible()
        expect(card.get_by_role('button', name=re.compile(r'^Assign (?!.* with a warning$)'))).to_have_count(0)
        panel.get_by_role('tab', name='Why', exact=True).click()
        why_row = panel.locator('.why-row').filter(has_text=row['display_name'])
        expect(why_row).to_contain_text('With ' + choice['display_name'])
        expect(why_row).to_contain_text(case['rule']['name'])
        expect(why_row.get_by_role('button', name='Assign ' + row['display_name'], exact=True)).to_have_count(0)
        assert h.detail(case['workspace']) == case['before']
        shot(page, 'manual-independent-blocker-1440-light', full_page=False)
    finally:
        context.close()


def test_person_filtered_assign_preserves_hidden_pair_and_provenance(browser, live_server, h, manual_supervision_case):
    case = manual_supervision_case
    context, page = sign_in(browser, live_server, 'coordinator', color_scheme='dark')
    try:
        open_manual_week(page, case['workspace'])
        target = case['shift']['id']
        hidden = page.locator(f"button.pchip[data-shift='{target}'][data-person='{case['other']}']")
        shown = page.locator(f"button.pchip[data-shift='{target}'][data-person='{case['supervisor']}']")
        expect(hidden).to_be_visible()
        expect(shown).to_be_visible()
        names = {p['id']: p['display_name'] for p in h.get('coordinator', '/api/catalogues')['people']}
        def person_only():
            pop = planner_filter(page)
            filter_pick(pop, 'Person', names[case['supervisor']])
            close_popover(page)
            pop = planner_filter(page)
            pop.get_by_role('radiogroup', name='Show', exact=True).get_by_role('radio', name='Matched person', exact=True).check()
            close_popover(page)
        def hidden_pair():
            expect(hidden).to_have_count(0)
            expect(shown).to_be_visible()
        person_only()
        # The Show control owns which participant rows a person-filtered duty draws.
        expect(hidden).to_have_count(0)
        expect(shown).to_be_visible()
        expect(page.locator(f"button.others[data-shift='{target}']")).to_contain_text('+1')
        assert h.detail(case['workspace']) == case['before']
        with page.expect_response(lambda r: r.url.endswith('/api/planner/fill-options')) as offered:
            page.locator(f"button.pchip.gap[data-shift='{target}']").click()
        assert offered.value.ok, offered.value.text()
        result = offered.value.json()
        row = next(r for r in result['candidates'] if r['person_id'] == case['trainee'])
        card = trainee_card(page, page.locator('.wk-panel'), row)
        saved = save_checked(page, h, case, card, result, before_reload=hidden_pair)
        # A dated route resets an unsaved display filter; choose it again, without changing the stored roster.
        person_only()
        hidden_pair()
        assert h.detail(case['workspace']) == saved
    finally:
        context.close()
# Include this file in hosted supervision and team-lead qualification.
