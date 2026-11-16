"""Closed native dialogs/popovers release their local save listener without weakening guards."""
import re
import pytest
from playwright.sync_api import expect
from .test_browser import browser,live_server,sign_in,navigate  # noqa: F401

pytestmark=pytest.mark.browser


def test_closed_dialogs_and_popovers_release_save_guards_and_dirty_forms_still_prompt(browser,live_server):
    context,page=sign_in(browser,live_server,'coordinator')
    try:
        context.add_init_script("""(() => {
            const add=EventTarget.prototype.addEventListener,remove=EventTarget.prototype.removeEventListener;
            window.saveGuardListeners=new Set();
            EventTarget.prototype.addEventListener=function(type,listener,...options){
                if(type==='rota-form-saved')window.saveGuardListeners.add(listener);
                return add.call(this,type,listener,...options);
            };
            EventTarget.prototype.removeEventListener=function(type,listener,...options){
                if(type==='rota-form-saved')window.saveGuardListeners.delete(listener);
                return remove.call(this,type,listener,...options);
            };
        })();""")
        page.reload();navigate(page,'rules')
        baseline=page.evaluate('() => window.saveGuardListeners.size')
        for _ in range(3):
            page.get_by_role('button',name='Add rule',exact=True).click()
            form=page.get_by_role('dialog',name='Add rule',exact=True)
            expect(form).to_be_visible()
            assert page.evaluate('() => window.saveGuardListeners.size')==baseline+1
            form.get_by_role('button',name='Close dialog',exact=True).click()
            expect(form).to_have_count(0)
            assert page.evaluate('() => window.saveGuardListeners.size')==baseline
            account=page.get_by_role('button',name=re.compile(r'^Account menu, ')).first
            account.click();expect(account).to_have_attribute('aria-expanded','true')
            assert page.evaluate('() => window.saveGuardListeners.size')==baseline+1
            page.keyboard.press('Escape');expect(account).to_have_attribute('aria-expanded','false')
            assert page.evaluate('() => window.saveGuardListeners.size')==baseline
        page.get_by_role('button',name='Add rule',exact=True).click()
        form=page.get_by_role('dialog',name='Add rule',exact=True)
        form.get_by_label('Rule title',exact=True).fill('Unsaved fictional change')
        form.get_by_role('button',name='Close dialog',exact=True).click()
        expect(form.get_by_role('button',name='Keep editing',exact=True)).to_be_visible()
        form.get_by_role('button',name='Discard changes',exact=True).click()
        expect(form).to_have_count(0)
        assert page.evaluate('() => window.saveGuardListeners.size')==baseline
    finally:
        context.close()
