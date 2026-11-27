"""Configuration › Tags: create a tag, add members, see what saving changes, chips with their members, and the tag
offered wherever a rule picks clinics."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, open_config, condition, pick  # noqa: F401

pytestmark = pytest.mark.browser


def services(h):
    return {s['name']: s['id'] for s in h.get('admin', '/api/catalogues')['services']}


def tag_row(page, name):
    return page.locator('.config-pane tr, .config-pane .dt-card').filter(has=page.locator('.tag-ref', has_text='#' + name))


@pytest.mark.parametrize('viewport', [None, {'width': 390, 'height': 844}], ids=['desktop', 'phone'])
def test_tags_page_creates_a_tag_adds_members_and_previews_the_change(browser, live_server, h, viewport):
    clinics = services(h)
    context, page = sign_in(browser, live_server, 'admin', viewport)
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    try:
        open_config(page, 'Tags')
        pane = page.locator('.config-pane')
        expect(pane.get_by_text('No tags yet', exact=True)).to_be_visible()
        expect(pane.get_by_role('button', name='New tag', exact=True)).to_have_count(1)  # the header's, not a second in the empty state
        pane.get_by_role('button', name='New tag', exact=True).click()
        dialog = page.get_by_role('region', name='New tag')  # the docked side panel, beside the list
        dialog.get_by_label('Name', exact=True).fill('VIP_Clinics')
        dialog.get_by_label('Description', exact=True).fill('Clinics with priority consultant cover')
        pick(dialog, 'Can label', ['People'], False)
        pick(dialog, 'Can label', ['Clinics'])
        dialog.get_by_role('button', name=re.compile('Add clinics')).click()
        page.locator('.picker').get_by_role('option', name='Continuity clinic', exact=True).check()
        page.keyboard.press('Escape')
        expect(dialog.locator('.tag-member-list')).to_contain_text('Continuity clinic')
        expect(dialog.locator('.tag-effect')).to_contain_text('not used by any rule')  # a new tag changes nothing else
        dialog.get_by_role('button', name='Save tag', exact=True).click()
        expect(page.get_by_text('Tag #VIP_Clinics created.', exact=True)).to_be_visible()
        chip = tag_row(page, 'VIP_Clinics').locator('.tag-ref').first
        expect(chip).to_contain_text('1')  # the chip carries its member count
        tag = next(t for t in h.get('admin', '/api/catalogues')['tags'] if t['name'] == 'VIP_Clinics')
        assert tag['labels'] == ['services'] and tag['description'] == 'Clinics with priority consultant cover'
        members = [m for m in h.get('admin', '/api/catalogues')['tag_members'] if m['tag_id'] == tag['id']]
        assert [(m['kind'], m['member_id']) for m in members] == [('services', clinics['Continuity clinic'])]

        # A rule names the tag: adding a clinic previews which rule follows and how many rotas it touches.
        h.policy({'kind': 'min_rest', 'hours': 11}, {'conditions': {'match': 'all', 'conditions': [
            {'field': 'service_ids', 'op': 'any', 'values': [tag['id']]}]}})
        h.shift(h.workspace(), service=1)
        page.reload()
        open_config(page, 'Tags')
        chip = tag_row(page, 'VIP_Clinics').locator('.tag-ref').first
        chip.click()  # click: the members, and a way into the tag
        pop = page.locator('.popover')
        expect(pop).to_contain_text('Continuity clinic')
        expect(pop).to_contain_text('1 line in 1 rule')
        pop.get_by_role('button', name='Open tag', exact=True).click()
        dialog = page.get_by_role('region', name='#VIP_Clinics')
        dialog.get_by_role('button', name=re.compile('Add clinics')).click()
        page.locator('.picker').get_by_role('option', name='Review clinic', exact=True).check()
        page.keyboard.press('Escape')
        effect = dialog.locator('.tag-effect')
        expect(effect).to_contain_text('1 line in 1 rule follows the change')
        expect(effect).to_contain_text('1 session in 1 rota still being planned involves')
        dialog.get_by_role('button', name='Save tag', exact=True).click()
        expect(page.get_by_text('Tag saved.', exact=True)).to_be_visible()
        expect(tag_row(page, 'VIP_Clinics').locator('.tag-ref').first).to_contain_text('2')
        # Undo puts the members back as they were.
        page.get_by_role('button', name='Undo', exact=True).click()
        expect(tag_row(page, 'VIP_Clinics').locator('.tag-ref').first).to_contain_text('1')
        assert not errors
    finally:
        context.close()


def test_tag_chips_show_on_lists_and_rules_can_pick_a_tag(browser, live_server, h):
    clinics = services(h)
    tag = h.configure('tags', {'name': 'Weekend_cover', 'labels': ['services', 'people']})['record']
    h.command('admin', 'tag.members', {'add': [{'kind': 'services', 'id': clinics['Review clinic']}], 'reason': 'Synthetic'}, tag['id'], tag['revision'])
    context, page = sign_in(browser, live_server, 'admin')
    try:
        open_config(page, 'Services')
        row = page.locator('.config-pane tr').filter(has_text='Review clinic')
        chip = row.locator('.tag-ref', has_text='#Weekend_cover')
        expect(chip).to_be_visible()
        chip.hover()
        expect(page.locator('.hovercard')).to_contain_text('Review clinic')  # hovering lists its members
        navigate(page, 'rules')
        page.get_by_role('button', name='Add rule', exact=True).click()
        form = page.get_by_role('dialog')
        form.get_by_role('radio', name='Minimum rest', exact=True).check()
        condition(form, 'Clinic', ['#Weekend_cover'])
        expect(form.locator('.scope-summary')).to_contain_text('#Weekend_cover')
    finally:
        context.close()


def test_tags_page_dark_on_a_phone(browser, live_server, h):
    clinics = services(h)
    tag = h.configure('tags', {'name': 'VIP_Clinics', 'labels': ['services'], 'colour': 'violet'})['record']
    h.command('admin', 'tag.members', {'add': [{'kind': 'services', 'id': clinics['Review clinic']}], 'reason': 'Synthetic'}, tag['id'], tag['revision'])
    context, page = sign_in(browser, live_server, 'admin', {'width': 390, 'height': 844})
    page.emulate_media(color_scheme='dark')
    try:
        open_config(page, 'Tags')
        chip = tag_row(page, 'VIP_Clinics').locator('.tag-ref').first
        expect(chip).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1')  # no sideways scroll
        # The chip keeps its own hue in dark mode, readable on the dark surface.
        colours = chip.evaluate("c => [getComputedStyle(c).color, getComputedStyle(c).backgroundColor, getComputedStyle(document.body).backgroundColor]")
        assert colours[0] != colours[1] and colours[1] != colours[2]
        chip.click()
        page.locator('.popover').get_by_role('button', name='Open tag', exact=True).click()
        dialog = page.get_by_role('region', name='#VIP_Clinics')
        expect(dialog.get_by_label('Name', exact=True)).to_have_value('VIP_Clinics')
        expect(dialog.locator('.tag-member-list')).to_contain_text('Review clinic')
        assert dialog.evaluate('d => d.scrollWidth <= d.clientWidth + 1')
    finally:
        context.close()


def test_tags_are_kept_in_one_place_and_offered_when_editing_a_person(browser, live_server, h):
    """Tags live in one place, Configuration › Tags (Names & codes shows none), archived tags are hidden there by
    default, and a person's form suggests the department's tags for people."""
    h.configure('tags', {'name': 'Part_time', 'labels': ['people']})
    h.configure('tags', {'name': 'VIP_Clinics', 'labels': ['services']})  # labels clinics: never suggested for a person
    old = h.configure('tags', {'name': 'QA_old', 'labels': ['people']})['record']
    h.configure('tags', {'name': 'QA_old', 'labels': ['people'], 'active': False}, target=old['id'], revision=old['revision'])
    context, page = sign_in(browser, live_server, 'admin')
    try:
        open_config(page, 'Names & codes')
        pane = page.locator('.config-pane')
        expect(pane.get_by_text('Tags', exact=True)).to_have_count(0)
        expect(pane.locator('.tag-chip, .names-tags')).to_have_count(0)
        nav = page.get_by_role('navigation', name='Configuration sections')
        expect(nav.get_by_role('button', name='Tags', exact=True)).to_contain_text('2')  # the archived one is not counted
        open_config(page, 'Tags')
        expect(tag_row(page, 'Part_time')).to_have_count(1)
        expect(tag_row(page, 'QA_old')).to_have_count(0)  # archived: hidden by default
        footer = page.locator('.app-footer .page-status')
        expect(footer).to_contain_text('2 of 3 tags')  # E13: the list's count once, in the footer
        expect(page.locator('#main .dt-count')).to_have_count(0)
        # E12: a short page is no taller than the window (the body's polite live region used to add 1px).
        assert page.evaluate('document.documentElement.scrollHeight - innerHeight') <= 0
        # A short table shows no scrollbar of its own (its hidden status sits at the top of the positioned frame).
        frame = page.locator('#main .table-scroll').first
        assert frame.evaluate('s => s.scrollHeight <= s.clientHeight && s.scrollWidth <= s.clientWidth')
        navigate(page, 'people')
        page.locator('#main tr').filter(has_text='Staff 01(test)').get_by_role('button', name=re.compile('^Open')).click()
        # P-02: the row button is Open (the side panel); its Edit opens the person form with the tags.
        page.get_by_role('button', name=re.compile('^Edit Staff 01\\(test\\)')).click()
        # One tags editor (P-05): the shared creatable picker offers the department's tags for people (never a clinic tag).
        page.get_by_role('dialog').get_by_role('button', name=re.compile('^Tags: ')).click()
        expect(page.get_by_role('option', name='Part_time', exact=True)).to_be_visible()
        expect(page.get_by_role('option', name='VIP_Clinics', exact=True)).to_have_count(0)
        expect(page.get_by_role('option', name='QA_old', exact=True)).to_have_count(0)
    finally:
        context.close()


def test_a_coordinator_makes_a_clinic_tag_in_configuration(browser, live_server, h):
    """Coordinators write #tag rule lines, so Configuration › Tags is theirs too (beside the Setup guide)."""
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_config(page, 'Tags')
        page.locator('.config-pane').get_by_role('button', name='New tag', exact=True).click()  # in the empty state while there are none (C-03)
        dialog = page.get_by_role('region', name='New tag')
        dialog.get_by_label('Name', exact=True).fill('VIP_Clinics')
        pick(dialog, 'Can label', ['People'], False)
        pick(dialog, 'Can label', ['Clinics'])
        dialog.get_by_role('button', name=re.compile('Add clinics')).click()
        page.locator('.picker').get_by_role('option', name='Review clinic', exact=True).check()
        page.keyboard.press('Escape')
        dialog.get_by_role('button', name='Save tag', exact=True).click()
        expect(page.get_by_text('Tag #VIP_Clinics created.', exact=True)).to_be_visible()
        expect(tag_row(page, 'VIP_Clinics')).to_have_count(1)
    finally:
        context.close()


