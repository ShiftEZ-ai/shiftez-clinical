"""Copy-hours and Split are edits even when no field is typed; cancelling preserves saved data."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, sign_in, toolbar, choose_radio  # noqa: F401
from .test_browser_shared_patch_regressions import DISPLAYS, keep_shot
from .test_browser_qa_planner import ANCHORED, settled

pytestmark = [pytest.mark.browser, pytest.mark.parametrize('h', [pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)]


@pytest.mark.parametrize('width,scheme', DISPLAYS)
@pytest.mark.parametrize('kind', ['operating_calendars', 'assignment_types'], ids=['copy-hours', 'split'])
def test_structural_edit_cancel_and_escape_guards_preserve_saved_data(browser, live_server, h, width, scheme, kind):
    if kind == 'operating_calendars':
        record = h.configure(kind, {'name': 'Fictional copy-hours regression', 'starts_on': '2020-01-01',
            'definition': {'timezone': 'Asia/Riyadh', 'week': [
                {'weekday': day, 'windows': [{'start_time': '08:00', 'end_time': '16:00' if day == 6 else '12:00'}]}
                for day in range(7)]}})['record']
        path, title = 'configuration/opening-hours', 'Edit opening hours'
        action = re.compile('^Copy .* hours to the other working days$')
    else:
        record = h.configure(kind, {'name': 'Fictional split regression', 'starts_on': '2020-01-01',
            'definition': {'requirements': [{'minimum': 1}], 'maximum_duration_minutes': 480}})['record']
        path, title = 'configuration/assignment_types', 'Edit duty type'
        action = '+ Split at another time'
    before = h.get('admin', '/api/catalogues')[kind]
    context, page = sign_in(browser, live_server, 'admin', viewport={'width': width, 'height': 900})
    try:
        page.emulate_media(color_scheme=scheme)
        for dismiss in ('Cancel', 'Escape', 'route'):
            navigate(page, path + '?open=' + record['id'])
            dialog = page.get_by_role('dialog', name=title, exact=True)
            expect(dialog).to_be_visible()
            # Review is ready before the only edit; observe a fresh preview with changed values afterwards.
            expect(dialog.locator('.configuration-impact-status').first).to_contain_text('checked', timeout=15000)
            with page.expect_request(lambda request: '/api/catalogue/preview' in request.url and request.method == 'POST') as recalculated:
                dialog.get_by_role('button', name=action, exact=isinstance(action, str)).click()
            edited = recalculated.value.post_data_json['payload']['value']['definition']
            if kind == 'assignment_types':
                assert len(edited['requirements']) == 2
                expect(dialog.get_by_label('Requirement to minute', exact=True).first).not_to_have_value('')
            else:
                original = {d['weekday']: d['windows'] for d in record['definition']['week']}
                assert any(d['windows'] != original[d['weekday']] for d in edited['week'])
            if dismiss == 'Cancel':
                dialog.get_by_role('button', name='Cancel', exact=True).click()
            elif dismiss == 'Escape':
                page.keyboard.press('Escape')
            else:
                page.evaluate("location.hash='home'")
            expect(dialog.get_by_role('alert')).to_contain_text('Not saved.')
            dialog.get_by_role('button', name='Keep editing', exact=True).click()
            expect(dialog.get_by_role('alert')).to_have_count(0)
            expect(dialog).to_be_visible()
            keep_shot(page, f'{kind}-{dismiss}-{width}-{scheme}')
            if dismiss == 'route':
                page.evaluate("location.hash='home'")
            elif dismiss == 'Escape':
                page.keyboard.press('Escape')
            else:
                dialog.get_by_role('button', name='Cancel', exact=True).click()
            expect(dialog.get_by_role('alert')).to_contain_text('Not saved.')
            dialog.get_by_role('button', name='Discard changes', exact=True).click()
            expect(dialog).to_have_count(0)
            if dismiss == 'route':
                expect(page.locator('#main[data-loaded-view=home]')).to_be_visible()
            assert h.get('admin', '/api/catalogues')[kind] == before
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', DISPLAYS)
def test_open_schedule_view_popover_survives_layout_changes_and_restores_focus(browser, live_server, h, width, scheme):
    w = h.shift(h.workspace('Fictional anchored layout regression'))['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': width, 'height': 900})
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, f"schedule/list/2026-10-04?edit={w['id']}")
        settled(page)
        bar = page.locator('.planner-toolbar')
        pop = toolbar(bar, 'View settings')
        for layout in ('Calendar', 'List', 'Coverage'):
            choose_radio(pop, 'Layout', layout)
            settled(page)
            page.wait_for_function(ANCHORED)
            expect(pop).to_be_visible()
            expect(pop.get_by_role('radiogroup', name='Layout').get_by_role('radio', name=layout, exact=True)).to_have_attribute('aria-checked', 'true')
            pop.evaluate('n=>Promise.allSettled(n.getAnimations({subtree:true}).map(a=>a.finished))')
            assert pop.evaluate('n=>{const r=n.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.bottom<=innerHeight&&n.scrollWidth<=n.clientWidth;}')
            if width == 390:
                assert pop.evaluate('''n=>{const r=n.getBoundingClientRect(),footer=parseFloat(getComputedStyle(document.documentElement)
                    .getPropertyValue('--status-footer-height'))||0;return Math.abs(r.left)<=1&&Math.abs(r.right-innerWidth)<=1
                    &&Math.abs(r.bottom-(innerHeight-footer))<=1;}''')
        page.keyboard.press('Escape')
        expect(pop).to_have_count(0)
        expect(bar.get_by_role('button', name=re.compile('^View settings'))).to_be_focused()
        assert h.detail(w)['workspace']['revision'] == w['revision']
    finally:
        context.close()


def test_popover_handover_repositions_when_new_anchor_returns_to_the_old_coordinates(browser, live_server, h):
    """A redraw can place a new button temporarily lower before it returns to the old button's position."""
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        page.evaluate('''async()=>{const{el,popover}=await window.importBundle('dom.js');
            const a=el('button',{id:'fixture-anchor-a',type:'button'},'Fixture tools');
            for(const[k,v]of Object.entries({position:'fixed',left:'200px',top:'200px'}))a.style.setProperty(k,v);
            document.body.append(a);popover(a,p=>p.append(el('p',{},'Shared toolbar redraw')),
                {label:'Fixture tools',key:'fixture-handover'});}''')
        panel = page.get_by_role('dialog', name='Fixture tools', exact=True)
        expect(panel).to_be_visible()
        panel.evaluate('n=>Promise.allSettled(n.getAnimations().map(a=>a.finished))')
        page.evaluate('''async()=>{const{adoptPopover}=await window.importBundle('dom.js');
            const a=document.querySelector('#fixture-anchor-a'),b=a.cloneNode(true);b.id='fixture-anchor-b';
            b.style.setProperty('top','236px');document.body.append(b);a.remove();
            adoptPopover('fixture-handover',b);b.style.setProperty('top','200px');}''')
        page.wait_for_function('''()=>{const p=document.querySelector('.popover'),a=document.querySelector('#fixture-anchor-b');
            const r=p.getBoundingClientRect(),b=a.getBoundingClientRect();return r.top>=b.bottom&&r.top<=b.bottom+12;}''')
        page.keyboard.press('Escape')
        expect(panel).to_have_count(0)
        expect(page.locator('#fixture-anchor-b')).to_be_focused()
    finally:
        context.close()


