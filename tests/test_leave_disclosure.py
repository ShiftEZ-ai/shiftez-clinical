"""How leave shows on planning findings (Configuration › Department settings › Leave on planning issues): none, its type
and dates (the default), or also its note. The server adds `leave` in one place (services/leave_disclosure.py); the
stored check, scores and fingerprints never carry it, and staff never get it."""
import copy
import json
import uuid
from datetime import date, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import pytest
from sqlalchemy import text
from apps.api import models as m
from apps.api.db import make_engine, transaction
from apps.api.services.leave_disclosure import disclose_leave, leave_wording, overlap_labels
from packages.contracts.schemas import ABSENCE_CATEGORIES, ABSENCE_CATEGORY_LABELS, ABSENCE_REPORT_LABEL
from packages.domain.time import aware, now as clock_now
from .test_planner_why import why

NOTE = 'Conference in Riyadh'


def rota(h, day=None):
    """Person 0 on a 4 Oct session while on annual leave 3–5 Oct (with a note); the check is stored on the rota."""
    day = day or clock_now().astimezone(ZoneInfo('Asia/Riyadh')).date().replace(year=2026, month=10, day=4)
    away = h.seed['people'][0]
    h.command('coordinator', 'availability.record', {'person_id': away, 'kind': 'recorded_leave', 'leave_type': 'Annual leave',
        'start': f'{day - timedelta(days=1)}T00:00:00+03:00', 'end': f'{day + timedelta(days=2)}T00:00:00+03:00', 'private_reason': NOTE})
    w = h.workspace('Leave shown', start=(day - timedelta(days=1)).isoformat(), end=(day + timedelta(days=3)).isoformat())
    w = h.shift(w, start=f'{day}T08:00:00+03:00', end=f'{day}T12:00:00+03:00')['workspace']
    w = h.lifecycle(h.detail(w)['workspace'], 'validate')['workspace']
    return w, away


def setting(h, value, status=200, role='admin'):
    me = h.get('admin', '/api/me')
    org, dept = me['organisation'], me['department']
    return h.command(role, 'department.settings', {'department_name': dept['name'], 'department_revision': dept['revision'],
        'organisation_name': org['name'], 'timezone': org['timezone'], 'week_start': org['week_start'],
        'leave_reason_display': value, 'reason': 'How leave shows on planning issues'}, dept['id'], org['revision'], status=status)


def leave_of(h, w, role='coordinator'):
    found = [f for f in h.get(role, '/api/workspaces/' + w['id'])['workspace']['validation']['findings'] if f['code'] == 'UNAVAILABLE']
    assert found
    return [f.get('leave') for f in found]


def test_leave_shows_as_the_department_setting_allows_for_every_planning_role(h):
    day = clock_now().date() + timedelta(days=3)  # the one clock (tests/test_clock_guard.py)
    w, away = rota(h, day)
    assert h.get('admin', '/api/me')['department']['leave_reason_display'] == 'type'
    typed = {'type': 'Annual', 'starts_on': (day - timedelta(days=1)).isoformat(), 'ends_on': (day + timedelta(days=1)).isoformat()}
    for role in ('coordinator', 'approver', 'auditor'):
        assert leave_of(h, w, role) == [typed]
    # The stored check is the validator's own: no leave, no note (the domain is unchanged).
    with transaction(h.app.state.engine) as db:
        stored = db.get(m.Workspace, w['id']).validation
    assert not any('leave' in f for f in stored['findings']) and NOTE not in json.dumps(stored)
    night = h.detail(w)['shifts'][0]
    rows = why(h, w, [night['id']])['sessions'][0]['rows']
    reasons = [f for r in rows if r['person_id'] == away for f in r['findings'] if f['code'] == 'UNAVAILABLE']
    assert all(f['leave'] == typed for f in reasons)

    saved = setting(h, 'details')
    assert saved['department']['leave_reason_display'] == 'details' and saved['previous']['leave_reason_display'] == 'type'
    assert leave_of(h, w) == [{**typed, 'note': NOTE}]
    assert leave_of(h, w, 'auditor') == [{**typed, 'note': NOTE}]

    setting(h, 'none')
    assert leave_of(h, w) == [None]
    assert NOTE not in json.dumps(h.get('approver', '/api/workspaces/' + w['id']))

    # Staff read no planning findings, and the one disclosure point gives them nothing even if asked.
    h.get('staff', '/api/workspaces/' + w['id'], status=403)
    setting(h, 'details')
    finding = {'code': 'UNAVAILABLE', 'person_id': away, 'start': f'{day}T05:00:00+00:00', 'end': f'{day}T09:00:00+00:00'}
    with transaction(h.app.state.engine) as db:
        dept = h.get('admin', '/api/me')['department']
        staff = SimpleNamespace(department_id=dept['id'], organisation_id=dept['organisation_id'], permits=lambda *r: 'staff' in r)
        assert 'leave' not in disclose_leave(db, staff, [dict(finding)])[0]
        coordinator = SimpleNamespace(**{**vars(staff), 'permits': lambda *r: 'coordinator' in r})
        assert disclose_leave(db, coordinator, [dict(finding)])[0]['leave']['note'] == NOTE


