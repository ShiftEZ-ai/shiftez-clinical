"""Bounded future export acceptance: real files, transient display state and deterministic close races.

Each image is one paper page of checked_rota's two sessions, never a stacked month canvas. Formula encoding is
covered by test_web_print_presets; PDF decoding remains in test_browser_schedule_export. These are prepared tests,
not evidence of browser qualification. Department scale and the full role/device/theme sweep remain separate gates.
"""
import csv
import io
import re
import zipfile
from dataclasses import replace

from PIL import Image, ImageChops
import pytest
from playwright.sync_api import expect

from apps.api.main import create_app
from packages.contracts.schemas import PRINT_IMAGE_DPI, PRINT_IMAGE_DPI_DEFAULT
from .test_browser import (browser, live_server, sign_in, navigate, planner_tool, toolbar,  # noqa: F401
                           close_popover, filter_by, view_settings, fonts_ready, show_names)
from .test_browser_qa_planner import checked_rota, settled
from .test_browser_schedule_export import open_export, preset
from .test_browser_final_shared_controls import capture
from .test_browser_schedule_team_pills import full_names

from .test_browser import DRAFT_CHIP, draft_chip  # noqa: F401
pytestmark = pytest.mark.browser
NAMES = ('Staff 01(test)', 'Staff 02(test)')


def format_as(dialog, label):
    dialog.get_by_role('radiogroup', name='Format', exact=True).get_by_role('radio', name=label, exact=True).click()


def adjust(page, dialog):
    dialog.get_by_role('button', name='Adjust what shows…', exact=True).click()
    return page.get_by_role('dialog', name='Adjust what shows', exact=True)


def screen_state(page):
    """Observable screen and exact device List state, without importing the implementation's state projection."""
    return page.locator('#schedule-view').evaluate('''v=>({
        label:v.getAttribute('aria-label'),text:v.innerText,
        sessions:[...v.querySelectorAll('[data-session]')].map(n=>n.dataset.session),
        columns:[...v.querySelectorAll('thead th[data-col]')].map(n=>[n.dataset.col,n.innerText]),
        cells:[...v.querySelectorAll('td[data-col]')].map(n=>[n.dataset.col,n.className,n.innerText]),
        device:Object.fromEntries(Object.entries(localStorage).filter(([k])=>k.startsWith('rota.table.')&&k.endsWith('planner-list')))
    })''')


# Both Schedule renderers (grid pills and the phone agenda) retain full names.
# Observe the real renderer's SVG input; never replace its image, canvas or pixels. Text and nonblank name regions
# alone do not prove readable output; role-badge pixel locations also check layout, and actual-file review is required.
RECORD_IMAGE = '''()=>{window.exportImages=[];const decode=HTMLImageElement.prototype.decode;
    HTMLImageElement.prototype.decode=async function(){await decode.call(this);
        if(this.src.startsWith('data:image/svg+xml;')){
            const xml=new DOMParser().parseFromString(decodeURIComponent(this.src.split(',').slice(1).join(',')),'image/svg+xml');
            window.exportImages.push({text:xml.documentElement.textContent,
                names:[...xml.querySelectorAll('.pchip-name,.ag-what small')].map(n=>n.textContent),
                chips:[...xml.querySelectorAll('DRAFT_CHIP')].map(n=>({person:n.getAttribute('data-person'),classes:n.getAttribute('class')}))});
        }
    };}'''.replace('DRAFT_CHIP', DRAFT_CHIP)


def decoded_png(download, tmp_path, name, size):
    path = tmp_path / name
    download.save_as(str(path))
    assert download.suggested_filename.endswith('.png')
    with Image.open(path) as image:
        assert image.format == 'PNG'
        image.load()  # Decode the whole downloaded file, not just IHDR.
        assert image.size == size
        rgba = image.convert('RGBA')
        assert rgba.getchannel('A').getextrema() == (255, 255)
        rgb = rgba.convert('RGB')
        assert ImageChops.difference(rgb, Image.new('RGB', size, 'white')).getbbox()
        return rgb


def name_regions(preview):
    return preview.locator('.export-page').evaluate('''p=>{const r=p.getBoundingClientRect();
                return [...p.querySelectorAll('.pchip-name,.ag-what small')].map(n=>{const b=n.getBoundingClientRect();
                    return {name:n.textContent,box:[(b.left-r.left)/r.width,(b.top-r.top)/r.height,
                        (b.right-r.left)/r.width,(b.bottom-r.top)/r.height],fits:n.scrollWidth<=n.clientWidth&&n.scrollHeight<=n.clientHeight};});}''')


def check_name_pixels(image, boxes, names):
    for name in names:
        matching = [b for b in boxes if b['name'] == name and b['fits']]
        assert matching, (name, boxes)
        for b in matching:
            crop = image.crop(tuple(round(x * image.size[i % 2]) for i, x in enumerate(b['box'])))
            assert all(high > low for low, high in crop.getextrema()), (name, b)


def role_badge_regions(preview):
    """Use the loaded styles/cascade and preview layout, without a copied palette or CSS snapshot."""
    # The preview's extra display zoom changes wrapping; PNG uses the document at paper zoom.
    return preview.locator('.export-page').evaluate('''p=>{
        const doc=p.closest('.export-doc'),zoom=doc.style.getPropertyValue('zoom'),priority=doc.style.getPropertyPriority('zoom');
        try {
            doc.style.setProperty('zoom','1');
            const r=p.getBoundingClientRect(),g=document.createElement('canvas').getContext('2d');
            // Not the printed key's "Supervision team" swatch (team_view.ts: a .team-box.lg-mark of two marks, no person).
            return [...p.querySelectorAll('.team-box:not(.lg-mark) .pchip')].map(n=>{
                const b=n.getBoundingClientRect();g.clearRect(0,0,1,1);g.fillStyle=getComputedStyle(n).backgroundColor;
                g.fillRect(0,0,1,1);return {role:n.textContent,colour:[...g.getImageData(0,0,1,1).data],
                    box:[(b.left-r.left)/r.width,(b.top-r.top)/r.height,(b.right-r.left)/r.width,(b.bottom-r.top)/r.height]};});
        } finally {if(zoom)doc.style.setProperty('zoom',zoom,priority);else doc.style.removeProperty('zoom');}
    }''')


def check_role_badge_pixels(image, badges):
    """A nonblank name crop can pass even when team names become clipped glyph columns in the actual PNG."""
    assert badges, 'No team role badges measured'
    for badge in badges:
        assert badge['colour'][3] == 255, badge
        assert 0 <= badge['box'][0] < badge['box'][2] <= 1, badge
        assert 0 <= badge['box'][1] < badge['box'][3] <= 1, badge
        crop = image.crop(tuple(round(x * image.size[i % 2]) for i, x in enumerate(badge['box'])))
        assert tuple(badge['colour'][:3]) in crop.getdata(), ('Team role badge displaced in downloaded PNG', badge)


