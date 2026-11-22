"""Cover precedence when overlapping sessions share just one eligible person."""

from apps.worker.main import process_one
from packages.domain.cp_sat import generate
from packages.domain.validation import finding_observations
from tests.test_template_scheduler_audit import use_cp_sat


def contended_cover(h, minimum=1, *, advisory=False):
    clinic = h.configure('services', {'name': 'Contended service', 'starts_on': '2020-01-01'})['record']
    h.seed['services'].append(clinic['id'])
    h.command('credentialer', 'eligibility.create', {
        'person_id': h.seed['people'][0], 'service_id': clinic['id'], 'starts_on': '2020-01-01',
    })
    short_type = h.configure('assignment_types', {
        'name': 'Short cover', 'starts_on': '2020-01-01', 'definition': {'requirements': []},
    })['record']['id']
    long_type = h.configure('assignment_types', {
        'name': 'Long desired cover', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 0, 'target': 1, 'maximum': 1}]},
    })['record']['id']
    scope = {'assignment_type_id': short_type}
    if advisory:
        h.cover_target(minimum, scope)
    else:
        h.policy({'kind': 'minimum_cover', 'minimum': minimum}, scope)
    workspace = h.shift(h.workspace(), end='2026-10-04T09:00:00+03:00',
                        people=[], service=2, typ=short_type)['workspace']
    workspace = h.shift(workspace, end='2026-10-04T14:00:00+03:00',
                        people=[], service=2, location=1, typ=long_type)['workspace']
    snapshot = h.snapshot(workspace)
    sessions = {s['assignment_type_id']: s['id'] for s in snapshot['shifts']}
    return workspace, snapshot, sessions[short_type], sessions[long_type]


def direct(snapshot, *, diagnostic_proposal=False):
    return generate(snapshot, 5, deterministic=True, diagnostic_proposal=diagnostic_proposal)


def scoped_gaps(result, session, severity):
    return [f for f in finding_observations(result.get('validation') or {})
            if f['shift_id'] == session and f['code'] == 'SCOPED_COVERAGE_GAP' and f['severity'] == severity]


def test_required_short_session_precedes_long_desired_session(h):
    _, snapshot, short, _ = contended_cover(h)
    result = direct(snapshot)
    assert result['status'] == 'candidate_feasible'
    assert [a['shift_id'] for a in result['additions']] == [short]
    assert result['validation']['valid']


def test_infeasible_required_cover_stays_visible_and_keeps_reachable_cover(h):
    _, snapshot, short, _ = contended_cover(h, minimum=2)
    strict = direct(snapshot)
    assert strict['status'] == 'requirements_conflict'
    result = direct(snapshot, diagnostic_proposal=True)
    assert result['status'] == 'candidate_with_gaps'
    assert [a['shift_id'] for a in result['additions']] == [short]
    gaps = scoped_gaps(result, short, 'error')
    assert [(f['detail']['available'], f['detail']['required']) for f in gaps] == [(1, 2)]


def test_advisory_shortfall_is_named_when_long_desired_cover_wins(h):
    _, snapshot, short, long = contended_cover(h, advisory=True)
    result = direct(snapshot)
    assert result['status'] == 'candidate_feasible'
    assert [a['shift_id'] for a in result['additions']] == [long]
    assert scoped_gaps(result, short, 'warning')
    assert any(row['shift_id'] == short and row['finding_code'] == 'SCOPED_COVERAGE_GAP'
               for row in result['allocation_diagnostics'])


def start_job(h, workspace):
    return run_job(h, h.command('coordinator', 'job.create', {
        'workspace_id': workspace['id'], 'kind': 'generation',
        'generation_options': {'on_conflict': 'report', 'search_minutes': 1},
    }, revision=workspace['revision'])['job'])


def continue_job(h, job):
    return run_job(h, h.command('coordinator', 'job.continue', {
        'reason': 'Fill everything else with gaps flagged',
    }, job['id'])['job'])


def run_job(h, job):
    assert process_one(h.app.state.engine, settings=h.settings)
    return h.get('coordinator', '/api/jobs/' + job['id'])['job']


def test_fill_everything_else_keeps_the_reachable_required_seat(h):
    use_cp_sat(h)
    workspace, _, short, _ = contended_cover(h, minimum=2)
    strict = start_job(h, workspace)
    assert strict['result']['status'] == 'requirements_conflict'
    assert strict['result']['resume_available']
    result = continue_job(h, strict)['result']
    assert result['status'] == 'candidate_with_gaps'
    assert [a['shift_id'] for a in result['additions']] == [short]
    assert scoped_gaps(result, short, 'error')