def test_leave_setting_is_admin_only_validated_and_audited(h):
    setting(h, 'secret', status=422)
    setting(h, 'details', status=403, role='coordinator')
    saved = setting(h, 'details')
    assert h.get('coordinator', '/api/me')['department']['leave_reason_display'] == 'details'
    with transaction(h.app.state.engine) as db:
        assert db.query(m.DefinitionHistory).filter(m.DefinitionHistory.kind == 'department_settings').count() >= 1
    # Unset keeps it (the Auto-schedule page saves without it).
    me = h.get('admin', '/api/me')
    payload = {k: v for k, v in saved['previous'].items() if k not in ('leave_reason_display', 'department_revision')}
    kept = h.command('admin', 'department.settings', {**payload, 'department_revision': me['department']['revision'],
                     'reason': 'Unrelated save'}, me['department']['id'], me['organisation']['revision'])
    assert kept['department']['leave_reason_display'] == 'details'


def test_approved_absence_findings_and_flags_read_the_request_text_once(h):
    person = h.seed['people'][0]
    reason, note = 'sick', 'Report note stays behind the setting'
    with transaction(h.app.state.engine, write=True) as db:
        report = m.RequestRecord(department_id=h.seed['department_id'], person_id=person, kind='absence_report',
            start=aware('2026-10-04T08:00:00+03:00'), end=aware('2026-10-04T12:00:00+03:00'),
            private_reason='Legacy free reason', status='approved', created_by=h.seed['identities']['staff'],
            details={'reporter_category': reason, 'note': note, 'document_reference': 'DOC-1'})
        db.add(report); db.flush()
        absence = m.Availability(department_id=h.seed['department_id'], person_id=person, kind='approved_leave',
            start=report.start, end=report.end, leave_type='', private_reason='', recorded_by=h.seed['identities']['coordinator'],
            request_id=report.id)
        db.add(absence); db.flush()
        absence_id = absence.id
    finding = {'code': 'UNAVAILABLE', 'person_id': person, 'start': '2026-10-04T05:00:00+00:00',
               'end': '2026-10-04T09:00:00+00:00', 'availability_id': absence_id,
               'detail': {'leave': {'type': 'Annual leave', 'starts_on': '2026-10-04',
                                    'ends_on': '2026-10-04', 'note': note}},
               'summary': {'leave': 3}}
    nested = {'type': 'Annual leave', 'starts_on': '2026-10-04', 'ends_on': '2026-10-04'}
    for mode, expected in [('none', None), ('type', {'type': ABSENCE_CATEGORY_LABELS[reason], 'starts_on': '2026-10-04', 'ends_on': '2026-10-04'}),
                           ('details', {'type': ABSENCE_CATEGORY_LABELS[reason], 'starts_on': '2026-10-04', 'ends_on': '2026-10-04', 'note': note})]:
        with transaction(h.app.state.engine, write=True) as db:
            db.get(m.Department, h.seed['department_id']).leave_reason_display = mode
            for role in ('approver', 'auditor'):
                actor = SimpleNamespace(department_id=h.seed['department_id'], organisation_id=h.seed['organisation_id'],
                    person_id=h.seed['people'][1], permits=lambda *allowed, role=role: role in allowed)
                body = disclose_leave(db, actor, [copy.deepcopy(finding)])[0]
                flag = overlap_labels(db, actor, [copy.deepcopy(finding)])[0]
                nested_expected = None if expected is None else {**nested, **({'note': note} if mode == 'details' else {})}
                if mode == 'none':
                    assert 'leave' not in body and 'leave' not in body.get('detail', {})
                else:
                    assert body['leave'] == expected and body['detail']['leave'] == nested_expected
                assert body['summary']['leave'] == 3
                assert flag['label'] == ('Unavailable' if expected is None else ABSENCE_CATEGORY_LABELS[reason] +
                                        ((': ' + note) if mode == 'details' else ''))
                if mode == 'none':
                    assert 'leave' not in flag and 'leave' not in flag.get('detail', {})
                else:
                    assert flag['detail']['leave'] == nested_expected
                assert flag['summary']['leave'] == 3
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Availability, absence_id).private_reason == ''


