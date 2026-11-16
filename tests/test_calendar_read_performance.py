"""Personal calendar read budgets count loaded rows, independent of CI host speed."""
import pytest

from apps.api.services import calendar as calendar_service, schedule_view


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_personal_calendar_loads_only_assigned_sessions(request, monkeypatch, backend):
    h = request.getfixturevalue(backend)
    me = h.get('staff', '/api/me')['person_id']
    other = next(pid for pid in h.seed['people'] if pid != me)
    workspace = h.shift(h.workspace(start='2026-10-01', end='2026-11-01'), people=[me, other])['workspace']
    for day in range(5, 17):
        workspace = h.shift(workspace, start=f'2026-10-{day:02}T08:00:00+03:00',
                            end=f'2026-10-{day:02}T12:00:00+03:00', people=[other])['workspace']
    h.publish(workspace)
    loaded = []
    original = schedule_view.shift_records

    def counted(db, rows):
        rows = list(rows)
        loaded.extend(row.id for row in rows)
        return original(db, rows)

    monkeypatch.setattr(schedule_view, 'shift_records', counted)
    path = '/api/calendar?start=2026-10-01&end=2026-11-01'
    personal = h.get('staff', path)
    assert len(personal['events']) == 1 and personal['sessions'] == []
    assert len(loaded) == 1, 'Personal reads must load assigned sessions, not the department month'
    selected = calendar_service.published_shifts

    def unfiltered(*args, **kwargs):
        kwargs.pop('person_id', None)
        return selected(*args, **kwargs)

    monkeypatch.setattr(calendar_service, 'published_shifts', unfiltered)
    assert h.get('staff', path) == personal
