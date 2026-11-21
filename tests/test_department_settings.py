from .clock_support import freeze_clock
import copy
from datetime import date
import uuid
import pytest
from sqlalchemy import select
from apps.api import models as m
from apps.api.db import transaction


@pytest.mark.postgres
@pytest.mark.parametrize('kind', ['exchange', 'give'])
def test_open_offer_setting_defaults_on_is_audited_undoable_and_preserves_inflight_consent(pg, kind):
    from datetime import timedelta
    from .test_staff_swaps import source_pair
    from .governance_support import transition
    h = pg
    with transaction(h.app.state.engine, write=True) as db:  # prepared by hand, without a settings audit row
        department = db.get(m.Department, h.seed['department_id'])
        department.swap_auto_prepare = department.swap_auto_publish = False
    pid, first, second, _ = source_pair(h, third=True)
    offer = {'kind': kind, 'publication_id': pid, 'assignment_id': first,
        'expires_at': (m.now() + timedelta(hours=1)).isoformat(), 'reason': 'Synthetic open offer'}
    row = h.command('staff', 'swap.offer', offer)['swap']
    me = h.get('admin', '/api/me')
    org, dept = me['organisation'], me['department']
    assert dept['swap_open_offers'] is True
    settings = {'department_name': dept['name'], 'department_revision': dept['revision'],
        'organisation_name': org['name'], 'timezone': org['timezone'], 'week_start': org['week_start'],
        'reason': 'Choose whether new swap offers can be open', 'swap_open_offers': False}
    for invalid in (0, 1, 'false', []):
        h.command('admin', 'department.settings', {**settings, 'swap_open_offers': invalid},
                  dept['id'], org['revision'], status=422)
    h.command('staff', 'department.settings', settings, dept['id'], org['revision'], status=403)
    saved = h.command('admin', 'department.settings', settings, dept['id'], org['revision'])
    assert saved['department']['swap_open_offers'] is False and saved['previous']['swap_open_offers'] is True
    assert saved['organisation'] == org
    assert h.get('staff', '/api/me')['department']['swap_open_offers'] is False
    for omitted in ({}, {'swap_open_offers': None}):
        kept = h.command('admin', 'department.settings', {**{k: v for k, v in settings.items() if k != 'swap_open_offers'},
            'department_revision': saved['department']['revision'], **omitted}, dept['id'], org['revision'])
        assert kept['department'] == saved['department']
    h.command('admin', 'department.settings', settings, dept['id'], org['revision'], status=409)
    assert h.command('staff', 'swap.offer', offer, status=409)['error']['code'] == 'SWAP_OPEN_DISABLED'
    named = h.command('staff', 'swap.offer', {**offer, 'recipient_person_id': h.seed['people'][2]})['swap']
    transition(h, named, 'withdraw')
    from .test_open_swaps import audience_ready
    audience_ready(h)
    assert h.get('staff_two', '/api/swaps/' + row['id'])['swap']['id'] == row['id']
    row = transition(h, row, 'accept' if kind == 'give' else 'counteroffer', 'staff_two', {
        'reason': 'Reply to an existing open offer', **({'assignment_id': second} if kind == 'exchange' else {})})['swap']
    row = transition(h, row, 'confirm')['swap']
    assert transition(h, row, 'prepare', 'coordinator')['swap']['status'] == 'reviewing'
    with transaction(h.app.state.engine) as db:
        histories = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.kind == 'department_settings')))
        assert len(histories) == 1 and histories[0].snapshot['swap_open_offers'] is False
    restored = h.command('admin', 'department.settings', {**saved['previous'], 'reason': 'Undo open offer setting'},
                         dept['id'], org['revision'])
    assert restored['department']['swap_open_offers'] is True


