"""Presentation metadata follows the server's accepted values and generated contract boundary."""
import json
from typing import get_args

import pytest

from packages.contracts import schemas as s
from scripts.generate_contracts import render
from .test_requests_search import both  # noqa: F401


@pytest.mark.parametrize('name', (
    'USUAL_ASSIGNEES_FIXED_DEFAULT', 'WEEKDAY_SHORT_LABELS', 'STRENGTH_LABELS',
    'SCORE_WEIGHT_LABELS', 'SCORE_WEIGHT_HELP', 'RULE_KINDS', 'RULE_HELP',
    'RULE_NUMBER_FIELD', 'RULE_TOPICS', 'FINDING_MESSAGES',
))
def test_presentation_metadata_is_exported_without_changing_values(name):
    source, _ = render()
    assert f'export const {name} = {json.dumps(getattr(s, name))} as const;' in source


def test_series_default_is_the_schema_default_and_respects_no_assignees():
    assert s.USUAL_ASSIGNEES_FIXED_DEFAULT is s.SeriesDefinition.model_fields['usual_assignees_fixed'].default
    definition = {'anchor': '2026-10-01', 'days': [{'weekday': 0, 'start_time': '08:00', 'end_time': '12:00'}]}
    assert s.SeriesDefinition(**definition).usual_assignees_fixed is False
    assert s.SeriesDefinition(**definition, default_person_ids=['00000000-0000-0000-0000-000000000001']).usual_assignees_fixed is s.USUAL_ASSIGNEES_FIXED_DEFAULT


def test_scoring_labels_cover_the_accepted_components_and_strengths_in_order():
    assert tuple(s.SCORE_WEIGHT_LABELS) == tuple(s.ScoreWeights.model_fields)
    assert s.SCORE_WEIGHT_HELP.keys() == s.SCORE_WEIGHT_LABELS.keys()
    assert tuple(s.STRENGTH_LABELS) == get_args(s.Strength) == tuple(s.StrengthMultipliers.model_fields)


def test_rule_metadata_covers_the_primitives_and_reuses_line_fields():
    kinds = [entry[0] for entry in s.RULE_KINDS]
    assert len(kinds) == len(set(kinds))
    assert set(kinds) == {get_args(model.model_fields['kind'].annotation)[0] for model in s.PRIMITIVE_MODELS}
    grouped = [kind for topic in s.RULE_TOPICS for kind in topic['kinds']]
    assert sorted(grouped) == sorted(kinds)
    assert set(s.RULE_HELP) == set(kinds) - {'balance_goal'}
    assert {kind: fields[0] for kind, fields in s.RULE_NUMBER_FIELD.items()
            if kind not in {'travel_buffer', 'team_lead_requirement'}} == s.PER_PERSON_FIELD
    assert s.RULE_NUMBER_FIELD['travel_buffer'] == ('minutes', 'Minutes')
    assert s.RULE_NUMBER_FIELD['team_lead_requirement'] == ('minimum', 'Minimum leaders')
    assert 'team_lead_requirement' not in s.PER_PERSON_FIELD  # qualification stays on governed catalogue lines
    for kind, (field, label) in s.RULE_NUMBER_FIELD.items():
        model = next(model for model in s.PRIMITIVE_MODELS if kind in get_args(model.model_fields['kind'].annotation))
        assert field in model.model_fields and label


def test_weekday_labels_use_the_existing_monday_zero_contract():
    assert s.WEEKDAY_SHORT_LABELS == ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
    assert all(s.SeriesDay(weekday=day, start_time='08:00', end_time='12:00').weekday == day
               for day in range(len(s.WEEKDAY_SHORT_LABELS)))


def test_findings_keep_actionable_wording_and_technical_codes():
    assert s.FINDING_MESSAGES['COVERAGE_GAP'] == 'Fewer people than the minimum — this session still needs someone'
    assert 'Configuration › Duty types' in s.FINDING_MESSAGES['TYPE_INACTIVE']
    assert all(code == code.upper() and message for code, message in s.FINDING_MESSAGES.items())


def test_exported_series_default_survives_save_reload_and_expansion(both):
    from .test_bulk_series_counting import series
    h = both
    row = series(h)  # Omits usual_assignees_fixed: the command must apply the canonical default.
    assert row['definition']['usual_assignees_fixed'] is s.USUAL_ASSIGNEES_FIXED_DEFAULT
    saved = next(item for item in h.get('admin', '/api/catalogues')['recurring_series'] if item['id'] == row['id'])
    assert saved['definition'] == row['definition']
    workspace = h.command('coordinator', 'workspace.create', {'name': 'Synthetic exported series default',
        'starts_on': '2026-10-04', 'ends_on': '2026-10-11', 'series_ids': [row['id']]})['workspace']
    shift, = h.detail(workspace)['shifts']
    assert shift['series_snapshot']['definition']['usual_assignees_fixed'] is s.USUAL_ASSIGNEES_FIXED_DEFAULT
    assert {person['person_id'] for person in shift['participants']} == set(row['definition']['default_person_ids'])


def test_exported_scoring_keys_survive_settings_reload_and_rota_validation(both):
    h = both
    factory = h.get('admin', '/api/policies/capabilities')['generation_defaults']['scoring']
    assert set(factory['weights']) == set(s.SCORE_WEIGHT_LABELS)
    assert set(factory['strengths']) == set(s.STRENGTH_LABELS)
    weights = {key: index + 1 for index, key in enumerate(s.SCORE_WEIGHT_LABELS)}
    strengths = {key: index + 1 for index, key in enumerate(s.STRENGTH_LABELS)}
    before = h.get('admin', '/api/me')
    org, department = before['organisation'], before['department']
    saved = h.command('admin', 'department.settings', {
        'department_name': department['name'], 'department_revision': department['revision'],
        'organisation_name': org['name'], 'timezone': org['timezone'], 'week_start': org['week_start'],
        'generation_defaults': {'scoring': {'weights': weights, 'strengths': strengths}},
        'reason': 'Synthetic exported scoring controls'}, department['id'], org['revision'])
    reloaded = h.get('admin', '/api/me')['department']['generation_defaults']['scoring']
    assert reloaded == saved['department']['generation_defaults']['scoring']
    assert reloaded['weights'] == weights and reloaded['strengths'] == strengths
    workspace = h.shift(h.workspace())['workspace']
    settings = h.detail(workspace)['workspace']['validation']['score']['settings']
    assert settings['weights'] == weights and settings['strengths'] == strengths
