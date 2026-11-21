"""Person workload from effective departmental publications and shared scheduled metrics."""

from packages.domain.recurrence import inactive
from collections import defaultdict
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import defer

from apps.api import models as m
from apps.api.errors import require
from apps.api.serialization import record
from apps.api.snapshots import project_series_shifts, workspace_shifts
from packages.contracts.schemas import ConsecutiveWeekends
from packages.domain.conditions import active_rules, change_points, scope_tree, selector_sources, with_tags, scope_matches_throughout
from packages.domain.fairness import local_day, offered_amounts, weekend_of
from packages.domain.overlap import overlap_dates
from packages.domain.quotas import applicable_policies, evaluate_quotas, period_bounds, project_report, quota_contribution
from packages.domain.reports import workload
from packages.domain.time import aware, local_midnight, now, overlaps, union_hours
from packages.domain.validation import (
    ancestor_ids, exception_for, index_person_rows, person_scope_ids, policy_applies, _policy_scope_key,
    within_dates, workload_windows,
)
from .common import scoped
from .fairness import READERS, department_data, period_records, viewer
from .person_record import require_person
from .planning import adjusted_rules
from .schedule import acknowledged, current_publications, in_force_shifts, until, window
from apps.api.services.common import neutral_rota_name


def _period_data(data, first, last):
    return {**data, 'workspace': {**data['workspace'], 'starts_on': first.isoformat(), 'ends_on': last.isoformat()}}


def _applies_during(data, policy, person_id, a, b):
    zone = data['organisation']['timezone']
    sources = [policy, *selector_sources(data['policies'], scope_tree(policy['definition']['scope']))]
    points = sorted(set(change_points(data, person_id, a, b)) | {
        point for source in sources for field in ('starts_on', 'ends_on') if source.get(field)
        if a < (point := local_midnight(date.fromisoformat(source[field]), zone)) < b})
    return any(within_dates(policy, lo, hi, data['organisation']['timezone'])
               and scope_matches_throughout(data, policy['definition']['scope'], person_id, None, lo, hi)
               for lo, hi in zip(points, points[1:] + [b]))


def _totals(data, person_id, first, last):
    zone = data['organisation']['timezone']
    a, b = local_midnight(first, zone), local_midnight(last, zone)
    snapshot = {'shifts': data['shifts'], 'people': data['people'], 'workspace': data['workspace'],
                'timezone': zone, 'metric_version': 'scheduled-v2',
                'input_revision': data['organisation']['input_revision'],
                'availability_revision': data['organisation']['availability_revision']}
    rows = workload(snapshot, person_id, a, b)['rows']
    row = rows[0] if rows else {}
    policy = {'definition': {'scope': {}, 'rule': {'metric': 'session_units', 'assignment_type_ids': [],
                                                'participation': [], 'overtime': 'include'}}}
    units = 0.0
    for shift in data['shifts']:
        if inactive(shift):
            continue
        for participant in shift['participants']:
            if participant['person_id'] == person_id:
                contribution = quota_contribution(data, policy, person_id, shift, participant, [], a, b)
                if contribution:
                    units += contribution['session_units']
    return {'duties': row.get('assignments', 0), 'hours': row.get('scheduled_hours', 0),
            'reserved_hours': row.get('reserved_hours', 0), 'session_units': round(units, 4),
            'overtime_duties': row.get('planned_overtime_assignments', 0),
            'overtime_hours': row.get('planned_overtime_hours', 0),
            'overtime_offered_hours': offered_amounts(data, first, last, people=[person_id]).get(person_id, 0.0),
            'lead_turns': row.get('lead_turns', 0),
            'nights': None}


def _weekends(data, person_id, first, last):
    data = {**data, 'policies': active_rules(data['policies'], context=data)}
    zone = data['organisation']['timezone']
    a, b = local_midnight(first, zone), local_midnight(last, zone)
    definitions = set()
    defaults = ConsecutiveWeekends(kind='max_consecutive_weekends')
    for policy in data['policies']:
        rule = policy['definition']['rule']
        if (rule['kind'] == 'max_consecutive_weekends' or rule.get('metric') == 'weekends') and (
                _applies_during(data, policy, person_id, a, b)):
            definitions.add((rule.get('weekend_start', defaults.weekend_start), rule.get('weekend_days', defaults.weekend_days)))
    definitions = definitions or {(defaults.weekend_start, defaults.weekend_days)}
    days = {local_day(p['start'], zone) for shift in data['shifts'] if not inactive(shift)
            for p in shift['participants'] if p['person_id'] == person_id and first <= local_day(p['start'], zone) < last}
    return [{'weekend_start': start, 'weekend_days': length,
             'value': len({weekend_of(day, {'weekend_start': start, 'weekend_days': length}) for day in days
                           if weekend_of(day, {'weekend_start': start, 'weekend_days': length}) is not None})}
            for start, length in sorted(definitions)]


