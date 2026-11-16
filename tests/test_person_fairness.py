"""Person fairness reads reuse the shared reports, protect peers, and stay bounded at department scale."""
import time
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import event, insert

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import fairness
from apps.api.services.person_fairness import published_trends
from packages.contracts.schemas import PersonFairness, PersonFairnessSource, ROLE_NAMES
from packages.domain import fairness as f
from packages.domain.conditions import active_rules
from packages.domain.quotas import METRIC_VERSION
from tests.governance_support import identity
from tests.test_rule_catalogue_api import approve, propose
from tests.test_rule_catalogue_guard import line


def endpoint(person, suffix=''):
    return f'/api/people/{person}/fairness' + suffix


def scalar_goal(h, **settings):
    return h.policy({'kind': 'balance_goal', 'metric': 'duties', 'aim': 'equal_share', 'period': 'month', **settings},
                    severity='advisory')


def test_goal_interval_cache_keeps_policy_copies_and_rota_periods_separate(h):
    person, other, _ = h.seed['people']
    goal = scalar_goal(h, period='rota')
    data = h.snapshot(h.workspace())
    data['_person_dates_cache'] = {}
    window = date(2026, 10, 1), date(2026, 10, 20)
    whole = f.goal_intervals(data, goal, person, *window)
    assert whole == [(date(2026, 10, 4), date(2026, 10, 11))]
    assert f.goal_intervals(data, goal, person, *window) == whole
    excluded = {**goal, 'definition': {**goal['definition'], 'scope': {'person_ids': [other]}}}
    assert f.goal_intervals(data, excluded, person, *window) == []
    shorter = {**data, 'workspace': {**data['workspace'], 'ends_on': '2026-10-08'}}
    assert f.goal_intervals(shorter, goal, person, *window) == [(date(2026, 10, 4), date(2026, 10, 8))]
    assert f.goal_intervals(data, goal, person, *window) == whole


def clinic_goal(h, metric, strengths=('medium', 'medium')):
    a, b, _ = h.seed['people']
    pair = metric == 'pair_exposure'
    settings = {'metric': metric, 'aim': 'min_per_pair' if pair else 'target', 'period': 'rota'}
    if pair:
        settings['partners'] = {'conditions': [{'field': 'person_ids', 'values': [b]}]}
    lines = [line(who={'conditions': [{'field': 'person_ids', 'values': [a]}]},
                  settings={'balance_goal': {'service_ids': [service]}},
                  numbers={'target': {'value': target, 'level': strength}})
             for service, target, strength in zip(h.seed['services'], (2, 5), strengths)]
    goal = approve(h, propose(h, 'fair_share', lines, settings={'balance_goal': settings},
                             balance_between_people=False)['record'])
    w = h.workspace(start='2026-10-04', end='2026-10-11')
    for day, service in (('04', 0), ('05', 1), ('06', 1)):
        w = h.shift(w, f'2026-10-{day}T08:00:00+03:00', f'2026-10-{day}T12:00:00+03:00',
                    people=[a, b] if pair else [a], service=service)['workspace']
    return goal, w


def test_person_counted_fairness_trends_keep_each_clinic_separate(h):
    person = h.seed['people'][0]
    clinics = h.seed['services'][:2]
    goal = h.policy({'kind': 'balance_goal', 'metric': 'duties', 'period': 'rota', 'aim': 'target', 'amount': 3},
                    scope={'conditions': {'conditions': [
                        {'field': 'service_ids', 'op': 'any', 'values': clinics, 'count': 'each'}]}},
                    severity='advisory')
    workspace = h.workspace()
    for day, service in (('04', 0), ('05', 1), ('06', 1)):
        workspace = h.shift(workspace, f'2026-10-{day}T08:00:00+03:00', f'2026-10-{day}T12:00:00+03:00',
                            people=[person], service=service)['workspace']
    publication = h.publish(workspace)['publication_id']
    reports = [report for report in h.get('coordinator', endpoint(person))['goals'] if report['policy_id'] == goal['id']]
    assert len(reports) == len({report['goal_id'] for report in reports}) == 2
    for report in reports:
        [trend] = report['trend']
        actual = 1 if report['count_bucket'][-1][1] == clinics[0] else 2
        assert report['trend_complete'] and report['people'][0]['actual'] == actual
        assert (trend['publication_id'], trend['actual'], trend['expected']) == (publication, actual, 3)
    changed = [{**report, 'strength': 'critical'} for report in reversed(reports)]
    with transaction(h.app.state.engine) as db:
        trends = published_trends(db, h.seed['department_id'], person, changed, date(2026, 10, 1), date(2026, 11, 1))
    assert [trend['trend'] for trend in trends] == [report['trend'] for report in reversed(reports)]
    assert all(trend['trend_complete'] for trend in trends)


