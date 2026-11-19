"""Session edits retain each person's planned presence across a PostgreSQL save and reload."""
import re
from datetime import UTC, datetime

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, open_rota, open_session, save_dialog, sign_in  # noqa: F401
from .test_participation_segments import at

pytestmark = [pytest.mark.browser, pytest.mark.postgres,
              pytest.mark.parametrize('h', ['postgresql'], indirect=True)]


@pytest.mark.parametrize('edit,new_start', [
    pytest.param('flag', '08:00', id='unrelated-edit'),
    pytest.param('extend', '07:30', id='time-change-keeps-segments'),
    pytest.param('cut', '09:15', id='time-change-warns-and-cuts'),
])
def test_edit_session_keeps_planned_presence(browser, live_server, h, edit, new_start):
    affected, other = h.seed['people'][0], h.seed['people'][2]
    names = {p['id']: p['display_name'] for p in h.get('coordinator', '/api/catalogues')['people']}
    w = h.workspace()
    bounds = {'start': at('08:00'), 'end': at('12:00')}
    h.command('coordinator', 'shift.create', {
        'workspace_id': w['id'], 'assignment_type_id': h.seed['type'],
        'location_id': h.seed['locations'][0], 'service_ids': [h.seed['services'][0]], **bounds,
        'participants': [
            {'person_id': affected, **bounds, 'segments': [
                {'start': at('08:00'), 'end': at('09:00')},
                {'start': at('09:30'), 'end': at('10:30')},
                {'start': at('11:00'), 'end': at('12:00')},
            ]},
            {'person_id': other, **bounds, 'segments': [] if edit == 'cut' else [
                {'start': at('08:00'), 'end': at('09:45')},
                {'start': at('10:15'), 'end': at('12:00')},
            ]},
        ],
    }, revision=w['revision'])

    before, = h.detail(w)['shifts']
    attendance_fields = ('start', 'end', 'segments')

    def attendance(shift):
        return {p['person_id']: {key: p[key] for key in attendance_fields} for p in shift['participants']}

    expected = attendance(before)
    assert set(expected) == {affected, other}
    assert len(expected[affected]['segments']) == 3
    assert len(expected[other]['segments']) == (0 if edit == 'cut' else 2)

    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        def editor():
            open_rota(page, w['id'])
            open_session(page)
            page.get_by_role('button', name='Edit session', exact=True).click()
            return page.get_by_role('region', name='Edit session and participants', exact=True)

        form = editor()
        notice = form.get_by_text(re.compile(r'^The new times cut the partial attendance of '))
        flag_name = f"Keep assigned \u2014 {names[affected]}"
        if edit == 'flag':
            flag = form.get_by_role('checkbox', name=flag_name, exact=True)
            expect(flag).not_to_be_checked()
            flag.check()
        else:
            form.get_by_role('spinbutton', name='Session start time', exact=True).fill(new_start)

        if edit == 'cut':
            expect(notice).to_be_visible()
            expect(notice).to_contain_text(names[affected])
            expect(notice).not_to_contain_text(names[other])
            assert attendance(h.detail(w)['shifts'][0]) == expected
            expected[affected]['segments'] = expected[affected]['segments'][1:]
        else:
            expect(notice).to_be_hidden()

        saved_start = datetime.fromisoformat(at(new_start)).astimezone(UTC).isoformat()
        if edit != 'flag':
            for participant in expected.values():
                participant['start'] = saved_start
                if participant['segments']:
                    participant['segments'][0]['start'] = saved_start

        save_dialog(form, 'Save draft session')
        page.reload()
        reopened = editor()
        expect(reopened.get_by_role('spinbutton', name='Session start time', exact=True)).to_have_value(new_start)
        if edit == 'flag':
            expect(reopened.get_by_role('checkbox', name=flag_name, exact=True)).to_be_checked()
        saved, = h.detail(w)['shifts']
        assert saved['id'] == before['id']
        assert (saved['start'], saved['end']) == (saved_start, before['end'])
        assert attendance(saved) == expected
    finally:
        context.close()
