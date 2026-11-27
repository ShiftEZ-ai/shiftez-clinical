"""Schedule › Print or export: one dialog with a live paged preview (the pages that print), presets (built-in and saved,
shared, defaults) and page settings; PDF through the print window on the chosen paper, PNG and CSV. Light colours in
either theme. See docs/design/schedule-views.md (Export) and docs/design/view-defaults-and-print-presets.md."""
import os
from pathlib import Path
import re

import pypdfium2
import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, planner_tool  # noqa: F401
from .test_browser_qa_planner import checked_rota, settled

pytestmark = pytest.mark.browser


def open_export(page):
    planner_tool(page, 'Export…')
    dialog = page.get_by_role('dialog', name='Print or export', exact=True)
    preview = dialog.get_by_role('img', name='Preview of the pages', exact=True)
    expect(preview.locator('.export-page').first).to_be_visible()
    return dialog, preview


def preset(page, dialog, name):
    dialog.get_by_role('button', name=re.compile('^Preset: ')).click()
    return page.get_by_role('menu', name='Presets', exact=True).get_by_role('menuitemradio', name=re.compile('^' + re.escape(name)))


def test_export_pages_the_view_and_saves_pdf_and_png(browser, live_server, h, tmp_path):
    checked_rota(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner'); settled(page)
        page.evaluate('() => { window.print = () => { window.__printed = document.body.classList.contains("exporting") && document.querySelectorAll("#export-print .export-page").length; }; }')
        page.evaluate("() => document.documentElement.setAttribute('data-theme', 'dark')")
        dialog, preview = open_export(page)
        # Pages of the view under a title block that says it is a draft, in light colours, numbered.
        expect(preview.locator('.export-page').first.locator('.export-head')).to_contain_text('Draft, not published')
        expect(preview.locator('.export-body .wk-grid').first).to_be_visible()
        assert preview.locator('.export-page').first.evaluate('n => getComputedStyle(n).backgroundColor') == 'rgb(255, 255, 255)'
        expect(preview.locator('.dt-resize')).to_have_count(0)
        expect(preview.locator('.view-legend .code-chip')).to_have_count(0)
        expect(preview).to_contain_text('Staff 01(test)')
        expect(preview.locator('.view-legend')).not_to_contain_text('Staff 01(test)')
        pages = preview.locator('.export-page')
        expect(pages.last.locator('.export-foot')).to_contain_text(re.compile(r'Page \d+ of \d+'))
        expect(dialog.get_by_role('status')).to_contain_text(re.compile(r'^\d+ pages? · A4 landscape'))
        # Every page repeats the column headers; Key off drops the legend.
        expect(pages.locator('.wk-head')).to_have_count(pages.count())
        dialog.get_by_role('checkbox', name='Key', exact=True).uncheck()
        expect(preview.locator('.view-legend')).to_have_count(0)
        # Paper and orientation size the pages; the preset is marked changed.
        dialog.get_by_label('Paper', exact=True).select_option('a3')
        dialog.get_by_label('Orientation', exact=True).select_option('portrait')
        expect(dialog.get_by_role('status')).to_contain_text('A3 portrait')
        expect(dialog.get_by_role('button', name=re.compile('^Preset: Standard.*, changed$'))).to_be_visible()
        # PDF: the pages alone go to the print window on the chosen paper, then everything is put back.
        dialog.get_by_role('button', name='Print…', exact=True).click()
        page.wait_for_function('() => window.__printed > 0')
        assert page.evaluate('() => [...document.adoptedStyleSheets].some(s => [...s.cssRules].some(r => r.cssText.includes("297mm 420mm")))')
        folder = Path(os.getenv('PW_TRACE_DIR', str(tmp_path))).parent / 'exports'
        folder.mkdir(parents=True, exist_ok=True)
        pdf = page.pdf(path=str(folder / 'schedule-a3-portrait.pdf'), prefer_css_page_size=True, print_background=True)
        assert pdf.startswith(b'%PDF-')
        with pypdfium2.PdfDocument(pdf) as document:
            assert len(document) == page.evaluate('window.__printed')
            # Paper wraps a name between whole words (c879b084c): the PDF's text breaks the line there, never the word.
            text = ' '.join(''.join(document[i].get_textpage().get_text_range() for i in range(len(document))).split())
            assert 'Staff 01(test)' in text and 'Staff 02(test)' in text
            width, height = document.get_page_size(0)
            assert abs(width - 297 * 72 / 25.4) < 1 and abs(height - 420 * 72 / 25.4) < 1
        page.evaluate("() => window.dispatchEvent(new Event('afterprint'))")
        expect(page.locator('#export-print')).to_have_count(0)
        assert not page.evaluate('() => [...document.adoptedStyleSheets].some(s => [...s.cssRules].some(r => r.cssText.includes("@page")))')
        assert page.evaluate("() => document.documentElement.getAttribute('data-theme')") == 'dark'
        # The settings stay for the session: the next dialog opens with them.
        dialog, preview = open_export(page)
        expect(dialog.get_by_label('Paper', exact=True)).to_have_value('a3')
        # PNG: one image per page; only a single-page document completes the dialog.
        dialog.get_by_role('radiogroup', name='Format').get_by_role('radio', name='Image (PNG)', exact=True).click()
        page_count = preview.locator('.export-page').count()
        with page.expect_download() as download:
            dialog.get_by_role('button', name='Save PNG', exact=True).click()
        assert download.value.suggested_filename.endswith('.png')
        download.value.save_as(str(folder / 'schedule.png'))
        with open(download.value.path(), 'rb') as f:
            head = f.read(24)
        assert head[:8] == b'\x89PNG\r\n\x1a\n'
        assert int.from_bytes(head[16:20], 'big') > 400  # width in pixels
        if page_count == 1:
            expect(dialog).to_have_count(0)
        else:
            expect(dialog).to_be_visible()
            expect(page.get_by_text(f'Page 1 of {page_count} saved.', exact=True)).to_be_visible()
            dialog.get_by_role('button', name='Cancel', exact=True).click()
    finally:
        context.close()


def test_presets_save_share_default_and_delete(browser, live_server, h):
    checked_rota(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner'); settled(page)
        dialog, preview = open_export(page)
        # The first built-in has the API's null target; a saved preset below must use its actual id.
        dialog.get_by_role('button', name=re.compile('^Preset: ')).click()
        menu = page.get_by_role('menu', name='Presets', exact=True)
        expect(menu.get_by_role('menuitem', name='Set as my default', exact=True)).to_have_count(1)
        with page.expect_response(lambda r: r.url.endswith('/api/commands') and r.request.method == 'POST'
                                  and r.request.post_data_json.get('kind') == 'table_view.set_default') as changed:
            menu.get_by_role('menuitem', name='Set as my default', exact=True).click()
        assert changed.value.ok
        assert changed.value.request.post_data_json['payload'] == {'table_key': 'schedule_print', 'scope': 'personal', 'view_id': None}
        # A built-in preset sets every field at once: Handout is greyscale.
        preset(page, dialog, 'Handout').click()
        expect(preview.locator('.export-doc')).to_have_class(re.compile(r'\bgrey\b'))
        expect(dialog.get_by_role('radiogroup', name='Colour').get_by_role('radio', name='Greyscale')).to_have_attribute('aria-checked', 'true')
        dialog.get_by_role('button', name=re.compile('^Preset: ')).click()
        menu = page.get_by_role('menu', name='Presets', exact=True)
        expect(menu.get_by_role('menuitem', name=re.compile('^Set as .*default$'))).to_have_count(0)
        expect(menu.get_by_role('menuitem', name='New preset…', exact=True)).to_have_count(1)
        page.keyboard.press('Escape')
        # Save as a new shared preset, then make it the department's default.
        dialog.get_by_role('radiogroup', name='Margins').get_by_role('radio', name='Wide', exact=True).click()
        dialog.get_by_role('button', name=re.compile('^Preset: ')).click()
        page.get_by_role('menu', name='Presets').get_by_role('menuitem', name='New preset…').click()
        form = page.get_by_role('dialog', name='New preset', exact=True)
        form.get_by_label('Preset name', exact=True).fill('Ward handout')
        form.get_by_role('checkbox', name='Share with the department').check()
        form.get_by_role('button', name='Save preset', exact=True).click()
        expect(dialog.get_by_role('button', name='Preset: Ward handout', exact=True)).to_be_visible()
        dialog.get_by_role('button', name='Preset: Ward handout', exact=True).click()
        with page.expect_response(lambda r: r.url.endswith('/api/commands') and r.request.method == 'POST'
                                  and r.request.post_data_json.get('kind') == 'table_view.set_default') as changed:
            page.get_by_role('menu', name='Presets').get_by_role('menuitem', name='Set as department default').click()
        assert changed.value.ok
        expect(page.get_by_text('This list now opens with this view for the department.', exact=True)).to_be_visible()
        stored = page.evaluate("() => fetch('/api/table-views?table=schedule_print').then(r => r.json())")
        view = next(v for v in stored['views'] if v['name'] == 'Ward handout')
        assert view['scope'] == 'department' and view['definition']['print']['margins'] == 'wide'
        assert stored['defaults']['department_view_id'] == view['id']
        assert changed.value.request.post_data_json['payload'] == {'table_key': 'schedule_print', 'scope': 'department', 'view_id': view['id']}
        dialog.get_by_role('button', name='Cancel', exact=True).click()
    finally:
        context.close()
    # Someone else in the department opens Print with the department's default preset.
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page, 'planner'); settled(page)
        dialog, preview = open_export(page)
        expect(dialog.get_by_role('button', name='Preset: Ward handout', exact=True)).to_be_visible()
        expect(dialog.get_by_role('radiogroup', name='Margins').get_by_role('radio', name='Wide')).to_have_attribute('aria-checked', 'true')
        # Delete is reversible, so it acts at once (D42), then Undo brings it back.
        dialog.get_by_role('button', name='Preset: Ward handout', exact=True).click()
        page.get_by_role('menu', name='Presets').get_by_role('menuitem', name='Delete “Ward handout”').click()
        expect(dialog.get_by_role('button', name=re.compile('^Preset: Standard'))).to_be_visible()
        # Undo restores it on the server, then the dialog reads its presets again: open the menu once that read is in.
        with page.expect_response(lambda r: r.request.method == 'GET' and '/api/table-views?table=schedule_print' in r.url):
            page.get_by_role('button', name='Undo', exact=True).click()
        preset(page, dialog, 'Ward handout')
        expect(page.get_by_role('menu', name='Presets').get_by_role('menuitemradio', name=re.compile('^Ward handout'))).to_be_visible()
    finally:
        context.close()


def test_range_page_breaks_and_csv(browser, live_server, h):
    checked_rota(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner'); settled(page)
        dialog, preview = open_export(page)
        # This month, a page per week: the Week view drawn once per week, each week starting a page.
        dialog.get_by_label('Range', exact=True).select_option('month')
        dialog.get_by_label('Page break', exact=True).select_option('week')
        heads = preview.locator('.export-page .export-head')
        expect(heads.nth(3)).to_be_visible()
        assert len(set(heads.all_text_contents())) == heads.count()  # each a different week
        # Closing puts the Schedule back as it was.
        dialog.get_by_role('button', name='Cancel', exact=True).click()
        expect(page.locator('#schedule-view')).to_have_attribute('aria-label', 'Week view')
        # CSV of the List: every row and person, without the actions column.
        navigate(page, 'schedule/list'); settled(page)
        dialog, _ = open_export(page)
        dialog.get_by_role('radiogroup', name='Format').get_by_role('radio', name='Data (CSV)', exact=True).click()
        with page.expect_download() as download:
            dialog.get_by_role('button', name='Save CSV', exact=True).click()
        text = open(download.value.path(), encoding='utf-8-sig').read()
        head = text.splitlines()[0]
        assert head.startswith('When,') and 'Actions' not in head
        assert 'Staff 01(test)' in text and 'Staff 02(test)' in text
    finally:
        context.close()