def tag_bounds(scope):
    tags = scope.locator('.pchip-tags,.pchip-tags .tag-chip')
    assert tags.count() > 0
    for tag in tags.all():
        assert tag.evaluate('''n=>{
            const r=n.getBoundingClientRect(),p=n.closest('.pchip').getBoundingClientRect();
            return n.scrollWidth<=n.clientWidth+1 && n.scrollHeight<=n.clientHeight+1 &&
                r.left>=p.left-1 && r.right<=p.right+1 && r.top>=p.top-1 && r.bottom<=p.bottom+1;
        }'''), 'A tag overflows its person pill'


@pytest.mark.parametrize('dpi', tuple(PRINT_IMAGE_DPI), ids=lambda dpi: f'{dpi}dpi')
def test_png_selected_quality_is_exact_at_both_device_densities(browser, live_server, h, tmp_path, dpi):
    checked_rota(h)
    # The pair shares the tiny API fixture, but fresh contexts prevent session settings leaking between DPRs.
    for dpr in (1, 2):
        width, scheme = {96: (1440, 'light'), 150: (1440, 'dark'),
                         200: (390, 'light'), 300: (390, 'dark')}[dpi]
        context, page = sign_in(browser, live_server, 'coordinator', device_scale_factor=dpr,
                                viewport={'width': width, 'height': 838})
        page.emulate_media(color_scheme=scheme)
        try:
            navigate(page, 'schedule/week'); settled(page); fonts_ready(page)
            dialog, preview = open_export(page)
            dialog.get_by_label('Scale', exact=True).select_option('fit_page')
            expect(preview.locator('.export-page')).to_have_count(1)
            format_as(dialog, 'Image (PNG)')
            dialog.get_by_role('radiogroup', name='Image quality', exact=True).get_by_role(
                'radio', name=PRINT_IMAGE_DPI[dpi], exact=True).click()
            # Paper dimensions are an independent physical expectation, not pagePixels() or summary text.
            size = (round(297 / 25.4 * dpi), round(210 / 25.4 * dpi))
            expect(dialog.get_by_role('status')).to_contain_text(f'1 page · {size[0]} × {size[1]} px')
            boxes = preview.locator('.export-page').evaluate('''p=>{const r=p.getBoundingClientRect();
                return [...p.querySelectorAll('.pchip-name,.ag-what small')].map(n=>{const b=n.getBoundingClientRect();
                    return {name:n.textContent,box:[(b.left-r.left)/r.width,(b.top-r.top)/r.height,
                        (b.right-r.left)/r.width,(b.bottom-r.top)/r.height],fits:n.scrollWidth<=n.clientWidth&&n.scrollHeight<=n.clientHeight};});}''')
            for name in NAMES:
                assert any(name in b['name'] and b['fits'] for b in boxes), boxes
            capture(page, tmp_path, f'export-quality-{width}-{scheme}-dpr{dpr}')
            preview.scroll_into_view_if_needed()
            capture(page, tmp_path, f'export-preview-{width}-{scheme}-dpr{dpr}')
            page.evaluate(RECORD_IMAGE)
            with page.expect_download() as saved:
                dialog.get_by_role('button', name='Save PNG', exact=True).click()
            image = decoded_png(saved.value, tmp_path, f'{dpi}dpi-dpr{dpr}.png', size)
            rendered = page.evaluate('exportImages')
            assert len(rendered) == 1
            for name in NAMES:
                assert any(name in text for text in rendered[0]['names'])
                for b in (b for b in boxes if name in b['name']):
                    crop = image.crop(tuple(round(x * size[i % 2]) for i, x in enumerate(b['box'])))
                    assert all(high > low for low, high in crop.getextrema()), (name, b)
            expect(dialog).to_have_count(0)  # A single page completes this export.
        finally:
            context.close()


def test_multipage_png_saves_only_the_chosen_page_and_keeps_dialog(browser, live_server, h, tmp_path):
    checked_rota(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'schedule/week'); settled(page)
        dialog, preview = open_export(page)
        dialog.get_by_label('Range', exact=True).select_option('2weeks')
        dialog.get_by_label('Scale', exact=True).select_option('fit_page')
        dialog.get_by_label('Page break', exact=True).select_option('week')
        expect(preview.locator('.export-page')).to_have_count(2)
        format_as(dialog, 'Image (PNG)')
        dialog.get_by_role('radiogroup', name='Image quality').get_by_role('radio', name=PRINT_IMAGE_DPI[96], exact=True).click()
        page.evaluate(RECORD_IMAGE)
        for number in (2, 1):
            dialog.get_by_label('Page to save', exact=True).select_option(str(number))
            head = preview.locator('.export-page').nth(number - 1).locator('.export-head').inner_text()
            with page.expect_download() as saved:
                dialog.get_by_role('button', name='Save PNG', exact=True).click()
            assert f'page {number}' in saved.value.suggested_filename
            decoded_png(saved.value, tmp_path, f'page-{number}.png', (1123, 794))
            rendered = page.evaluate('exportImages.at(-1)')
            assert all(word in rendered['text'] for word in head.split())
            assert any('Staff 01(test)' in name for name in rendered['names']) == (number == 1)
            expect(dialog).to_be_visible()
            expect(page.get_by_text(f'Page {number} of 2 saved.', exact=True)).to_be_visible()
            expect(dialog.get_by_label('Page to save', exact=True)).to_have_value('2')
        dialog.get_by_role('button', name='Cancel', exact=True).click()
    finally:
        context.close()


