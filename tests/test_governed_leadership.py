"""Governed named leadership: safety intersection, cutover and historical preservation."""
import copy
from datetime import date
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError
from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from packages.contracts import schemas as s
from packages.domain import leadership
from packages.domain.conditions import active_rules
from packages.domain.validation import evaluate
from packages.domain.cp_sat import generate
from packages.domain.generation import generate as greedy

pytestmark = pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)

START, END = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'


def line(typ, people=(), minimum=1, maximum=1, continuity='whole_duty', **kw):
    return {'id': str(uuid.uuid4()), 'where': {'match': 'all', 'conditions': [
        {'field': 'assignment_type_ids', 'op': 'any', 'values': [typ]}]},
        'who': {'match': 'all', 'conditions': [{'field': 'person_ids', 'op': 'any', 'values': list(people)}]} if people else None,
        'numbers': {'min': {'value': minimum, 'level': 'mandatory'},
                    **({'max': {'value': maximum, 'level': 'mandatory'}} if maximum is not None else {})},
        'settings': {'team_lead_requirement': {'continuity': continuity}}, **kw}


def proposal(h, lines, **kw):
    return h.command('coordinator', 'rule.propose', {'catalogue_id': 'team_leads', 'catalogue_version': 1,
        'lines': lines, 'reason': 'Fictional independently reviewed leadership', **kw})['record']


def activate(h, p):
    return h.command('approver', 'policy.activate', {'reason': 'Independent fictional review'}, p['id'], p['revision'])['records'][0]


@pytest.fixture(autouse=True)
def refresh_leadership_baseline(h, monkeypatch):
    """These pre-baseline fixtures add reviewed inputs after draft creation; refresh before each read."""
    original = h.snapshot
    def snapshot(workspace):
        with transaction(h.app.state.engine) as db:
            status = db.get(m.Workspace, workspace['id']).status
        if status != 'draft':
            return original(workspace)
        preview = h.command('coordinator', 'workspace.baseline.refresh.preview', {}, workspace['id'], workspace['revision'])
        if preview['changes']:
            refreshed = h.command('coordinator', 'workspace.baseline.refresh.apply', {
                'expected_baseline_hash': preview['baseline']['snapshot_hash'],
                'decisions': {source: 'use_department' for source in preview['conflicts']},
                'reason': 'Refresh the leadership fixture baseline after reviewed input changes'},
                workspace['id'], workspace['revision'])['workspace']
            workspace.update(refreshed)
        return original(workspace)
    monkeypatch.setattr(h, 'snapshot', snapshot)


def current_snapshot(h, workspace):
    from apps.api.snapshots import domain_snapshot
    with transaction(h.app.state.engine) as db:
        return domain_snapshot(db, db.get(m.Workspace, workspace['id']), current_scheduling=True)


def prepared(h, people=()):
    typ = h.configure('assignment_types', {'name': 'Governed duty', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 2, 'maximum': 3}], 'allow_shared_services': True,
                       'shared_duty_reason': 'Single shared fictional team'}})['record']
    w = h.shift(h.workspace(), typ=typ['id'], people=list(people))['workspace']
    return typ, w


def errors(data):
    return [f for f in evaluate(data)['findings'] if f['severity'] == 'error' and f['code'].startswith('LEAD')]


def test_governed_leader_obligation_and_intersection(h):
    a,b,c = h.seed['people']
    typ,w = prepared(h, [a,b,c])
    active = activate(h, proposal(h, [line(typ['id'], [a,b]), line(typ['id'], [b,c])]))
    data = h.snapshot(w)
    assert len(errors(data)) == 2  # zero flags; both obligations survive
    shift = data['shifts'][0]
    next(p for p in shift['participants'] if p['person_id'] == b)['team_lead'] = True
    assert not errors(data)
    next(p for p in shift['participants'] if p['person_id'] == a)['team_lead'] = True
    assert {f['code'] for f in errors(data)} == {'LEAD_GAP', 'LEAD_NOT_QUALIFIED'}
    assert all(f['rule_id'] == active['id'] for f in errors(data))
    assert all((f.get('detail') or {}).get('lead_sources') for f in errors(data))
    # A qualification-empty person set cannot erase the duty obligation.
    empty = copy.deepcopy(data)
    empty['shifts'][0]['participants'] = []
    empty['people'] = {}
    assert len(errors(empty)) == 2


