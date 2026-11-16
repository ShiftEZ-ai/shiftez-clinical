"""Auto-schedule outcomes that are not failures (audit SR-25, SR-26): a run that ran out of time before any proposal
continues from its saved preparation with twice the time, or, at the department maximum, runs again; places nobody
can fill do not stop the run or start a second one: the one run fills everything else and keeps its reasons.
Operator screens are desktop-only: 1440 light and dark."""
import re
import pytest
from playwright.sync_api import expect
from apps.worker.main import process_one
from packages.contracts.schemas import AUTO_SCHEDULE_LIMITS
from .test_browser import minutes_words, wait_run_created, browser, live_server, open_rota, open_scheduling  # noqa: F401
from .test_browser_cp_sat import cp_server, _finish  # noqa: F401
from .test_browser_autoschedule_budget import confirm
from .test_browser_autoschedule_review import _open, _run, DESKTOP
from .test_staff_wishes import department_settings

pytestmark = pytest.mark.browser
SCHEMES = ['light', 'dark']


def _time_limited(snapshot, **options):
    """The search ran out of time before any proposal (a real stored unknown_time_limit result, not a browser edit)."""
    return {'status': 'unknown_time_limit', 'algorithm_version': snapshot['compiler_version'],
            'input_fingerprint': snapshot['fingerprint'],
            'phase_timings': {'preparation_seconds': 1, 'search_seconds': 300, 'validation_seconds': 0, 'summary_seconds': 0}}


def _out_of_time(browser, server, h, name, scheme):
    w = h.shift(h.workspace(name), people=[])['workspace']
    context, page, panel = _open(browser, server, h, w, DESKTOP, scheme)
    panel.get_by_role('button', name='Run auto-schedule', exact=True).click()
    expect(panel.get_by_role('progressbar')).to_be_visible()
    wait_run_created(page)
    assert process_one(h.app.state.engine, scheduler=_time_limited)
    expect(panel.get_by_text('Ran out of time · no proposal yet', exact=True)).to_be_visible(timeout=15000)
    expect(panel.locator('.auto-outcome.err')).to_have_count(0)  # not a failure
    return context, page, panel, h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs']


def _sent(page, kind):
    return page.expect_request(lambda r: r.url.endswith('/api/commands') and (r.post_data_json or {}).get('kind') == kind)


@pytest.mark.parametrize('scheme', SCHEMES)
def test_unknown_time_limit_continues_with_twice_the_time(browser, cp_server, h, scheme):
    context, page, panel, jobs = _out_of_time(browser, cp_server, h, 'FM out of time', scheme)
    try:
        first = jobs[0]
        doubled = int(first['effective_options']['search_minutes']) * 2
        assert doubled <= AUTO_SCHEDULE_LIMITS['default_max_minutes']
        # One way on: Continue. No second time field it would ignore, and no fresh Run again beside it.
        expect(panel.get_by_label('Search time', exact=True)).to_have_count(0)
        expect(panel.get_by_role('button', name=re.compile('^Run again'))).to_have_count(0)
        twice = panel.get_by_role('button', name=f'Continue with {doubled} minutes', exact=True)
        expect(twice).to_have_class(re.compile(r'\bprimary\b'))
        twice.click()
        with _sent(page, 'job.continue') as sent:
            continued = confirm(page, 'Continue auto-schedule', 'Continue', 'Twice the search time')
        # The total search time (not an increment), on the run that ran out of time: its saved preparation is reused.
        assert sent.value.post_data_json['target_id'] == first['id']
        assert sent.value.post_data_json['payload']['minutes'] == doubled
        assert continued['preparation_expired'] is False
        assert continued['job']['effective_options']['search_minutes'] == doubled
        assert [j['id'] for j in h.get('coordinator', '/api/jobs?workspace_id=' + first['workspace_id'])['jobs']] == [continued['job']['id'], first['id']]
    finally:
        context.close()


