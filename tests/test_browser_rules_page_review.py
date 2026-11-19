"""quiet rule-change controls, line-scoped attention and dense row facts."""
import re
from datetime import date, timedelta

import pytest
from playwright.sync_api import expect

from .test_browser import add_filter, browser, live_server, navigate, route_fetch, sign_in
from .test_browser_audit_attention_layout import view
from .test_browser_audit_rules_layout import populated_catalogue
from .test_browser_rule_catalogue import catalogue_rule, flag, line
from .test_browser_rule_catalogue_v2 import line as v2_line, v2_rule
from .test_browser_rule_filter_builder import pick
from .test_rule_catalogue_conflicts import tag_people

pytestmark = pytest.mark.browser


def _who(field, values):
    return {'match': 'all', 'conditions': [{'field': field, 'values': list(values)}]}


def _assert_quiet_propose(page):
    reason = 'Nothing to propose yet: change a line first.'
    propose = page.get_by_role('button', name='Propose', exact=True)
    expect(propose).to_be_disabled()
    expect(propose).to_have_attribute('data-tip', reason)
    described = propose.get_attribute('aria-describedby')
    assert described, 'disabled Propose must reference its reason'
    description = page.locator('#' + described.split()[0])
    expect(description).to_have_text(reason)
    expect(propose).to_have_accessible_description(reason)
    clipped_or_hidden = '''e => {const s=getComputedStyle(e), r=e.getBoundingClientRect();return e.hidden ||
        s.display==='none' || s.visibility==='hidden' || !e.getClientRects().length ||
        (e.classList.contains('sr-only') && s.position==='absolute' && r.width<=1 && r.height<=1 &&
         s.overflow==='hidden' && s.clipPath==='inset(50%)');}'''
    assert description.evaluate(clipped_or_hidden), 'The Propose reason is visibly drawn beside the button'
    assert propose.locator('svg').count() == 0
    assert page.get_by_text(reason, exact=True).evaluate_all(
        '(nodes) => nodes.every(' + clipped_or_hidden + ')'), 'A visible reason text node remains'
    return propose


def test_rules_page_propose_reason_is_quiet_and_iconless_on_list_and_rule_page(browser, live_server, h):
    """Items 1-2: the disabled Propose reason is an accessible description, not visible helper copy or an icon."""
    flag(h, legacy=False)
    active = v2_rule(h, 'workload_limit', [v2_line({'max': {'value': 5, 'level': 'mandatory'}},
                                                   measure='duties', period={'kind': 'calendar_week'})])
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 900})
    try:
        navigate(page, 'rules')
        _assert_quiet_propose(page)
        navigate(page, 'rules/workload_limit')
        _assert_quiet_propose(page)
        line_id = active['definition']['rule']['lines'][0]['id']
        navigate(page, f'rules/workload_limit/{line_id}')
        panel = page.locator('.rc-line-panel')
        panel.get_by_label('Maximum', exact=True).fill('6')
        panel.get_by_role('button', name='Keep change', exact=True).click()
        panel.get_by_role('button', name='Close panel', exact=True).click()
        changed = page.get_by_role('button', name='Propose 1 change', exact=True)
        expect(changed).to_be_enabled()
        expect(changed).to_have_attribute('data-tip', re.compile(r'^Send 1 change to an approver'))
        assert not changed.get_attribute('aria-describedby')
        expect(page.get_by_text('Nothing to propose yet: change a line first.', exact=True)).not_to_be_visible()
        assert changed.locator('svg').count() == 0
    finally:
        context.close()


