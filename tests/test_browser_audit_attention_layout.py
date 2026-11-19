"""Issue views keep long line facts separate and reuse the common collection spacing."""
import re
from datetime import date, timedelta

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, sign_in  # noqa: F401
from .test_browser_rule_catalogue import catalogue_rule, flag, line, shot
from .test_browser_rules_column_menus import column
from .test_browser_toolbar_layout import GEOMETRY

pytestmark = pytest.mark.browser

PERSON = 'Dr. Jules Xu'
RANK = 'Senior Department A Training Registrar'
CLINIC = 'Department A Specialist Continuity Clinic'


def issue_fixture(h):
    flag(h)
    catalogues = h.get('admin', '/api/catalogues')
    for kind, ident, label in [('people', h.seed['people'][0], PERSON),
                               ('categories', h.seed['category'], RANK),
                               ('services', h.seed['services'][0], CLINIC)]:
        record = next(x for x in catalogues[kind] if x['id'] == ident)
        value = {'display_name': label} if kind == 'people' else {'name': label, 'starts_on': record['starts_on']}
        h.configure(kind, value, record['id'], record['revision'])
    today = date.fromisoformat(h.get('coordinator', '/api/rules/catalogue')['day'])
    numbers = {'min': {'value': 111, 'level': 'mandatory'}, 'max': {'value': 222, 'level': 'mandatory'},
               'target': {'value': 166, 'level': 'medium'}}
    where = {'conditions': [{'field': 'service_ids', 'values': h.seed['services']}]}
    lines = [line(numbers, who={'conditions': [{'field': field, 'values': values}]}, where=where,
                  ends_on=str(today + timedelta(days=11)))
             for field, values in [('person_ids', h.seed['people'][:2]), ('category_ids', [h.seed['category']])]]
    catalogue_rule(h, 'duties_per_week', lines)
    issues = h.get('coordinator', '/api/rules/catalogue')['attention']
    # Both views have real issue rows. Proposal acknowledgements can also add multi-line rows to Acknowledged.
    rank_issue = next(x for x in issues if x['kind'] == 'ending' and x['line_ids'] == [lines[1]['id']])
    h.command('coordinator', 'rule.acknowledge', {'fingerprints': [rank_issue['fingerprint']], 'note': 'Layout fixture'})


def view(page, label):
    page.locator('#main button.dt-saved').click()
    page.get_by_role('menuitemradio', name=re.compile('^' + re.escape(label) + r'\b')).first.click()


def assert_collection_spacing(page):
    geometry = page.evaluate(GEOMETRY, ['#main .dt-toolbar', '#main .rt-body'])
    assert abs(geometry['left']) <= 1 and abs(geometry['right']) <= 1, geometry
    assert 12 <= geometry['gap'] <= 16, geometry


def assert_line_facts_are_separate(page):
    result = page.locator('.rt-iline').evaluate_all(r'''lines => {
      const failures=[];
      let facts=0;
      for (const [index,line] of lines.entries()) {
        const owner=line.closest('td'),cell=owner.getBoundingClientRect();
        const slots=[...line.children].map(n=>n.getBoundingClientRect());
        if(slots.some((r,i)=>i&&r.left<slots[i-1].right-1))
          failures.push({line:index,reason:'line facts no longer scan in separate columns'});
        if(owner.classList.contains('dt-wrap'))for(const item of line.querySelectorAll('.pill-item')) {
          const pill=item.querySelector(':scope > .rp');if(!pill)continue;
          const box=item.getBoundingClientRect(),visible=pill.getBoundingClientRect();
          if(visible.width&&visible.height&&box.width>visible.width+1)
            failures.push({line:index,text:pill.textContent.trim(),reason:'wrapped pill reserves empty horizontal space'});
        }
        const nodes=[...line.querySelectorAll(':scope > .rt-n, :scope > .rp, :scope > .rule-num, .pill-item:not(.pill-over) > *, .pill-more')]
          .map(n=>({text:n.textContent.trim(),r:n.getBoundingClientRect()})).filter(n=>n.r.width&&n.r.height);
        facts+=nodes.length;
        const walker=document.createTreeWalker(line.querySelector('.rule-num'),NodeFilter.SHOW_TEXT);
        let text;
        while(text=walker.nextNode())for(const token of text.textContent.matchAll(/\d+(?:\.\d+)?[!↰]*/g)){
          const range=document.createRange();range.setStart(text,token.index);range.setEnd(text,token.index+token[0].length);
          if(range.getClientRects().length>1)failures.push({line:index,text:token[0],reason:'split number'});
        }
        for (let i=0;i<nodes.length;i++) {
          const a=nodes[i];
          if(a.r.left<cell.left-1||a.r.right>cell.right+1||a.r.top<cell.top-1||a.r.bottom>cell.bottom+1)
            failures.push({line:index,text:a.text,reason:'outside its cell'});
          for(let j=i+1;j<nodes.length;j++) {
            const b=nodes[j];
            if(Math.min(a.r.right,b.r.right)-Math.max(a.r.left,b.r.left)>1 &&
               Math.min(a.r.bottom,b.r.bottom)-Math.max(a.r.top,b.r.top)>1)
              failures.push({line:index,a:a.text,b:b.text,reason:'facts overlap'});
          }
        }
      }
      return {lines:lines.length,facts,failures,viewport:innerWidth,scrollWidth:document.documentElement.scrollWidth};
    }''')
    assert result['lines'] > 0 and result['facts'] >= 4, result
    assert not result['failures'], result
    assert result['scrollWidth'] <= result['viewport'], result


