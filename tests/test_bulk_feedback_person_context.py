"""Feedback conditions refer to the report's recorded reporter, preserving read scope."""
import pytest

from .test_bulk_linked_people import people_and_accounts, post


@pytest.mark.parametrize('field', ['person_ids', 'category_ids', 'programme_ids'])
@pytest.mark.parametrize('op', ['any', 'none'])
def test_feedback_person_conditions_resolve_and_page_the_recorded_reporter(h, field, op):
    people, values, _ = people_and_accounts(h)
    reports = [h.command(role, 'feedback.create', {'kind': 'problem', 'route': '#home',
        'message': 'Synthetic reporter context ' + role})['id']
        for role in ('linked-alex-staff', 'linked-blair-staff')]
    expected = reports[0 if op == 'any' else 1]
    selection = {'collection': 'feedback', 'where': {'as_of': '2026-10-04',
        'filters': {'id': reports}, 'conditions': {'conditions': [
            {'field': field, 'op': op, 'values': [values[field]]}]}}}
    resolved = post(h, 'resolve', selection, role='auditor')
    assert resolved['count'] == 1 and [row['id'] for row in resolved['sample']] == [expected]
    first = post(h, 'rows', {'selection': selection, 'columns': ['reporter_person_id'], 'limit': 1}, role='auditor')
    later = post(h, 'rows', {'selection': selection, 'columns': ['reporter_person_id'], 'offset': 1, 'limit': 1}, role='auditor')
    assert first['count'] == later['count'] == 1 and not later['rows']
    assert first['rows'][0]['id'] == expected
    assert first['rows'][0]['reporter_person_id'] == people[0 if op == 'any' else 1]
    # A condition must never widen the reporter-only read to another person's report.
    own = post(h, 'resolve', selection, role='linked-alex-staff')
    assert own['count'] == (1 if op == 'any' else 0)
