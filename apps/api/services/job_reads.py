"""Read projections leave immutable job inputs and detailed solver evidence in storage."""
from sqlalchemy import Integer, JSON, case, cast, column, func, literal, select, true, type_coerce
from sqlalchemy.dialects.postgresql import aggregate_order_by

from apps.api import models as m
from apps.api.errors import require
from apps.api.serialization import (CANDIDATE_EVIDENCE_REF, candidate_evidence_count, effective_input_fingerprint,
                                    job_read_metadata, job_result_projection)
from apps.api.serialization import job_reconciliation_columns, job_result_with_reconciliation


def job_stamp(db, *, department_id=None, workspace_id=None):
    """Bounded revision stamp shared by the jobs list and conditional workspace reads."""
    scope = (m.Job.department_id == department_id if department_id is not None
             else m.Job.workspace_id == workspace_id)
    return tuple(db.execute(select(func.count(m.Job.id), func.coalesce(func.sum(m.Job.revision), 0),
        func.max(m.Job.revision), func.max(m.Job.created_at)).where(scope)).one())


def metadata(job, *, load_legacy=True):
    projection = job.read_metadata
    if projection is None:
        projection = job_read_metadata(job.input_snapshot, job_result_with_reconciliation(job, job.result)) if load_legacy else {'input_snapshot': {}, 'result': None}
    # Missing retained context is unknown, not a request to reload the frozen input blob.
    return {**projection, 'input_snapshot': {'run_context': None, **projection['input_snapshot']}}


def status_metadata(db, job_id):
    """Select status facts only; retained reconciliation checks and manifests can be arbitrarily large."""
    from apps.api.serialization import JOB_INPUT_FIELDS, JOB_SUMMARY_FIELDS, job_result_metadata
    inputs = [key for key in JOB_INPUT_FIELDS if key not in {'bindings', 'currency_hint', 'input_manifest'}]
    paths = [('generation_outcome',), ('status',), ('algorithm_version',), ('diagnostic_proposal',), ('change_count',),
             ('solver_status',), ('score',), ('search_stop',), ('stop_reason',), ('continuation',), ('on_conflict',),
             ('planning_group_version_id',), ('planning_context',), ('lookahead',), ('proof_scope',), ('completion',),
             ('error_detail',),
             *[('validation', key) for key in ('valid', 'error_count', 'warning_count')],
             *[('summary', key) for key in JOB_SUMMARY_FIELDS]]
    engine_type = (func.json_type(m.Job.read_metadata, '$.input_snapshot.generation_engine')
                   if db.get_bind().dialect.name == 'sqlite'
                   else func.json_typeof(m.Job.read_metadata['input_snapshot', 'generation_engine']))
    values = db.execute(select(*[m.Job.read_metadata['input_snapshot', key] for key in inputs],
        *[m.Job.read_metadata[('result', *path)] for path in paths], engine_type,
        m.Job.read_metadata['input_fingerprint'].as_string(), m.Job.input_fingerprint,
        m.Job.read_metadata.is_(None)).where(m.Job.id == job_id)).one()
    snapshot = {key: value for key, value in zip(inputs, values[:len(inputs)]) if value is not None}
    engine_index = len(inputs) + len(paths)
    if values[engine_index] == 'null':
        snapshot['generation_engine'] = None
    result = {}
    metadata_offset = len(inputs) + len(paths) + 1
    for path, value in zip(paths, values[len(inputs):metadata_offset]):
        if value is not None:
            if path == ('validation', 'valid'):
                value = bool(value)
            if len(path) == 1:
                result[path[0]] = value
            else:
                result.setdefault(path[0], {})[path[1]] = value
    metadata_fingerprint, column_fingerprint, metadata_missing = values[metadata_offset:]
    if metadata_missing or snapshot.get('_deferred_generation'):
        fingerprint = None
    elif metadata_fingerprint is not None:
        fingerprint = metadata_fingerprint
    else:
        # Legacy retained projections have the frozen value only on the row.
        fingerprint = column_fingerprint if snapshot else None
    return {'input_snapshot': snapshot, 'result': job_result_metadata(result, include_error_detail=True) if result else None,
            # A null projection has no safe fingerprint.  In particular, never expose
            # the deferred request token stored in jobs.input_fingerprint.
            'input_fingerprint': fingerprint}