@pytest.mark.parametrize('present', [False, True])
@pytest.mark.parametrize('provenance', [False, True])
def test_counted_trend_marks_older_combined_person_totals_incomplete(present, provenance):
    person, policy, clinic = (str(uuid.uuid4()) for _ in range(3))
    report = {'policy_id': policy, 'line_id': str(uuid.uuid4()), 'line': 0, 'slot': 'target',
              'metric': 'duties', 'unit': 'duties', 'aim': 'target', 'period': 'month', 'strength': 'medium',
              'count_bucket': [['0:service_ids', clinic]]}
    saved = {key: value for key, value in report.items()
             if key != 'count_bucket' and (provenance or key not in {'line_id', 'line', 'slot'})}
    saved['people'] = [{'person_id': person, 'actual': 3, 'expected': 4}] if present else []
    publication = SimpleNamespace(id=str(uuid.uuid4()), workspace_id=str(uuid.uuid4()),
                                  starts_on=date(2026, 10, 1), ends_on=date(2026, 11, 1),
                                  effective_until=None, goals=[saved])
    responses = iter([SimpleNamespace(all=lambda: [publication]), []])
    db = SimpleNamespace(execute=lambda _query: next(responses))
    [trend] = published_trends(db, str(uuid.uuid4()), person, [report], date(2026, 10, 1), date(2026, 11, 1))
    assert trend == {'trend': [], 'trend_complete': not present}


@pytest.mark.parametrize('metric', ['duties', 'pair_exposure'])
def test_person_fairness_saved_trends_keep_same_metadata_clinic_copies_separate(h, metric):
    a = h.seed['people'][0]
    goal, w = clinic_goal(h, metric)
    saved = [r for r in h.detail(w)['workspace']['validation']['goals'] if r['policy_id'] == goal['id']]
    configured = goal['definition']['rule']['lines']
    assert [(r['line_id'], r['line'], r['slot']) for r in saved] == [
        (configured[n]['id'], n, 'target') for n in range(2)]
    publication = h.publish(w)['publication_id']
    reports = [r for r in h.get('staff', endpoint(a))['goals'] if r['policy_id'] == goal['id']]
    assert len(reports) == 2
    for n, report in enumerate(reports):
        assert (report['line_id'], report['line'], report['slot']) == (configured[n]['id'], n, 'target')
        assert report['trend_complete'] is True
        [trend] = report['trend']
        assert (trend['publication_id'], trend['actual'], trend['expected']) == (publication, n + 1, (2, 5)[n])
        assert trend['partner_id'] == (h.seed['people'][1] if metric == 'pair_exposure' else None)
    # Display order and today's strength can change without losing the same stable line's recorded values.
    changed = [{**r, 'line': 7 - n, 'strength': 'critical'} for n, r in enumerate(reports)]
    with transaction(h.app.state.engine) as db:
        trends = published_trends(db, h.seed['department_id'], a, changed, date(2026, 10, 1), date(2026, 11, 1))
    assert [t['trend'] for t in trends] == [r['trend'] for r in reports]
    assert all(t['trend_complete'] for t in trends)


@pytest.mark.parametrize('metric', ['duties', 'pair_exposure'])
@pytest.mark.parametrize('strengths,complete', [(('medium', 'medium'), False), (('low', 'high'), True)])
def test_person_fairness_legacy_trends_require_unique_metadata(h, metric, strengths, complete):
    a = h.seed['people'][0]
    goal, w = clinic_goal(h, metric, strengths)
    legacy = [{k: v for k, v in r.items() if k not in ('line_id', 'line', 'slot')}
              for r in h.detail(w)['workspace']['validation']['goals'] if r['policy_id'] == goal['id']]
    with transaction(h.app.state.engine, write=True) as db:
        historical = m.Workspace(department_id=h.seed['department_id'], name='Synthetic legacy goal receipt',
                                starts_on=date(2026, 9, 4), ends_on=date(2026, 9, 11), status='published',
                                created_by=h.seed['identities']['coordinator'], last_edited_by=h.seed['identities']['coordinator'])
        db.add(historical)
        db.flush()
        publication = m.Publication(department_id=h.seed['department_id'], workspace_id=historical.id,
                                    sequence=1, starts_on=historical.starts_on, ends_on=historical.ends_on,
                                    published_by=h.seed['identities']['coordinator'], snapshot={'shifts': []},
                                    fingerprint='0' * 64, validation={'goals': legacy, 'contracts': {
                                        'block_quota': METRIC_VERSION, 'programmes': 'programmes-v1'}})
        db.add(publication)
        db.flush()
        publication_id = publication.id
        # Indistinguishable duplicate ledger aims must never be used to repair an ambiguous saved receipt.
        if metric == 'duties':
            db.execute(insert(m.FairnessLedger), [{'department_id': h.seed['department_id'],
                'publication_id': publication.id, 'person_id': a, 'metric_key': 'goal:' + goal['id'],
                'bucket': publication.starts_on, 'value': 99 + n, 'expected': 200 + n} for n in range(2)])
    reports = [r for r in h.get('coordinator', endpoint(a, '?workspace_id=' + w['id']))['goals']
               if r['policy_id'] == goal['id']]
    assert len(reports) == 2
    for n, report in enumerate(reports):
        assert report['trend_complete'] is complete
        if complete:
            [trend] = report['trend']
            assert (trend['publication_id'], trend['actual'], trend['expected']) == (publication_id, n + 1, (2, 5)[n])
        else:
            assert report['trend'] == []