@pytest.mark.postgres
@pytest.mark.parametrize('allowed', [[], ['exchange'], ['give'], ['give', 'exchange']])
def test_swap_kinds_are_validated_audited_preserved_and_undoable(pg, allowed):
    h = pg
    me = h.get('admin', '/api/me')
    org, dept = me['organisation'], me['department']
    assert dept['swap_kinds_allowed'] == ['exchange', 'give']
    payload = {'department_name': dept['name'], 'department_revision': dept['revision'],
        'organisation_name': org['name'], 'timezone': org['timezone'], 'week_start': org['week_start'],
        'reason': 'Choose the permitted swap kinds'}
    for invalid in (['unknown'], ['give', 'give'], ['exchange', 'give', 'exchange'], 'give'):
        h.command('admin', 'department.settings', {**payload, 'swap_kinds_allowed': invalid},
                  dept['id'], org['revision'], status=422)
    h.command('staff', 'department.settings', {**payload, 'swap_kinds_allowed': allowed},
              dept['id'], org['revision'], status=403)
    saved = h.command('admin', 'department.settings', {**payload, 'swap_kinds_allowed': allowed},
                      dept['id'], org['revision'])
    expected = [kind for kind in ('exchange', 'give') if kind in allowed]
    assert saved['department']['swap_kinds_allowed'] == expected
    assert saved['organisation'] == org  # Invitations are not scheduling inputs.
    assert saved['previous']['swap_kinds_allowed'] == dept['swap_kinds_allowed']
    assert h.get('staff', '/api/me')['department']['swap_kinds_allowed'] == expected
    for omitted in ({}, {'swap_kinds_allowed': None}):
        kept = h.command('admin', 'department.settings', {**payload,
            'department_revision': saved['department']['revision'], **omitted}, dept['id'], org['revision'])
        assert kept['department'] == saved['department']
    with transaction(h.app.state.engine) as db:
        histories = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.kind == 'department_settings')))
        assert len(histories) == (expected != dept['swap_kinds_allowed'])
    restored = h.command('admin', 'department.settings', {**saved['previous'], 'reason': 'Undo swap kind selection'},
                         dept['id'], org['revision'])
    assert restored['department']['swap_kinds_allowed'] == dept['swap_kinds_allowed']


@pytest.mark.postgres
@pytest.mark.parametrize('kind', ['exchange', 'give'])
def test_swap_kind_setting_blocks_new_offers_and_keeps_existing_consent(pg, kind):
    h = pg
    from datetime import timedelta
    from .test_staff_swaps import source_pair
    from .governance_support import swap_automation, transition
    swap_automation(h, prepare=False, publish=False)  # this test prepares by hand
    pid, first, second, _ = source_pair(h)
    payload = {'kind': kind, 'publication_id': pid, 'assignment_id': first,
        'recipient_person_id': h.seed['people'][1], 'expires_at': (m.now() + timedelta(hours=1)).isoformat(),
        'reason': 'Synthetic in-flight swap'}
    row = h.command('staff', 'swap.offer', payload)['swap']
    me = h.get('admin', '/api/me')
    org, dept = me['organisation'], me['department']
    h.command('admin', 'department.settings', {'department_name': dept['name'], 'department_revision': dept['revision'],
        'organisation_name': org['name'], 'timezone': org['timezone'], 'week_start': org['week_start'],
        'swap_kinds_allowed': [], 'reason': 'Disable new invitations'}, dept['id'], org['revision'])
    assert h.command('staff', 'swap.offer', payload, status=409)['error']['code'] == 'SWAP_KIND_DISABLED'
    reply = {'reason': 'Consent to the existing invitation'}
    if kind == 'exchange':
        reply['assignment_id'] = second
    row = transition(h, row, 'accept' if kind == 'give' else 'counteroffer', 'staff_two', reply)['swap']
    row = transition(h, row, 'confirm')['swap']
    prepared = transition(h, row, 'prepare', 'coordinator')
    publication = h.publish(prepared['workspace'])
    assert h.get('staff', '/api/swaps/' + row['id'])['swap']['result_publication_id'] == publication['publication_id']


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_settings_are_audited_stale_safe_and_preserve_publications(request, database):
    h = request.getfixturevalue(database)
    publication = h.publish(h.shift(h.workspace())['workspace'])
    w = h.shift(h.workspace('Next week', start='2026-10-11', end='2026-10-18'),
        start='2026-10-11T08:00:00+03:00', end='2026-10-11T12:00:00+03:00')['workspace']
    w = h.lifecycle(h.lifecycle(w, 'submit')['workspace'], 'approve', 'approver')['workspace']
    before = h.get('admin', '/api/me')
    org, dept = before['organisation'], before['department']
    payload = {'department_name': 'Configurable department', 'department_revision': dept['revision'],
        'organisation_name': 'Configurable hospital', 'timezone': 'UTC', 'week_start': 0,
        'reason': 'Synthetic institutional settings acceptance'}
    with transaction(h.app.state.engine) as db:
        issued = copy.deepcopy(db.get(m.Publication, publication['publication_id']).snapshot)
    for role in ('staff', 'coordinator', 'approver'):
        h.command(role, 'department.settings', payload, dept['id'], org['revision'], status=403)
    h.command('admin', 'department.settings', {**payload, 'timezone': 'Unknown/Invalid'}, dept['id'], org['revision'], status=422)
    h.command('admin', 'department.settings', payload, str(uuid.uuid4()), org['revision'], status=404)
    saved = h.command('admin', 'department.settings', payload, dept['id'], org['revision'])
    assert saved['organisation']['input_revision'] == org['input_revision'] + 1
    assert saved['organisation']['timezone'] == 'UTC' and saved['organisation']['week_start'] == 0
    assert h.lifecycle(w, 'publish', status=409)['error']['code'] == 'STALE_APPROVAL'
    h.command('admin', 'department.settings', payload, dept['id'], org['revision'], status=409)
    payload['department_revision'] = saved['department']['revision']
    unchanged = h.command('admin', 'department.settings', payload, dept['id'], saved['organisation']['revision'])
    assert unchanged['organisation'] == saved['organisation'] and unchanged['department'] == saved['department']
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Publication, publication['publication_id']).snapshot == issued
        histories = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.kind.in_(
            ['organisation_settings', 'department_settings']))))
        assert len(histories) == 2 and all(r.reason == payload['reason'] for r in histories)


