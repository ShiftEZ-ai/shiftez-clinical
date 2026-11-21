"""Decision history: server-side filters span all history, and the CSV export carries the same filters safely."""
from packages.domain.time import now as clock_now
import csv
import io
import json
import pytest
from uuid import uuid4
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo
from sqlalchemy import select
from apps.api import main
from apps.api import models as m
from apps.api.db import transaction


pytestmark = pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)


def events(h, query, role='auditor'):
    return h.get(role, '/api/audit?' + query)['events']


def export(h, query='', role='auditor', status=200):
    response = h.client(role).get('/api/audit.csv?' + query)
    assert response.status_code == status, response.text
    return response


@pytest.mark.parametrize('category', ['sick', None])
def test_absence_report_audit_uses_fixed_reason_and_optional_category(h, category):
    from .test_published_changes import issued
    from .governance_support import identity
    identity(h, 'audit_admin', ['admin', 'auditor'])
    pub, shifts = issued(h)
    payload = {'kind': 'absence_report', 'publication_id': pub,
        'assignment_id': shifts[0]['participants'][0]['id'], 'reason': 'Private health explanation',
        'note': 'Private note', **({'category': category} if category is not None else {})}
    reply = h.command('staff', 'request.create', payload)
    with transaction(h.app.state.engine) as db:
        stored = db.scalar(select(m.AuditEvent).where(m.AuditEvent.detail['receipt_id'].as_string() == reply['receipt_id']))
        assert stored.reason == 'Absence reported'
    for role in ('auditor', 'audit_admin', 'operator'):
        event = next(event for event in h.get(role, '/api/audit?people=false')['events']
                     if event['action'] == 'request.create' and event['detail'].get('receipt_id') == reply['receipt_id'])
        assert event['reason'] == ''
        assert event['detail'].get('absence_category') == category
        assert 'Private health explanation' not in str(event) and 'Private note' not in str(event)


def test_old_and_new_absence_audit_reasons_are_masked_on_every_read_path(h):
    identity_id = h.seed['identities']['coordinator']
    with transaction(h.app.state.engine, write=True) as db:
        db.add_all([
            m.AuditEvent(department_id=h.seed['department_id'], actor_id=identity_id, action='request.create',
                reason='Old hidden health reason', target_id=None, correlation_id=str(uuid4()),
                detail={'payload_fields': ['kind', 'assignment_id', 'publication_id']}),
            m.AuditEvent(department_id=h.seed['department_id'], actor_id=identity_id, action='request.create',
                reason='Absence reported', target_id=None, correlation_id=str(uuid4()),
                detail={'payload_fields': ['kind', 'assignment_id', 'publication_id'], 'absence_category': 'sick'}),
            m.AuditEvent(department_id=h.seed['department_id'], actor_id=identity_id, action='request.create',
                reason='Ordinary leave request', target_id=None, correlation_id=str(uuid4()),
                detail={'payload_fields': ['kind', 'start', 'end']}),
        ])
    for role in ('auditor', 'operator'):
        raw = h.get(role, '/api/audit?people=false')['events']
        hidden = [event for event in raw if event['action'] == 'request.create' and event['detail'].get('payload_fields') ==
                  ['kind', 'assignment_id', 'publication_id']]
        assert hidden and all(event['reason'] == '' for event in hidden)
        folded = h.get(role, '/api/audit?people=false&fold=true')['events']
        assert all(event['reason'] == '' for event in folded
                   if event['action'] == 'request.create' and event['detail'].get('payload_fields') ==
                   ['kind', 'assignment_id', 'publication_id'])
        csv_text = export(h, role=role).text
        assert 'Old hidden health reason' not in csv_text
        assert 'Absence reported' not in csv_text
        assert 'Ordinary leave request' in csv_text
        assert not h.get(role, '/api/audit?q=Old+hidden+health+reason&people=false')['events']


