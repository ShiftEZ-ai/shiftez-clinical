"""Bulk configuration changes: one checked, audited change across many records, all or nothing."""


def rows(h, kind, ids):
    return [r for r in h.get('admin', '/api/catalogues')[kind] if r['id'] in ids]


def test_bulk_switch_off_colour_and_tag_records(h):
    ids = h.seed['services'][:2]
    items = [{'id': r['id'], 'revision': r['revision']} for r in rows(h, 'services', ids)]
    h.command('admin', 'catalogue.bulk_update', {'catalogue': 'services', 'items': items, 'active': False, 'colour': 'violet', 'reason': 'Clinics paused'})
    after = rows(h, 'services', ids)
    assert all(not r['active'] and r['colour'] == 'violet' and r['revision'] == 2 for r in after)
    # A record edited since it was selected is refused and nothing is written.
    stale = [{'id': r['id'], 'revision': 1} for r in after]
    h.command('admin', 'catalogue.bulk_update', {'catalogue': 'services', 'items': stale, 'active': True, 'reason': 'Reopen'}, status=409)
    assert all(not r['active'] for r in rows(h, 'services', ids))
    people = [{'id': p['id'], 'revision': p['revision']} for p in rows(h, 'people', h.seed['people'][:3])]
    h.command('coordinator', 'catalogue.bulk_update', {'catalogue': 'people', 'items': people, 'add_tags': ['Night team'], 'reason': 'New team'})
    assert all('Night team' in p['tags'] for p in rows(h, 'people', h.seed['people'][:3]))


def test_bulk_follows_the_single_edit_permissions(h):
    items = [{'id': r['id'], 'revision': r['revision']} for r in rows(h, 'services', h.seed['services'][:1])]
    h.command('coordinator', 'catalogue.bulk_update', {'catalogue': 'services', 'items': items, 'active': False, 'reason': 'Not allowed'}, status=403)
    h.command('admin', 'catalogue.bulk_update', {'catalogue': 'services', 'items': items, 'reason': 'Nothing'}, status=422)


def test_inline_edit_sets_fields_on_one_or_many_records(h):
    one = rows(h, 'people', h.seed['people'][:1])[0]
    h.command('coordinator', 'catalogue.bulk_update', {'catalogue': 'people', 'items': [{'id': one['id'], 'revision': one['revision']}],
              'set': {'short_code': 'ZQX'}, 'reason': 'Inline edit'})
    assert rows(h, 'people', [one['id']])[0]['short_code'] == 'ZQX'
    many = [{'id': s['id'], 'revision': s['revision']} for s in rows(h, 'services', h.seed['services'][:2])]
    h.command('admin', 'catalogue.bulk_update', {'catalogue': 'services', 'items': many, 'set': {'description': 'Shared clinic'}, 'reason': 'Inline edit'})
    assert all(s['description'] == 'Shared clinic' for s in rows(h, 'services', h.seed['services'][:2]))
    # Values are validated by the same schema as a single edit; unknown or protected fields are refused.
    fresh = rows(h, 'people', h.seed['people'][:1])[0]
    ref = [{'id': fresh['id'], 'revision': fresh['revision']}]
    h.command('coordinator', 'catalogue.bulk_update', {'catalogue': 'people', 'items': ref, 'set': {'short_code': 'x' * 40}, 'reason': 'Too long'}, status=422)
    h.command('coordinator', 'catalogue.bulk_update', {'catalogue': 'people', 'items': ref, 'set': {'department_id': 'x'}, 'reason': 'Protected'}, status=422)


def test_edits_return_previous_values_that_undo_them(h):
    """Undo in the app re-saves previous_value through the same checked, audited edit."""
    before = rows(h, 'services', h.seed['services'][:1])[0]
    result = h.command('admin', 'catalogue.bulk_update', {'catalogue': 'services', 'items': [{'id': before['id'], 'revision': before['revision']}],
                       'set': {'description': 'Changed by mistake'}, 'reason': 'Inline edit'})
    previous, saved = result['previous_values'][0], result['records'][0]
    assert previous['description'] == before['description'] and 'revision' not in previous
    undone = h.command('admin', 'catalogue.update', {'catalogue': 'services', 'value': previous, 'reason': 'Undo'}, saved['id'], saved['revision'])
    assert undone['record']['description'] == before['description']
    assert undone['previous_value']['description'] == 'Changed by mistake'
    # An undo based on an old revision is refused, never overwriting a later edit.
    h.command('admin', 'catalogue.update', {'catalogue': 'services', 'value': previous, 'reason': 'Undo'}, saved['id'], saved['revision'], status=409)


def test_services_and_locations_take_an_optional_short_code_by_the_duty_type_rule(h):
    """A service or location may carry a short code for dense grids and print (A-Z and 0-9, up to 6), set one at a time
    or in bulk; a save that leaves it out keeps it, and Undo restores the blank."""
    service = rows(h, 'services', h.seed['services'][:1])[0]
    value = {key: service[key] for key in ('name', 'starts_on')}
    saved = h.configure('services', {**value, 'short_code': 'CL1'}, service['id'], service['revision'])['record']
    assert saved['short_code'] == 'CL1'
    kept = h.configure('services', {**value, 'description': 'Code kept'}, saved['id'], saved['revision'])['record']
    assert kept['short_code'] == 'CL1'
    for bad in ('cl1', 'C L', 'ABCDEFG'):
        h.configure('services', {**value, 'short_code': bad}, kept['id'], kept['revision'], status=422)
    location_type = h.get('admin', '/api/catalogues')['location_types'][0]['id']
    places = [h.configure('locations', {'name': f'Synthetic coded place {i}', 'starts_on': '2020-01-01',
                                        'location_type_id': location_type})['record'] for i in (1, 2)]
    assert [p['short_code'] for p in places] == ['', '']
    items = [{'id': p['id'], 'revision': p['revision']} for p in places]
    h.command('admin', 'catalogue.bulk_update', {'catalogue': 'locations', 'items': items, 'set': {'short_code': 'w2'},
                                                  'reason': 'Codes for print'}, status=422)
    result = h.command('admin', 'catalogue.bulk_update', {'catalogue': 'locations', 'items': items, 'set': {'short_code': 'W2'},
                                                           'reason': 'Codes for print'})
    assert sorted(r['short_code'] for r in result['records']) == ['W2', 'W2']
    assert [p['short_code'] for p in result['previous_values']] == ['', '']
    first = result['records'][0]
    undone = h.command('admin', 'catalogue.update', {'catalogue': 'locations', 'value': result['previous_values'][0],
                                                     'reason': 'Undo'}, first['id'], first['revision'])['record']
    assert undone['short_code'] == ''