def test_solver_and_greedy_name_governed_leader(h):
    a,b,c = h.seed['people']
    typ,w = prepared(h)
    activate(h, proposal(h, [line(typ['id'], [c])]))
    data = h.snapshot(w)
    solved = generate(copy.deepcopy(data), 10, workers=1)
    assert solved['status'] == 'candidate_feasible', solved
    assert [r['participant']['person_id'] for r in solved['additions'] if r['participant']['team_lead']] == [c]
    result = greedy(data, 20)
    assert result['validation']['valid'], result['validation']['findings']
    assert [r['participant']['person_id'] for r in result['additions'] if r['participant'].get('team_lead')] == [c]


def legacy_type(h, lead=None):
    """Historical data fixture, deliberately bypassing today's authoring boundary."""
    typ = h.configure('assignment_types', {'name': 'Frozen historical duty', 'starts_on': '2020-01-01',
                        'definition': {'requirements': [{'minimum': 1, 'maximum': 3}]}})['record']
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.AssignmentType, typ['id'])
        row.definition = {**row.definition, 'lead': s.TeamLead.model_validate(lead or {}).model_dump(mode='json')}
        row.revision += 1
        typ = record(row)
    return typ


def test_type_authoring_cannot_bypass_rules_or_undo(h):
    typ = legacy_type(h)
    base = {key: typ[key] for key in ('name','starts_on','definition')}
    for change in [{'minimum': 0}, {'category_ids': [str(uuid.uuid4())]}]:
        value = copy.deepcopy(base)
        value['definition']['lead'].update(change)
        refusal = h.configure('assignment_types', value, typ['id'], typ['revision'], status=422)
        assert refusal['error']['code'] == 'USE_RULE_CATALOGUE'
    value = copy.deepcopy(base)
    del value['definition']['lead']
    h.configure('assignment_types', value, typ['id'], typ['revision'], status=422)
    base['name'] = 'Unrelated rename'
    assert h.configure('assignment_types', base, typ['id'], typ['revision'])['record']['definition']['lead'] == typ['definition']['lead']


def test_conversion_is_equivalent_independent_and_never_resurrects(h):
    from apps.api.services.leadership import conversion_plan
    typ = legacy_type(h)
    a = h.seed['people'][0]
    w = h.shift(h.workspace(), typ=typ['id'], people=[{'person_id': a, 'start': START, 'end': END, 'team_lead': True}])['workspace']
    before = current_snapshot(h, w)
    actor = SimpleNamespace(department_id=typ['department_id'])
    with transaction(h.app.state.engine) as db:
        plan = conversion_plan(db, actor, '2026-10-04')
    p = proposal(h, plan['lines'], legacy_lead_conversion=plan['legacy_lead_conversion'])
    # Proposal never claims ownership or changes evaluation.
    assert current_snapshot(h, w)['type_lead_ownership'] == []
    current = activate(h, p)
    after = current_snapshot(h, w)
    assert len(after['type_lead_ownership']) == 1
    assert not errors(before) and not errors(after)
    from packages.domain.rule_migration_proof import leadership_proof
    proof = leadership_proof(before, after)
    assert proof['identical'], {key: value for key, value in proof.items() if isinstance(value, dict) and not value.get('equal')}
    missing = [copy.deepcopy(data) for data in (before, after)]
    for data in missing:
        data['shifts'][0]['participants'][0]['team_lead'] = False
    gap_proof = leadership_proof(*missing, model=False)
    assert all(gap_proof[key]['equal'] for key in ('role_semantics', 'findings', 'quota', 'fairness', 'score')), gap_proof

    assert leadership.summary(after, after['shifts'][0])['continuity'] == 'continuous'
    receipts = copy.deepcopy(after['type_lead_ownership'])
    # Even with no effective rule, the obsolete type cannot regain ownership.
    after['policies'] = [row for row in after['policies'] if row['id'] != current['id']]
    assert leadership.summary(after, after['shifts'][0]) is None
    assert after['type_lead_ownership'] == receipts
    with pytest.raises(DBAPIError, match="immutable leadership evidence"):
        with transaction(h.app.state.engine, write=True) as db:
            db.execute(update(m.TypeLeadOwnership).values(source_lead_hash='rewritten'))


