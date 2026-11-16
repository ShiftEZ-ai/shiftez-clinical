"""Drill metric quantities stay distinct from the existing bounded row counter."""
import pytest

from .test_fairness_dashboard import session, PERIOD


@pytest.mark.parametrize('metric,expected', [('hours', 12), ('duties', 3), ('session_units', 3),
    ('overtime_hours', 4), ('overtime_duties', 1), ('weekends', 0), ('pair_exposure', 1)])
def test_metric_amount_uses_whole_canonical_result_with_row_cap(h, monkeypatch, metric, expected):
    from apps.api.services import fairness
    a, b, _ = h.seed['people']
    w = session(h, h.workspace(), 4, [a, b], overtime=[a])
    w = session(h, w, 5, [a])
    w = session(h, w, 6, [a])
    h.publish(w)
    monkeypatch.setattr(fairness, 'SESSION_LIMIT', 1)
    reply = h.get('coordinator', f'/api/fairness/sessions?person_id={a}&metric={metric}' + PERIOD)
    assert reply['total'] == 1  # Pager contract remains a row count.
    assert reply['metric_value'] == expected
    assert reply['metric_value'] == sum(p['value'] for p in reply['publications'])
    assert reply['basis'] == 'published_plan'
    assert reply['truncated'] == (metric not in ('pair_exposure', 'overtime_hours', 'overtime_duties'))
    empty = h.get('coordinator', f'/api/fairness/sessions?person_id={a}&metric={metric}&starts_on=2027-01-01&ends_on=2027-02-01')
    assert empty['total'] == empty['metric_value'] == 0


def test_filtered_partial_plan_and_cancelled_duties_use_the_existing_ledger(h):
    from apps.api.db import transaction
    from apps.api import models as m
    from packages.domain import fairness as f
    a = h.seed['people'][0]
    w = h.workspace()
    start, end = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'
    created = h.shift(w, start, end, people=[{'person_id': a, 'start': start, 'end': end,
        'segments': [{'start': start, 'end': '2026-10-04T09:00:00+03:00'},
                     {'start': '2026-10-04T11:00:00+03:00', 'end': end}]}, h.seed['people'][1]])
    w = created['workspace']
    w = session(h, w, 5, [a])
    cancelled = h.shift(w, '2026-10-06T08:00:00+03:00', '2026-10-06T12:00:00+03:00')
    w = h.command('coordinator', 'shift.action', {'workspace_id': w['id'], 'shift_ids': [cancelled['shift_id']],
        'action': 'cancel', 'reason': 'Cancelled synthetic duty'}, revision=cancelled['workspace']['revision'])['workspace']
    pub = h.publish(w)['publication_id']
    with transaction(h.app.state.engine) as db:
        shift = db.get(m.Publication, pub).snapshot['shifts'][0]
        participant = next(p for p in shift['participants'] if p['person_id'] == a)
        expected = f.contribution('hours', shift, participant)
    reply = h.get('coordinator', f'/api/fairness/sessions?person_id={a}&metric=hours&starts_on=2026-10-04&ends_on=2026-10-05')
    assert reply['total'] == 1 and reply['metric_value'] == expected
    assert reply['publications'][0]['sessions'][0]['value'] == expected
    all_days = h.get('coordinator', f'/api/fairness/sessions?person_id={a}&metric=hours' + PERIOD)
    assert all_days['total'] == 2 and all_days['metric_value'] == expected + 4
    assert all_days['basis'] == 'published_plan'  # Planned reservation quantity, never actual attendance.


def test_weekend_total_is_distinct_across_current_publication_partitions(h):
    a = h.seed['people'][0]
    h.publish(session(h, h.workspace('Friday', '2026-10-09', '2026-10-10'), 9, [a]))
    h.publish(session(h, h.workspace('Saturday', '2026-10-10', '2026-10-11'), 10, [a]))
    reply = h.get('coordinator', f'/api/fairness/sessions?person_id={a}&metric=weekends' + PERIOD)
    distribution = h.get('coordinator', '/api/fairness/distribution?metric=weekends&starts_on=2026-10-01&ends_on=2026-11-01')
    expected = next(row['value'] for row in distribution['people'] if row['person_id'] == a)
    assert reply['total'] == 2 and reply['metric_value'] == expected == 1
    assert [p['value'] for p in reply['publications']] == [1, 1]  # Existing per-publication evidence is unchanged.