def test_multipage_png_all_pages_saves_one_zip_with_every_page(browser, live_server, h, tmp_path):
    checked_rota(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    downloads = []
    def record_download(download):
        downloads.append(download)

    page.on('download', record_download)
    try:
        navigate(page, 'schedule/week'); settled(page)
        open_export(page)
        dialog = page.locator('dialog[open]')
        preview = dialog.get_by_role('img', name='Preview of the pages', exact=True)
        dialog.get_by_label('Range', exact=True).select_option('2weeks')
        dialog.get_by_label('Scale', exact=True).select_option('fit_page')
        dialog.get_by_label('Page break', exact=True).select_option('week')
        expect(preview.locator('.export-page')).to_have_count(2)
        format_as(dialog, 'Image (PNG)')
        dialog.get_by_role('radiogroup', name='Image quality').get_by_role(
            'radio', name=PRINT_IMAGE_DPI[96], exact=True).click()
        page_to_save = dialog.get_by_label('Page to save', exact=True)
        expect(page_to_save.locator('option').last).to_have_text('All pages (.zip)')
        expect(page_to_save).to_have_value('1')

        head = preview.locator('.export-page').first.locator('.export-head-main')
        title = head.locator('.export-title').inner_text()
        period = head.locator(':scope > span').first.inner_text().split(' · ', 1)[-1]
        draft = head.locator('.export-status.draft')
        name = f'{title} {period}' + (f' ({draft.inner_text().lower()})' if draft.count() else '')
        expected = {f'{name} page {number}.png' for number in (1, 2)}
        page_to_save.select_option('all')
        with page.expect_download() as saved:
            dialog.get_by_role('button', name='Save PNG', exact=True).click()
        assert len(downloads) == 1
        assert downloads[0] is saved.value
        assert saved.value.suggested_filename.endswith('.zip')

        archive = tmp_path / 'all-pages.zip'
        saved.value.save_as(str(archive))
        with zipfile.ZipFile(archive) as zipped:
            assert set(zipped.namelist()) == expected
            for name in expected:
                data = zipped.read(name)
                assert data.startswith(b'\x89PNG\r\n\x1a\n')
                assert len(data) >= 24 and data[12:16] == b'IHDR'
                assert int.from_bytes(data[16:20], 'big') > 0
                assert int.from_bytes(data[20:24], 'big') > 0
                with Image.open(io.BytesIO(data)) as image:
                    assert image.format == 'PNG'
                    image.load()
    finally:
        context.close()


def test_key_repeats_on_every_page_and_goes_with_include_key(browser, live_server, h):
    checked_rota(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'schedule/month'); settled(page)
        dialog, preview = open_export(page)
        # One view, no page breaks, drawn large: its week bands run over several pages of a single chunk.
        dialog.get_by_label('Scale', exact=True).select_option('custom')
        zoom = dialog.get_by_label('Zoom (%)', exact=True)
        zoom.fill('200'); zoom.dispatch_event('change')
        pages = preview.locator('.export-page')
        expect(pages.nth(1)).to_be_visible()
        # One key per page, between the rows and the footer, inside the sheet.
        placed = pages.evaluate_all('''ps=>ps.map(p=>{const k=p.querySelectorAll('.view-legend'),b=p.querySelector('.export-body');
            if(k.length!==1)return k.length;const r=k[0].getBoundingClientRect(),s=p.getBoundingClientRect();
            return r.top>=b.getBoundingClientRect().bottom-1&&r.bottom<=p.querySelector('.export-foot').getBoundingClientRect().top+1
                &&r.left>=s.left-1&&r.right<=s.right+1&&k[0].innerText.trim()!=='';})''')
        assert len(placed) >= 2 and all(p is True for p in placed), placed
        dialog.get_by_role('checkbox', name='Key', exact=True).uncheck()
        expect(preview.locator('.view-legend')).to_have_count(0)
        expect(pages.nth(1)).to_be_visible()
        dialog.get_by_role('button', name='Cancel', exact=True).click()
    finally:
        context.close()


@pytest.mark.parametrize('layout', ['week', 'day', 'people', 'list'])
@pytest.mark.parametrize('query', ['Staff 01(test)', 'No such export person'], ids=['match', 'zero'])
def test_csv_uses_the_original_dates_filter_and_person_search(browser, live_server, h, tmp_path, layout, query):
    workspace, typ = checked_rota(h)
    # Distinct distractors exercise date, person and clinic. Keep the canonical roster for every command.
    workspace = h.shift(workspace, '2026-10-07T08:00:00+03:00', '2026-10-07T12:00:00+03:00',
                        people=[h.seed['people'][2]], typ=typ)['workspace']
    workspace = h.shift(workspace, '2026-10-04T14:00:00+03:00', '2026-10-04T18:00:00+03:00',
                        people=h.seed['people'][:2], service=1, typ=typ)['workspace']
    workspace = h.shift(workspace, '2026-10-06T08:00:00+03:00', '2026-10-06T12:00:00+03:00',
                        people=h.seed['people'][:1], typ=typ)['workspace']
    h.lifecycle(workspace, 'validate')
    catalogues = h.get('coordinator', '/api/catalogues')
    service = next(s['name'] for s in catalogues['services'] if s['id'] == h.seed['services'][0])
    place = next(s['name'] for s in catalogues['locations'] if s['id'] == h.seed['locations'][0])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'schedule/' + layout + '/2026-10-04'); settled(page)
        filter_by(page.locator('.planner-toolbar'), 'Service', [service])
        search = page.get_by_role('searchbox', name='Search people' if layout == 'people' else 'Search sessions', exact=True)
        search.fill(query); search.press('Enter')
        # Wait for the observable result, not a debounce delay.
        if layout == 'people':
            expect(page.locator('#schedule-view [data-row-person]')).to_have_count(1 if query == NAMES[0] else 0)
        elif layout == 'day':
            # Day uses accessible timeline bars, while the other session renderers use data-session.
            expect(page.locator('#schedule-view .sd-track button.sd-bar')).to_have_count(1 if query == NAMES[0] else 0)
        else:
            count = 2 if query == NAMES[0] else 0
            expect(page.locator('#schedule-view [data-session]')).to_have_count(count)
        before = screen_state(page)
        downloads = []
        page.on('download', lambda d: downloads.append(d))
        # An empty filtered view has no paper page; choose CSV without a paper-only readiness assertion.
        planner_tool(page, 'Export…')
        dialog = page.get_by_role('dialog', name='Print or export', exact=True)
        format_as(dialog, 'Data (CSV)')
        if query == NAMES[0]:
            expect(dialog.get_by_role('status')).to_contain_text(f'{1 if layout == "day" else 2} rows')
            with page.expect_download() as saved:
                dialog.get_by_role('button', name='Save CSV', exact=True).click()
            path = tmp_path / f'{layout}.csv'
            saved.value.save_as(str(path))
            raw = path.read_bytes()
            assert raw.startswith(b'\xef\xbb\xbf') and raw.endswith(b'\r\n')
            assert b'\n' not in raw.replace(b'\r\n', b'')
            rows = list(csv.reader(io.StringIO(raw.decode('utf-8-sig'), newline='')))
            assert rows[0] == ['When', 'Duty', 'Place', 'Service', 'People', 'Status']
            # Constructed from the fixture, never read from the List or the CSV preview. A session search draws the person
            # it names first (SW3-A-31, e5a7e55b9), whatever the random ids' order; the People layout's search finds rows
            # of people and leaves each session's people in their own order (app.ts weekView.matchPeople).
            people = sorted(zip(h.seed['people'][:2], NAMES, strict=True)) if layout == 'people' else zip(NAMES, NAMES)
            expected = {'When': '4 Oct 2026 08:00–12:00', 'Duty': 'QA clinic', 'Place': place,
                        'Service': service, 'People': '; '.join(name for _, name in people), 'Status': 'Covered'}
            expected_rows = [expected]
            if layout != 'day':
                expected_rows.append({**expected, 'When': '6 Oct 2026 08:00–12:00', 'People': NAMES[0]})
            assert [dict(zip(rows[0], row, strict=True)) for row in rows[1:]] == expected_rows
            expect(dialog).to_have_count(0)
        else:
            # There is no table with zero matches. Honest no-file behavior, never an unrelated/unfiltered CSV.
            expect(dialog.get_by_role('button', name='Save CSV', exact=True)).to_be_disabled()
            expect(dialog.get_by_role('img', name='Preview of the pages')).to_have_text('Nothing to list for these days and filters.')
            dialog.get_by_role('button', name='Cancel', exact=True).click()
            assert downloads == []
        settled(page)
        expect(search).to_have_value(query)
        assert screen_state(page) == before
    finally:
        context.close()


