"""Session saves share scheduling sources across independently shaped snapshot views."""
import json

import pytest
from sqlalchemy import delete

from apps.api import models as m, snapshots
from apps.api.db import transaction
from apps.api.services.planning_preparation import bind
from packages.contracts import schemas as s
from packages.domain.serialization import json_default
from tests.test_backend_reads_scale import read_department
from tests.test_backend_scale import post
from tests.test_leave_types import absence


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_session_edit_snapshot_views_match_separate_reads(h):
    h.policy({'kind': 'balance_goal', 'metric': 'overtime_duties', 'aim': 'equal_share',
              'period': 'rolling', 'period_days': 84}, {'person_ids': [h.seed['people'][0]]}, severity='advisory')
    earlier = h.workspace(start='2026-08-09', end='2026-08-16')
    earlier = h.shift(earlier, start='2026-08-09T08:00:00+03:00', end='2026-08-09T12:00:00+03:00')['workspace']
    h.publish(earlier)
    historical = absence(h, start='2026-08-10T08:00:00+03:00', end='2026-08-10T12:00:00+03:00')
    workspace = h.shift(h.workspace())['workspace']
    shift = h.detail(workspace)['shifts'][0]
    plan = {'seed': m.uid(), 'at': m.now(), 'kind': 'shift.update', 'checks': []}

    def serialized(data):
        return json.dumps([data, getattr(data, 'read_quality', None)], separators=(',', ':'),
                          default=json_default).encode()

    with transaction(h.app.state.engine, write=True) as db:
        bind(db, plan)
        row = db.get(m.Workspace, workspace['id'])

        def check_view(**options):
            prepared = snapshots.domain_snapshot(db, row, **options)
            external = db.info.pop('_prepared_external_schedule', None)
            try:
                separate = snapshots._domain_snapshot(db, row, **options)
            finally:
                db.info.pop('_prepared_external_schedule', None)
                if external is not None:
                    db.info['_prepared_external_schedule'] = external
            assert serialized(prepared) == serialized(separate)
            return prepared

        placement = check_view(own_shifts=[], include_fairness=False, include_manifest=False)
        assert not placement['focus_ids'] and 'input_manifest' not in placement
        placement['types'].clear()
        # Change the only local session's catalogue references between the placement and admission views.
        db.get(m.Shift, shift['id']).location_id = h.seed['locations'][1]
        admission = check_view(include_fairness=False, include_objectives=False)
        assert admission['focus_ids'] == [shift['id']]
        assert admission['shifts'][0]['location_id'] == h.seed['locations'][1]
        assert admission['wishes'] == [] and 'fairness_history_start' not in admission
        assert historical['id'] not in {row['id'] for row in admission['rotation_leave_availability']}
        admission['people'].clear()
        row.revision += 1
        row.content_revision += 1
        checked = check_view(mandatory_rules=True, current_mandatory=False)
        assert checked['workspace']['content_revision'] == row.content_revision
        assert checked['fingerprint'] != admission['fingerprint']
        assert historical['id'] in {row['id'] for row in checked['rotation_leave_availability']}


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_session_save_acquires_common_snapshot_sources_once(scale_harness, monkeypatch, record_property, backend):
    h = scale_harness(backend)
    workspace = read_department(h)
    shift = next(row for row in h.detail(workspace)['shifts'] if len(row['participants']) == 3)
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(delete(m.Assignment).where(m.Assignment.shift_id == shift['id'],
            m.Assignment.person_id == shift['participants'][-1]['person_id']))
    post(h, 'workspace.validate', {'reason': 'Check the current rota'}, workspace['id'], workspace['revision'])
    payload = {key: shift[key] for key in s.ShiftInput.model_fields}
    payload.update(workspace_id=workspace['id'], shift_id=shift['id'], participants=[
        {key: person[key] for key in s.ParticipantInput.model_fields if key in person}
        for person in shift['participants']])

    acquisitions, overlays = [], []
    original_rows, original_tags = snapshots.rows_many, snapshots.with_tags

    def count_rows(db, sources, **options):
        if 'event_occurrences' in sources:
            acquisitions.append(bool(db.connection().info.get('rota_write')))
        return original_rows(db, sources, **options)

    def count_tags(data):
        overlays.append(data['workspace']['id'])
        return original_tags(data)

    monkeypatch.setattr(snapshots, 'rows_many', count_rows)
    monkeypatch.setattr(snapshots, 'with_tags', count_tags)
    saved = post(h, 'shift.update', payload, revision=workspace['revision'])
    assert saved['shift_id'] == shift['id']
    record_property('common_snapshot_acquisitions', len(acquisitions))
    record_property('common_snapshot_overlays', len(overlays))
    assert acquisitions == [False]
    assert overlays == [workspace['id']]
