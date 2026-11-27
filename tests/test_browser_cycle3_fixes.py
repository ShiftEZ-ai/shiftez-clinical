"""the published view stays responsive at department scale and
reads its publication once (G-01); Home lists each Can't make it with its own Replace and a published session says who
reported absent (G-02); Fairness goal cards name their group (G-03); Home's leave clash opens that person's replacement
list (G-05); a refused Undo is one message (G-04)."""
import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from playwright.sync_api import expect

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from .test_browser import browser, live_server, navigate, sign_in, route_fetch  # noqa: F401
from .test_browser_published_changes import FakeChanges, published, open_session, local_today
from .test_browser_fairness_dashboard import october, section
from .test_browser_rule_catalogue import flag
from .test_browser_rule_catalogue_v2 import line, themed
from .test_browser_fair_share_dials import fair_share_rule
from .test_browser_supervision_balance import supervised_october

from .test_browser import DRAFT_CHIP, draft_chip  # noqa: F401
pytestmark = pytest.mark.browser
DESKTOP = {'width': 1440, 'height': 900}
LONG_TASKS = """window.__long=[];new PerformanceObserver(l=>{for(const e of l.getEntries())window.__long.push(e.duration);})
  .observe({type:'longtask',buffered:true});"""


def names(h):
    return {p['id']: p['display_name'] for p in h.get('coordinator', '/api/catalogues')['people']}


def with_home_item(page, item):
    def home(route):
        reply = route_fetch(route)
        data = reply.json()
        data['attention'] = [item, *data.get('attention', [])]
        route.fulfill(response=reply, body=json.dumps(data))
    page.route(re.compile(r'.*/api/home(\?.*)?$'), home)


