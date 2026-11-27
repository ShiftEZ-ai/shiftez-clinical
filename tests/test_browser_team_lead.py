"""Team lead in the browser: who may lead through the shared conditions, one lead by radio or several by checkbox, badge."""
from packages.domain.time import now as clock_now
import re
import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, navigate, save_dialog, open_session, route_fetch, wait_held  # noqa: F401

from .test_governed_leadership import activate, proposal, line

pytestmark = pytest.mark.browser


def test_duty_type_lead_settings_radio_choice_and_badge(browser, live_server, h):
    a, b, _ = h.seed['people']
    learners = h.seed['trainee_category']
    h.command('coordinator', 'membership.create', {'person_id': b, 'category_id': learners, 'starts_on': '2020-01-01'})
    typ = h.configure('assignment_types', {'name': 'Led review', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 2, 'maximum': 2}]}})['record']
    governed = line(typ['id'])
    governed['who'] = {'match': 'all', 'conditions': [{'field': 'category_ids', 'op': 'any', 'values': [learners]}]}
    activate(h, proposal(h, [governed]))
    assert typ['definition'].get('lead') is None
    errors = []
    w = h.shift(h.workspace('Lead browser'), typ=typ['id'], people=[a, b])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        navigate(page, 'planner')
        open_session(page)
        page.get_by_role('button', name='Edit session', exact=True).click()
        form = page.locator('.dock-panel')
        expect(form.get_by_role('radio', name='No team lead')).to_be_checked()
        leads = form.get_by_role('radio', name=re.compile('^Team lead'))
        expect(leads).to_have_count(2)
        expect(form.get_by_text('Cannot lead · does not meet who may lead this duty', exact=True)).to_be_visible()
        enabled = [i for i in range(2) if leads.nth(i).is_enabled()]
        assert len(enabled) == 1  # Alex is not a learner, so may not lead
        leads.nth(enabled[0]).check()
        save_dialog(form, 'Save draft session')
        detail = h.detail(w)
        assert {p['person_id']: p['team_lead'] for p in detail['shifts'][0]['participants']} == {a: False, b: True}
        assert detail['workspace']['validation']['valid']
        open_session(page)
        expect(page.locator('.wk-panel .wk-session').get_by_text('Team lead', exact=True)).to_be_visible()  # a fact chip on the person (S-09)
        assert not errors
    finally:
        context.close()


def test_session_form_allows_several_leads_up_to_the_maximum(browser, live_server, h):
    a, b, c = h.seed['people']
    typ = h.configure('assignment_types', {'name': 'Two leads', 'starts_on': '2020-01-01', 'definition': {
        'requirements': [{'minimum': 3, 'maximum': 3}]}})['record']['id']
    activate(h, proposal(h, [line(typ, maximum=2)]))
    w = h.shift(h.workspace('Several leads'), typ=typ, people=[a, b, c])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator'); errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        navigate(page, 'planner')
        open_session(page)
        page.get_by_role('button', name='Edit session', exact=True).click()
        form = page.locator('.dock-panel')
        expect(form.get_by_role('radio', name='No team lead')).to_be_hidden()
        expect(form.get_by_text('Choose “Team lead” on up to 2 people below.', exact=True)).to_be_visible()
        leads = form.get_by_role('checkbox', name=re.compile('^Team lead'))
        expect(leads).to_have_count(3)
        leads.nth(0).check()
        leads.nth(1).check()
        expect(leads.nth(2)).to_be_disabled()  # the most is reached
        leads.nth(1).uncheck()
        expect(leads.nth(2)).to_be_enabled()
        leads.nth(2).check()
        save_dialog(form, 'Save draft session')
        detail = h.detail(w)
        assert sorted(p['team_lead'] for p in detail['shifts'][0]['participants']) == [False, True, True]
        assert detail['workspace']['validation']['valid']
        assert not errors
    finally:
        context.close()


