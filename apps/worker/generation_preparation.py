"""Reuse frozen candidate exclusions for an immediate strict-to-diagnostic follow-up."""
import logging

from ortools import __version__ as solver_version
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import aliased

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import CANDIDATE_EVIDENCE_REF, candidate_evidence_count
from packages.contracts.schemas import GenerationOptions
from packages.domain.input_manifest import digest
from packages.domain.solver_resume import resume_key
from packages.domain.solver_rules import COMPILER_VERSION


log = logging.getLogger('rota.worker')
# shortcut: Reuse at most 50,000 rows; page predecessor evidence if larger follow-ups need reuse.
MAX_REUSE_EVIDENCE_ROWS = 50_000


def candidate_evidence_for_followup(engine, claim):
    snapshot = claim['input']
    if (claim['kind'] != 'generation' or snapshot.get('generation_engine') != 'cp_sat'
            or not snapshot.get('diagnostic_proposal') or claim.get('resume_state') is not None):
        return None
    current, previous = aliased(m.Job), aliased(m.Job)
    evidence = previous.result['candidate_evidence']
    reference = previous.result[CANDIDATE_EVIDENCE_REF]
    bounded_evidence = case((and_(reference['version'].as_integer().is_(None),
                                 func.json_array_length(evidence) <= MAX_REUSE_EVIDENCE_ROWS), evidence))
    statement = (select(previous.id, previous.status, previous.input_fingerprint, previous.input_snapshot,
        previous.result['status'].as_string(), previous.result['algorithm_version'].as_string(),
        previous.result['solver_version'].as_string(), previous.result['input_fingerprint'].as_string(),
        reference, bounded_evidence).join(current, current.id == claim['id']).where(
            current.status == 'running', current.attempts == claim['attempt'],
            previous.workspace_id == current.workspace_id, previous.department_id == current.department_id,
            previous.kind == 'generation', or_(previous.created_at < current.created_at,
                and_(previous.created_at == current.created_at, previous.id < current.id)))
        .order_by(previous.created_at.desc(), previous.id.desc()).limit(1))
    def key(data):
        return resume_key({**data, 'diagnostic_proposal': False},
                          GenerationOptions.model_validate(data.get('generation_options') or {}), False, False)

    try:
        # Keep the large JSON read outside the worker's serialized write transactions.
        with transaction(engine) as db:
            source = db.execute(statement).first()
            if source is None:
                return None
            job_id, status, fingerprint, old, outcome, compiler, solver, result_fingerprint, reference, evidence = source
            if (status != 'succeeded' or outcome != 'requirements_conflict' or compiler != COMPILER_VERSION
                    or solver != solver_version or fingerprint != snapshot['fingerprint']
                    or result_fingerprint != fingerprint or old.get('diagnostic_proposal')
                    or old.get('generation_engine') != 'cp_sat'):
                return None
            count = candidate_evidence_count(reference) if reference is not None else None
            if reference is not None:
                if count is None or count > MAX_REUSE_EVIDENCE_ROWS:
                    return None
            elif not isinstance(evidence, list):
                return None
            # Fingerprints omit per-run options and selected sessions.
            if key(old) != key(snapshot):
                return None
            if reference is None:
                return evidence
            stored = m.JobCandidateEvidence
            rows = list(db.scalars(select(stored.payload).where(stored.job_id == job_id)
                                   .order_by(stored.ordinal).limit(count + 1)))
        return rows if len(rows) == count and digest(rows) == reference['digest'] else None
    except (SQLAlchemyError, KeyError, TypeError, ValueError) as exc:
        log.warning('generation_preparation_reuse_unavailable', extra={'error_code': type(exc).__name__})
        return None