def test_rules_page_rule_attention_is_line_scoped_and_rule_rows_never_say_overlaps(browser, live_server, h):
    """Item 3: ordered winners stay quiet and the retired overlap kind is absent from the UI."""
    flag(h)
    end = str(date.fromisoformat(h.get('coordinator', '/api/rules/catalogue')['day']) + timedelta(days=11))
    group = tag_people(h, 'Synthetic rule group', [h.seed['people'][0]])
    where = _who('service_ids', h.seed['services'][:1])
    everyone = line({'max': {'value': 5, 'level': 'mandatory'}})
    personal = line({'max': {'value': 3, 'level': 'mandatory'}},
                    who=_who('person_ids', h.seed['people'][:1]), ends_on=end)
    ranked = line({'max': {'value': 4, 'level': 'mandatory'}}, who=_who('category_ids', [h.seed['category']]), where=where)
    tagged = line({'max': {'value': 6, 'level': 'mandatory'}}, who=_who('person_ids', [group]), where=where)
    catalogue_rule(h, 'duties_per_week', [everyone, personal, ranked, tagged])
    data = h.get('coordinator', '/api/rules/catalogue')
    issues = data['attention']
    overlaps = next(e for e in data['entries'] if e['id'] == 'duties_per_week')['line_overlaps']
    assert all(item['winner'] == personal['id'] for ident in (everyone['id'], personal['id']) for item in overlaps[ident])
    assert all(item['winner'] is None for ident in (ranked['id'], tagged['id']) for item in overlaps[ident])
    assert not [x for x in issues if x['kind'] == 'overlap'], issues
    assert any(x['kind'] == 'ending' for x in issues), issues
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 900})
    try:
        navigate(page, 'rules')
        rule = page.locator('li.rt-item').filter(has_text='Duties per week')
        expect(rule.locator('.rt-att')).to_have_text(re.compile(r'^⚠ \d+ lines? need attention$'))
        expect(rule.locator(':scope > .rt-row')).not_to_contain_text('Overlaps')
        rule.get_by_role('button', name='Duties per week lines', exact=True).click()
        for ident in (everyone['id'], personal['id']):
            expect(rule.locator(f'li.rt-ln[data-line="{ident}"] .rt-ln-flags')).not_to_contain_text('Overlaps')
        for ident in (ranked['id'], tagged['id']):
            expect(rule.locator(f'li.rt-ln[data-line="{ident}"] .rt-ln-flags')).to_contain_text('⚠ Overlaps')
        view(page, 'Needs attention')
        # Both issues of this rule are listed (both apply, ending soon); none says a line gives way.
        expect(page.locator('.rt-issue')).to_have_count(2)
        expect(page.locator('.rt-issue').filter(has_text='gives way to')).to_have_count(0)
        page.get_by_role('button', name=re.compile(r'^Filter')).click()
        pop = page.locator('.dt-pop')
        add_filter(pop)
        line_filter = pop.locator('.cond-line').last
        chooser = line_filter.get_by_label('Filter on', exact=True)
        if chooser.evaluate('s=>s.selectedOptions[0].textContent') != 'Issue':
            chooser.select_option(label='Issue')
        else:
            line_filter.locator('.pick-button').click()
        expect(page.locator('.picker')).to_be_visible()
        choices = page.locator('.picker [role=option]')
        expect(choices.filter(has_text='Overlap')).to_have_count(0)
        page.keyboard.press('Escape')
    finally:
        context.close()


def test_rules_page_usual_assignees_count_as_who_and_large_rule_meta_stays_one_line(browser, live_server, h):
    """Item 4: usual assignees are people, while the two largest seeded rows keep one readable meta line."""
    populated_catalogue(h, large_scope=True)  # turns the catalogue on itself; a second flag(h) is refused as stale
    people = h.seed['people'][:3]
    services = h.seed['services'][:1]
    catalogue_rule(h, 'duties_per_month', [
        line({'max': {'value': 5, 'level': 'mandatory'}}, where={'conditions': [
            {'field': 'usual_assignee_ids', 'values': people}, {'field': 'service_ids', 'values': services}]}),
        line({'max': {'value': 4, 'level': 'mandatory'}}, where={'conditions': [
            {'field': 'usual_assignee_ids', 'values': [people[0]]}]})])
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 900})
    try:
        navigate(page, 'rules')
        row = page.locator('li.rt-item').filter(has_text='Duties per month')
        text = row.locator('.rt-meta').inner_text()
        assert 'who:' in text and 'usual assignee' in text and 'where:' in text
        where = text.split('where:', 1)[1]
        assert 'usual assignee' not in where
        largest = page.locator('li.rt-item').evaluate_all('''rows => rows.map(row => {
            const m=row.querySelector('.rt-meta'), n=Number((m?.textContent||'').match(/^(\\d+)/)?.[1]||0);
            return {id:row.dataset.rule,n};}).sort((a,b)=>b.n-a.n).slice(0,2).map(x=>x.id)''')
        assert len(largest) == 2, largest
        for rule_id in largest:
            meta = page.locator(f'li.rt-item[data-rule="{rule_id}"] .rt-meta')
            result = meta.evaluate('''e => {const r=document.createRange();r.selectNodeContents(e);return {
                rects:r.getClientRects().length,height:e.getBoundingClientRect().height,
                lineHeight:parseFloat(getComputedStyle(e).lineHeight),clipped:e.scrollHeight>e.clientHeight+1};}''')
            assert result['rects'] == 1 and result['height'] <= result['lineHeight'] * 1.5 and not result['clipped'], result
    finally:
        context.close()