def test_unknown_time_limit_at_the_department_maximum_runs_again(browser, cp_server, h):
    """Already at the maximum, twice the time is not allowed: Continue is not offered; Run again starts a fresh run."""
    maximum = AUTO_SCHEDULE_LIMITS['default_minutes']
    h.command('admin', 'department.settings', {**department_settings(h), 'generation_defaults': {'max_search_minutes': maximum}},
              h.seed['department_id'], h.get('admin', '/api/me')['organisation']['revision'])
    context, page, panel, jobs = _out_of_time(browser, cp_server, h, 'FM out of time at the maximum', 'light')
    try:
        assert int(jobs[0]['effective_options']['search_minutes']) == maximum
        expect(panel.get_by_role('button', name=re.compile('^Continue with'))).to_have_count(0)
        field = panel.get_by_label('Search time', exact=True)
        expect(field).to_have_value(minutes_words(maximum))  # one unit, as people read it (L10)
        field.fill(str(maximum + 1))
        expect(field).to_have_js_property('validationMessage', f'Between 1 min and {minutes_words(maximum)}.')
        field.fill(minutes_words(maximum))
        again = panel.get_by_role('button', name=f'Run again for {minutes_words(maximum)}', exact=True)
        expect(again).to_have_class(re.compile(r'\bprimary\b'))
        # A fresh run, not a continuation. At the department default the panel sends no generation_options (the server
        # applies the department's), so what counts is the run the server accepted.
        with page.expect_response(lambda r: r.url.endswith('/api/commands') and r.request.method == 'POST'
                                  and (r.request.post_data_json or {}).get('kind') == 'job.create') as created:
            again.click()
        assert created.value.ok, created.value.text()
        job = created.value.json()['job']
        assert job['kind'] == 'generation' and job['id'] != jobs[0]['id']
        assert (job['effective_options']['search_minutes'], job['effective_options']['max_search_minutes']) == (maximum, maximum)
        wait_run_created(page)  # the panel shows the new run before the page closes
    finally:
        context.close()


@pytest.mark.parametrize('scheme', SCHEMES)
def test_one_run_fills_everything_else_without_a_second_run(browser, cp_server, h, scheme):
    """A place nobody can fill stops the run at the clash until the person chooses Fill everything else (2850a3ee6);
    that choice fills everything else (no further run goes on by itself) and the result says why the place stays open."""
    h.policy({'kind': 'restriction', 'weekdays': [6]})  # nobody may work Sundays
    w = h.shift(h.workspace('FM Sunday reasons'), people=[])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    context, page, panel = _open(browser, cp_server, h, w, DESKTOP, scheme)
    try:
        _run(page, h, panel)
        _finish(page, h)
        review = panel.get_by_role('region', name='Review')
        # The place nobody may work stays open, said once with its cause (A3: one Fix list row, the server's words).
        expect(review.get_by_role('status').first).to_contain_text('1 place stays open', timeout=15000)
        still = review.get_by_role('region', name='Still to fix').locator('li[data-cause]')
        expect(still).to_have_count(1)
        jobs = h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs']
        cause = next(c for c in h.get('coordinator', '/api/jobs/' + jobs[0]['id'])['job']['result']['fix_list_after']['causes'] if c['level'] == 'must')
        expect(still.first).to_contain_text(cause['sentence'])
        expect(panel.get_by_role('button', name='Apply 1 change', exact=True)).to_be_visible()
        expect(panel.get_by_text('fills everything else', exact=False)).to_have_count(0)
        expect(panel.get_by_role('progressbar')).to_have_count(0)
        jobs = h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs']
        assert len(jobs) == 1 and jobs[0]['diagnostic_proposal'] and jobs[0]['status'] == 'succeeded'
        assert h.get('coordinator', '/api/jobs/' + jobs[0]['id'])['job']['result']['status'] == 'candidate_with_gaps'
    finally:
        context.close()
