"""Run the JSON condition cases against the shared domain evaluator.

Frontend tests can import fixtures/condition_cases.json directly: shallow-merge the root context with case.context,
use case.held as the candidate's field values, and compare with case.expected. case.row, when present, supplies a
separate scalar row; otherwise it is case.held. Lists are sets of held values. Relative values come from context.values
or the context calendar; no database, Python metadata or additional expected-result implementation is needed.
"""
from copy import deepcopy
from datetime import date
import json
from pathlib import Path

import pytest

from packages.domain.conditions import EvaluationContext, evaluate


FIXTURE = json.loads((Path(__file__).parent / 'fixtures' / 'condition_cases.json').read_text(encoding='utf-8'))


@pytest.mark.parametrize('case', FIXTURE['cases'], ids=lambda case: case['id'])
def test_shared_condition_case(case):
    original = deepcopy(case)
    calendar = deepcopy(FIXTURE['context'] | case.get('context', {}))
    for key in ('today', 'as_of', 'duty_day'):
        if calendar.get(key):
            calendar[key] = date.fromisoformat(calendar[key])
    if 'allowed_tokens' in calendar:
        calendar['allowed_tokens'] = frozenset(calendar['allowed_tokens'])
    context = EvaluationContext(**calendar)

    def holds(field):
        value = case['held'].get(field)
        return set(value) if isinstance(value, list) else value

    assert evaluate(case['tree'], holds, context, row=case.get('row', case['held'])) is case['expected']
    if len(case['tree']['conditions']) == 1 and 'conditions' not in case['tree']['conditions'][0]:
        assert evaluate(case['tree']['conditions'][0], holds, context,
                        row=case.get('row', case['held'])) is case['expected']
    assert case == original