def test_no_lead_choice_without_a_lead_setting_and_staff_see_they_lead(browser, live_server, h):
    a, b, _ = h.seed['people']
    plain = h.shift(h.workspace('No lead setting'), people=[a])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner')
        open_session(page)
        page.get_by_role('button', name='Edit session', exact=True).click()
        form = page.locator('.dock-panel')
        expect(form.get_by_label('Participant', exact=True)).to_have_count(1)
        expect(form.get_by_role('radio', name=re.compile('Team lead'))).to_be_hidden()
        expect(form.get_by_role('radio', name='No team lead')).to_be_hidden()
    finally:
        context.close()
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    today = clock_now().astimezone(ZoneInfo('Asia/Riyadh')).date()
    day = (today + timedelta(days=3)).isoformat()
    typ = h.configure('assignment_types', {'name': 'Led today', 'starts_on': '2020-01-01', 'definition': {
        'requirements': [{'minimum': 1, 'maximum': 2}]}})['record']['id']
    activate(h, proposal(h, [line(typ, [a])]))
    w = h.workspace('Led this week', today.isoformat(), (today + timedelta(days=7)).isoformat())
    start, end = f'{day}T08:00:00+03:00', f'{day}T12:00:00+03:00'
    w = h.shift(w, start, end, typ=typ, people=[{'person_id': a, 'start': start, 'end': end, 'team_lead': True}])['workspace']
    h.publish(w)
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page,'home')
        # The duty they lead is the next one: the hero names it; Today (planners) and My next duties carry the TL tag.
        hero = page.locator('.home-hero')
        expect(hero).to_contain_text('in 3 days')
        # The duty's own tag (its facts line); the hero's always-visible Tags key explains the same mark.
        expect(hero.get_by_role('paragraph').locator('.wk-tag.lead')).to_have_text('TL')
        expect(hero.get_by_role('list', name='Tags').locator('.wk-tag.lead')).to_have_text('TL')
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', [(1440, 'light'), (390, 'dark')])
@pytest.mark.parametrize('answer', ['fresh', 'error', 'stale'])
def test_filled_session_lead_context_is_fresh_or_visibly_unavailable(browser, live_server, h, width, scheme, answer):
    """Headcount is full; the server's dated answer alone enables choosing an existing participant as lead."""
    a, b, _ = h.seed['people']
    typ = h.configure('assignment_types', {'name': 'Shared governed clinic', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 2, 'maximum': 2}], 'allow_shared_services': True,
                       'shared_duty_reason': 'One fictional shared team'}})['record']
    activate(h, proposal(h, [line(typ['id'], [b])]))
    w = h.workspace('Filled headcount without lead')
    start, end = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'
    w = h.command('coordinator', 'shift.create', {'workspace_id': w['id'], 'assignment_type_id': typ['id'],
        'location_id': h.seed['locations'][0], 'service_ids': h.seed['services'], 'start': start, 'end': end,
        'participants': [{'person_id': pid, 'start': start, 'end': end} for pid in (a, b)],
        'override_reason': 'Synthetic missing named lead'}, revision=w['revision'])['workspace']
    before = h.detail(w)
    shift = before['shifts'][0]
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': width, 'height': 900})
    try:
        page.emulate_media(color_scheme=scheme)
        held, asked = [], []
        def hold(route):
            asked.append(route.request.post_data_json)
            held.append(route)
        page.route('**/lead-context', hold)
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        if width == 390:
            page.get_by_role('radiogroup', name='Calendar layout').get_by_role('radio', name='Grid', exact=True).click()
        gap = page.locator(f'.pchip.role-gap[data-shift="{shift["id"]}"]')
        expect(gap).to_have_accessible_name(re.compile('every place is filled'))
        expect(gap).not_to_contain_text('open place')
        gap.click()
        picker = page.locator('.wk-lead')
        expect(picker.get_by_role('heading', name='Choose team lead')).to_be_visible()
        wait_held(page, held)
        expect(picker).to_have_attribute('aria-busy', 'true')
        expect(picker.get_by_role('button', name='Make team lead')).to_have_count(0)
        assert asked[0]['shift_id'] == shift['id']
        assert set(asked[0]['shift']['service_ids']) == set(h.seed['services'])
        if answer == 'error':
            held[0].fulfill(status=503, json={'error': {'code': 'TEMPORARY', 'message': 'Synthetic context unavailable'}})
            expect(picker).to_contain_text('Couldn’t check who may lead.')
            expect(picker.get_by_role('button', name='Retry')).to_be_visible()
            expect(picker.get_by_role('button', name='Make team lead')).to_have_count(0)
            page.unroute('**/lead-context', hold)
            picker.get_by_role('button', name='Retry', exact=True).click()
            expect(picker.get_by_role('button', name='Make team lead')).to_have_count(1)
        else:
            response = route_fetch(held[0])
            assert response.status == 200
            body = response.json()
            assert body['workspace_revision'] == w['revision']
            assert {p['person_id']: p['can_lead'] for p in body['people']} == {a: False, b: True}
            if answer == 'stale':
                body['workspace_revision'] -= 1
            held[0].fulfill(response=response, json=body)
            if answer == 'stale':
                expect(picker).to_contain_text('The rota changed since this was checked.')
                expect(picker.get_by_role('button', name='Check again', exact=True)).to_be_visible()
                expect(picker.get_by_role('button', name='Make team lead')).to_have_count(0)
                assert h.detail(w)['shifts'] == before['shifts']
                return
        candidate = picker.locator('.wk-cand').filter(has_text='Staff 02(test)')
        expect(candidate.get_by_role('button', name='Make team lead')).to_be_enabled()
        expect(picker).to_contain_text(re.compile('does not meet who may lead', re.I))
        candidate.get_by_role('button', name='Make team lead').click()
        expect(page.locator('.wk-panel')).to_contain_text('leads this session')
        saved = h.detail(w)
        assert len(saved['shifts']) == 1
        assert saved['shifts'][0]['service_ids'] == shift['service_ids']
        assert {p['person_id']: p['team_lead'] for p in saved['shifts'][0]['participants']} == {a: False, b: True}
        assert saved['workspace']['validation']['valid']
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_lead_context_refreshes_after_governed_qualification_changes_without_draft_edit(browser, live_server, h):
    a, b, _ = h.seed['people']
    typ = h.configure('assignment_types', {'name': 'Qualification refresh duty', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 2, 'maximum': 2}]}})['record']
    rule = activate(h, proposal(h, [line(typ['id'], [b])]))
    w = h.shift(h.workspace('Qualification changes, same draft revision'), typ=typ['id'], people=[a, b])['workspace']
    [shift] = h.detail(w)['shifts']
    gap = f'.pchip.role-gap[data-shift="{shift["id"]}"]'  # the grid's chip, not the legend's lg-mark key
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        contexts, inputs = [], []
        def context_answer(route):
            inputs.append(route.request.post_data_json)
            response = route_fetch(route)
            contexts.append(response.json())
            route.fulfill(response=response)
        page.route('**/lead-context', context_answer)
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        page.locator(gap).click()
        picker = page.locator('.wk-lead')
        expect(picker.locator('.wk-cand').filter(has_text='Staff 02(test)').get_by_role('button', name='Make team lead')).to_be_enabled()
        revision = h.detail(w)['workspace']['revision']
        updated = proposal(h, [line(typ['id'], [a])], expected_policy_id=rule['id'], expected_revision=rule['revision'])
        activate(h, updated)
        assert h.detail(w)['workspace']['revision'] == revision
        fresh = h.client('coordinator').post(f"/api/workspaces/{w['id']}/lead-context", json=inputs[0])
        assert fresh.status_code == 200, fresh.text
        fresh = fresh.json()
        assert fresh['input_revision'] != contexts[0]['input_revision']
        assert {p['person_id']: p['can_lead'] for p in fresh['people']} == {a: True, b: False}
        navigate(page, 'home')
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        page.locator(gap).click()
        expect(picker.locator('.wk-cand').filter(has_text='Staff 01(test)').get_by_role('button', name='Make team lead')).to_be_enabled()
        expect(picker.locator('.wk-cand').filter(has_text='Staff 02(test)').get_by_role('button', name='Make team lead')).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize('changed', [False, True], ids=['exact-preview', 'stale-preview'])
