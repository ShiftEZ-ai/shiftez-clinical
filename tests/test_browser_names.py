"""Short codes (audit C-23): an "Al" before the family name is ignored whether it is joined, hyphenated or spaced, and
clashes resolve from the same family word the code was made from, so a department of look-alike names still gets one
code each. Names & codes counts what is still shared. Admin, 1440 light and 390 dark."""
import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate  # noqa: F401

pytestmark = pytest.mark.browser

CODES = """async () => { const {autoCode, uniqueCodes, codeRules} = await window.importBundle('dom.js');
  const names = ['Iris Al Parker', 'Iris AlParker', 'Iris Al-Parker', 'Iris Al Cooper', 'Iris Al Cooper',
    'Dr. Keiko Al Cooper', 'Felix Al Wren', 'Allen Brown', 'Ivo Allen', 'Madonna', 'Al Cooper'];
  const auto = names.map(autoCode);
  codeRules.ignoreAl = false; const kept = autoCode('Iris Al Parker'); codeRules.ignoreAl = true;
  // 40 people of one name: each still gets its own code.
  const crowd = uniqueCodes(Array.from({length: 40}, (_, i) => ({id: 'p' + i, display_name: 'Iris Al Cooper'})));
  const twins = uniqueCodes([{id: 'a', display_name: 'Iris Al Parker'}, {id: 'b', display_name: 'Iris AlParker'}]);
  return {auto, kept, crowd: [...crowd.values()], twins: [twins.get('a'), twins.get('b')]}; }"""


def test_a_spaced_al_is_ignored_and_clashes_use_the_same_family_word(browser, live_server):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page, 'configuration/names')
        got = page.evaluate(CODES)
        assert got['auto'] == ['IPA', 'IPA', 'IPA', 'ICO', 'ICO', 'KCO', 'FWR', 'ABR', 'IAL', 'MMA', 'ACO']
        assert got['kept'] == 'IAP'
        assert len(set(got['crowd'])) == 40 and all(len(c) == 3 for c in got['crowd'])
        assert got['twins'][0] != got['twins'][1] and set(got['twins']) == {'IPR', 'IPA'}
    finally:
        context.close()


@pytest.mark.parametrize('view,scheme', [({'width': 1440, 'height': 900}, 'light'), ({'width': 390, 'height': 844}, 'dark')])
def test_names_page_counts_only_codes_still_shared(browser, live_server, h, view, scheme):
    for name in ('Fictional Iris Al Cooper', 'Fictional Iris Cooper', 'Fictional Iris Al-Cooper'):
        h.configure('people', {'display_name': name})
    context, page = sign_in(browser, live_server, 'admin', viewport=view)
    page.emulate_media(color_scheme=scheme)
    try:
        navigate(page, 'configuration/names')
        page.get_by_label('Find people', exact=True).fill('Fictional Iris')
        rows = page.locator('#main .dt-body tbody tr, #main .dt-body .dt-card')
        expect(rows).to_have_count(3)
        codes = [rows.nth(i).locator('.code-chip').inner_text() for i in range(3)]
        assert len(set(codes)) == 3 and all(c.startswith('FI') for c in codes)
        expect(rows.filter(has_text='Needs a look')).to_have_count(0)
    finally:
        context.close()
