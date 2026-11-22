"""The fairness ledger (written on publish, rebuilt on demand) and the fairness dashboard reads.

Pure calculations live in packages/domain/fairness.py; this module stores and queries them. Coordinators,
approvers and administrators see everyone; auditors and programme leads read everyone too (the dashboard has no
writes; the preview belongs to the rule form, which they cannot use); staff see only their own rows.
"""

from packages.domain.leadership import lead_turn_pairs
from packages.domain.recurrence import inactive
from packages.domain.time import now as clock_now
import json
import itertools
from collections import defaultdict
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, delete, func, literal, select, true, union_all
from sqlalchemy.orm import defer
from sqlalchemy.exc import DBAPIError

from apps.api import models as m
from apps.api.auth import require_role
from apps.api.errors import require
from apps.api.serialization import record
from packages.contracts import schemas as s
from packages.domain import fairness as f
from packages.domain import leadership
from packages.domain.conditions import PERSON_SCOPE_KEYS, SESSION_FIELDS, conditions_match, tag_scope, with_tags
from packages.domain.rota_summary import normalized_check
from packages.domain.rule_lines import bound_policies
from packages.domain.time import aware, local_midnight
from apps.api.snapshots import DomainSnapshot, scheduling_inputs, tag_data
from .common import scoped
from .date_sets import data as date_set_data
from apps.api.services.common import neutral_rota_name

ALL_ROLES = ("coordinator", "approver", "admin")
READERS = s.PERMISSIONS["fairness.read_team"]  # read-only: programme leads are department-wide, as on the planner
PAIR_METRICS = {'pair', 'supervised'}


def sees_everyone(actor):
    return actor.permits(*READERS)


def viewer(actor):
    require_role(actor, "staff", *READERS)
    require(sees_everyone(actor) or bool(actor.person_id), "FORBIDDEN", "Your account is not linked to a person", 403)


def _dashboard_inputs(db, actor, workspace_id):
    viewer(actor)
    P, W, L = m.Publication, m.Workspace, m.FairnessLedger
    ledger = select(func.count(L.id), func.coalesce(func.sum(L.id), 0),
                    func.max(L.id), func.max(L.bucket)).where(L.department_id == actor.department_id).subquery()
    D, O = m.Department, m.Organisation
    query = (select(*ledger.c, D.revision, O.revision, O.input_revision, O.availability_revision)
             .select_from(D).join(O, O.id == D.organisation_id).join(ledger, true())
             .where(D.id == actor.department_id))
    if workspace_id:
        options = (defer(W.validation), defer(W.review_snapshot))
        # Keep the fetched workspace alive for scoped() to run its usual visibility checks.
        revisions = db.execute(query.add_columns(W).outerjoin(
            W, and_(W.id == workspace_id, W.department_id == D.id)).options(*options)).one()
        workspace = scoped(db, W, workspace_id, actor, options=options)
        target = (workspace.id, workspace.revision, workspace.content_revision)
    else:
        workspace = None
        target = db.execute(select(
            func.count(P.id), func.coalesce(func.sum(P.revision), 0),
            func.max(P.revision), func.max(P.created_at)
        ).where(P.department_id == actor.department_id, P.current.is_(True))).one()
        revisions = db.execute(query).one()
    return (tuple(target), revisions[:4], *revisions[4:8]), workspace


def _dashboard_tag(kind, actor, values, inputs):
    from .conditional_reads import revision_etag
    return revision_etag(kind, actor.department_id, actor.identity_id, actor.person_id,
                         sorted(actor.roles), *values, *inputs)


def _dashboard_etag(db, actor, kind, *values, workspace_id=None):
    """Cheap validator for a fairness dashboard read before its domain projection."""
    inputs, _ = _dashboard_inputs(db, actor, workspace_id)
    return _dashboard_tag(kind, actor, values, inputs)


def goals_etag(db, actor, workspace_id=None, who=None, starts_on=None, ends_on=None):
    inputs, _ = _dashboard_inputs(db, actor, workspace_id)
    return _goals_tag(actor, workspace_id, who, starts_on, ends_on, inputs), inputs


def _goals_tag(actor, workspace_id, who, starts_on, ends_on, inputs):
    return _dashboard_tag("fairness-goals-v3", actor, (workspace_id, who, starts_on, ends_on), inputs)


def exposure_etag(db, actor, rows=None, cols=None, pairing='present', type_ids=None, service_ids=None,
                  starts_on=None, ends_on=None, location_ids=None, within=True, limit=None):
    first, last = period(starts_on, ends_on)
    return _dashboard_etag(db, actor, "fairness-exposure", rows, cols, pairing, type_ids, service_ids,
                           first, last, location_ids, within, limit)


def distribution_etag(db, actor, metric='duties', who=None, type_ids=None, service_ids=None,
                      starts_on=None, ends_on=None, group_by=None, location_ids=None, within=True,
                      workspace_id=None, goal_id=None):
    first, last = period(starts_on, ends_on)
    return _dashboard_etag(db, actor, "fairness-distribution", metric, who, type_ids, service_ids,
                           first, last, group_by, location_ids, within, workspace_id, goal_id,
                           workspace_id=workspace_id)


# ---- writing -------------------------------------------------------------------------------------------------

def rebuild(db, publication, *, own_shifts=None, data=None, shared_inputs=None):
    """Replace this publication's ledger rows (idempotent)."""
    from apps.api.snapshots import domain_snapshot
    workspace = db.get(m.Workspace, publication.workspace_id)
    if data is None:
        data = domain_snapshot(db, workspace, own_shifts=own_shifts, shared_inputs=shared_inputs)
    else:
        data = {**data, 'workspace': record(workspace, exclude=('validation',))}
        if publication.supersedes_id:
            # Supersession removed the predecessor's rows. Refresh this canonical history,
            # including legacy out-of-period ledger rows, without rebuilding unchanged inputs.
            data = {**data, 'fairness_history': history(db, data, f.balance_goals(data))}
    data = {**data, '_line_index_cache': {}, '_person_dates_cache': {}}
    department_id, publication_id = publication.department_id, publication.id
    rows = [{'department_id': department_id, 'publication_id': publication_id, 'person_id': pid,
             'other_person_id': other, 'metric_key': key, 'bucket': day, 'value': value, 'expected': None}
            for pid, other, key, day, value in f.ledger_facts(data)]
    for policy in f.balance_goals(data):
        report = f.goal_report(data, policy)
        rows.append({'department_id': department_id, 'publication_id': publication_id, 'person_id': None,
                     'other_person_id': None, 'metric_key': 'goal:' + f.goal_identity(policy), 'bucket': workspace.starts_on,
                     'value': report['score'], 'expected': 100.0})
        for r in report['people']:
            rows.append({'department_id': department_id, 'publication_id': publication_id, 'person_id': r['person_id'],
                         'other_person_id': None, 'metric_key': 'goal:' + f.goal_identity(policy), 'bucket': workspace.starts_on,
                         'value': r['actual'], 'expected': r['expected']})
    comparisons = [group for group in f.goal_groups(data) if len(group) > 1]
    for report in f.goal_reports(data, comparisons) + f.quota_goal_reports(data):
        if report.get('source_goal_ids') or report.get('quota_target'):
            rows.append({'department_id': department_id, 'publication_id': publication_id, 'person_id': None,
                         'other_person_id': None, 'metric_key': 'goal:' + report['goal_id'], 'bucket': workspace.starts_on,
                         'value': report['score'], 'expected': 100.0})
    return _write_ledger_rows(db, department_id, rows, delete_publication_ids=[publication.id])


