"""Actor-scoped navigation for evidenced generation failures; never a retry or workflow decision."""
from packages.contracts import schemas as s
from apps.api.auth import CATALOGUE_READ_ROLES


def recovery(actor, job, result, *, stale):
    if stale or job.kind != 'generation' or job.status not in {'succeeded', 'failed'}:
        return None
    code = (result or {}).get('status')
    if code == 'unsupported_rules':
        return s.JobRecovery(cause_code=code, action='review_rules', owner_roles=list(s.PERMISSIONS['rule.propose']),
            link_target='#rules' if actor.permits(*CATALOGUE_READ_ROLES) else None).model_dump(mode='json')
    if code in {'implementation_error', 'translation_error', 'model_invalid'} or job.status == 'failed' and job.error_code:
        return s.JobRecovery(cause_code=job.error_code or code, action='report_problem',
            owner_roles=list(s.PERMISSIONS['feedback.triage']),
            link_target='#system/feedback' if actor.permits(*s.PERMISSIONS['feedback.read_all']) else None).model_dump(mode='json')
    return None