def test_conversion_discovery_tracks_only_unowned_legacy_sources(h):
    def shown():
        page = h.get('coordinator', '/api/rules/catalogue')
        entry = next(row for row in page['entries'] if row['id'] == 'team_leads')
        detail = h.get('coordinator', '/api/rules/catalogue/team_leads/detail')['entry']
        assert entry['legacy_leads'] == detail['legacy_leads']
        assert all('legacy_leads' not in row for row in page['entries'] if row['id'] != 'team_leads')
        return entry['legacy_leads']

    assert shown() == 0
    legacy_type(h)
    with transaction(h.app.state.engine) as db:
        revision = db.scalar(select(m.Organisation.input_revision))
    assert shown() == 1
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.Organisation.input_revision)) == revision
    preview = h.client('coordinator').post('/api/rules/catalogue/team_leads/check',
        json={'legacy_effective_on': '2026-10-04'})
    assert preview.status_code == 200, preview.text
    plan = preview.json()
    waiting = proposal(h, plan['lines'], legacy_lead_conversion=plan['legacy_lead_conversion'])
    assert shown() == 0
    current = activate(h, waiting)
    assert shown() == 0
    h.command('approver', 'policy.retire', {'reason': 'Retire governed conversion'}, current['id'], current['revision'])
    assert shown() == 0


@pytest.mark.parametrize('conflict', ['primary_key', 'department_type'])
def test_sqlite_replace_cannot_rewrite_approved_ownership(h, conflict):
    if h.app.state.engine.dialect.name != 'sqlite':
        pytest.skip('SQLite conflict replacement semantics')
    from apps.api.services.leadership import conversion_plan
    typ = legacy_type(h)
    with transaction(h.app.state.engine) as db:
        plan = conversion_plan(db, SimpleNamespace(department_id=typ['department_id']), '2026-10-04')
    activate(h, proposal(h, plan['lines'], legacy_lead_conversion=plan['legacy_lead_conversion']))
    with transaction(h.app.state.engine) as db:
        table = m.TypeLeadOwnership.__table__
        original = dict(db.execute(select(table)).mappings().one())
        assert db.connection().exec_driver_sql('PRAGMA recursive_triggers').scalar_one() == 0
    changed = {**original, 'source_lead_hash': 'c' * 64}
    if conflict == 'primary_key':
        other = h.configure('assignment_types', {'name': 'Other provenance source',
            'starts_on': '2020-01-01', 'definition': {}})['record']
        with transaction(h.app.state.engine) as db:
            history_id = db.scalar(select(m.DefinitionHistory.id).where(m.DefinitionHistory.object_id == other['id'],
                m.DefinitionHistory.object_revision == other['revision']))
            assert history_id is not None
        changed.update(assignment_type_id=other['id'], source_history_id=history_id, source_type_revision=other['revision'])
    else:
        changed['id'] = str(uuid.uuid4())
    with pytest.raises(DBAPIError, match='invalid leadership provenance'):
        with transaction(h.app.state.engine, write=True) as db:
            db.execute(table.insert().prefix_with('OR REPLACE').values(**changed))
    with transaction(h.app.state.engine) as db:
        assert dict(db.execute(select(table)).mappings().one()) == original


def test_new_whole_duty_requires_same_people_legacy_continuous_allows_handover(h):
    a,b,c = h.seed['people']
    typ,w = prepared(h,[a,b])
    activate(h, proposal(h,[line(typ['id'])]))
    data = h.snapshot(w)
    ps = data['shifts'][0]['participants']
    ps[0].update(end='2026-10-04T10:00:00+03:00',team_lead=True)
    ps[1].update(start='2026-10-04T10:00:00+03:00',team_lead=True)
    assert errors(data)
    for policy in data['policies']:
        if policy['definition']['rule'].get('id') == 'team_leads':
            policy['definition']['rule']['lines'][0]['settings']['team_lead_requirement']['continuity'] = 'continuous'
    assert not errors(data)


def test_cancel_before_resume_decode(h, monkeypatch):
    from packages.domain import solver_resume
    _,w = prepared(h)
    called=[]
    monkeypatch.setattr(solver_resume, 'decompress_state', lambda state, **kw: called.append('decode'))
    result = generate(h.snapshot(w), 10, cancelled=lambda: True, resume_state={'encoding':'invalid'})
    assert result['status'] == 'cancelled' and called == []


