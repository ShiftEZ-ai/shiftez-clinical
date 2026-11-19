"""Period-bound comparisons of identical demand through the ordinary rota evaluator."""
import copy
from collections import Counter, defaultdict
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import defer

from apps.api import models as m, snapshots
from apps.api.auth import require_role
from apps.api.errors import require
from apps.api.serialization import json_value, record
from apps.api.snapshots import session_key, mapped_score_baseline as _mapped_baseline
from packages.contracts import schemas as s
from packages.domain.patterns import wish_outcomes
from packages.domain.rota_summary import rota_summary
from packages.domain.rule_map import rule_number
from packages.domain.score import COMPONENTS
from packages.domain.time import aware
from . import jobs, planning
from .common import expected, scoped

READ_ROLES = ('coordinator', 'approver', 'programme_lead', 'auditor')


def _baseline(db, actor, workspace):
    publications = list(db.scalars(select(m.Publication).where(
        m.Publication.department_id == actor.department_id, m.Publication.current.is_(True),
        m.Publication.starts_on == workspace.starts_on, m.Publication.ends_on == workspace.ends_on)))
    require(len(publications) <= 1, 'BENCHMARK_BASELINE',
            'The period has several publications; select an unambiguous amendment scope', 409)
    publication = publications[0] if publications else None
    require(workspace.base_publication_id == (publication.id if publication else None),
            'BENCHMARK_BASELINE', 'Compare drafts based on the current publication for this period', 409)
    return publication


def _import_gaps(db, actor, workspace):
    if workspace.origin != 'import':
        return None
    batch = db.scalar(select(m.ScoreCheckImportBatch).where(
        m.ScoreCheckImportBatch.department_id == actor.department_id,
        m.ScoreCheckImportBatch.workspace_id == workspace.id).order_by(
            m.ScoreCheckImportBatch.created_at.desc(), m.ScoreCheckImportBatch.id.desc()).limit(1))
    if batch is None:
        return None
    from . import score_check
    counts = score_check.gap_counts(db, batch)
    require(counts['missing'] == 0, 'IMPORT_ALARMS',
            'Acknowledge the missing staff and cover alarms before benchmarking', 409)
    return counts


def matching_auto_draft(db, actor, source, source_demand):
    """The same matching draft is reused for admission and for the Compare entry point."""
    candidates = db.scalars(select(m.Workspace).where(m.Workspace.department_id == actor.department_id,
        m.Workspace.origin == 'auto', m.Workspace.starts_on == source.starts_on, m.Workspace.ends_on == source.ends_on,
        m.Workspace.base_publication_id == source.base_publication_id, m.Workspace.archived_at.is_(None),
        m.Workspace.status.in_(('draft', 'submitted', 'approved'))).order_by(m.Workspace.created_at.desc(), m.Workspace.id.desc()))
    shared, source_context = None, None
    for workspace in candidates:
        if sorted(map(session_key, snapshots.workspace_shifts(db, workspace.id))) != source_demand:
            continue
        if shared is None:
            shared = snapshots.snapshot_inputs(db, db.get(m.Organisation, actor.organisation_id))
            source_context = _common_inputs(snapshots.domain_snapshot(db, source, shared_inputs=shared))
        if _common_inputs(snapshots.domain_snapshot(db, workspace, shared_inputs=shared)) == source_context:
            return workspace
    return None


