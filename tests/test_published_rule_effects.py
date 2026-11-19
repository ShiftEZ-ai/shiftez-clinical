"""Published rule previews use the same operational inputs as activation checks."""
import pytest

from apps.api import models as m
from apps.api.db import transaction
from .test_contextual_feasibility import current_window
from .test_rule_catalogue_api import line, propose


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_published_line_effect_uses_current_tightened_duty_type(request, database):
    h = request.getfixturevalue(database)
    start, end = current_window(h)
    typ = h.configure('assignment_types', {'name': 'Published effect duty', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 2}]}})['record']
    workspace = h.shift(h.workspace('Published effect duty', str(start), str(end)),
                        start=f'{start}T08:00:00+03:00', end=f'{start}T12:00:00+03:00', typ=typ['id'])['workspace']
    published = h.publish(workspace)['workspace']
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.AssignmentType, typ['id'])
        row.definition = {**row.definition, 'requirements': [{'minimum': 2, 'maximum': 2}]}
        row.revision += 1
        db.get(m.Organisation, h.seed['organisation_id']).input_revision += 1
    policy = propose(h, 'duties_per_week', [line(numbers={'max': {'value': 5}})])['record']
    h.command('approver', 'policy.activate', {'reason': 'Seed the published effect preview'}, policy['id'], policy['revision'])
    current = h.get('coordinator', '/api/rules/catalogue')['entries']
    current = next(entry for entry in current if entry['id'] == 'duties_per_week')['rule']
    edited = {**current['definition']['rule']['lines'][0], 'numbers': {'max': {'value': 4}}}
    response = h.client('coordinator').post('/api/rules/catalogue/duties_per_week/effect', json={
        'expected_policy_id': current['id'], 'expected_revision': current['revision'],
        'line': edited, 'catalogue_version': current['definition']['rule'].get('catalogue_version', 2)})
    assert response.status_code == 200, response.text
    effect = response.json()['effects'][edited['id']][0]
    assert any(f['code'] == 'COVERAGE_GAP' for f in effect['before']['validation']['findings'])
    assert effect['workspace_id'] == published['id']
