"""Browser helpers for the Rules page (the legacy list): its status views live in the shared Saved views menu."""
import re
from playwright.sync_api import expect
from .test_browser import toolbar, close_popover


def rule_status(page, label):
    """Show one status view (Current, Needs attention, Waiting for approval, History) from the Rules toolbar's Saved views."""
    bar = page.locator('.rule-toolbar')
    toolbar(bar, 'Saved views').get_by_role('menuitemradio', name=re.compile('^' + re.escape(label) + r'\b')).first.click()
    close_popover(page)
    expect(bar.locator('button.dt-saved')).to_have_attribute('aria-label', re.compile(rf'^Saved views: {re.escape(label)}\b'))


def rule_row(scope, name):
    """A rule's row in a rules panel, by its rule's name. The row says a sentence, not the name (CONTRACT §10), so the
    name is read from `data-rk-name`; a met, unchanged rule sits under the "N more are met" toggle, which is opened."""
    row = scope.locator('.rk-row[data-rk-name="%s"]' % name.replace('\\', '\\\\').replace('"', '\\"'))
    fold = scope.locator('.rk-fold[aria-expanded="false"]')
    expect(row.or_(fold).first).to_be_visible()
    if not row.count():
        fold.first.click()
    return row


def rule_action(row, label):
    """Choose one of an opened rule row's actions under ⋯ (Why panel v2: Fix is the row's one button; Open rule, Show on
    the schedule, Adjust or Make stricter for this rota, an exception and Make department default are in the menu)."""
    head = row.locator('.rk-row-head')
    if head.get_attribute('aria-expanded') != 'true':
        head.click()
    row.locator('.rk-more').click()
    row.page.get_by_role('menu', name='More actions').get_by_role('menuitem', name=re.compile('^' + re.escape(label))).click()


def rules_by_family(h, page, role='coordinator'):
    """Group the Rules list by Family on this device before it opens: Question (A–F) is the default (Why panel v2).
    The setting is kept per account, so `role` is the one signed in."""
    me = h.get(role, '/api/me')
    key = f"rota.rules.tree.{me['identity_id']}.{me['department']['id']}"
    page.evaluate("([k, v]) => localStorage.setItem(k, JSON.stringify(v))", [key, {'group': 'family', 'question_default': True}])
