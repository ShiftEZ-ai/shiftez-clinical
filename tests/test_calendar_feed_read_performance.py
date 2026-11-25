"""ICS projections preserve frozen report contents without decoding unrelated publication data."""
from datetime import date

import pytest
from sqlalchemy import event
from sqlalchemy.orm.attributes import set_committed_value

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import calendar_feeds, schedule
from packages.domain.reports import calendar_feed, publication_duty_ids


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('trimmed', [False, True])
def test_subscription_projects_frozen_personal_duties_and_matches_full_snapshot(request, backend, trimmed):
    h = request.getfixturevalue(backend)
    me = h.get('staff', '/api/me')['person_id']
    other = next(pid for pid in h.seed['people'] if pid != me)
    workspace = h.shift(h.workspace(), people=[me, other])['workspace']
    workspace = h.shift(workspace, start='2026-10-06T08:00:00+03:00',
                        end='2026-10-06T12:00:00+03:00', people=[me])['workspace']
    workspace = h.shift(workspace, start='2026-10-07T08:00:00+03:00',
                        end='2026-10-07T12:00:00+03:00', people=[other])['workspace']
    publication = h.publish(workspace)
    statements = []
    with transaction(h.app.state.engine) as db:
        pub = db.get(m.Publication, publication['publication_id'])
        if trimmed:
            set_committed_value(pub, 'effective_until', date(2026, 10, 6))
        snapshot = dict(schedule.in_force_snapshot(db, pub))
        assert pub.calendar_lineage is True
        snapshot['_calendar_duty_ids'] = publication_duty_ids([(pub.snapshot['shifts'], True, None)], me)
        expected = calendar_feed([(snapshot, pub.sequence, pub.created_at)], me, 'My duties')
        # The cutoff row is a read-only test input; immutable publication fields are never changed.
        cutoff = pub.effective_until
        db.expunge(pub)
        light, = schedule.current_publications(db, [h.seed['department_id']], light=True)
        set_committed_value(light, 'effective_until', cutoff)

        def executed(conn, cursor, statement, parameters, context, many):
            statements.append(statement)

        event.listen(h.app.state.engine, 'before_cursor_execute', executed)
        try:
            projected = calendar_feeds.publication_calendars(db, [light], me, 'Asia/Riyadh')
        finally:
            event.remove(h.app.state.engine, 'before_cursor_execute', executed)
        snapshot, _, _ = projected[0]
        assert len(snapshot['shifts']) == (1 if trimmed else 2)
        assert all(any(p['person_id'] == me for p in row['participants']) for row in snapshot['shifts'])
        assert 'validation' not in snapshot and 'wishes' not in snapshot and 'policies' not in snapshot
        assert calendar_feed(projected, me, 'My duties') == expected
        assert 0 < len(statements) <= 2, 'Projection query budget stays constant as publication sessions grow'
        assert all('publications.validation' not in sql and 'SELECT publications.snapshot ' not in sql for sql in statements)
        if backend == 'h':
            assert statements[0].lower().count('json_extract(') == 1, 'Project frozen fields in one snapshot parse'
            assert all('json_quote(' not in sql.lower() for sql in statements), 'Do not reserialize arrays before scanning them'
        else:
            assert statements[0].lower().count('json_to_record(') == 1, 'Project frozen fields in one snapshot parse'
        assert 'snapshot' in light._sa_instance_state.unloaded and 'validation' in light._sa_instance_state.unloaded
