"""Observed saved-assignment changes, independent of search feasibility proof."""
from .feasibility import compare_findings
from .validation import finding_observations


def observed_effect(before, after):
    def phase(validation):
        return {'findings': validation.get('findings', []), 'diagnostics': [], 'candidate_evidence': []}
    baseline, proposed = phase(before), phase(after)
    def delta(severities):
        forward = compare_findings(baseline, proposed, severities=severities, protect_intervals=False)
        reverse = compare_findings(proposed, baseline, severities=severities, protect_intervals=False)
        return ({'clears': sum(finding['change'] == 'new' for finding in reverse['new_or_worse_findings']),
                 'adds': len(forward['new_or_worse_findings'])}, forward['recited'])
    (mandatory, mandatory_recited), (advisory, advisory_recited) = delta(('error',)), delta(('warning',))
    complete = all((v.get('quota_report') or {}).get('complete', True) and
                   not any(f.get('implementation_error') or f.get('proven') is False for f in finding_observations(v))
                   for v in (before, after))
    clears, adds = (mandatory[key] + advisory[key] for key in ('clears', 'adds'))
    return {'status': 'ready' if complete else 'unresolved', 'clears': clears, 'adds': adds,
            'mandatory': mandatory, 'advisory': advisory,
            'recited': mandatory_recited + advisory_recited,
            'effect': 'adds' if adds else 'clears' if clears else 'no_change',
            'summary': (f'Clears {clears} check' + ('s' if clears != 1 else '') + (f'; adds {adds}' if adds else '')
                        if clears else f'Adds {adds} check' + ('s' if adds != 1 else '') if adds else 'No change') if complete else 'Check incomplete'}
