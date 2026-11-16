"""G10: the real Audit log Export CSV button exports the entire permitted filtered history."""
import csv
import io
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import expect

from apps.api import models as m
from apps.api.db import transaction
from .test_browser import browser, live_server, sign_in, navigate, keyboard_focus  # noqa: F401
from .test_browser_collection_toolbars import open_filter

pytestmark = pytest.mark.browser


def filtered_history(h):
    """105 matching rows plus independent actor, action, inclusive-date and department exclusions."""
    actor = h.seed['identities']['coordinator']
    zone = ZoneInfo(h.get('admin', '/api/me')['organisation']['timezone'])
    start = datetime(2026, 10, 4, tzinfo=zone).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    refs = {f'g10-permitted-{n:03}' for n in range(105)}
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, actor).display_name = 'Fictional Departmental Rota Coordinator With A Long Display Name'
        foreign = m.Department(organisation_id=h.seed['organisation_id'], name='Fictional other audit department')
        db.add(foreign)
        db.flush()

        def add(reference, at, actor_id=actor, action='workspace.submit', department=None, reason=None):
            db.add(m.AuditEvent(department_id=department or h.seed['department_id'], actor_id=actor_id,
                               action=action, target_id=str(uuid.uuid4()), created_at=at,
                               reason=reason or 'Fictional audit row ' + reference, detail={}, correlation_id=reference))

        for n in range(105):
            # Exact included start and final second make the department-local date contract observable.
            at = start if n == 0 else end - timedelta(seconds=1) if n == 104 else start + timedelta(minutes=n)
            add(f'g10-permitted-{n:03}', at, reason='=G10, "quoted" audit reason' if n == 0 else None)
        add('g10-excluded-before', start - timedelta(seconds=1))
        add('g10-excluded-after', end)
        add('g10-excluded-actor', start, actor_id=h.seed['identities']['admin'])
        add('g10-excluded-action', start, action='policy.activate')
        add('g10-excluded-department', start, department=foreign.id)
    return actor, zone, refs


@pytest.mark.parametrize('role,width,scheme', [('operator', 1440, 'light'), ('auditor', 390, 'dark')])
def test_audit_export_csv_matches_actor_action_dates_and_permitted_history(browser, live_server, h, role, width, scheme):
    actor, zone, refs = filtered_history(h)
    actions = ['workspace', 'shift', 'publication', 'scheduling', 'job', 'generation', 'series']
    params = {'from': '2026-10-04', 'to': '2026-10-04', 'actor_id': actor, 'action': actions}
    query = urlencode(params, doseq=True)
    expected = [event for offset in (0, 100)
                for event in h.get(role, '/api/audit?' + query + '&offset=' + str(offset))['events']]
    assert {event['correlation_id'] for event in expected} == refs
    assert len(expected) == 105 and all(event['actor_id'] == actor for event in expected)
    context, page = sign_in(browser, live_server, role, viewport={'width': width, 'height': 900})
    page.emulate_media(color_scheme=scheme)
    try:
        navigate(page, 'system/audit')
        pop = open_filter(page.locator('#main'))
        pop.get_by_label('Kind of change', exact=True).select_option('rotas')
        pop.get_by_label('Person', exact=True).select_option(actor)
        pop.get_by_label('From date', exact=True).fill('2026-10-04')
        pop.get_by_label('From date', exact=True).blur()
        with page.expect_response(lambda response: urlparse(response.url).path == '/api/audit'
                                  and parse_qs(urlparse(response.url).query).get('to') == ['2026-10-04']
                                  and parse_qs(urlparse(response.url).query).get('actor_id') == [actor]):
            pop.get_by_label('To date', exact=True).fill('2026-10-04')
            pop.get_by_label('To date', exact=True).blur()
        page.keyboard.press('Escape')
        expect(page.get_by_role('button', name='Filter (4 active)', exact=True)).to_be_visible()
        expect(page.locator('.app-footer .footer-status')).to_have_text('1–100 changes')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        # The tooltip describes the export without changing its accessible name.
        button = page.get_by_role('group', name='Audit log', exact=True).get_by_role('button', name=re.compile('^Export CSV'))
        keyboard_focus(button)
        expect(button).to_be_focused()
        expect(button).to_have_accessible_name('Export CSV')
        with page.expect_download() as pending:
            button.press('Enter')
        downloaded = pending.value
        assert downloaded.failure() is None
        assert re.fullmatch(r'decision-history-.+-2026-10-04-to-2026-10-04\.csv', downloaded.suggested_filename)
        text = Path(downloaded.path()).read_text(encoding='utf-8-sig')
        parsed = list(csv.DictReader(io.StringIO(text)))
        assert len(parsed) == 105  # the export includes the five matching records beyond the visible page
        assert list(parsed[0]) == ['time', 'person', 'action', 'what_changed', 'reason', 'self_approved', 'reference']
        wanted = []
        for event in expected:
            at = datetime.fromisoformat(event['created_at'].replace('Z', '+00:00'))
            at = (at if at.tzinfo else at.replace(tzinfo=timezone.utc)).astimezone(zone)
            reason = event['reason']
            wanted.append({'time': at.strftime('%Y-%m-%d %H:%M:%S'), 'person': event['actor_display_name'],
                           # SW3-D-22 (BE-23): the CSV says what changed in the on-screen sentence, not a raw id.
                           'action': event['action'], 'what_changed': event['summary'],
                           'reason': "'" + reason if reason.startswith('=') else reason,
                           'self_approved': 'no', 'reference': event['correlation_id']})
        assert parsed == wanted
        assert not any('g10-excluded-' in value for row in parsed for value in row.values())
        expect(button).to_be_focused()
    finally:
        context.close()