def test_person_standing_matches_domain_pro_rata_target_and_history(h):
    a, b, c = h.seed['people']
    share = scalar_goal(h)
    target = scalar_goal(h, aim='target', amount=8, person_amounts={a: 12})
    unrelated = h.policy({'kind': 'balance_goal', 'metric': 'hours', 'period': 'month'},
                         scope={'person_ids': [b]}, severity='advisory')
    h.command('coordinator', 'availability.record', {'person_id': c, 'kind': 'recorded_leave',
              'private_reason': 'Synthetic leave', 'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-18T00:00:00+03:00'})
    w = h.shift(h.workspace(), people=[a, b])['workspace']
    publication = h.publish(w)['publication_id']
    draft = h.shift(h.workspace(start='2026-10-11', end='2026-10-18'),
                    start='2026-10-11T08:00:00+03:00', end='2026-10-11T12:00:00+03:00', people=[a])['workspace']
    result = h.get('coordinator', endpoint(a, '?workspace_id=' + draft['id']))
    assert result['person_id'] == a and result['peers_visible'] is True
    assert any(r['metric'] == 'workload' for r in result['goals'])
    reports = {r['policy_id']: r for r in result['goals'] if r['metric'] != 'workload'}
    assert set(reports) == {share['id'], target['id']} and unrelated['id'] not in reports
    data = h.snapshot(draft)
    for pid, actual in reports.items():
        expected = f.goal_report(data, next(p for p in data['policies'] if p['id'] == pid))
        [person] = actual['people']
        row = next(r for r in expected['people'] if r['person_id'] == a)
        assert {k: person[k] for k in row} == row
        assert actual['score'] == expected['score']
        assert person['rank'] == 1 + sum(r['cost'] < row['cost'] for r in expected['people'])
        assert person['peers'] == person['peer_count'] == 2 and actual['rank_basis'] == 'cost'
        assert (actual['starts_on'], actual['ends_on']) == ('2026-10-01', '2026-11-01')
        assert (actual['history_starts_on'], actual['history_ends_on']) == ('2026-10-01', '2026-10-11')
        [trend] = actual['trend']
        assert trend['publication_id'] == publication and trend['actual'] == 1 and trend['partner_id'] is None
    [person] = reports[share['id']]['people']
    assert (person['prior'], person['planned'], person['actual'], person['expected']) == (1, 1, 2, 1.5)
    assert reports[target['id']]['people'][0]['expected'] == round(12 * 17 / 31, 2)


def test_person_fairness_catalogue_dated_person_and_separate_clinic_lines(h):
    a = h.seed['people'][0]
    who = {'conditions': [{'field': 'person_ids', 'values': [a]}]}
    lines = [line(numbers={'target': {'value': 2, 'level': 'medium'}}),
             line(who=who, starts_on='2026-10-05', ends_on='2026-10-07',
                  numbers={'target': {'value': 7, 'level': 'high'}}),
             line(settings={'balance_goal': {'service_ids': [h.seed['services'][1]]}},
                  numbers={'target': {'value': 3, 'level': 'low'}})]
    goal = approve(h, propose(h, 'fair_share', lines, acknowledged=[lines[0]['id']],
                             settings={'balance_goal': {'metric': 'duties', 'aim': 'target', 'period': 'rota'}},
                             balance_between_people=False)['record'])
    w = h.workspace(start='2026-10-04', end='2026-10-11')
    for day, service in (('04', 0), ('06', 0), ('08', 1)):
        w = h.shift(w, f'2026-10-{day}T08:00:00+03:00', f'2026-10-{day}T12:00:00+03:00',
                    people=[a], service=service)['workspace']
    reports = h.get('coordinator', endpoint(a, '?workspace_id=' + w['id']))['goals']
    reports = [r for r in reports if r['policy_id'] == goal['id']]
    # D53: activation selects the rota; the person line then supplies its whole-window dial.
    # Its broader comparison and the different clinic comparison remain independent.
    assert sorted((r['people'][0]['planned'], r['people'][0]['expected']) for r in reports) == [(1, 3), (3, 7)]
    data = h.snapshot(w)
    assert next(p for p in data['policies'] if p['id'] == goal['id'])['kind'] == 'catalogue'
    expanded = {**data, 'policies': active_rules(data['policies'], overrides=False)}
    assert [f.goal_report(data, p) for p in f.balance_goals(data)] == [
        f.goal_report(expanded, p) for p in f.balance_goals(expanded)]
    expected = {r['goal_id']: r for r in f.goal_reports(data) if r['policy_id'] == goal['id']}
    for report in reports:
        row = next(r for r in expected[report['goal_id']]['people'] if r['person_id'] == a)
        assert {key: report['people'][0][key] for key in row} == row


