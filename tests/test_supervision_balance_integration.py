"""Real scheduler, catalogue, publication and Fix-list paths for supervision Fair share."""
from collections import Counter
from copy import deepcopy
from datetime import date, timedelta
from uuid import NAMESPACE_URL, uuid5

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from packages.domain import fairness
from packages.domain.cp_sat import generate
from packages.domain.rota_summary import rota_summary
from packages.domain.validation import evaluate
from tests.scheduler_departments import apply
from tests.test_rule_catalogue_api import approve, line, propose


def stable_id(*parts):
    return str(uuid5(NAMESPACE_URL, 'supervision-balance:' + ':'.join(map(str, parts))))


def paired_department(h, *, groups=1, sessions=8):
    """A fixed consultant rota with one vacant trainee place per session; five eligible people per team."""
    typ = h.configure('assignment_types', {'name': 'Named supervision clinic', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 5, 'maximum': 5}],
            'supervision': {'trainee_category_ids': [h.seed['trainee_category']],
                'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 1,
                'require_pairing': True}}})['record']
    w = h.workspace(start='2026-12-01', end='2027-01-01')
    w = h.shift(w, typ=typ['id'], start='2026-12-01T08:00:00+03:00',
                end='2026-12-01T12:00:00+03:00', people=[])['workspace']
    data = h.snapshot(w)
    template = next(s for s in data['shifts'] if s['id'] in data['focus_ids'])
    source = h.seed['people'][0]
    source_person = data['people'][source]
    base = {key: [r for r in data[key] if r['person_id'] == source]
            for key in ('memberships', 'eligibilities', 'grants')}
    data.update(people={}, memberships=[], eligibilities=[], grants=[], availability=[], rotations=[], policies=[])
    crews, supervisors = [], []
    for group in range(groups):
        crew = [stable_id('person', group, i) for i in range(5)]
        crews.append(crew)
        supervisors.extend(crew[:4])
        for i, pid in enumerate(crew):
            data['people'][pid] = {**source_person, 'id': pid, 'display_name': f'Team {group} person {i}'}
            for key, rows in base.items():
                if key == 'grants' and i == 4:
                    continue
                for j, row in enumerate(rows):
                    entry = {**row, 'id': stable_id(key, pid, j), 'person_id': pid}
                    if key == 'memberships':
                        entry['category_id'] = h.seed['trainee_category'] if i == 4 else h.seed['category']
                    data[key].append(entry)
    data['shifts'] = []
    for index in range(sessions):
        crew = crews[index % groups]
        day = date(2026, 12, 1) + timedelta(days=index // groups)
        start, end = f'{day}T08:00:00+03:00', f'{day}T12:00:00+03:00'
        shift = {**deepcopy(template), 'id': stable_id('session', index), 'start': start, 'end': end,
            'series_snapshot': {'definition': {'default_person_ids': crew, 'usual_assignees_fixed': True}},
            'participants': [{'id': stable_id('assignment', index, pid), 'person_id': pid,
                'start': start, 'end': end, 'participation': 'supervisor', 'overtime': False,
                'source': 'manual', 'fixed': True} for pid in crew[:4]]}
        data['shifts'].append(shift)
    data['focus_ids'] = [s['id'] for s in data['shifts']]
    data.pop('generation_session_ids', None)
    for location in data['locations'].values():
        location['capacity'] = groups * 5
    data['generation_options'] = {'mode': 'fill_gaps', 'shortlist_width': 0}
    goal = {'id': stable_id('goal'), 'name': 'Supervision fair share', 'revision': 1,
        'department_id': data['workspace']['department_id'], 'status': 'active', 'active': True,
        'starts_on': '2020-01-01', 'ends_on': None,
        'definition': {'rule': {'kind': 'catalogue', 'id': 'fair_share', 'catalogue_version': 2,
            'lines': [{'id': stable_id('line'),
                'who': {'conditions': [{'field': 'category_ids', 'values': [h.seed['category']]}]},
                'numbers': {'apply': {'value': None, 'level': 'medium'}},
                'settings': {'balance_goal': {'metric': 'supervision', 'period': 'month',
                    'share_by': 'available_days', 'tolerance_units': 0, 'tolerance_percent': 0}}}]}}}
    return data, goal, supervisors


def supervision_totals(data):
    return Counter(p['supervisor_person_id'] for s in data['shifts'] for p in s['participants']
                   if p.get('supervisor_person_id'))


def test_complete_scheduler_changes_skew_to_two_each_without_changing_hard_cover(h):
    data, goal, supervisors = paired_department(h)
    results = []
    for enabled in (False, True):
        candidate = deepcopy(data)
        candidate['policies'] = [goal] if enabled else []
        result = generate(candidate, 120, workers=1)
        assert result['status'] == 'candidate_feasible', result
        assert result['validation']['error_count'] == 0
        assert result['weighted_status'] in {'OPTIMAL', 'FEASIBLE'}
        draft = apply(candidate, result)
        assert evaluate(draft)['valid']
        assert all(len(s['participants']) == 5 for s in draft['shifts'])
        results.append(supervision_totals(draft))
        if enabled:
            [report] = [r for r in result['validation']['goals'] if r['metric'] == 'supervision']
            assert report['score'] == 100
            assert result['compiled_component_values']['fairness'] == pytest.approx(
                result['objective_components']['fairness']['value'], abs=0.0001)
    assert max(results[0].values()) == 8, results
    assert results[1] == dict.fromkeys(supervisors, 2), results


def test_goal_narrows_supervision_spread_whatever_the_machine_speed(h):
    """Core check: deterministic time, one worker, so the result does not depend on wall-clock speed."""
    data, goal, supervisors = paired_department(h)
    spreads = []
    for enabled in (False, True):
        candidate = deepcopy(data)
        candidate['policies'] = [goal] if enabled else []
        result = generate(candidate, 60, workers=1, deterministic=True)
        assert result['status'] == 'candidate_feasible', result
        assert result['validation']['error_count'] == 0
        assert result['weighted_status'] in {'OPTIMAL', 'FEASIBLE'}
        draft = apply(candidate, result)
        assert all(len(s['participants']) == 5 for s in draft['shifts'])
        totals = supervision_totals(draft)
        spreads.append(max(totals.values(), default=0) - min((totals.get(pid, 0) for pid in supervisors), default=0))
    assert spreads[1] < spreads[0], spreads


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_catalogue_publication_history_distribution_and_fix_causes(h):
    trainee, supervisor, colleague = h.seed['people']
    typ = h.configure('assignment_types', {'name': 'Supervision history', 'starts_on': '2020-01-01',
        'definition': {'supervision': {'trainee_category_ids': [h.seed['trainee_category']],
            'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 1,
            'require_pairing': True}}})['record']
    setting = {'metric': 'supervision', 'period': 'month', 'share_by': 'available_days',
               'tolerance_percent': 0, 'tolerance_units': 0}
    rule_line = line(who={
        'conditions': [{'field': 'person_ids', 'values': [supervisor, colleague]}]},
        numbers={'apply': {'value': None, 'level': 'medium'}}, settings={'balance_goal': setting})
    saved = approve(h, propose(h, 'fair_share', [rule_line])['record'])
    capabilities = h.get('coordinator', '/api/policies/capabilities')['definition_schema']
    assert 'supervision' in capabilities['$defs']['BalanceGoal']['properties']['metric']['enum']
    view = h.command('coordinator', 'table_view.create', {'table_key': 'fairness', 'name': 'Supervision',
        'definition': {'fairness': {'metric': 'supervision'}}})['view']
    assert view['definition']['fairness']['metric'] == 'supervision'
    w = h.workspace()
    for day in range(4, 8):
        start, end = f'2026-10-{day:02}T08:00:00+03:00', f'2026-10-{day:02}T12:00:00+03:00'
        participants = [{'person_id': pid, 'start': start, 'end': end,
            'participation': 'trainee' if pid == trainee else 'supervisor',
            **({'supervisor_person_id': supervisor} if pid == trainee else {})}
            for pid in (trainee, supervisor, colleague)]
        w = h.shift(w, typ=typ['id'], start=start, end=end, people=participants)['workspace']
    validation = evaluate(h.snapshot(w))
    assert validation['valid']
    [report] = [r for r in validation['goals'] if r['metric'] == 'supervision']
    assert {r['person_id']: r['actual'] for r in report['people']} == {supervisor: 4, colleague: 0}
    warnings = [f for f in validation['findings'] if f['code'] == 'BALANCE_GOAL']
    assert len(warnings) == 2 and all(f['severity'] == 'warning' for f in warnings)
    summary = rota_summary(validation)
    assert summary['fix_list']['summary']['must'] == 0
    assert any(c['level'] == 'could' and any(e['code'] == 'BALANCE_GOAL' for e in c['evidence'])
               for c in summary['fix_list']['causes'])
    published = h.publish(w)
    with transaction(h.app.state.engine) as db:
        pub = db.get(m.Publication, published['publication_id'])
        before = deepcopy(pub.snapshot)
        ledger_before = [(r.id, r.metric_key, r.value) for r in db.scalars(select(m.FairnessLedger).where(
            m.FairnessLedger.publication_id == pub.id))]
    period = '&starts_on=2026-10-01&ends_on=2026-11-01'
    url = '/api/fairness/distribution?metric=supervision' + period
    for suffix in ('', '&group_by=service_ids', '&group_by=assignment_type_ids', '&group_by=location_ids'):
        distribution = h.get('coordinator', url + suffix)
        assert {r['person_id']: r['value'] for r in distribution['people']} == {trainee: 0, supervisor: 4, colleague: 0}
        assert distribution['months'] == {'2026-10': 4}
        if suffix:
            assert sum(r['value'] for r in distribution['groups']) == 4
    drill = h.get('coordinator', f'/api/fairness/sessions?metric=supervision&person_id={supervisor}' + period)
    assert drill['metric_value'] == 4
    assert sum(r['value'] for pub in drill['publications'] for r in pub['sessions']) == 4
    h.get('staff', f'/api/fairness/sessions?metric=supervision&person_id={supervisor}' + period, status=403)
    assert [r['person_id'] for r in h.get('staff', url)['people']] == [trainee]
    nxt = h.workspace(start='2026-10-11', end='2026-10-18')
    data = h.snapshot(nxt)
    [goal] = [g for g in fairness.balance_goals(data) if g['id'] == saved['id']]
    historical = data['fairness_history'][fairness.goal_identity(goal)]['people']
    assert historical[supervisor]['value'] == 4 and historical[colleague]['value'] == 0
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Publication, published['publication_id']).snapshot == before
        assert [(r.id, r.metric_key, r.value) for r in db.scalars(select(m.FairnessLedger).where(
            m.FairnessLedger.publication_id == published['publication_id']))] == ledger_before