@pytest.mark.parametrize('exit_path', ['cancel-close', 'escape', 'back', 'navigation', 'reset', 'what'])
def test_export_list_columns_wrap_and_order_restore_exact_screen_and_device(browser, live_server, h, tmp_path, exit_path):
    checked_rota(h)
    other = h.command('coordinator', 'table_view.create', {'table_key': 'planner', 'name': 'Export alternate List',
        'definition': {'planner': {'mode': 'list', 'range': 'week'}}})['view']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'home'); navigate(page, 'schedule/list'); settled(page)
        # Establish a nondefault device state before capture, so restoring defaults cannot pass.
        panel = view_settings(page.locator('.planner-toolbar'), 'Columns')
        panel.get_by_role('checkbox', name='Status', exact=True).uncheck()
        close_popover(page)
        before = screen_state(page)
        dialog, preview = open_export(page)
        panel = adjust(page, dialog)
        panel.locator('.vs-row', has_text='Columns').click()
        panel.get_by_role('checkbox', name='Service', exact=True).uncheck()
        panel.get_by_role('switch', name='Wrap all columns', exact=True).check()
        panel.get_by_role('button', name='Move People up', exact=True).click()
        close_popover(page)
        expect(preview.locator('th[data-col=service]')).to_have_count(0)
        expect(preview.locator('td[data-col=people].dt-wrap').first).to_be_visible()
        assert preview.locator('thead th[data-col]').evaluate_all('ns=>ns.map(n=>n.dataset.col)') == ['when', 'duty', 'place', 'people']
        assert screen_state(page) != before
        if exit_path == 'reset':
            panel = adjust(page, dialog)
            panel.get_by_role('button', name='Reset to the view', exact=True).click()
            expect(preview.locator('th[data-col=service]')).to_have_count(1)
            assert screen_state(page) == before
        elif exit_path == 'what':
            dialog.get_by_label('What', exact=True).select_option(other['id'])
            expect(preview.locator('th[data-col=service]')).to_have_count(1)
            dialog.get_by_label('What', exact=True).select_option('')
            expect(preview.locator('th[data-col=service]')).to_have_count(1)
            assert screen_state(page) == before
        if exit_path in ('reset', 'what', 'cancel-close'):
            dialog.get_by_role('button', name='Cancel', exact=True).click()
        elif exit_path == 'escape':
            page.keyboard.press('Escape')
            # Adjusted controls are guarded like other dialogs; accept the existing close choice.
            expect(dialog.locator('.unsaved-bar')).to_contain_text('Not saved.')
            dialog.get_by_role('button', name='Discard changes', exact=True).click()
        elif exit_path == 'back':
            page.go_back()
        else:
            navigate(page, 'home')
        expect(dialog).to_have_count(0)
        if exit_path in ('back', 'navigation'):
            navigate(page, 'schedule/list')
        settled(page)
        assert screen_state(page) == before
        if exit_path == 'cancel-close':
            # A separate explicit Close path, after reopening the restored screen.
            dialog, _ = open_export(page)
            dialog.get_by_role('button', name='Close dialog', exact=True).click()
            expect(dialog).to_have_count(0)
            assert screen_state(page) == before
            # The same transient columns also govern the real CSV, then saving restores the original List.
            dialog, _ = open_export(page)
            format_as(dialog, 'Data (CSV)')
            panel = adjust(page, dialog)
            panel.locator('.vs-row', has_text='Columns').click()
            panel.get_by_role('checkbox', name='Service', exact=True).uncheck()
            close_popover(page)
            with page.expect_download() as saved:
                dialog.get_by_role('button', name='Save CSV', exact=True).click()
            with open(saved.value.path(), 'rb') as stream:
                raw = stream.read()
            assert raw.startswith(b'\xef\xbb\xbf') and raw.endswith(b'\r\n')
            rows = list(csv.reader(io.StringIO(raw.decode('utf-8-sig'), newline='')))
            assert rows[0] == ['When', 'Duty', 'Place', 'People']
            # A session's people come in the server's participant order (by person id, apps/api/snapshots.py), as above.
            people = '; '.join(name for _, name in sorted(zip(h.seed['people'][:2], NAMES, strict=True)))
            assert len(rows) == 3 and rows[1][3] == people and rows[2][3] == 'No one yet'
            expect(dialog).to_have_count(0)
            assert screen_state(page) == before
        page.reload(); settled(page)
        assert screen_state(page) == before
    finally:
        context.close()


def test_export_axes_properties_density_and_wrapping_reach_searchable_output(browser, live_server, h, tmp_path):
    import pypdfium2

    checked_rota(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'schedule/week'); settled(page)
        before = screen_state(page)
        dialog, preview = open_export(page)
        panel = adjust(page, dialog)
        panel.get_by_label('Rows', exact=True).select_option('person')
        panel.get_by_label('Columns', exact=True).select_option('week')
        panel.get_by_role('radiogroup', name='Cells show').get_by_role('radio', name='Counts', exact=True).click()
        close_popover(page)
        expect(preview.get_by_role('grid', include_hidden=True)).to_have_attribute('aria-label', re.compile('^Counts, people by week'))
        panel = adjust(page, dialog)
        panel.get_by_label('Columns', exact=True).select_option('day')
        panel.get_by_role('radiogroup', name='Cells show').get_by_role('radio', name='Duty codes', exact=True).click()
        close_popover(page)
        expect(preview.locator('.export-body')).to_contain_text(NAMES[0])
        # Return to duties, where card properties and density are advertised.
        panel = adjust(page, dialog)
        panel.get_by_label('Rows', exact=True).select_option('duty')
        close_popover(page)
        # Adjust offers what the drawn preview supports: wait for the duty calendar before asking for its density.
        expect(preview.locator('.wk-grid')).to_have_count(1)
        panel = adjust(page, dialog)
        panel.get_by_role('radiogroup', name='Cells show').get_by_role('radio', name='Sessions', exact=True).click()
        panel.get_by_role('radiogroup', name='Density').get_by_role('radio', name='Compact', exact=True).click()
        panel.locator('.vs-row', has_text='Properties').click()
        panel.get_by_role('button', name='Hide Time', exact=True).click()
        close_popover(page)
        expect(preview.locator('.wk-fact-time')).to_have_count(0)
        expect(preview.locator('.wk-grid')).to_have_class(re.compile(r'\bcompact\b'))
        capture(page, tmp_path, 'export-axes-properties-wrap')
        page.evaluate('() => { window.print = () => {}; }')
        count = preview.locator('.export-page').count()
        dialog.get_by_role('button', name='Print…', exact=True).click()
        expect(page.locator('#export-print .export-page')).to_have_count(count)
        pdf = page.pdf(path=str(tmp_path / 'display.pdf'), prefer_css_page_size=True, print_background=True)
        with pypdfium2.PdfDocument(pdf) as document:
            assert len(document) == count
            width, height = document.get_page_size(0)
            assert abs(width - 297 * 72 / 25.4) < 1 and abs(height - 210 * 72 / 25.4) < 1
            # Paper wraps a name between whole words (c879b084c): the PDF's text breaks the line there, never the word.
            text = ' '.join(''.join(document[i].get_textpage().get_text_range() for i in range(len(document))).split())
            assert all(name in text for name in NAMES)
            assert '08:00–12:00' not in text
        page.evaluate("() => window.dispatchEvent(new Event('afterprint'))")
        settled(page)
        assert screen_state(page) == before
    finally:
        context.close()


