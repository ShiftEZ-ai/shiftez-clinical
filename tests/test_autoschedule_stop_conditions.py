"""Generation stop controls are bounded, explicit and part of the shared contract."""
from datetime import datetime, timezone

import pytest

from packages.contracts.schemas import GenerationOptions
from packages.domain import solver_control
from packages.domain.solver_control import SolverControl


@pytest.mark.parametrize(
    ('options', 'objective', 'bound', 'score', 'reason'),
    [
        ({'target_gap_percent': 5}, 105, 100, None, 'target_gap'),
        ({'target_gap_percent': 4}, 105, 100, None, None),
        ({'target_score': 80}, None, None, 80, 'target_score'),
        ({'target_score': 80}, None, None, 79.9, None),
    ],
)
def test_target_stop_conditions_use_the_shared_generation_options(options, objective, bound, score, reason):
    control = SolverControl()
    control.configure_stop_conditions(GenerationOptions.model_validate(options), 'UTC')
    control.best_score = score
    assert control.target_stop_reason(objective, bound) == reason


def test_clock_stop_uses_department_timezone_and_supports_midnight(monkeypatch):
    now = {'value': datetime(2026, 11, 7, 4, 1, tzinfo=timezone.utc)}
    monkeypatch.setattr(solver_control, 'utc_now', lambda: now['value'])
    control = SolverControl()
    control.configure_stop_conditions(GenerationOptions(stop_by='07:00'), 'Asia/Riyadh')
    assert control.clock_stop_due()  # 07:01 local

    now['value'] = datetime(2026, 11, 7, 20, 59, tzinfo=timezone.utc)
    control.configure_stop_conditions(GenerationOptions(stop_by='24:00'), 'Asia/Riyadh')
    assert not control.clock_stop_due()  # 00:59 local, before the next midnight
    now['value'] = datetime(2026, 11, 7, 21, 0, tzinfo=timezone.utc)
    assert control.clock_stop_due()  # midnight local is the following local date


@pytest.mark.parametrize('field, value', [('target_gap_percent', 101), ('target_score', -1)])
def test_target_stop_values_are_bounded_by_generation_options(field, value):
    with pytest.raises(ValueError):
        GenerationOptions.model_validate({field: value})


def test_generation_extension_is_the_add_time_contract_and_continue_minutes_are_optional():
    from packages.contracts.schemas import GenerationContinue, GenerationExtension, JobTimeInput

    assert GenerationExtension(minutes=3, reason='Add three minutes').minutes == 3
    assert JobTimeInput(minutes=3, reason='Legacy add time').minutes == 3
    assert GenerationContinue(reason='Continue the saved run').minutes is None