def test_person_grouped_fairness_reuses_shared_ranking_and_own_source_provenance(h):
    a, b, _ = h.seed['people']
    lines = [line(who={'conditions': [{'field': 'person_ids', 'values': [pid]}]},
                  numbers={'target': {'value': amount, 'level': level}})
             for pid, amount, level in ((a, 2, 'high'), (b, 4, 'low'))]
    goal = approve(h, propose(h, 'fair_share', lines, settings={'balance_goal': {
        'metric': 'duties', 'aim': 'target', 'period': 'rota', 'comparison': 'pooled'}})['record'])
    w = h.workspace()
    for day in (4, 5, 6, 7):
        w = h.shift(w, f'2026-10-{day:02}T08:00:00+03:00', f'2026-10-{day:02}T12:00:00+03:00',
                    people=[a])['workspace']
    [shared] = [r for r in f.goal_reports(h.snapshot(w)) if r['policy_id'] == goal['id']]
    result = h.get('coordinator', endpoint(a, '?workspace_id=' + w['id']))
    PersonFairness.model_validate(result)
    [report] = [r for r in result['goals'] if r['policy_id'] == goal['id']]
    [person] = report['people']
    expected = next(r for r in shared['people'] if r['person_id'] == a)
    assert {k: person[k] for k in expected} == expected
    assert report['score'] == shared['score']
    assert (person['rank'], person['peer_count']) == (1, 2)
    assert report['source_goal_ids'] == shared['source_goal_ids']
    assert (report['line_id'], report['line'], report['slot']) == (None, None, None)
    [source] = person['sources']
    assert (source['line_id'], source['line'], source['slot'], source['strength']) == (lines[0]['id'], 0, 'target', 'high')
    [other_report] = [r for r in h.get('coordinator', endpoint(b, '?workspace_id=' + w['id']))['goals']
                      if r['policy_id'] == goal['id']]
    [other] = other_report['people']
    assert (other['rank'], other['peer_count']) == (2, 2)
    publication = h.publish(w)['publication_id']
    [own] = [r for r in h.get('staff', endpoint(a))['goals'] if r['policy_id'] == goal['id']]
    assert own['score'] is None and not {'rank', 'peers', 'peer_count'} & own['people'][0].keys()
    [trend] = own['trend']
    assert own['trend_complete'] and (trend['publication_id'], trend['actual'], trend['expected']) == (publication, 4, 2)
    with pytest.raises(ValidationError, match='extra_forbidden'):
        PersonFairness.model_validate({**result, 'goals': [{**report, 'unsupported': True}]})
    with pytest.raises(ValidationError, match='extra_forbidden'):
        PersonFairnessSource.model_validate({**source, 'unsupported': True})


