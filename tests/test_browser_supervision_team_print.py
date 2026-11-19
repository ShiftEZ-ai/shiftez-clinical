"""Server print groups a personal supervision team on desktop and phone."""
from pathlib import Path

import pytest
from playwright.sync_api import expect

from .governance_support import identity
from .test_home_team_payload import supervision_type
from .test_browser import browser, live_server, sign_in  # noqa: F401

pytestmark = [pytest.mark.browser, pytest.mark.postgres]


@pytest.mark.parametrize('h', ['postgresql'], indirect=True)
@pytest.mark.parametrize('width', [1440, 390])
def test_personal_team_print_preserves_names_ranks_and_times(browser, live_server, h, width):
    trainee, supervisor, unrelated = h.seed['people']
    identity(h, 'synthetic_print_supervisor', ['staff'], supervisor)
    start, end = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'
    w = h.shift(h.workspace(), typ=supervision_type(h), people=[
        {'person_id': trainee, 'start': start, 'end': end, 'participation': 'trainee', 'supervisor_person_id': supervisor},
        {'person_id': supervisor, 'start': start, 'end': end, 'participation': 'supervisor'},
        {'person_id': unrelated, 'start': start, 'end': end}])['workspace']
    pub = h.publish(w)['publication_id']
    issued = h.get('coordinator', f'/api/publications/{pub}')['snapshot']
    names = {pid: row['display_name'] for pid, row in issued['people'].items()}
    rank = next(p['rank'] for p in issued['shifts'][0]['participants'] if p['person_id'] == trainee)
    context, page = sign_in(browser, live_server, 'synthetic_print_supervisor', viewport={'width': width, 'height': 900})
    try:
        page.goto(live_server + f'/api/reports/{pub}?format=print')
        expect(page.locator('tbody tr')).to_have_count(1)
        expect(page.get_by_text(f'Supervisor: {names[supervisor]} — {rank} {names[trainee]}', exact=True)).to_be_visible()
        expect(page.locator('tbody')).to_contain_text('04 Oct 08:00–04 Oct 12:00')
        expect(page.locator('body')).not_to_contain_text(names[unrelated])
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        page.emulate_media(media='print')
        expect(page.get_by_role('navigation')).to_be_hidden()
        folder = Path('.run/team-box-be')
        folder.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(folder / f'personal-team-print-{width}.png'), full_page=True)
    finally:
        context.close()
# Include this file in hosted supervision and team-lead qualification.
