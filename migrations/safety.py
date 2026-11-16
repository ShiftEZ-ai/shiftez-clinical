"""Shared verified migration classes for CI and deployment; see docs/release/data-safety.md."""
import ast
import hashlib
import json
from pathlib import Path
from migrations.source_safety import CLASSES, analyze, parse

VERSIONS = Path(__file__).resolve().parent / 'versions'
MIN_REASON = 40  # a sentence, not "ok"

# Closed historical review inventory. Pending class-3 IDs still require owner confirmation:
#   0001_core                   downgrade drops every table (initial schema; never run by the pipeline)
#   0021..0024                  SQLite only: DROP TRIGGER + identical re-create of the publication contract triggers
#   0027_fixed_assignments      backfill UPDATE assignments SET fixed; downgrade drops the column
#   0028_job_progress           backfill UPDATE jobs SET heartbeat_at; downgrade drops the columns
#   0029..0033                  downgrade drops the added display/default columns
#   0034_duty_colour_automatic  rewrites the 'teal' colour default in assignment_types.definition
#   0036_planner_saved_views    copies planner_views into table_views, then DELETE FROM planner_views
#   0042_rate_limits            downgrade drops the rate_limits table (disposable counters)
GRANDFATHERED = frozenset({
    '0001_core', '0021_session_units', '0022_session_unit_basis', '0023_rolling_quotas', '0024_prorated_targets',
    '0027_fixed_assignments', '0028_job_progress', '0029_generation_defaults', '0030_clinic_closure',
    '0031_person_codes_tags', '0032_department_display', '0033_catalogue_colours', '0034_duty_colour_automatic',
    '0036_planner_saved_views', '0042_rate_limits'})


def _module(path):
    return parse(Path(path).read_text(encoding='utf-8'))


def _assigned(tree, name):
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets) \
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return node.value.value
    return None


def revision(path):
    return _assigned(_module(path), 'revision')


def marker(path):
    """The reviewed reason, or None when absent/too short to be a real review."""
    reason = (_assigned(_module(path), 'DESTRUCTIVE_REVIEWED') or '').strip()
    if not reason:
        _, record = declaration(path)
        reason = (record or {}).get('review', '').strip()
    return reason if len(reason) >= MIN_REASON else None


def declaration(path):
    """Module declarations for new revisions; source-bound declarations preserve delivered files."""
    source = Path(path).read_text(encoding='utf-8')
    tree = parse(source)
    catalogue = Path(__file__).with_name('safety_declarations.json')
    records = json.loads(catalogue.read_text(encoding='utf-8')) if catalogue.exists() else {}
    record = records.get(_assigned(tree, 'revision'))
    if record and record['sha256'] != hashlib.sha256(source.encode()).hexdigest():
        return None, None
    assignments = [node for node in tree.body if isinstance(node, (ast.Assign, ast.AnnAssign))
                   and any(isinstance(target, ast.Name) and target.id == 'SAFETY_CLASS'
                           for target in (node.targets if isinstance(node, ast.Assign) else [node.target]))]
    if assignments:
        if len(assignments) != 1 or not isinstance(assignments[0].value, ast.Constant):
            return None, None
        explicit = assignments[0].value.value
        return (explicit, record) if not record or explicit == record['class'] else (None, None)
    if record:
        return record['class'], record
    return None, None


def declaration_errors(path):
    declared, _ = declaration(path)
    inferred = analyze(path)['class']
    if declared not in CLASSES:
        return ['Missing or invalid SAFETY_CLASS declaration (delivered source may have changed)']
    if declared != inferred:
        return [f'SAFETY_CLASS {declared!r} contradicts source classification {inferred!r}']
    return []


def classification(path):
    """Invalid or contradictory declarations never turn unproved operations into automatic ones."""
    return 'destructive' if declaration_errors(path) else analyze(path)['class']


def findings(path):
    return analyze(path)['findings']


def unreviewed(path):
    """Destructive findings that neither the grandfather list nor a DESTRUCTIVE_REVIEWED marker covers."""
    errors = declaration_errors(path)
    if errors:
        return errors
    if classification(path) != 'destructive' or revision(path) in GRANDFATHERED or marker(path):
        return []
    _, record = declaration(path)
    return [] if record and len(record.get('review', '').strip()) >= MIN_REASON else analyze(path)['findings']


def needs_confirmation(path):
    """Every pending class-3 revision needs exact-ID production approval."""
    return classification(path) == 'destructive'


def reviewed_confirmations():
    """Reviewed revision IDs for disposable fixtures/rehearsals, never production approval."""
    return tuple(sorted(revision(path) for path in VERSIONS.glob('*.py')
                        if needs_confirmation(path) and not unreviewed(path)))


if __name__ == '__main__':
    print(','.join(reviewed_confirmations()))