def test_department_admin_cannot_change_another_departments_organisation_settings(h):
    me = h.get('admin', '/api/me')
    org, dept = me['organisation'], me['department']
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.Department(organisation_id=org['id'], name='Other administered department'))
    payload = {'department_name': 'Renamed own department', 'department_revision': dept['revision'],
        'organisation_name': org['name'], 'timezone': org['timezone'], 'week_start': org['week_start'],
        'reason': 'Synthetic scoped settings acceptance'}
    h.command('admin', 'department.settings', {**payload, 'week_start': (org['week_start']+1)%7},
        dept['id'], org['revision'], status=403)
    assert h.command('admin', 'department.settings', payload, dept['id'], org['revision'])['department']['name'] == payload['department_name']


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_swap_settings_validate_persist_audit_and_undo_without_invalidating_rotas(request, database):
    h = request.getfixturevalue(database)
    me = h.get('admin', '/api/me')
    org, dept = me['organisation'], me['department']
    assert (dept['swap_offer_window_days'], dept['swap_open_offer_cap']) == (7, 25)
    payload = {'department_name': dept['name'], 'department_revision': dept['revision'],
               'organisation_name': org['name'], 'timezone': org['timezone'], 'week_start': org['week_start'],
               'reason': 'Tune departmental swap settings'}
    # Pin auto-schedule defaults before approval: the full Undo form must not turn inherited mode into explicit mode.
    baseline = h.command('admin', 'department.settings', {**payload, 'generation_defaults': {'mode': 'fill_gaps'}},
                         dept['id'], org['revision'])
    org, dept = baseline['organisation'], baseline['department']
    payload['department_revision'] = dept['revision']
    w = h.shift(h.workspace())['workspace']
    w = h.lifecycle(h.lifecycle(w, 'submit')['workspace'], 'approve', 'approver')['workspace']
    org = h.get('admin', '/api/me')['organisation']
    for field in ('swap_offer_window_days', 'swap_open_offer_cap'):
        for bad in (0, -1, True, 1.5, '7', 2_147_483_648):
            h.command('admin', 'department.settings', {**payload, field: bad}, dept['id'], org['revision'], status=422)
    changed = {**payload, 'swap_offer_window_days': 2_147_483_647, 'swap_open_offer_cap': 2_147_483_647}
    h.command('coordinator', 'department.settings', changed, dept['id'], org['revision'], status=403)
    saved = h.command('admin', 'department.settings', changed, dept['id'], org['revision'])
    assert saved['organisation'] == org
    for field, default in (('swap_offer_window_days', 7), ('swap_open_offer_cap', 25)):
        assert saved['department'][field] == 2_147_483_647 and saved['previous'][field] == default
    assert h.get('staff', '/api/me')['department'] == saved['department']
    with transaction(h.app.state.engine) as db:
        audit = db.scalar(select(m.DefinitionHistory).where(m.DefinitionHistory.kind == 'department_settings',
            m.DefinitionHistory.object_revision == saved['department']['revision']))
        assert audit.reason == payload['reason'] and audit.snapshot['swap_open_offer_cap'] == 2_147_483_647
    h.command('admin', 'department.settings', changed, dept['id'], org['revision'], status=409)
    # Both omitted and explicit null fields preserve saved settings on older frontend submissions.
    kept = h.command('admin', 'department.settings', {**payload, 'department_revision': saved['department']['revision'],
        'swap_offer_window_days': None}, dept['id'], org['revision'])
    assert kept['department'] == saved['department']
    restored = h.command('admin', 'department.settings', {**saved['previous'], 'reason': 'Undo'}, dept['id'], org['revision'])
    assert (restored['department']['swap_offer_window_days'], restored['department']['swap_open_offer_cap']) == (7, 25)
    assert restored['organisation'] == org
    assert h.lifecycle(w, 'publish')['workspace']['status'] == 'published'