def create_auto_draft(db, actor, command, settings):
    require_role(actor, 'coordinator')
    value = s.ScoreCheckAutoInput.model_validate(command.payload)
    source = scoped(db, m.Workspace, value.workspace_id, actor)
    expected(source, command.expected_revision)
    require(source.origin == 'import' and source.status == 'draft' and source.archived_at is None,
            'WORKFLOW_STATE', 'Choose an imported draft that is still in use', 409)
    _import_gaps(db, actor, source)
    _baseline(db, actor, source)
    rows = snapshots.workspace_shifts(db, source.id)
    require(rows, 'EMPTY_ROTA', 'Import sessions before auto-scheduling', 409)
    require(settings.generation_engine == 'cp_sat', 'SCORE_CHECK_ENGINE_REQUIRED',
            'Score checking auto-schedule requires the CP-SAT scheduler', 422)
    if existing := matching_auto_draft(db, actor, source, sorted(map(session_key, rows))):
        job = db.scalar(select(m.Job).options(defer(m.Job.input_snapshot), defer(m.Job.result))
                        .where(m.Job.workspace_id == existing.id, m.Job.kind == 'generation')
                        .order_by(m.Job.created_at.desc(), m.Job.id.desc()).limit(1))
        require(False, 'BENCHMARK_AUTO_EXISTS', 'An auto-schedule draft already exists; open it in Planner to review or re-run', 409,
                {'workspace_id': existing.id, 'revision': existing.revision,
                 'job_id': job.id if job else None, 'job_status': job.status if job else None})
    reply = planning.create_workspace(db, actor, s.Command(kind='workspace.create', payload={
        'name': value.name or ('Rota · ' + source.name)[:160],
        'starts_on': source.starts_on.isoformat(), 'ends_on': source.ends_on.isoformat()}), settings)
    workspace = db.get(m.Workspace, reply['workspace']['id'])
    workspace.origin = 'auto'
    for field in ('base_publication_id', 'replaces_publication_id', 'urgency', 'urgency_reason',
                  'target_overrides', 'rule_adjustments'):
        setattr(workspace, field, copy.deepcopy(getattr(source, field)))
    # Copy stored type definitions and exact times; publication and imported people never seed the solver.
    empty = [{**row, 'participants': [], 'locked': False} for row in rows]
    planning.copy_snapshot_into(db, actor, workspace, {'shifts': empty}, detach_series=True)
    actual = snapshots.workspace_shifts(db, workspace.id)
    require(sorted(map(session_key, rows)) == sorted(map(session_key, actual)),
            'BENCHMARK_SESSIONS', 'Copied sessions do not match the imported rota', 409)
    validation = planning.revalidate(db, workspace)
    queued = jobs.create_job(db, actor, s.Command(kind='job.create', expected_revision=workspace.revision,
        payload={'workspace_id': workspace.id, 'kind': 'generation'}), settings)
    return {'workspace': record(workspace, exclude=('validation',)), 'validation': validation,
            'source_fingerprint': snapshots.fingerprint(db, source), **queued}


