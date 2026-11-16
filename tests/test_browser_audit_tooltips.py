"""The shared hover/focus help stays readable at edges and gives way to the user's actual action."""
import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, sign_in, keyboard_focus  # noqa: F401

pytestmark = pytest.mark.browser


@pytest.mark.parametrize('width,scheme', [(1440, 'light'), (1440, 'dark'), (390, 'light'), (390, 'dark')])
def test_shared_tooltips_fit_edges_and_preserve_hover_focus_and_actions(browser, live_server, width, scheme):
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': width, 'height': 900})
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, 'programmes')
        rotation_help = ('External rotations block overlapping duties. '
                         'Internal rotations allow duties when the person is clinically eligible.')
        # What blocks duties is in the page's help (approved Timeline mockup, 21 Oct 2026); the board shows it as a conflict.
        expect(page.locator('#main').get_by_text(rotation_help, exact=True)).to_have_count(0)
        page_help = page.get_by_role('button', name='About this page', exact=True)
        page_help.focus()
        page.keyboard.press('Tab')
        page.keyboard.press('Shift+Tab')
        expect(page_help).to_be_focused()
        page_tip = page.get_by_role('tooltip')
        expect(page_tip).to_have_text(rotation_help)  # the help that changes what you do, no tagline (N-15)
        expect(page_help).to_have_accessible_name('About this page')
        assert page_tip.get_attribute('id') in page_help.get_attribute('aria-describedby').split()
        page.keyboard.press('Escape')
        expect(page_tip).to_have_count(0)
        expect(page_help).to_be_focused()
        navigate(page, 'people')
        page.evaluate("""async () => {
          const {el,button,compact,field,hoverCard,popover,showDialog}=await window.importBundle('dom.js');
          const f=el('div',{id:'tooltip-fixture'});document.body.append(f);
          for(const [k,v] of Object.entries({position:'fixed',inset:'0','z-index':'4000','pointer-events':'none'}))f.style.setProperty(k,v);
          const place=(n,x,y)=>{for(const [k,v] of Object.entries({position:'absolute',left:x,top:y,'pointer-events':'auto'}))n.style.setProperty(k,v);f.append(n);return n;};
          const long=window.tooltipText='Complete help with a long reference '+ 'UnbrokenReference'.repeat(18)+' and the final words.';
          f.append(el('span',{id:'existing-description',class:'sr-only'},'Existing description'));
          for(const edge of ['top-left','top-right','bottom-left','bottom-right']){
            const b=el('button',{id:edge,type:'button','aria-label':edge,'aria-describedby':'existing-description','data-tip':edge+': '+long},'?');
            b.style.setProperty('width','40px');b.style.setProperty('height','40px');
            const clip=el('div',{},b);for(const [k,v] of Object.entries({width:'40px',height:'40px',overflow:'hidden'}))clip.style.setProperty(k,v);
            place(clip,edge.endsWith('left')?'8px':'calc(100% - 48px)',edge.startsWith('top')?'8px':'calc(100% - 48px)');
          }
          const compactRow=place(el('div',{id:'compact-row',class:'page-actions'},compact(button('Compact action',()=>{}),'edit')),'48px','330px');
          compactRow.querySelector('button').id='compact-hint';
          const clipped=place(el('h3',{id:'clipped-hint',tabindex:0,'data-ctip':long},long),'48px','390px');
          for(const [k,v] of Object.entries({width:'80px','white-space':'nowrap',overflow:'hidden','text-overflow':'ellipsis'}))clipped.style.setProperty(k,v);
          const rich=place(el('button',{id:'rich-hint',type:'button'},'Rich card ',el('span',{id:'child-hint','data-tip':'A specific child mark'},'ⓘ')),'48px','450px');
          hoverCard(rich,()=>[el('p',{},'Rich card content')]);
          const menu=place(el('button',{id:'menu-hint',type:'button','data-tip':'Open the choices','aria-haspopup':'dialog','aria-expanded':'false'},'Choices'),'48px','510px');
          menu.addEventListener('click',()=>popover(menu,p=>p.append(el('p',{},'The actual choices')),{label:'Tooltip test choices'}));
          const modal=place(button('Open help modal',()=>showDialog('Tooltip test modal','',body=>{
            const rich=el('button',{id:'modal-rich',type:'button'},'Modal details');
            hoverCard(rich,()=>[el('p',{},'Modal rich card content')]);
            body.append(el('button',{id:'modal-hint',type:'button','data-tip':long},'Modal explanation'),
              el('button',{id:'modal-help',type:'button','data-help':'','data-tip':'Deliberate modal explanation'},'About this modal'),
              rich,field('Modal draft name').wrap);
          })),'48px','570px');
          modal.id='open-help-modal';
          const scroll=place(el('div',{id:'scroll-hints'},el('button',{id:'scroll-hint',type:'button','data-tip':'Help in a scrolling collection'},'Scroll help'),el('div',{},'More rows')),'48px','660px');
          scroll.style.setProperty('height','70px');scroll.style.setProperty('overflow','auto');
          scroll.lastElementChild.style.setProperty('height','240px');
          scroll.append(el('button',{id:'scroll-end-hint',type:'button','data-tip':'Help reached by keyboard scrolling'},'End help'));
        }""")
        tip = page.get_by_role('tooltip')
        text = page.evaluate('window.tooltipText')

        def fits():
            assert tip.evaluate("""n => {const r=n.getBoundingClientRect();
                return r.left>=7.5 && r.right<=innerWidth-7.5 && r.top>=7.5 && r.bottom<=innerHeight-7.5
                  && n.scrollWidth<=n.clientWidth+1;}""")

        for edge in ['top-left', 'top-right', 'bottom-left', 'bottom-right']:
            trigger = page.locator('#' + edge)
            trigger.focus()
            expect(tip).to_have_text(edge + ': ' + text)
            fits()
            expect(trigger).to_be_focused()
            expect(trigger).to_have_accessible_name(edge)
            assert trigger.get_attribute('aria-describedby').split() == ['existing-description', tip.get_attribute('id')]
            page.keyboard.press('Escape')
            expect(tip).to_have_count(0)
            expect(trigger).to_be_focused()
            expect(trigger).to_have_attribute('aria-describedby', 'existing-description')

        # Hover help never takes the pointer from what lies under it, and never takes focus.
        page.locator('#main').focus()
        page.mouse.move(width // 2, 250)
        page.locator('#top-right').hover()
        expect(tip).to_have_text('top-right: ' + text)
        expect(tip).to_have_css('pointer-events', 'none')
        expect(page.locator('#main')).to_be_focused()
        page.mouse.move(width // 2, 750)
        expect(tip).to_have_count(0)

        # Focus moved by a click or by the app (panel arrows, a closing menu) is not keyboard focus: no help appears.
        page.locator('#compact-hint').click()
        page.locator('#top-left').focus()
        page.clock.run_for(400)  # past the 280ms help delay
        expect(tip).to_have_count(0)
        page.keyboard.press('Shift')  # keyboard modality again for the focus checks below

        # Labelled controls need no duplicate hint; the same control gets one after the actual compact CSS hides it.
        compact = page.locator('#compact-hint')
        compact.focus()
        expect(tip).to_have_count(0)
        page.locator('#compact-row').evaluate("n => n.classList.add('tight')")
        page.locator('#main').focus()
        compact.focus()
        expect(tip).to_have_text('Compact action')
        page.locator('#clipped-hint').focus()
        expect(tip).to_have_text(text)
        expect(tip).to_have_count(1)
        fits()

        # The nearest child explanation replaces its parent's rich card; it never layers a second tooltip over it.
        page.locator('#rich-hint').focus()
        expect(tip).to_have_text('Rich card content')
        page.locator('#child-hint').hover()
        expect(tip).to_have_text('A specific child mark')
        expect(tip).to_have_count(1)
        fits()

        # Activating a control gives way to its real popup. Escape then closes the popup and returns useful focus.
        menu = page.locator('#menu-hint')
        menu.focus()
        expect(tip).to_have_text('Open the choices')
        menu.press('Enter')
        popup = page.get_by_role('dialog', name='Tooltip test choices', exact=True)
        expect(popup).to_contain_text('The actual choices')
        expect(tip).to_have_count(0)
        page.keyboard.press('Escape')
        expect(popup).to_have_count(0)
        expect(menu).to_be_focused()

        # A top-layer tooltip inside a modal is readable. Help shown by keyboard focus alone never costs an extra
        # Escape: one press hides it and closes the modal.
        page.locator('#open-help-modal').click()
        modal = page.get_by_role('dialog', name='Tooltip test modal', exact=True)
        modal_hint = modal.locator('#modal-hint')
        page.keyboard.press('Shift')
        modal_hint.focus()
        expect(tip).to_have_text(text)
        fits()
        page.keyboard.press('Escape')
        expect(tip).to_have_count(0)
        expect(modal).to_have_count(0)

        # A passive pointer tooltip, including the modal's focused Close button, also gives that one Escape
        # to the modal. Deliberately opened help and rich hover cards retain their own dismissal first.
        page.locator('#open-help-modal').click()
        close = modal.get_by_role('button', name='Close dialog', exact=True)
        # A dialog opens on its first field (X-06): put the focus on Close for this case.
        close.focus()
        close.hover()
        expect(close).to_be_focused()
        expect(tip).to_have_text('Close dialog')
        page.keyboard.press('Escape')
        expect(tip).to_have_count(0)
        expect(modal).to_have_count(0)

        page.locator('#open-help-modal').click()
        modal.locator('#modal-help').click()
        expect(tip).to_have_text('Deliberate modal explanation')
        page.keyboard.press('Escape')
        expect(tip).to_have_count(0)
        expect(modal).to_be_visible()
        page.keyboard.press('Escape')
        expect(modal).to_have_count(0)

        page.locator('#open-help-modal').click()
        modal.locator('#modal-rich').hover()
        expect(tip).to_have_text('Modal rich card content')
        page.keyboard.press('Escape')
        expect(tip).to_have_count(0)
        expect(modal).to_be_visible()
        page.keyboard.press('Escape')
        expect(modal).to_have_count(0)

        # The same pointer-help dismissal reaches the existing guard, preserving a dirty modal's edit.
        page.locator('#open-help-modal').click()
        modal.get_by_label('Modal draft name').fill('Half typed')
        close.hover()
        expect(tip).to_have_text('Close dialog')
        page.keyboard.press('Escape')
        expect(tip).to_have_count(0)
        bar = modal.locator('.unsaved-bar')
        expect(bar).to_contain_text('Not saved. Close anyway?')
        expect(modal.get_by_label('Modal draft name')).to_have_value('Half typed')
        bar.get_by_role('button', name='Keep editing', exact=True).click()
        expect(bar).to_have_count(0)
        page.keyboard.press('Escape')
        expect(bar).to_be_visible()
        bar.get_by_role('button', name='Discard changes', exact=True).click()
        expect(modal).to_have_count(0)
        page.keyboard.press('Shift')  # keyboard modality for the remaining focus-help checks

        # The same shared fallback escapes the clipped trigger parent when native popovers are unavailable.
        page.evaluate("""() => {window.savedPopover=Object.getOwnPropertyDescriptor(HTMLElement.prototype,'showPopover');
            delete HTMLElement.prototype.showPopover;}""")
        try:
            page.locator('#top-right').focus()
            expect(tip).to_have_text('top-right: ' + text)
            fits()
            page.keyboard.press('Escape')
            expect(tip).to_have_count(0)
        finally:
            page.evaluate("() => Object.defineProperty(HTMLElement.prototype,'showPopover',window.savedPopover)")

        # Native Tab brings an offscreen control into view; that reveal scroll must not cancel its pending help.
        page.locator('#scroll-hint').focus()
        page.keyboard.press('Tab')
        end_hint = page.locator('#scroll-end-hint')
        expect(end_hint).to_be_focused()
        assert page.locator('#scroll-hints').evaluate('n => n.scrollTop') > 0
        expect(tip).to_have_text('Help reached by keyboard scrolling')
        assert tip.get_attribute('id') in end_hint.get_attribute('aria-describedby').split()
        fits()
        page.keyboard.press('Escape')
        expect(tip).to_have_count(0)
        expect(end_hint).to_be_focused()

        # Once help is visible, scrolling the collection still dismisses it.
        page.locator('#scroll-hint').focus()
        expect(tip).to_have_text('Help in a scrolling collection')
        page.locator('#scroll-hints').evaluate('n => n.scrollTop=40')
        expect(tip).to_have_count(0)
        expect(page.locator('#scroll-hint')).to_be_focused()

        # A manual scroll hides a still-focused control before its delay ends: no detached help appears later.
        page.locator('#main').focus()
        page.locator('#scroll-hints').evaluate('n => n.scrollTop=0')
        page.locator('#scroll-hint').evaluate("""n => new Promise(resolve => {
            const scroller=n.parentElement;
            scroller.addEventListener('scroll',resolve,{once:true});
            n.focus();scroller.scrollTop=80;
        })""")
        page.clock.run_for(400)
        expect(tip).to_have_count(0)
        expect(page.locator('#scroll-hint')).to_be_focused()
        assert page.locator('#scroll-hint').get_attribute('aria-describedby') is None

        # A removed trigger or a changed viewport cannot leave stale help floating over the next view.
        page.locator('#top-left').focus()
        expect(tip).to_be_visible()
        page.locator('#top-left').evaluate('n => n.remove()')
        expect(tip).to_have_count(0)
        page.locator('#top-right').focus()
        expect(tip).to_be_visible()
        page.set_viewport_size({'width': width - 20, 'height': 880})
        expect(tip).to_have_count(0)
        expect(page.locator('#top-right')).to_be_focused()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    finally:
        context.close()


@pytest.mark.parametrize('surface,role', [('page', 'coordinator'), ('quota', 'coordinator')])
def test_explanation_only_help_opens_by_touch_enter_and_space(browser, live_server, surface, role):
    authenticated, _ = sign_in(browser, live_server, role)
    try:
        state = authenticated.storage_state()
    finally:
        authenticated.close()
    context = browser.new_context(storage_state=state, has_touch=True, viewport={'width': 390, 'height': 900},
                                  locale='en-GB', timezone_id='Asia/Riyadh')
    try:
        page = context.new_page()
        page.goto(live_server)
        expect(page.locator('#main[data-loaded-view]')).to_have_count(1, timeout=30000)
        if surface == 'page':
            navigate(page, 'programmes')
            trigger = page.get_by_role('button', name='About this page', exact=True)
            text = 'External rotations block overlapping duties. Internal rotations allow duties when the person is clinically eligible.'
        else:
            # Exercise the real shared quota component's existing explanation-only control.
            navigate(page, 'insights/workload')
            page.evaluate("""async () => {
              const {quotaPanel}=await window.importBundle('quotas.js');
              document.querySelector('#main').replaceChildren(quotaPanel({metric_version:'fixture',timezone:'Asia/Riyadh',
                basis:'Synthetic quota calculation basis.',complete:false,rows:[]}));
            }""")
            trigger = page.get_by_role('button', name='How this is calculated', exact=True)
            text = 'Synthetic quota calculation basis. Times in Asia/Riyadh.'
        tip = page.get_by_role('tooltip')
        for activate in [trigger.tap, lambda: trigger.press('Enter'), lambda: trigger.press('Space')]:
            activate()
            expect(tip).to_have_text(text)
            expect(trigger).to_be_focused()
            assert tip.get_attribute('id') in trigger.get_attribute('aria-describedby').split()
            page.keyboard.press('Escape')
            expect(tip).to_have_count(0)
            expect(trigger).to_be_focused()
        trigger.tap()
        expect(tip).to_have_text(text)
        page.locator('.topbar h1').tap()
        expect(tip).to_have_count(0)
    finally:
        context.close()


def test_pointer_help_never_opens_or_stays_once_the_pointer_has_left(browser, live_server):
    """A control focused by a click (the panel's ↓ keeps focus on itself) shows hover help only while the pointer is
    over it: otherwise that help opens later and takes the next Escape from the panel it sits in."""
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        navigate(page, 'people')
        page.evaluate("""() => {const b=document.createElement('button');b.id='clicked-help';b.type='button';b.dataset.tip='Clicked help';
          b.textContent='Next';b.addEventListener('click',()=>{const n=b.cloneNode(true);b.replaceWith(n);n.focus();});
          document.querySelector('#main').prepend(b);}""")
        tip = page.get_by_role('tooltip')
        page.clock.pause_at(page.evaluate('Date.now()') + 1000)
        # Clicked, the control is redrawn under the resting pointer and keeps focus: its hover help waits out the delay.
        page.locator('#clicked-help').click()
        page.mouse.move(5, 5)
        page.locator('#clicked-help').hover()
        expect(page.locator('#clicked-help')).to_be_focused()
        page.clock.run_for(200)
        page.mouse.move(-1, -1)
        page.clock.run_for(400)  # past the 280 ms help delay and the 120 ms grace
        expect(tip).to_have_count(0)
        # Shown under the pointer, it closes once the pointer leaves, though the control is still focused.
        page.locator('#clicked-help').hover()
        page.clock.run_for(400)
        expect(tip).to_have_text('Clicked help')
        page.mouse.move(-1, -1)
        page.clock.run_for(200)
        expect(tip).to_have_count(0)
        expect(page.locator('#clicked-help')).to_be_focused()
    finally:
        context.close()


def test_visible_help_hit_tests_once_a_frame_while_the_page_changes(browser, live_server):
    """AD-7: 40 separate changes to the page while help is shown cost at most a hit test per frame, not one each;
    a layer opened over the control still takes its help away."""
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        navigate(page, 'people')
        page.evaluate("""async () => {
          const {el}=await window.importBundle('dom.js');
          const b=el('button',{id:'busy-hint',type:'button','data-tip':'Help over a busy page'},'Busy');
          for(const [k,v] of Object.entries({position:'fixed',left:'48px',top:'200px','z-index':'4000'}))b.style.setProperty(k,v);
          document.body.append(b);
          const hit=document.elementFromPoint.bind(document);window.hitTests=0;
          document.elementFromPoint=(x,y)=>{window.hitTests++;return hit(x,y);};
        }""")
        keyboard_focus(page.locator('#busy-hint'))
        tip = page.get_by_role('tooltip')
        expect(tip).to_have_text('Help over a busy page')
        tests = page.evaluate("""async () => {
          window.hitTests=0;const box=document.createElement('div');document.querySelector('#main').append(box);
          for(let i=0;i<40;i++){box.append(document.createElement('span'));await Promise.resolve();await Promise.resolve();}
          box.textContent='';
          await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
          return window.hitTests; }""")
        assert tests <= 2, f'{tests} hit tests for 40 changes'
        expect(tip).to_have_text('Help over a busy page')
        # A layer over the control: the help goes.
        page.evaluate("""() => {const cover=document.createElement('div');cover.id='cover-layer';
          for(const [k,v] of Object.entries({position:'fixed',inset:'0','z-index':'5000',background:'transparent'}))cover.style.setProperty(k,v);
          document.body.append(cover);}""")
        expect(tip).to_have_count(0)
    finally:
        context.close()