@pytest.mark.parametrize('role', ['approver', 'auditor'])
def test_read_only_roles_see_tags_but_cannot_change_them(browser, live_server, h, role):
    person = h.seed['people'][0]
    tag = h.configure('tags', {'name': 'Night', 'labels': ['people']})['record']
    tag = h.command('admin', 'tag.members', {'add': [{'kind': 'people', 'id': person}], 'reason': 'Synthetic'}, tag['id'], tag['revision'])['record']
    h.configure('tags', {'name': 'Late', 'labels': ['people']}, role=role, status=403)
    h.command(role, 'tag.members', {'remove': [{'kind': 'people', 'id': person}], 'reason': 'Synthetic'}, tag['id'], tag['revision'], status=403)
    context, page = sign_in(browser, live_server, role)
    try:
        navigate(page, 'people')
        chip = page.locator('.tag-ref', has_text='#Night').first
        expect(chip).to_be_visible()
        chip.click()
        page.locator('.popover').get_by_role('button', name='Open tag', exact=True).click()
        dialog = page.get_by_role('region', name='#Night')
        expect(dialog.get_by_label('Name', exact=True)).to_be_disabled()
        expect(dialog.get_by_role('button', name='Save tag')).to_have_count(0)
        expect(dialog.get_by_role('button', name=re.compile('^Remove '))).to_have_count(0)
        expect(dialog.get_by_role('button', name=re.compile('Add people'))).to_have_count(0)
        expect(dialog.locator('.tag-member-list')).to_have_count(1)  # the members are shown
    finally:
        context.close()


