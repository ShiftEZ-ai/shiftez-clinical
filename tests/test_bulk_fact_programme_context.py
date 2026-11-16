"""Calendar facts retain their canonical frozen programme and a separate person context."""
from uuid import uuid4

import pytest

from .test_bulk_linked_people import post


@pytest.mark.parametrize('personal', [False, True])
@pytest.mark.parametrize('path', ['condition', 'filter'])
def test_calendar_fact_programme_filters_use_frozen_scope_and_keep_person_programmes(h, personal, path):
    programmes = [h.configure('programmes', {'name': 'Synthetic ' + name, 'starts_on': '2026-01-01'})['record']
        for name in ('calendar programme', 'person programme', 'later calendar programme')]
    person = h.configure('people', {'display_name': 'Synthetic calendar fact subject'})['record']
    h.configure('programme_memberships', {'person_id': person['id'], 'programme_id': programmes[1]['id'],
        'starts_on': '2026-01-01', 'ends_on': '2027-01-01'})
    calendar_value = {'programme_id': programmes[0]['id'], 'name': 'Synthetic academic block',
        'kind': 'academic_block', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11'}
    entry = h.configure('programme_calendar_entries', calendar_value)['record']
    fact = h.command('coordinator', 'calendar.protection.propose', {
        'kind': 'protected_time', 'calendar_entry_id': entry['id'], 'catalogue_id': 'protected_time',
        **({'person_id': person['id']} if personal else {}), 'line': {
            'id': str(uuid4()), 'starts_on': calendar_value['starts_on'], 'ends_on': calendar_value['ends_on'],
            'weekdays': [1], 'start_time': '09:00', 'end_time': '11:00',
            'numbers': {'apply': {'value': None, 'level': 'mandatory'}}},
        'reason': 'Synthetic calendar programme selection'})['fact']
    selection = {'collection': 'scheduling_facts', 'where': {'as_of': '2026-10-04',
        'filters': {'id': [fact['id']]}}}

    def selected(programme):
        if path == 'condition':
            selection['where']['conditions'] = {'conditions': [{'field': 'programme_ids', 'values': [programme]}]}
        else:
            selection['where']['filters']['programme_ids'] = [programme]
        found = post(h, 'resolve', selection)
        page = post(h, 'rows', {'selection': selection, 'columns': ['data', 'person.programme_ids'],
            'sort': [{'field': 'person.programme_ids'}], 'group': 'person.programme_ids', 'limit': 1})
        assert found['count'] == page['count']
        return page

    for _ in range(2):
        page = selected(programmes[0]['id'])
        assert page['count'] == 1 and page['rows'][0]['id'] == fact['id']
        assert page['rows'][0]['data']['programme_id'] == programmes[0]['id']
        assert page['rows'][0]['person.programme_ids'] == ([programmes[1]['id']] if personal else None)
        assert selected(programmes[1]['id'])['count'] == 0
        assert selected(programmes[2]['id'])['count'] == 0
        if fact['status'] == 'proposed':
            fact = h.command('coordinator', 'calendar.protection.decline', {'reason': 'Synthetic declined scope retained'},
                fact['id'], fact['revision'])['fact']
            h.configure('programme_calendar_entries', {**calendar_value, 'programme_id': programmes[2]['id']},
                entry['id'], entry['revision'])