# The anti-diagonal (Design Lead, 10 Nov 2026): the test below runs the diagonal, so together the two issue-view tests
# cover all four width x theme cases for both roles. Spacing and geometry only; no colour is read.
@pytest.mark.parametrize('width,scheme', [(1440, 'dark'), (390, 'light')])
@pytest.mark.parametrize('role', ['coordinator', 'auditor'])
def test_issue_facts_and_collection_spacing_in_both_issue_views(browser, live_server, h, width, scheme, role):
    issue_fixture(h)
    context, page = sign_in(browser, live_server, role, {'width': width, 'height': 900})
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, 'rules')
        view(page, 'All rules')
        assert_collection_spacing(page)
        for label in ['Needs attention', 'Acknowledged']:
            view(page, label)
            expect(page.locator('.rt-issue').first).to_be_visible()
            assert_collection_spacing(page)
            assert_line_facts_are_separate(page)
            # A short issue list keeps its natural height rather than stretching its rows to fill the Rules pane.
            short = page.locator('.rt-scroll').evaluate('''e => ({view:e.clientHeight,total:e.scrollHeight,
                table:e.querySelector('table').getBoundingClientRect().height})''')
            if short['total'] <= short['view'] + 1:
                assert short['view'] <= short['table'] + 2, short
            expect(page.locator('.rt-iline').filter(has_text=PERSON if label == 'Needs attention' else RANK).first).to_be_visible()
            expect(page.locator('.rt-ilines').first).to_contain_text(CLINIC)
            expect(page.locator('.rt-ilines .rule-num').first).to_contain_text('111 – 222 Mandatory → 166 Advisory · medium')
            if role == 'auditor' or label == 'Acknowledged':
                choices = page.locator('.rt-choice')
                expect(choices.filter(has_text=re.compile('acknowledge', re.I))).to_have_count(0)
                expect(choices.filter(has_text=re.compile('^Let it end$'))).to_have_count(1)
                if label == 'Acknowledged':
                    # A person line outranking a rank line is an ordered exception, never an overlap to acknowledge.
                    expect(choices.filter(has_text=re.compile('^Keep lines$'))).to_have_count(0)
            if role == 'auditor':
                expect(page.locator('.rt-issues input[type=checkbox]')).to_have_count(0)
                expect(page.locator('.rt-best > button, .rt-fixes button, .rt-acked button')).to_have_count(0)
            else:
                # Group boxes retain the same checkbox gutter as regular rows and the header.
                lefts = page.locator('.rt-issues input[type=checkbox]').evaluate_all(
                    'nodes => nodes.map(n=>n.getBoundingClientRect().left)')
                assert max(lefts) - min(lefts) <= 1, lefts
            shot(page, f'attention-layout-{role}-{label.replace(" ", "-").lower()}-{width}-{scheme}')
            # The overflow control remains reachable, including after scrolling the phone's table.
            more = page.locator('.rt-ilines .pill-more').first
            more.click()
            expect(page.get_by_role('dialog', name=re.compile('^All (Who|Where)$'))).to_be_visible()
            page.keyboard.press('Escape')
            expect(page.get_by_role('dialog', name=re.compile('^All (Who|Where)$'))).to_have_count(0)
            if role == 'coordinator' and label == 'Needs attention':
                action = page.get_by_role('button', name='Let it end · acknowledge…', exact=True).first
                expect(action).to_have_text('Let it end · acknowledge…')
                expect(page.locator('.rt-issues').get_by_role('button', name='Apply', exact=True)).to_have_count(0)
                action.click()
                confirmation = page.get_by_role('dialog', name='Acknowledge 1 issue', exact=True)
                expect(confirmation).to_be_visible()
                expect(confirmation).to_contain_text('The rules and rota checks do not change.')
                confirmation.get_by_role('button', name='Cancel', exact=True).click()
                expect(confirmation).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', [(1440, 'light'), (390, 'dark')])
