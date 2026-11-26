"""Small native replacement gate for real tagged cards and downloadable paper output."""
import json
import re

import pypdfium2
import pytest
from playwright.sync_api import expect

from packages.contracts.schemas import PRINT_IMAGE_DPI
from .test_browser import (browser, live_server, sign_in, pick_view, view_settings,  # noqa: F401
                           close_popover, fonts_ready)
from .test_browser_qa_planner import checked_rota, settled
from .test_browser_schedule_team_pills import fixture, calendar, full_names, NAMES, member, team
from .test_browser_export_display import (open_export, format_as, decoded_png, RECORD_IMAGE, screen_state,
                                          tag_bounds, name_regions, check_name_pixels,
                                          role_badge_regions, check_role_badge_pixels)
from .test_browser_final_shared_controls import capture

pytestmark = pytest.mark.browser


def tag_people(h, people):
    tag = h.configure('tags', {'name': 'Fictional teaching', 'labels': ['people'],
                               'colour': 'violet'})['record']
    h.command('admin', 'tag.members', {'add': [{'kind': 'people', 'id': p} for p in people],
                                      'reason': 'Fictional tagged card acceptance'}, tag['id'], tag['revision'])
    return tag


def show_tags(page, form):
    panel = view_settings(page.locator('.planner-toolbar'), 'Properties')
    panel.get_by_role('button', name='Show Person tags', exact=True).click()
    panel.get_by_label('Show Person tags as', exact=True).select_option(form)
    close_popover(page)


