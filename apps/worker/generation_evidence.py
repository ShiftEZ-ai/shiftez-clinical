"""Retain the exact generated candidate evidence with its completed job."""
from sqlalchemy import insert

from apps.api.models import JobCandidateEvidence
from apps.api.serialization import CANDIDATE_EVIDENCE_REF, CANDIDATE_EVIDENCE_VERSION, job_result_projection
from apps.api.workspace_baselines import generation_manifest
from packages.domain.input_manifest import digest


def retain_completed_candidate_evidence(db, job, baseline, result):
    evidence = (result or {}).get('candidate_evidence')
    has_evidence = isinstance(evidence, list)
    if has_evidence:
        result = {key: value for key, value in result.items() if key != 'candidate_evidence'}
        result[CANDIDATE_EVIDENCE_REF] = {'version': CANDIDATE_EVIDENCE_VERSION, 'count': len(evidence),
                                          'digest': digest(evidence)}
    manifest = generation_manifest(baseline, job.input_snapshot, job=job,
                                   requester_id=job.requested_by, result=result, status=job.status)
    if has_evidence:
        table = JobCandidateEvidence.__table__
        for start in range(0, len(evidence), 1000):
            db.connection().execute(insert(table), [
                {'job_id': job.id, 'ordinal': ordinal, 'shift_id': row.get('shift_id'), 'payload': row}
                for ordinal, row in enumerate(evidence[start:start + 1000], start)])
        job.result_summary = job_result_projection(job.kind, {**result, 'candidate_evidence': evidence})
    return result, manifest
