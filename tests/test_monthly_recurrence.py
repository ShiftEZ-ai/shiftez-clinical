from datetime import date
from zoneinfo import ZoneInfo
import pytest
from packages.contracts.schemas import SeriesDefinition
from packages.domain.recurrence import expand


def test_monthly_weekday_uses_calendar_not_viewport_and_retains_midnight():
    definition = SeriesDefinition(anchor=date(2026, 1, 1), month_occurrence=-1,
        days=[{'weekday': 1, 'start_time': '16:00', 'end_time': '24:00'}])
    all_rows = expand(definition, date(2026, 2, 1), date(2026, 4, 1))
    assert [r['start'].date().isoformat() for r in all_rows] == ['2026-02-24', '2026-03-31']
    assert all_rows[1:] == expand(definition, date(2026, 3, 10), date(2026, 4, 1))
    assert all_rows[-1]['end'].astimezone(ZoneInfo('Asia/Riyadh')).date() == date(2026, 4, 1)
    with pytest.raises(ValueError, match='either monthly'):
        SeriesDefinition(**{**definition.model_dump(), 'interval_weeks': 2})
