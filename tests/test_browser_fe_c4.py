"""Cycle-4 frontend fixes (journey root-cause audit, SWEEP findings): every duty still ahead offers Offer swap and Can't
make it, on Home's Coming up and the Schedule's duty panel, phone and desktop (SW-05); a change the approver returned
says Returned with the reason and can be taken back to edit (SW-06); an absence that closes the session says so before
anything is sent, with Replace as the primary (SW-02); a give-away nobody can take cannot be sent (SW-03, SW-25); the
run's changes are named by their cause (SW-04); a busy button says how long it has run (SW-12); a stricter person line is
not told the broader line's number binds (SW-10); a programme lead's published Schedule does not read what it may not
(SW-15); names wrap at spaces (SW-13)."""
import json
import os
import re
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlencode

import pytest
from playwright.sync_api import expect
from packages.contracts.schemas import ABSENCE_REPORT_LABEL

from .test_browser import browser, live_server, sign_in, navigate, route_fetch, expect_loaded, wait_held  # noqa: F401
from .test_browser_home import local_today
from .test_browser_schedule import show
from .test_browser_published_changes import FakeChanges, published, open_session
from .test_browser_rule_catalogue import flag
from .test_browser_rule_catalogue_v2 import line, v2_rule
from .governance_support import identity
from .test_browser_operator_role_coverage import all_roles  # noqa: F401

pytestmark = pytest.mark.browser
DESKTOP = {'width': 1440, 'height': 900}
PHONE = {'width': 390, 'height': 844}
SHOTS = os.getenv('FE_C4_SHOTS')  # a folder: the fixed screens, for the handoff's after pictures


def shot(page, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f'after-{name}.png'))


def my_duties(h):
    """A published rota with the staff member on three afternoons (tomorrow, in two and in three days), and a colleague
    with a staff sign-in (so an offer has someone to go to). Returns the days and each day's assignment id."""
    me = h.get('staff', '/api/me')['person_id']
    other = next(p for p in h.seed['people'] if p != me)
    identity(h, 'staff_two', ['staff'], other)
    day = local_today()
    w = h.workspace('FE-C4 my duties', day.isoformat(), (day + timedelta(days=10)).isoformat())
    days = [day + timedelta(days=n) for n in (1, 2, 3)]
    for d in days:
        w = h.shift(w, start=f'{d}T13:00:00+03:00', end=f'{d}T16:00:00+03:00', people=[me])['workspace']
    pub = h.publish(w)['publication_id']
    shifts = h.get('coordinator', '/api/publications/' + pub)['snapshot']['shifts']
    ids = {s['start'][:10]: next(p['id'] for p in s['participants'] if p['person_id'] == me) for s in shifts}
    return me, days, ids


def sent_commands(page, kinds):
    sent = []

    def commands(route):  # what the client sends; the real server answers
        body = json.loads(route.request.post_data or '{}')
        if body.get('kind') in kinds:
            sent.append(body)
        route.fallback()
    page.route('**/api/commands', commands)
    return sent