@pytest.mark.parametrize('layout', ['week', 'month'])
def test_tags_are_opt_in_ordered_by_id_and_follow_rename_recolour(browser, live_server, h, tmp_path, layout):
    checked_rota(h)
    tags = [h.configure('tags', {'name': name, 'labels': ['people'], 'colour': colour})['record']
            for name, colour in [('Export teaching', 'violet'), ('Export clinical', 'blue')]]
    for tag in tags:
        h.command('admin', 'tag.members', {'add': [{'kind': 'people', 'id': h.seed['people'][0]}],
                  'reason': 'Fictional export display'}, tag['id'], tag['revision'])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'schedule/' + layout); settled(page)
        show_names(page, 'full')
        view = page.locator('#schedule-view')
        expect(view.locator('.pchip-tags')).to_have_count(0)
        panel = view_settings(page.locator('.planner-toolbar'), 'Properties')
        panel.get_by_role('button', name='Show Person tags', exact=True).click()
        close_popover(page)
        panel = view_settings(page.locator('.planner-toolbar'), 'Colour by')
        panel.get_by_role('radiogroup', name='Colour by').get_by_role('radio', name='Tag', exact=True).click()
        for tag in tags:
            panel.get_by_role('button', name='Show #' + tag['name'], exact=True).click()
        panel.get_by_role('button', name='Move #Export clinical', exact=True).press('Alt+ArrowUp')
        close_popover(page)
        first = view.locator(draft_chip(h.seed["people"][0]))
        neutral = view.locator(draft_chip(h.seed["people"][1]))
        expect(first.first).to_have_class(re.compile(r'\bduty-blue\b'))
        expect(neutral.first).to_have_class(re.compile(r'\bduty-slate\b'))
        full_names(view, h.seed['people'][:2], names=NAMES)
        tag_bounds(view)
        capture(page, tmp_path, f'tags-screen-{layout}')
        neutral_classes = neutral.first.get_attribute('class')
        expect(view.locator('.pchip-tags').first).to_contain_text('#Export teaching')
        toolbar(page.locator('.planner-toolbar'), 'Saved views').get_by_role('menuitem', name='New view…', exact=True).click()
        form = page.get_by_role('dialog', name='New view', exact=True)
        form.get_by_label('View name', exact=True).fill('Export tag priority')
        form.get_by_role('button', name='Save view', exact=True).click()
        expect(page.locator('.planner-toolbar button.dt-saved')).to_contain_text('Export tag priority')
        saved = next(v for v in h.get('coordinator', '/api/table-views?table=planner')['views'] if v['name'] == 'Export tag priority')
        ids = [tags[1]['id'], tags[0]['id']]
        assert saved['definition']['planner']['colour_tags'] == ids
        updated = next(t for t in h.get('admin', '/api/catalogues')['tags'] if t['id'] == tags[1]['id'])
        h.configure('tags', {'name': 'Renamed export clinical', 'labels': ['people'], 'colour': 'teal'},
                    updated['id'], updated['revision'])
        page.reload(); settled(page)
        expect(first.first).to_have_class(re.compile(r'\bduty-teal\b'))
        expect(neutral.first).to_have_attribute('class', neutral_classes)
        expect(view.locator('.pchip-tags').first).to_contain_text('#Renamed export clinical')
        assert next(v for v in h.get('coordinator', '/api/table-views?table=planner')['views'] if v['id'] == saved['id'])['definition']['planner']['colour_tags'] == ids
        before = screen_state(page)
        dialog, preview = open_export(page)
        panel = adjust(page, dialog)
        panel.locator('.vs-row', has_text='Colour by').click()
        panel.get_by_role('button', name='Move #Export teaching', exact=True).press('Alt+ArrowUp')
        close_popover(page)
        expect(preview.locator(draft_chip(h.seed["people"][0])).first).to_have_class(re.compile(r'\bduty-violet\b'))
        expect(preview.locator('.pchip-tags').first).to_contain_text('#Renamed export clinical')
        full_names(preview.locator('.export-body'), h.seed['people'][:2], names=NAMES)
        tag_bounds(preview)
        expect(preview.locator('.view-legend')).to_contain_text('#Export teaching')
        expect(preview.locator('.view-legend')).to_contain_text('No chosen tag')
        for name in NAMES:
            expect(preview.locator('.view-legend')).not_to_contain_text(name)
        dialog.get_by_label('Scale', exact=True).select_option('fit_page')
        format_as(dialog, 'Image (PNG)')
        dialog.get_by_role('radiogroup', name='Image quality').get_by_role('radio', name=PRINT_IMAGE_DPI[96], exact=True).click()
        expect(preview.locator('.export-page')).to_have_count(1)
        capture(page, tmp_path, f'export-tags-{layout}')
        boxes = name_regions(preview)
        page.evaluate(RECORD_IMAGE)
        with page.expect_download() as downloaded:
            dialog.get_by_role('button', name='Save PNG', exact=True).click()
        image = decoded_png(downloaded.value, tmp_path, f'{layout}-tags.png', (1123, 794))
        check_name_pixels(image, boxes, NAMES)
        assert all(name in page.evaluate('exportImages[0].names') for name in NAMES)
        rendered = page.evaluate('exportImages[0]')
        assert '#Renamed export clinical' in rendered['text']
        assert any(c['person'] == h.seed['people'][0] and 'duty-violet' in c['classes'] for c in rendered['chips'])
        assert any(c['person'] == h.seed['people'][1] and 'duty-slate' in c['classes'] for c in rendered['chips'])
        expect(dialog).to_have_count(0)
        settled(page)
        assert screen_state(page) == before
    finally:
        context.close()