def test_scoring_settings_persist_score_every_check_and_undo(h):
    me = h.get('admin', '/api/me')
    org, dept = me['organisation'], me['department']
    payload = {'department_name': dept['name'], 'department_revision': dept['revision'], 'organisation_name': org['name'],
               'timezone': org['timezone'], 'week_start': org['week_start'], 'reason': 'Tune the schedule score'}
    zero = {'weights': {'coverage': 0, 'rules': 0, 'fairness': 0, 'preferences': 0, 'stability': 0}}
    h.command('admin', 'department.settings', {**payload, 'generation_defaults': {'scoring': zero}}, dept['id'], org['revision'], status=422)
    scoring = {'weights': {'coverage': 1, 'rules': 0, 'fairness': 0, 'preferences': 0, 'stability': 0},
               'strengths': {'low': 1, 'medium': 2, 'high': 5, 'critical': 20}, 'tolerance': {'targets': 5}, 'wish_budget_points': 8}
    saved = h.command('admin', 'department.settings', {**payload, 'generation_defaults': {'scoring': scoring}}, dept['id'], org['revision'])
    stored = saved['department']['generation_defaults']['scoring']
    assert stored['weights']['coverage'] == 1 and stored['strengths']['critical'] == 20
    assert 'tolerance' not in stored
    assert saved['previous']['generation_defaults']['scoring']['weights']['coverage'] == 35  # what Undo puts back
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.DefinitionHistory).where(m.DefinitionHistory.kind == 'department_settings')) is not None
    # A draft with one of two required places open: only coverage is weighted now, so the score is 50.
    typ = h.configure('assignment_types', {'name': 'Pair clinic', 'starts_on': '2020-01-01',
                                           'definition': {'requirements': [{'minimum': 2}]}})['record']
    w = h.shift(h.workspace(), typ=typ['id'])['workspace']
    score = h.detail(w)['workspace']['validation']['score']
    assert score['score'] == 50 and score['components']['coverage']['value'] == 0.5 and score['settings']['weights']['coverage'] == 1
    assert round(sum(line['points'] for line in score['why']), 1) == 50
    listed = next(r for r in h.get('coordinator', '/api/workspaces?limit=50&offset=0')['workspaces'] if r['id'] == w['id'])
    assert listed['health']['score'] == 50
    undo = saved['previous']
    back = h.command('admin', 'department.settings', {**undo, 'reason': 'Undo'}, dept['id'], saved['organisation']['revision'])
    assert back['department']['generation_defaults']['scoring']['weights']['coverage'] == 35


def test_compare_and_publication_exports_carry_the_score(h):
    first = h.shift(h.workspace())['workspace']
    second = h.shift(h.workspace('Other draft'), people=[])['workspace']
    compared = h.get('coordinator', f"/api/workspaces/{first['id']}/compare?other_id={second['id']}&limit=50&offset=0")
    assert compared['scores']['before']['score'] > compared['scores']['after']['score']
    assert compared['scores']['before']['fresh'] and 'coverage' in compared['scores']['after']['components']
    published = h.publish(first)
    report = h.get('coordinator', f"/api/reports/{published['publication_id']}")
    assert report['schedule_score'] == compared['scores']['before']['score']
    assert 'schedule_score' not in h.get('staff', f"/api/reports/{published['publication_id']}")
    page = h.client('coordinator').get(f"/api/reports/{published['publication_id']}?format=print&layout=week")
    from packages.web_assets import asset_url
    assert f'href="{asset_url("print.css")}"' in page.text
    assert h.client('coordinator').get(asset_url('print.css')).status_code == 200
    assert f"Schedule score at publication: {report['schedule_score']:g} / 100" in page.text