def context_actor(h, role='coordinator'):
    from apps.api.auth import Actor
    with transaction(h.app.state.engine) as db:
        department = db.get(m.Department, h.seed['department_id'])
        return Actor(h.seed['identities'][role], department.id, department.organisation_id, 'Fictional reviewer',
                     frozenset({role}), None, str(uuid.uuid4()))


def test_read_only_context_includes_assigned_people_and_is_repeatable(h):
    from apps.api.services.leadership import lead_context
    from apps.api.errors import Problem
    a,b,c = h.seed['people']
    typ,w = prepared(h,[a,b])
    activate(h, proposal(h,[line(typ['id'],[b])]))
    shift = h.snapshot(w)['shifts'][0]
    proposed = {k: shift[k] for k in s.ShiftInput.model_fields if k in shift}
    proposed['participants'] = [{k: p[k] for k in s.ParticipantInput.model_fields if k in p}
                                for p in shift['participants']]
    value = s.LeadContextInput(shift=proposed,
                               shift_id=shift['id'], person_ids=[a,b])
    actor = context_actor(h)
    with transaction(h.app.state.engine) as db:
        before = db.scalar(select(m.Organisation.input_revision))
        result = lead_context(db, actor, w['id'], value)
        again = lead_context(db, actor, w['id'], value)
        assert result == again
        assert {p.person_id:p.can_lead for p in result.people} == {a:False,b:True}
        assert all(seg.enabled and seg.minimum == seg.maximum == 1 for seg in result.segments)
        assert db.scalar(select(m.Organisation.input_revision)) == before
        with pytest.raises(Problem):
            lead_context(db, context_actor(h,'staff'), w['id'], value)
    path = f"/api/workspaces/{w['id']}/lead-context"
    response = h.client('coordinator').post(path, json=value.model_dump(mode='json'))
    assert response.status_code == 200, response.text
    assert s.LeadContext.model_validate(response.json()) == result
    assert h.client('staff').post(path, json=value.model_dump(mode='json')).status_code == 403
    foreign = value.model_dump(mode='json') | {'person_ids': [str(uuid.uuid4())]}
    assert h.client('coordinator').post(path, json=foreign).status_code == 404


def test_optional_and_null_only_leadership_and_conflicting_safety_lines(h):
    a,b,c = h.seed['people']
    typ,w = prepared(h,[a,b])
    first = line(typ['id'], [a], minimum=0, maximum=None)
    active = activate(h,proposal(h,[first]))
    data=h.snapshot(w)
    assert not errors(data)
    shift=data['shifts'][0]
    next(p for p in shift['participants'] if p['person_id']==b)['team_lead']=True
    assert {f['code'] for f in errors(data)} == {'LEAD_NOT_QUALIFIED'}
    policy=next(p for p in data['policies'] if p['id']==active['id'])
    policy['definition']['rule']['lines'][0]['numbers']['min']['value']=None
    assert leadership.summary(data,shift) is None
    # Individually valid lines can contradict. All constraints remain; no priority/Who winner.
    policy['definition']['rule']['lines']=[line(typ['id'],minimum=2,maximum=None),line(typ['id'],minimum=0,maximum=1)]
    assert leadership.summary(data,shift)['minimum']==2
    assert leadership.summary(data,shift)['maximum']==1
    assert errors(data)


def test_leadership_dates_clock_cutover_and_strict_qualification(h):
    a,b,c=h.seed['people']
    typ,w=prepared(h,[a,b])
    rules=[line(typ['id'],[a],start_time='08:00',end_time='10:00'),
           line(typ['id'],[b],start_time='10:00',end_time='12:00')]
    activate(h,proposal(h,rules))
    data=h.snapshot(w);shift=data['shifts'][0]
    rows=leadership.segments(data,shift,START,END)
    assert len(rows)==2 and all(len(r)==1 for _,_,r in rows)
    assert not leadership.qualified(data,a,shift,START,END)
    assert not leadership.qualified(data,b,shift,START,END)
    # New whole-duty min/max binds the same person across both requirement intervals.
    for p in shift['participants']:p['team_lead']=True
    assert errors(data)