def published_month_at_scale(h, start):
    """150 people with ranks and clinics, and a published four-week rota of 500 sessions (the dev department's shape)."""
    w = h.workspace('Scale month', start=start.isoformat(), end=(start + timedelta(days=28)).isoformat())
    dep, org = h.seed['department_id'], h.seed['organisation_id']
    with transaction(h.app.state.engine, write=True) as db:
        people = [{'id': str(uuid.uuid4()), 'organisation_id': org, 'display_name': f'Scale Person {k:03}', 'short_code': f'S{k:03}'}
                  for k in range(150)]
        db.execute(m.Person.__table__.insert(), people)
        pids = [p['id'] for p in people]
        db.execute(m.Membership.__table__.insert(), [{'id': m.uid(), 'department_id': dep, 'person_id': pid, 'category_id': h.seed['category'],
                                                      'starts_on': date(2024, 1, 1), 'ends_on': None} for pid in pids])
        db.execute(m.Eligibility.__table__.insert(), [{'id': m.uid(), 'department_id': dep, 'person_id': pid, 'service_id': sv,
                                                       'starts_on': date(2024, 1, 1)} for pid in pids for sv in h.seed['services']])
        snapshot = record(db.get(m.AssignmentType, h.seed['type']))
        shifts, assignments = [], []
        for n in range(500):  # 18 a day, two at a time (one per location), two hours each: valid, so it publishes
            at = datetime.combine(start + timedelta(days=n % 28), datetime.min.time(), timezone.utc) + timedelta(hours=2 * ((n // 28) // 2))
            sid = m.uid()
            shifts.append({'id': sid, 'workspace_id': w['id'], 'assignment_type_id': h.seed['type'], 'type_revision': snapshot['revision'],
                           'type_snapshot': snapshot, 'location_id': h.seed['locations'][(n // 28) % 2], 'start': at, 'end': at + timedelta(hours=2)})
            assignments.append({'id': m.uid(), 'shift_id': sid, 'person_id': pids[n % 150], 'start': at, 'end': at + timedelta(hours=2)})
        db.execute(m.Shift.__table__.insert(), shifts)
        db.execute(m.ShiftService.__table__.insert(), [{'shift_id': s['id'], 'service_id': h.seed['services'][0]} for s in shifts])
        db.execute(m.Assignment.__table__.insert(), assignments)
    w = h.get('coordinator', '/api/workspaces/' + w['id'])['workspace']
    return w, h.publish(w)['publication_id']


def test_a_published_rota_at_department_scale_opens_responsive_and_reads_its_publication_once(browser, live_server, h):
    """G-01: a link to a published rota (?edit=<rota>, as the QA used) opens its published version on its days; the tab
    keeps answering, the main thread stays within budget, and a redraw (its change list arriving) reuses the publication
    it read (megabytes for a coordinator on dev) instead of reading it again."""
    start = local_today() + timedelta(days=(6 - local_today().weekday()) % 7 + 14)  # a Sunday two weeks ahead: changeable
    w, pub = published_month_at_scale(h, start)
    # An unsent change: its chip in the top bar is drawn by the redraw that follows reading the change list.
    sh = h.get('coordinator', '/api/publications/' + pub)['snapshot']['shifts'][40]
    h.command('coordinator', 'publication.change', {'kind': 'replace', 'shift_id': sh['id'], 'assignment_id': sh['participants'][0]['id'],
        'person_id': h.seed['people'][2], 'reason': 'Sick'}, target=pub)
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    page.add_init_script(LONG_TASKS)
    reads = []
    page.on('request', lambda r: reads.append(r.url) if f'/api/publications/{pub}' in r.url else None)
    try:
        page.reload()
        navigate(page, 'home')
        reads.clear()
        page.evaluate('window.__long=[]')
        navigate(page, f"schedule/week/{(start + timedelta(days=7)).isoformat()}?edit={w['id']}")
        expect(page).to_have_url(re.compile('source=' + pub))
        expect(page.locator(f'#schedule-view {DRAFT_CHIP}').first).to_be_visible()
        # Its change list has been read and the page redrawn for it before the reads are counted.
        expect(page.locator('.pc-chip')).to_contain_text('1 change · not sent')
        assert page.evaluate('1 + 1') == 2  # the tab answers
        whole = [u for u in reads if u.endswith(f'/api/publications/{pub}')]
        assert len(whole) == 1, reads
        # Measured locally (6 Nov 2026): no long task at all; the budget leaves a loaded runner a wide margin.
        assert sum(page.evaluate('window.__long')) < 2000, page.evaluate('window.__long')
    finally:
        context.close()


def test_a_published_rota_link_after_an_amendment_shows_the_days_as_published_now(browser, live_server, h):
    """G-01 (side): a link to a rota amended and published again opens its days as they are published now, not the
    superseded version (no ?source= of the old publication)."""
    tomorrow, _ = published(h)
    w = next(x for x in h.get('coordinator', '/api/workspaces')['workspaces'] if x['name'] == 'Published changes rota')
    pub = w['publication_id']
    sh = h.get('coordinator', '/api/publications/' + pub)['snapshot']['shifts'][0]
    r = h.command('coordinator', 'publication.change', {'kind': 'replace', 'shift_id': sh['id'], 'assignment_id': sh['participants'][0]['id'],
        'person_id': h.seed['people'][2], 'reason': 'Sick'}, target=pub)
    sent = h.command('coordinator', 'publication.changes.submit', {'reason': 'Review'}, target=pub, revision=r['workspace']['revision'])
    h.lifecycle(h.lifecycle(sent['workspace'], 'approve', 'approver')['workspace'], 'publish')
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        navigate(page, f"schedule/week/{tomorrow.isoformat()}?edit={w['id']}")
        expect(page.locator(f'#schedule-view {DRAFT_CHIP}').first).to_be_visible()
        assert 'source=' + pub not in page.url
        expect(page.locator('#schedule-view').get_by_text(names(h)[h.seed['people'][2]], exact=True).first).to_be_visible()
    finally:
        context.close()


@pytest.mark.parametrize('width,theme', [(1440, 'light'), (390, 'dark')])
def test_home_lists_each_cant_make_it_with_its_own_replace(browser, live_server, h, width, theme):
    """G-02: up to five reports (the server's sample) are five lines, each with its own action; those past the sample
    are one more line. The third's Replace opens its own session on Replace for that person (not the first report's)."""
    tomorrow, later = published(h)
    named = names(h)
    context, page = themed(browser, live_server, 'coordinator', width, theme)
    try:
        fake = FakeChanges(h)
        fake.install(page)
        report = lambda n, s, p: {'request_id': f'r{n}', 'person_id': p['person_id'], 'person': named[p['person_id']],  # noqa: E731
                                  'start': s['start'], 'end': s['end'], 'status': 'approved',
                                  'replace_action': {'key': 'amend_publication', 'publication_id': 'p', 'shift_id': s['id'], 'assignment_id': p['id']}}
        first = next(s for s in fake.shifts if s['start'].startswith(tomorrow.isoformat()))
        second = next(s for s in fake.shifts if s['start'].startswith(later.isoformat()))
        duties = [(first, first['participants'][0]), (first, first['participants'][1]), (second, second['participants'][0])] * 2
        with_home_item(page, {'key': 'absence_reported', 'count': 7, 'sample': [report(n, s, p) for n, (s, p) in enumerate(duties[:5])]})
        page.reload()
        navigate(page, 'home')
        row = page.locator('#main .home-needs-list > li[data-need=absence_reported]')
        lines = row.locator('.home-need-items > li')
        expect(lines).to_have_count(6)
        for n, (_, p) in enumerate(duties[:5]):
            expect(lines.nth(n)).to_contain_text(named[p['person_id']])
            expect(lines.nth(n).get_by_role('button', name='Replace')).to_be_visible()
        expect(lines.nth(5)).to_contain_text('2 more')
        expect(lines.nth(5).get_by_role('button', name='Show')).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        if width == 1440:
            lines.nth(2).get_by_role('button', name='Replace').click()
            who = named[second['participants'][0]['person_id']]
            expect(page.locator('.dock-panel').get_by_role('heading', name='Replace ' + who)).to_be_visible()
            assert later.isoformat() in page.url  # that report's own day (the first report's is another)
    finally:
        context.close()


@pytest.mark.parametrize('status,words', [('submitted', 'waiting for approval'), ('approved', 'needs cover')])
def test_a_published_session_says_who_reported_absent_and_offers_their_replace(browser, live_server, h, status, words):
    """G-02: a staff Can't make it on a published session (the server's absence_reports, pending ones included) marks
    that person's chip absent and the session panel says it, with Replace for them first."""
    tomorrow, _ = published(h)
    a = h.seed['people'][0]  # the staff account's own person
    who = names(h)[a]
    w = next(x for x in h.get('coordinator', '/api/workspaces')['workspaces'] if x['name'] == 'Published changes rota')
    pub = w['publication_id']
    shift = next(x for x in h.get('coordinator', '/api/publications/' + pub)['snapshot']['shifts'] if x['start'].startswith(tomorrow.isoformat()))
    duty = next(p for p in shift['participants'] if p['person_id'] == a)
    reported = h.command('staff', 'request.create', {'kind': 'absence_report', 'publication_id': pub, 'assignment_id': duty['id'],
                                                     'reason': 'Synthetic report'})['record']
    if status == 'approved':
        h.command('approver', 'request.decide', {'decision': 'approve', 'reason': 'Independent absence decision'},
                  target=reported['id'], revision=reported['revision'])
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        panel = open_session(page, h, tomorrow)
        expect(page.locator(f'#schedule-view .pchip.absent[data-person~="{a}"]').first).to_be_visible()
        expect(panel.locator('.notice').filter(has_text=f'{who} reported absent · {words}')).to_be_visible()
        expect(panel.get_by_role('button', name=f'Replace {who}…')).to_be_visible()
    finally:
        context.close()


def test_home_leave_clash_replace_opens_the_replacement_list_for_the_person_on_leave(browser, live_server, h):
    """G-05 / fix 4: Home › Duties during approved leave › Replace opens that draft session's replacement list for the
    person on leave (the second on the session, not its first) and focuses their chip."""
    a, b, _ = h.seed['people']
    day = local_today() + timedelta(days=5)
    at = lambda hour: f'{day.isoformat()}T{hour:02}:00:00+03:00'  # noqa: E731
    w = h.workspace('Leave clash rota', day.isoformat(), (day + timedelta(days=7)).isoformat())
    w = h.shift(w, at(8), at(12), people=[a, b])['workspace']
    leave = h.command('coordinator', 'request.create', {'person_id': b, 'start': at(9), 'end': at(10),
                                                         'private_reason': 'Synthetic leave'})['record']
    h.command('approver', 'request.decide', {'decision': 'approve', 'reason': 'Independent test decision'}, leave['id'], leave['revision'])
    item = next(i for i in h.get('coordinator', '/api/home')['attention'] if i['key'] == 'leave_conflicts')
    assert item['sample'][0]['person_id'] == b and item['sample'][0]['assignment_id']
    who = names(h)[b]
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        navigate(page, 'home')
        page.locator('.home-needs li[data-need=leave_conflicts]').get_by_role('button', name='Replace').click()
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule')
        assert f"edit={w['id']}" in page.url
        expect(page.locator('.wk-panel').get_by_role('heading', name='Replace ' + who)).to_be_visible()
        expect(page.locator('.wk-panel').get_by_role('heading', name='Can take over', exact=True)).to_be_visible()
        expect(page.locator(f'#schedule-view {draft_chip(b)}').first).to_be_focused()
    finally:
        context.close()


@pytest.mark.parametrize('width,theme', [(1440, 'light'), (390, 'dark')])
def test_fairness_goal_cards_name_their_group(browser, live_server, h, width, theme):
    """G-03: a Fair share rule's lines are one goal card each, named by the server's audience_label and the measure
    ("<rank> · Duties"), never the same title twice; Compare with names them the same way."""
    flag(h)
    supervised_october(h)
    ranks = {c['id']: c['name'] for c in h.get('admin', '/api/catalogues')['categories']}
    goal = {'balance_goal': {'metric': 'duties', 'period': 'month'}}
    fair_share_rule(h, [line({'apply': {'value': None, 'level': 'medium'}}, settings=goal,
                             who={'match': 'all', 'conditions': [{'field': 'category_ids', 'op': 'any', 'values': [rank]}]})
                        for rank in (h.seed['category'], h.seed['trainee_category'])])
    context, page = themed(browser, live_server, 'coordinator', width, theme)
    try:
        navigate(page, 'insights/fairness')
        october(page)
        titles = section(page, 'goals').locator('.fx-goal-name h3').all_text_contents()
        for rank in (h.seed['category'], h.seed['trainee_category']):
            assert f'{ranks[rank]} · Duties' in titles, titles
        assert len(titles) == len(set(titles)), titles
        pick = section(page, 'distribution').get_by_label('Compare with', exact=True)
        expect(pick.locator('option', has_text=f"{ranks[h.seed['category']]} · Duties")).to_have_count(1)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    finally:
        context.close()


def test_a_refused_undo_is_one_message_with_try_again(browser, live_server, h):
    """G-04: Session saved · Undo, then the Undo is refused because a check ran at the same moment (STALE_INPUTS,
    recoverable): one message says nothing changed, with Try again; no review-stage words on a draft, no "Not saved"
    in the footer. Try again undoes it."""
    a, b, _ = h.seed['people']
    w = h.shift(h.workspace('Undo rota'), people=[a])['workspace']
    shift = h.detail(w)['shifts'][0]
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    refused = []

    def commands(route):
        if (route.request.post_data_json or {}).get('kind') == 'workspace.undo' and not refused:
            refused.append(True)
            return route.fulfill(status=409, json={'error': {'code': 'STALE_INPUTS', 'details': {'recoverable': True},
                'message': 'The rota or its scheduling inputs changed during preparation; refresh and apply the edit again'}})
        route.continue_()
    try:
        navigate(page, f"schedule/week/{w['starts_on']}?edit={w['id']}")
        page.route('**/api/commands', commands)
        payload = {'workspace_id': w['id'], 'shift_id': shift['id'], 'assignment_type_id': shift['assignment_type_id'], 'location_id': shift['location_id'],
                   'service_ids': shift['service_ids'], 'start': shift['start'], 'end': shift['end'], 'attributes': shift.get('attributes') or {},
                   'participants': [{'person_id': b, 'start': shift['start'], 'end': shift['end']}]}
        page.evaluate("""async ([payload, revision]) => {const A = await window.importBundle('api.js'), D = await window.importBundle('dom.js');
            await A.command('shift.update', payload, undefined, revision); D.notify('Session saved');}""", [payload, w['revision']])
        saved = page.locator('.toast').filter(has_text='Session saved')
        saved.get_by_role('button', name='Undo').click()
        refusal = page.locator('.toast.error')
        expect(refusal).to_have_count(1)
        expect(refusal).to_contain_text('Not undone: the rota was being checked at the same moment. Nothing changed.')
        expect(page.locator('.toast').filter(has_text='reopens and resubmits')).to_have_count(0)
        expect(page.locator('.toast').filter(has_text='changed during preparation')).to_have_count(0)
        expect(page.locator('#save-state')).not_to_contain_text('Not saved')
        refusal.get_by_role('button', name='Try again').click()
        expect(page.locator('.toast').filter(has_text='Undone')).to_be_visible()
        assert [p['person_id'] for p in h.detail(w)['shifts'][0]['participants']] == [a]
    finally:
        context.close()
