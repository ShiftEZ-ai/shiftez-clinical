"""One person's fairness standing, from the shared goal reports and publication ledger."""
from collections import Counter, defaultdict
from datetime import date

from sqlalchemy import select

from apps.api import models as m
from apps.api.errors import require
from apps.api.snapshots import domain_snapshot
from packages.domain import fairness as f
from packages.domain.conditions import counting_key
from . import fairness
from .common import scoped
from .person_record import require_person
from apps.api.services.common import neutral_rota_name

TREND_LIMIT = 12


def standing(db, actor, person_id, workspace_id=None, starts_on=None, ends_on=None):
    fairness.viewer(actor)
    require_person(db, actor, person_id, fairness.READERS)
    peers = actor.permits(*fairness.ALL_ROLES, 'auditor')
    first_trend, last_trend = fairness.period(starts_on, ends_on)
    if workspace_id:
        require(fairness.sees_everyone(actor), 'FORBIDDEN', 'Drafts are visible to planners only', 403)
        workspace = scoped(db, m.Workspace, workspace_id, actor)
    else:
        latest = db.scalar(select(m.Publication).where(m.Publication.department_id == actor.department_id,
                           m.Publication.current.is_(True)).order_by(m.Publication.starts_on.desc(),
                                                                    m.Publication.sequence.desc()).limit(1))
        workspace = db.get(m.Workspace, latest.workspace_id) if latest else None
    if workspace is None:
        return {'person_id': person_id, 'peers_visible': peers, 'workspace': None, 'goals': []}
    # As on the fairness dashboard, standing uses today's active goals; publication trends retain their recorded aims.
    data = domain_snapshot(db, workspace, current_scheduling=True)
    from packages.domain.validation import index_person_rows
    data['_person_rows'] = index_person_rows(data)
    data['_policy_applies_cache'] = {}
    data['_scope_interval_cache'] = {}
    data['_person_dates_cache'] = {}
    data['_line_index_cache'] = {}
    data['_fair_share_eligibility'] = {}
    data['_distribution_cache'] = {}
    data['_workload_normalization_cache'] = {}
    first, last = f.rota_dates(data)
    reports = []
    groups = f.goal_groups(data)
    goals = [(report, f.comparison_goal(group)) for group, report in zip(groups, f.goal_reports(data, groups))]
    goals += [(report, None) for report in f.quota_goal_reports(data)]
    for report, policy in goals:
        people = [r for r in report['people'] if r['person_id'] == person_id]
        pairs = [r for r in report['pairs'] if r['person_id'] == person_id]
        if not people and not pairs:
            continue
        if policy is None:
            start, end = (date.fromisoformat(str(report[k])) for k in ('starts_on', 'ends_on'))
            history_window = None
        else:
            start, end = f.goal_period(data, policy, person_id)
            history_window = f.history_windows(data, policy).get(person_id)
        grouped, periods = defaultdict(list), {}
        for row in report['people'] + report['pairs']:
            pid = row['person_id']
            if pid not in periods:
                periods[pid] = f.goal_period(data, policy, pid) if policy else (start, end)
            grouped[row.get('partner_id'), periods[pid]].append(row)

        def personal(row):
            if not peers:
                return row
            others = grouped[row.get('partner_id'), periods[row['person_id']]]
            return {**row, 'rank': 1 + sum(r['cost'] < row['cost'] for r in others),
                    'peers': len(others), 'peer_count': len(others)}

        public_report = {key: value for key, value in report.items()
                         if key not in {'workload_spread', 'value', 'score_present'}}
        reports.append({**public_report, 'score': report['score'] if peers else None,
                        'starts_on': start.isoformat(), 'ends_on': end.isoformat(),
                        'history_starts_on': history_window[0].isoformat() if history_window else None,
                        'history_ends_on': history_window[1].isoformat() if history_window else None,
                        'rank_basis': 'cost' if peers else None,
                        'people': [personal(r) for r in people],
                        'pairs': [{**personal(r), 'partner_name': data['people'].get(r['partner_id'], {}).get('display_name', 'Someone')}
                                  for r in pairs], 'trend': [], 'trend_complete': True})
    if reports:
        trends = published_trends(db, actor.department_id, person_id, reports, first_trend, last_trend)
        for report, trend in zip(reports, trends):
            report.update(trend)
    return {'person_id': person_id, 'peers_visible': peers,
            'workspace': {'id': workspace.id, 'name': neutral_rota_name(workspace.name), 'status': workspace.status,
                          'starts_on': first.isoformat(), 'ends_on': last.isoformat()}, 'goals': reports}