def _time(db, workspace):
    if workspace.origin == 'import':
        batch = db.scalar(select(m.ScoreCheckImportBatch).where(
            m.ScoreCheckImportBatch.department_id == workspace.department_id,
            m.ScoreCheckImportBatch.workspace_id == workspace.id).order_by(
                m.ScoreCheckImportBatch.created_at.desc(), m.ScoreCheckImportBatch.id.desc()).limit(1))
        if batch is not None and batch.manual_time_hours is not None:
            return {'work_hours': batch.manual_time_hours, 'people_count': batch.manual_time_people,
                    'source': 'entered'}
        files = list(db.scalars(select(m.ImportSource).where(m.ImportSource.workspace_id == workspace.id,
                                                             m.ImportSource.department_id == workspace.department_id)))
        entered = [row for row in files if row.work_hours_each is not None and row.made_by]
        return {'work_hours': sum(row.work_hours_each * len(set(row.made_by)) for row in entered) if entered else None,
                'people_count': len({pid for row in entered for pid in row.made_by}) or None,
                'missing_files': len(files) - len(entered), 'source': 'entered' if entered else 'not_entered'}
    if workspace.origin != 'auto':
        return None
    runs = list(db.scalars(select(m.Job).options(defer(m.Job.input_snapshot)).where(m.Job.workspace_id == workspace.id,
        m.Job.department_id == workspace.department_id, m.Job.kind == 'generation').order_by(m.Job.created_at, m.Job.id)))
    measured = [row for row in runs if row.started_at is not None and row.completed_at is not None]
    seconds = sum(max(0, (aware(row.completed_at) - aware(row.started_at)).total_seconds()) for row in measured)
    from apps.api.serialization import job_result_with_reconciliation
    revisions = {(job_result_with_reconciliation(row, row.result) or {}).get('applied_content_revision')
                 for row in runs if row.status == 'applied'} - {None}
    changes = list(db.scalars(select(m.ChangeSet).where(m.ChangeSet.workspace_id == workspace.id)
                             .order_by(m.ChangeSet.created_at, m.ChangeSet.id)))
    applied = [row for row in changes if row.resulting_content_revision in revisions and not row.undone]
    reviewers, review_seconds = set(), 0
    if applied:
        first = min(aware(row.created_at) for row in applied)
        by_person = defaultdict(list)
        for row in changes:
            stamp = aware(row.created_at)
            if stamp >= first and (workspace.submitted_at is None or stamp <= aware(workspace.submitted_at)):
                by_person[row.actor_id].append(stamp)
        for pid, stamps in by_person.items():
            reviewers.add(pid)
            points = [first, *stamps, *([aware(workspace.submitted_at)] if workspace.submitted_at else [])]
            review_seconds += sum((end - start).total_seconds() for start, end in zip(points, points[1:])
                                  if timedelta(0) <= end - start <= timedelta(minutes=15))
    return {'work_hours': (seconds + review_seconds) / 3600 if measured or applied else None,
            'people_count': len(reviewers) or None, 'solver_seconds': seconds if measured else None,
            'review_hours': review_seconds / 3600 if applied else None,
            'source': 'mixed' if measured and applied else 'estimated' if applied else 'measured' if measured else 'not_entered'}


def _common_inputs(data):
    focus = set(data['focus_ids'])
    context = {key: value for key, value in data.items()
               if not key.startswith('_') and key not in ('workspace', 'baseline', 'fingerprint', 'input_manifest',
                                                           'focus_ids', 'shifts', 'score_baseline')}
    context['external_shifts'] = [row for row in data['shifts'] if row['id'] not in focus]
    context['workspace'] = {key: data['workspace'].get(key) for key in (
        'starts_on', 'ends_on', 'base_publication_id', 'replaces_publication_id', 'target_overrides', 'rule_adjustments')}
    return snapshots.snapshot_hash(json_value(context))


def _time_description(time):
    if not time or time.get('work_hours') is None:
        return None, 'Not entered'
    if time['source'] == 'entered':
        people = time.get('people_count')
        detail = f"{time['work_hours']:g} work hours" + (f" by {people} people" if people else '') + ' (entered)'
        if missing := time.get('missing_files'):
            detail += f'; {missing} files have no time entered'
        return 'entered', detail
    parts = []
    if time.get('solver_seconds') is not None:
        parts.append(f"Solver {time['solver_seconds']:g} seconds (measured)")
    if time.get('review_hours') is not None:
        parts.append(f"review {time['review_hours']:g} work hours (estimated)")
    return 'estimated' if time.get('review_hours') is not None else None, ' + '.join(parts)


