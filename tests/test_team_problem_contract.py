"""One exported wording set for supervision team problem states."""
import json
from pathlib import Path

from packages.contracts.schemas import TEAM_PROBLEM_WORDS


def test_team_problem_words_are_generated_from_contract():
    assert TEAM_PROBLEM_WORDS == {'needs': 'Needs supervisor', 'elsewhere': 'Not on this session',
                                  'unpaired': 'Unpaired', 'over': '{n} · limit {limit}'}
    assert TEAM_PROBLEM_WORDS['over'].format(n=3, limit=2) == '3 · limit 2'
    source = Path('apps/web/src/generated.ts').read_text()
    assert 'export const TEAM_PROBLEM_WORDS = ' + json.dumps(TEAM_PROBLEM_WORDS) + ' as const;' in source
