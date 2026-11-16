"""Operations collection filters cover the complete authorized history, while metrics stay global."""
import json
import uuid
from datetime import timedelta
from urllib.parse import urlencode

import pytest

from apps.api import models as m
from apps.api.db import transaction


def history(h, foreign=False):
    """Both exceptional kinds/states live beyond the newest 100 completed events."""
    with transaction(h.app.state.engine, write=True) as db:
        now, dept = m.now(), h.seed['department_id']

        def add(kind, state, error, age=0, department=dept):
            event = m.Outbox(department_id=department, event_key=str(uuid.uuid4()), kind=kind,
                payload={'private_reason': 'hidden payload needle'}, state=state, last_error=error,
                next_attempt_at=now, created_at=now-timedelta(days=age))
            db.add(event)
            db.flush()
            return event.id

        recent = [add('request', 'available_in_app', f'Completed event {i}' if i else 'Pattern decoy 100XXmatchZpath') for i in range(105)]
        failed = add('publication_notice', 'failed', r'Older Alpha 100%_match\path', 2)
        retry = add('announcement', 'retry', 'Older Beta delivery error', 1)
        if foreign:
            other = m.Department(organisation_id=h.seed['organisation_id'], name='Foreign operations department')
            db.add(other)
            db.flush()
            add('foreign_only_kind', 'failed', 'Older Alpha foreign secret', department=other.id)
    return {'recent': recent, 'failed': failed, 'retry': retry}


@pytest.mark.parametrize('role', ['operator', 'auditor'])
def test_operations_search_states_and_kinds_cover_all_history_without_changing_metrics(h, role):
    ids = history(h, foreign=True)
    read = lambda **query: h.get(role, '/api/operations?' + urlencode(query))
    all_events = read()
    assert all_events['total'] == 107 and len(all_events['outbox']) == 100
    assert ids['failed'] not in {event['id'] for event in all_events['outbox']}
    assert all_events['kinds'] == ['announcement', 'publication_notice', 'request']
    expected_states = {'queued': 0, 'retry': 1, 'failed': 1, 'available_in_app': 105, 'superseded': 0, 'sent': 0}
    assert all_events['metrics']['outbox_states'] == expected_states
    for query, expected in (
        ({'q': 'oLdEr aLpHa'}, {ids['failed']}),
        ({'q': r'100%_match\path'}, {ids['failed']}),
        ({'q': 'publication notice'}, {ids['failed']}),
        ({'q': 'failed'}, {ids['failed']}),
        ({'state': 'failed,retry'}, {ids['failed'], ids['retry']}),
        ({'kind': 'publication_notice,announcement'}, {ids['failed'], ids['retry']}),
        ({'state': 'failed,retry', 'kind': 'announcement', 'q': 'Beta'}, {ids['retry']}),
        ({'q': 'foreign secret'}, set()),
        ({'q': 'hidden payload needle'}, set()),
    ):
        reply = read(**query)
        assert {event['id'] for event in reply['outbox']} == expected, query
        assert reply['total'] == len(expected) and not reply['has_more'], query
        assert reply['metrics']['outbox_states'] == expected_states
        assert reply['metrics']['outbox_due_count'] == 1
        assert reply['kinds'] == all_events['kinds']
        assert 'private_reason' not in json.dumps(reply) and 'hidden payload needle' not in json.dumps(reply)

    first = read(q='available in app', state='available_in_app', kind='request')
    second = read(q='available in app', state='available_in_app', kind='request', offset=100)
    assert first['total'] == second['total'] == 105
    assert len(first['outbox']) == 100 and first['has_more']
    assert len(second['outbox']) == 5 and not second['has_more']
    assert {event['id'] for page in (first, second) for event in page['outbox']} == set(ids['recent'])


@pytest.mark.parametrize('query', [
    {'state': 'failed,delivered'}, {'state': 'failed,,retry'}, {'kind': ''}, {'kind': 'x'*41}, {'q': 'x'*201},
])
def test_operations_rejects_invalid_query(h, query):
    h.get('operator', '/api/operations?' + urlencode(query), status=422)


def test_operations_history_query_keeps_read_authority(h):
    h.get('staff', '/api/operations?state=failed,retry&q=Older&kind=announcement', status=403)