def test_rules_page_footer_status_icon_children_have_accessible_names(browser, live_server, h):
    """Item 9: service-status icons keep their names in the footer."""
    flag(h, legacy=False)
    v2_rule(h, 'workload_limit', [v2_line({'max': {'value': 5, 'level': 'mandatory'}},
                                           measure='duties', period={'kind': 'calendar_week'})])
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 900})
    try:
        navigate(page, 'rules')
        names = page.locator('.service-status > *').evaluate_all('''nodes => nodes.filter(n => n.querySelector('svg')).map(n => ({
            aria:n.getAttribute('aria-label'), text:(n.textContent||'').trim()}))''')
        assert names and all(item['aria'] for item in names), names
        indicators = page.locator('.service-status > *').filter(has=page.locator('svg'))
        for indicator in indicators.all():
            expect(indicator).to_have_accessible_name(re.compile(r'\S'))
        expect(page.get_by_role('button', name='Send feedback', exact=True)).to_be_visible()
        expect(page.locator('#connection')).to_have_attribute('role', 'status')
        expect(page.locator('.env-badge')).to_have_attribute('aria-label', re.compile(r'^(Test environment|Production)'))
        expect(page.locator('.env-badge')).to_have_accessible_name(re.compile(r'^(Test environment|Production)'))
    finally:
        context.close()


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_future_and_ended_lines_are_plain_secondary_text(browser, live_server, h, theme):
    flag(h, legacy=False)
    today = date.fromisoformat(h.get('coordinator', '/api/rules/catalogue')['day'])
    future = v2_line({'max': {'value': 3, 'level': 'mandatory'}}, who=_who('person_ids', h.seed['people'][:1]),
                     measure='duties', period={'kind': 'calendar_week'}, starts_on=str(today + timedelta(days=40)))
    ended = v2_line({'max': {'value': 2, 'level': 'mandatory'}}, who=_who('person_ids', h.seed['people'][1:2]),
                    measure='duties', period={'kind': 'calendar_week'}, starts_on=str(today - timedelta(days=40)),
                    ends_on=str(today - timedelta(days=1)))
    v2_rule(h, 'workload_limit', [v2_line({'max': {'value': 5, 'level': 'mandatory'}},
        measure='duties', period={'kind': 'calendar_week'}), future, ended])
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 900})
    try:
        page.emulate_media(color_scheme=theme)
        navigate(page, 'rules')
        page.evaluate('t => {document.documentElement.dataset.theme=t;}', theme)
        item = page.locator('.rt-item').filter(has=page.get_by_role('button', name='Workload limit lines', exact=True))
        item.get_by_role('button', name='Workload limit lines', exact=True).click()

        def plain(ident, words):
            row = item.locator(f'li.rt-ln[data-line="{ident}"]')
            flag = row.locator('.rt-ln-flags > span.muted')
            expect(flag).to_have_text(words)
            expect(row.locator('.rt-ln-flags .badge').filter(has_text=words)).to_have_count(0)
            style = row.evaluate('''r => {const a=getComputedStyle(r.querySelector('.rt-ln-flags > .muted')),
                b=getComputedStyle(r.querySelector('.rt-ln-when'));return {size:a.fontSize,colour:a.color,whenSize:b.fontSize,whenColour:b.color};}''')
            assert style['size'] == style['whenSize'] == '12px' and style['colour'] == style['whenColour'], style

        plain(future['id'], 'Starts later')
        page.get_by_role('button', name=re.compile(r'^Filter')).click()
        pop = page.locator('.dt-pop')
        add_filter(pop)
        row = pop.locator('.cond-line').last
        current = row.get_by_label('Filter on', exact=True).evaluate('s => s.selectedOptions[0].textContent')
        pick(page, row, None if current == 'Lines: Valid' else 'Lines: Valid', ['Ended'])
        page.keyboard.press('Escape')
        plain(ended['id'], 'Ended')
    finally:
        context.close()