def test_absence_category_read_normalizes_case_without_rewriting(h):
    row = SimpleNamespace(kind='absence_report', person_id='other', private_reason='Legacy free reason',
        details={'reporter_category': 'Sick', 'note': 'Private note'})
    department = SimpleNamespace(leave_reason_display='details')
    actor = SimpleNamespace(person_id='another', permits=lambda *roles: 'coordinator' in roles)
    assert leave_wording(department, actor, row) == {'absence_reason': 'sick', 'note': 'Private note'}


@pytest.mark.parametrize('mode', ['none', 'type', 'details'])
def test_absence_report_privacy_matrix_runs_on_sqlite(h, mode):
    row = SimpleNamespace(kind='absence_report', person_id='owner', private_reason='Private health explanation',
        details={'reporter_category': 'sick', 'note': 'Private report note', 'document_reference': 'Private document'})
    department = SimpleNamespace(leave_reason_display=mode)
    reporter = SimpleNamespace(person_id='owner', permits=lambda *roles: False)
    for role in ('approver', 'auditor'):
        reader = SimpleNamespace(person_id='other', permits=lambda *allowed, role=role: role in allowed)
        own = leave_wording(department, reporter, row)
        shown = leave_wording(department, reader, row)
        assert own == {'absence_reason': 'sick', 'note': 'Private report note', 'document_reference': 'Private document'}
        assert shown == ({'absence_reason': 'sick'} if mode == 'type' else
                         {'absence_reason': 'sick', 'note': 'Private report note', 'document_reference': 'Private document'}
                         if mode == 'details' else {})
        linked = SimpleNamespace(kind='recorded_leave', person_id='owner', private_reason='Legacy report reason',
            absence_report=True, absence_report_category='sick', absence_report_note='Private report note',
            absence_report_reason='Legacy report reason')
        linked_shown = leave_wording(department, reader, linked)
        assert linked_shown.get('label') == (ABSENCE_REPORT_LABEL if mode == 'none' else ABSENCE_CATEGORY_LABELS['sick'])