@pytest.mark.parametrize('balance', [True, False])
def test_person_quota_targets_match_dashboard_and_keep_publication_trends(h, balance):
    a, b, _ = h.seed['people']
    lines = [line(who={'conditions': [{'field': 'person_ids', 'values': [pid]}]},
                  numbers={'target': {'value': amount, 'level': level}})
             for pid, amount, level in ((a, 2, 'high'), (b, 4, 'low'))]
    goal = approve(h, propose(h, 'duties_per_month', lines, settings={'block_quota': {
        'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'window_alignment': 'calendar_month'}},
        balance_between_people=balance)['record'])
    w = h.shift(h.workspace(), people=[a])['workspace']
    dashboard = h.get('coordinator', '/api/fairness/goals?workspace_id=' + w['id'])
    shared = next(r for r in dashboard['goals'] if r['policy_id'] == goal['id']
                  and any(p['person_id'] == a for p in r['people']))
    result = h.get('coordinator', endpoint(a, '?workspace_id=' + w['id']))
    PersonFairness.model_validate(result)
    [report] = [r for r in result['goals'] if r['policy_id'] == goal['id']]
    [person] = report['people']
    expected = {k: v for k, v in next(r for r in shared['people'] if r['person_id'] == a).items() if k != 'name'}
    assert {k: person[k] for k in expected} == expected
    assert (person['actual'], person['expected']) == (1, 1)  # ceil(2 * 5/21 working days)
    quota = next(row for row in h.detail(w)['workspace']['validation']['quota_report']['rows']
                 if row['policy_id'] == goal['id'] and row['person_id'] == a)
    assert {key: quota['proration'][key] for key in (
        'configured_amount', 'amount', 'working_days', 'full_working_days')} == {
        'configured_amount': 2, 'amount': 1, 'working_days': 5, 'full_working_days': 21}
    assert report['quota_target'] and report['score'] == shared['score']
    assert person['peer_count'] == (2 if balance else 1)
    assert person.get('available_days') is None and person.get('rota_days') is None
    assert (report['starts_on'], report['ends_on']) == ('2026-10-03', '2026-11-01')  # active-rule start clips the month
    assert report['history_starts_on'] is None and report['history_ends_on'] is None
    publication = h.publish(w)['publication_id']
    [published] = [r for r in h.get('staff', endpoint(a))['goals'] if r['policy_id'] == goal['id']]
    assert {key: published[key] for key in ('goal_id', 'quota_target', 'starts_on', 'ends_on')} == {
        key: report[key] for key in ('goal_id', 'quota_target', 'starts_on', 'ends_on')}
    assert (published['people'][0]['actual'], published['people'][0]['expected']) == (
        person['actual'], person['expected'])
    [trend] = published['trend']
    assert published['trend_complete'] and (trend['publication_id'], trend['actual'], trend['expected']) == (publication, 1, 1)


@pytest.mark.parametrize('saved_kind,complete', [('same_group', True), ('changed_group', False), ('individual', False)])
def test_grouped_person_trend_never_substitutes_individual_or_changed_source_receipts(saved_kind, complete):
    person, policy = str(uuid.uuid4()), str(uuid.uuid4())
    report = {'policy_id': policy, 'line_id': None, 'line': None, 'slot': None,
              'metric': 'duties', 'unit': 'duties', 'aim': 'target', 'period': 'month', 'strength': 'medium',
              'source_goal_ids': ['first-line', 'second-line']}
    saved = {**report, 'source_goal_ids': list(reversed(report['source_goal_ids'])),
             'people': [{'person_id': person, 'actual': 3, 'expected': 4}]}
    if saved_kind == 'changed_group':
        saved['source_goal_ids'] = ['first-line', 'third-line']
    elif saved_kind == 'individual':
        saved.pop('source_goal_ids')
        saved.update(line_id=str(uuid.uuid4()), line=0, slot='target')
    publication = SimpleNamespace(id=str(uuid.uuid4()), workspace_id=str(uuid.uuid4()),
                                  starts_on=date(2026, 10, 1), ends_on=date(2026, 11, 1),
                                  effective_until=None, goals=[saved])
    responses = iter([SimpleNamespace(all=lambda: [publication]), []])
    db = SimpleNamespace(execute=lambda _query: next(responses))
    [trend] = published_trends(db, str(uuid.uuid4()), person, [report], date(2026, 10, 1), date(2026, 11, 1))
    assert trend['trend_complete'] is complete
    assert len(trend['trend']) == int(complete)
    if complete:
        assert (trend['trend'][0]['actual'], trend['trend'][0]['expected']) == (3, 4)


@pytest.mark.parametrize('role', ROLE_NAMES)
def test_person_fairness_permissions_follow_existing_fairness_readers(h, role):
    a, b, _ = h.seed['people']
    if role == 'programme_lead':
        identity(h, role, [role])
    permitted = role in {'admin', 'coordinator', 'approver', 'auditor', 'programme_lead', 'staff'}
    own = h.get(role, endpoint(a), status=200 if permitted else 403)
    if permitted:
        assert own['workspace'] is None and own['goals'] == []
    h.get(role, endpoint(b), status=403 if role in {'staff', 'credentialer', 'operator'} else 200)


@pytest.mark.parametrize('role', ROLE_NAMES)
def test_person_fairness_peer_standing_role_matrix(h, role):
    a, b, _ = h.seed['people']
    if role == 'programme_lead':
        identity(h, role, [role])
    goal = scalar_goal(h, aim='target', amount=1, period='rota')
    w = h.shift(h.workspace(), people=[a, b])['workspace']
    h.publish(w)
    permitted = role in {'admin', 'coordinator', 'approver', 'auditor', 'programme_lead', 'staff'}
    result = h.get(role, endpoint(a), status=200 if permitted else 403)
    if not permitted:
        return
    [report] = [report for report in result['goals'] if report['policy_id'] == goal['id']]
    [person] = report['people']
    peers = role in {'admin', 'coordinator', 'approver', 'auditor'}
    assert result['peers_visible'] is peers
    assert person['person_id'] == a
    if peers:
        assert person['rank'] == 1 and person['peers'] == person['peer_count'] == 3
    else:
        assert not {'rank', 'peers', 'peer_count'} & person.keys()
        assert report['rank_basis'] is None and report['score'] is None


def test_person_fairness_block_peers_share_the_same_resolved_period(h, monkeypatch):
    from apps.api.services import person_fairness
    a, b, c = h.seed['people']
    goal = scalar_goal(h, aim='target', amount=1, period='block')
    w = h.shift(h.workspace(), people=[a, b])['workspace']
    source = h.snapshot(w)
    source['rotations'] = [{'person_id': pid, 'starts_on': start, 'ends_on': '2026-11-01'}
                           for pid, start in ((a, '2026-10-01'), (b, '2026-10-01'), (c, '2026-10-02'))]
    monkeypatch.setattr(person_fairness, 'domain_snapshot', lambda *args, **kwargs: source)
    [report] = [report for report in h.get('coordinator', endpoint(a, '?workspace_id=' + w['id']))['goals']
                if report['policy_id'] == goal['id']]
    [person] = report['people']
    assert (report['starts_on'], report['ends_on']) == ('2026-10-01', '2026-11-01')
    assert person['rank'] == 1 and person['peers'] == person['peer_count'] == 2
    [other] = [report for report in h.get('coordinator', endpoint(c, '?workspace_id=' + w['id']))['goals']
               if report['policy_id'] == goal['id']]
    assert other['starts_on'] == '2026-10-02' and other['people'][0]['peers'] == 1


def test_person_fairness_pair_rank_compares_the_same_partner_only(h):
    a, b, c = h.seed['people']
    goal = scalar_goal(h, metric='pair_exposure', aim='min_per_pair', amount=2, period='rota',
                partners={'conditions': [{'field': 'person_ids', 'values': [a, b, c]}]})
    w = h.shift(h.workspace(), people=[a, b])['workspace']
    [report] = [report for report in h.get('coordinator', endpoint(a, '?workspace_id=' + w['id']))['goals']
                if report['policy_id'] == goal['id']]
    assert report['people'] == []
    assert {row['partner_id'] for row in report['pairs']} == {b, c}
    assert all(row['rank'] == 1 and row['peers'] == row['peer_count'] == 2 for row in report['pairs'])
    [other] = [report for report in h.get('coordinator', endpoint(c, '?workspace_id=' + w['id']))['goals']
               if report['policy_id'] == goal['id']]
    assert all(row['rank'] == 2 and row['peers'] == 2 for row in other['pairs'])


def test_staff_fairness_has_own_published_trend_without_peer_standing_or_drafts(h):
    a, b, _ = h.seed['people']
    goal = scalar_goal(h, period='rota')
    w = h.shift(h.workspace(), people=[a, b])['workspace']
    published = h.publish(w)
    result = h.get('staff', endpoint(a))
    [report] = [report for report in result['goals'] if report['policy_id'] == goal['id']]
    assert report['policy_id'] == goal['id'] and result['peers_visible'] is False
    assert report['score'] is None and report['rank_basis'] is None
    [person] = report['people']
    assert person['person_id'] == a and not {'rank', 'peers', 'peer_count'} & person.keys()
    assert report['pairs'] == [] and report['history_starts_on'] is None and report['history_ends_on'] is None
    assert report['trend'][0]['publication_id'] == published['publication_id']
    draft = h.workspace(start='2026-10-11', end='2026-10-18')
    h.get('staff', endpoint(a, '?workspace_id=' + draft['id']), status=403)
    identity(h, 'unlinked-fairness', ['staff'])
    h.get('unlinked-fairness', endpoint(a), status=403)


def test_person_pair_minimum_and_trend_reuse_saved_domain_reports(h):
    a, b, c = h.seed['people']
    goal = h.policy({'kind': 'balance_goal', 'metric': 'pair_exposure', 'aim': 'min_per_pair', 'amount': 2,
                    'period': 'rota', 'partners': {'match': 'all', 'conditions': [
                        {'field': 'person_ids', 'op': 'any', 'values': [b, c]}]}},
                    scope={'person_ids': [a]}, severity='advisory')
    w = h.shift(h.workspace(), people=[a, b])['workspace']
    expected = next(r for r in h.detail(w)['workspace']['validation']['goals'] if r['policy_id'] == goal['id'])
    published = h.publish(w)
    result = h.get('staff', endpoint(a))
    [report] = [report for report in result['goals'] if report['policy_id'] == goal['id']]
    assert report['people'] == [] and report['aim'] == 'min_per_pair'
    assert {r['partner_id'] for r in report['pairs']} == {b, c}
    for row in report['pairs']:
        saved = next(r for r in expected['pairs'] if r['partner_id'] == row['partner_id'])
        assert {k: row[k] for k in saved} == saved
        assert not {'rank', 'peers', 'peer_count'} & row.keys() and row['partner_name']
    assert {(r['partner_id'], r['actual'], r['expected']) for r in report['trend']} == {
        (r['partner_id'], r['actual'], r['expected']) for r in expected['pairs']}
    assert {r['publication_id'] for r in report['trend']} == {published['publication_id']}


def test_person_fairness_rejects_other_departments_and_invalid_period(h):
    a = h.seed['people'][0]
    with transaction(h.app.state.engine, write=True) as db:
        other = m.Department(organisation_id=h.seed['organisation_id'], name='Other synthetic department')
        outsider = m.Person(organisation_id=h.seed['organisation_id'], display_name='Other synthetic person')
        db.add_all([other, outsider])
        db.flush()
        db.add(m.Membership(person_id=outsider.id, department_id=other.id, starts_on=date(2020, 1, 1),
                            category_id=h.seed['category']))
        workspace = m.Workspace(department_id=other.id, name='Other draft', starts_on=date(2026, 10, 1),
                                ends_on=date(2026, 11, 1), created_by=h.seed['identities']['admin'],
                                last_edited_by=h.seed['identities']['admin'])
        db.add(workspace)
        db.flush()
        outsider_id, workspace_id = outsider.id, workspace.id
    h.get('admin', endpoint(outsider_id), status=404)
    h.get('admin', endpoint(a, '?workspace_id=' + workspace_id), status=404)
    h.get('admin', endpoint(a, '?starts_on=2026-11-01&ends_on=2026-10-01'), status=422)
    h.get('admin', endpoint(a, '?starts_on=2020-01-01&ends_on=2026-10-01'), status=422)


@pytest.fixture(scope='module')
def person_fairness_scale(scale_harness):
    h = scale_harness('h')
    goal = scalar_goal(h, period='rolling', period_days=84)
    scalar_goal(h, metric='hours', period='rolling', period_days=84)
    scalar_goal(h, metric='session_units', period='rolling', period_days=84)
    w = h.workspace(start='2026-10-01', end='2026-11-01')
    people = [str(uuid.uuid4()) for _ in range(150)]
    coordinator = h.seed['identities']['coordinator']
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(insert(m.Person), [{'id': p, 'organisation_id': h.seed['organisation_id'],
                                     'display_name': f'Synthetic person {n}'} for n, p in enumerate(people)])
        db.execute(insert(m.Membership), [{'person_id': p, 'department_id': h.seed['department_id'],
                                         'starts_on': date(2020, 1, 1), 'category_id': h.seed['category']} for p in people])
        shifts, assignments = [], []
        for n in range(500):
            shift_id = str(uuid.uuid4())
            start = datetime(2026, 10, 1, 5, tzinfo=timezone.utc) + timedelta(days=n % 30)
            end = start + timedelta(hours=4)
            shifts.append({'id': shift_id, 'workspace_id': w['id'], 'assignment_type_id': h.seed['type'],
                           'type_revision': 1, 'type_snapshot': {'name': 'Synthetic duty', 'definition': {}},
                           'location_id': h.seed['locations'][0], 'start': start, 'end': end})
            assignments.append({'shift_id': shift_id, 'person_id': people[n % 150], 'start': start, 'end': end})
        db.execute(insert(m.Shift), shifts)
        db.execute(insert(m.Assignment), assignments)
        for n in range(13):
            month = 9 + n
            first = date(2025 + (month - 1) // 12, (month - 1) % 12 + 1, 1)
            last = date(first.year + first.month // 12, first.month % 12 + 1, 1)
            workspace = m.Workspace(department_id=h.seed['department_id'], name='Historical synthetic rota',
                                    starts_on=first, ends_on=last, status='published', created_by=coordinator,
                                    last_edited_by=coordinator)
            db.add(workspace)
            db.flush()
            publication = m.Publication(department_id=h.seed['department_id'], workspace_id=workspace.id,
                                        sequence=n + 1, starts_on=first, ends_on=last, published_by=coordinator,
                                        snapshot={'shifts': []}, fingerprint='0' * 64,
                                        validation={'contracts': {'block_quota': METRIC_VERSION, 'programmes': 'programmes-v1'}})
            db.add(publication)
            db.flush()
            db.add(m.FairnessLedger(department_id=h.seed['department_id'], publication_id=publication.id,
                                   person_id=people[0], metric_key='goal:' + goal['id'], bucket=first,
                                   value=n, expected=5))
            if n == 12:
                db.execute(insert(m.FairnessLedger), [{'department_id': h.seed['department_id'], 'publication_id': publication.id,
                           'person_id': pid, 'metric_key': key, 'bucket': first, 'value': value}
                           for pid in people for key, value in ((f.AVAILABLE, 1), ('duties', 1), ('hours', 4), ('session_units', 1))])
    h.client('coordinator')
    queries = []

    def count(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith('SELECT'):
            queries.append(statement)

    event.listen(h.app.state.engine, 'before_cursor_execute', count)
    started = time.process_time()
    try:
        result = h.get('coordinator', endpoint(people[0], '?workspace_id=' + w['id']
                       + '&starts_on=2025-09-01&ends_on=2026-10-01'))
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', count)
    elapsed = time.process_time() - started
    return result, queries, elapsed, goal


def test_person_fairness_department_budget_and_batched_recent_trends(person_fairness_scale):
    result, queries, _, goal = person_fairness_scale
    assert len(queries) < 90, len(queries)
    [standing] = [r for r in result['goals'] if r['policy_id'] == goal['id']]
    assert standing['people'][0]['actual'] == 5 and standing['people'][0]['prior'] == 1
    assert standing['people'][0]['peers'] == standing['people'][0]['peer_count'] == 153
    assert len(standing['trend']) == 12 and standing['trend'][0]['starts_on'] == '2025-10-01'
    assert [r['actual'] for r in standing['trend']] == list(range(1, 13))


@pytest.mark.cpu_budget
def test_person_fairness_department_budget_and_batched_recent_trends_cpu_budget(person_fairness_scale):
    import coverage
    _, _, elapsed, _ = person_fairness_scale
    # This API read includes SQL/IO; retain wall time and account for CI coverage tracing.
    budget = 15 if coverage.Coverage.current() else 5
    assert elapsed < budget, (elapsed, budget)


def test_history_batches_thousand_goals_and_preserves_scalar_weekend_pair_windows(h):
    people = h.seed['people']
    w = h.workspace()
    type_id, services = h.seed['type'], h.seed['services']
    days = [date(2026, 8, 31), date(2026, 9, 5), date(2026, 9, 19), date(2026, 9, 26), date(2026, 10, 4)]
    with transaction(h.app.state.engine, write=True) as db:
        historical = m.Workspace(department_id=h.seed['department_id'], name='Synthetic fairness history',
                                starts_on=date(2026, 8, 1), ends_on=date(2026, 10, 11), status='published',
                                created_by=h.seed['identities']['coordinator'], last_edited_by=h.seed['identities']['coordinator'])
        db.add(historical)
        db.flush()
        historical_session = {'id': str(uuid.uuid4()), 'assignment_type_id': type_id,
            'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00',
            'cancelled': False, 'service_ids': services[:1], 'location_id': h.seed['locations'][0],
            'type_snapshot': {'definition': db.get(m.AssignmentType, type_id).definition}, 'participants': []}
        publication = m.Publication(department_id=h.seed['department_id'], workspace_id=historical.id,
                                    sequence=1, starts_on=historical.starts_on, ends_on=historical.ends_on,
                                    published_by=h.seed['identities']['coordinator'], fingerprint='0' * 64,
                                    snapshot={'shifts': [historical_session]},
                                    validation={'contracts': {'block_quota': METRIC_VERSION, 'programmes': 'programmes-v1'}})
        db.add(publication)
        db.flush()
        rows = []
        for n, pid in enumerate(people):
            for day in days:
                for key, value in ((f.AVAILABLE, 1), ('duties|type:' + type_id, 0.5), ('hours', 2.5),
                                   *[('days_worked|service:' + sid, 1) for sid in services]):
                    rows.append({'person_id': pid, 'other_person_id': None, 'metric_key': key, 'bucket': day, 'value': value})
                for value in (0.25, 0.5):
                    rows.append({'person_id': pid, 'other_person_id': people[(n + 1) % len(people)],
                                 'metric_key': 'pair|type:' + type_id, 'bucket': day, 'value': value})
        db.execute(insert(m.FairnessLedger), [{'department_id': h.seed['department_id'], 'publication_id': publication.id, **r}
                                            for r in rows])
    data = h.snapshot(w)
    # A real session proves the eligible audience; its empty attendance supplies no pair exposure.
    data['_fair_share_history_shifts'] = [historical_session]
    data['rotations'] = [{'person_id': pid, 'starts_on': start, 'ends_on': '2026-10-31'}
                         for pid, start in zip(people, ('2026-09-01', '2026-09-15', '2026-09-25'))]
    rules = [{'kind': 'balance_goal', 'metric': 'duties', 'aim': 'equal_share', 'period': 'block', 'assignment_type_ids': [type_id]},
             {'kind': 'balance_goal', 'metric': 'weekends', 'aim': 'equal_share', 'period': 'block', 'service_ids': services[:1]},
             {'kind': 'balance_goal', 'metric': 'pair_exposure', 'aim': 'min_per_pair', 'amount': 1,
              'period': 'block', 'assignment_type_ids': [type_id],
              'partners': {'match': 'all', 'conditions': [{'field': 'person_ids', 'op': 'any', 'values': people}]}},
             {'kind': 'balance_goal', 'metric': 'hours', 'aim': 'equal_share', 'period': 'rolling', 'period_days': 84}]
    policy_base = {'status': 'active', 'department_id': h.seed['department_id'],
                   'starts_on': '2020-01-01', 'ends_on': None}
    policies = [{**policy_base, 'id': str(uuid.uuid4()),
                 'definition': {'scope': {}, 'rule': rules[n % len(rules)]}} for n in range(1000)]
    with transaction(h.app.state.engine) as db:
        expected = {}
        # Independent ledger readers check scalar totals; empty attendance has no pair exposure.
        for policy in policies[:4]:
            rule = policy['definition']['rule']
            values, pairs = {}, defaultdict(dict)
            for pid, (start, end) in f.history_windows(data, policy).items():
                keys = f.keys_for(rule)
                if rule['metric'] == 'pair_exposure':
                    pairs[pid] = {}  # Preserve the eligible audience without inventing pair exposure.
                    continue
                else:
                    amounts = (fairness.weekends(db, h.seed['department_id'], keys, start, end, [pid], rule)
                               if rule['metric'] == 'weekends' else fairness.sums(db, h.seed['department_id'], keys, start, end, [pid]))
                    available = fairness.sums(db, h.seed['department_id'], [f.AVAILABLE], start, end, [pid])
                    values[pid] = {'value': amounts.get(pid, 0.0), 'available': available.get(pid, 0.0)}
            expected[rule['metric']] = {'people': values, 'pairs': dict(pairs)}
        queries = []

        def count(_connection, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().upper().startswith('SELECT'):
                queries.append(statement)

        event.listen(h.app.state.engine, 'before_cursor_execute', count)
        try:
            result = fairness.history(db, data, policies)
            assert fairness.history(db, data, [{**policy_base, 'id': 'no-history', 'definition': {
                'scope': {}, 'rule': {'metric': 'duties', 'aim': 'equal_share', 'period': 'rota'}}}]) == {}
        finally:
            event.remove(h.app.state.engine, 'before_cursor_execute', count)
    assert len(queries) <= 4, len(queries)
    assert set(result) == {policy['id'] for policy in policies}
    for policy in policies:
        assert result.get(policy['id'], {'people': {}, 'pairs': {}}) == expected[policy['definition']['rule']['metric']]
    assert [expected['duties']['people'][pid]['value'] for pid in people] == [1.5, 1.0, 0.5]
    assert [expected['weekends']['people'][pid]['value'] for pid in people] == [3, 2, 1]