def _limit_usage(data, policy, person_id, entries, a, b):
    if not _applies_during(data, policy, person_id, a, b):
        return
    rule = policy['definition']['rule']
    zone = data['organisation']['timezone']
    week_start = data['organisation']['week_start']
    selected = [(s, p, c, d) for s, p, c, d in entries if policy_applies(data, policy, person_id, s, c, d)
                and (rule.get('include_overtime', True) or not p.get('overtime', False))]
    windows = workload_windows(a, b, rule, zone, week_start) | {pair for _, _, c, d in selected
               for pair in workload_windows(c, d, rule, zone, week_start) if overlaps(a, b, *pair)}
    for start, end in sorted(windows):
        if not _applies_during(data, policy, person_id, start, end):
            continue
        if rule['kind'] == 'max_hours':
            yield start, end, None, union_hours([(max(start, c), min(end, d)) for _, _, c, d in selected
                                                 if overlaps(start, end, c, d)])
        else:
            groups = defaultdict(set)
            group_by = rule.get('group_by', 'all')
            for shift, participant, c, _ in selected:
                if start <= c < end:
                    key = (participant.get('supervisor_person_id') if group_by == 'supervisor' else
                           c.astimezone(ZoneInfo(zone)).weekday() if group_by == 'weekday' else None)
                    groups[key].add(shift['id'])
            for group, duties in groups.items():
                yield start, end, group, len(duties)
            if not groups:
                yield start, end, None, 0


def _limits(data, person_id, first, last):
    """Show the actual canonical limit windows; never scale a weekly ceiling to the displayed period."""
    data = {**data, 'policies': active_rules(data['policies'], context=data)}
    zone, result = data['organisation']['timezone'], []
    a, b = local_midnight(first, zone), local_midnight(last, zone)
    entries = [(s, p, aware(p['start']), aware(p['end'])) for s in data['shifts'] if not inactive(s)
               for p in s['participants'] if p['person_id'] == person_id]
    # Selector sources and rule adjustments can differ between periods.
    usage = {}
    for policy in data['policies']:
        rule = policy['definition']['rule']
        if rule['kind'] not in {'max_hours', 'max_duties'}:
            continue
        people = person_scope_ids(policy['definition']['scope'])
        if people and person_id not in people:
            continue
        key = (_policy_scope_key(policy), rule['kind'], rule.get('window'), rule.get('window_days'),
               rule.get('group_by', 'all'), rule.get('include_overtime', True))
        if key not in usage:
            usage[key] = list(_limit_usage(data, policy, person_id, entries, a, b))
        for start, end, group, actual in usage[key]:
            result.append({'policy_id': policy['id'], 'policy_revision': policy['revision'],
                           'policy_name': policy['name'], 'kind': rule['kind'],
                           'severity': policy['definition']['severity'],
                           'strength': policy['definition'].get('strength', 'medium'),
                           'start': start.isoformat(), 'end': end.isoformat(),
                           'actual': round(actual, 4), 'maximum': rule.get('hours', rule.get('maximum')),
                           'group_by': rule.get('group_by', 'all'), 'group': group,
                           'line_id': (policy.get('template_line') or {}).get('id')})
    return result


