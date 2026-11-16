INTEGRITY_CONFLICT_MESSAGE = "The write conflicts with a related or duplicate record; no partial change was committed"

PROBLEM_DETAIL_LIMIT = 50
_PUBLIC_PROBLEM_DETAIL_FIELDS = ('rule_id', 'rule_revision', 'field', 'code', 'message')


class Problem(Exception):
    def __init__(self, status: int, code: str, message: str, details=None):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.details = details


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