def test_shared_duty_counts_one_lead_turn_and_intersects_service_requirements(h):
    from packages.domain.fairness import contribution
    a,b,c=h.seed['people'];typ,w=prepared(h,[a,b])
    # Add a second service to the same existing Shift; one role flag remains factual.
    p=activate(h,proposal(h,[line(typ['id'],[a])]))
    data=h.snapshot(w);shift=data['shifts'][0]
    shift['service_ids']=h.seed['services'][:2]
    lead=next(p for p in shift['participants'] if p['person_id']==a);lead['team_lead']=True
    assert contribution('lead_turns',shift,lead)==1
    assert not errors(data)
    policy=next(row for row in data['policies'] if row['id']==p['id'])
    second=line(typ['id'],[b]);second['where']['conditions'].append(
        {'field':'service_ids','op':'any','values':[shift['service_ids'][1]]})
    policy['definition']['rule']['lines'].append(second)
    assert {f['code'] for f in errors(data)}=={'LEAD_NOT_QUALIFIED','LEAD_GAP'}


def test_concurrent_duties_share_one_named_leader(h):
    a, b, _ = h.seed['people']
    typ = h.configure('assignment_types', {'name': 'Concurrent leadership duty', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 2}], 'allow_shared_services': True,
                       'shared_duty_reason': 'Synthetic concurrent leadership'}})['record']
    w = h.workspace()
    w = h.shift(w, typ=typ['id'], people=[a])['workspace']
    w = h.shift(w, service=1, typ=typ['id'], people=[b])['workspace']
    requirement = line(typ['id'], [a, b])
    requirement['settings']['team_lead_requirement']['count_scope'] = 'concurrent_duties'
    activate(h, proposal(h, [requirement]))
    data = h.snapshot(w)
    first = next(shift for shift in data['shifts'] if any(p['person_id'] == a for p in shift['participants']))
    second = next(shift for shift in data['shifts'] if any(p['person_id'] == b for p in shift['participants']))
    zero = [f for f in errors(data) if f['code'] == 'LEAD_GAP']
    assert len(zero) == 1 and zero[0]['detail']['available'] == 0
    next(p for p in first['participants'] if p['person_id'] == a)['team_lead'] = True
    assert not errors(data)
    next(p for p in second['participants'] if p['person_id'] == b)['team_lead'] = True
    findings = errors(data)
    assert any(f['code'] == 'LEAD_GAP' and f['detail']['scheduled'] == 2 for f in findings)
    duplicate = copy.deepcopy(data)
    duplicate_second = next(shift for shift in duplicate['shifts'] if shift['id'] == second['id'])
    duplicate_second['participants'][0]['person_id'] = a
    from packages.domain.fairness import ledger_facts
    lead_rows = [row for row in ledger_facts(duplicate) if row[2] == 'lead_turns']
    assert sum(row[4] for row in lead_rows) == 1


def test_concurrent_duties_partial_overlap_counts_each_interval(h):
    a, b, _ = h.seed['people']
    typ = h.configure('assignment_types', {'name': 'Partial concurrent leadership duty', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 2}], 'allow_shared_services': True,
                       'shared_duty_reason': 'Synthetic concurrent leadership'}})['record']
    w = h.workspace()
    w = h.shift(w, typ=typ['id'], people=[a])['workspace']
    w = h.shift(w, start='2026-10-04T10:00:00+03:00', end='2026-10-04T14:00:00+03:00',
                typ=typ['id'], people=[b])['workspace']
    w = h.shift(w, start='2026-10-04T11:00:00+03:00', end='2026-10-04T15:00:00+03:00',
                typ=typ['id'], people=[h.seed['people'][2]])['workspace']
    requirement = line(typ['id'], [a, b])
    requirement['settings']['team_lead_requirement']['count_scope'] = 'concurrent_duties'
    activate(h, proposal(h, [requirement]))
    data = h.snapshot(w)
    first = next(shift for shift in data['shifts'] if any(p['person_id'] == a for p in shift['participants']))
    second = next(shift for shift in data['shifts'] if any(p['person_id'] == b for p in shift['participants']))
    third = next(shift for shift in data['shifts'] if any(p['person_id'] == h.seed['people'][2] for p in shift['participants']))
    next(p for p in first['participants'] if p['person_id'] == a)['team_lead'] = True
    next(p for p in second['participants'] if p['person_id'] == b)['team_lead'] = True
    findings = [f for f in errors(data) if f['code'] == 'LEAD_GAP']
    assert any(f['detail']['scheduled'] == 2 for f in findings)
    assert any(set(f['detail']['affected_shift_ids']) == {first['id'], second['id'], third['id']}
               for f in findings)