def _frontend_projection(workspaces, rotas, contexts, checks):
    labels = {'coverage': 'Coverage', 'rules': 'Rules and targets', 'fairness': 'Fairness',
              'preferences': 'Wishes', 'stability': 'Stability'}
    workload = [max((goal for goal in check.get('goals', []) if goal.get('metric') == 'workload'),
                    key=lambda goal: (len(goal.get('people', [])), goal.get('goal_id', '')), default=None)
                for check in checks]
    units = {goal['unit'] for goal in workload if goal is not None}
    require(len(units) <= 1, 'BENCHMARK_FAIRNESS_UNITS', 'Compare fairness in the same configured units', 409)
    people = [{row['person_id']: row for row in (goal or {}).get('people', [])} for goal in workload]
    sides = []
    for workspace, rota, data, rows in zip(workspaces, rotas, contexts, people):
        score, time = rota['score'] or {}, rota['time']
        basis, time_detail = _time_description(time)
        wishes = rota['preferences']['wishes']
        points = {wish['id']: wish['points'] for wish in data.get('wishes', [])}
        total_points = sum(points.get(wish['wish_id'], 0) for wish in wishes)
        stability = rota['stability']
        stability_detail = ('No baseline' if stability is None else
            'Baseline unavailable: sessions cannot be matched' if stability['state'] == 'unavailable' else
            f"Partial baseline: {stability['matched_sessions']} sessions matched; "
            f"{stability['unmatched_sessions'] + stability['ambiguous_sessions']} unmatched or ambiguous"
            if stability['state'] == 'partial' else 'Compared with the current publication')
        if workspace.origin == 'import':
            detail = 'Imported rota'
        else:
            detail = 'Auto-schedule · same sessions' if workspace.origin == 'auto' else 'Planner draft'
        sides.append({'workspace_id': workspace.id, 'name': workspace.name, 'origin': workspace.origin,
            'detail': detail, 'score': score.get('score'), 'publishable': bool(score.get('publishable')),
            'before_cap': score.get('uncapped_score') if score.get('capped') else None,
            'blocking': rota['blocking_count'], 'warnings': rota['warnings_count'], 'not_covered': rota['places_open'],
            'fairness_gap': max((abs(row['actual'] - row['expected']) for row in rows.values()), default=None),
            'within_fair': sum(row.get('cost') == 0 for row in rows.values()) if rows else None,
            'people': len(rows), 'wishes_met': round(100 * sum(w['honoured'] * points.get(w['wish_id'], 0)
                for w in wishes) / total_points, 1) if total_points else None,
            'time_hours': time.get('work_hours') if time else None, 'time_basis': basis, 'time_detail': time_detail,
            'stability_detail': stability_detail, 'parts': [{'label': labels[key],
                'value': -(score['components'][key]['lost']) if score.get('components', {}).get(key, {}).get('value') is not None else None}
                for key in COMPONENTS]})
    fairness = []
    for pid in sorted({pid for rows in people for pid in rows}):
        targets = {rows[pid]['expected'] for rows in people if pid in rows}
        name = next(data['people'][pid]['display_name'] for data in contexts if pid in data['people'])
        fairness.append({'person_id': pid, 'name': name, 'fair_share': next(iter(targets)) if len(targets) == 1 else None,
            'counts': [rows[pid]['actual'] if pid in rows else None for rows in people],
            'within': [rows[pid].get('cost') == 0 if pid in rows else None for rows in people]})
    rules = []
    policies = {policy['id']: policy for data in contexts for policy in data['policies']}
    counts = [Counter(f['rule_id'] for f in rota['findings'] if f.get('rule_id')) for rota in rotas]
    blocking_rules = {f['rule_id'] for rota in rotas for f in rota['findings']
                      if f.get('rule_id') and f['severity'] == 'error'}
    for rid in sorted({rid for side in counts for rid in side}):
        policy = policies.get(rid)
        number = rule_number(policy['definition']['rule']) if policy else None
        value = number.get('value') if number else None
        rules.append({'rule_id': rid, 'number': f"{value:g} {number.get('unit') or ''}".strip() if isinstance(value, (int, float)) else None,
            'name': policy['name'] if policy else 'Rule',
            'mandatory': bool(policy and policy['definition']['severity'] == 'mandatory') or rid in blocking_rules,
            'counts': [side[rid] for side in counts]})
    groups = []
    for data, check in zip(contexts, checks):
        focus = set(data['focus_ids'])
        rows = defaultdict(list)
        summaries = {row['shift_id']: row for row in rota_summary(check)['sessions']}
        for row in data['shifts']:
            if row['id'] in focus:
                rows[session_key(row)].append((row, summaries.get(row['id'], {}).get('places_open', 0)))
        groups.append({key: sorted(items, key=lambda item: item[0]['id']) for key, items in rows.items()})
    gaps = []
    for key, originals in groups[0].items():
        for index, (row, _) in enumerate(originals):
            opened = [side[key][index][1] for side in groups]
            if any(opened):
                services = [contexts[0]['services'][sid]['name'] for sid in row['service_ids'] if sid in contexts[0]['services']]
                place = contexts[0]['locations'].get(row['location_id'], {}).get('name')
                gaps.append({'shift_id': row['id'], 'name': ' · '.join(filter(None, [row['type_snapshot']['name'],
                    ', '.join(services), place])), 'start': row['start'], 'open': opened})
    gap_counts = next((rota['import_gaps'] for rota in rotas if rota['origin'] == 'import' and rota['import_gaps'] is not None), None)
    return s.Benchmark(period_start=workspaces[0].starts_on, period_end=workspaces[0].ends_on,
        sessions=sum(len(rows) for rows in groups[0].values()), sides=sides, fairness_unit=next(iter(units), None),
        rules=rules, fairness=fairness, gaps=gaps, gaps_total=sum(max(row['open']) for row in gaps),
        import_summary={'added': gap_counts['added'], 'left_out': gap_counts['excluded'] + gap_counts['unknown']
                        + gap_counts['uncertain'] + gap_counts['unreadable'],
                        'acknowledged': gap_counts['acknowledged']} if gap_counts is not None else None).model_dump(mode='json')