def test_weekend_days_are_one_audited_department_setting_that_leaves_rotas_alone(h):
    """Weekend days (Monday 0 … Sunday 6) default to Friday and Saturday, are saved sorted and audited, come back in
    /api/me and in Undo's previous values. With a selected operating calendar they only change shading."""
    me = h.get('admin', '/api/me')
    org, dept = me['organisation'], me['department']
    assert dept['weekend_days'] == [4, 5]
    assert dept['operating_calendar_id'] is not None
    w = h.shift(h.workspace('Weekend check', start='2026-10-11', end='2026-10-18'),
        start='2026-10-11T08:00:00+03:00', end='2026-10-11T12:00:00+03:00')['workspace']
    w = h.lifecycle(h.lifecycle(w, 'submit')['workspace'], 'approve', 'approver')['workspace']
    org = h.get('admin', '/api/me')['organisation']
    payload = {'department_name': dept['name'], 'department_revision': dept['revision'], 'organisation_name': org['name'],
               'timezone': org['timezone'], 'week_start': org['week_start'], 'reason': 'Weekend is Saturday and Sunday'}
    for bad in ([5, 5], [7], [-1]):
        h.command('admin', 'department.settings', {**payload, 'weekend_days': bad}, dept['id'], org['revision'], status=422)
    h.command('coordinator', 'department.settings', {**payload, 'weekend_days': [6, 5]}, dept['id'], org['revision'], status=403)
    saved = h.command('admin', 'department.settings', {**payload, 'weekend_days': [6, 5]}, dept['id'], org['revision'])
    assert saved['department']['weekend_days'] == [5, 6] and saved['previous']['weekend_days'] == [4, 5]
    assert saved['department']['revision'] == dept['revision'] + 1 and saved['organisation'] == org
    assert h.get('coordinator', '/api/me')['department']['weekend_days'] == [5, 6]
    with transaction(h.app.state.engine) as db:
        audit = db.scalar(select(m.DefinitionHistory).where(m.DefinitionHistory.kind == 'department_settings'))
        assert audit is not None and audit.reason == payload['reason']
    # Unset keeps it; the approved rota still publishes (no rota input changed).
    kept = h.command('admin', 'department.settings', {**payload, 'department_revision': saved['department']['revision']},
                     dept['id'], org['revision'])
    assert kept['department']['weekend_days'] == [5, 6]
    assert h.lifecycle(w, 'publish')['workspace']['status'] == 'published'


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_swap_approval_settings_preserve_omissions_clear_undo_and_audit_without_staling_rotas(request, database):
    h = request.getfixturevalue(database)
    route = h.command('admin', 'review_route.create', {'name': 'Swap review', 'purpose': 'swap',
        'starts_on': '2020-01-01', 'definition': {'steps': [{'key': 'clinical', 'label': 'Clinical review'}]},
        'reason': 'Independent swap review configuration'})['route']
    route = h.command('approver', 'review_route.activate', {'reason': 'Independent activation'},
                      route['id'], route['revision'])['route']
    w = h.lifecycle(h.lifecycle(h.shift(h.workspace())['workspace'], 'submit')['workspace'],
                    'approve', 'approver')['workspace']
    me = h.get('admin', '/api/me')
    org, dept = me['organisation'], me['department']
    defaults = {'swap_review_route_id': None, 'swap_auto_prepare': True, 'swap_auto_publish': True}
    assert {key: dept[key] for key in defaults} == defaults
    payload = {'department_name': dept['name'], 'department_revision': dept['revision'],
               'organisation_name': org['name'], 'timezone': org['timezone'], 'week_start': org['week_start'],
               'reason': 'Department chooses the swap approval workflow'}
    chosen = {'swap_review_route_id': route['id'], 'swap_auto_prepare': False, 'swap_auto_publish': False}
    saved = h.command('admin', 'department.settings', {**payload, **chosen}, dept['id'], org['revision'])
    assert {key: saved['department'][key] for key in chosen} == chosen
    assert {key: saved['previous'][key] for key in defaults} == defaults
    assert saved['department']['revision'] == dept['revision'] + 1 and saved['organisation'] == org
    current = h.get('coordinator', '/api/me')['department']
    assert {key: current[key] for key in chosen} == chosen
    # Older forms omit these fields, so saving unrelated settings cannot reset the department's choices.
    kept = h.command('admin', 'department.settings', {**payload, 'department_revision': saved['department']['revision']},
                     dept['id'], org['revision'])
    assert kept['department'] == saved['department']
    cleared = h.command('admin', 'department.settings', {**payload, 'department_revision': kept['department']['revision'],
                        'swap_review_route_id': None}, dept['id'], org['revision'])
    assert cleared['department']['swap_review_route_id'] is None
    assert not cleared['department']['swap_auto_prepare'] and not cleared['department']['swap_auto_publish']
    restored_route = h.command('admin', 'department.settings', {**cleared['previous'], 'reason': 'Undo clearing the swap route'},
                               dept['id'], org['revision'])
    assert {key: restored_route['department'][key] for key in chosen} == chosen
    back = h.command('admin', 'department.settings', {**saved['previous'], 'department_revision': restored_route['department']['revision'],
                     'reason': 'Undo swap approval settings'}, dept['id'], org['revision'])
    assert {key: back['department'][key] for key in defaults} == defaults
    assert back['organisation'] == org
    with transaction(h.app.state.engine) as db:
        histories = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.kind == 'department_settings')
                                   .order_by(m.DefinitionHistory.object_revision)))
        assert len(histories) == 4
        assert {key: histories[0].snapshot[key] for key in chosen} == chosen
        assert histories[0].reason == payload['reason']
    assert h.lifecycle(w, 'publish')['workspace']['status'] == 'published'