def test_a_popover_whose_new_button_comes_after_a_read_stays_open_until_it_does(browser, live_server, h):
    """Auto-schedule's panel swaps in as a skeleton and builds its People to schedule button only after its jobs read
    (run 42077364181, browser 15): an open builder waits for that button (awaitAdoption) instead of closing as an
    orphan when the page swaps in, moves to it when it comes, and closes if none comes."""
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        panel = page.get_by_role('dialog', name='Fixture people', exact=True)
        for comes in (True, False):
            page.evaluate('''async()=>{const{el,popover,awaitAdoption,closeOrphanPopover}=await window.importBundle('dom.js');
                const a=el('button',{id:'fixture-late-a',type:'button'},'Fixture people');document.body.append(a);
                popover(a,p=>p.append(el('p',{},'Builder')),{label:'Fixture people',key:'fixture-late'});
                window.fixtureDone=awaitAdoption('fixture-late');a.remove();closeOrphanPopover();}''')
            expect(panel).to_be_visible()  # the page swapped in before its new button exists: still open
            if comes:
                page.evaluate('''async()=>{const{el,adoptPopover}=await window.importBundle('dom.js');
                    const b=el('button',{id:'fixture-late-b',type:'button'},'Fixture people');document.body.append(b);
                    queueMicrotask(()=>adoptPopover('fixture-late',b));window.fixtureDone();}''')
                expect(page.locator('#fixture-late-b')).to_have_attribute('aria-expanded', 'true')
                expect(panel).to_be_visible()
                page.keyboard.press('Escape')
            else:
                page.evaluate('window.fixtureDone()')
            expect(panel).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize('key', [None, 'fixture-not-awaited'], ids=['keyless', 'keyed'])
def test_an_orphan_popover_closes_when_no_adoption_is_awaited(browser, live_server, h, key):
    """The hand-over waits only for an awaited key (awaitAdoption): with none awaited, an orphan popover closes on the
    redraw, keyless (Rota health) or keyed (run 42093943605, browser 15: Rota health floated over the new view)."""
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        page.evaluate('''async key=>{const{el,popover,closeOrphanPopover}=await window.importBundle('dom.js');
            const a=el('button',{type:'button'},'Fixture orphan');document.body.append(a);
            popover(a,p=>p.append(el('p',{},'Stale')),{label:'Fixture orphan',...(key?{key}:{})});
            a.remove();closeOrphanPopover();}''', key)
        expect(page.get_by_role('dialog', name='Fixture orphan', exact=True)).to_have_count(0)
    finally:
        context.close()
