"""Saved table views: private to their owner, one list each, bounded and revision-checked."""


def definition(**extra):
    return {'layout': 'cards', 'hidden': ['skills'], 'filters': {'rank': ['Consultant']}, 'group': 'rank', 'sort': ['name', 'asc'], **extra}


def test_saved_views_are_private_and_per_list(h):
    saved = h.command('staff', 'table_view.create', {'table_key': 'people', 'name': 'My consultants', 'definition': definition()})['view']
    assert h.get('staff', '/api/table-views?table=people')['views'][0]['definition']['layout'] == 'cards'
    assert h.get('staff', '/api/table-views?table=directory')['views'] == []
    assert h.get('coordinator', '/api/table-views?table=people')['views'] == []
    h.command('coordinator', 'table_view.delete', {}, target=saved['id'], revision=1, status=404)
    updated = h.command('staff', 'table_view.update', {'table_key': 'people', 'name': 'Consultants', 'definition': definition(layout='table')}, target=saved['id'], revision=1)['view']
    assert updated['revision'] == 2 and updated['definition']['layout'] == 'table'
    h.command('staff', 'table_view.update', {'table_key': 'people', 'name': 'Stale', 'definition': definition()}, target=saved['id'], revision=1, status=409)
    h.command('staff', 'table_view.delete', {}, target=saved['id'], revision=2)
    assert h.get('staff', '/api/table-views?table=people')['views'] == []


def test_saved_view_input_is_bounded(h):
    h.command('staff', 'table_view.create', {'table_key': 'People!', 'name': 'x', 'definition': definition()}, status=422)
    h.command('staff', 'table_view.create', {'table_key': 'people', 'name': 'x', 'definition': definition(layout='chart')}, status=422)
    h.get('staff', '/api/table-views?table=../etc', status=422)


def test_rules_views_round_trip_modes_expansion_and_shared_filters(h):
    for mode in ('rules', 'attention', 'acknowledged'):
        value = {'table_key': 'rule-tree', 'name': 'Rules ' + mode, 'definition': {
            'q': 'Duties', 'filters': {'kind': ['Ending soon']}, 'group': 'kind',
            'hidden': ['reach'], 'wrap': ['where'], 'order': ['valid', 'where'],
            'rules': {'mode': mode, 'open': ['duties_per_week']}}}
        created = h.command('coordinator', 'table_view.create', value)['view']
        stored = next(v for v in h.get('coordinator', '/api/table-views?table=rule-tree')['views'] if v['id'] == created['id'])
        assert all(stored['definition'][key] == expected for key, expected in value['definition'].items())
        assert h.get('staff', '/api/table-views?table=rule-tree')['views'] == []
    old = h.command('coordinator', 'table_view.create', {'table_key': 'rule-tree', 'name': 'Shared fields only',
        'definition': {'q': 'Duties', 'group': 'family'}})['view']
    assert old['definition']['rules'] is None and old['definition']['q'] == 'Duties'


def test_rules_view_definition_rejects_unbounded_and_unknown_state(h):
    for rules in ({'mode': 'unsupported'}, {'open': ['duties_per_week'] * 1001}, {'open': ['bad key!']}, {'unexpected': True}):
        h.command('coordinator', 'table_view.create', {'table_key': 'rule-tree', 'name': 'Invalid',
            'definition': {'rules': rules}}, status=422)
    for extra in ({'mode': 'attention'}, {'open': []}):
        h.command('coordinator', 'table_view.create', {'table_key': 'rule-tree', 'name': 'Invalid',
            'definition': extra}, status=422)
    assert h.get('coordinator', '/api/table-views?table=rule-tree')['views'] == []


def test_planner_views_keep_view_filters_and_display(h):
    planner = {'mode': 'coverage', 'search': 'clinic', 'location_id': h.seed['locations'][0], 'group_by': 'type', 'colour_by': 'person', 'compact': True, 'only_open': True}
    view = h.command('coordinator', 'table_view.create', {'table_key': 'planner', 'name': 'Open places', 'definition': {'planner': planner}})['view']
    assert view['definition']['planner']['mode'] == 'coverage' and view['definition']['planner']['compact'] is True
    import uuid
    h.command('coordinator', 'table_view.create', {'table_key': 'planner', 'name': 'Unknown place', 'definition': {'planner': {'location_id': str(uuid.uuid4())}}}, status=404)


def test_shared_planner_view_round_trips_duty_time_then_by_and_collapsed_groups(h):
    planner = {'mode': 'week', 'rows': 'time', 'columns': 'day', 'group_by': 'none', 'subgroup': 'service',
               'collapsed': ['time:08:00–12:00', 'service:synthetic']}
    value = {'table_key': 'planner', 'name': 'Duty times', 'scope': 'department',
             'definition': {'group': 'time', 'then': 'service', 'planner': planner}}
    created = h.command('coordinator', 'table_view.create', value)['view']

    def read():
        return next(v for v in h.get('staff', '/api/table-views?table=planner')['views'] if v['id'] == created['id'])

    saved = read()
    assert saved['scope'] == 'department' and saved['revision'] == 1
    assert (saved['definition']['group'], saved['definition']['then']) == ('time', 'service')
    assert all(saved['definition']['planner'][key] == val for key, val in planner.items())
    planner = {**planner, 'rows': 'service', 'subgroup': 'time', 'collapsed': ['service:changed', 'time:13:00–17:00']}
    value = {**value, 'name': 'Services then duty times',
             'definition': {'group': 'service', 'then': 'time', 'planner': planner}}
    h.command('coordinator', 'table_view.update', value, target=created['id'], revision=1)
    updated = read()
    assert updated['revision'] == 2 and updated['name'] == value['name']
    assert (updated['definition']['group'], updated['definition']['then']) == ('service', 'time')
    assert all(updated['definition']['planner'][key] == val for key, val in planner.items())


