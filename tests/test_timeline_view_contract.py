"""A saved Timeline view (TableViewDefinition.timeline) keeps its zoom, layers, properties, density and weekends, and the
Rotation board's Filter properties (TABLE_CONDITION_DEFINITIONS['rotation-board']) are accepted in its conditions."""
import pytest
from pydantic import ValidationError

from packages.contracts import schemas as s


def test_rotation_board_view_round_trips_with_an_or_group():
    view = s.TableViewInput.model_validate({"table_key": "rotation-board", "name": "Fictional planning view", "definition": {
        "layout": "timeline", "filters": {"programme": ["p1"]}, "group": "programme",
        "conditions": {"match": "any", "conditions": [{"field": "rotation_type", "op": "any", "values": ["t1"]},
                                                      {"field": "trainees", "op": "any", "values": ["external"]}]},
        "timeline": {"zoom": "week", "hidden": ["leave"], "properties": ["cohort"], "density": "compact", "weekends": False}}})
    t = view.definition.timeline
    assert (view.definition.layout, t.zoom, t.hidden, t.properties, t.density, t.weekends) == ("timeline", "week", ["leave"], ["cohort"], "compact", False)
    assert {f["key"] for f in s.table_condition_definitions("rotation-board")} == {"programme", "cohort", "rotation_type", "trainees"}


def test_timeline_settings_are_bounded():
    with pytest.raises(ValidationError):
        s.TimelineViewSettings.model_validate({"zoom": "decade"})
    with pytest.raises(ValidationError):
        s.TableViewInput.model_validate({"table_key": "rotation-board", "name": "x", "definition": {
            "layout": "timeline", "conditions": {"match": "all", "conditions": [{"field": "unknown_field", "op": "any", "values": ["a"]}]}}})


@pytest.mark.parametrize('bar_label', ['name_length', 'name', 'dates'])
@pytest.mark.parametrize('colour_by', ['rotation_type', 'service', 'cohort'])
def test_timeline_bar_settings_round_trip(bar_label, colour_by):
    definition = s.TableViewDefinition(timeline={'bar_label': bar_label, 'colour_by': colour_by})
    saved = s.TableViewDefinition.model_validate_json(definition.model_dump_json())
    assert (saved.timeline.bar_label, saved.timeline.colour_by) == (bar_label, colour_by)


def test_timeline_bar_settings_defaults_and_validation():
    saved = s.TableViewDefinition.model_validate({'timeline': {'zoom': 'month'}})
    assert (saved.timeline.bar_label, saved.timeline.colour_by) == ('name_length', 'rotation_type')
    for value in ({'bar_label': 'length'}, {'colour_by': 'person'}, {'bar_label': None}, {'colour_by': None}):
        with pytest.raises(ValidationError):
            s.TimelineViewSettings.model_validate(value)
