"""Bounded precheck evidence for live scheduling progress, without finding descriptions."""
from packages.contracts.schemas import AUTOSCHEDULE_PRECHECK_LIMITS


def precheck_issues(findings, classification, previous=()):
    groups = {(issue['classification'], issue['code']): {**issue, 'samples': list(issue['samples'])}
              for issue in previous}
    for finding in findings:
        code = finding['code']
        group = groups.setdefault((classification, code),
            {'classification': classification, 'code': code, 'count': 0, 'samples': []})
        group['count'] += 1
        sample = {key: finding[key] for key in ('shift_id', 'person_id', 'rule_id') if finding.get(key)}
        if sample and sample not in group['samples'] and len(group['samples']) < AUTOSCHEDULE_PRECHECK_LIMITS['samples']:
            group['samples'].append(sample)
    return [groups[key] for key in sorted(groups)[:AUTOSCHEDULE_PRECHECK_LIMITS['issues']]]
