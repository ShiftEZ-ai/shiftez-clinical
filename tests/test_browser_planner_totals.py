"""People › Totals: View settings keeps them as Properties are (add, hide, reorder, saved with the view); a total that a
person quota in the rota's check measures over the view's days shows n / target with its bar and, over it, the warning
mark with "Over target" in the key; the CSV ends with the same totals. The quota rows are the server's report, laid over
the workspace read so the window matches the view's days exactly."""
import json
import re
from datetime import datetime, timedelta

from playwright.sync_api import expect

from .test_browser import (browser, live_server, sign_in, navigate, planner_tool, toolbar, route_fetch,  # noqa: F401
                           close_popover, view_settings)
from .test_browser_qa_planner import checked_rota, settled


def quota(pid, days, **over):
    return {'policy_id': 'q-' + over['metric'], 'policy_revision': 1, 'policy_name': 'Synthetic quota', 'person_id': pid,
            'display_name': '', 'starts_on': days[0], 'ends_on': days[1], 'comparison': 'maximum', 'difference': 0,
            'severity': 'advisory', 'overtime': 'include', 'assignment_type_ids': [], **over}


def test_people_totals_follow_view_settings_and_show_quota_progress(browser, live_server, h):
    w, _ = checked_rota(h)
    a, b, _ = h.seed['people']
    window = []

    def report(route):
        reply = route_fetch(route)
        try:
            body = reply.json() if route.request.method == 'GET' and reply.ok else None
        except ValueError:
            body = None
        check = body.get('workspace', {}).get('validation') if isinstance(body, dict) and isinstance(body.get('workspace'), dict) else None
        if not check or not window:
            route.fulfill(response=reply)
            return
        check['quota_report'] = {'metric_version': '1', 'timezone': 'UTC', 'basis': 'rota', 'complete': True, 'rows': [
            quota(a, window, metric='scheduled_hours', amount=2, actual=4, status='above_maximum'),
            quota(a, window, metric='duties', comparison='target', amount=3, actual=1, status='below_target'),
            # Another window: not these days, so not shown against them.
            quota(b, [window[0], '2099-01-01'], metric='scheduled_hours', amount=1, actual=4, status='above_maximum')]}
        route.fulfill(response=reply, body=json.dumps(body))

    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/people/2026-10-04?edit={w['id']}"); settled(page)
        grid = page.locator('#schedule-view .people-grid')
        labels = grid.locator(':scope>.pg-head>.pg-day').evaluate_all('n=>n.map(x=>x.getAttribute("aria-label"))')
        first, last = (datetime.strptime(x, '%d %b %Y').date() for x in (labels[0], labels[-1]))
        window[:] = [first.isoformat(), (last + timedelta(days=1)).isoformat()]
        page.route(re.compile(rf"/api/workspaces/{w['id']}"), report)
        page.reload(); settled(page)

        hours = grid.locator(f'[data-row-person="{a}"] [data-total="hours"]')
        expect(hours).to_have_text('4 / 2 h')
        expect(hours).to_have_class(re.compile(r'\bover\b'))
        expect(hours).to_have_attribute('aria-label', 'Hours: 4 h of 2 · over target')
        expect(hours.locator('svg')).to_have_count(1)
        expect(hours.locator('.pg-bar.over')).to_have_count(1)
        expect(page.locator('#schedule-view .view-legend [data-lg-k="over"]')).to_contain_text('Over target')
        # No quota over these days: the number alone, no bar.
        other = grid.locator(f'[data-row-person="{b}"] [data-total="hours"]')
        expect(other).to_have_text('4 h')
        expect(other.locator('.pg-bar')).to_have_count(0)

        panel = view_settings(page.locator('.planner-toolbar'), 'Totals')
        panel.get_by_label('Add total', exact=True).select_option(label='Duties')
        panel.get_by_role('button', name='Hide Hours', exact=True).click()
        expect(panel.get_by_role('button', name='Show Hours', exact=True)).to_be_focused()
        panel.get_by_role('button', name='Move Duties', exact=True).press('Alt+ArrowUp')
        panel.get_by_role('button', name='Show Hours', exact=True).click()
        close_popover(page)
        expect(grid.locator(':scope>.pg-head>[role=columnheader]:not(.pg-name):not(.pg-day)')).to_have_text(['Duties', 'Hours'])
        duties = grid.locator(f'[data-row-person="{a}"] [data-total="duties"]')
        expect(duties).to_have_text('1 / 3')
        expect(duties).not_to_have_class(re.compile(r'\bover\b'))
        expect(duties.locator('.pg-bar:not(.over)')).to_have_count(1)
        expect(grid.locator(f'[data-row-person="{b}"] [data-total="duties"]')).to_have_text('1')

        planner_tool(page, 'Export…')
        dialog = page.get_by_role('dialog', name='Print or export', exact=True)
        dialog.get_by_role('radiogroup', name='Format').get_by_role('radio', name='Data (CSV)', exact=True).click()
        csv = dialog.locator('pre.export-csv')
        expect(csv).to_contain_text('Person,')
        head = csv.evaluate('n=>n.textContent.split("\\n")[0]')
        assert head.endswith(',Duties,Hours'), head
        dialog.get_by_role('button', name='Cancel', exact=True).click()
        expect(dialog).to_have_count(0)

        toolbar(page.locator('.planner-toolbar'), 'Saved views').get_by_role('menuitem', name='New view…', exact=True).click()
        form = page.get_by_role('dialog', name='New view', exact=True)
        form.get_by_label('View name', exact=True).fill('Synthetic totals')
        form.get_by_role('button', name='Save view', exact=True).click()
        expect(page.locator('.planner-toolbar button.dt-saved')).to_contain_text('Synthetic totals')
        saved = next(v for v in h.get('coordinator', '/api/table-views?table=planner')['views'] if v['name'] == 'Synthetic totals')
        assert saved['definition']['planner']['totals'] == [{'key': 'duties', 'on': True}, {'key': 'hours', 'on': True}]
    finally:
        context.close()
