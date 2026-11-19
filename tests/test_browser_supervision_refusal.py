"""A session edit the server refuses for supervision pairing says what is wrong and what to do (D65, DL-048), through the real server path."""
import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, open_session  # noqa: F401
from .test_manual_supervision_staffing import world, command_for, START, END

pytestmark = pytest.mark.browser
SAID = 'This session has no named, qualified supervisor. Assign or restore one first.'


def test_removing_the_named_supervisor_is_refused_with_the_next_step(browser, live_server, h):
    w, _, shift, trainee, supervisor, _ = world(h)
    paired = {'person_id': trainee, 'start': START, 'end': END, 'participation': 'trainee', 'supervisor_person_id': supervisor}
    reply = h.command('coordinator', 'shift.update', command_for(w, shift, paired)['payload'], revision=w['revision'])
    assert reply['validation']['valid'], reply['validation']['findings']
    w = reply['workspace']
    before = h.detail(w)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner/' + w['id'])
        open_session(page)
        page.get_by_role('button', name='Edit session', exact=True).click()
        form = page.locator('.dock-panel')
        form.locator(f'.nested-card:has(select[name="Participant"] option[value="{supervisor}"]:checked)').get_by_role(
            'button', name='Remove participant', exact=True).click()
        with page.expect_response(lambda r: r.url.endswith('/api/commands')
                and r.request.post_data_json.get('kind') == 'shift.update') as refused:
            form.get_by_role('button', name='Save draft session', exact=True).click()
        assert refused.value.status == 422, refused.value.text()
        error = refused.value.json()['error']
        assert error['code'] == 'EDIT_INVALID'
        assert 'SUPERVISION_PAIRING' in {f['code'] for f in error['details']['findings']}
        # DL-048: what is wrong and what to do, in place of the server's generic sentence.
        expect(form).to_contain_text(SAID)
        expect(form).not_to_contain_text(error['message'])
        assert h.detail(w) == before
    finally:
        context.close()