def test_preset_quality_reload_revision_history_and_undo(browser, live_server, h):
    checked_rota(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'schedule/week'); settled(page)
        dialog, _ = open_export(page)
        format_as(dialog, 'Image (PNG)')
        dialog.get_by_role('radiogroup', name='Image quality').get_by_role('radio', name=PRINT_IMAGE_DPI[300], exact=True).click()
        dialog.get_by_role('button', name=re.compile('^Preset: ')).click()
        page.get_by_role('menu', name='Presets').get_by_role('menuitem', name='New preset…', exact=True).click()
        form = page.get_by_role('dialog', name='New preset', exact=True)
        form.get_by_label('Preset name', exact=True).fill('Export quality history')
        with page.expect_response(lambda r: r.url.endswith('/api/commands') and r.request.method == 'POST'
                                  and r.request.post_data_json.get('kind') == 'table_view.create') as created:
            form.get_by_role('button', name='Save preset', exact=True).click()
        original = created.value.json()['view']
        assert original['definition']['print']['image_dpi'] == 300
        assert 'wrap' not in original['definition']['print']  # the retired Long names choice is never written (DL-014)
        dialog.get_by_role('button', name='Cancel', exact=True).click()
        page.reload(); settled(page)
        dialog, _ = open_export(page)
        preset(page, dialog, 'Export quality history').click()
        expect(dialog.get_by_role('radiogroup', name='Image quality').get_by_role('radio', name=PRINT_IMAGE_DPI[300], exact=True)).to_have_attribute('aria-checked', 'true')
        dialog.get_by_role('radiogroup', name='Image quality').get_by_role('radio', name=PRINT_IMAGE_DPI[PRINT_IMAGE_DPI_DEFAULT], exact=True).click()
        dialog.get_by_role('button', name=re.compile('^Preset: ')).click()
        with page.expect_response(lambda r: r.url.endswith('/api/commands') and r.request.method == 'POST'
                                  and r.request.post_data_json.get('kind') == 'table_view.update') as changed:
            page.get_by_role('menu', name='Presets').get_by_role('menuitem', name='Update “Export quality history”', exact=True).click()
        reply = changed.value.json()
        assert reply['previous_view']['definition'] == original['definition']
        assert reply['view']['revision'] == original['revision'] + 1
        assert not {'image_dpi', 'wrap'} & reply['view']['definition']['print'].keys()
        with page.expect_response(lambda r: r.url.endswith('/api/commands') and r.request.method == 'POST'
                                  and r.request.post_data_json.get('kind') == 'table_view.update') as undone:
            page.get_by_role('button', name='Undo', exact=True).click()
        assert undone.value.json()['view']['definition'] == original['definition']
        assert undone.value.json()['view']['revision'] == original['revision'] + 2
        preset(page, dialog, 'Export quality history').click()
        expect(dialog.get_by_role('radiogroup', name='Image quality').get_by_role('radio', name=PRINT_IMAGE_DPI[300], exact=True)).to_have_attribute('aria-checked', 'true')
        stored = next(v for v in h.get('coordinator', '/api/table-views?table=schedule_print')['views'] if v['id'] == original['id'])
        assert stored['definition'] == original['definition']
        history = h.get('auditor', '/api/audit?target_id=' + original['id'])['events']
        assert [event['action'] for event in reversed(history)] == ['table_view.create', 'table_view.update', 'table_view.update']
        assert len({event['detail']['receipt_id'] for event in history}) == 3
        dialog.get_by_role('button', name='Cancel', exact=True).click()
    finally:
        context.close()