def test_absence_audit_search_and_collection_reason_use_the_masked_reason(h):
    actor_id = h.seed['identities']['coordinator']
    secret = 'Confidential absence secret'
    with transaction(h.app.state.engine, write=True) as db:
        db.add_all([
            m.AuditEvent(department_id=h.seed['department_id'], actor_id=actor_id, action='request.create',
                reason=secret, target_id=None, correlation_id=str(uuid4()),
                detail={'target_label': 'privacy-absence', 'payload_fields': ['kind', 'assignment_id', 'publication_id']}),
            m.AuditEvent(department_id=h.seed['department_id'], actor_id=actor_id, action='request.create',
                reason='Null payload reason', target_id=None, correlation_id=str(uuid4()),
                detail={'target_label': 'privacy-null', 'payload_fields': None}),
            m.AuditEvent(department_id=h.seed['department_id'], actor_id=actor_id, action='request.create',
                reason='AAA visible reason', target_id=None, correlation_id=str(uuid4()),
                detail={'target_label': 'privacy-visible', 'payload_fields': ['kind']}),
        ])
    name = next(person['display_name'] for person in h.get('auditor', '/api/audit')['people'] if person['id'] == actor_id)
    by_actor = h.get('auditor', '/api/audit?people=false&q=' + quote_plus(name))['events']
    absence = next(event for event in by_actor if event['detail'].get('target_label') == 'privacy-absence')
    assert absence['reason'] == ''
    by_null_reason = h.get('auditor', '/api/audit?people=false&q=' + quote_plus('Null payload reason'))['events']
    assert next(event for event in by_null_reason if event['detail'].get('target_label') == 'privacy-null')['reason'] == 'Null payload reason'

    contains_secret = quote_plus(json.dumps({'conditions': {'conditions': [
        {'field': 'reason', 'op': 'contains', 'value': secret}]}}))
    filtered = h.get('auditor', '/api/audit?people=false&filter=' + contains_secret)['events']
    assert not any(event['detail'].get('target_label') == 'privacy-absence' for event in filtered)

    sorted_rows = h.get('auditor', '/api/audit?people=false&filter=' + quote_plus(json.dumps({
        'sort': ['reason', 'asc']})))['events']
    privacy_rows = [event['detail'].get('target_label') for event in sorted_rows
                    if event['detail'].get('target_label', '').startswith('privacy-')]
    assert privacy_rows[:2] == ['privacy-absence', 'privacy-visible']


def test_withdrawn_absence_audit_uses_a_fixed_reason_and_is_masked(h):
    from .test_published_changes import issued
    pub, shifts = issued(h)
    request = h.command('staff', 'request.create', {'kind': 'absence_report', 'publication_id': pub,
        'assignment_id': shifts[0]['participants'][0]['id'], 'reason': 'Private withdrawal explanation'})['record']
    h.command('staff', 'request.decide', {'decision': 'withdraw', 'reason': 'Private withdrawal explanation'},
              target=request['id'], revision=request['revision'])
    with transaction(h.app.state.engine) as db:
        event = db.scalar(select(m.AuditEvent).where(m.AuditEvent.target_id == request['id'],
            m.AuditEvent.action == 'request.decide'))
        assert event.reason == 'Absence withdrawn'
    event = next(event for event in h.get('auditor', '/api/audit?people=false')['events']
                 if event['target_id'] == request['id'] and event['action'] == 'request.decide')
    assert event['reason'] == ''


def test_filters_by_action_group_person_text_and_dates(h):
    w = h.shift(h.workspace('Filter check'))['workspace']
    h.command('coordinator', 'workspace.submit', {'reason': '=Ready for the lead'}, w['id'], w['revision'])
    everything = events(h, 'offset=0')
    assert {'workspace.create', 'workspace.submit'} <= {e['action'] for e in everything}
    coordinator = h.seed['identities']['coordinator']
    assert any(p['id'] == coordinator for p in h.get('auditor', '/api/audit')['people'])
    only = events(h, 'action=workspace&action=availability_pattern')
    assert only and all(e['action'].startswith(('workspace.', 'availability_pattern.')) for e in only)
    assert all(e['actor_id'] == coordinator for e in events(h, f'actor_id={coordinator}'))
    assert [e['action'] for e in events(h, 'q=ready+for+THE')] == ['workspace.submit']
    assert events(h, 'q=100%25') == []  # wildcards are literal
    assert events(h, 'self_approved=true') == []
    today = clock_now().astimezone(ZoneInfo(h.get('auditor', '/api/me')['organisation']['timezone'])).date()
    assert len(events(h, f'from={today}&to={today}')) == len(everything)
    assert events(h, f'to={today - timedelta(days=1)}') == []
    assert events(h, f'from={today + timedelta(days=1)}') == []
    # Newest first, with a stable tie-break.
    stamps = [e['created_at'] for e in everything]
    assert stamps == sorted(stamps, reverse=True)