def compare_report(db, actor, workspaces):
    """Evaluate current inputs without saving checks or benchmark history."""
    first = workspaces[0]
    publication = _baseline(db, actor, first)
    shared = snapshots.snapshot_inputs(db, db.get(m.Organisation, actor.organisation_id))
    rotas, contexts, checks, demand, common = [], [], [], None, None
    for workspace in workspaces:
        require(workspace.archived_at is None and workspace.status in ('draft', 'submitted', 'approved'),
                'WORKFLOW_STATE', 'Compare drafts that are still in use', 409)
        require((workspace.starts_on, workspace.ends_on, workspace.base_publication_id) ==
                (first.starts_on, first.ends_on, first.base_publication_id),
                'BENCHMARK_PERIOD', 'Every compared rota must have the same period and base publication', 409)
        rows = snapshots.workspace_shifts(db, workspace.id)
        keys = sorted(map(session_key, rows))
        require(demand is None or demand == keys, 'BENCHMARK_SESSIONS',
                'Compare rotas with exactly the same sessions, types, places, clinics, times and attributes', 409)
        demand = keys
        captured = []
        check = planning.revalidate(db, workspace, store=False, shared_inputs=shared, data_out=captured)
        stability = captured[0].get('score_baseline')
        require(not check.get('scope'), 'BENCHMARK_SCOPE', 'Compare complete rotas rather than partial published-change checks', 409)
        inputs = _common_inputs(captured[0])
        require(common is None or common == inputs, 'BENCHMARK_INPUTS',
                'Rules, targets, preferences and scheduling context must match across compared rotas', 409)
        common = inputs
        contexts.append(captured[0])
        checks.append(check)
        summary = rota_summary(check, rows)
        score = check.get('score')
        outcomes = wish_outcomes(captured[0])
        spreads = [{'goal_id': goal.get('goal_id', goal['policy_id']), 'unit': goal['unit'],
                    'value': max(row['actual'] for row in goal['people']) - min(row['actual'] for row in goal['people'])}
                   for goal in check.get('goals', []) if goal.get('workload_spread') and goal.get('people')]
        rotas.append({'workspace_id': workspace.id, 'revision': workspace.revision, 'name': workspace.name,
            'origin': workspace.origin, 'status': workspace.status, 'fingerprint': snapshots.fingerprint(db, workspace),
            'score': score, 'blocking_count': summary['issues']['blocking'], 'warnings_count': summary['issues']['advisory'],
            'places_open': summary['places_open'], 'fairness': {'goals': check.get('goals', []),
                'people': (score or {}).get('people', {}), 'spreads': spreads,
                'component': (score or {}).get('components', {}).get('fairness')},
            'preferences': {'component': (score or {}).get('components', {}).get('preferences'),
                'wishes': [{'wish_id': key, 'honoured': result[0], 'days': result[1]} for key, result in outcomes.items()]},
            'stability': {**{key: value for key, value in stability.items() if key != 'assignments'}, 'baseline_publication_id': publication.id,
                'component': (score or {}).get('components', {}).get('stability')} if publication and stability else None,
            'findings': check.get('findings', []), 'time': _time(db, workspace),
            'import_gaps': _import_gaps(db, actor, workspace)})
    return {'starts_on': first.starts_on, 'ends_on': first.ends_on,
        'baseline_publication_id': publication.id if publication else None,
        'common_inputs_fingerprint': snapshots.snapshot_hash({'inputs': common, 'sessions': demand,
            'baseline_publication_id': publication.id if publication else None}),
        'rotas': rotas, 'frontend': _frontend_projection(workspaces, rotas, contexts, checks)}