@pytest.mark.parametrize('exit_path', ['cancel', 'escape'])
def test_preview_finishing_during_save_cannot_enable_duplicate_or_download_after_close(browser, live_server, h, exit_path):
    checked_rota(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    downloads = []
    page.on('download', lambda d: downloads.append(d))
    try:
        navigate(page, 'schedule/week'); settled(page)
        # Existing late-host-draw harness, with real pages/renderer and a barrier after actual PNG encoding.
        page.evaluate('''async()=>{const{exportDialog}=await window.importBundle('schedule_export.js');
            const{session}=await window.importBundle('print_presets.js');session.last=null;
            const source={title:'Export race',period:'Fixture week',department:'Fictional department',status:'Draft',
                draft:true,by:'Fictional coordinator',filters:[],view:document.querySelector('#schedule-view').cloneNode(true)};
            window.exportRace={draws:0,restores:0,encodes:0,finished:false};const p=exportRace;
            const toBlob=HTMLCanvasElement.prototype.toBlob;
            HTMLCanvasElement.prototype.toBlob=function(callback,...args){p.encodes++;
                toBlob.call(this,blob=>{window.releasePng=()=>{callback(blob);p.finished=true;};},...args);};
            exportDialog({views:[],rota:true,canShare:true,restore:()=>p.restores++,draw:()=>{
                p.draws++;return new Promise(resolve=>window.finishPreview=()=>resolve([source]));}});}''')
        dialog = page.get_by_role('dialog', name='Print or export', exact=True)
        page.wait_for_function('() => exportRace.draws===1')
        page.evaluate('finishPreview()')
        expect(dialog.locator('.export-page').first).to_be_visible()
        format_as(dialog, 'Image (PNG)')
        dialog.get_by_role('button', name='Save PNG', exact=True).click()
        page.wait_for_function('() => typeof releasePng === "function"')
        save = dialog.get_by_role('button', name=re.compile(r'^(Saving…|Save PNG)$'))
        expect(save).to_be_disabled()
        dialog.get_by_label('Range', exact=True).select_option('week')
        page.wait_for_function('() => exportRace.draws===2')
        page.evaluate('finishPreview()')
        expect(dialog.locator('.export-page').first).to_be_visible()
        expect(save).to_be_disabled()
        assert page.evaluate('exportRace.encodes') == 1
        if exit_path == 'cancel':
            dialog.get_by_role('button', name='Cancel', exact=True).click()
        else:
            page.keyboard.press('Escape')
        expect(dialog).to_have_count(0)
        page.evaluate('''async()=>{releasePng();await new Promise(resolve=>requestAnimationFrame(resolve));}''')
        assert page.evaluate('exportRace') == {'draws': 2, 'restores': 1, 'encodes': 1, 'finished': True}
        assert downloads == []
        expect(page.locator('.export-offscreen')).to_have_count(0)
    finally:
        context.close()


LONG_NAMES = ('Dr. Fictional Saranoura Binqassimah', 'Dr. Fictional Abdulwahhab Alkhuzaymah', 'Dr. Fictional Reemas Alghadhbanotaibi',
              'Dr. Fictional Mohanned Linnet')  # invented, never a real person


# What is really cut on paper, not the computed property alone: an element that ends in "…" and holds more than it shows.
REALLY_CUT = '''p=>[...p.querySelectorAll('.export-body *')].filter(n=>{const s=getComputedStyle(n);
  return s.textOverflow==='ellipsis'&&s.overflowX!=='visible'&&n.scrollWidth>n.clientWidth+1;})
  .map(n=>n.className+': '+n.textContent.slice(0,40))'''


def busy_week(h):
    """A full week: two clinics, a morning and an afternoon session every day, three long-named people on each, checked."""
    people = []
    for name in LONG_NAMES:
        pid = h.configure('people', {'display_name': name})['record']['id']
        h.command('coordinator', 'membership.create', {'person_id': pid, 'starts_on': '2020-01-01', 'category_id': h.seed['category']})
        for service in h.seed['services'][:2]:
            h.command('credentialer', 'eligibility.create', {'person_id': pid, 'service_id': service, 'starts_on': '2020-01-01'})
        people.append(pid)
    w = h.workspace('Fictional busy print week', '2026-10-04', '2026-10-11')
    for day in range(4, 11):
        for start, end in (('08:00', '12:00'), ('13:00', '17:00')):
            for service in (0, 1):
                crew = [people[(day + service + k) % len(people)] for k in range(3)]
                w = h.shift(w, f'2026-10-{day:02}T{start}:00+03:00', f'2026-10-{day:02}T{end}:00+03:00', people=crew, service=service)['workspace']
    return h.lifecycle(w, 'validate')['workspace']


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_week_prints_whole_words_whole_counts_and_few_pages(browser, live_server, h, tmp_path, scheme):
    """on paper a name never breaks inside a word, each day's count stays inside its header, and a
    week (two clinics here) fits on 1–2 A4 landscape pages with the default preset. The pages counted are the ones print
    and PDF use (one renderer)."""
    w = busy_week(h)
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 900}, color_scheme=scheme)
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}"); settled(page)
        dialog, preview = open_export(page)
        pages = preview.locator('.export-page')
        expect(pages.first.locator('.pchip-name').first).to_be_visible()
        assert 1 <= pages.count() <= 2, pages.count()
        # Every word of every name is on one line (a word split across lines has two client rects).
        split = preview.evaluate('''p=>{const out=[];for(const n of p.querySelectorAll('.pchip-name')){const t=n.firstChild;
            if(!t||t.nodeType!==3)continue;const text=t.textContent;let at=0;
            for(const word of text.split(/\\s+/)){const i=text.indexOf(word,at);if(!word){continue;}at=i+word.length;
              const r=document.createRange();r.setStart(t,i);r.setEnd(t,i+word.length);
              const lines=new Set([...r.getClientRects()].filter(x=>x.width>0).map(x=>Math.round(x.top)));if(lines.size>1)out.push(word);}}
            return out;}''')
        assert split == [], split
        # Each day's places count sits inside its day header (not cut at the edge).
        cut = preview.evaluate('''p=>[...p.querySelectorAll('.wk-day')].filter(d=>{const c=d.querySelector('.wk-cov');if(!c)return false;
            const a=c.getBoundingClientRect(),b=d.getBoundingClientRect();return a.right>b.right+1||a.left<b.left-1||c.scrollWidth>c.clientWidth+1;}).length''')
        assert cut == 0, cut
        # DL-014: Appearance ends after Colour (the Long names row is gone); one capture per colour scheme.
        appearance = dialog.get_by_role('region', name='Appearance', exact=True)
        expect(appearance.get_by_role('radiogroup')).to_have_count(2)
        expect(appearance.get_by_role('radiogroup').last).to_have_accessible_name('Colour')
        appearance.scroll_into_view_if_needed()
        capture(page, tmp_path, f'export-appearance-{scheme}')
        dialog.get_by_role('button', name='Cancel', exact=True).click()
        expect(dialog).to_have_count(0)
        # A List on paper with long clinic names (plain-text cells) and long people: as the view has it, then with
        # every column wrapped (Adjust what shows). DL-016: neither cuts a cell; both wrap.
        clinic = 'Fictional long-named outreach and follow-up clinic'
        long = small_clinics(h, clinic)
        navigate(page, f"schedule/list/2026-10-04?edit={long['id']}"); settled(page)
        dialog, preview = open_export(page)
        expect(preview.locator('.export-page .pchip-name').first).to_be_visible()
        expect(preview).to_contain_text(clinic + ' 1')
        expect(preview).to_contain_text('Saranoura Binqassimah')
        assert preview.evaluate(NAMES_CUT) == []
        cut = preview.evaluate(REALLY_CUT)
        assert cut == [], cut[:10]
        capture(page, tmp_path, f'export-list-long-cell-default-{scheme}')
        panel = adjust(page, dialog)
        panel.locator('.vs-row', has_text='Columns').click()
        panel.get_by_role('switch', name='Wrap all columns', exact=True).check()
        close_popover(page)
        expect(preview.locator('td[data-col=people].dt-wrap').first).to_be_visible()
        expect(preview).to_contain_text(clinic + ' 1')
        expect(preview).to_contain_text('Saranoura Binqassimah')
        assert preview.evaluate(NAMES_CUT) == []
        cut = preview.evaluate(REALLY_CUT)
        assert cut == [], cut[:10]
        capture(page, tmp_path, f'export-list-long-cell-wrapped-{scheme}')
        dialog.get_by_role('button', name='Cancel', exact=True).click()
    finally:
        context.close()


FIRST = ('Abdulwahhab', 'Saranoura', 'Reemas', 'Mohanned', 'Nouflah', 'Faisalan', 'Hessanah', 'Khalidun', 'Lameesah', 'Turkiyan',
         'Ghadeerah', 'Badrulla')
FAMILY = ('Alkhuzaymah', 'Binqassimah', 'Alghadhbani', 'Linnet', 'Alqahtaniyah', 'Alshammarani')


def department_week(h):
    """About department scale: 8 clinics, 2–4 sessions a day each, 3–6 people a session, 24 invented long names, checked."""
    # About 400 fixture commands in one window (192 by the credentialer alone): the command rate limit has its own
    # tests (test_rate_limits.py), so the harness lifts it for this build, as test_golden_scenarios does.
    h.app.state.engine.dispose()
    h.settings = replace(h.settings, rate_commands_per_minute=10_000)
    h.app, h.clients = create_app(h.settings), {}
    seed = h.get('admin', '/api/catalogues')
    services = [h.configure('services', {'name': f'Fictional clinic {i + 1}', 'starts_on': '2020-01-01'})['record']['id'] for i in range(8)]
    people = []
    for i in range(24):
        pid = h.configure('people', {'display_name': f'Dr. Fictional {FIRST[i % len(FIRST)]} {FAMILY[i % len(FAMILY)]}'})['record']['id']
        h.command('coordinator', 'membership.create', {'person_id': pid, 'starts_on': '2020-01-01', 'category_id': h.seed['category']})
        for service in services:
            h.command('credentialer', 'eligibility.create', {'person_id': pid, 'service_id': service, 'starts_on': '2020-01-01'})
        people.append(pid)
    assert seed['locations'], 'the seed has a place'
    w = h.workspace('Fictional department print week', '2026-10-04', '2026-10-11')
    times = (('08:00', '12:00'), ('12:00', '16:00'), ('16:00', '20:00'), ('20:00', '23:00'))
    for day in range(4, 11):
        for c, service in enumerate(services):
            for t, (start, end) in enumerate(times[:2 + (c % 3)]):
                a, b = f'2026-10-{day:02}T{start}:00+03:00', f'2026-10-{day:02}T{end}:00+03:00'
                crew = [people[(c * 3 + t * 5 + day + k) % len(people)] for k in range(3 + (c + t + day) % 4)]
                w = h.command('coordinator', 'shift.create', {'workspace_id': w['id'], 'assignment_type_id': h.seed['type'],
                    'location_id': h.seed['locations'][0], 'service_ids': [service], 'start': a, 'end': b,
                    'participants': [{'person_id': p, 'start': a, 'end': b} for p in crew], 'attributes': {},
                    'override_reason': 'Synthetic assignment for the print scale test'}, revision=w['revision'])['workspace']
    return h.lifecycle(w, 'validate')['workspace']