@pytest.mark.parametrize('layout,density,form,width,scheme,dpi', [
    ('Week', 'Comfortable', 'chip', 1440, 'light', 96),
    ('Week', 'Compact', 'text', 1440, 'dark', 300),
    ('Month', 'Compact', 'chip', 390, 'light', 96),
    ('Month', 'Comfortable', 'text', 390, 'dark', 300),
])
def test_tag_forms_keep_team_names_actions_and_real_export(browser, live_server, h, tmp_path,
                                                          layout, density, form, width, scheme, dpi):
    w, session, people, _ = fixture(h)
    tag_people(h, [people[0], people[1], people[3]])
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': width, 'height': 900},
                            color_scheme=scheme)
    try:
        grid = calendar(page, w, layout)
        show_tags(page, form)
        panel = view_settings(page.locator('.planner-toolbar'))
        panel.get_by_role('radiogroup', name='Density').get_by_role('radio', name=density, exact=True).click()
        close_popover(page)
        fonts_ready(page)
        expect(grid.locator('.pchip-tags')).to_have_count(3)
        tag_bounds(grid)
        expect(team(grid, session['id']).locator('[data-person]')).to_have_count(3)
        # Dense views name people short (one line); the chip's label keeps the full name.
        expect(member(grid, session['id'], people[3])).to_have_attribute('aria-label', re.compile('^' + re.escape(NAMES[3]) + ','))
        if density == 'Comfortable':
            full_names(team(grid, session['id']), people[:3], names=NAMES[:3], narrow_ok=True)  # a screen cell
        for p, name in zip(people, NAMES, strict=True):
            expect(member(grid, session['id'], p)).to_have_attribute('aria-label', re.compile(re.escape(name)))
        # Standalone screen pills may ellipsize; their complete name must be recoverable by pointer and keyboard.
        standalone = member(grid, session['id'], people[3])
        standalone.hover()
        expect(standalone).to_be_in_viewport()
        expect(page.locator('.hc-head strong')).to_have_text(NAMES[3])
        expect(page.locator('.hc-head strong')).to_be_visible()
        page.mouse.move(0, 0)
        # Mark the next focus as keyboard navigation; focus() targets this roving grid stop.
        page.keyboard.press('Tab')
        standalone.focus()
        expect(page.locator('.hc-head strong')).to_have_text(NAMES[3])
        expect(page.locator('.hc-head strong')).to_be_visible()
        page.locator('.planner-toolbar button.dt-saved').focus()
        capture(page, tmp_path, f'tag-screen-{layout}-{width}-{scheme}-{form}-{density}')
        # Measure the known hover/menu row rather than asserting an invented no-shift budget.
        chip = member(grid, session['id'], people[1])
        page.mouse.move(0, 0)
        before = chip.bounding_box()
        chip.hover()
        expect(page.locator('.hc-head strong')).to_have_text(NAMES[1])
        expect(page.locator('.hc-head strong')).to_be_visible()
        hover = chip.bounding_box()
        capture(page, tmp_path, f'tag-hover-{layout}-{width}-{scheme}-{form}-{density}')
        chip.locator('.pchip-more').click()
        menu = page.get_by_role('menu', name='Actions for ' + NAMES[1], exact=True)
        expect(menu.get_by_role('menuitem', name='Remove from this session', exact=True)).to_be_visible()
        page.keyboard.press('Escape')
        page.mouse.move(0, 0)
        page.locator('.planner-toolbar button.dt-saved').focus()
        chip.focus()
        expect(chip).to_be_focused()
        expect(page.locator('.hc-head strong')).to_have_text(NAMES[1])
        focused = chip.bounding_box()
        chip.press('Shift+F10')
        expect(menu.get_by_role('menuitem', name='Remove from this session', exact=True)).to_be_visible()
        page.keyboard.press('Escape')
        (tmp_path / 'hover-geometry.json').write_text(json.dumps({'before': before, 'hover': hover, 'focus': focused,
            'full_name_recovered': NAMES[1], 'pointer_menu_reached': True, 'keyboard_menu_reached': True}, indent=2))
        # Remove focus/hover from the card before comparing screen restoration.
        page.locator('.planner-toolbar button.dt-saved').focus()
        page.mouse.move(0, 0)
        before_export = screen_state(page)
        dialog, preview = open_export(page)
        dialog.get_by_label('Scale', exact=True).select_option('fit_page')
        # Phone may default to another renderer; the original calendar fixture explicitly selected Grid.
        expect(preview.locator('.export-page')).to_have_count(1)
        # Names wrap on paper by default; the Long names choice is retired (DL-014/DL-016).
        full_names(preview.locator('.export-body'), people, names=NAMES)
        tag_bounds(preview)
        capture(page, tmp_path, f'tag-preview-{layout}-{width}-{scheme}-{form}-{density}')
        if layout == 'Week' and density == 'Compact':
            # Actual paper, including a >14ch unbroken trainee surname, searchable in full and in order.
            page.evaluate('() => {window.print=()=>{};}')
            count = preview.locator('.export-page').count()
            dialog.get_by_role('button', name='Print…', exact=True).click()
            pdf = page.pdf(path=str(tmp_path / 'tagged-long-team.pdf'), prefer_css_page_size=True, print_background=True)
            with pypdfium2.PdfDocument(pdf) as document:
                assert len(document) == count == 1
                pw, ph = document.get_page_size(0)
                assert abs(pw - 297 * 72 / 25.4) < 1 and abs(ph - 210 * 72 / 25.4) < 1
                text = ''.join(document[i].get_textpage().get_text_range() for i in range(len(document)))
                normal = re.sub(r'\s+', '', text)
                for name in NAMES:
                    assert re.sub(r'\s+', '', name) in normal, text
                supervisor = normal.index(re.sub(r'\s+', '', NAMES[0]))
                for trainee in NAMES[1:3]:
                    assert supervisor < normal.index(re.sub(r'\s+', '', trainee))
                assert '#Fictionalteaching' in normal
                document[0].render(scale=1.5).to_pil().save(tmp_path / 'tagged-long-team-pdf.png')
            page.evaluate("() => window.dispatchEvent(new Event('afterprint'))")
            dialog, preview = open_export(page)
        format_as(dialog, 'Image (PNG)')
        dialog.get_by_role('radiogroup', name='Image quality').get_by_role('radio', name=PRINT_IMAGE_DPI[dpi], exact=True).click()
        boxes = name_regions(preview)
        badges = role_badge_regions(preview)
        assert len(badges) == 3  # One supervisor and two trainees; sibling order is immaterial.
        (tmp_path / 'png-role-regions.json').write_text(json.dumps(badges, indent=2))
        page.evaluate(RECORD_IMAGE)
        with page.expect_download() as downloaded:
            dialog.get_by_role('button', name='Save PNG', exact=True).click()
        size = (round(297 / 25.4 * dpi), round(210 / 25.4 * dpi))
        image = decoded_png(downloaded.value, tmp_path, 'tagged-long-team.png', size)
        check_name_pixels(image, boxes, NAMES)
        check_role_badge_pixels(image, badges)
        rendered = page.evaluate('exportImages[0]')
        assert all(name in rendered['names'] for name in NAMES)
        assert '#Fictional teaching' in rendered['text']
        expect(dialog).to_have_count(0)
        settled(page)
        assert screen_state(page) == before_export
    finally:
        context.close()