@pytest.mark.parametrize('size', [PHONE, DESKTOP], ids=['phone', 'desktop'])
def test_every_duty_ahead_offers_swap_and_cant_make_it(browser, live_server, h, size):
    me, days, ids = my_duties(h)
    context, page = sign_in(browser, live_server, 'staff', size, phone_notice=size is PHONE)
    try:
        sent = sent_commands(page, ('request.create',))
        page.reload()
        expect_loaded(page, 'home')
        coming = page.get_by_role('region', name='Coming up')
        rows = coming.locator('.home-duty-list li')
        expect(rows).to_have_count(2)  # the hero holds tomorrow; the other two are here
        more = rows.nth(0).get_by_role('button', name=re.compile('^Actions for '))
        more.click()
        menu = page.get_by_role('menu', name=re.compile('^Actions for '))
        expect(menu.get_by_role('menuitem', name='Offer swap')).to_be_visible()
        shot(page, f"SW-05-coming-up-menu-{size['width']}")
        menu.get_by_role('menuitem', name=ABSENCE_REPORT_LABEL).click()
        dialog = page.get_by_role('dialog', name=ABSENCE_REPORT_LABEL + '?')
        dialog.get_by_role('button', name='Tell the coordinator').click()
        expect(dialog).to_have_count(0)
        # The report is on that duty (the second one), not the next one the hero shows.
        assert sent[-1]['payload']['assignment_id'] == ids[days[1].isoformat()]
        expect(rows.nth(0).locator('.badge').filter(has_text='Reported')).to_be_visible()
        expect(page.locator('#main .home-hero').get_by_role('button', name=ABSENCE_REPORT_LABEL, exact=True)).to_be_visible()
        if size is DESKTOP:
            # The Schedule's panel of a later duty offers the same two actions.
            show(page, f'schedule/week/{days[2].isoformat()}')
            chip = page.locator(f'#schedule-view [data-person~="{me}"]:visible').last
            expect(chip).to_be_visible()
            chip.click()
            panel = page.locator('.dock-panel')
            expect(panel.get_by_role('button', name=ABSENCE_REPORT_LABEL, exact=True)).to_be_visible()
            expect(panel.get_by_role('button', name='Offer swap', exact=True)).to_be_visible()
            shot(page, 'SW-05-schedule-duty-panel-1440')
            panel.get_by_role('button', name='Offer swap', exact=True).click()
            expect(page).to_have_url(re.compile(r'#requests'))
            dialog = page.get_by_role('dialog')
            expect(dialog.get_by_label('Your duty to swap')).to_have_value(ids[days[2].isoformat()])
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_a_waiting_report_says_reported_even_when_homes_read_lands_before_coming_up(browser, live_server, h):
    """The late-run failure of the test above: Home's read answered before Coming up's own read, so its rows were built
    after Home's data was known and the "Reported" mark was dropped for a row not yet on the page. Forced here: Coming
    up's calendar read waits until Home's read has answered."""
    me, days, ids = my_duties(h)
    day = days[1].isoformat()
    events = h.get('staff', '/api/calendar?' + urlencode({'start': day, 'end': days[2].isoformat(), 'person_id': me}))['events']
    pub = next(e['source_id'] for e in events if e.get('id') == ids[day])
    h.command('staff', 'request.create', {'kind': 'absence_report', 'publication_id': pub, 'assignment_id': ids[day], 'reason': 'sick'})
    context, page = sign_in(browser, live_server, 'staff', PHONE, phone_notice=True)
    held = []
    try:
        page.route(re.compile(r'/api/calendar\?'), lambda route: held.append(route))
        with page.expect_response(lambda r: r.url.endswith('/api/home')):
            page.reload()
        wait_held(page, held)
        for route in held:  # released once each, before unroute (unroute continues whatever it still holds)
            route.continue_()
        page.unroute(re.compile(r'/api/calendar\?'))
        rows = page.get_by_role('region', name='Coming up').locator('.home-duty-list li')
        expect(rows).to_have_count(2)
        expect(rows.nth(0).locator('.badge').filter(has_text='Reported')).to_be_visible()
        expect(rows.nth(1).get_by_role('button', name=re.compile('^Actions for '))).to_be_visible()
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_a_give_away_nobody_can_take_cannot_be_sent(browser, live_server, h):
    me, days, ids = my_duties(h)

    def nobody(route):  # maintenance: every colleague is listed with `eligible`; here none can take the duty
        if 'assignment_id=' not in route.request.url:
            return route.fallback()
        response = route_fetch(route)
        body = response.json()
        body['recipients'] = [{**r, 'eligible': False, 'message': 'Not eligible for this clinic'} for r in body['recipients']]
        route.fulfill(response=response, json=body)
    context, page = sign_in(browser, live_server, 'staff', PHONE, phone_notice=True)
    try:
        page.route('**/api/swaps/options?*', nobody)
        navigate(page, f'requests?new=1&type=swap&duty={ids[days[1].isoformat()]}')
        dialog = page.get_by_role('dialog')
        kind = dialog.get_by_label('Kind of offer')
        if kind.count():
            kind.select_option(label='Give away · anyone eligible takes it')
            expect(dialog.get_by_label('Message to colleagues')).to_be_visible()
        expect(dialog.get_by_text('Nobody eligible can take this duty.', exact=False)).to_be_visible()
        send = dialog.get_by_role('button', name='Send offer')
        expect(send).to_be_disabled()
        shot(page, 'SW-03-give-away-nobody-390')
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_a_returned_change_says_so_with_the_reason_and_is_taken_back_to_edit(browser, live_server, h):
    tomorrow, _ = published(h)
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        fake = FakeChanges(h)
        fake.install(page)
        s = next(x for x in fake.shifts if x['start'][:10] == tomorrow.isoformat())
        fake.changes = [{'id': 'c1', 'kind': 'absence', 'shift_id': s['id'], 'assignment_id': s['participants'][0]['id'], 'absence_reason': 'Sick',
                         'reason': 'Alex absent', 'status': 'submitted', 'workspace_id': 'candidate', 'created_at': '2026-11-03T10:00:00+00:00',
                         'urgency': 'routine', 'submitted_at': '2026-11-03T10:05:00+00:00',
                         'returned': {'by_name': 'Fictional approver', 'at': '2026-11-03T11:00:00+00:00', 'reason': 'Ask Taylor first'}}]
        show(page, f'schedule/week/{tomorrow.isoformat()}')
        chip = page.locator('.pc-chip')
        expect(chip).to_have_text('1 change returned')
        chip.click()
        panel = page.locator('.dock-panel.pc-panel')
        expect(panel).to_contain_text('Returned by Fictional approver')
        expect(panel).to_contain_text('“Ask Taylor first”')
        expect(panel.locator('.badge').filter(has_text='Returned')).to_be_visible()
        expect(panel.locator('.badge').filter(has_text='Waiting')).to_have_count(0)
        shot(page, 'SW-06-returned-change-list-1440')
        panel.get_by_role('button', name='Edit the changes').click()
        expect(panel.locator('.badge').filter(has_text='Not sent')).to_be_visible()
        expect(panel.get_by_role('button', name=re.compile('^Undo this change'))).to_be_visible()
        assert fake.sent[-1][0] == 'publication.changes.withdraw'
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def absent_alone(browser, live_server, h):
    """Taylor is alone in three days; the coordinator marks them absent (FakeChanges: nobody left)."""
    _, later = published(h)
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    fake = FakeChanges(h, closes=True)
    fake.install(page)
    panel = open_session(page, h, later, person=2)
    panel.get_by_role('button', name='Change Taylor Noor on this session').click()
    page.get_by_role('menu', name='Change Taylor Noor').get_by_role('menuitem', name='Mark absent · Sick').click()
    return context, page, panel, fake


