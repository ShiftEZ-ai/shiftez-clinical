#!/usr/bin/env python3
"""Bind a clean-source verification run to tracked release evidence.

Run AFTER verify.py, before the documentation-only evidence commit. An optional
--attempt records a failed full-environment attempt without promoting it to a
passed gate. Source changes or a dirty checkout require a new verification run.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = {'apps', 'packages', 'tests', 'scripts', 'migrations', 'requirements', 'infra', '.github'}
SOURCE_FILES = {'pyproject.toml', 'package.json', 'package-lock.json', 'alembic.ini', '.env.example', '.gitignore', '.dockerignore'}
LOGS = ('contracts.log', 'compile.log', 'frontend.log', 'tests.log', 'junit.xml', 'verification.json')


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(['git', *args], cwd=root, text=True).strip()


def implementation_files(root: Path) -> list[dict]:
    """Hash exactly the tracked implementation/test/deployment inputs, not evidence."""
    paths = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).decode().split('\0')
    files = []
    for name in sorted(filter(None, paths)):
        path = Path(name)
        if path.parts[0] not in SOURCE_DIRS and name not in SOURCE_FILES:
            continue
        content = (root / path).read_bytes()
        files.append({'path': name, 'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content)})
    return files


def source_fingerprint(root: Path) -> str:
    return hashlib.sha256(json.dumps(implementation_files(root), sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def load_run(directory: Path) -> tuple[dict, dict, dict]:
    report = json.loads((directory / 'verification.json').read_text())
    cases = list(ET.parse(directory / 'junit.xml').getroot().iter('testcase'))
    totals = {'tests': len(cases), 'passed': 0, 'failed': 0, 'errors': 0, 'skipped': 0}
    for case in cases:
        key = ('errors' if case.find('error') is not None else 'failed' if case.find('failure') is not None
               else 'skipped' if case.find('skipped') is not None else 'passed')
        totals[key] += 1
    text = (directory / 'tests.log').read_text()
    deselected = re.findall(r'(\d+) deselected', text)
    totals['deselected'] = int(deselected[-1]) if deselected else 0
    coverage = json.loads((directory / 'coverage.json').read_text())
    summary = {'meta': coverage['meta'], 'totals': coverage['totals'],
               'files': {name: item['summary'] for name, item in sorted(coverage['files'].items())},
               'scope': 'Measured apps/api and packages/domain Python line coverage, not branch, frontend, '
                        'worker, operator-script, requirements or institutional-acceptance coverage.'}
    for name in LOGS:
        if not (directory / name).is_file():
            raise ValueError(f'Missing run evidence: {directory / name}')
    return report, totals, summary


def record_release(root: Path, run: Path, attempt: Path | None = None) -> dict:
    root, run = root.resolve(), run.resolve()
    head = git(root, 'rev-parse', 'HEAD')
    if git(root, 'status', '--porcelain'):
        raise ValueError('Commit all source and documentation changes before recording evidence')
    report, totals, coverage = load_run(run)
    def check_source(run_report):
        if (run_report.get('git_head_at_start') != head or run_report.get('working_tree_dirty') is not False
                or run_report.get('git_head_at_end') != head or run_report.get('working_tree_dirty_at_end') is not False
                or run_report.get('source_unchanged') is not True
                or run_report.get('source_fingerprint_at_start') != source_fingerprint(root)
                or run_report.get('source_fingerprint_at_end') != source_fingerprint(root)):
            raise ValueError('Verification must start and finish on this exact clean, unchanged source commit')
    check_source(report)
    gates = report.get('results', [])
    if (report.get('selected_gates_passed') is not True or len(gates) != 4
            or {gate['gate'] for gate in gates} != {'contracts', 'compile', 'frontend', 'tests'}
            or any(gate['exit_code'] != 0 for gate in gates)
            or totals['failed'] or totals['errors'] or totals['skipped'] or totals['passed'] == 0):
        raise ValueError('The primary run must have all selected gates and tests passing without skips')
    attempt_data = None
    if attempt is not None:
        attempt = attempt.resolve()
        attempt_data = load_run(attempt)
        check_source(attempt_data[0])
    manifest = {'schema_version': 2, 'source_commit': head,
                'scope': 'Git-tracked application, tests, build, migration, dependency and deployment source; '
                         'excludes documentation/evidence and private reference to avoid circular hashes.', 'files': []}
    manifest['files'] = implementation_files(root)
    if not manifest['files']:
        raise ValueError('No tracked application/source files found')
    output = root / 'docs' / 'evidence'
    if output == run or output in run.parents or (attempt and (output == attempt or output in attempt.parents)):
        raise ValueError('Run evidence must be outside the destination evidence directory')
    # All prerequisites are checked before changing the documentation tree.
    output.mkdir(parents=True, exist_ok=True)
    for name in LOGS:
        shutil.copyfile(run / name, output / name)
    shutil.copyfile(run / 'tests.log', output / 'test-output.txt')
    (output / 'coverage-summary.json').write_text(json.dumps(coverage, indent=2) + '\n')
    (output / 'source-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    qualification = {'schema_version': 1, 'recorded_at': datetime.now(timezone.utc).isoformat(),
                     'source_commit': head, 'local_run': {'selected': report['selected'], 'totals': totals,
                     'selected_gates_passed': True}, 'production_ready': False,
                     'boundary': 'Passing selected tests does not close omitted, failed, externally unqualified '
                                 'or still-unimplemented blueprint requirements.'}
    attempts = output / 'full-environment-attempt'
    # A later recording without an attempt must not leave a previous attempt looking current.
    if attempts.exists():
        shutil.rmtree(attempts)
    if attempt_data:
        attempt_report, attempt_totals, _ = attempt_data
        attempts.mkdir()
        for name in LOGS:
            shutil.copyfile(attempt / name, attempts / name)
        qualification['full_environment_attempt'] = {'selected': attempt_report['selected'],
            'totals': attempt_totals, 'selected_gates_passed': attempt_report['selected_gates_passed']}
    (output / 'qualification-summary.json').write_text(json.dumps(qualification, indent=2) + '\n')
    return qualification


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True, help='Passing clean-source verify.py output directory')
    parser.add_argument('--attempt', type=Path, help='Optional actual full-environment attempt on the same source')
    args = parser.parse_args()
    try:
        result = record_release(ROOT, args.run, args.attempt)
    except (ValueError, OSError, KeyError, ET.ParseError) as exc:
        parser.exit(1, f'Evidence not recorded: {exc}\n')
    print(json.dumps(result, indent=2))
    print('Review the evidence and record the actual results.')


if __name__ == '__main__':
    main()
