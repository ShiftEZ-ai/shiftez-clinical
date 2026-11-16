"""Actual browser/API/DB flows. No route stubs and no managed-policy workaround."""
import re
import pytest
from playwright.sync_api import expect
from .test_browser import session_action, browser, live_server, sign_in, navigate, save_dialog, pick_view, open_session  # noqa: F401

pytestmark = pytest.mark.browser


def test_segment_editor_preview_and_staffing_option_never_autosave(browser,live_server,h):
    w=h.shift(h.workspace('Presence and staffing browser journey'))['workspace']
    context,page=sign_in(browser,live_server,'coordinator');errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    try:
        navigate(page,'planner')
        open_session(page)
        page.get_by_role('button',name='Edit session',exact=True).click()
        form=page.locator('.dock-panel')
        expect(form.get_by_label('Specify off-cover gaps',exact=True)).to_have_count(0)
        form.get_by_role('checkbox',name=re.compile('^Keep assigned — ')).check()
        before=h.detail(w)
        form.get_by_role('button',name='Calculate draft impact',exact=True).click()
        expect(form.get_by_text('Not saved yet: review what this changes',exact=False)).to_be_visible()
        assert h.detail(w)==before
        save_dialog(form,'Save draft session')
        assert h.detail(w)['shifts'][0]['participants'][0]['fixed']
        open_session(page)
        session_action(page, 'Find staffing or replacement options')
        form=page.locator('.dock-panel')
        expect(form.get_by_label('Proposed reserved start',exact=True)).to_have_attribute('readonly', '')
        expect(form.get_by_label('Proposed reserved end',exact=True)).to_have_attribute('readonly', '')
        # The search is the shared collection toolbar's, live once options are calculated.
        search=form.get_by_role('group',name='Staffing options').get_by_role('searchbox',name='Find staff by name')
        search.fill('Jordan')
        before=h.detail(w)
        form.get_by_role('button',name='Calculate staffing options',exact=True).click()
        expect(form.get_by_role('button',name='Review Jordan Lee in draft',exact=True)).to_be_visible()
        # Each option's findings are shown under their heading, never folded away.
        expect(form.locator('section.candidate-details h4').first).to_contain_text('person-specific blockers')
        expect(form.locator('section.candidate-details details')).to_have_count(0)
        assert h.detail(w)==before
        search.fill('Taylor')
        expect(form.get_by_role('button',name='Review Jordan Lee in draft',exact=True)).to_have_count(0)
        form.get_by_role('button',name='Review Taylor Noor in draft',exact=True).click()
        form=page.locator('.dock-panel');expect(form.get_by_label('Participant',exact=True)).to_have_count(2)
        assert h.detail(w)==before
        save_dialog(form,'Save draft session')
        after=h.detail(w);assert after['workspace']['validation']['valid'] and len(after['shifts'][0]['participants'])==2
        assert next(p for p in after['shifts'][0]['participants'] if p['person_id']==h.seed['people'][0])['fixed']
        assert after['workspace']['status']=='draft' and not h.get('coordinator','/api/publications')['publications']
        page.reload();navigate(page,'planner')
        persisted=h.detail(w)['shifts'][0]
        assert all(p['start']==persisted['start'] and p['end']==persisted['end'] and not p['segments'] for p in persisted['participants'])
        assert not errors
    finally:
        context.close()


def test_draft_impact_findings_are_shown_under_labelled_headings(browser,live_server,h):
    w=h.workspace('Draft impact findings shown')
    w=h.shift(w,start='2026-10-05T08:00:00+03:00',end='2026-10-05T12:00:00+03:00',people=[])['workspace']  # an open place: a finding elsewhere in the rota
    h.shift(w)
    context,page=sign_in(browser,live_server,'coordinator')
    try:
        navigate(page,'planner')
        open_session(page)
        page.get_by_role('button',name='Edit session',exact=True).click()
        form=page.locator('.dock-panel')
        form.get_by_role('checkbox',name=re.compile('^Keep assigned — ')).check()
        form.get_by_role('button',name='Calculate draft impact',exact=True).click()
        expect(form.get_by_text('Not saved yet: review what this changes',exact=False)).to_be_visible()
        heading=form.get_by_role('heading',name=re.compile(r'^(Checks for changed sessions|Other rota checks) · \d+$')).first
        expect(heading).to_be_visible()
        # The findings are read without opening anything.
        expect(form.locator('section.preview-findings table').first).to_be_visible()
        expect(form.locator('section.preview-findings details')).to_have_count(0)
    finally:
        context.close()


