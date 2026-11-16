"""Writers without definition history either invalidate catalogues or have an absolute expiry."""
from types import SimpleNamespace

from sqlalchemy import func, select

from apps.api import main, models as m
from apps.api.db import transaction
from tests.test_announcement_receipts import notice


def revisions_and_history(h):
    with transaction(h.app.state.engine) as db:
        stamp = db.execute(select(m.Department.catalogue_revision, m.Organisation.input_revision,
            m.Organisation.revision).join(m.Organisation, m.Organisation.id == m.Department.organisation_id)
            .where(m.Department.id == h.seed['department_id'])).one()
        history = db.scalar(select(func.count()).select_from(m.DefinitionHistory).where(
            m.DefinitionHistory.department_id == h.seed['department_id']))
        return tuple(stamp), history


def test_closure_without_definition_history_refreshes_a_warm_catalogue(h):
    before = h.get('coordinator', '/api/catalogues?include=closures')
    stamp, history = revisions_and_history(h)
    created = h.command('coordinator', 'closure.create', {
        'location_id': h.seed['locations'][0], 'start': '2026-10-04T08:00:00+03:00',
        'end': '2026-10-04T12:00:00+03:00', 'reason': 'Synthetic maintenance closure'})['record']
    changed_stamp, changed_history = revisions_and_history(h)
    assert changed_history == history and changed_stamp[0] == stamp[0]
    assert changed_stamp[1] > stamp[1]

    after = h.get('coordinator', '/api/catalogues?include=closures&known=' + before['_versions']['closures'])
    assert after['_versions']['closures'] != before['_versions']['closures']
    assert next(row for row in after['closures'] if row['id'] == created['id'])['reason'] == created['reason']


def test_reminder_metadata_refreshes_at_absolute_120_seconds_despite_cache_hits(h, monkeypatch):
    row = notice(h, {'roles': ['staff']})
    clock = SimpleNamespace(seconds=1000)
    monkeypatch.setattr(main, 'time', SimpleNamespace(monotonic=lambda: clock.seconds))
    path = '/api/catalogues?include=department_content'
    before = h.get('coordinator', path)
    initial = next(item for item in before['department_content'] if item['id'] == row['id'])
    assert initial['last_reminded_at'] is None
    stamp, history = revisions_and_history(h)

    reminded = h.command('coordinator', 'department_content.remind', {'reason': 'Synthetic follow up'},
                         row['id'], row['revision'])
    assert reminded['reminded'] > 0 and reminded['record']['last_reminded_at'] is not None
    assert revisions_and_history(h) == (stamp, history)

    for elapsed in (60, 119):
        clock.seconds = 1000 + elapsed
        unchanged = h.get('coordinator', path + '&known=' + before['_versions']['department_content'])
        assert 'department_content' not in unchanged
        assert unchanged['_versions'] == before['_versions']

    clock.seconds = 1120
    after = h.get('coordinator', path + '&known=' + before['_versions']['department_content'])
    current = next(item for item in after['department_content'] if item['id'] == row['id'])
    assert current['last_reminded_at'] is not None
    assert after['_versions']['department_content'] != before['_versions']['department_content']
