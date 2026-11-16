def test_work_directory_roundtrip_and_staff_boundary(h):
    harness = h
    record = harness.configure('people', {'display_name': 'Fictional Directory Person',
        'title': 'Section coordinator', 'work_phone': '12345', 'work_extension': '67890',
        'staff_number': 'TEST-001', 'directory_section': 'Example section', 'directory_order': 2})['record']
    people = harness.get('coordinator', '/api/catalogues')['people']
    assert next(p for p in people if p['id'] == record['id'])['work_phone'] == '12345'
    harness.get('staff', '/api/catalogues', status=403)
    # A directory entry does not create clinical authority or an application account.
    catalogue = harness.get('coordinator', '/api/catalogues')
    assert not any(r['person_id'] == record['id'] for r in catalogue['eligibilities'])
    assert not any(r['person_id'] == record['id'] for r in catalogue['grants'])