def test_day_keeps_saved_tag_configuration_dormant(browser, live_server, h, tmp_path):
    checked_rota(h)
    tag = tag_people(h, h.seed['people'][:1])
    saved = h.command('coordinator', 'table_view.create', {'table_key': 'planner', 'name': 'Fictional saved tags',
        'definition': {'planner': {'mode': 'week', 'range': 'week', 'colour_by': 'tag', 'colour_tags': [tag['id']],
                                  'properties': [{'key': 'tags', 'on': True, 'form': 'text'}]}}})['view']
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        from .test_browser import navigate
        navigate(page, 'schedule/week'); settled(page)
        pick_view(page, saved['name']); settled(page)
        expect(page.locator('#schedule-view .pchip-tags').first).to_be_visible()
        original = next(v for v in h.get('coordinator', '/api/table-views?table=planner')['views'] if v['id'] == saved['id'])
        pick_view(page, 'Day'); settled(page)
        panel = view_settings(page.locator('.planner-toolbar'), 'Properties')
        expect(panel.locator('[data-part="tags"]')).to_have_count(0)
        expect(panel.get_by_role('button', name=re.compile('^(Show|Hide) Person tags$'))).to_have_count(0)
        close_popover(page)
        # Day's bars are sessions: Colour by offers a session's colours only (duty type, service); the saved Tag stays
        # dormant and the bars keep the duty type's colour (never a setting that does nothing).
        panel = view_settings(page.locator('.planner-toolbar'), 'Colour by')
        colours = panel.get_by_role('radiogroup', name='Colour by', exact=True)
        expect(colours.get_by_role('radio')).to_have_text(['Duty type', 'Service'])
        expect(colours.get_by_role('radio', name='Duty type', exact=True)).to_have_attribute('aria-checked', 'true')
        capture(page, tmp_path, 'day-dormant-tag-controls')
        close_popover(page)
        for layout in ['Week', 'Month']:
            pick_view(page, layout); settled(page)
            expect(page.locator('#schedule-view .pchip-tags').first).to_contain_text('#' + tag['name'])
            panel = view_settings(page.locator('.planner-toolbar'), 'Colour by')
            expect(panel.get_by_role('radio', name='Tag', exact=True)).to_have_attribute('aria-checked', 'true')
            close_popover(page)
            panel = view_settings(page.locator('.planner-toolbar'), 'Properties')
            expect(panel.get_by_role('button', name='Hide Person tags', exact=True)).to_be_visible()
            expect(panel.get_by_label('Show Person tags as', exact=True)).to_have_value('text')
            close_popover(page)
        current = next(v for v in h.get('coordinator', '/api/table-views?table=planner')['views'] if v['id'] == saved['id'])
        assert current == original  # Includes stable IDs, definition, properties and revision; mode may be dirty locally.
    finally:
        context.close()