def _write_ledger_rows(db, department_id, rows, *, delete_publication_ids=()):
    """The only raw ledger writer; COPY/batching and one transactional stamp stay together."""
    if delete_publication_ids:
        # Use the same direct connection path as COPY/SQLite batches; the adapter owns the one stamp below.
        db.connection().execute(m.FairnessLedger.__table__.delete().where(
            m.FairnessLedger.publication_id.in_(delete_publication_ids)))
    columns = ('department_id', 'publication_id', 'person_id', 'other_person_id', 'metric_key', 'bucket', 'value', 'expected')
    if db.bind.dialect.name == 'postgresql' and rows:
        # The same transaction and defaults; COPY streams facts without a driver round trip per row.
        statement = 'COPY fairness_ledger (' + ','.join(columns) + ') FROM STDIN (FORMAT BINARY)'
        driver = db.bind.dialect.loaded_dbapi
        try:
            with db.connection().connection.driver_connection.cursor() as cursor:
                with cursor.copy(statement) as copy:
                    copy.set_types(['text', 'text', 'text', 'text', 'text', 'date', 'float8', 'float8'])
                    for row in rows:
                        copy.write_row((row['department_id'], row['publication_id'], row['person_id'],
                                        row['other_person_id'], row['metric_key'], row['bucket'], row['value'], row.get('expected')))
        except driver.Error as error:
            # SW3-B-07: a raw-driver error (e.g. the statement timeout) must reach the shared write retry and the
            # API's database handlers as SQLAlchemy's own error, so it is a 503 "try again", never an unhandled 500.
            raise DBAPIError.instance(statement, None, error, driver.Error) from error
    elif db.bind.dialect.name == 'sqlite' and rows:
        from sqlite3 import SQLITE_LIMIT_VARIABLE_NUMBER
        # One bound statement per batch avoids per-row VM dispatch; keep transaction and cursor deadline guards.
        connection = db.connection()
        limit = connection.connection.driver_connection.getlimit(SQLITE_LIMIT_VARIABLE_NUMBER)
        batch_size = max(1, min(5000, limit // len(columns)))
        # Match the metric/day index's leading keys instead of revisiting its pages for each person.
        rows.sort(key=lambda row: (row['metric_key'], row['bucket'], row['person_id'] or '', row['other_person_id'] or ''))
        statement = 'INSERT INTO fairness_ledger (' + ','.join(columns) + ') VALUES '
        for start in range(0, len(rows), batch_size):
            values = [
                (row['department_id'], row['publication_id'], row['person_id'], row['other_person_id'],
                 row['metric_key'], row['bucket'].isoformat(), row['value'], row.get('expected'))
                for row in rows[start:start + batch_size]]
            connection.exec_driver_sql(statement + ','.join(['(?, ?, ?, ?, ?, ?, ?, ?)'] * len(values)),
                                       tuple(value for row in values for value in row))
    else:
        for chunk in (rows[k:k + 5000] for k in range(0, len(rows), 5000)):
            db.execute(m.FairnessLedger.__table__.insert(), chunk)
    from apps.api.db import bump_snapshot_input_revisions, snapshot_input_listener_classes
    organisation_id = db.get(m.Department, department_id).organisation_id
    bump_snapshot_input_revisions(db, [organisation_id],
        availability=snapshot_input_listener_classes()[m.FairnessLedger] == 'availability')
    return len(rows)


def on_publish(db, publication, trimmed=None, *, own_shifts=None, data=None, shared_inputs=None):
    """A new publication: its rows replace those of the publication it supersedes, and a rota it replaces from a date
    keeps rows only for the days still in force (`trimmed`, rebuilt first). Later publications' Balance goal rows read
    this one as history, so they are rebuilt too (oldest first, as `backfill` does)."""
    if publication.supersedes_id:
        _write_ledger_rows(db, publication.department_id, [], delete_publication_ids=[publication.supersedes_id])
    if trimmed is not None:
        rebuild(db, trimmed, shared_inputs=shared_inputs)
    rebuild(db, publication, own_shifts=own_shifts, data=data, shared_inputs=shared_inputs)
    later = select(m.Publication).where(m.Publication.department_id == publication.department_id, m.Publication.current.is_(True),
                                        m.Publication.starts_on > publication.starts_on).order_by(m.Publication.starts_on, m.Publication.sequence)
    for other in db.scalars(later).all():
        rebuild(db, other, shared_inputs=shared_inputs)


def backfill(db, department_id=None):
    """Rebuild the ledger from every current publication, oldest first (later goals read earlier history)."""
    query = select(m.Publication).where(m.Publication.current.is_(True)).order_by(m.Publication.starts_on, m.Publication.sequence)
    stale = select(m.Publication.id).where(m.Publication.current.is_(False))
    if department_id:
        query = query.where(m.Publication.department_id == department_id)
        stale = stale.where(m.Publication.department_id == department_id)
    stale_rows = list(db.execute(select(m.Publication.id, m.Publication.department_id).where(
        m.Publication.id.in_(stale))))
    for department, ids in itertools.groupby(sorted(stale_rows, key=lambda row: row.department_id), key=lambda row: row.department_id):
        _write_ledger_rows(db, department, [], delete_publication_ids=[row.id for row in ids])
    total, count = 0, 0
    for publication in db.scalars(query):
        total += rebuild(db, publication)
        count += 1
        db.flush()
    return {'publications': count, 'rows': total}


# ---- history for the validator and the solver ----------------------------------------------------------------

def ledger_measure(keys):
    """Supervision reads the supervisor side of retained supervised facts; no ledger rewrite or backfill."""
    supervision = bool(keys) and keys[0].split('|')[0] == 'supervision'
    if supervision:
        return [key.replace('supervision', 'supervised', 1) for key in keys], m.FairnessLedger.other_person_id
    return keys, m.FairnessLedger.person_id


def sums(db, department_id, keys, start, end, people, pairs=False):
    """Ledger totals per person (or per person and other person) over [start, end)."""
    if not people or start >= end:
        return {}
    if clinic_union(keys):
        totals = defaultdict(float)
        for _, pid, other, _, value in publication_facts(db, department_id, keys, start, end, people):
            totals[(pid, other) if pairs else pid] += value
        return dict(totals)
    L = m.FairnessLedger
    keys, person = ledger_measure(keys)
    columns = (person, L.other_person_id) if pairs else (person,)
    query = (select(*columns, func.sum(L.value)).where(L.department_id == department_id, L.metric_key.in_(keys),
             L.bucket >= start, L.bucket < end, person.in_(list(people))).group_by(*columns))
    return {tuple(row[:-1]) if pairs else row[0]: float(row[-1]) for row in db.execute(query)}


def clinic_union(keys):
    return len(keys) > 1 and all('|service:' in key for key in keys)


def published_snapshots(db, department_id, start, end):
    from .schedule import current_publications, in_force_snapshot

    return [(publication.id, in_force_snapshot(db, publication))
            for publication in current_publications(db, [department_id], start, end)]


def publication_facts(db, department_id, keys, start, end, people, snapshots=None, windows=None, policy=None, data=None,
                      prepared_context=None):
    """Matching published contributions, once per duty/person/partner, with complete spans when a goal needs them."""
    metric = keys[0].split('|')[0]
    rule = policy['definition']['rule'] if policy is not None else {}
    if policy is None:
        for suffix, field in (('type', 'assignment_type_ids'), ('service', 'service_ids'), ('location', 'location_ids')):
            rule[field] = [key.split('|' + suffix + ':', 1)[1] for key in keys if '|' + suffix + ':' in key]
    people, totals = set(people), defaultdict(float)
    for publication_id, snapshot in (snapshots if snapshots is not None else published_snapshots(db, department_id, start, end)):
        zone = snapshot.get('timezone') or data['organisation']['timezone']
        shared_leads = (lead_turn_pairs(snapshot) if metric == 'lead_turns' else None)
        # Match old duties against their publication's frozen place ancestry, with publication-local caches.
        context = (prepared_context if prepared_context is not None else
                   {**data, 'locations': snapshot.get('locations', data.get('locations', {})),
                    '_policy_applies_cache': {}, '_scope_interval_cache': {}, '_bucket_keys_cache': {}}) if policy is not None else data
        for shift in snapshot['shifts']:
            if inactive(shift) or (policy is None or not policy.get('_balance_sources')) and not f.counts(rule, shift):
                continue
            for participant in f.metric_participants(metric, shift):
                pid = participant.get('person_id')
                day = f.local_day(participant['start'], zone)
                if (pid not in people or not start <= day < end
                        or windows is not None and not any(a <= day < b for a, b in windows.get(pid, []))):
                    continue
                if metric == 'lead_turns' and (shift['id'], pid) not in shared_leads:
                    continue
                if policy is not None and not f.goal_applies(context, policy, pid, shift, aware(participant['start']), aware(participant['end'])):
                    continue
                if metric in PAIR_METRICS:
                    others = ({q['person_id'] for q in shift['participants'] if q.get('person_id')} - {pid}
                              if metric == 'pair' else {participant.get('supervisor_person_id')} - {None})
                    for other in sorted(others):
                        if policy is not None and not conditions_match(context, rule['partners'], other, None,
                                                                       aware(shift['start']), aware(shift['end'])):
                            continue
                        totals[publication_id, pid, other, day] += 1
                elif metric == 'days_worked':
                    totals[publication_id, pid, None, day] = 1
                else:
                    totals[publication_id, pid, None, day] += f.contribution(metric, shift, participant)
    return [(*key, round(value, 4)) for key, value in totals.items()]


def precise_history(policy, windows):
    """Whether the day ledger loses scope or effective-date detail needed by this goal."""
    from packages.domain.validation import every_day_all_day

    scope = policy['definition']['scope']
    return (bool(policy.get('_balance_sources'))
            or bool(scope.get('conditions') or any(scope.get(field) for field in PERSON_SCOPE_KEYS - {'person_id', 'person_ids', 'tags'})
                 or any(scope.get(field) or scope.get(field[:-1]) for field in SESSION_FIELDS))
            or not every_day_all_day(scope)
            or bool(policy.get('ends_on')) or policy['definition']['rule']['metric'] in {'pair_exposure', 'supervision'}
            or any(str(policy.get('starts_on') or '') > start.isoformat() for start, _ in windows.values()))


def weekends(db, department_id, keys, start, end, people, rule, windows=None):
    L = m.FairnessLedger
    seen = defaultdict(set)
    for pid, day in db.execute(select(L.person_id, L.bucket).where(L.department_id == department_id, L.metric_key.in_(keys),
                                                                   L.bucket >= start, L.bucket < end, L.person_id.in_(list(people)))):
        if windows is not None and not any(a <= day < b for a, b in windows.get(pid, [])):
            continue
        weekend = f.weekend_of(day, rule)
        if weekend:
            seen[pid].add(weekend)
    return {pid: float(len(days)) for pid, days in seen.items()}


def prepare_history_window(db, data):
    """One indexed lower-bound read; do this before availability and goal-window preparation."""
    if f.legacy_semantics(data) or 'fairness_history_start' in data:
        return
    P = m.Publication
    first, _ = f.rota_dates(data)
    earliest = db.scalar(select(func.min(P.starts_on)).where(
        P.department_id == data['workspace']['department_id'], P.current.is_(True), P.starts_on < first))
    data['fairness_history_start'] = str(earliest or first)


def history(db, data, policies):
    """Accumulate every requested goal in one streamed publication read and one availability read.

    Only per-goal/person aggregates and distinct weekend/distribution buckets survive a publication.
    Effective dates choose applicable goals; their measurement windows include earlier published work.
    """
    if f.legacy_semantics(data):
        return _legacy_history(db, data, policies)
    from packages.domain.validation import index_person_rows
    from packages.domain.fairness_distribution import counted_groups, numeric_groups
    from .schedule import in_force_snapshot
    from sqlalchemy.orm import defer

    prepare_history_window(db, data)
    data = {**data, '_line_index_cache': {}, '_person_dates_cache': {}, '_person_rows': index_person_rows(data),
            '_fair_share_eligibility': {}}
    department = data['workspace']['department_id']
    groups = f.goal_groups(data, policies)
    goals = {f.goal_identity(p): p for group in groups for p in group}
    goals.update({f.goal_identity(p): p for group in groups if len(group) > 1 for p in [f.comparison_goal(group)]})
    requests = {}
    for key, policy in goals.items():
        raw = f.history_windows(data, policy, preload=True)
        if not raw:
            continue
        requests[key] = {'policy': policy, 'raw': raw,
            'source': precise_history(policy, raw) or clinic_union(f.keys_for(policy['definition']['rule'])),
            'intervals': {pid: f.goal_intervals(data, policy, pid, *window) for pid, window in raw.items()},
            'people': {pid: {'value': 0.0, 'available': 0.0} for pid in raw},
            'eligible': set(f.goal_people(data, policy)['who']),
            'pairs': defaultdict(lambda: defaultdict(float)), 'weekends': defaultdict(set),
            'distribution': defaultdict(lambda: {'total': 0.0, 'selected': 0.0})}
    bounds = [window for r in requests.values() for window in r['raw'].values()]
    if not bounds:
        return {}
    start, end = min(a for a, _ in bounds), max(b for _, b in bounds)
    P = m.Publication
    query = (select(P).where(P.department_id == department, P.current.is_(True),
        P.starts_on < end, func.coalesce(P.effective_until, P.ends_on) > start)
        .options(defer(P.validation)).order_by(P.starts_on, P.sequence).execution_options(yield_per=1))
    for publication in db.scalars(query):
        snapshot = in_force_snapshot(db, publication)
        context = {**data, **snapshot.get('fairness_qualification', {}),
            'locations': snapshot.get('locations', data.get('locations', {})),
            'organisation': {**data['organisation'], 'timezone': snapshot.get('timezone') or data['organisation']['timezone']},
            '_line_index_cache': {}, '_person_dates_cache': {}, '_policy_applies_cache': {},
            '_scope_interval_cache': {}, '_bucket_keys_cache': {}, '_fair_share_eligibility': {},
            '_policy_applies_people': True}
        context['_person_rows'] = index_person_rows(context)
        context['_fair_share_history_shifts'] = snapshot['shifts']
        for request in requests.values():
            policy, raw, people = request['policy'], request['raw'], request['people']
            rule = policy['definition']['rule']
            if not any(a < publication.ends_on and publication.starts_on < b for a, b in raw.values()):
                continue
            # Eligibility is a union across publications. Once every possible subject is
            # known, later publications still contribute amounts but need no audience scan.
            if not request['raw'].keys() <= request['eligible']:
                request['eligible'].update(f.goal_people(context, policy)['who'])
            if rule.get('distribution', 'people') != 'people':
                entries = []
                for shift in snapshot['shifts']:
                    if inactive(shift):
                        continue
                    for participant in shift['participants']:
                        pid = participant.get('person_id')
                        day = f.local_day(participant['start'], context['organisation']['timezone'])
                        if pid not in raw or not raw[pid][0] <= day < raw[pid][1]:
                            continue
                        pairs = ({p['person_id']: 1 for p in shift['participants'] if p.get('person_id') != pid}
                                 if rule.get('pairing', 'present') == 'present' else {})
                        entries.append((shift, participant, 1, pairs))
                for row in numeric_groups(counted_groups(context, policy, entries, include_history=False)):
                    group = request['distribution'][row['person_id'], row['bucket'], row['partner_id']]
                    for field in ('total', 'selected'):
                        group[field] += row[field]
                continue
            for _, pid, other, day, value in (publication_facts(db, department, f.keys_for(rule), start, end, raw,
                    snapshots=[(publication.id, snapshot)], windows={pid: [window] for pid, window in raw.items()},
                    policy=policy, data=context, prepared_context=context) if request['source'] else []):
                request['eligible'].add(pid)
                if rule['metric'] == 'weekends':
                    weekend = f.weekend_of(day, rule)
                    if weekend:
                        request['weekends'][pid].add(weekend)
                elif rule['metric'] == 'pair_exposure':
                    request['pairs'][pid][other] += value
                else:
                    people[pid]['value'] += value
            if rule.get('share_by') == 'supervisable_sessions':
                for pid, value in f.supervisable_amounts(context, policy, people,
                        shifts=snapshot['shifts'], windows=raw).items():
                    people[pid]['supervisable_sessions'] = people[pid].get('supervisable_sessions', 0) + value
                    if value:
                        request['eligible'].add(pid)
    # The persisted daily availability is historical evidence, including leave and dated membership.
    # Aggregate as it streams instead of retaining years of per-person/day ledger rows.
    audience = defaultdict(list)
    ledger_keys = {f.AVAILABLE}
    for request in requests.values():
        rule = request['policy']['definition']['rule']
        if rule.get('distribution', 'people') == 'people':
            keys = set() if request['source'] else set(f.keys_for(rule))
            ledger_keys.update(keys)
            for pid, intervals in request['intervals'].items():
                if pid in request['eligible'] and intervals:
                    audience[pid].append((request, intervals, keys))
    L = m.FairnessLedger
    if audience:
        query = select(L.metric_key, L.person_id, L.bucket, func.sum(L.value)).where(
            L.department_id == department, L.metric_key.in_(ledger_keys), L.bucket >= start, L.bucket < end,
            L.person_id.in_(audience)).group_by(L.metric_key, L.person_id, L.bucket).execution_options(yield_per=1000)
        for key, pid, day, value in db.execute(query):
            for request, intervals, keys in audience[pid]:
                if not any(a <= day < b for a, b in intervals):
                    continue
                if key == f.AVAILABLE:
                    request['people'][pid]['available'] += float(value)
                elif key in keys:
                    rule = request['policy']['definition']['rule']
                    if rule['metric'] == 'weekends':
                        weekend = f.weekend_of(day, rule)
                        if weekend:
                            request['weekends'][pid].add(weekend)
                    else:
                        request['people'][pid]['value'] += float(value)
    result = {}
    for key, request in requests.items():
        rule = request['policy']['definition']['rule']
        if rule.get('distribution', 'people') != 'people':
            result[key] = {'distribution': [dict(person_id=pid, bucket=bucket, partner_id=partner, **values)
                for (pid, bucket, partner), values in request['distribution'].items()]}
            continue
        people = {pid: row for pid, row in request['people'].items() if pid in request['eligible']}
        for pid, days in request['weekends'].items():
            people[pid]['value'] = len(days)
        if rule['metric'] in ('overtime_duties', 'overtime_hours'):
            intervals = defaultdict(list)
            for pid in people:
                people[pid]['offered'] = 0.0
                for window in request['intervals'][pid]:
                    intervals[window].append(pid)
            for (a, b), who in intervals.items():
                for pid, value in f.offered_amounts(data, a, b, people=who).items():
                    people[pid]['offered'] += value
        result[key] = {'people': people if rule['metric'] != 'pair_exposure' else {},
                       'pairs': {pid: dict(request['pairs'].get(pid, {})) for pid in request['eligible']}
                       if rule['metric'] == 'pair_exposure' else {}}
    return result


def _legacy_history(db, data, policies):
    """data['fairness_history']: per goal, each person's ledger value and available days before this rota."""
    from packages.domain.validation import index_person_rows
    data = {**data, '_line_index_cache': {}, '_person_dates_cache': {}, '_person_rows': index_person_rows(data),
            '_fair_share_eligibility': {}}
    department = data['workspace']['department_id']
    first, _ = f.rota_dates(data)
    bounds = [window for policy in policies if policy['definition']['rule']['metric'] != 'workload'
              for pid in data['people'] for start, end in [f.goal_period(data, policy, pid)]
              for window in [(start, min(first, end))] if window[0] < window[1]]
    modern = all('active' in person for person in data['people'].values())
    snapshots = (published_snapshots(db, department, min(a for a, _ in bounds), max(b for _, b in bounds))
                 if modern and bounds else None)
    data['_fair_share_history_shifts'] = [
        {**shift, '_fair_share_locations': snapshot.get('locations', data.get('locations', {}))}
        for _, snapshot in snapshots or [] for shift in snapshot['shifts'] if not inactive(shift)]
    requests, distributions = [], []
    groups = f.goal_groups(data, policies)
    comparisons = [f.comparison_goal(group) for group in groups if len(group) > 1]
    sources = {f.goal_identity(p): p for group in groups for p in group}
    for policy in [*sources.values(), *comparisons]:
        raw = f.history_windows(data, policy)
        if policy['definition']['rule'].get('distribution', 'people') != 'people':
            distributions.append((policy, raw))
            continue
        requests.append((policy, raw, {pid: f.goal_intervals(data, policy, pid, *window) for pid, window in raw.items()}))
    ledger_windows = [window for _, _, requested in requests for intervals in requested.values() for window in intervals]
    indexed = defaultdict(list)
    if ledger_windows:
        ledger_keys = {key for policy, raw, requested in requests if any(requested.values())
                       and not (precise_history(policy, raw) or clinic_union(f.keys_for(policy['definition']['rule'])))
                       for key in f.keys_for(policy['definition']['rule'])}
        if any(any(requested.values()) and policy['definition']['rule']['metric'] != 'pair_exposure'
               for policy, _, requested in requests):
            ledger_keys.add(f.AVAILABLE)
        ledger_people = {pid for _, _, requested in requests for pid, intervals in requested.items() if intervals}
        if ledger_keys:
            L = m.FairnessLedger
            for key, pid, other, day, value in db.execute(select(L.metric_key, L.person_id, L.other_person_id, L.bucket, func.sum(L.value))
                    .where(L.department_id == department, L.metric_key.in_(ledger_keys), L.person_id.in_(ledger_people),
                           L.bucket >= min(start for start, _ in ledger_windows), L.bucket < max(end for _, end in ledger_windows))
                    .group_by(L.metric_key, L.person_id, L.other_person_id, L.bucket)):
                indexed[key, pid].append((day, other, float(value)))
    cache = {}

    def total(keys, pid, intervals, rule=None, pairs=False):
        keys, intervals = tuple(sorted(set(keys))), tuple(intervals)
        weekend = (rule.get('weekend_start', 4), rule.get('weekend_days', 2)) if rule else None
        signature = (keys, pid, intervals, weekend, pairs)
        if signature not in cache:
            rows = (row for key in keys for row in indexed.get((key, pid), ()) if any(start <= row[0] < end for start, end in intervals))
            if pairs:
                amounts = defaultdict(float)
                for _, other, value in rows:
                    amounts[other] += value
                cache[signature] = dict(amounts)
            elif rule:
                days = {f.weekend_of(day, rule) for day, _, _ in rows}
                cache[signature] = float(len(days - {None}))
            else:
                cache[signature] = sum((value for _, _, value in rows), 0.0)
        return cache[signature]

    result = {}
    for policy, raw, requested in requests:
        rule = policy['definition']['rule']
        windows = defaultdict(list)
        for pid, intervals in requested.items():
            for window in intervals:
                windows[window].append(pid)
        people, pairs = {}, defaultdict(dict)
        keys = f.keys_for(rule)
        precise = precise_history(policy, raw)
        source = precise or clinic_union(keys)
        source_values, source_pairs, source_weekends = defaultdict(float), defaultdict(float), defaultdict(set)
        if raw and source:
            if snapshots is None:
                ranges = [window for goal, raw_windows, _ in requests
                          if precise_history(goal, raw_windows) or clinic_union(f.keys_for(goal['definition']['rule']))
                          for window in raw_windows.values()]
                snapshots = published_snapshots(db, department, min(a for a, _ in ranges), max(b for _, b in ranges))
            start, end = min(a for a, _ in raw.values()), max(b for _, b in raw.values())
            for _, pid, other, day, value in publication_facts(db, department, keys, start, end, raw, snapshots=snapshots,
                    windows={pid: [window] for pid, window in raw.items()}, policy=policy, data=data):
                if rule['metric'] == 'weekends':
                    weekend = f.weekend_of(day, rule)
                    if weekend:
                        source_weekends[pid].add(weekend)
                elif rule['metric'] == 'pair_exposure':
                    source_pairs[pid, other] += value
                else:
                    source_values[pid] += value
            if rule['metric'] != 'pair_exposure':
                people = {pid: {'value': 0.0, 'available': 0.0} for pid in raw}
            else:
                pairs.update({pid: {} for pid in raw})
        weekend_values = ({pid: total(keys, pid, intervals, rule=rule) for pid, intervals in requested.items() if intervals}
                          if windows and rule['metric'] == 'weekends' and not source else {})
        for (start, end), who in windows.items():
            if rule['metric'] == 'pair_exposure':
                if not source:
                    for pid in who:
                        for other, value in total(keys, pid, [(start, end)], pairs=True).items():
                            pairs[pid][other] = pairs[pid].get(other, 0.0) + value
                continue
            values = {} if source or rule['metric'] == 'weekends' else {pid: total(keys, pid, [(start, end)]) for pid in who}
            available = {pid: total([f.AVAILABLE], pid, [(start, end)]) for pid in who}
            offered = f.offered_amounts(data, start, end, people=who) if rule['metric'] in ('overtime_duties', 'overtime_hours') else {}
            for pid in who:
                entry = people.setdefault(pid, {'value': 0.0, 'available': 0.0})
                entry['value'] += values.get(pid, 0.0)
                entry['available'] += available.get(pid, 0.0)
                if rule['metric'] in ('overtime_duties', 'overtime_hours'):
                    entry['offered'] = entry.get('offered', 0.0) + offered.get(pid, 0.0)
        for (pid, other), value in source_pairs.items():
            pairs[pid][other] = value
        for pid, days in source_weekends.items():
            source_values[pid] = len(days)
        for pid, value in (weekend_values if rule['metric'] == 'weekends' and not source else source_values).items():
            people[pid]['value'] = value
        if people or pairs:
            result[f.goal_identity(policy)] = {'people': people, 'pairs': dict(pairs)}
    bounds = [window for _, requested in distributions for window in requested.values()]
    if bounds:
        from packages.domain.fairness_distribution import counted_groups, numeric_groups
        published = published_snapshots(db, department, min(a for a, _ in bounds), max(b for _, b in bounds))
        for policy, requested in distributions:
            totals = defaultdict(lambda: {'total': 0.0, 'selected': 0.0})
            for _, snapshot in published:
                context = {**data, 'locations': snapshot.get('locations', data.get('locations', {})),
                           'organisation': {**data['organisation'], 'timezone': snapshot.get('timezone') or data['organisation']['timezone']},
                           '_policy_applies_cache': {}, '_scope_interval_cache': {}, '_bucket_keys_cache': {}}
                entries = []
                for shift in snapshot['shifts']:
                    for participant in shift['participants']:
                        pid = participant.get('person_id')
                        day = f.local_day(participant['start'], context['organisation']['timezone'])
                        if pid not in requested or not requested[pid][0] <= day < requested[pid][1]:
                            continue
                        pairs = ({p['person_id']: 1 for p in shift['participants'] if p.get('person_id') != pid}
                                 if policy['definition']['rule'].get('pairing', 'present') == 'present' else {})
                        entries.append((shift, participant, 1, pairs))
                for row in numeric_groups(counted_groups(context, policy, entries, include_history=False)):
                    group = totals[row['person_id'], row['bucket'], row['partner_id']]
                    for field in ('total', 'selected'):
                        group[field] += row[field]
            if totals:
                result[f.goal_identity(policy)] = {'distribution': [
                    {'person_id': pid, 'bucket': bucket, 'partner_id': partner, **values}
                    for (pid, bucket, partner), values in totals.items()]}
    return result


# ---- dashboard reads -----------------------------------------------------------------------------------------

def period_records(db, department_id, first, last):
    return {name: [record(r) for r in db.scalars(select(cls).where(cls.department_id == department_id,
                cls.starts_on < last, cls.ends_on.is_(None) | (cls.ends_on > first)))]
            for name, cls in {'memberships': m.Membership, 'eligibilities': m.Eligibility,
                              'grants': m.CapabilityGrant, 'rotations': m.Rotation,
                              'programme_memberships': m.ProgrammeMembership}.items()}


def department_data(db, actor, first, last, *, include_offers=False, workspace_id=None, include_period_facts=True):
    """The records the condition evaluator needs, for conditions over [first, last) without a rota."""
    department = db.get(m.Department, actor.department_id)
    org = db.get(m.Organisation, department.organisation_id)
    data = DomainSnapshot({'organisation': record(org), 'department': record(department),
            'workspace': {'department_id': department.id, 'starts_on': first.isoformat(), 'ends_on': last.isoformat()},
            'people': {r.id: record(r) for r in db.scalars(select(m.Person).where(m.Person.organisation_id == org.id))},
            'policies': [], 'shifts': [], 'focus_ids': [], 'availability': []})
    for name, cls in {'categories': m.Category, 'capabilities': m.Capability, 'programmes': m.Programme,
                      'rotation_types': m.RotationType, 'cohorts': m.Cohort, 'services': m.Service, 'types': m.AssignmentType,
                      'locations': m.Location, 'operating_calendars': m.OperatingCalendar}.items():
        data[name] = {r.id: record(r) for r in db.scalars(select(cls).where(cls.department_id == department.id))}
    if include_period_facts:
        data.update(period_records(db, department.id, first, last))
    data['tags'] = tag_data(db, [department.id])
    data['date_sets'] = date_set_data(db, [department.id])
    if include_offers:
        data.update(scheduling_inputs(db, [department.id], include_offer_history=True))
        data['policies'] = [record(p) for p in db.scalars(select(m.Policy).where(m.Policy.department_id == department.id))]
        from packages.domain.rule_lines import read_quality_for
        data['policies'] = bound_policies(data['policies'], data, read_quality=read_quality_for(data))
        data['_catalogue_v2_materialized'] = True
        data['policies'] = [p for p in data['policies'] if p['definition'].get('workspace_id') in (None, workspace_id)]
        lo, hi = local_midnight(first, org.timezone), local_midnight(last, org.timezone)
        data['availability'] = [record(r, exclude=('private_reason',)) for r in db.scalars(select(m.Availability).where(
            m.Availability.person_id.in_(data['people']), m.Availability.start < hi, m.Availability.end > lo,
            m.Availability.retracted.is_(False), m.Availability.counts_as_unavailable.is_(True)))]
        from .availability_patterns import expand
        data['availability'] += expand(db, [department.id], list(data['people']), lo, hi, org.timezone)
    from packages.domain.validation import index_person_rows
    data['_person_rows'] = index_person_rows(data)
    return with_tags(data)


def parse_tree(text):
    if not text:
        return None
    try:
        return s.Conditions.model_validate(json.loads(text)).model_dump(mode='json')
    except (ValueError, TypeError) as error:
        require(False, 'SCHEMA_INVALID', 'Conditions are not valid: ' + str(error)[:200])


def period(starts_on, ends_on):
    # Default: a year back and the year ahead (published rotas run ahead of today).
    last = ends_on or (starts_on or clock_now().date()) + timedelta(days=366)
    first = starts_on or last - timedelta(days=731)
    require(first < last and (last - first).days <= 3 * 366, 'VALUE_INVALID', 'Choose a period of up to three years')
    return first, last


def matching(data, tree, first, last):
    zone = data['organisation']['timezone']
    if tree:  # a tag in a filter line reads as its members, as in rules
        tree = tag_scope(data, {'conditions': tree})['conditions']
    return f.people_matching(data, tree, None, local_midnight(first, zone), local_midnight(last, zone))


def names(data, ids):
    return {pid: data['people'].get(pid, {}).get('display_name', 'Someone') for pid in ids}


def personal_goals(reports, person_id):
    """Project a personal read without changing the cached departmental reports."""
    return [{**{key: value for key, value in report.items()
                if key not in {'workload_spread', 'value', 'score_present'}},
             'score': None, 'trend': [],
             'people': [row for row in report['people'] if row['person_id'] == person_id],
             'pairs': [row for row in report['pairs'] if row['person_id'] == person_id]}
            for report in reports]


def _goals_uncached(db, actor, workspace_id=None, who=None, starts_on=None, ends_on=None):
    """Each Balance goal on a rota (a draft, or the latest publication): score, per-person values against the
    expected band, and the score at each publication in the period (trend)."""
    from apps.api.snapshots import domain_snapshot
    viewer(actor)
    first, last = period(starts_on, ends_on)
    if workspace_id:
        require(sees_everyone(actor), 'FORBIDDEN', 'Drafts are visible to planners only', 403)
        workspace = scoped(db, m.Workspace, workspace_id, actor)
    else:
        department = db.get(m.Department, actor.department_id)
        organisation = db.get(m.Organisation, department.organisation_id)
        today = clock_now().astimezone(ZoneInfo(organisation.timezone)).date()
        P = m.Publication
        end = func.coalesce(P.effective_until, P.ends_on)
        current = (P.starts_on <= today) & (end > today)
        upcoming = P.starts_on > today
        latest = db.scalar(select(P).where(P.department_id == actor.department_id, P.current.is_(True))
            .where(P.starts_on >= first, P.starts_on < last)
            .options(defer(P.snapshot), defer(P.validation))
            .order_by(case((current, 0), (upcoming, 1), else_=2),
                      case((current, P.starts_on)).desc(), case((upcoming, P.starts_on)).asc(),
                      end.desc(), P.sequence.desc()).limit(1))
        if latest is None:
            return {'workspace': None, 'goals': [], 'schedule': None, 'score_trend': []}
        workspace = db.get(m.Workspace, latest.workspace_id)
    # Today's active goals use the rota's captured settings until publication.
    goals = []
    data = domain_snapshot(db, workspace, current_goals=True, fairness_goals_out=goals)
    tree = parse_tree(who)
    allowed = set(matching(data, tree, workspace.starts_on, workspace.ends_on)) if tree else None
    if not sees_everyone(actor):
        allowed = {actor.person_id}
    L = m.FairnessLedger
    result = []
    from packages.domain.validation import index_person_rows
    data = {**data, '_line_index_cache': {}, '_person_dates_cache': {},
            '_person_rows': index_person_rows(data), '_distribution_cache': {},
            '_policy_applies_cache': {}, '_policy_applies_people': True, '_scope_interval_cache': {}}
    reports = f.goal_reports(data, groups=f.goal_groups(data, goals)) + f.quota_goal_reports(data)
    trends = defaultdict(list)
    if reports:
        for key, bucket, value in db.execute(select(L.metric_key, L.bucket, L.value).where(
                L.department_id == actor.department_id, L.metric_key.in_({'goal:' + r['goal_id'] for r in reports}),
                L.person_id.is_(None), L.bucket >= first, L.bucket < last).order_by(L.bucket, L.id)):
            trends[key].append({'starts_on': bucket.isoformat(), 'score': value})
    for report in reports:
        trend = trends['goal:' + report['goal_id']]
        people = [r for r in report['people'] if allowed is None or r['person_id'] in allowed]
        pairs = [r for r in report['pairs'] if allowed is None or r['person_id'] in allowed]
        label = names(data, {r['person_id'] for r in people + pairs} | {r['partner_id'] for r in pairs})
        result.append({**report, 'trend': trend,
                       'people': [{**r, 'name': label[r['person_id']]} for r in people],
                       'pairs': [{**r, 'name': label[r['person_id']], 'partner_name': label[r['partner_id']]} for r in pairs]})
    planner = sees_everyone(actor)
    return {'workspace': {'id': workspace.id, 'name': neutral_rota_name(workspace.name), 'status': workspace.status,
                          'starts_on': workspace.starts_on.isoformat(), 'ends_on': workspace.ends_on.isoformat()},
            'goals': result if planner else personal_goals(result, actor.person_id),
            'schedule': schedule(db, workspace) if planner else None,
            'score_trend': score_trend(db, actor.department_id, first, last) if planner else []}


def _published_goal_workspace(db, actor):
    """Resolve the published workspace anchoring the goals projection."""
    department = db.get(m.Department, actor.department_id)
    organisation = db.get(m.Organisation, department.organisation_id)
    today = clock_now().astimezone(ZoneInfo(organisation.timezone)).date()
    P = m.Publication
    end = func.coalesce(P.effective_until, P.ends_on)
    current = (P.starts_on <= today) & (end > today)
    upcoming = P.starts_on > today
    latest = db.scalar(select(P.workspace_id).where(P.department_id == actor.department_id, P.current.is_(True))
        .order_by(case((current, 0), (upcoming, 1), else_=2),
                  case((current, P.starts_on)).desc(), case((upcoming, P.starts_on)).asc(),
                  end.desc(), P.sequence.desc()).limit(1))
    return db.get(m.Workspace, latest, options=(defer(m.Workspace.validation), defer(m.Workspace.review_snapshot))) if latest else None


def goals(db, actor, workspace_id=None, who=None, starts_on=None, ends_on=None, *, validator=None):
    """Read goals through the durable scoped projection; a miss uses the canonical full calculation once."""
    viewer(actor)
    if workspace_id:
        require(sees_everyone(actor), 'FORBIDDEN', 'Drafts are visible to planners only', 403)
        inputs, workspace = _dashboard_inputs(db, actor, workspace_id)
    else:
        workspace = _published_goal_workspace(db, actor)
        if workspace is None:
            return {'workspace': None, 'goals': [], 'schedule': None, 'score_trend': []}
        inputs, _ = _dashboard_inputs(db, actor, None)
    from .read_projections import cached
    # A concurrent commit can change the projection inputs after the HTTP check.
    stamp = validator[0] if validator is not None and validator[1] == inputs else _goals_tag(
        actor, workspace_id, who, starts_on, ends_on, inputs)
    return cached(db, [workspace.id], 'fairness_goals', {workspace.id: stamp},
                  lambda _: _goals_uncached(db, actor, workspace_id, who, starts_on, ends_on))[workspace.id]


def schedule(db, workspace):
    """The rota's schedule score from its last check (None when never checked); `fresh`: nothing changed since."""
    from apps.api.snapshots import domain_snapshot
    from .planning import complete_check
    score = (normalized_check(workspace.validation) or {}).get('score')
    if not score or score['score'] is None:
        return None
    data = domain_snapshot(db, workspace)
    return {**{k: score.get(k) for k in ('score', 'publishable', 'gate', 'components', 'why')},
            'fresh': complete_check(workspace.validation, data, data['department'].get('generation_defaults'))}


def score_trend(db, department_id, first, last):
    """The schedule score of each current publication starting in [first, last)."""
    P, W = m.Publication, m.Workspace
    rows = db.execute(select(P.id, P.workspace_id, W.name, P.sequence, P.starts_on, P.ends_on, P.validation).join(W, W.id == P.workspace_id)
                      .where(P.department_id == department_id, P.current.is_(True), P.starts_on >= first, P.starts_on < last)
                      .order_by(P.starts_on, P.sequence))
    return [{'publication_id': pid, 'workspace_id': wid, 'name': neutral_rota_name(name), 'sequence': seq, 'starts_on': a.isoformat(), 'ends_on': b.isoformat(),
             'score': score['score'], 'publishable': score['publishable'], 'components': {c: v['value'] for c, v in score['components'].items()}}
            for pid, wid, name, seq, a, b, validation in rows
            for score in [(normalized_check(validation) or {}).get('score')] if score and score['score'] is not None]


def goal_filters(type_ids, service_ids, location_ids=None):
    """Count only one kind of session: these duty types, clinics or places (the ledger keeps one filter per key)."""
    require(sum(map(bool, (type_ids, service_ids, location_ids))) <= 1, 'VALUE_INVALID', 'Filter on duty types, clinics or places: one of them')
    return {'assignment_type_ids': type_ids or [], 'service_ids': service_ids or [], 'location_ids': location_ids or []}


def places(locations, ids, within=True):
    """The places a filter counts: the chosen ones, and with `within` every place inside them. An unknown place counts nothing
    (a list that is never empty, so the filter still narrows)."""
    if not ids:
        return []
    return (f.places_within(locations, ids) if within else [i for i in ids if i in locations]) or ['none']


def place_tree(db, department_id):
    return {r.id: {'parent_id': r.parent_id} for r in db.execute(select(m.Location.id, m.Location.parent_id).where(m.Location.department_id == department_id))}


def exposure(db, actor, rows=None, cols=None, pairing='present', type_ids=None, service_ids=None, starts_on=None, ends_on=None,
             location_ids=None, within=True, limit=None):
    """Sessions together (or named supervision) between the people in `rows` and `cols`, over the period."""
    viewer(actor)
    require(pairing in ('present', 'supervised'), 'VALUE_INVALID', 'Pairing is present or supervised')
    first, last = period(starts_on, ends_on)
    data = department_data(db, actor, first, last)
    row_ids = matching(data, parse_tree(rows), first, last)
    col_ids = matching(data, parse_tree(cols), first, last)
    if not sees_everyone(actor):
        row_ids = [p for p in row_ids if p == actor.person_id]
    rule = {'metric': 'pair_exposure', 'pairing': pairing, **goal_filters(type_ids, service_ids, places(data['locations'], location_ids, within))}
    values = sums(db, actor.department_id, f.keys_for(rule), first, last, row_ids, pairs=True)
    label = names(data, set(row_ids) | set(col_ids))
    order = lambda ids: sorted(ids, key=lambda pid: (label[pid], pid))
    ordered_rows, ordered_cols = order(row_ids), order(col_ids)
    row_ids, col_ids = set(row_ids), set(col_ids)
    row_totals, col_totals = defaultdict(float), defaultdict(float)
    for (row, col), value in values.items():
        if row in row_ids and col in col_ids:
            row_totals[row] += value
            col_totals[col] += value
    if limit is not None and (len(row_ids) > limit or len(col_ids) > limit):
        # Rank against the complete opposite axis; stable sorting keeps the full matrix's name/id tie order.
        ordered_rows = sorted(ordered_rows, key=lambda pid: -row_totals[pid])[:limit]
        ordered_cols = sorted(ordered_cols, key=lambda pid: -col_totals[pid])[:limit]
    return {'starts_on': first.isoformat(), 'ends_on': last.isoformat(), 'pairing': pairing,
            'total_rows': len(row_ids), 'total_cols': len(col_ids),
            'row_totals': [row_totals[p] for p in ordered_rows],
            'rows': [{'person_id': p, 'name': label[p]} for p in ordered_rows],
            'cols': [{'person_id': p, 'name': label[p]} for p in ordered_cols],
            'cells': [[values.get((r, c), 0.0) for c in ordered_cols] for r in ordered_rows]}


# Group by who people are (anyone matching the value at some moment in the period) ...
GROUPS = {'category_ids': 'categories', 'capability_ids': 'capabilities', 'programme_ids': 'programmes',
          'rotation_type_ids': 'rotation_types', 'cohort_ids': 'cohorts', 'eligible_service_ids': 'services', 'tags': None}
# ... or by what the work was (the ledger's filtered keys): the catalogue and the key's filter name.
SESSION_GROUPS = {'service_ids': ('services', 'service'), 'assignment_type_ids': ('types', 'type'), 'location_ids': ('locations', 'location')}
FILTER_NAMES = {'assignment_type_ids': 'duty types', 'service_ids': 'clinics', 'location_ids': 'places'}


def metric_rule(metric, type_ids, service_ids, pairing='present', location_ids=None):
    """The rule dict a dashboard read sums (the same keys a Balance goal sums)."""
    known = metric in f.LEDGER_KEY or metric in f.WISH_METRICS or metric == 'pair_exposure'
    require(known, 'VALUE_INVALID', 'Unknown measure')
    require(metric not in f.WISH_METRICS or not (type_ids or service_ids or location_ids), 'VALUE_INVALID',
            'Preferences are not counted per duty type, clinic or place')
    return {'metric': metric, 'pairing': pairing, **goal_filters(type_ids, service_ids, location_ids)}


def distribution(db, actor, metric='duties', who=None, type_ids=None, service_ids=None, starts_on=None, ends_on=None, group_by=None,
                 location_ids=None, within=True, workspace_id=None, goal_id=None):
    """A selected canonical goal report, or a published reference distribution with optional exploratory grouping."""
    viewer(actor)
    if goal_id is not None:
        require(not (type_ids or service_ids or location_ids or group_by), 'VALUE_INVALID',
                'A selected goal owns its scope and groups; clear the reference filters')
        result = goals(db, actor, workspace_id, who, starts_on, ends_on)
        report = next((r for r in result['goals'] if r['goal_id'] == goal_id), None)
        require(report is not None, 'NOT_FOUND', 'Fair share goal not found', 404)
        return {**report, 'basis': 'goal', 'workspace': result['workspace']}
    require(workspace_id is None, 'VALUE_INVALID', 'Choose a goal to view a rota distribution')
    require(metric != 'pair_exposure', 'VALUE_INVALID', 'Unknown measure')
    require(not group_by or group_by in GROUPS or group_by in SESSION_GROUPS, 'VALUE_INVALID',
            'Group by rank, skill, programme, rotation type, cohort, tag, clinic, duty type or place')
    first, last = period(starts_on, ends_on)
    data = department_data(db, actor, first, last, include_offers=metric not in f.WISH_METRICS)
    rule = metric_rule(metric, type_ids, service_ids, location_ids=places(data['locations'], location_ids, within))
    group_keys = {}  # ledger key -> group id, when grouping by what the work was
    if group_by in SESSION_GROUPS:
        require(metric not in f.WISH_METRICS, 'VALUE_INVALID', 'Preferences are not counted per duty type, clinic or place')
        other = next((k for k in FILTER_NAMES if rule[k] and k != group_by), None)
        require(other is None, 'VALUE_INVALID', f'Group by {FILTER_NAMES[group_by][:-1]} counts only {FILTER_NAMES[group_by]}: clear the filter on {FILTER_NAMES.get(other, "")}')
        catalogue, name = SESSION_GROUPS[group_by]
        base = f.keys_for({'metric': metric})[0]
        group_keys = {f'{base}|{name}:{gid}': gid for gid in (rule[group_by] or data[catalogue])}
    people = matching(data, parse_tree(who), first, last)
    # Work-dimension groups report what was done, so their rows have no expected
    # audience.  Eligibility is only needed for the people-facing comparison.
    if metric not in f.WISH_METRICS and group_by not in SESSION_GROUPS:
        people = sorted(_eligible_distribution_people(db, actor, data, rule, first, last, people))
    L = m.FairnessLedger
    monthly = defaultdict(lambda: defaultdict(float))
    per_group = defaultdict(float)  # (group id, person) -> value
    if metric in f.WISH_METRICS:
        W = m.WishLedger
        for pid, day, value in db.execute(select(W.person_id, W.period_start, W.value).where(
                W.department_id == actor.department_id, W.metric == metric, W.period_start >= first, W.period_start < last,
                W.person_id.in_(people))):
            monthly[pid][day.strftime('%Y-%m')] += float(value)
    else:
        wanted = set(f.keys_for(rule))
        _, person = ledger_measure(sorted(wanted))

        def per_key(*columns, group=()):
            # One indexed read per key, as one statement: a key list makes SQLite (no planner statistics) leave the
            # (department, key, day) index for a whole-department scan.
            parts = [select(*columns, literal(key).label('key')).where(
                L.department_id == actor.department_id, L.metric_key == ledger_measure([key])[0][0], L.bucket >= first, L.bucket < last,
                person.in_(people)).group_by(*group) for key in sorted(wanted | set(group_keys))]
            return db.execute(parts[0] if len(parts) == 1 else union_all(*parts))

        if metric == 'weekends':  # a weekend counts once per person (and group), however many duties or keys: read the days
            seen = set()
            for pid, day, key in per_key(L.person_id, L.bucket):
                weekend = f.weekend_of(day, rule)
                for group in ([None] if key in wanted else []) + ([group_keys[key]] if key in group_keys else []):
                    if weekend and (group, pid, weekend) not in seen:
                        seen.add((group, pid, weekend))
                        if group is None:
                            monthly[pid][weekend.strftime('%Y-%m')] += 1
                        else:
                            per_group[group, pid] += 1
        else:  # summed per month in the database: a year of a big department is ~2,000 rows, not ~50,000
            year, month = func.extract('year', L.bucket), func.extract('month', L.bucket)
            for pid, y, mo, value, key in per_key(person, year, month, func.sum(L.value), group=(person, year, month)):
                if key in wanted:
                    monthly[pid][f'{int(y)}-{int(mo):02}'] += float(value)
                if key in group_keys:
                    per_group[group_keys[key], pid] += float(value)
            if clinic_union(list(wanted)):
                monthly.clear()
                for _, pid, _, day, value in publication_facts(db, actor.department_id, sorted(wanted), first, last, people):
                    monthly[pid][day.strftime('%Y-%m')] += value
    available = sums(db, actor.department_id, [f.AVAILABLE], first, last, people)
    offered = f.offered_amounts(data, first, last, people=people) if metric in ('overtime_duties', 'overtime_hours') else None
    totals = {pid: sum(monthly[pid].values()) for pid in people}
    basis = available if offered is None else offered
    total, days = sum(totals.values()), sum(basis.values())
    expected = f.expected_shares(total, {pid: basis.get(pid, 0) for pid in people}, {}) if days else {}
    label = names(data, people)
    rows = []
    for pid in sorted(people, key=lambda p: (label[p], p)):
        e = float(expected[pid]) if days else 0.0 if offered is not None else None
        low, high, _ = f.band(metric, e) if e is not None else (None, None, None)
        rows.append({'person_id': pid, 'name': label[pid], 'value': round(totals[pid], 2), 'available_days': available.get(pid, 0.0),
                     'expected': None if e is None else round(e, 2), 'low': None if e is None else round(low, 2),
                     'high': None if e is None else round(high, 2),
                     'status': None if e is None else 'within' if offered is not None and not offered.get(pid) else f.status(metric, totals[pid], e),
                     **({'overtime_offered_hours': round(offered.get(pid, 0.0), 4)} if offered is not None else {}),
                     'months': dict(sorted(monthly[pid].items()))})
    if not sees_everyone(actor):
        rows = [r for r in rows if r['person_id'] == actor.person_id]
    months = defaultdict(float)
    for r in rows:
        for month, value in r['months'].items():
            months[month] += value
    return {'metric': metric, 'unit': f.UNITS[metric], 'starts_on': first.isoformat(), 'ends_on': last.isoformat(), 'people': rows,
            'basis': 'reference_available_days' if offered is None else 'reference_offered',
            'share_by': 'available_days' if offered is None else 'offered',
            'months': {k: round(v, 2) for k, v in sorted(months.items())},
            'group_kind': None if not group_by else 'work' if group_by in SESSION_GROUPS else 'people',
            'groups': groups(data, rows, group_by, first, last, per_group) if group_by and sees_everyone(actor) else []}


def _eligible_distribution_people(db, actor, data, rule, first, last, people):
    """Use the solver's candidate eligibility for the duties being measured."""
    snapshots = published_snapshots(db, actor.department_id, first, last)
    lo, hi = (local_midnight(day, data['organisation']['timezone']) for day in (first, last))
    shifts = []
    for _, snapshot in snapshots:
        for shift in snapshot.get('shifts', []):
            if inactive(shift) or not f.counts(rule, shift):
                continue
            if aware(shift['end']) > lo and aware(shift['start']) < hi:
                shifts.append(shift)
    if not snapshots:
        shifts = [shift for shift in data.get('shifts', [])
                  if not inactive(shift) and f.counts(rule, shift)
                  and aware(shift['end']) > lo and aware(shift['start']) < hi]
        offered = f.offered_intervals(data, first, last, people=people)
        for type_id, type_snapshot in sorted(data.get('types', {}).items()):
            for pid, intervals in offered.items():
                for n, (start, end) in enumerate(intervals):
                    shift = {'id': f'offer:{pid}:{type_id}:{n}:{start.isoformat()}',
                             'start': start.isoformat(), 'end': end.isoformat(),
                             'assignment_type_id': type_id, 'type_revision': type_snapshot.get('revision', 1),
                             'type_snapshot': type_snapshot, 'service_ids': [], 'location_id': None,
                             'participants': []}
                    if f.counts(rule, shift):
                        shifts.append(shift)
    if not shifts:
        return set()
    policy = {'id': 'distribution-audience', 'name': 'Distribution audience', 'status': 'active', 'active': True,
              'department_id': actor.department_id, 'definition': {'scope': {}, 'rule': rule}}
    # Issued participation is historical eligibility, even after an absence is reported.
    # Reuse the goal audience's history path; unassigned people still need candidate eligibility.
    context = {**data,
               'shifts': shifts if not snapshots else [],
               'focus_ids': [shift['id'] for shift in shifts] if not snapshots else [],
               '_fair_share_history_shifts': shifts if snapshots else [],
               '_fair_share_eligibility_policies': data.get('policies', []),
               '_fair_share_eligibility': {}, '_distribution_cache': {}, '_policy_applies_cache': {},
               '_scope_interval_cache': {}, '_bucket_keys_cache': {}}
    from packages.domain.validation import prepare_assignment_trials
    context = prepare_assignment_trials(context)
    context['_fair_share_eligibility_policies'] = context['policies']
    context['_qualification_cache'] = context['_qualification_evidence_cache'] = {}
    context['_candidate_absence_cache'] = {}
    return set(f.eligible_people(context, policy, people))


def groups(data, rows, field, first, last, per_group=None):
    """Totals per group. People groups: everyone matching `field` = key at some moment in the period, then those in none
    (the expected share is theirs). Work groups (clinic, duty type, place): what each person did there, from the ledger's
    filtered keys; members are the people with any, and there is no expected share (a clinic has none); a place group
    'None' holds work at no place."""
    listed = {r['person_id']: r for r in rows}
    work = field in SESSION_GROUPS
    if field == 'tags':
        keys = {t: t for pid in listed for t in data['people'].get(pid, {}).get('tags') or []}
    else:
        keys = {k: v.get('name', k) for k, v in data[SESSION_GROUPS[field][0] if work else GROUPS[field]].items()}
    result, placed = [], set()

    def add(key, name, ids, values=None):
        values = values or {p: listed[p]['value'] for p in ids}
        if not ids:
            return
        value = sum(values[p] for p in ids)
        expected = [listed[p]['expected'] for p in ids if listed[p]['expected'] is not None] if not work else []
        total = round(sum(expected), 2) if expected else None
        result.append({'key': key, 'label': name, 'person_ids': ids, 'people': len(ids), 'value': round(value, 2), 'expected': total,
                       'mean': round(value / len(ids), 2), 'expected_mean': None if total is None else round(total / len(ids), 2)})

    for key, name in sorted(keys.items(), key=lambda kv: (kv[1].lower(), kv[0])):
        if work:
            values = {p: per_group.get((key, p), 0.0) for p in listed}
            add(key, name, [p for p in listed if values[p]], values)
            continue
        tree = {'match': 'all', 'conditions': [{'field': field, 'op': 'any', 'values': [key]}]}
        found = set(matching(data, tree, first, last))
        ids = [p for p in listed if p in found]
        placed.update(ids)
        add(key, name, ids)
    if not work:
        add('', 'None', [p for p in listed if p not in placed])
    elif field == 'location_ids':  # every session has one place or none, so the rest of each total is work at no place
        rest = {p: round(listed[p]['value'] - sum(per_group.get((k, p), 0.0) for k in keys), 4) for p in listed}
        add('', 'None', [p for p in listed if rest[p] > 0], rest)
    return result


SESSION_LIMIT = 500


def sessions(db, actor, person_id, other_person_id=None, metric='duties', pairing='present', type_ids=None, service_ids=None,
             starts_on=None, ends_on=None, month=None, location_ids=None, within=True, workspace_id=None, goal_id=None):
    """Drill-down: the published sessions (or wishes) behind one person's value, per publication."""
    from apps.api.auth import person_access
    viewer(actor)
    require(sees_everyone(actor) or person_id == actor.person_id, 'FORBIDDEN', 'Staff see only their own sessions', 403)
    require(pairing in ('present', 'supervised'), 'VALUE_INVALID', 'Pairing is present or supervised')
    goal, goal_data = None, None
    if metric == 'workload':
        from apps.api.snapshots import domain_snapshot
        require(workspace_id and goal_id, 'VALUE_INVALID', 'Choose the workload goal and its rota')
        workspace = scoped(db, m.Workspace, workspace_id, actor)
        require(sees_everyone(actor) or workspace.status == 'published', 'FORBIDDEN', 'Drafts are visible to planners only', 403)
        require(workspace.status == 'published', 'VALUE_INVALID', 'Workload session details require a published rota')
        goal_data = domain_snapshot(db, workspace, current_scheduling=True)
        goal = next((p for group in f.goal_groups(goal_data) for p in [f.comparison_goal(group)]
                     if f.goal_identity(p) == goal_id and p['definition']['rule']['metric'] == 'workload'), None)
        require(goal is not None, 'NOT_FOUND', 'This workload goal is no longer available', 404)
        metric = 'duties' if f.workload_measure(goal_data) == 'duties' else 'hours'
        # The goal owns its counted scope; dashboard distribution filters describe a different number.
        type_ids, service_ids, location_ids = None, None, None
    rule = metric_rule(metric, type_ids, service_ids, pairing,
                       places(place_tree(db, actor.department_id), location_ids, within) if location_ids else None)
    person = person_access(db, actor, person_id)
    other = person_access(db, actor, other_person_id) if other_person_id and metric == 'pair_exposure' else None
    first, last = period(starts_on, ends_on)
    if month:
        try:
            a = date.fromisoformat(month + '-01')
        except ValueError:
            require(False, 'VALUE_INVALID', 'Month is YYYY-MM')
        first, last = max(first, a), min(last, date(a.year + a.month // 12, a.month % 12 + 1, 1))
        require(first < last, 'VALUE_INVALID', 'The month is outside the period')
    if goal is not None:
        start, end = f.rota_dates(goal_data)
        first, last = max(first, start), min(last, end)
        require(first < last, 'VALUE_INVALID', 'The period is outside this workload rota')
    value, days, wishes = defaultdict(float), defaultdict(set), defaultdict(list)
    if metric in f.WISH_METRICS:
        from .wishes import outcome_reasons
        W = m.WishLedger
        for pid, v in db.execute(select(W.publication_id, W.value).where(W.department_id == actor.department_id, W.person_id == person_id,
                                 W.metric == metric, W.period_start >= first, W.period_start < last)):
            value[pid] += float(v)
        from .schedule import in_force_snapshot
        legacy = []
        for publication in db.scalars(select(m.Publication).where(m.Publication.id.in_(list(value)))):
            snapshot = in_force_snapshot(db, publication)
            if 'wishes' in snapshot:
                wishes[publication.id] = [{**{k: w.get(k) for k in ('id', 'kind', 'first_day', 'last_day', 'points', 'outcome', 'limit', 'weekend_start', 'weekend_days')},
                                           'outcome_reasons': outcome_reasons(actor, w.get('outcome_reasons'))}
                                          for w in snapshot['wishes'] if w['person_id'] == person_id]
            else:
                legacy.append(publication.id)
        for w in db.scalars(select(m.StaffWish).where(m.StaffWish.person_id == person_id, m.StaffWish.publication_id.in_(legacy))
                            .order_by(m.StaffWish.first_day, m.StaffWish.id)) if legacy else ():
            wishes[w.publication_id].append({'id': w.id, 'kind': w.kind, 'first_day': w.first_day.isoformat(),
                                             'last_day': w.last_day.isoformat(), 'points': w.points, 'outcome': w.outcome,
                                             'limit': w.limit, 'weekend_start': w.weekend_start, 'weekend_days': w.weekend_days,
                                             'outcome_reasons': outcome_reasons(actor, w.outcome_reasons)})
    elif goal is not None:
        from .schedule import in_force_snapshot
        snapshots = [(pub.id, in_force_snapshot(db, pub)) for pub in db.scalars(select(m.Publication).where(
            m.Publication.department_id == actor.department_id, m.Publication.workspace_id == workspace_id,
            m.Publication.current.is_(True)))]
        for publication_id, _, _, day, amount in publication_facts(db, actor.department_id, f.keys_for(rule),
                first, last, [person_id], snapshots=snapshots, policy=goal, data=goal_data):
            value[publication_id] += amount
            days[publication_id].add(day)
    else:
        L = m.FairnessLedger
        keys = f.keys_for(rule)
        stored_keys, person_column = ledger_measure(keys)
        query = select(L.publication_id, L.bucket, L.value).where(L.department_id == actor.department_id, person_column == person_id,
                                                                  L.metric_key.in_(stored_keys), L.bucket >= first, L.bucket < last)
        if other:
            query = query.where(L.other_person_id == other.id)
        weekend_days = defaultdict(set)
        for pid, day, v in db.execute(query):
            days[pid].add(day)
            value[pid] += float(v)
            if metric == 'weekends' and f.weekend_of(day, rule):
                weekend_days[pid].add(f.weekend_of(day, rule))
        if metric == 'weekends':
            value = {pid: float(len(weekend_days[pid])) for pid in value}
        elif clinic_union(keys):
            value.clear()
            days.clear()
            for publication_id, _, partner, day, amount in publication_facts(db, actor.department_id, keys, first, last, [person_id]):
                if other and partner != other.id:
                    continue
                value[publication_id] += amount
                days[publication_id].add(day)
    P, WS = m.Publication, m.Workspace
    base = f.LEDGER_KEY.get(metric)
    publications, total, truncated = [], 0, False
    from .schedule import in_force_snapshot
    for pub, name in db.execute(select(P, WS.name).join(WS, WS.id == P.workspace_id).where(P.id.in_(list(value))).order_by(P.starts_on, P.sequence)):
        snap, rows = in_force_snapshot(db, pub), []
        zone = snap['timezone']
        context = {**goal_data, 'locations': snap.get('locations', goal_data['locations']),
                   '_policy_applies_cache': {}, '_scope_interval_cache': {}, '_bucket_keys_cache': {}} if goal is not None else None
        for shift in snap['shifts'] if days[pub.id] else []:
            if inactive(shift) or not f.counts(rule, shift):
                continue
            for p in f.metric_participants(metric, shift):
                if p.get('person_id') != person_id or f.local_day(p['start'], zone) not in days[pub.id]:
                    continue
                if goal is not None and not f.goal_applies(context, goal, person_id, shift, aware(p['start']), aware(p['end'])):
                    continue
                if metric == 'pair_exposure':
                    partners = ({q['person_id'] for q in shift['participants']} - {person_id} if pairing == 'present'
                                else {p.get('supervisor_person_id')} - {None})
                    if not (other.id in partners if other else partners):
                        continue
                if total >= SESSION_LIMIT:
                    truncated = True
                    break
                total += 1
                rows.append({'shift_id': shift['id'], 'day': f.local_day(p['start'], zone).isoformat(),
                             'start': aware(p['start']).isoformat(), 'end': aware(p['end']).isoformat(),
                             'type_id': shift['assignment_type_id'], 'type_name': shift['type_snapshot']['name'],
                             'services': [snap.get('services', {}).get(s, {}).get('name', '') for s in shift['service_ids']],
                             'location': snap.get('locations', {}).get(shift.get('location_id') or '', {}).get('name', ''),
                             'value': 1 if metric == 'pair_exposure' else f.contribution(base, shift, p),
                             'team_lead': bool(p.get('team_lead')), 'overtime': bool(p.get('overtime'))})
        rows.sort(key=lambda r: r['start'])
        publications.append({'publication_id': pub.id, 'workspace_id': pub.workspace_id, 'name': name, 'sequence': pub.sequence,
                             'starts_on': pub.starts_on.isoformat(), 'ends_on': pub.ends_on.isoformat(), 'value': round(value[pub.id], 2),
                             'sessions': rows, 'wishes': wishes[pub.id]})
    metric_value = (weekends(db, actor.department_id, f.keys_for(rule), first, last, [person_id], rule).get(person_id, 0.0)
                    if metric == 'weekends' else sum(value.values()))
    return {'person': {'id': person.id, 'name': person.display_name}, 'other': {'id': other.id, 'name': other.display_name} if other else None,
            'metric': metric, 'unit': f.UNITS[metric], 'starts_on': first.isoformat(), 'ends_on': last.isoformat(),
            **s.FairnessMetricTotal(metric_value=round(metric_value, 4)).model_dump(),
            'total': total, 'truncated': truncated, 'publications': publications}


def preview(db, actor, value):
    """A Balance goal (saved or not) on a draft: each person's current value against their aim."""
    from apps.api.snapshots import domain_snapshot
    require_role(actor, *ALL_ROLES)
    require(isinstance(value, dict), 'SCHEMA_INVALID', 'Send a workspace and a rule definition')
    workspace = scoped(db, m.Workspace, value.get('workspace_id'), actor)
    definition = s.PolicyDefinition.model_validate(value.get('definition') or {}).model_dump(mode='json')
    require(definition['rule']['kind'] == 'balance_goal', 'VALUE_INVALID', 'Preview is for balance goals')
    # A definition-only preview has no activation date: include its configured historical period.
    policy = {'id': 'preview', 'name': str(value.get('name') or 'Balance goal')[:160], 'status': 'active', 'active': True,
              'department_id': workspace.department_id, 'kind': 'balance_goal', 'revision': 1,
              'starts_on': None, 'ends_on': None, 'definition': definition}
    data = domain_snapshot(db, workspace, policy_overrides=[policy])
    report = f.goal_report(data, policy)
    label = names(data, {r['person_id'] for r in report['people'] + report['pairs']} | {r['partner_id'] for r in report['pairs']})
    return {**report, 'people': [{**r, 'name': label[r['person_id']]} for r in report['people']],
            'pairs': [{**r, 'name': label[r['person_id']], 'partner_name': label[r['partner_id']]} for r in report['pairs']]}