def complete_validation_delta(db, job, workspace):
    """Return the compact terminal-check envelope used by the planner settle path.

    The retained projection is the public validation result without private
    evaluator inputs/evidence. Missing projections intentionally return None so
    callers fall back to the authoritative workspace read.
    """
    if job.kind != 'validation' or job.status != 'succeeded':
        return None
    projection = job.result_summary or {}
    result = projection.get('result')
    if not isinstance(result, dict):
        return None
    checked_revision = result.get('checked_revision')
    if type(checked_revision) is not int:
        return None
    validation = result.get('validation') if isinstance(result.get('validation'), dict) and 'findings' not in result else result
    if not isinstance(validation, dict) or not isinstance(validation.get('findings'), list) or not isinstance(validation.get('coverage'), list):
        return None
    from apps.api.snapshots import cheap_fingerprints
    watermark = validation.get('fingerprint')
    current = (workspace.status == 'draft' and workspace.archived_at is None and checked_revision == workspace.revision
               and bool(watermark) and watermark == cheap_fingerprints(
                   db, [workspace], checks={workspace.id: projection}).get(workspace.id))
    partial = bool(validation.get('scope') or validation.get('checking'))
    summary = None
    if current and not partial:
        from .rota_summary import current_check
        saved = workspace.validation or {}
        current = saved.get('evaluated_at') == validation.get('evaluated_at') and current_check(db, workspace, watermark)
        if current:
            summary = (saved.get('fix_list_delta') or {}).get('summary')
    return {'kind': 'complete_check', 'workspace_id': workspace.id, 'revision': checked_revision,
            'current_fingerprint': watermark, 'partial': partial, 'stale': not current,
            'fix_summary': summary, 'validation': validation}


def result_stale(job, projection, current_fingerprint, source_digest, *, input_fingerprint=...):
    """One currency classifier for history, Home and detail; absent evidence is unknown."""
    from packages.domain.solver_rules import COMPILER_VERSION
    from packages.domain.generation import ALGORITHM_VERSION
    from apps.api.snapshots import input_current
    if (isinstance(projection.get('input_snapshot'), dict)
            and projection['input_snapshot'].get('_deferred_generation')):
        return None
    if input_fingerprint is ...:
        input_fingerprint = effective_input_fingerprint(job, projection=projection)
    if input_fingerprint is None:
        return None
    snapshot, result = projection['input_snapshot'], projection['result'] or {}
    engine = snapshot.get('generation_engine')
    if engine not in {'cp_sat', 'legacy'}:
        return None
    version = COMPILER_VERSION if engine == 'cp_sat' else ALGORITHM_VERSION
    version_stale = engine == 'cp_sat' and snapshot.get('compiler_version') != version or (
        result.get('algorithm_version') and result['algorithm_version'] != version)
    current = input_current(input_fingerprint, current_fingerprint)
    if current is None:
        return True if version_stale else None
    if not current or version_stale:
        return True
    if not result.get('algorithm_version'):
        return None
    if snapshot.get('generation_scope') is not None:
        if not snapshot.get('generation_scope_source') or not result.get('generation_scope_source') or result.get('generation_scope') is None:
            return None
        return (source_digest is None or snapshot['generation_scope_source'] != source_digest
                or result['generation_scope_source'] != source_digest or result['generation_scope'] != snapshot['generation_scope'])
    return False


def generation_currency(job, projection, *, stale, can_recheck=False, superseded=False, deferred=None):
    """Classify one generation read; deferred preparation always wins over stale checks."""
    if deferred is None:
        snapshot = projection.get('input_snapshot') if isinstance(projection, dict) else None
        deferred = isinstance(snapshot, dict) and snapshot.get('_deferred_generation') is True
    if deferred:
        return 'preparing'
    if superseded:
        return 'superseded'
    if stale is None or (stale and not can_recheck):
        return 'unknown'
    return 'revalidation_required' if stale else 'current'


def current_source_digest():
    from .jobs import catalogue_proof_identity
    identity = catalogue_proof_identity()
    return identity['source_digest'] if identity else None


