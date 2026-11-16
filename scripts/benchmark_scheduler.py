"""Compare schedulers against frozen input with a bounded search budget.

Input is an exported job containing `input`. Full results can contain personal
records: select a private output directory. No records are changed or applied.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import time
import sys

try:
    import resource
except ModuleNotFoundError:
    resource = None

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packages.domain.cp_sat import generate as cp_sat
from packages.domain.generation import generate as greedy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('engine', choices=['cp_sat', 'greedy'])
    parser.add_argument('output', type=Path)
    parser.add_argument('--source-commit', required=True)
    parser.add_argument('--expected-sha256', required=True)
    parser.add_argument('--seconds', type=int, choices=[60, 300, 600], default=300)
    args = parser.parse_args()
    raw = args.source.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != args.expected_sha256:
        parser.error('Source fingerprint does not match the frozen input')
    snapshot = json.loads(raw.decode('utf-8-sig'))['input']
    started = time.monotonic()
    result = (cp_sat if args.engine == 'cp_sat' else greedy)(snapshot, time_budget_seconds=args.seconds)
    summary = {'source_commit': args.source_commit, 'engine': args.engine, 'input_sha256': digest,
        'search_budget_seconds': args.seconds, 'total_seconds': time.monotonic() - started,
        'status': result['status'], 'phase_timings': result.get('phase_timings'),
        'first_solution_seconds': result.get('first_solution_seconds'),
        'peak_rss_mib': (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss /
                         (1024 ** 2 if sys.platform == 'darwin' else 1024)) if resource else None,
        'additions': len(result.get('additions', [])), 'removals': len(result.get('removals', [])),
        'optimality_proven': result.get('optimality_proven', False),
        'validation_performed': bool(result.get('validation')),
        'blocking_findings': result.get('validation', {}).get('error_count'),
        'advisory_findings': result.get('validation', {}).get('warning_count'),
        'validation_counts': dict(Counter(f['code'] for f in result.get('validation', {}).get('findings', []))),
        'diagnostic_counts': dict(Counter(d['code'] for d in result.get('diagnostics', [])))}
    for setting in ('cpu.max', 'memory.max'):
        path = Path('/sys/fs/cgroup') / setting
        summary[setting] = path.read_text().strip() if path.exists() else None
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    (args.output / 'result.json').write_text(json.dumps(result), encoding='utf-8')
    print(json.dumps(summary), flush=True)


if __name__ == '__main__':
    main()