def test_department_week_prints_on_few_pages_and_fits_one_page_when_asked(browser, live_server, h, record_property):
    """a department-scale week prints on few A4 landscape pages with the default preset: on paper a
    cell's people flow side by side and wrap between whole pills, never inside a name. AGENTS › Limits: the count is
    recorded (print_pages) and the budget is generous until CI has measured it, then measured plus a margin. Fit on one
    page puts it on one page; its zoom is recorded (print_fit_zoom) to judge legibility."""
    w = department_week(h)
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 900})
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}"); settled(page)
        dialog, preview = open_export(page)
        pages = preview.locator('.export-page')
        expect(pages.first.locator('.pchip-name').first).to_be_visible()
        count = pages.count()
        record_property('print_pages', count)
        print(f'print_pages={count}')
        # Pills share lines: more pills than lines in a busy cell (one pill per line was the old 17-page layout).
        shared = preview.evaluate('''p=>[...p.querySelectorAll('.wk-cell')].some(c=>{const tops=new Set([...c.querySelectorAll(':scope>.pchip')]
            .map(x=>Math.round(x.getBoundingClientRect().top)));return c.querySelectorAll(':scope>.pchip').length>tops.size;})''')
        assert shared, 'no cell put two pills on one line'
        # shortcut: provisional budget (AGENTS › Limits: measure before limits); set it to the recorded count plus one
        # once CI has measured it at this scale.
        assert 1 <= count <= 6, count
        dialog.get_by_label('Scale', exact=True).select_option('fit_page')
        expect(pages).to_have_count(1)
        zoom = float(preview.locator('.export-body').first.evaluate("b=>getComputedStyle(b).zoom||'1'"))
        record_property('print_fit_zoom', zoom)
        print(f'print_fit_zoom={zoom:.2f}')
        dialog.get_by_role('button', name='Cancel', exact=True).click()
    finally:
        context.close()


# maintenance (LEAD-QA on 6cea25859): print chips ended names in "…" ("Dr. Exampl…"), so names were lost on paper.
NAMES_CUT = '''p=>[...p.querySelectorAll('.export-page .pchip-name')].flatMap(n=>{const s=getComputedStyle(n),why=[];
  if(s.textOverflow==='ellipsis')why.push('text-overflow');
  if((s.webkitLineClamp||s.getPropertyValue('-webkit-line-clamp'))&&!['none',''].includes(String(s.webkitLineClamp||s.getPropertyValue('-webkit-line-clamp'))))why.push('line-clamp');
  if(n.scrollWidth>n.clientWidth+1||n.scrollHeight>n.clientHeight+1)why.push('clipped');
  return why.length?[n.textContent+': '+why.join(', ')]:[];})'''


def test_printed_names_are_never_cut_or_ellipsized(browser, live_server, h):
    """maintenance: on a printed or exported page a person's name is never cut. No ellipsis, no line clamp, no clipped word:
    it wraps between whole words on as many lines as it needs. There is no setting that cuts it (DL-014)."""
    w = busy_week(h)
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 900})
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}"); settled(page)
        dialog, preview = open_export(page)
        expect(preview.locator('.export-page .pchip-name').first).to_be_visible()
        expect(dialog.get_by_role('radiogroup', name='Long names')).to_have_count(0)
        cut = preview.evaluate(NAMES_CUT)
        assert cut == [], cut[:10]
        dialog.get_by_role('button', name='Cancel', exact=True).click()
    finally:
        context.close()


def small_clinics(h, clinic='Fictional small clinic'):
    """Five clinics, one morning session a day each with one long-named person: rows short enough for several clinics
    to share a page."""
    services = [h.configure('services', {'name': f'{clinic} {i + 1}', 'starts_on': '2020-01-01'})['record']['id'] for i in range(5)]
    people = []
    for i, service in enumerate(services):
        pid = h.configure('people', {'display_name': LONG_NAMES[i % len(LONG_NAMES)] + f' {i}'})['record']['id']
        h.command('coordinator', 'membership.create', {'person_id': pid, 'starts_on': '2020-01-01', 'category_id': h.seed['category']})
        h.command('credentialer', 'eligibility.create', {'person_id': pid, 'service_id': service, 'starts_on': '2020-01-01'})
        people.append(pid)
    w = h.workspace(clinic + 's print week', '2026-10-04', '2026-10-11')
    for day in range(4, 11):
        for c, service in enumerate(services):
            a, b = f'2026-10-{day:02}T08:00:00+03:00', f'2026-10-{day:02}T12:00:00+03:00'
            w = h.command('coordinator', 'shift.create', {'workspace_id': w['id'], 'assignment_type_id': h.seed['type'],
                'location_id': h.seed['locations'][0], 'service_ids': [service], 'start': a, 'end': b,
                'participants': [{'person_id': people[c], 'start': a, 'end': b}], 'attributes': {},
                'override_reason': 'Synthetic assignment for the print flow test'}, revision=w['revision'])['workspace']
    return h.lifecycle(w, 'validate')['workspace']


def test_clinics_flow_onto_the_first_page(browser, live_server, h):
    """maintenance: page 1 is not kept to two clinics. Pagination keeps nothing together but one duty row (never split
    across sheets) and a clinic heading with its first row (never last on a page), so short clinics flow and fill the
    page."""
    w = small_clinics(h)
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 900})
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}"); settled(page)
        dialog, preview = open_export(page)
        first = preview.locator('.export-page').first
        expect(first.locator('.pchip-name').first).to_be_visible()
        groups = first.locator('.wk-row.wk-group:not(.wk-subgroup):not(.export-continued)').count()
        assert groups > 2, f'page 1 holds {groups} clinics'
        dialog.get_by_role('button', name='Cancel', exact=True).click()
    finally:
        context.close()
