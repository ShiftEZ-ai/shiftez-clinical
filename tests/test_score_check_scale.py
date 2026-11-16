"""The legacy CSV transport boundary must not split a department session."""
import pytest

pytestmark = pytest.mark.usefixtures('score_check_enabled')
import base64
import csv
from datetime import date, timedelta
from io import StringIO

from .test_score_check import internal_upload


def test_large_import_keeps_shared_sessions_together_across_transport_batches(h):
    catalogues = h.get('coordinator', '/api/catalogues')
    names = {p['id']: p['display_name'] for p in catalogues['people']}
    duty = next(t['name'] for t in catalogues['assignment_types'] if t['id'] == h.seed['type'])
    text = StringIO()
    writer = csv.writer(text)
    writer.writerow(['Person', 'Date', 'Duty code', 'Clinic', 'Start', 'End'])
    for person in h.seed['people']:
        for index in range(400):
            day = date(2026, 10, 4) + timedelta(days=index // 5)
            hour = 8 + index % 5
            writer.writerow([names[person], day.isoformat(), duty, 'Imported shared clinic',
                             f'{hour:02}:00', f'{hour + 1:02}:00'])
    result = internal_upload(h, {'files': [{'file_name': 'Shared sessions.csv',
        'content_base64': base64.b64encode(text.getvalue().encode()).decode()}]})
    batch = result['batch']
    clinic = next(g for g in batch['gaps'] if g['entity_kind'] == 'clinic')
    batch = h.command('coordinator', 'score_check.decide', {'batch_id': batch['id'], 'gap_id': clinic['id'],
        'action': 'match', 'target_id': h.seed['services'][0], 'location_id': h.seed['locations'][0]},
        revision=batch['revision'])['batch']
    for item in batch['gaps']:
        if item['kind'] == 'missing' and item.get('decision') is None:
            batch = h.command('coordinator', 'score_check.decide', {'batch_id': batch['id'],
                'gap_id': item['id'], 'action': 'acknowledge'}, revision=batch['revision'])['batch']
    saved = h.command('coordinator', 'score_check.save', {'batch_id': batch['id']}, revision=batch['revision'])
    detail = h.detail(saved['workspace'])
    assert len(detail['shifts']) == 400
    assert sum(len(row['participants']) for row in detail['shifts']) == 1200
    assert all({p['person_id'] for p in row['participants']} == set(h.seed['people']) for row in detail['shifts'])
    assert len({(row['start'], row['end']) for row in detail['shifts']}) == 400