def read(db, actor, person_id, on: date | None = None, workspace_id=None):
    viewer(actor)
    require_person(db, actor, person_id, READERS)
    department = db.get(m.Department, actor.department_id)
    organisation = db.get(m.Organisation, department.organisation_id)
    on = on or now().astimezone(ZoneInfo(organisation.timezone)).date()
    chosen = None
    if workspace_id:
        require(actor.permits(*READERS), 'FORBIDDEN', 'Drafts are visible to planners only', 403)
        chosen = scoped(db, m.Workspace, workspace_id, actor)
        require(chosen.status in {'draft', 'submitted', 'approved'}, 'VALUE_INVALID', 'Choose a current planning rota')
    base = select(m.Publication, m.Workspace).join(m.Workspace, m.Workspace.id == m.Publication.workspace_id).options(
        defer(m.Publication.snapshot), defer(m.Publication.validation)).where(
        m.Publication.department_id == actor.department_id,
        m.Publication.current.is_(True)).order_by(m.Publication.starts_on.desc()).limit(1)
    context = db.execute(base.where(m.Publication.starts_on <= on,
                                   func.coalesce(m.Publication.effective_until, m.Publication.ends_on) > on)).first()
    context = context or db.execute(base).first()
    rota, published_workspace = context if context else (None, None)
    workspace = chosen or published_workspace
    week = on - timedelta(days=(on.weekday() - organisation.week_start) % 7)
    month = on.replace(day=1)
    periods = [('this_week', week, week + timedelta(days=7)),
               ('this_month', month, date(month.year + month.month // 12, month.month % 12 + 1, 1)),
               ('rolling_12_weeks', on - timedelta(days=83), on + timedelta(days=1))]
    if workspace:
        lo, hi = window(db, chosen) if chosen else (rota.starts_on, until(rota))
        periods.insert(2, ('this_rota', lo, hi))
    first, last = min(p[1] for p in periods), max(p[2] for p in periods)
    data = department_data(db, actor, first, last, include_offers=True, workspace_id=chosen.id if chosen else None,
                           include_period_facts=False)
    data['workspace'].update(id=workspace.id if workspace else None, status=chosen.status if chosen else 'published',
                             target_overrides=workspace.target_overrides if workspace else [])
    data['policies'] = [p for p in with_tags(data)['policies'] if p['status'] == 'active']
    current_policies = data['policies']
    if chosen and chosen.rule_adjustments:
        data['policies'] = [q for q in (adjusted_rules(p, chosen.rule_adjustments)
                                       for p in data['policies']) if q is not None]
    bounds = [(local_midnight(first - timedelta(days=32), organisation.timezone),
               local_midnight(last + timedelta(days=32), organisation.timezone))]
    for _, start, end in periods:
        for policy in applicable_policies(_period_data(data, start, end)):
            try:
                bounds.append(period_bounds(policy, organisation.timezone))
            except ValueError:
                pass  # The quota evaluator returns an explicit incomplete result for an ambiguous boundary.
    start, end = min(a for a, _ in bounds), max(b for _, b in bounds)
    data['exceptions'] = [record(e) for e in db.scalars(select(m.PolicyException).where(
        m.PolicyException.department_id == actor.department_id, m.PolicyException.person_id == person_id))]
    shifts = []
    publications = current_publications(db, [actor.department_id], *overlap_dates(start, end, organisation.timezone))
    for pub in publications:
        if chosen and pub.id == chosen.base_publication_id:
            continue
        if chosen and pub.starts_on < chosen.ends_on and chosen.starts_on < until(pub):
            require(acknowledged(chosen, pub), 'SCHEDULE_OVERLAP',
                    'Resolve this draft\'s overlap with the published schedule before comparing workload', 409)
        rows = in_force_shifts(pub, pub.snapshot['shifts'], organisation.timezone,
                               chosen.starts_on if chosen and pub.id == chosen.replaces_publication_id else None)
        for shift in rows:
            # issued-v1 hours counted the envelope even if later source records gain segments.
            if pub.snapshot.get('metric_version', 'scheduled-v1') == 'scheduled-v1':
                shift = {**shift, 'participants': [{k: v for k, v in p.items() if k != 'segments'} for p in shift['participants']]}
            shifts.append(shift)
    if chosen:
        shifts.extend(project_series_shifts(db, chosen, workspace_shifts(db, chosen.id)))
    # Quota and limit windows can extend beyond the displayed periods, including edge duties' full envelopes.
    data.update(period_records(db, actor.department_id, *overlap_dates(start, end, organisation.timezone)))
    data.update(shifts=shifts, focus_ids=[s['id'] for s in shifts], quota_subjects={person_id},
                quota_context={'start': start.isoformat(), 'end': end.isoformat(), 'complete': True,
                    'publications': [{'publication_id': pub.id, 'sequence': pub.sequence, 'workspace_id': pub.workspace_id}
                                     for pub in publications if not chosen or pub.id != chosen.base_publication_id]})
    data.update(_person_rows=index_person_rows(data), _person_dates_cache={},
                _scope_interval_cache={}, _line_index_cache={})
    result = []
    for key, first, last in periods:
        period_data = _period_data(data, first, last)
        if key != 'this_rota':
            period_data['policies'] = current_policies
            period_data['workspace']['target_overrides'] = []
        period_data['policies'] = active_rules(period_data['policies'], context=period_data)
        quota = evaluate_quotas(period_data, lambda *args, **kwargs: None, ancestor_ids, exception_for)
        result.append({'key': key, 'starts_on': first.isoformat(), 'ends_on': last.isoformat(),
                       'totals': _totals(period_data, person_id, first, last),
                       'weekends': _weekends(period_data, person_id, first, last),
                       'quotas': project_report(quota, person_id)['rows'] if quota else [],
                       'limits': _limits(period_data, person_id, first, last), 'complete': quota['complete'] if quota else True})
    return {'person_id': person_id, 'on': on.isoformat(), 'timezone': organisation.timezone,
            'basis': 'Planned departmental work only. Hours split at local boundaries; duties count at their start. '
                     'Current publications only; a selected draft replaces its acknowledged publication. '
                     'Rota adjustments and target overrides apply only to the rota comparison. '
                     'Limit windows use reserved duty envelopes. External work, attendance and payroll are excluded.',
            'rule_basis': 'current_rules', 'unsupported_metrics': ['nights'],
            'workspace': ({'id': workspace.id, 'name': neutral_rota_name(workspace.name), 'status': workspace.status,
                           'starts_on': workspace.starts_on.isoformat(), 'ends_on': (hi if chosen else until(rota)).isoformat()}
                          if workspace else None), 'periods': result}
