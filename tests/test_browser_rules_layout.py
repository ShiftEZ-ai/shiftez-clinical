"""The Rules page's layout: it scrolls inside its own area (the top bar, rail and footer stay; nothing chains to the
document), the rule sheet never scrolls sideways, and the sheet's draft section reports on the draft being worked on."""
import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, open_rota, open_rule  # noqa: F401
from .test_rule_templates import activate, propose

pytestmark = pytest.mark.browser


def capacity(h):
    """A rule for its lines only whose lines name ranks, clinics, duty types, days and hours (the wide lines table)."""
    cats = [c['id'] for c in h.get('coordinator', '/api/catalogues')['categories']]
    lines = [{'category_ids': cats, 'service_ids': h.seed['services'], 'assignment_type_ids': [h.seed['type']], 'weekdays': [0, 1, 2, 3, 6],
              'start_time': '08:00', 'end_time': '16:00', 'amount': n % 4, 'starts_on': '2026-10-01', 'ends_on': '2026-12-01'} for n in range(5)]
    return activate(h, propose(h, {'kind': 'maximum_participants', 'maximum': 0}, name='UCC capacity', listed_only=True, per_person=lines))


@pytest.mark.parametrize('width', [1440, 390])
def test_rule_sheet_with_a_wide_lines_table_never_scrolls_sideways(browser, live_server, h, width):
    capacity(h)
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': 900})
    try:
        navigate(page, 'rules')
        sheet = open_rule(page, 'UCC capacity')
        expect(sheet.locator('.rule-lines')).to_be_visible()
        assert sheet.evaluate('d => d.scrollWidth <= d.clientWidth'), sheet.evaluate(
            "d => [...d.querySelectorAll('*')].filter(n => n.getBoundingClientRect().right > d.getBoundingClientRect().right + 1).slice(0, 5).map(n => n.tagName + '.' + n.className)")
        assert sheet.locator('.rule-lines .table-scroll').evaluate('s => s.scrollWidth <= s.clientWidth')
    finally:
        context.close()


@pytest.mark.parametrize('width', [1440, 390])
def test_rules_scroll_inside_the_page_not_the_document(browser, live_server, h, width):
    for n in range(30):
        activate(h, propose(h, {'kind': 'min_rest', 'hours': 10 + n % 4}, name=f'Fictional rest rule {n:02}'))
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': 838})
    try:
        navigate(page, 'rules')
        expect(page.locator('.rule-item')).to_have_count(31)  # Authored rest rules plus seeded Fair share.
        authored = page.locator('.rule-item').filter(has_text='Fictional rest rule')
        expect(authored.locator('.rl-mark .sr-only')).to_have_text(['Mandatory'] * 30)
        assert 'Mandatory' in authored.last.aria_snapshot()  # Clipping the hidden name must not remove it from AX.
        assert page.evaluate('document.documentElement.scrollHeight === document.documentElement.clientHeight')
        page.mouse.move(width // 2, 500)
        page.mouse.wheel(0, 2500)
        page.wait_for_function("() => document.querySelector('#main').scrollTop > 0")
        assert page.evaluate('document.scrollingElement.scrollTop') == 0
        assert page.evaluate("document.querySelector('.topbar').getBoundingClientRect().top") == 0
        assert page.evaluate("document.querySelector('.app-footer').getBoundingClientRect().bottom") <= 838
        # The open sheet keeps its scroll: the page behind does not move.
        sheet = open_rule(page, 'Fictional rest rule 29')
        behind = page.evaluate("document.querySelector('#main').scrollTop")
        box = sheet.bounding_box()
        page.mouse.move(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
        for _ in range(3):
            page.mouse.wheel(0, 1500)
        expect(sheet).to_have_js_property('scrollTop', sheet.evaluate('d => d.scrollHeight - d.clientHeight'))
        assert page.evaluate("[document.scrollingElement.scrollTop, document.querySelector('#main').scrollTop]") == [0, behind]
        assert page.evaluate('document.documentElement.scrollHeight === document.documentElement.clientHeight')
    finally:
        context.close()


def test_draft_section_follows_the_schedule_then_a_draft_with_sessions_and_switches(browser, live_server, h):
    a = h.seed['people'][0]
    activate(h, propose(h, {'kind': 'max_hours', 'hours': 6, 'window': 'day'}, name='Daily hours'))
    september = h.workspace('September draft', '2026-09-01', '2026-10-01')
    h.shift(september, '2026-09-06T08:00:00+03:00', '2026-09-06T16:00:00+03:00', people=[a])
    empty = h.workspace('October draft', '2026-10-01', '2026-11-01')
    context, page = sign_in(browser, live_server, 'coordinator')
    reads = []
    try:
        page.clock.set_fixed_time('2026-11-04T09:00:00+03:00')
        page.on('request', lambda request: reads.append(request.url) if request.url.endswith('/rule-map') else None)
        open_rota(page, september['id'])
        reads.clear()
        navigate(page, 'rules')
        sheet = open_rule(page, 'Daily hours')
        section = sheet.locator('.rl-sec').filter(has=page.get_by_role('heading', name='In September draft'))
        show = section.get_by_role('button', name='Show effect in September draft', exact=True)
        expect(show).to_be_visible()
        assert not reads
        show.click()
        expect(section.locator('.rl-day.short')).to_have_count(1)   # 8 hours on 6 Sep
        section.get_by_label('Draft').select_option(label='October draft')
        section = sheet.locator('.rl-sec').filter(has=page.get_by_role('heading', name='In October draft'))
        expect(section.get_by_text('No duties yet.', exact=True)).to_be_visible()
        expect(section.locator('.rl-day.short')).to_have_count(0)
        sheet.get_by_role('heading', level=2).click()   # out of the select
        page.keyboard.press('Escape')
        expect(sheet).to_have_count(0)
        open_rota(page, empty['id'])
        reads.clear()
        navigate(page, 'rules')
        sheet = open_rule(page, 'Daily hours')
        expect(sheet.get_by_role('heading', name='In October draft', exact=True)).to_be_visible()
        show = sheet.get_by_role('button', name='Show effect in October draft', exact=True)
        expect(show).to_be_visible()
        assert not reads
        show.click()
        expect(sheet.get_by_text('No duties yet.', exact=True)).to_be_visible()
    finally:
        context.close()


def test_family_rows_carry_no_unexplained_hue(browser, live_server, h):
    """RU-05: a family row is its name and count; hues belong to duty types (DESIGN), so no coloured square is drawn."""
    from .test_browser_rule_catalogue import flag
    flag(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        family = page.locator('#main section.rt-q').first
        expect(family).to_be_visible()
        expect(family.locator('.rt-qhead h3')).to_be_visible()
        expect(family.locator('.rt-qhead .dt-group-count')).to_be_visible()
    finally:
        context.close()