def test_an_absence_that_leaves_nobody_keeps_the_session_open_and_offers_replace_first(browser, live_server, h):
    """D38: an absence never closes a session by itself. Nobody left is said before anything is sent, Replace is the
    primary, and there is no Keep it closed."""
    context, page, panel, fake = absent_alone(browser, live_server, h)
    try:
        expect(panel).to_contain_text('Nobody is left on this session.')
        expect(panel).to_contain_text('Its place stays open.')
        replace = panel.get_by_role('button', name='Replace Taylor Noor…')
        expect(replace).to_have_class(re.compile(r'\bprimary\b'))
        expect(panel.get_by_role('button', name='Keep it closed')).to_have_count(0)
        expect(panel.get_by_role('button', name='Close the session…')).not_to_have_class(re.compile(r'\bprimary\b'))
        shot(page, 'SW-02-nobody-left-1440')
        replace.click()
        expect(panel.get_by_role('heading', name='Replace Taylor Noor')).to_be_visible()
        assert [k for k, _, _ in fake.sent] == ['publication.change']  # SW3-A-18: opening Replace keeps the absence
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_closing_the_session_after_an_absence_is_the_explicit_audited_cancellation(browser, live_server, h):
    """D38 / maintenance: Close the session is the session-level cancel (no assignment), said as removing the needed place."""
    context, page, panel, fake = absent_alone(browser, live_server, h)
    try:
        panel.get_by_role('button', name='Close the session…').click()
        expect(panel.get_by_role('heading', name='Close the session')).to_be_visible()
        expect(panel).to_contain_text('Its needed place is removed.')
        panel.get_by_label('Reason').fill('Clinic closed for the day')
        panel.get_by_role('button', name='Close session').click()
        panel.get_by_role('button', name='Confirm close?').click()
        expect(page.get_by_text('Closing added to the changes. Send it with the others.')).to_be_visible()
        kind, payload, _ = fake.sent[-1]
        assert kind == 'publication.change' and payload['kind'] == 'cancel' and 'assignment_id' not in payload
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_run_changes_are_named_by_cause_and_a_busy_button_shows_its_time(browser, live_server):
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        # SW-04: the changes grouped by cause read the cause's sentence from the before Fix list; the rest come last.
        text = page.evaluate("""async()=>{const R=await window.importBundle('autoschedule_result.js');
          const n=R.changesByCause({beforeCauses:[{id:'c1',sentence:'3 sessions need cover',meta:'Fri 7 May'}],
            changesByCause:[{cause_id:null,changes:3},{cause_id:'c1',changes:5}]},8);
          return [...n.querySelectorAll('li')].map(li=>li.innerText.replace(/\\s+/g,' ').trim());}""")
        assert text[0].startswith('5') and '3 sessions need cover' in text[0] and '5 changes · Fri 7 May' in text[0]
        assert 'Other changes' in text[1] and '3 changes' in text[1]
        # SW-12: a button whose work runs past 3 s shows its elapsed time; its name stays its label.
        page.clock.pause_at(page.evaluate('new Date(Date.now() + 1000).toISOString()'))
        page.clock.run_for(1000)
        page.evaluate("""async()=>{const d=await window.importBundle('dom.js');window.finish=null;
          const b=d.button('Publish',()=>new Promise(r=>window.finish=r),'primary');b.id='fe-c4-busy';document.querySelector('#main').prepend(b);b.click();}""")
        b = page.locator('#fe-c4-busy')
        page.clock.run_for(5000)
        expect(b).to_contain_text('Publishing…')
        expect(b.locator('.elapsed-time')).to_have_text(' · 0:05')
        expect(page.get_by_role('button', name='Publishing…', exact=True)).to_have_count(1)
        page.evaluate('window.finish()')
        page.clock.run_for(1000)
        expect(b).to_have_text('Publish')
    finally:
        context.close()


