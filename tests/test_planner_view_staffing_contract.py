"""PlannerViewSettings.staffing reviewed: a saved Schedule view may hide its staffing numbers. Absent keeps
older views unchanged (shown) and is not stored; only a strict boolean is accepted. PlannerViewSettings.row_height
likewise: absent fits each row to its duties and is not stored; only "fit" or "same" is accepted."""
import pytest
from pydantic import ValidationError

from packages.contracts import schemas as s


def test_staffing_is_optional_strict_and_only_stored_when_set():
    assert 'staffing' not in s.PlannerViewSettings().model_dump()
    assert s.PlannerViewSettings(staffing=False).model_dump()['staffing'] is False
    assert s.TableViewDefinition(planner={'staffing': False}).planner.staffing is False
    with pytest.raises(ValidationError):
        s.PlannerViewSettings(staffing='no')


def test_row_height_is_optional_closed_and_only_stored_when_set():
    assert 'row_height' not in s.PlannerViewSettings().model_dump()
    assert s.PlannerViewSettings(row_height='same').model_dump()['row_height'] == 'same'
    assert s.TableViewDefinition(planner={'row_height': 'fit'}).planner.row_height == 'fit'
    with pytest.raises(ValidationError):
        s.PlannerViewSettings(row_height='tall')