def test_announcement_audience_and_fairness_filters_offer_tags(browser, live_server, h):
    first = h.seed['people'][0]
    tag = h.configure('tags', {'name': 'Night_team', 'labels': ['people']})['record']
    h.command('admin', 'tag.members', {'add': [{'kind': 'people', 'id': first}], 'reason': 'Synthetic'}, tag['id'], tag['revision'])
    h.publish(h.shift(h.workspace('Tag fairness source'))['workspace'])  # with nothing published Fairness is one empty card (f10)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'inbox')
        page.get_by_role('button', name='New announcement', exact=True).click()
        form = page.get_by_role('dialog')
        form.get_by_label('For', exact=True).select_option('some')
        condition(form, 'Tag', ['#Night_team'])
        expect(form.locator('[data-review-label=Conditions]')).to_have_attribute('data-review-value', re.compile('#Night_team'))
        page.keyboard.press('Escape')
        form.get_by_role('button', name='Discard changes').click()
        navigate(page, 'insights/fairness')
        page.get_by_role('button', name='Filter', exact=True).click()
        pop = page.get_by_role('dialog', name='Filter')
        condition(pop, 'Person', ['#Night_team'])
        expect(page.locator('.fx-scope')).to_contain_text('#Night_team')  # live filter; it reads the tag by name
        page.keyboard.press('Escape')
        expect(page.get_by_role('button', name='Filter (1 active)', exact=True)).to_be_visible()
    finally:
        context.close()