def test_solver_concurrent_duties_can_use_one_leader(h):
    a, b, _ = h.seed['people']
    typ = h.configure('assignment_types', {'name': 'Concurrent solver duty', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 2}]}})['record']
    w = h.workspace()
    w = h.shift(w, typ=typ['id'], people=[a])['workspace']
    w = h.shift(w, typ=typ['id'], people=[b])['workspace']
    requirement = line(typ['id'], [a, b])
    requirement['settings']['team_lead_requirement']['count_scope'] = 'concurrent_duties'
    activate(h, proposal(h, [requirement]))
    result = generate(copy.deepcopy(h.snapshot(w)), 10, workers=1)
    assert result['status'] == 'candidate_feasible', (result['status'], result.get('issues'), result.get('validation', {}).get('findings'))
    assert sum(bool(row['participant'].get('team_lead')) for row in result['additions']) == 1


def test_conversion_freezes_publication_and_future_cutover_then_retire(h):
    from apps.api.services.leadership import conversion_plan
    typ=legacy_type(h);a=h.seed['people'][0]
    w=h.shift(h.workspace(),typ=typ['id'],people=[{'person_id':a,'start':START,'end':END,'team_lead':True}])['workspace']
    published=h.publish(w)
    with transaction(h.app.state.engine) as db:
        pub=db.get(m.Publication,published['publication_id']);frozen=copy.deepcopy(pub.snapshot)
        plan=conversion_plan(db,SimpleNamespace(department_id=typ['department_id']),'2026-10-05')
    p=activate(h,proposal(h,plan['lines'],legacy_lead_conversion=plan['legacy_lead_conversion']))
    historical=h.snapshot(w)
    assert historical['type_lead_ownership']==[]
    assert leadership.summary(historical,historical['shifts'][0])['continuity']=='continuous'
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Publication,published['publication_id']).snapshot==frozen
        from apps.api.snapshots import domain_snapshot
        current=domain_snapshot(db,db.get(m.Workspace,w['id']),current_scheduling=True)
    shift=current['shifts'][0]
    assert 'legacy' in leadership.requirements(current,shift,START,END)[0]
    shifted={**shift,'start':'2026-10-05T08:00:00+03:00','end':'2026-10-05T12:00:00+03:00'}
    assert 'policy' in leadership.requirements(current,shift,shifted['start'],shifted['end'])[0]
    h.command('approver','policy.retire',{'reason':'Retire approved leadership with visible review'},p['id'],p['revision'])
    with transaction(h.app.state.engine) as db:
        from apps.api.snapshots import domain_snapshot
        retired=domain_snapshot(db,db.get(m.Workspace,w['id']),current_scheduling=True)
    assert len(retired['type_lead_ownership'])==1
    assert leadership.summary(retired,shift,shifted['start'],shifted['end']) is None


def test_stale_conversion_is_rejected_without_receipt(h):
    from apps.api.services.leadership import conversion_plan
    typ=legacy_type(h)
    with transaction(h.app.state.engine) as db:
        plan=conversion_plan(db,SimpleNamespace(department_id=typ['department_id']),'2026-10-04')
    p=proposal(h,plan['lines'],legacy_lead_conversion=plan['legacy_lead_conversion'])
    h.configure('assignment_types',{k:typ[k] for k in ('name','starts_on','definition')},typ['id'],typ['revision'])
    result=h.command('approver','policy.activate',{'reason':'Review stale proposal'},p['id'],p['revision'],status=409)
    assert result['error']['code']=='STALE_LEAD_CONVERSION'
    with transaction(h.app.state.engine) as db:
        assert list(db.scalars(select(m.TypeLeadOwnership)))==[]