def published_trends(db, department_id, person_id, reports, first, last):
    """Saved SSOT reports identify goal copies; old receipts and ledger rows are used only when unambiguous."""
    P, L = m.Publication, m.FairnessLedger
    publications = db.execute(select(P.id, P.workspace_id, P.starts_on, P.ends_on, P.effective_until,
                             P.validation['goals'].label('goals')).where(
                             P.department_id == department_id, P.current.is_(True),
                             P.starts_on >= first, P.starts_on < last).order_by(P.starts_on.desc(),
                                                                              P.sequence.desc()).limit(TREND_LIMIT)).all()
    result = [{'trend': [], 'trend_complete': True} for _ in reports]
    by_id = {p.id: p for p in publications}
    keys = sorted({'goal:' + r['policy_id'] for r in reports if r['metric'] != 'pair_exposure'})
    ledger = defaultdict(list)
    if by_id and keys:
        for row in db.execute(select(L.publication_id, L.metric_key, L.value, L.expected).where(
                L.department_id == department_id, L.person_id == person_id, L.publication_id.in_(by_id), L.metric_key.in_(keys))):
            ledger[row.publication_id, row.metric_key[5:]].append(row)

    def identity(report):
        return tuple(report.get(k) for k in ('policy_id', 'line_id', 'slot', 'metric', 'unit')) + (
            tuple(sorted(report.get('source_goal_ids') or [])), counting_key(report))

    def metadata(report):
        return tuple(report.get(k) for k in ('policy_id', 'metric', 'aim', 'period', 'strength')) + (
            tuple(sorted(report.get('source_goal_ids') or [])), counting_key(report))

    identities = Counter(identity(r) for r in reports)
    strengths = Counter((identity(r), r['strength']) for r in reports)
    descriptions = Counter(metadata(r) for r in reports)
    policy_counts = Counter(r['policy_id'] for r in reports)
    for publication in publications:
        exact, legacy = defaultdict(list), defaultdict(list)
        stored_policies, grouped_policies, person_policies = set(), set(), set()
        for saved in publication.goals or []:
            stored_policies.add(saved['policy_id'])
            if not any(r['person_id'] == person_id for r in saved.get('people', []) + saved.get('pairs', [])):
                continue
            person_policies.add(saved['policy_id'])
            if saved.get('source_goal_ids'):
                grouped_policies.add(saved['policy_id'])
            if saved.get('source_goal_ids') or all(k in saved for k in ('line_id', 'line', 'slot')):
                exact[identity(saved)].append(saved)
            else:
                legacy[metadata(saved)].append(saved)
        for report, trend in zip(reports, result):
            key = identity(report)
            matches = exact.get(key, [])
            if matches:
                # A line can split into inherited strength copies. A strength change alone never erases a unique line.
                if len(matches) > 1 or identities[key] > 1:
                    matches = [r for r in matches if r['strength'] == report['strength']]
                    unique = strengths[key, report['strength']] == 1
                else:
                    unique = True
                if not matches:
                    trend['trend_complete'] = False
                    continue
            else:
                matches = legacy.get(metadata(report), [])
                unique = descriptions[metadata(report)] == 1
            if matches:
                if len(matches) != 1 or not unique:
                    trend['trend_complete'] = False
                    continue
                saved = matches[0]
                for row in saved.get('people', []) + saved.get('pairs', []):
                    if row['person_id'] == person_id:
                        trend['trend'].append({**trend_period(publication), 'actual': row['actual'],
                                               'expected': row['expected'], 'partner_id': row.get('partner_id')})
            elif counting_key(report) and (exact.get(key[:-1] + ((),)) or legacy.get(metadata(report)[:-1] + ((),))):
                trend['trend_complete'] = False  # An older combined total cannot be split into today's buckets.
            elif report['policy_id'] in person_policies and (
                    report.get('source_goal_ids') or report['policy_id'] in grouped_policies):
                trend['trend_complete'] = False  # Across-line union totals cannot be reconstructed from individual receipts.
            elif report['policy_id'] not in stored_policies:
                # Older synthetic/legacy publications may have only ledger aims, without saved goal reports.
                rows = ledger.get((publication.id, report['policy_id']), [])
                if rows and (report.get('source_goal_ids') or report.get('quota_target')
                             or policy_counts[report['policy_id']] != 1 or len(rows) != 1):
                    trend['trend_complete'] = False
                elif rows:
                    row = rows[0]
                    trend['trend'].append({**trend_period(publication), 'actual': row.value,
                                           'expected': row.expected, 'partner_id': None})
    for trend in result:
        trend['trend'].sort(key=lambda r: (r['starts_on'], r['publication_id'], r['partner_id'] or ''))
    return result


def trend_period(publication):
    return {'publication_id': publication.id, 'workspace_id': publication.workspace_id,
            'starts_on': publication.starts_on.isoformat(), 'ends_on': publication.ends_on.isoformat(),
            'effective_until': publication.effective_until.isoformat() if publication.effective_until else None}