def test_a_stricter_person_line_is_not_told_the_broader_number_binds(browser, live_server, h):
    flag(h, legacy=False)
    person = h.seed['people'][0]
    name = next(p['display_name'] for p in h.get('coordinator', '/api/catalogues')['people'] if p['id'] == person)
    where = {'match': 'all', 'conditions': [{'field': 'service_ids', 'op': 'any', 'values': [h.seed['services'][0]]}]}
    rank = {'match': 'all', 'conditions': [{'field': 'category_ids', 'op': 'any', 'values': [h.seed['category']]}]}
    who = {'match': 'all', 'conditions': [{'field': 'person_ids', 'op': 'any', 'values': [person]}]}
    broad = line({'max': {'value': 5, 'level': 'mandatory'}}, who=rank, where=where, measure='duties', period={'kind': 'calendar_week'})
    mine = line({'max': {'value': 3, 'level': 'medium'}}, who=who, where=where, measure='duties', period={'kind': 'calendar_week'},
                starts_on='2026-10-04', ends_on='2026-10-11')
    v2_rule(h, 'workload_limit', [broad, mine])

    relations = {mine['id']: {'line_id': broad['id'], 'line_no': 1, 'overrides': False, 'both_apply': True,
                              'reason': 'where_differs', 'fix': None, 'default_severity': {'max': 'mandatory'}}}

    def check(route):  # the rule page reads its check inline with the rule's detail, then from the check itself
        response = route_fetch(route)
        body = response.json()
        held = body.get('check') if route.request.url.split('?')[0].endswith('/detail') else body
        if held is not None:
            held['relations'] = relations
        route.fulfill(response=response, json=body)
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        page.route('**/api/rules/catalogue/workload_limit/check', check)
        page.route('**/api/rules/catalogue/workload_limit/detail?*', check)
        navigate(page, f"rules/workload_limit/{mine['id']}")
        relation = page.locator('.rc-line-panel .rc-says .rc-relation')
        expect(relation).to_contain_text('Does not override line 1: it names other clinics. Both apply.')
        expect(relation).not_to_contain_text(f'{name} still has at most 5')
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_a_programme_lead_reads_the_published_schedule_without_refusals(browser, live_server, all_roles):
    tomorrow, _ = published(all_roles)
    context, page = sign_in(browser, live_server, 'programme_lead', DESKTOP)
    refused = []
    try:
        page.on('response', lambda r: refused.append(r.url) if r.status == 403 and '/coverage' in r.url else None)
        show(page, f'schedule/week/{tomorrow.isoformat()}')
        expect(page.locator('#schedule-view .pchip').first).to_be_visible()
        assert refused == []
    finally:
        context.close()