@pytest.mark.parametrize('role', ['coordinator', 'auditor'])
def test_issue_column_and_view_wrap_controls_are_shared_persistent_and_separate_from_rules(browser, live_server, h, width, scheme, role):
    issue_fixture(h)
    before = h.get('auditor', '/api/rules/catalogue')
    context, page = sign_in(browser, live_server, role, {'width': width, 'height': 900})
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, 'rules')
        # the Rules list has no column menu; Needs attention below keeps the shared menus.
        expect(page.locator('.rt-scroll thead')).to_have_count(0)
        view(page, 'Needs attention')
        heads = page.locator('.rt-issues th[data-col]')
        expect(heads).to_have_text(['Rule', 'Lines', 'What happens', 'Recommendation'])
        expect(page.locator('.rt-issues .dt-wrap')).to_have_count(0)

        what = page.locator('.rt-issues thead').get_by_role('button', name='What happens', exact=True)
        what.focus()
        page.keyboard.press('Enter')
        menu = page.get_by_role('menu', name='What happens column', exact=True)
        expect(menu.get_by_role('menuitemcheckbox', name='Wrap this column', exact=True)).to_have_attribute('aria-checked', 'false')
        menu.get_by_role('menuitemcheckbox', name='Wrap this column', exact=True).click()
        expect(what).to_be_focused()
        expect(page.locator('.rt-issues th[data-col=issue-what]')).to_have_class(re.compile('dt-wrap'))
        expect(page.locator('.rt-issue [data-col=issue-what]').first).to_have_class(re.compile('dt-wrap'))
        expect(page.locator('.rt-issues th[data-col=issue-lines]')).not_to_have_class(re.compile('dt-wrap'))

        column(page, 'Lines').get_by_role('menuitemcheckbox', name='Wrap all columns', exact=True).click()
        expect(page.locator('.rt-issues th.dt-wrap')).to_have_count(4)
        # The shared pills reveal the complete scopes; wrapping does not discard facts to make the row fit.
        expect(page.locator('.rt-ilines .pill-over').first).to_be_visible()
        expect(page.locator('.rt-ilines .pill-more').first).to_be_hidden()
        assert_line_facts_are_separate(page)
        page.get_by_role('button', name='View settings', exact=True).click()
        settings = page.get_by_role('dialog', name='View settings', exact=True)
        expect(settings.get_by_role('switch', name='Wrap all columns', exact=True)).to_be_checked()
        page.keyboard.press('Escape')
        shot(page, f'attention-wrapped-{role}-{width}-{scheme}')

        page.reload()
        expect(page.locator('.rt-issues th.dt-wrap')).to_have_count(4)
        view(page, 'Acknowledged')
        expect(page.locator('.rt-issues th.dt-wrap')).to_have_count(4)
        page.get_by_role('button', name='View settings', exact=True).click()
        settings.get_by_role('switch', name='Wrap all columns', exact=True).uncheck()
        expect(page.locator('.rt-issues .dt-wrap')).to_have_count(0)
        view(page, 'All rules')
        expect(page.locator('.rt-scroll thead')).to_have_count(0)
        assert h.get('auditor', '/api/rules/catalogue') == before
    finally:
        context.close()
