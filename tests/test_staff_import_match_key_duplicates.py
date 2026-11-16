"""Source duplicate checks use the same keys as matching existing staff."""
import pytest

from .test_staff_import import apply, catalogues, csv_of, preview, row


@pytest.mark.parametrize('field, first, second', [
    ('staff_number', 'Case-42', 'case-42'),
    ('staff_number', 'Cáse-42', 'case-42'),
    ('short_code', 'Ab42', 'ab42'),
])
def test_import_refuses_normalized_duplicate_source_identifiers(h, field, first, second):
    before = len(catalogues(h)['people'])
    result = preview(h, csv_of(row(display_name='First synthetic person', **{field: first}),
                               row(display_name='Second synthetic person', **{field: second})))
    assert result['rows'][0]['errors'] == {}
    assert result['rows'][1]['errors'][field] == f"Same {field.replace('_', ' ')} as row 2"
    h.command('coordinator', 'staff_import.apply', {'reason': 'Must reject duplicate source rows'},
              result['batch']['id'], result['batch']['revision'], status=422)
    assert len(catalogues(h)['people']) == before


def test_import_refuses_accent_and_space_equivalent_new_names(h):
    result = preview(h, csv_of(row(display_name='Dr José   Núñez'), row(display_name='dr jose nunez')))
    assert result['rows'][0]['errors'] == {}
    assert result['rows'][1]['errors']['display_name'].startswith('Same name as row 2;')
    h.command('coordinator', 'staff_import.apply', {'reason': 'Must reject duplicate source names'},
              result['batch']['id'], result['batch']['revision'], status=422)


def test_import_normalized_names_with_distinct_identifiers_remain_distinct(h):
    result = preview(h, csv_of(row(display_name='Dr José Núñez', staff_number='Case-42'),
                               row(display_name='dr jose nunez', staff_number='Case-43')))
    assert all(not entry['errors'] for entry in result['rows'])
    apply(h, 'coordinator', result['batch'])
    assert {'Case-42', 'Case-43'} <= {person['staff_number'] for person in catalogues(h)['people']}