def proposals(db, actor, workspaces):
    """One ranked metadata-only read, at most one eligible candidate per visible draft."""
    from sqlalchemy.orm import load_only
    from apps.api.serialization import job_outcome
    if not actor.permits('coordinator'):
        return {}
    drafts = {w.id: w for w in workspaces if w.status == 'draft'}
    if not drafts:
        return {}
    ranked = select(m.Job.id, func.row_number().over(partition_by=m.Job.workspace_id,
        order_by=(m.Job.created_at.desc(), m.Job.seq.desc())).label('position')).where(
            m.Job.department_id == actor.department_id, m.Job.workspace_id.in_(drafts),
            m.Job.kind == 'generation', m.Job.status == 'succeeded').subquery()
    from .jobs import superseded_flag
    rows = db.execute(select(m.Job, superseded_flag(db, m.Job)).join(ranked, ranked.c.id == m.Job.id)
                      .where(ranked.c.position == 1).options(
                          load_only(m.Job.id, m.Job.workspace_id, m.Job.input_fingerprint,
                                    m.Job.read_metadata, m.Job.created_at))).all()
    superseded = {job.id for job, flag in rows if flag}
    found = {}
    for job, _ in rows:
        projection = metadata(job, load_legacy=False)
        result = projection['result'] or {}
        outcome = job_outcome('succeeded', result)
        if outcome in {'complete', 'partial'} and not result.get('applied'):
            found[job.workspace_id] = {'job_id': job.id, 'outcome': outcome, 'created_at': job.created_at,
                                       'summary': result.get('summary') or {},
                                       'currency': generation_currency(job, projection, stale=None,
                                                                        superseded=job.id in superseded),
                                       'reconciliation': result.get('reconciliation')}
    return found


def completed_feasibility_payload():
    payload = m.JobFeasibilityResult
    return select(payload.payload).where(
        payload.job_id == m.Job.id, payload.attempt == m.Job.attempts,
        m.Job.kind == 'feasibility', m.Job.status == 'succeeded',
    ).correlate(m.Job).scalar_subquery()


def full_job_result(db, job):
    if job.kind == 'feasibility' and job.status == 'succeeded':
        stored = db.scalar(select(completed_feasibility_payload()).select_from(m.Job).where(m.Job.id == job.id))
        if stored is not None:
            return stored
    return job.result


