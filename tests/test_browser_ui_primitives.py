"""Shared UI primitives in a real browser: the selection bar, tri-state checkboxes, docked resizable side panels and
their header, pill lists, the column header menu and Group › Then by, and the icon-only footer status.
Primitives are driven on a fixture inside the signed-in app (the same compiled modules the screens import); the
column menu and footer are exercised on real screens."""
import re
import pytest
from playwright.sync_api import expect
from apps.api.version import __version__
from .test_browser import browser, live_server, sign_in, navigate, toolbar, close_popover, press_escape, fonts_ready  # noqa: F401
from .test_browser_operator_footer import wait_for_held_route

pytestmark = pytest.mark.browser
MODES = [(1440, 'light'), (390, 'dark')]


def signed_in(browser, live_server, width, scheme, role='coordinator'):
    context, page = sign_in(browser, live_server, role, viewport={'width': width, 'height': 900})
    page.emulate_media(color_scheme=scheme)
    navigate(page, 'people')
    fonts_ready(page)
    return context, page


def fixture(page, html):
    page.evaluate("""html => {document.querySelector('#ui-fixture')?.remove();
      const f=document.createElement('div');f.id='ui-fixture';f.innerHTML=html;document.querySelector('#main').prepend(f);}""", html)


@pytest.mark.parametrize('entry', ['cell', 'bulk'])
def test_committed_table_edit_is_not_rejected_when_after_save_refresh_fails(browser, live_server, h, entry):
    tags = [h.configure('tags', {'name': 'Refresh tag ' + str(i), 'labels': ['people'], 'description': 'Before'})['record'] for i in range(2)]
    context, page = signed_in(browser, live_server, 1440, 'light', 'admin')
    writes = []
    try:
        fixture(page, '<div id="refresh-edit-host"></div>')
        page.evaluate("""async rows => {
          const {dataTable}=await window.importBundle('table_view.js'),{command}=await window.importBundle('api.js');
          const table=dataTable({id:'refresh-edit-proof',noun:['tag','tags'],rows,rowId:r=>r.id,bulk:()=>[],
            columns:[{key:'description',label:'Description',cell:r=>r.description,text:r=>r.description,
              edit:{kind:'text',value:r=>r.description,save:async (chosen,value)=>{
                const result=await command('catalogue.bulk_update',{catalogue:'tags',items:chosen.map(r=>({id:r.id,revision:r.revision})),set:{description:value},reason:'Refresh failure test'});return result.records;
              },afterSave:async()=>{throw new Error('Synthetic redraw unavailable');}}}]});
          document.getElementById('refresh-edit-host').append(table.root);
        }""", tags)
        page.on('request', lambda request: writes.append(request) if request.method == 'POST' and request.url.endswith('/api/commands') else None)
        scope = page.locator('#refresh-edit-host')
        if entry == 'bulk':
            for box in scope.locator('td .bulk-check').all():
                box.check()
            scope.get_by_role('toolbar', name='Bulk actions').get_by_role('button', name='Edit field\u2026', exact=True).click()
            dialog = page.get_by_role('dialog', name='Edit 2 tags', exact=True)
            dialog.get_by_label('Description', exact=True).fill('Committed')
            dialog.get_by_role('button', name='Apply to all', exact=True).click()
            expect(dialog).not_to_be_visible()
            message, changed = 'Description updated for 2 tags.', {t['id'] for t in tags}
        else:
            cell = scope.locator('td[data-col="description"]').first
            cell.click()
            cell.get_by_label('Description', exact=True).fill('Committed')
            cell.get_by_label('Description', exact=True).press('Enter')
            expect(cell).to_have_text('Committed')
            message, changed = 'Description saved.', {tags[0]['id']}
        expect(page.get_by_text('Changes saved, but the table could not refresh. Synthetic redraw unavailable', exact=True)).to_be_visible()
        expect(scope.locator('.editing,.saving')).to_have_count(0)
        assert len(writes) == 1
        saved = {t['id']: t['description'] for t in h.get('admin', '/api/catalogues')['tags'] if t['id'] in changed}
        assert set(saved.values()) == {'Committed'}
        if entry == 'cell':
            # The current server revision remains editable after the failed redraw; no frozen editor or stale retry.
            cell.click()
            cell.get_by_label('Description', exact=True).fill('Cancelled')
            cell.get_by_label('Description', exact=True).press('Escape')
            expect(cell).to_have_text('Committed')
            expect(scope.locator('.editing,.saving')).to_have_count(0)
            assert len(writes) == 1
            cell.click()
            cell.get_by_label('Description', exact=True).fill('Refined')
            cell.get_by_label('Description', exact=True).press('Enter')
            expect(cell).to_have_text('Refined')
            expect(scope.locator('.editing,.saving')).to_have_count(0)
            assert len(writes) == 2
            # A rejected forward command restores the latest confirmed value, unlike a failed redraw after commit.
            def reject(route):
                route.fulfill(status=409, content_type='application/json', body='{"error":{"code":"EDIT_REJECTED","message":"Synthetic edit rejected"}}')
            page.route('**/api/commands', reject)
            cell.click()
            cell.get_by_label('Description', exact=True).fill('Rejected')
            cell.get_by_label('Description', exact=True).press('Enter')
            expect(page.get_by_text('Synthetic edit rejected', exact=True)).to_be_visible()
            expect(cell).to_have_text('Refined')
            expect(scope.locator('.editing,.saving')).to_have_count(0)
            assert len(writes) == 3
            page.unroute('**/api/commands', reject)
        page.locator('.toast').filter(has_text=message).first.get_by_role('button', name='Undo', exact=True).click()
        expect(page.get_by_text('Undone. The previous values are back.', exact=True)).to_be_visible()
        restored = {t['id']: t['description'] for t in h.get('admin', '/api/catalogues')['tags'] if t['id'] in changed}
        assert set(restored.values()) == ({'Committed'} if entry == 'cell' else {'Before'})
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', MODES, ids=['desktop-light', 'phone-dark'])
def test_action_row_refits_when_labels_or_visibility_change(browser, live_server, width, scheme):
    context, page = signed_in(browser, live_server, width, scheme)
    try:
        fixture(page, '<div id="fit-host"></div><output id="fit-result">0</output>')
        page.evaluate("""async () => {await document.fonts.ready;
          const {el,button,compact,fitRow}=await window.importBundle('dom.js'),host=document.getElementById('fit-host');
          host.style.setProperty('width','240px');host.style.setProperty('height','40px');
          const review=compact(button('Review',()=>{})),discard=button('Discard changes',()=>{});
          let clicks=0;const propose=compact(button('Propose',()=>{document.getElementById('fit-result').textContent=String(++clicks);},'primary'));
          review.id='fit-review';discard.id='fit-discard';discard.hidden=true;propose.id='fit-propose';
          const row=el('div',{id:'fit-actions',class:'page-actions'},review,discard,propose);host.append(row);fitRow(row);
        }""")
        row = page.locator('#fit-actions')
        review_label = page.locator('#fit-review .label')
        propose_label = page.locator('#fit-propose .label')
        propose = page.locator('#fit-propose')
        expect(row).to_have_attribute('data-fitted', '1')
        for shown in (review_label, propose_label):
            expect(shown).to_be_visible()
            expect(shown).to_have_css('clip-path', 'none')
            assert shown.evaluate('l => l.getBoundingClientRect().width') > 1

        # No resize: only an existing action becoming visible changes the space required.
        page.locator('#fit-discard').evaluate('b => b.hidden=false')
        expect(review_label).to_have_css('clip-path', 'inset(50%)')
        assert review_label.evaluate('l => l.getBoundingClientRect().width') <= 1
        expect(page.locator('#fit-review')).to_have_accessible_name('Review')
        assert row.evaluate('r => r.scrollWidth <= r.clientWidth + 1')
        expect(propose).to_be_in_viewport(ratio=1)
        propose.click()
        expect(page.locator('#fit-result')).to_have_text('1')
        page.locator('#fit-discard').evaluate('b => b.hidden=true')
        for shown in (review_label, propose_label):
            expect(shown).to_be_visible()
            expect(shown).to_have_css('clip-path', 'none')
            assert shown.evaluate('l => l.getBoundingClientRect().width') > 1

        # Nested text changes must refit independently and retain the action's full accessible name.
        label = 'Propose all 200 selected rule changes for independent approval'
        propose.evaluate("async (b,text) => {const {relabel}=await window.importBundle('dom.js');relabel(b,text);}", label)
        expect(propose_label).to_have_css('clip-path', 'inset(50%)')
        assert propose_label.evaluate('l => l.getBoundingClientRect().width') <= 1
        expect(propose).to_have_accessible_name(label)
        assert row.evaluate('r => r.scrollWidth <= r.clientWidth + 1')
        expect(propose).to_be_in_viewport(ratio=1)
        propose.click()
        expect(page.locator('#fit-result')).to_have_text('2')
        propose.evaluate("async b => {const {relabel}=await window.importBundle('dom.js');relabel(b,'Propose');}")
        for shown in (review_label, propose_label):
            expect(shown).to_be_visible()
            expect(shown).to_have_css('clip-path', 'none')
            assert shown.evaluate('l => l.getBoundingClientRect().width') > 1
        expect(propose).to_have_accessible_name('Propose')
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', MODES, ids=['desktop-light', 'phone-dark'])
def test_selection_bar_and_tri_state_roll_up(browser, live_server, width, scheme):
    context, page = signed_in(browser, live_server, width, scheme)
    try:
        fixture(page, '<div id="list" class="ui-list"><input type=checkbox id=all aria-label="Select all">'
                      '<input type=checkbox id=g1 aria-label="Group 1"><input type=checkbox id=a aria-label=a><input type=checkbox id=b aria-label=b>'
                      '<input type=checkbox id=g2 aria-label="Group 2"><input type=checkbox id=c aria-label=c></div>')
        page.evaluate("""async () => {
          const {checkTree}=await window.importBundle('check_tree.js'),{selectionBar}=await window.importBundle('selection_bar.js');
          const $=id=>document.getElementById(id),picked=new Set(),log=window.uiLog=[];
          checkTree($('g1'),[$('a'),$('b')]);checkTree($('g2'),[$('c')]);checkTree($('all'),[$('g1'),$('g2')]);
          const bar=selectionBar({label:'Bulk actions'});$('list').append(bar.root);
          const draw=()=>bar.show(picked.size?{count:picked.size,noun:['line','lines'],detail:'lines in Duties per week',clear:()=>{for(const id of ['a','b','c']){$(id).checked=false;$(id).dispatchEvent(new Event('change'));}},
            actions:[{label:'Numbers…',icon:'edit',run:()=>log.push('numbers'),primary:true},{label:'Duplicate',icon:'copy',run:()=>log.push('dup')},
                     {label:'Valid dates…',icon:'calendar',run:()=>log.push('dates'),more:true},{label:'Remove',icon:'close',run:()=>log.push('remove'),danger:true}]}:null);
          for(const id of ['a','b','c'])$(id).addEventListener('change',()=>{$(id).checked?picked.add(id):picked.delete(id);draw();});
        }""")
        box = lambda name: page.get_by_role('checkbox', name=name, exact=True)
        # Child → group → header: one line ticked makes its group and the header mixed.
        box('a').check()
        for name in ('Group 1', 'Select all'):
            expect(box(name)).to_have_attribute('aria-checked', 'mixed')
            assert box(name).evaluate('b => b.indeterminate')
        box('b').check()
        expect(box('Group 1')).to_be_checked()
        expect(box('Group 1')).not_to_have_attribute('aria-checked', 'mixed')
        expect(box('Select all')).to_have_attribute('aria-checked', 'mixed')
        # The header ticks every line below it (the leaves report it, so the selection follows).
        box('Select all').check()
        expect(box('c')).to_be_checked()
        expect(box('Group 2')).to_be_checked()
        bar = page.get_by_role('toolbar', name='Bulk actions')
        expect(bar).to_contain_text('3 lines selected')
        expect(bar.locator('.sel-badge')).to_have_text('3')
        expect(bar.locator('.sel-words')).to_contain_text('lines in Duties per week')
        # Primary solid accent, the rest inline, ⋯ More for the others, the destructive one last with a bin, then Esc ×.
        expect(bar.get_by_role('button', name='Numbers…')).to_have_class(re.compile(r'\bprimary\b'))
        names = bar.locator('button').evaluate_all('bs => bs.map(b => b.getAttribute("aria-label"))')
        # A phone has no room for every action: the secondary ones fold into ⋯ More (the bar never scrolls sideways).
        assert names == (['Numbers…', 'Duplicate', 'More actions', 'Remove', 'Clear selection'] if width > 600 else
                         ['Numbers…', 'More actions', 'Remove', 'Clear selection']), names
        assert bar.evaluate('b => b.scrollWidth <= b.clientWidth + 1')
        expect(bar.get_by_role('button', name='Remove')).to_have_class(re.compile(r'\bdanger\b'))
        assert bar.get_by_role('button', name='Valid dates…').count() == 0
        # One row, centred over its list, never wider than it.
        geo = page.evaluate("""() => {const b=document.querySelector('#list .selection-bar').getBoundingClientRect(),l=document.querySelector('#list').getBoundingClientRect();
          return {bw:b.width,lw:l.width,mid:Math.abs((b.left+b.width/2)-(l.left+l.width/2)),h:b.height};}""")
        assert geo['bw'] <= geo['lw'] + 1 and geo['mid'] <= 2 and geo['h'] < 60, geo
        bar.get_by_role('button', name='More actions').click()
        page.get_by_role('menuitem', name='Valid dates…').click()
        bar.get_by_role('button', name='Numbers…').click()
        assert page.evaluate('window.uiLog') == ['dates', 'numbers']
        # Esc clears the selection (from the list), and every box rolls back to empty.
        box('c').focus()
        page.keyboard.press('Escape')
        expect(bar).to_be_hidden()
        for name in ('Select all', 'Group 1', 'Group 2', 'a'):
            expect(box(name)).not_to_be_checked()
        expect(box('Select all')).not_to_have_attribute('aria-checked', 'mixed')
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', MODES, ids=['desktop-light', 'phone-dark'])
def test_side_panel_is_docked_resizable_and_names_what_is_open(browser, live_server, width, scheme):
    context, page = signed_in(browser, live_server, width, scheme)
    try:
        fixture(page, '<div id="dock" class="wk-layout"><div id="content" class="ui-content">Content</div></div>'
                      '<ul id="items"><li>One</li><li>Two</li><li>Three</li></ul>')
        page.evaluate("""async () => {
          localStorage.removeItem('rota.panel.ui-test');document.querySelector('#content').style.setProperty('flex','1');
          const {sidePanel,panelHead}=await window.importBundle('side_panel.js'),{panelHighlight}=await window.importBundle('dom.js');
          const items=[...document.querySelectorAll('#items li')];window.uiClosed=0;
          const aside=document.createElement('aside');aside.className='wk-panel';aside.setAttribute('aria-labelledby','ui-title');
          const show=i=>{aside.querySelector('.side-head')?.remove();aside.append(panelHead({id:'ui-title',title:items[i].textContent,
            position:{index:i,total:items.length,noun:'line',go:show},close:()=>{window.uiClosed++;}}));panelHighlight.highlight([items[i]],true);};
          document.querySelector('#dock').append(sidePanel(aside,{key:'ui-test',width:384,close:()=>{window.uiClosed++;}}));show(0);
        }""")
        panel = page.locator('#dock .wk-panel')
        expect(panel.locator('.side-pos')).to_have_text('Line 1 of 3')
        expect(panel.get_by_role('heading', name='One')).to_be_visible()
        expect(page.locator('#items li').first).to_have_class(re.compile(r'\bis-explained\b'))
        prev, nxt = panel.get_by_role('button', name='Previous line'), panel.get_by_role('button', name='Next line')
        expect(prev).to_be_disabled()
        expect(nxt).to_have_attribute('data-tip', 'Next line')
        nxt.click()
        expect(panel.locator('.side-pos')).to_have_text('Line 2 of 3')
        expect(page.locator('#items li.is-explained')).to_have_text('Two')
        # No coloured edge stripe on the panel or its header.
        assert page.evaluate("""() => [document.querySelector('#dock .wk-panel'),document.querySelector('#dock .side-head')]
          .every(n => !getComputedStyle(n).boxShadow.includes('inset'))""")
        panel.get_by_role('button', name='Next line').focus()
        press_escape(page)
        page.wait_for_function('() => window.uiClosed === 1')
        grip = panel.get_by_role('separator', name='Resize panel')
        if width < 1181:  # stacked in the flow at full width: nothing to resize
            expect(grip).to_be_hidden()
            assert page.evaluate("() => document.querySelector('#dock .wk-panel').getBoundingClientRect().width >= innerWidth - 40")
            return
        # Docked: the content beside it reflows as the panel grows (it never floats over the content).
        content_before = page.locator('#content').bounding_box()['width']
        expect(grip).to_have_attribute('aria-valuenow', '384')
        grip.focus()
        page.keyboard.press('ArrowLeft')
        expect(grip).to_have_attribute('aria-valuenow', '400')
        page.keyboard.press('Shift+ArrowLeft')
        expect(grip).to_have_attribute('aria-valuenow', '464')
        assert page.locator('#content').bounding_box()['width'] < content_before
        p, c = panel.bounding_box(), page.locator('#content').bounding_box()
        assert c['x'] + c['width'] <= p['x'] + 1  # beside, not under
        page.keyboard.press('ArrowRight')
        expect(grip).to_have_attribute('aria-valuenow', '448')
        assert page.evaluate("localStorage.getItem('rota.panel.ui-test')") == '448'
        # Pointer drag from the left edge, clamped to the maximum (60% of the window at most); double-click resets.
        g = grip.bounding_box()
        page.mouse.move(g['x'] + 3, g['y'] + 40)
        page.mouse.down()
        page.mouse.move(g['x'] - 900, g['y'] + 40, steps=4)
        page.mouse.up()
        top = int(grip.get_attribute('aria-valuemax'))
        assert 448 < top <= width * 0.6
        expect(grip).to_have_attribute('aria-valuenow', str(top))
        # Dragged the other way it stops at the minimum, 360px.
        g = grip.bounding_box()
        page.mouse.move(g['x'] + 3, g['y'] + 40)
        page.mouse.down()
        page.mouse.move(g['x'] + 600, g['y'] + 40, steps=4)
        page.mouse.up()
        expect(grip).to_have_attribute('aria-valuenow', '360')
        expect(grip).to_have_attribute('aria-valuemin', '360')
        grip.dblclick()
        expect(grip).to_have_attribute('aria-valuenow', '384')
        assert page.evaluate("localStorage.getItem('rota.panel.ui-test')") is None
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', MODES, ids=['desktop-light', 'phone-dark'])
def test_pill_list_shows_first_pills_then_a_numbered_list(browser, live_server, width, scheme):
    context, page = signed_in(browser, live_server, width, scheme)
    try:
        fixture(page, '<div id="plain"></div><div id="wrapped" class="dt-wrap"></div>')
        page.evaluate("""async () => {const {pillList}=await window.importBundle('table_menu.js');const {badge}=await window.importBundle('dom.js');
          for(const id of ['plain','wrapped'])document.getElementById(id).append(pillList(['Family Medicine','Virtual','Procedure','Wellness','Sport'].map(x=>badge(x)),{label:'clinics',shown:2}));}""")
        plain = page.locator('#plain')
        expect(plain.locator('.pill-item:visible')).to_have_count(2)
        more = plain.get_by_role('button', name='3 more clinics: show all 5')
        expect(more).to_have_text('+3')
        more.click()
        pop = page.get_by_role('dialog', name='All clinics')
        expect(pop.locator('ol > li')).to_have_count(5)
        expect(pop.locator('ol > li').nth(4)).to_have_text('Sport')
        page.keyboard.press('Escape')
        expect(more).to_be_focused()
        # A wrapped column shows every pill and no "+N".
        expect(page.locator('#wrapped .pill-item:visible')).to_have_count(5)
        expect(page.locator('#wrapped .pill-more')).to_be_hidden()
    finally:
        context.close()


