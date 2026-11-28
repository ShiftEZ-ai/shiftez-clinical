"""The run card's words for stop conditions come from the contract: every stop reason and every conflict choice has
words, and every stop option has a title and help (exported to the client as GENERATION_STOP_FIELDS)."""
from typing import get_args

from packages.contracts.schemas import (GENERATION_CONFLICT_CHOICES, GENERATION_STAGE_LABELS, GENERATION_STOP_REASONS,
                                       GenerationOptions, GenerationSearchStop)


def test_every_stop_reason_and_conflict_choice_has_words():
    assert set(GENERATION_STOP_REASONS) == set(get_args(GenerationSearchStop.model_fields['reason'].annotation))
    assert set(GENERATION_CONFLICT_CHOICES) == set(get_args(GenerationOptions.model_fields['on_conflict'].annotation))
    assert all(words.strip() for words in [*GENERATION_STOP_REASONS.values(), *GENERATION_CONFLICT_CHOICES.values()])


def test_every_stage_the_engine_searches_has_a_name():
    # The receipt names each objectives row by the contract (fill stages 'fill:<group>:<order>' by their group), and the
    # engine's warnings say the same words in lower case (fill_priority.stage_label).
    import re
    from pathlib import Path
    from packages.domain.fill_priority import STAGE_LABELS, STAGE_ORDER
    source = (Path(__file__).resolve().parents[1] / 'packages/domain/cp_sat.py').read_text(encoding='utf-8')
    tiers = set(re.findall(r"\('(\w+)', [^()]*?, 0\)", source))
    assert {'coverage', 'desired_coverage', 'staffing', 'weighted_prefix', 'weighted'} <= tiers
    assert tiers | set(STAGE_ORDER) <= set(GENERATION_STAGE_LABELS)
    assert all(words.strip() for words in GENERATION_STAGE_LABELS.values())
    assert {stage: GENERATION_STAGE_LABELS[stage].lower() for stage in STAGE_ORDER} == STAGE_LABELS


def test_every_stop_option_has_a_title_and_help():
    for name in ('stop_without_improvement_seconds', 'target_gap_percent', 'target_score', 'stop_by', 'on_conflict'):
        field = GenerationOptions.model_fields[name]
        assert field.title and field.description, name


def test_the_cost_of_continuing_past_a_conflict_is_written_once_in_the_contract():
    # app.ts says the help after its first sentence beside the choice (CONFLICT_COST) and never types it again.
    from pathlib import Path
    first, cost = GenerationOptions.model_fields['on_conflict'].description.split('. ', 1)
    assert cost == 'Every broken rule is a must-fix and blocks publishing until fixed.' and '.' not in first
    app = (Path(__file__).resolve().parents[1] / 'apps/web/src/app.ts').read_text(encoding='utf-8')
    assert cost not in app
    assert "CONFLICT_COST=GENERATION_STOP_FIELDS.on_conflict.help.replace(" in app and app.count('CONFLICT_COST') == 3