def test_migration_adds_the_setting_defaulting_to_type(tmp_path):
    """Data safety: 0057 adds one column in place; existing departments show the leave type."""
    from .migration_support import add_historical_admin

    from alembic import command
    from scripts.manage import alembic_config, migrate
    url = 'sqlite:///' + str(tmp_path / 'before.db')
    command.upgrade(alembic_config(url), '0056_department_weekend')
    engine = make_engine(url)
    org, dept = str(uuid.uuid4()), str(uuid.uuid4())
    with engine.begin() as c:
        c.execute(text("INSERT INTO organisations (id, revision, created_at, name, timezone, week_start, input_revision, availability_revision) "
                       "VALUES (:id, 1, '2026-01-01', 'Org', 'Asia/Riyadh', 6, 1, 1)"), {'id': org})
        c.execute(text("INSERT INTO departments (id, revision, created_at, organisation_id, name, generation_defaults) "
                       "VALUES (:id, 1, '2026-01-01', :o, 'Dept', '{}')"), {'id': dept, 'o': org})
        add_historical_admin(c, dept)
    engine.dispose()
    migrate(url)
    engine = make_engine(url)
    try:
        with engine.begin() as c:
            assert c.execute(text('SELECT name, leave_reason_display FROM departments')).fetchall() == [('Dept', 'type')]
    finally:
        engine.dispose()


def test_calendar_absence_wording_follows_the_setting_and_never_hides_a_persons_own(h):
    """A person's Work tab and the calendar read absences from /api/calendar: others' types and notes show only as the
    department setting allows; the person always sees their own record's type."""
    rota(h)
    away = h.seed['people'][0]

    def absence(role):
        events = h.get(role, f'/api/calendar?start=2026-10-03&end=2026-10-06&person_id={away}')['events']
        found = [e for e in events if e['kind'] == 'availability']
        assert len(found) == 1, found
        if role == 'staff':
            assert found[0]['leave'] == {'type': 'Annual', 'starts_on': '2026-10-03', 'ends_on': '2026-10-05'}
        elif value == 'none':
            assert 'leave' not in found[0]
        else:
            expected_leave = {'type': 'Annual', 'starts_on': '2026-10-03', 'ends_on': '2026-10-05'}
            if value == 'details':
                expected_leave['note'] = NOTE
            assert found[0]['leave'] == expected_leave
        return {k: v for k, v in found[0].items() if k in ('label', 'note')}

    assert h.get('staff', '/api/me')['person_id'] == away
    for value, seen in [('type', {'label': 'Annual'}), ('details', {'label': 'Annual', 'note': NOTE}),
                        ('none', {'label': 'On leave or away'})]:
        if value != 'type':
            setting(h, value)
        for role in ('coordinator', 'approver', 'auditor', 'admin'):
            assert absence(role) == seen, (value, role)
        assert absence('staff') == {'label': 'Annual'}  # their own record, whatever the setting


def test_coordinator_absence_choices_are_contract_categories():
    """The coordinator's "Mark absent" words and the reporter's categories are one list (no "other" for "Leave")."""
    from apps.api.services.leave_disclosure import canonical_absence_category
    assert [canonical_absence_category(word.capitalize()) for word in ABSENCE_CATEGORIES] == list(ABSENCE_CATEGORIES)
    assert tuple(ABSENCE_CATEGORY_LABELS) == ABSENCE_CATEGORIES
    assert canonical_absence_category('Leave') == 'leave'
    assert canonical_absence_category('Annual leave, private') is None


def test_a_reporter_cannot_choose_leave_but_every_category_still_reads():
    """DL-035: one reading vocabulary; a staff "Can’t make it" never offers or accepts "leave" (planned leave is a
    Leave request with its approval, balance and overlap checks)."""
    import pydantic
    from packages.contracts import schemas as s
    base = {'kind': 'absence_report', 'publication_id': s.uid() if hasattr(s, 'uid') else '00000000-0000-4000-8000-000000000001',
            'assignment_id': '00000000-0000-4000-8000-000000000002', 'reason': 'Unwell today'}
    for category in s.ABSENCE_REPORTER_CATEGORIES:
        assert s.AbsenceReportInput.model_validate({**base, 'category': category}).category == category
    with pytest.raises(pydantic.ValidationError, match='category'):
        s.AbsenceReportInput.model_validate({**base, 'category': 'leave'})
    assert 'leave' in s.ABSENCE_CATEGORIES and 'leave' not in s.ABSENCE_REPORTER_CATEGORIES
    assert s.ABSENCE_REPORT_LABEL == "Can’t make it"
