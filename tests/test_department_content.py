import pytest
from packages.contracts.schemas import DepartmentContentInput


def test_staff_sees_current_department_content_but_cannot_edit(h):
    for name, end in [('Current notice', None), ('Expired notice', '2021-01-01')]:
        h.configure('department_content', {'name': name, 'description': 'Departmental information',
            'starts_on': '2020-01-01', 'ends_on': end})
    items = h.get('staff', '/api/content')['items']
    assert [r['name'] for r in items] == ['Current notice']
    h.configure('department_content', {'name': 'Unauthorised', 'starts_on': '2020-01-01'}, role='staff', status=403)
    with pytest.raises(ValueError, match='HTTP'):
        DepartmentContentInput(name='Unsafe link', starts_on='2020-01-01', kind='link', url='javascript:alert(1)')