@pytest.mark.parametrize('state', ['current', 'ended', 'future', 'parent_inactive'])
def test_context_cohort_qualification_follows_rotation_and_parent_dates(h, state):
    from apps.api.services.leadership import lead_context
    a,b,c = h.seed['people']
    typ,w = prepared(h,[a,b])
    programme = h.configure('programmes', {'name':'Fictional leadership programme','starts_on':'2026-01-01'})['record']
    cohort = h.configure('cohorts', {'name':'Fictional leadership cohort','programme_id':programme['id'],
        'starts_on':'2026-01-01','ends_on':'2027-01-01'})['record']
    h.configure('programme_memberships', {'person_id':a,'programme_id':programme['id'],
        'starts_on':'2026-01-01','ends_on':'2027-01-01'})
    first,last = {'current':('2026-10-01','2026-11-01'), 'ended':('2026-09-01','2026-10-04'),
                  'future':('2026-10-05','2026-11-01'), 'parent_inactive':('2026-10-01','2026-11-01')}[state]
    h.configure('rotations', {'person_id':a,'cohort_id':cohort['id'],'service_id':h.seed['services'][0],
        'starts_on':first,'ends_on':last})
    requirement = line(typ['id'])
    requirement['who'] = {'match':'all','conditions':[{'field':'cohort_ids','op':'any','values':[cohort['id']]}]}
    activate(h,proposal(h,[requirement]))
    if state == 'parent_inactive':
        with transaction(h.app.state.engine,write=True) as db:
            db.get(m.Programme,programme['id']).active=False
    shift=h.snapshot(w)['shifts'][0]
    proposed={k:shift[k] for k in s.ShiftInput.model_fields if k in shift}
    proposed['participants']=[{k:p[k] for k in s.ParticipantInput.model_fields if k in p} for p in shift['participants']]
    with transaction(h.app.state.engine) as db:
        result=lead_context(db,context_actor(h),w['id'],s.LeadContextInput(shift=proposed,shift_id=shift['id'],person_ids=[a]))
    assert result.people[0].can_lead is (state == 'current')
    assert all(row.enabled and row.minimum == 1 for row in result.segments)


def test_bulk_conversion_materializes_missing_history_and_is_idempotent(h):
    from apps.api.services.leadership import conversion_plan
    from tests.test_bulk import preview
    typ=legacy_type(h)
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.DefinitionHistory.id).where(m.DefinitionHistory.object_id==typ['id'],
            m.DefinitionHistory.object_revision==typ['revision'])) is None
        plan=conversion_plan(db,SimpleNamespace(department_id=typ['department_id']),'2026-10-04')
    p=proposal(h,plan['lines'],legacy_lead_conversion=plan['legacy_lead_conversion'])
    payload={'item_kind':'policy.activate','items':[{'target_id':p['id'],'expected_revision':p['revision'],'payload':{}}],
             'reason':'Independent bulk review of fictional legacy leadership'}
    assert preview(h,'approver',payload)['counts']['problem']==0
    key=str(uuid.uuid4())
    first=h.command('approver','bulk.apply',payload,key=key)
    assert first['items'][0]['record']['status']=='active'
    again=h.command('approver','bulk.apply',payload,key=key)
    assert again['items'][0]['record']['id']==p['id']
    with transaction(h.app.state.engine) as db:
        receipts=list(db.scalars(select(m.TypeLeadOwnership)))
        assert len(receipts)==1
        baseline=db.get(m.DefinitionHistory,receipts[0].source_history_id)
        assert baseline.object_revision==typ['revision']
        assert baseline.snapshot['definition']['lead']==typ['definition']['lead']
        assert baseline.batch_id is not None


def test_untracked_primitive_and_inconsistent_history_fail_closed(h):
    from apps.api.errors import Problem
    from apps.api.services.leadership import conversion_plan
    refused=h.command('coordinator','policy.create', {'name':'Untracked leader source','owner':'Fictional owner',
        'source':'Fictional authoring guard','starts_on':'2026-10-01',
        'definition':{'rule':{'kind':'team_lead_requirement','minimum':1,'maximum':1}}},status=422)
    assert refused['error']['code']=='USE_RULE_CATALOGUE'
    typ=h.configure('assignment_types', {'name':'Inconsistent imported history','starts_on':'2020-01-01','definition':{}})['record']
    with transaction(h.app.state.engine,write=True) as db:
        row=db.get(m.AssignmentType,typ['id'])
        row.definition={**row.definition,'lead':s.TeamLead().model_dump(mode='json')}
    with transaction(h.app.state.engine) as db:
        with pytest.raises(Problem) as failure:
            conversion_plan(db,SimpleNamespace(department_id=typ['department_id']),'2026-10-04')
        assert failure.value.code=='LEAD_HISTORY_MISMATCH'
        assert list(db.scalars(select(m.TypeLeadOwnership)))==[]
# Include this file in hosted supervision and team-lead qualification.