def test_staffing_options_show_more_pages_from_the_server(browser,live_server,h):
    rank=next(c for c in h.get('admin','/api/catalogues')['categories'] if c['id']!=h.seed['trainee_category'])
    for i in range(11):  # department staff: a rank in this department makes them options
        person=h.configure('people',{'display_name':f'Paging person {i:02d}'})['record']
        h.command('coordinator','membership.create',{'person_id':person['id'],'category_id':rank['id'],'starts_on':'2020-01-01'})
    h.shift(h.workspace('Staffing options paging'))
    context,page=sign_in(browser,live_server,'coordinator')
    try:
        navigate(page,'planner')
        open_session(page)
        session_action(page, 'Find staffing or replacement options')
        form=page.locator('.dock-panel')
        form.get_by_role('searchbox',name='Find staff by name').fill('Paging person')
        form.get_by_role('button',name='Calculate staffing options',exact=True).click()
        reviews=form.get_by_role('button',name=re.compile(r'^Review Paging person \d+ in draft$'))
        expect(reviews).to_have_count(10)
        expect(form.get_by_text('Showing 10 of 11',exact=True)).to_be_visible()
        form.get_by_role('button',name='Show 1 more',exact=True).click()
        expect(reviews).to_have_count(11)
        expect(form.get_by_role('button',name=re.compile('^Show .* more$'))).to_have_count(0)
        expect(form.get_by_text('11 of 11 matching people checked',exact=False)).to_be_visible()
        # A search that matches nobody says so, with the toolbar still there to change it.
        form.get_by_role('searchbox',name='Find staff by name').fill('Nobody by this name')
        expect(form.get_by_text('No unassigned departmental people match this search',exact=False)).to_be_visible()
    finally:
        context.close()


def test_staffing_search_change_during_pending_response_restarts_and_binds_pages(browser,live_server,h):
    """A search typed while a calculation is pending replaces it: the older answer is never rendered, Calculate reads
    the search box itself (not the debounced value), and Show more pages the search that produced the first page."""
    rank=next(c for c in h.get('admin','/api/catalogues')['categories'] if c['id']!=h.seed['trainee_category'])
    for i in range(11):
        person=h.configure('people',{'display_name':f'Paging person {i:02d}'})['record']
        h.command('coordinator','membership.create',{'person_id':person['id'],'category_id':rank['id'],'starts_on':'2020-01-01'})
    h.shift(h.workspace('Staffing search race'))
    context,page=sign_in(browser,live_server,'coordinator')
    held,sent=[],[]
    def hold_first(route):
        sent.append(route.request.post_data_json)
        if len(sent)==1:held.append(route)
        else:route.continue_()
    try:
        navigate(page,'planner')
        open_session(page)
        session_action(page, 'Find staffing or replacement options')
        form=page.locator('.dock-panel')
        page.route('**/api/planner/staffing-suggestions',hold_first)
        search=form.get_by_role('searchbox',name='Find staff by name')
        # Typed and calculated at once: the request carries the typed text although the debounce has not fired.
        search.fill('Jordan')
        with page.expect_request('**/api/planner/staffing-suggestions') as first:
            form.get_by_role('button',name='Calculate staffing options',exact=True).click()
        assert first.value.post_data_json['search']=='Jordan'
        # The search changes while that answer is still held: a new calculation starts for the new search.
        search.fill('Paging person')
        reviews=form.get_by_role('button',name=re.compile(r'^Review Paging person \d+ in draft$'))
        expect(reviews).to_have_count(10)
        assert sent[-1]['search']=='Paging person' and sent[-1]['offset']==0
        assert len(held)==1
        with page.expect_response(lambda r:r.request==first.value):
            held[0].continue_()
        expect(form.get_by_text('Showing 10 of 11',exact=True)).to_be_visible()
        expect(form.get_by_role('button',name=re.compile('^Review Jordan'))).to_have_count(0)
        form.get_by_role('button',name='Show 1 more',exact=True).click()
        expect(reviews).to_have_count(11)
        assert sent[-1]['search']=='Paging person' and sent[-1]['offset']==10
        expect(form.get_by_role('button',name=re.compile('^Review Jordan'))).to_have_count(0)
    finally:
        page.unroute('**/api/planner/staffing-suggestions')
        context.close()