def test_column_header_menu_and_then_by_save_with_the_view(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin', viewport={'width': 1440, 'height': 900})
    try:
        navigate(page, 'people')
        head = page.locator('.dt-table thead')
        rank = lambda: head.get_by_role('button', name='Current rank', exact=True)
        # Click opens the column menu; Sort Z → A sorts and marks the header.
        rank().click()
        menu = page.get_by_role('menu', name='Current rank column')
        expect(menu.get_by_role('menuitemradio', name='Sort A → Z')).to_be_visible()
        expect(menu.get_by_role('menuitem', name='Move left')).to_be_disabled()  # the name column stays first
        menu.get_by_role('menuitemradio', name='Sort Z → A').click()
        expect(head.locator('th', has=page.get_by_role('button', name='Current rank', exact=True))).to_have_attribute('aria-sort', 'descending')
        # Right-click opens it too: wrap this column (a switch) marks its cells.
        rank().click(button='right')
        page.get_by_role('menu', name='Current rank column').get_by_role('menuitemcheckbox', name='Wrap this column').click()
        expect(page.locator('.dt-table td[data-col="rank"]').first).to_have_class(re.compile(r'\bdt-wrap\b'))
        # Keyboard: Enter opens it; Move right swaps the column with its neighbour and keeps focus on it.
        before = head.locator('th[scope=col]').all_inner_texts()
        rank().focus()
        page.keyboard.press('Enter')
        page.get_by_role('menu', name='Current rank column').get_by_role('menuitem', name='Move right').click()
        after = head.locator('th[scope=col]').all_inner_texts()
        i = before.index('Current rank')
        assert after[i + 1] == 'Current rank' and after[i] == before[i + 1], (before, after)
        expect(rank()).to_be_focused()
        # Hide column from its menu (View settings › Shown properties brings it back).
        head.get_by_role('button', name='Extension', exact=True).click()
        page.get_by_role('menu', name='Extension column').get_by_role('menuitem', name='Hide column').click()
        expect(head.get_by_role('button', name='Extension', exact=True)).to_have_count(0)
        # Group by from the header, then "Then by" in the Group menu (the default is marked).
        rank().click()
        page.get_by_role('menu', name='Current rank column').get_by_role('menuitemcheckbox', name='Group by Current rank').click()
        group = toolbar(page, 'Group')
        expect(group.get_by_label('Group by', exact=True)).to_have_value('rank')
        expect(group.get_by_role('button', name='Remove grouping', exact=True)).to_be_visible()
        group.get_by_role('button', name='+ Add subgroup', exact=True).click()
        then = group.get_by_label('Then by', exact=True)
        expect(then.locator('option[value="rank"]')).to_have_count(0)
        then.select_option(label='Skills')
        levels = group.get_by_role('list', name='Groups').get_by_role('listitem')
        levels.nth(0).get_by_label('Group order', exact=True).select_option('desc')
        levels.nth(1).get_by_label('Group order', exact=True).select_option('asc')
        group.get_by_role('button', name='Move Skills up', exact=True).click()
        expect(group.get_by_label('Group by', exact=True)).to_have_value('skills')
        expect(levels.nth(1).get_by_label('Group order', exact=True)).to_have_value('desc')
        group.get_by_role('button', name='Move Skills down', exact=True).click()
        expect(group.get_by_role('button', name='+ Add subgroup', exact=True)).to_be_hidden()
        group.get_by_role('button', name='Collapse all', exact=True).click()
        expect(page.locator('.dt-table tbody tr[data-row]:visible')).to_have_count(0)
        group.get_by_role('button', name='Expand all', exact=True).click()
        close_popover(page)
        expect(page.get_by_role('button', name='Group: Current rank › Skills', exact=True)).to_be_visible()
        expect(page.locator('.dt-table tr.dt-subgroup').first).to_be_visible()
        # A group's box ticks its rows and rolls up to select-all.
        page.locator('tr.dt-group:not(.dt-subgroup) .bulk-check').first.check()
        bar = page.get_by_role('toolbar', name='Bulk actions')
        expect(bar).to_contain_text('3 people selected')
        expect(page.get_by_role('checkbox', name='Select all people shown')).to_be_checked()
        page.locator('.dt-table tbody tr[data-row] .bulk-check').first.uncheck()
        expect(page.get_by_role('checkbox', name='Select all people shown')).to_have_attribute('aria-checked', 'mixed')
        expect(page.locator('tr.dt-group:not(.dt-subgroup) .bulk-check').first).to_have_attribute('aria-checked', 'mixed')
        page.keyboard.press('Escape')
        expect(bar).to_be_hidden()
        # Everything above saves with the view.
        toolbar(page, 'Saved views').get_by_role('menuitem', name='New view…').click()
        dialog = page.get_by_role('dialog', name='New view')
        dialog.get_by_label('View name').fill('Column menu')
        dialog.get_by_role('button', name='Save view').click()
        expect(page.get_by_role('button', name='Saved views: Column menu', exact=True)).to_be_visible()
        saved = h.get('admin', '/api/table-views?table=people')['views'][0]['definition']
        assert (saved['group'], saved['then'], saved['wrap'], saved['sort']) == ('rank', 'skills', ['rank'], ['rank', 'desc']), saved
        assert saved['order'].index('rank') == 1 and 'ext' in saved['hidden'], saved
        assert saved['group_sort'] == [['rank', 'desc']], saved
        # View settings › Wrap all columns.
        view = toolbar(page, 'View settings')
        view.get_by_role('switch', name='Wrap all columns').check()
        close_popover(page)
        expect(page.locator('.dt-table td[data-col="name"]').first).to_have_class(re.compile(r'\bdt-wrap\b'))
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', MODES, ids=['desktop-light', 'phone-dark'])
def test_footer_general_status_is_icons_with_named_tooltips(browser, live_server, width, scheme):
    context, page = signed_in(browser, live_server, width, scheme)
    try:
        # "? Key": one button opening the explanation above the footer.
        page.evaluate("""async () => {const {footerKey,footerStatus}=await window.importBundle('dom.js');
          document.querySelector('.app-footer .page-status').append(footerStatus(footerKey(p => p.append('5! = mandatory maximum'))));}""")
        key = page.locator('.app-footer').get_by_role('button', name='Key', exact=True)
        key.click()
        pop = page.get_by_role('dialog', name='Key')
        expect(pop).to_contain_text('5! = mandatory maximum')
        pop.evaluate('e => Promise.allSettled(e.getAnimations({subtree: true}).map(a => a.finished))')
        assert pop.bounding_box()['y'] + pop.bounding_box()['height'] <= page.locator('.app-footer').bounding_box()['y'] + 1
        page.keyboard.press('Escape')
        expect(pop).to_have_count(0)
        footer = page.locator('.app-footer .service-status')
        env = footer.locator('.env-badge')
        expect(env).to_have_attribute('aria-label', f'Test environment: test accounts only · Release {__version__}')
        expect(env).to_have_attribute('data-tip', f'Test environment: test accounts only · Release {__version__}')
        expect(env.locator('.sr-only')).to_have_text('Test environment')
        # Icons only: no visible words in the general status.
        assert footer.evaluate("""f => [...f.querySelectorAll('*')].filter(n => n.childNodes.length && [...n.childNodes].some(c => c.nodeType===3 && c.textContent.trim())
            && n.getBoundingClientRect().width > 2).map(n => n.textContent)""") == []
        expect(footer.locator('#connection')).to_have_attribute('aria-label', 'Connected')
        # Saving: a spinner while a change is on its way; ✓ once saved; red when a save fails.
        held = []
        page.route('**/api/commands', lambda route: held.append(route))
        page.evaluate("() => { window.importBundle('api.js').then(m => m.command('table_view.create',{table_key:'people',name:'Footer view',definition:{}}).catch(() => {})); }")
        expect(footer.locator('#save-state')).to_have_class(re.compile(r'\bsaving\b'))
        expect(footer.locator('#save-state')).to_have_attribute('aria-label', 'Saving…')
        wait_for_held_route(page, held)
        held[0].continue_()
        expect(footer.locator('#save-state')).to_have_class(re.compile(r'\bsaved\b'))
        expect(footer.locator('#save-state')).to_have_attribute('aria-label', 'Saved just now')
        page.unroute('**/api/commands')
        page.route('**/api/commands', lambda route: route.fulfill(status=409, content_type='application/json',
                                                                  body='{"code":"CONFLICT","message":"Changed meanwhile"}'))
        page.evaluate("() => { window.importBundle('api.js').then(m => m.command('table_view.create',{table_key:'people',name:'Footer view 2',definition:{}}).catch(() => {})); }")
        expect(footer.locator('#save-state')).to_have_class(re.compile(r'\berror\b'))
        expect(footer.locator('#save-state')).to_have_attribute('aria-label', re.compile('^Not saved'))
        expect(footer.locator('#save-state .status-word')).to_have_text('Not saved')  # a change of state is said on screen (G-04)
        # Offline: a red dot named "Offline · retrying".
        context.set_offline(True)
        page.evaluate("window.dispatchEvent(new Event('offline'))")
        expect(footer.locator('#connection')).to_have_attribute('aria-label', 'Offline · retrying')
        expect(footer.locator('#connection')).to_have_class(re.compile(r'\boffline\b'))
        expect(footer.locator('#connection .status-word')).to_have_text('Offline')
        context.set_offline(False)
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', MODES, ids=['desktop-light', 'phone-dark'])
def test_every_field_uses_the_one_control_look(browser, live_server, width, scheme):
    """No input, select or text area is left in the browser's own look (inset border, default background)."""
    context, page = signed_in(browser, live_server, width, scheme, role='admin')
    try:
        for key in ('people', 'rules', 'configuration', 'requests', 'schedule', 'insights', 'system'):
            navigate(page, key)
            bare = page.evaluate("""() => [...document.querySelectorAll('input,select,textarea')].filter(n => {
                if (['checkbox','radio','range','file','color','hidden'].includes(n.type) || !n.getClientRects().length) return false;
                const s = getComputedStyle(n);
                return ['inset','outset'].includes(s.borderTopStyle) || !s.fontFamily.includes('Geist');
              }).map(n => n.outerHTML.slice(0, 120))""")
            assert bare == [], (key, bare)
    finally:
        context.close()


def test_pending_button_returns_only_unclaimed_focus_after_success_or_error(browser, live_server):
    context, page = signed_in(browser, live_server, 1440, 'light')
    try:
        fixture(page, '<input aria-label="Another field"><p id="focus-elsewhere">Read something else</p>')
        page.evaluate("""async () => {
            const {button}=await window.importBundle('dom.js');
            const b=button('Export fixture',()=>new Promise((resolve,reject)=>{
                window.finishFixture=fail=>fail?reject(new Error('Fictional delayed action failure')):resolve();
            }));b.id='pending-focus';document.querySelector('#ui-fixture').prepend(b);
        }""")
        button = page.locator('#pending-focus')
        other = page.get_by_role('textbox', name='Another field', exact=True)
        for outcome in ('success', 'error', 'field', 'elsewhere', 'success-again', 'removed'):
            button.focus()
            expect(button).to_be_focused()
            button.press('Enter')
            expect(button).to_be_disabled()
            if outcome == 'field':
                other.click()
                expect(other).to_be_focused()
            elif outcome == 'elsewhere':
                page.locator('#focus-elsewhere').click()
                # The paragraph's nearest focusable ancestor is the app's main region (tabindex=-1).
                expect(page.locator('#main')).to_be_focused()
            elif outcome == 'removed':
                button.evaluate('b=>b.remove()')
                other.focus()
            page.evaluate('(fail)=>window.finishFixture(fail)', outcome == 'error')
            if outcome == 'removed':
                expect(button).to_have_count(0)
                expect(other).to_be_focused()
                continue
            expect(button).to_be_enabled()
            expect(button).not_to_have_attribute('aria-busy', 'true')
            if outcome == 'field':
                expect(other).to_be_focused()
            elif outcome == 'elsewhere':
                expect(page.locator('#main')).to_be_focused()
            else:
                expect(button).to_be_focused()
            if outcome == 'error':
                expect(page.locator('.toast').filter(has_text='Fictional delayed action failure')).to_be_visible()
    finally:
        context.close()


def test_popover_entry_preserves_preceding_controls_and_unchecked_radio_fallback(browser, live_server):
    context, page = signed_in(browser, live_server, 1440, 'light')
    try:
        for before in ['search', 'action', 'unchecked']:
            fixture(page, '<div id="entry-focus-host"></div>')
            page.evaluate("""async before => {
              const {el,popover}=await window.importBundle('dom.js');
              const host=document.getElementById('entry-focus-host');
              const open=el('button',{type:'button'},'Open entry choices');
              open.addEventListener('click',()=>popover(open,panel=>{
                if(before==='search')panel.append(el('input',{type:'search','aria-label':'Search entry choices'}));
                if(before==='action')panel.append(el('button',{type:'button'},'First entry action'));
                panel.append(el('div',{role:'radiogroup','aria-label':'Entry choices'},
                  el('button',{type:'button',role:'radio','aria-checked':'false'},'First choice'),
                  el('button',{type:'button',role:'radio','aria-checked':String(before!=='unchecked')},'Second choice')));
              },{label:'Entry focus choices'}));
              host.append(open);
            }""", before)
            opener = page.get_by_role('button', name='Open entry choices', exact=True)
            opener.focus()
            page.keyboard.press('Enter')
            panel = page.get_by_role('dialog', name='Entry focus choices', exact=True)
            first = panel.get_by_role('radio', name='First choice', exact=True)
            second = panel.get_by_role('radio', name='Second choice', exact=True)
            if before == 'search':
                expect(panel.get_by_role('searchbox', name='Search entry choices', exact=True)).to_be_focused()
                expect(second).to_have_attribute('aria-checked', 'true')
            elif before == 'action':
                expect(panel.get_by_role('button', name='First entry action', exact=True)).to_be_focused()
                expect(second).to_have_attribute('aria-checked', 'true')
            else:
                expect(first).to_be_focused()
                expect(first).to_have_attribute('aria-checked', 'false')
                expect(second).to_have_attribute('aria-checked', 'false')
            page.keyboard.press('Escape')
            expect(panel).to_have_count(0)
            expect(opener).to_be_focused()
    finally:
        context.close()
