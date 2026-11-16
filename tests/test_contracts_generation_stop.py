"""The run card's words for stop conditions come from the contract: every stop reason and every conflict choice has
words, and every stop option has a title and help (exported to the client as GENERATION_STOP_FIELDS)."""
from typing import get_args

from packages.contracts.schemas import GENERATION_CONFLICT_CHOICES, GENERATION_STOP_REASONS, GenerationOptions, GenerationSearchStop


def test_every_stop_reason_and_conflict_choice_has_words():
    assert set(GENERATION_STOP_REASONS) == set(get_args(GenerationSearchStop.model_fields['reason'].annotation))
    assert set(GENERATION_CONFLICT_CHOICES) == set(get_args(GenerationOptions.model_fields['on_conflict'].annotation))
    assert all(words.strip() for words in [*GENERATION_STOP_REASONS.values(), *GENERATION_CONFLICT_CHOICES.values()])


def test_every_stop_option_has_a_title_and_help():
    for name in ('stop_without_improvement_seconds', 'target_gap_percent', 'target_score', 'stop_by', 'on_conflict'):
        field = GenerationOptions.model_fields[name]
        assert field.title and field.description, name