def test_swap_approval_settings_validate_boolean_values_and_scoped_active_routes(h):
    from .governance_support import route
    from .test_swap_approval_settings import swap_route
    active = swap_route(h)
    routine = route(h)
    proposed = h.command('admin', 'review_route.create', {'name': 'Proposed swap route', 'purpose': 'swap',
        'starts_on': '2020-01-01', 'definition': active['definition'], 'reason': 'Synthetic swap proposal'})['route']
    me = h.get('admin', '/api/me')
    org, dept = me['organisation'], me['department']
    payload = {'department_name': dept['name'], 'department_revision': dept['revision'],
               'organisation_name': org['name'], 'timezone': org['timezone'], 'week_start': org['week_start'],
               'reason': 'Validate the configured swap approval workflow'}
    for field in ('swap_auto_prepare', 'swap_auto_publish'):
        for invalid in ('false', 0, None):
            h.command('admin', 'department.settings', {**payload, field: invalid}, dept['id'], org['revision'], status=422)
    for role in ('staff', 'coordinator', 'approver'):
        h.command(role, 'department.settings', {**payload, 'swap_auto_prepare': False}, dept['id'], org['revision'], status=403)
    h.command('admin', 'department.settings', {**payload, 'swap_review_route_id': 'invalid'},
              dept['id'], org['revision'], status=422)
    h.command('admin', 'department.settings', {**payload, 'swap_review_route_id': str(uuid.uuid4())},
              dept['id'], org['revision'], status=404)
    assert h.command('admin', 'department.settings', {**payload, 'swap_review_route_id': proposed['id']},
                     dept['id'], org['revision'], status=409)['error']['code'] == 'ROUTE_STATE'
    with transaction(h.app.state.engine, write=True) as db:
        foreign = m.Department(organisation_id=org['id'], name='Another department')
        db.add(foreign); db.flush()
        other_route = m.ReviewRoute(department_id=foreign.id, name='Other department route', starts_on=date(2020, 1, 1),
                                   definition=active['definition'], status='active', created_by=h.seed['identities']['admin'])
        db.add(other_route); db.flush()
        foreign_route_id = other_route.id
    org = h.get('admin', '/api/me')['organisation']
    h.command('admin', 'department.settings', {**payload, 'swap_review_route_id': foreign_route_id},
              dept['id'], org['revision'], status=404)
    refused = h.command('admin', 'department.settings', {**payload, 'swap_review_route_id': routine['id']},
                        dept['id'], org['revision'], status=409)
    assert refused['error']['code'] == 'ROUTE_PURPOSE'
    assert h.get('admin', '/api/me')['department'] == dept
    saved = h.command('admin', 'department.settings', {**payload, 'swap_review_route_id': active['id']}, dept['id'], org['revision'])
    assert saved['department']['swap_review_route_id'] == active['id'] and saved['organisation'] == org


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_selected_swap_review_route_cannot_retire_until_cleared_and_settings_undo_still_works(request, database):
    from .test_swap_approval_settings import swap_route
    h = request.getfixturevalue(database)
    active = swap_route(h)
    me = h.get('admin', '/api/me')
    org, dept = me['organisation'], me['department']
    payload = {'department_name': dept['name'], 'department_revision': dept['revision'],
               'organisation_name': org['name'], 'timezone': org['timezone'], 'week_start': org['week_start'],
               'reason': 'Choose the department swap review route'}
    chosen = h.command('admin', 'department.settings', {**payload, 'swap_review_route_id': active['id']},
                       dept['id'], org['revision'])
    refused = h.command('approver', 'review_route.retire', {'reason': 'Retire the configured route'},
                        active['id'], active['revision'], status=409)
    assert refused['error']['code'] == 'ROUTE_IN_USE'
    assert h.get('admin', '/api/me')['department'] == chosen['department']
    assert next(r for r in h.get('admin', '/api/review-routes')['routes'] if r['id'] == active['id']) == active
    with transaction(h.app.state.engine) as db:
        assert not db.scalar(select(m.AuditEvent).where(m.AuditEvent.target_id == active['id'],
                                                       m.AuditEvent.action == 'review_route.retire'))
    saved = h.command('admin', 'department.settings', {**payload, 'department_revision': chosen['department']['revision'],
                       'weekend_days': [5, 6]}, dept['id'], org['revision'])
    assert saved['previous']['swap_review_route_id'] == active['id']
    back = h.command('admin', 'department.settings', {**saved['previous'], 'reason': 'Undo unrelated weekend change'},
                      dept['id'], org['revision'])
    assert back['department']['weekend_days'] == dept['weekend_days']
    assert back['department']['swap_review_route_id'] == active['id']
    cleared = h.command('admin', 'department.settings', {**payload, 'department_revision': back['department']['revision'],
                         'swap_review_route_id': None}, dept['id'], org['revision'])
    retired = h.command('approver', 'review_route.retire', {'reason': 'Retire after clearing department selection'},
                        active['id'], active['revision'])['route']
    assert retired['status'] == 'retired'
    rejected = h.command('admin', 'department.settings', {**payload, 'department_revision': cleared['department']['revision'],
                          'swap_review_route_id': active['id']}, dept['id'], org['revision'], status=409)
    assert rejected['error']['code'] == 'ROUTE_STATE'


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_swap_submission_refuses_legacy_selected_rota_route_until_swap_route_is_chosen(request, database, monkeypatch):
    from apps.api import auth
    from packages.domain.time import aware
    from .governance_support import route, transition
    from .test_staff_swaps import accepted
    from .test_swap_approval_settings import settings, swap_route
    h = request.getfixturevalue(database)
    now = aware('2026-09-18T10:00:00+00:00')
    freeze_clock(monkeypatch, lambda: now)
    monkeypatch.setattr(auth, 'now', lambda: now)
    routine = route(h)
    row, _ = accepted(h)
    workspace = transition(h, row, 'prepare', 'coordinator')['workspace']
    # Model a saved selection from before route-purpose validation existed.
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed['department_id']).swap_review_route_id = routine['id']
    refused = h.lifecycle(workspace, 'submit', status=409)
    assert refused['error']['code'] == 'ROUTE_PURPOSE'
    assert h.detail(workspace)['workspace']['status'] == 'draft'
    assert h.get('admin', '/api/me')['department']['swap_review_route_id'] == routine['id']
    dedicated = swap_route(h)
    settings(h, swap_review_route_id=dedicated['id'])
    submitted = h.lifecycle(workspace, 'submit')['workspace']
    assert submitted['status'] == 'submitted' and submitted['review_snapshot']['route_id'] == dedicated['id']
