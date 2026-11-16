"""Date-set contracts preserve existing inputs and distinguish named entry filters."""
from uuid import uuid4

import pytest
from pydantic import ValidationError

from packages.contracts import schemas as s
from scripts.generate_contracts import render


def test_existing_calendar_and_conditions_serialize_unchanged():
    calendar = {'schema_version': 1, 'timezone': 'Asia/Riyadh', 'week': [], 'overrides': []}
    assert s.OperatingCalendarDefinition.model_validate(calendar).model_dump(mode='json') == calendar
    condition = {'field': 'service_ids', 'op': 'any', 'values': ['clinic']}
    assert s.Condition.model_validate(condition).model_dump(mode='json') == condition


def test_date_set_atom_is_session_scope_and_part_of_generated_field_list():
    atom = {'field': 'date_set_ids', 'values': [str(uuid4())], 'entry_names': ['Eid']}
    parsed = s.Condition.model_validate(atom)
    assert parsed.field in s.CONDITION_FIELDS and parsed.field in s.SESSION_SCOPE_FIELDS
    assert parsed.field not in s.LEAD_FIELDS
    with pytest.raises(ValidationError, match='Who cannot filter'):
        s.RuleLine(id=str(uuid4()), who={'conditions': [atom]}, numbers={'apply': {}})
    assert s.RuleLine(id=str(uuid4()), where={'conditions': [atom]}, numbers={'apply': {}}).where


def test_generated_date_set_contracts_preserve_field_order_and_export_markers():
    assert s.CONDITION_FIELDS.index('person_ids') == s.CONDITION_FIELDS.index('tags') + 1
    assert s.CONDITION_FIELDS[-1] == 'date_set_ids'
    content, schemas = render()
    assert 'export type DateSetMarker = ' in content
    assert schemas['DateSetMarker'] == s.DateSetMarker.model_json_schema()


def test_entry_filter_is_part_of_rule_identity_and_only_applies_to_date_sets():
    atom = {'field': 'date_set_ids', 'values': [str(uuid4())]}
    first = {'conditions': [{**atom, 'entry_names': ['Eid', 'Ramadan']}]}
    assert s._canonical_tree(first) == s._canonical_tree({'conditions': [{**atom, 'entry_names': ['Ramadan', 'Eid']}]})
    assert s._canonical_tree(first) != s._canonical_tree({'conditions': [atom]})
    with pytest.raises(ValidationError, match='Entry names apply only'):
        s.Condition(field='tags', values=['holiday'], entry_names=['Eid'])
    with pytest.raises(ValidationError):
        s.Condition(field='date_set_ids', values=['unknown'])


def test_date_set_calendar_windows_are_validated():
    rule = {'date_set_id': str(uuid4()), 'windows': []}
    assert s.DateSetCalendarRule.model_validate(rule).windows == []
    with pytest.raises(ValidationError, match='must not overlap'):
        s.DateSetCalendarRule.model_validate({**rule, 'windows': [
            {'start_time': '08:00', 'end_time': '12:00'}, {'start_time': '11:00', 'end_time': '13:00'}]})


def test_date_set_all_accepts_overlapping_sets_but_single_valued_fields_still_refuse_all():
    ids = [str(uuid4()), str(uuid4())]
    condition = s.Condition(field='date_set_ids', op='all', values=ids)
    assert condition.op == 'all' and condition.values == ids
    for field in ('person_ids', 'location_ids', 'assignment_type_ids'):
        with pytest.raises(ValidationError, match='Has all of cannot match'):
            s.Condition(field=field, op='all', values=ids)


def test_date_set_entry_and_year_adjustment_contracts_reject_ambiguous_data():
    entry = {'id': str(uuid4()), 'name': 'Eid', 'repeat': 'yearly_hijri',
             'first_day': '1446-10-01', 'last_day': '1446-10-04'}
    assert s.DateSetInput(name='Public holidays', entries=[entry]).entries[0].repeat == 'yearly_hijri'
    with pytest.raises(ValidationError, match='Entry IDs must be unique'):
        s.DateSetInput(name='Holidays', entries=[entry, entry])
    with pytest.raises(ValidationError, match='same recurrence year'):
        s.DateSetAdjustmentInput(entry_id=entry['id'], year=1446, override={
            'year': 1447, 'first_day': '2025-03-31', 'last_day': '2025-04-03'}, reason='Moon sighting')
    assert s.DateSetAdjustmentInput(entry_id=entry['id'], year=1446, override=None, reason='Clear adjustment').override is None


@pytest.mark.parametrize('changes', [
    {'start_year': 0}, {'start_year': 10000}, {'start_year': True}, {'start_year': '2025'},
    {'repeat': 'once', 'start_year': 2025}, {'repeat': 'yearly_hijri', 'fixed_duration': True},
    {'fixed_duration': True, 'last_day_of_month': True},
])
def test_yearly_import_metadata_is_validated(changes):
    with pytest.raises(ValidationError):
        s.DateSetEntry.model_validate({'name': 'Annual event', 'repeat': 'yearly_gregorian',
                                      'first_day': '2025-02-28', 'last_day': '2025-03-01', **changes})
