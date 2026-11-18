"""Final proposal truth is separate from search phases and diagnostic model proofs."""


SOLVER_STATUSES = frozenset({'OPTIMAL', 'FEASIBLE', 'INFEASIBLE', 'UNKNOWN'})
DIAGNOSTIC_PROPOSAL_MESSAGE = 'Incomplete diagnostic proposal. Review it; it is not a feasible operational rota.'
SCORE_OPTIMISATION_UNFINISHED = 'complete; score optimisation unfinished'


def solver_status(result):
    """Return the literal solver status retained on every generation result."""
    explicit = result.get('solver_status')
    if explicit in SOLVER_STATUSES:
        return explicit
    weighted = result.get('weighted_status')
    if weighted in SOLVER_STATUSES:
        return weighted
    if result.get('status') == 'requirements_conflict':
        return 'INFEASIBLE'
    outcome = result.get('generation_outcome') or {}
    return outcome.get('status') if outcome.get('status') in SOLVER_STATUSES else 'UNKNOWN'


def score_value(result):
    """Return the compact score only when available and final."""
    if result.get('score_completed') is False:
        return None
    summary = result.get('summary') or {}
    saved = summary.get('score_saved') or summary.get('score')
    if not isinstance(saved, dict):
        return None
    value = saved.get('score')
    # A candidate with mandatory findings has no publishable score, but its
    # uncapped score is still useful in the run receipt once it placed work.
    if value is None and (summary.get('filled_places') or 0) > 0:
        value = saved.get('uncapped_score')
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def generation_outcome(result):
    status = result.get('status')
    validation = result.get('validation') or {}
    retained = result.get('generation_outcome') or {}
    strict = (result.get('conflict') or {}).get('strict_status', retained.get('strict_status'))
    if status == 'requirements_conflict':
        strict = 'INFEASIBLE'
    candidate = 'none'
    if result.get('checkpointed'):
        candidate = 'with_gaps'
    elif status in {'candidate_feasible', 'candidate_with_gaps', 'unknown_time_limit', 'needs_more_time'}:
        candidate = ('with_gaps' if result.get('diagnostic_proposal') and
                     (validation.get('valid') is not None or validation.get('error_count')) else
                     'valid' if validation.get('valid') is True and not validation.get('error_count')
                     else 'with_gaps' if validation.get('valid') is False or validation.get('error_count') else 'none')
    optimal = candidate == 'valid' and result.get('optimality_proven', retained.get('optimality_proven')) is True
    return {'status': ('OPTIMAL' if optimal else 'FEASIBLE') if candidate == 'valid' else
                     'INFEASIBLE' if strict == 'INFEASIBLE' else 'UNKNOWN',
            'candidate': candidate, 'strict_status': strict, 'optimality_proven': optimal}