def evidence_page(db, job, *, phase, diagnostic_index, shift_id, offset, limit):
    path = ['checks', phase] if phase else []
    diagnostics = [*path, 'diagnostics']
    rows = [*diagnostics, diagnostic_index, 'candidates'] if diagnostic_index is not None else [*path, 'candidate_evidence']
    group = [*diagnostics, diagnostic_index] if diagnostic_index is not None else [*path, 'candidate_evidence']
    stats_path = [*group, *(['shifts', shift_id] if shift_id else ['all'])]
    top_level = job.kind == 'generation' and phase is None and diagnostic_index is None
    raw = (job.result or {}) if job.read_metadata is None else None
    if raw is not None and not (top_level and CANDIDATE_EVIDENCE_REF in raw):
        # Legacy null projections page the same loaded payload their counts came from, without a second read.
        source = (job_result_projection(job.kind, raw) or {}).get('evidence', {})
        for key in path:
            source = source.get(key, {})
            raw = raw.get(key, {})
        count = len(source.get('diagnostics', []))
        value = ((source.get('diagnostics') or [])[diagnostic_index]
                 if diagnostic_index is not None and diagnostic_index < count
                 else source.get('candidate_evidence', {}))
        stats = value.get('shifts', {}).get(shift_id) if shift_id else value.get('all')
        candidates = ((raw.get('diagnostics') or [])[diagnostic_index].get('candidates', [])
                      if diagnostic_index is not None and diagnostic_index < count else raw.get('candidate_evidence', []))
        if shift_id:
            candidates = [row for row in candidates if row.get('shift_id', value.get('shift_id')) == shift_id]
        return {**(stats or {'total': 0, 'eligible': 0, 'reasons': {}}),
                'candidates': candidates[offset:offset + limit], 'diagnostic_count': count}
    stats = m.Job.result_summary['evidence', *stats_path]
    fallback = m.Job.result_summary['evidence', *group, 'shift_id'].as_string() if diagnostic_index is not None else literal(None)
    count = func.json_array_length(m.Job.result_summary['evidence', *diagnostics]) if diagnostic_index is not None else literal(0)
    # The same statement picks the format and reads its rows, including during worker completion.
    reference = m.Job.result[CANDIDATE_EVIDENCE_REF] if top_level else literal(None, JSON)
    summary = select(stats.label('stats'), fallback.label('evidence_shift'), count.label('diagnostic_count'),
                     reference.label('reference')).where(m.Job.id == job.id).cte('metadata')
    result = func.coalesce(completed_feasibility_payload(), m.Job.result) if phase else m.Job.result
    sqlite = db.bind.dialect.name == 'sqlite'
    if sqlite:
        json_path = '$' + ''.join(f'[{part}]' if isinstance(part, int) else '.' + part for part in rows)
        entries = func.json_each(result, json_path).table_valued('key', column('value', JSON))
        candidate, ordinal, first = entries.c.value, cast(entries.c.key, Integer), 0
        candidate_shift = case((func.json_type(candidate, '$.shift_id').is_(None), summary.c.evidence_shift),
                               else_=func.json_extract(candidate, '$.shift_id'))
    else:
        entries = func.json_array_elements(func.coalesce(result[tuple(rows)], literal([], JSON))).table_valued(
            column('candidate', JSON), with_ordinality='ordinal').render_derived()
        candidate, ordinal, first = entries.c.candidate, entries.c.ordinal, 1
        candidate_shift = case((func.json_typeof(candidate['shift_id']).is_(None), summary.c.evidence_shift),
                               else_=candidate['shift_id'].as_string())
    page = select(candidate.label('candidate'), ordinal.label('ordinal')).select_from(m.Job).join(entries, true()).join(
        summary, true()).where(m.Job.id == job.id)
    if shift_id:
        page = page.where(candidate_shift == shift_id).offset(offset)
    else:
        page = page.where(ordinal >= offset + first, ordinal < offset + limit + first)

    def page_payload(page):
        page = page.limit(limit).subquery()
        aggregate = (func.json_group_array(func.json(page.c.candidate)) if sqlite
                     else func.coalesce(func.json_agg(aggregate_order_by(page.c.candidate, page.c.ordinal)),
                                        literal([], JSON)))
        return select(type_coerce(aggregate, JSON)).select_from(page).scalar_subquery()

    candidates = page_payload(page.order_by(ordinal))
    artifact_present = literal(True)
    if top_level:
        evidence = m.JobCandidateEvidence
        saved = select(evidence.payload.label('candidate'), evidence.ordinal).where(evidence.job_id == job.id)
        if shift_id:
            saved = saved.where(evidence.shift_id == shift_id).offset(offset)
        else:
            saved = saved.where(evidence.ordinal >= offset)
        external = summary.c.reference['version'].as_integer().is_not(None)
        candidates = case((external, page_payload(saved.order_by(evidence.ordinal))), else_=candidates)
        first_row = select(evidence.ordinal).where(evidence.job_id == job.id, evidence.ordinal == 0).exists()
        artifact_present = case((external, first_row), else_=artifact_present)
    # Counts, diagnostic context and candidate rows share one PostgreSQL READ COMMITTED statement snapshot.
    stats, count, reference, candidates, artifact_present = db.execute(select(
        summary.c.stats, summary.c.diagnostic_count, summary.c.reference,
        type_coerce(candidates, JSON), artifact_present)).one()
    stats = stats or {'total': 0, 'eligible': 0, 'reasons': {}}
    if reference is not None:
        row_count = candidate_evidence_count(reference)
        require(row_count is not None and (row_count == 0 or artifact_present)
                and (shift_id is not None or stats['total'] == row_count)
                and len(candidates) == min(limit, max(0, stats['total'] - offset)),
                'EVIDENCE_NOT_FOUND', 'This explanation is unavailable', 404)
    return {**stats, 'candidates': candidates, 'diagnostic_count': count or 0}


def reconciliation_page(db, job_id, section, offset, limit):
    """Receipt ID, count and rows come from one statement's MVCC snapshot."""
    receipt, _, _ = job_reconciliation_columns()
    source = receipt[section]
    if db.bind.dialect.name == 'sqlite':
        entries = func.json_each(source).table_valued('key', column('value', JSON))
        candidate, ordinal = entries.c.value, cast(entries.c.key, Integer)
    else:
        array = case((func.json_typeof(source) == 'array', source), else_=literal([], JSON))
        entries = func.json_array_elements(array).table_valued(column('value', JSON), with_ordinality='ordinal').render_derived()
        candidate, ordinal = entries.c.value, entries.c.ordinal
    page = select(candidate.label('value'), ordinal.label('ordinal')).select_from(m.Job).join(entries, true()).where(
        m.Job.id == job_id).order_by(ordinal).offset(offset).limit(limit).subquery()
    aggregate = (func.json_group_array(func.json(page.c.value)) if db.bind.dialect.name == 'sqlite' else
                 func.coalesce(func.json_agg(aggregate_order_by(page.c.value, page.c.ordinal)), literal([], JSON)))
    values = select(type_coerce(aggregate, JSON)).select_from(page).scalar_subquery()
    return db.execute(select(receipt['id'].as_string(), func.json_array_length(source), values).where(
        m.Job.id == job_id)).one()