def test_names_in_the_requests_table_wrap_at_spaces(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        page.evaluate("""async()=>{const d=await window.importBundle('person_label.js');const box=document.createElement('div');box.id='fe-c4-name';
          box.style.setProperty('width','90px');box.append(d.personLink('p','Dr. Felix Parker',()=>{}));document.querySelector('#main').prepend(box);}""")
        assert page.locator('#fe-c4-name .person-link').evaluate("b=>getComputedStyle(b).overflowWrap") == 'break-word'
    finally:
        context.close()


def test_ending_an_eligibility_names_the_person_once_and_opens_every_duty(browser, live_server, h):
    cat = h.get('credentialer', '/api/catalogues')
    e = next(x for x in cat['eligibilities'] if not x.get('ends_on') and not x.get('revoked'))
    person = next(p for p in cat['people'] if p['id'] == e['person_id'])['display_name']
    sessions = [{'person': person, 'type': 'Clinical review session', 'start': f'2026-10-{d:02}T10:00:00+00:00'} for d in range(5, 18)]
    context, page = sign_in(browser, live_server, 'credentialer', DESKTOP)
    try:
        page.route('**/api/eligibilities/*/published-impact', lambda route: route.fulfill(json={
            'published_impact': {'count': 13, 'sessions': sessions},
            'draft_impact': {'count': 1, 'sessions': [{'person': person, 'rota_name': 'December draft', 'type': 'Clinical review session',
                                                       'start': '2026-12-02T10:00:00+00:00'}]}}))
        navigate(page, f"people?open={e['person_id']}")
        page.locator('.dock-panel').get_by_role('button', name=re.compile('^End ')).first.click()
        dialog = page.get_by_role('dialog', name=re.compile('^End '))
        expect(dialog).to_contain_text(f'{person}’s 13 published duties would need cover:')
        items = dialog.locator('.impact-list').first.locator('li:not([hidden])')
        expect(items).to_have_count(11)  # ten duties and Show 3 more
        expect(items.first).not_to_contain_text(person)
        dialog.get_by_role('button', name='Show 3 more').click()
        expect(items).to_have_count(13)
        # maintenance draft_impact: duties in shared drafts get their own sentence and list, each line naming its rota.
        expect(dialog).to_contain_text(f'{person}’s 1 duty in a draft would need cover:')
        expect(dialog.locator('.impact-list').nth(1)).to_contain_text('December draft')
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_a_month_with_nothing_says_so_under_its_day_grid(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        show(page, 'schedule/month/2031-05-12?edit=none')  # far from any seeded rota
        grid = page.locator('#schedule-view .wk-grid')
        expect(grid.locator('.mo-wk').first).to_be_visible()  # the day grid stays (its days are click targets)
        empty = grid.locator('.mo-empty')
        expect(empty.get_by_role('button', name='New rota')).to_be_visible()
    finally:
        context.close()


def test_one_reported_absence_is_one_home_row(browser, live_server):
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        rows = page.evaluate("""async()=>{const H=await window.importBundle('home.js');
          const ctx={me:{organisation:{timezone:'Asia/Riyadh'},person_id:'me'},catalogues:{people:[{id:'p1',display_name:'Hugo Young'}]},navigate(){},setWorkspace(){}};
          const x={person_id:'p1',person:'Hugo Young',start:'2026-11-06T10:00:00Z',end:'2026-11-06T13:00:00Z',shift_id:'s',publication_id:'pub'};
          const one=H.needsOf(ctx,{},{attention:[{key:'published_absence_conflicts',count:1,sample:[x],action:{key:'amend_publication'}},
            {key:'absence_reported',count:1,sample:[{...x,status:'approved',replace_action:{shift_id:'s',assignment_id:'a'}}]}],rotas:0,my_requests:[],recent_changes:[]});
          const two=H.needsOf(ctx,{},{attention:[{key:'published_absence_conflicts',count:1,sample:[{...x,person_id:'p2'}],action:{key:'amend_publication'}},
            {key:'absence_reported',count:1,sample:[{...x,status:'approved',replace_action:{shift_id:'s',assignment_id:'a'}}]}],rotas:0,my_requests:[],recent_changes:[]});
          return [one.map(r=>[r.title,r.action]),two.length];}""")
        assert rows[0] == [[ABSENCE_REPORT_LABEL, 'Replace']] and rows[1] == 2
    finally:
        context.close()


def test_submit_shows_could_improve_and_the_score_from_the_fix_list(browser, live_server, h):
    w = h.shift(h.workspace('FE-C4 submit tiles'), people=[h.seed['people'][0]])['workspace']
    h.command('coordinator', 'workspace.validate', {'reason': 'Routine rota check'}, w['id'], w['revision'])
    s = h.get('coordinator', f"/api/workspaces/{w['id']}/fix-list")['summary']
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        show(page, f"schedule/week/{w['starts_on']}?edit={w['id']}")
        page.get_by_role('button', name='Submit for review…', exact=True).click()
        tiles = page.get_by_role('dialog').locator('.publish-summary')
        expect(tiles).to_contain_text('Places filled')
        if s['could']:
            expect(tiles).to_contain_text(f"Could improve{s['could']}")
        if s.get('score') is not None and not s['must']:
            expect(tiles).to_contain_text(f"Score{round(s['score'])}")
    finally:
        context.close()


def test_wish_settings_inputs_line_up_however_their_labels_wrap(browser, live_server, h):
    from .test_browser import open_config
    context, page = sign_in(browser, live_server, 'admin', DESKTOP)
    try:
        open_config(page, 'Auto-schedule & scoring')
        page.get_by_role('tab', name='Staff preferences', exact=True).click()
        wishes = page.locator('fieldset').filter(has=page.locator('legend', has_text='Staff wishes'))
        expect(wishes.locator('.field').first).to_be_visible()
        rows = wishes.evaluate("""f=>{const by={};for(const x of f.querySelectorAll(':scope>.field')){const i=x.querySelector('input,select');if(!i)continue;
          const k=Math.round(x.getBoundingClientRect().top);(by[k]||=[]).push(Math.round(i.getBoundingClientRect().top));}return Object.values(by);}""")
        assert rows and all(max(r) - min(r) <= 1 for r in rows), rows
        assert wishes.locator('legend').bounding_box()['y'] < wishes.get_by_label('Wish points renew each', exact=True).bounding_box()['y']
        shot(page, 'SW-27-wish-settings-1440')
    finally:
        context.close()