def test_tag_form_opens_for_every_kind_and_labels_announcements_and_events(browser, live_server, h):
    """Every kind the contract allows has words in the tag form: an existing tag opens, and a new one can label
    announcements and events."""
    h.configure('tags', {'name': 'Existing', 'labels': ['people']})
    notice = h.configure('department_content', {'name': 'Fire drill', 'starts_on': '2026-01-01', 'notify': False})['record']
    context, page = sign_in(browser, live_server, 'admin')
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    try:
        open_config(page, 'Tags')
        tag_row(page, 'Existing').locator('.tag-ref').first.click()
        page.locator('.popover').get_by_role('button', name='Open tag', exact=True).click()
        dialog = page.get_by_role('region', name='#Existing')
        dialog.locator('.pick-field').filter(has_text='Can label').locator('.pick-button').click()
        expect(page.locator('.picker').get_by_role('option', name='Events', exact=True)).to_be_visible()
        expect(page.locator('.picker').get_by_role('option', name='Announcements', exact=True)).to_be_visible()
        page.keyboard.press('Escape')
        dialog.get_by_role('button', name='Cancel', exact=True).click()
        page.locator('.config-pane').get_by_role('button', name='New tag', exact=True).click()
        dialog = page.get_by_role('region', name='New tag')
        dialog.get_by_label('Name', exact=True).fill('Drills')
        pick(dialog, 'Can label', ['People'], False)
        pick(dialog, 'Can label', ['Announcements', 'Events'])
        expect(dialog.locator('.tag-member-kind')).to_have_count(2)
        dialog.get_by_role('button', name=re.compile('Add announcements')).click()
        page.locator('.picker').get_by_role('option', name='Fire drill', exact=True).check()
        page.keyboard.press('Escape')
        dialog.get_by_role('button', name='Save tag', exact=True).click()
        expect(page.get_by_text('Tag #Drills created.', exact=True)).to_be_visible()
        tag = next(t for t in h.get('admin', '/api/catalogues')['tags'] if t['name'] == 'Drills')
        assert sorted(tag['labels']) == ['department_content', 'events']
        members = [m for m in h.get('admin', '/api/catalogues')['tag_members'] if m['tag_id'] == tag['id']]
        assert [(m['kind'], m['member_id']) for m in members] == [('department_content', notice['id'])]
        assert not errors
    finally:
        context.close()