def test_target_filter_includes_rule_proposal_and_decline(h):
    policy = h.command('coordinator', 'rule.propose', {'catalogue_id': 'duties_per_week',
        'lines': [{'id': str(uuid4()), 'numbers': {'max': {'value': 5}}}], 'reason': 'Weekly duty limit'})['record']
    h.command('approver', 'policy.decline', {'reason': 'Keep the current rules'}, policy['id'], policy['revision'])

    rows = events(h, f"target_id={policy['id']}")
    assert {(row['action'], row['target_id'], row['actor_id']) for row in rows} == {
        ('rule.propose', policy['id'], h.seed['identities']['coordinator']),
        ('policy.decline', policy['id'], h.seed['identities']['approver']),
    }


def test_invalid_filters_are_refused(h):
    for query in ('from=2026-10-05&to=2026-10-01', 'from=1999-12-31', 'to=not-a-date', 'action=Workspace', 'action=a.b',
                  'self_approved=maybe', 'q=' + 'x' * 201, 'actor_id=' + 'x' * 37, 'offset=-1'):
        assert h.client('auditor').get('/api/audit?' + query).status_code == 422, query
        assert query == 'offset=-1' or h.client('auditor').get('/api/audit.csv?' + query).status_code == 422, query


def test_only_auditors_and_developers_read_or_export(h):
    for role in ('staff', 'coordinator', 'approver', 'admin'):
        export(h, role=role, status=403)
        h.get(role, '/api/audit', status=403)
    export(h, role='operator')


def test_csv_columns_escaping_filename_and_truncation(h, monkeypatch):
    w = h.shift(h.workspace('CSV check'))['workspace']
    h.command('coordinator', 'workspace.submit', {'reason': '=HYPERLINK("x")'}, w['id'], w['revision'])
    response = export(h, 'from=2026-01-01&to=2099-12-31&action=workspace')
    assert response.headers['content-type'].startswith('text/csv')
    disposition = response.headers['content-disposition']
    assert 'decision-history-' in disposition and '2026-01-01-to-2099-12-31.csv' in disposition
    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows[0] == ['time', 'person', 'action', 'what_changed', 'reason', 'self_approved', 'reference']
    submit = next(r for r in rows[1:] if r[2] == 'workspace.submit')
    api = next(e for e in events(h, 'action=workspace') if e['action'] == 'workspace.submit')
    # SW3-D-22: what_changed is the sentence the screen shows, not an internal id.
    assert submit[4] == '\'=HYPERLINK("x")' and submit[3] == api['summary'] == 'submitted a rota' and submit[5] == 'no' and submit[6]
    zone = ZoneInfo(h.get('auditor', '/api/me')['organisation']['timezone'])
    local = datetime.fromisoformat(api['created_at'].replace('Z', '+00:00'))
    local = (local if local.tzinfo else local.replace(tzinfo=timezone.utc)).astimezone(zone)
    assert submit[0] == local.strftime('%Y-%m-%d %H:%M:%S') and submit[1] == api['actor_display_name']
    assert all(r[2].startswith('workspace.') for r in rows[1:])
    monkeypatch.setattr(main, 'AUDIT_EXPORT_MAX', 1)
    truncated = export(h, 'action=workspace')
    assert truncated.headers['x-export-truncated'] == '1'
    rows = list(csv.reader(io.StringIO(truncated.text)))
    assert rows[0][0].startswith('Note: only the newest 1') and rows[1][0] == 'time' and len(rows) == 3


def test_summary_reads_a_short_page_without_the_people_list(h):
    w = h.workspace('Limit check')
    for _ in range(3):
        w = h.shift(w)['workspace']
    page = h.get('auditor', '/api/audit?limit=2&people=false')
    assert len(page['events']) == 2 and page['limit'] == 2 and page['people'] == []
    assert page['events'] == h.get('auditor', '/api/audit')['events'][:2]
    assert h.get('auditor', '/api/audit')['people']
    h.get('auditor', '/api/audit?limit=0', status=422)
    h.get('auditor', '/api/audit?limit=101', status=422)


def test_a_configuration_change_names_its_list_and_a_create_names_its_record(h):
    made = h.configure('location_types', {'name': 'Procedure room', 'description': '', 'starts_on': '2026-10-03', 'active': True})['record']
    h.configure('location_types', {'name': 'Procedure room B', 'description': '', 'starts_on': '2026-10-03', 'active': True}, made['id'], made['revision'])
    changed, created = events(h, 'action=catalogue')[:2]
    assert (created['action'], created['target_id'], created['detail']['catalogue']) == ('catalogue.create', made['id'], 'location_types')
    assert (changed['action'], changed['target_id'], changed['detail']['catalogue']) == ('catalogue.update', made['id'], 'location_types')