def test_views_keep_then_by_wrapped_columns_and_column_order(h):
    """The column menu and the Group menu save with the view: a second grouping, wrapped columns and column order."""
    view = h.command('staff', 'table_view.create', {'table_key': 'people', 'name': 'Wrapped', 'definition': definition(
        then='status', wrap=['skills', 'tags'], order=['tags', 'rank'])})['view']
    saved = h.get('staff', '/api/table-views?table=people')['views'][0]['definition']
    assert (saved['then'], saved['wrap'], saved['order']) == ('status', ['skills', 'tags'], ['tags', 'rank'])
    # Views saved before these existed read as the defaults.
    plain = h.command('staff', 'table_view.create', {'table_key': 'people', 'name': 'Plain', 'definition': definition()})['view']
    assert (plain['definition']['then'], plain['definition']['wrap'], plain['definition']['order']) == ('', [], [])
    for bad in ({'wrap': ['x'] * 31}, {'order': ['x'] * 31}, {'then': 'bad key!'}):
        h.command('staff', 'table_view.update', {'table_key': 'people', 'name': 'Wrapped', 'definition': definition(**bad)},
                  target=view['id'], revision=1, status=422)



def test_planner_view_display_settings_round_trip_and_validate(h):
    planner = {'mode': 'timeline', 'range': 'custom', 'days': 10, 'density': 'comfortable', 'weekdays': [6, 0, 1, 2, 3], 'week_start': 6,
               'time_window': {'start': '07:00', 'end': '19:00'},
               'properties': [{'key': 'label', 'on': True, 'form': 'text'}, {'key': 'lead', 'on': False, 'form': 'icon'}],
               'label': {'parts': ['short', 'level', 'pager'], 'separator': 'comma', 'honorific': 'none'}}
    view = h.command('staff', 'table_view.create', {'table_key': 'planner', 'name': 'Timeline', 'definition': {'planner': planner}})['view']
    saved = h.get('staff', '/api/table-views?table=planner')['views'][0]['definition']['planner']
    assert saved['label'] == planner['label'] and saved['weekdays'] == [6, 0, 1, 2, 3] and saved['days'] == 10 and view['revision'] == 1
    for bad in ({'weekdays': [1, 1]}, {'weekdays': [7]}, {'weekdays': []}, {'days': 0}, {'days': 63}, {'range': 'fortnight'},
                {'time_window': {'start': '19:00', 'end': '07:00'}}, {'properties': [{'key': 'lead'}, {'key': 'lead'}]},
                {'properties': [{'key': 'salary'}]}, {'label': {'parts': []}}, {'label': {'parts': ['short', 'short']}}, {'density': 'tiny'}):
        h.command('staff', 'table_view.create', {'table_key': 'planner', 'name': 'Bad', 'definition': {'planner': bad}}, status=422)


def test_planner_views_saved_before_display_settings_still_load(h):
    """A definition stored before density, days, label and properties existed reads back unchanged and validates."""
    from packages.contracts import schemas as s
    old = {'planner': {'schema_version': 1, 'mode': 'month', 'search': '', 'location_id': None, 'assignment_type_id': None, 'person_id': None,
                       'group_by': 'service', 'sort_by': 'time', 'colour_by': 'type', 'compact': True, 'only_open': False, 'only_issues': False}}
    planner = s.TableViewDefinition.model_validate(old).planner
    assert planner.compact is True and planner.density is None and planner.label is None and planner.weekdays is None and planner.range is None
    h.command('staff', 'table_view.create', {'table_key': 'planner', 'name': 'Old month', 'definition': old})


def test_person_pager_is_kept_and_listed(h):
    person = h.command('admin', 'catalogue.create', {'catalogue': 'people', 'value': {'display_name': 'Dr Pager Test', 'pager': '4471'}, 'reason': 'New starter'})['record']
    assert person['pager'] == '4471'
    h.command('admin', 'catalogue.create', {'catalogue': 'people', 'value': {'display_name': 'Dr Long Pager', 'pager': 'x' * 41}, 'reason': 'New starter'}, status=422)
    assert any(p.get('pager') == '4471' for p in h.get('staff', '/api/directory')['people'])  # absent for a person with no pager contact

def test_proposed_rule_can_be_declined_not_active_one(h):
    rule = h.command('coordinator', 'policy.create', {'name': 'Imported weekly minimum', 'owner': 'Dept', 'source': 'Import', 'starts_on': '2026-10-01',
        'definition': {'severity': 'mandatory', 'exception_allowed': False, 'scope': {}, 'rule': {'kind': 'min_rest', 'hours': 10}}})['record']
    h.command('staff', 'policy.decline', {'reason': 'Not ours'}, rule['id'], rule['revision'], status=403)
    done = h.command('coordinator', 'policy.decline', {'reason': 'Duplicate import'}, rule['id'], rule['revision'])['record']
    assert done['status'] == 'retired' and not done['active']
    h.command('coordinator', 'policy.decline', {'reason': 'Again'}, done['id'], done['revision'], status=409)