def _workload_line(value, **fields):
    return v2_line({'max': {'value': value, 'level': 'mandatory'}},
                   measure='duties', period={'kind': 'calendar_week'}, **fields)


def test_shadowed_attention_is_grouped_searchable_and_removes_the_right_line(browser, live_server, h):
    flag(h, legacy=False)
    day = h.get('coordinator', '/api/rules/catalogue?version=2')['day']
    member = h.seed['people'][0]
    roster = h.get('admin', '/api/catalogues?include=memberships')['memberships']
    old_rank = next(row for row in roster if row['person_id'] == member and row['category_id'] == h.seed['category'])
    h.command('admin', 'membership.update_dates', {'ends_on': day, 'reason': 'Split ranks for the rule fixture'},
              old_rank['id'], old_rank['revision'])
    h.command('admin', 'membership.create', {'person_id': member, 'category_id': h.seed['trainee_category'],
                                             'starts_on': day})
    anchor = _workload_line(5)
    ranks = [_workload_line(value, who=_who('category_ids', [rank]))
             for value, rank in [(4, h.seed['category']), (3, h.seed['trainee_category'])]]
    v2_rule(h, 'workload_limit', [anchor, *ranks])
    issues = h.get('coordinator', '/api/rules/catalogue?version=2')['attention']
    shadowed = next(x for x in issues if x['kind'] == 'shadowed' and x['catalogue_id'] == 'workload_limit')
    assert shadowed['line_ids'] == [anchor['id']] and shadowed['people'] == len(h.seed['people'])
    assert shadowed['facts'] == {'winners': [row['id'] for row in ranks], 'numbers': ['max']}
    remove = next(f for f in shadowed['fixes'] if f['kind'] == 'remove')
    assert remove['line_id'] == anchor['id'] and remove['ops'] == [{'line_id': anchor['id'], 'remove': True}]

    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 900})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('console', lambda message: errors.append(message.text) if message.type == 'error' else None)

    def add_future_kind(route):
        response = route_fetch(route)
        body = response.json()
        body['attention'].append({**shadowed, 'id': 'future-issue-fixture', 'kind': 'future_issue',
                                  'facts': {}, 'fixes': [shadowed['fixes'][0]]})
        route.fulfill(response=response, json=body)

    try:
        page.route(re.compile(r'/api/rules/catalogue\?version=2&include_hidden=true$'), add_future_kind)
        navigate(page, 'rules')
        view(page, 'Needs attention')
        row = page.locator(f'tr.rt-issue[data-issue="{shadowed["id"]}"]')
        expect(row.locator('xpath=preceding-sibling::tr[contains(@class,"rt-kind")][1]')).to_have_text('Applies to no one 1')
        words = f'Line 1 applies to no one today: line 2 and line 3 replace every number it sets (max), for all {len(h.seed["people"])} people'
        expect(row.locator('td[data-col=issue-what]')).to_have_text(words)
        future = page.locator('tr.rt-issue[data-issue="future-issue-fixture"]')
        expect(future.locator('xpath=preceding-sibling::tr[contains(@class,"rt-kind")][1]')).to_have_text('Future issue 1')
        expect(future.locator('td[data-col=issue-what]')).to_have_text('Future issue')

        page.get_by_role('button', name=re.compile(r'^Group\b')).click()
        page.get_by_role('menuitemradio', name='No grouping').click()
        expect(page.locator('tr.dt-group.rt-kind')).to_have_count(0)
        expect(row.locator('td[data-col=issue-what]')).to_have_text(words)
        search = page.get_by_role('searchbox', name='Search issues')
        search.fill('Synthetic nonmatching text')
        search.press('Enter')
        expect(row).to_have_count(0)
        search.fill('Workload limit')
        search.press('Enter')
        expect(row).to_be_visible()
        expect(row.get_by_role('button', name='Remove line 0', exact=True)).to_have_count(0)
        row.get_by_role('button', name='Remove line 1', exact=True).click()
        expect(row).to_have_count(0)
        expect(page.locator('.toast')).to_contain_text('Remove line 1: added to the proposal.')
        # The removal is a change not yet proposed: going to another address asks first, and going on keeps it.
        page.evaluate("location.hash='rules/workload_limit'")
        page.get_by_role('dialog', name='Leave this page?').get_by_role('button', name=re.compile(r'^Go to Rules')).click()
        expect(page.locator('#main')).to_have_attribute('data-loaded-tab', 'workload_limit')
        lines = page.locator('[data-table=rule-line-db] tr[data-row]')
        expect(lines).to_have_count(len(ranks))
        expect(lines.and_(page.locator(f'[data-line="{anchor["id"]}"]'))).to_have_count(0)
        for rank in ranks:
            expect(lines.and_(page.locator(f'[data-line="{rank["id"]}"]'))).to_be_visible()
        assert not errors, errors
    finally:
        context.close()


