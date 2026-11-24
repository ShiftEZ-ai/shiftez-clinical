import re

INTEGRITY_CONFLICT_MESSAGE = "The write conflicts with a related or duplicate record; no partial change was committed"
VALUE_INVALID_MESSAGE = "A value is not valid for this request"

PROBLEM_DETAIL_LIMIT = 50
_PUBLIC_PROBLEM_DETAIL_FIELDS = ('rule_id', 'rule_revision', 'field', 'code', 'message')


class Problem(Exception):
    def __init__(self, status: int, code: str, message: str, details=None):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.details = details


def unique_violation(error):
    original = error.orig
    if (getattr(original, 'sqlstate', None) or getattr(original, 'pgcode', None)) == '23505':
        return getattr(getattr(original, 'diag', None), 'constraint_name', None)
    return None


def integrity_problem(error):
    code, message = {
        'uq_calendar_feeds_active': ('INTEGRITY_CONFLICT', INTEGRITY_CONFLICT_MESSAGE),
        'uq_people_active_short_code': ('SHORT_CODE_TAKEN', 'This short code is already used by another person'),
        'uq_jobs_live_generation': ('JOB_ALREADY_RUNNING', 'This draft already has a queued or running auto-schedule run'),
        'uq_jobs_live_validation': ('STALE_INPUTS', 'The rota check changed; queue it again'),
    }.get(unique_violation(error), ('INTEGRITY_CONFLICT', INTEGRITY_CONFLICT_MESSAGE))
    return Problem(409, code, message)


def problem_payload(problem: 'Problem', *, cap: int | None = None):
    """Build the public refusal envelope; capped job copies keep only safe issue fields."""
    payload = {'code': problem.code, 'message': problem.message, 'details': problem.details}
    if cap is None:
        return payload
    raw = problem.details if isinstance(problem.details, list) else []
    payload['details'] = public_problem_details(raw, cap)
    payload['total'] = len(raw)
    return payload


def public_problem_details(details, cap):
    """Return the public issue fields retained in a capped stored problem envelope."""
    raw = details if isinstance(details, list) else []
    return [{key: item[key] for key in _PUBLIC_PROBLEM_DETAIL_FIELDS if key in item}
            for item in raw if isinstance(item, dict)][:cap]


def require(condition: bool, code: str, message: str, status: int = 422, details=None):
    if not condition:
        raise Problem(status, code, message, details)


def safe_message(text: str) -> str:
    """Our own messages are plain sentences; library errors quote the offending input (int('â€¦'), fromisoformat('â€¦'),
    a union tag), so anything quoted (not an apostrophe inside a word), bracketed or escaped becomes a generic message."""
    risky = re.search(r"[\x00-\x1f\"`\\<>{}\[\]]|(?<![A-Za-z])'|'(?![A-Za-z])", text or "")
    return VALUE_INVALID_MESSAGE if not text or risky else text[:300]