def test_legacy_leads_convert_from_exact_preview_and_require_independent_activation(browser, live_server, h, changed):
    from .legacy_lead import historical_type, set_historical_lead
    from .test_browser_rule_catalogue import flag
    from .test_browser_rule_catalogue_v2 import reason
    flag(h)
    typ = historical_type(h, {'name': 'Fictional historical leadership', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1}], 'lead': {'minimum': 1, 'maximum': 1}}})
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        plans, sent = [], []
        def preview(route):
            response = route_fetch(route)
            if route.request.post_data_json.get('legacy_effective_on'):
                plans.append(response.json())
            route.fulfill(response=response)
        def commands(route):
            if route.request.post_data_json['kind'] == 'rule.propose':
                sent.append(route.request.post_data_json['payload'])
            route.continue_()
        page.route('**/team_leads/check', preview)
        page.route('**/api/commands', commands)
        navigate(page, 'rules/team_leads')
        page.get_by_role('button', name='Convert into this rule…', exact=True).click()
        dialog = page.get_by_role('dialog', name='Convert duty type leads into Team leads', exact=True)
        expect(dialog.locator('.rc-diff li').first).to_be_visible()
        dialog.get_by_label('Effective from', exact=True).fill('2026-10-04')
        dialog.get_by_label('Effective from', exact=True).press('Tab')
        expect(dialog.locator('[role=status]')).to_contain_text('4 Oct 2026')
        if changed:
            set_historical_lead(h, typ['id'], {**typ['definition']['lead'], 'maximum': 2})
        reason(page, 'Preserve exact historical leadership semantics', 'Propose')
        if changed:
            expect(dialog).to_contain_text('Duty type leadership changed. Review the updated lines, then propose again.')
            expect(dialog.locator('[role=status]')).to_contain_text('4 Oct 2026')
            assert not any(e['proposed'] for e in h.get('coordinator', '/api/rules/catalogue')['entries'] if e['id'] == 'team_leads')
            reason(page, 'Reviewed refreshed exact historical leadership', 'Propose')
        expect(dialog).to_have_count(0)
        assert sent[-1]['on'] is True and sent[-1]['settings'] == {}
        assert sent[-1]['lines'] == plans[-1]['lines']
        assert sent[-1]['legacy_lead_conversion'] == plans[-1]['legacy_lead_conversion']
        entry = next(e for e in h.get('coordinator', '/api/rules/catalogue')['entries'] if e['id'] == 'team_leads')
        assert entry['rule'] is None and entry['proposed']
        activate(h, entry['proposed'])
        page.reload()
        expect(page.get_by_role('button', name='Convert into this rule…', exact=True)).to_have_count(0)
        entry = next(e for e in h.get('coordinator', '/api/rules/catalogue')['entries'] if e['id'] == 'team_leads')
        assert entry['rule']['status'] == 'active'
        assert entry.get('legacy_leads', 0) == 0
    finally:
        context.close()


def test_team_leads_without_legacy_types_offers_no_conversion(browser, live_server, h):
    from .test_browser_rule_catalogue import flag
    flag(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules/team_leads')
        expect(page.locator('#main[data-loaded-view=rules]')).to_be_visible()
        expect(page.get_by_role('button', name='Convert into this rule…', exact=True)).to_have_count(0)
        assert next(e for e in h.get('coordinator', '/api/rules/catalogue')['entries'] if e['id'] == 'team_leads').get('legacy_leads', 0) == 0
    finally:
        context.close()