def compare(db, actor, command, settings):
    require_role(actor, 'coordinator')
    value = s.ScoreCheckBenchmarkInput.model_validate(command.payload)
    workspaces = [scoped(db, m.Workspace, object_id, actor) for object_id in value.workspace_ids]
    checked = compare_report(db, actor, workspaces)
    first = workspaces[0]
    result = m.BenchmarkResult(department_id=actor.department_id, starts_on=first.starts_on, ends_on=first.ends_on,
        created_by=actor.identity_id, baseline_publication_id=checked['baseline_publication_id'],
        workspace_ids=value.workspace_ids, report={})
    db.add(result)
    db.flush()
    report = s.ScoreCheckBenchmarkResult(id=result.id, created_at=result.created_at, **checked).model_dump(mode='json')
    result.report = report
    db.flush()
    return {'benchmark': report}


def list_results(db, actor, starts_on=None, ends_on=None, limit=50):
    require_role(actor, *READ_ROLES)
    require(1 <= limit <= 100, 'INVALID_LIMIT', 'Choose between 1 and 100 benchmark results')
    query = select(m.BenchmarkResult).where(m.BenchmarkResult.department_id == actor.department_id)
    if starts_on is not None:
        query = query.where(m.BenchmarkResult.starts_on == starts_on)
    if ends_on is not None:
        query = query.where(m.BenchmarkResult.ends_on == ends_on)
    rows = db.scalars(query.order_by(m.BenchmarkResult.created_at.desc(), m.BenchmarkResult.id.desc()).limit(limit))
    return {'benchmarks': [row.report for row in rows]}


def detail(db, actor, result_id):
    require_role(actor, *READ_ROLES)
    return {'benchmark': scoped(db, m.BenchmarkResult, result_id, actor).report}


def frontend_view(db, actor, imported_workspace_id):
    """Current Compare page; GET never persists a check or creates benchmark history."""
    require_role(actor, *READ_ROLES)
    source = scoped(db, m.Workspace, imported_workspace_id, actor)
    require(source.origin == 'import', 'BENCHMARK_ORIGIN', 'Open Compare from an imported rota', 409)
    source_demand = sorted(map(session_key, snapshots.workspace_shifts(db, source.id)))
    selected = [source]
    if workspace := matching_auto_draft(db, actor, source, source_demand):
        selected.append(workspace)
    latest = db.scalar(select(m.BenchmarkResult).where(m.BenchmarkResult.department_id == actor.department_id,
        m.BenchmarkResult.starts_on == source.starts_on, m.BenchmarkResult.ends_on == source.ends_on)
        .order_by(m.BenchmarkResult.created_at.desc(), m.BenchmarkResult.id.desc()).limit(1))
    if latest and source.id in latest.workspace_ids and len(selected) > 1:
        for object_id in latest.workspace_ids:
            workspace = scoped(db, m.Workspace, object_id, actor)
            if (workspace.id not in {row.id for row in selected} and workspace.origin != 'auto'
                    and workspace.archived_at is None and workspace.status in ('draft', 'submitted', 'approved')):
                selected.append(workspace)
    return compare_report(db, actor, selected)['frontend']