def test_winning_exception_needs_no_warning_or_acknowledgement(browser, live_server, h):
    flag(h, legacy=False)
    anchor = _workload_line(5)
    personal = _workload_line(6, who=_who('person_ids', h.seed['people'][:1]))
    v2_rule(h, 'workload_limit', [anchor, personal])
    detail = h.get('coordinator', '/api/rules/catalogue/workload_limit/detail?version=2')
    assert detail['check']['line_replaces'][personal['id']] == [{'replaced_line_id': anchor['id'], 'numbers': ['max']}]
    assert detail['check']['overlaps'] and detail['check']['warnings'] == []

    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 900})
    sent = []

    def record_proposal(request):
        if request.method == 'POST' and request.url.endswith('/api/commands'):
            command = request.post_data_json
            if command['kind'] == 'rule.propose':
                sent.append(command['payload'])

    page.on('request', record_proposal)
    try:
        navigate(page, 'rules/workload_limit')
        lines = page.locator('[data-table=rule-line-db] tr[data-row]')
        for part in (anchor, personal):
            row = lines.and_(page.locator(f'[data-line="{part["id"]}"]'))
            expect(row.locator('td[data-col=check]')).to_be_visible()
            expect(row.locator('td[data-col=check] .rc-overlap')).to_have_count(0)
        expect(page.locator('.rc-checkbar')).to_be_empty()
        expect(page.locator('.app-footer')).not_to_contain_text(re.compile('overlap', re.I))
        expect(page.get_by_role('button', name='That is right')).to_have_count(0)
        page.get_by_role('button', name=re.compile(r'^Filter\b')).click()
        pop = page.locator('.dt-pop')
        add_filter(pop)
        pick(page, pop.locator('.cond-line').last, 'Overlaps', ['No overlap'])
        page.keyboard.press('Escape')
        expect(lines).to_have_count(2)

        navigate(page, f'rules/workload_limit/{personal["id"]}')
        panel = page.locator('.rc-line-panel')
        panel.get_by_label('Maximum', exact=True).fill('7')
        panel.get_by_role('button', name='Keep change', exact=True).click()
        panel.get_by_role('button', name='Close panel', exact=True).click()
        page.get_by_role('button', name='Propose 1 change', exact=True).click()
        dialog = page.get_by_role('dialog', name=re.compile(r'^Propose'))
        expect(dialog).not_to_contain_text('Lines that give way to more specific ones')
        expect(dialog.get_by_role('checkbox')).to_have_count(0)
        dialog.get_by_label('Reason', exact=True).fill('Adjust the personal limit')
        submit = dialog.get_by_role('button', name='Propose', exact=True)
        expect(submit).to_be_enabled()
        submit.click()
        waiting = page.locator('.rc-waiting')
        expect(waiting).to_be_visible()
        # No warning for the winning exception; the waiting version still says which line gives way (release decision,
        # 19 Nov 2026: the approver sees it as information).
        expect(waiting.locator('.rc-prop-warnings')).to_have_count(0)
        expect(waiting.locator('.rc-prop-gives')).to_contain_text('gives way to')
        assert len(sent) == 1 and sent[0]['acknowledged_overlaps'] == []
        saved = h.get('coordinator', '/api/rules/catalogue/workload_limit/detail?version=2')['entry']['proposed']
        assert saved['check']['overlaps'] and saved['check']['warnings'] == []
    finally:
        context.close()
