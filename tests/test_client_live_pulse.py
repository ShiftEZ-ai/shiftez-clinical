"""P0 edit latency (PERF slice c + LEAD-UIUX, 8 Nov 2026): edit_delta.ts withoutOwnCheck, the one place a live pulse's
changes lose this page's own rota check (its job_id: settle applies that job's delta). Someone else's change in the
same pulse must still be refreshed; a pulse that says only "your check landed", with nobody's activity moved, is
skipped. The real compiled module in Node (logic only, no DOM)."""
import json
import subprocess

ROTA, OWN_JOB = 'rota-1', 'job-own'


def without_own_check(client_modules, changes, moved):
    script = (f"const m=await import({json.dumps((client_modules/'edit_delta.js').as_uri())});const a=JSON.parse(process.argv[1]);"
              # The page's own check: a workspace change for the open rota naming the job it waits for.
              "const own=c=>c.scope==='workspace'&&c.id===a.rota&&c.job_id===a.job;"
              "console.log(JSON.stringify(m.withoutOwnCheck(a.changes,own,a.moved)));")
    out = subprocess.run(['node', '--input-type=module', '-e', script,
                          json.dumps({'changes': changes, 'moved': moved, 'rota': ROTA, 'job': OWN_JOB})],
                         capture_output=True, text=True, check=True, timeout=15)
    return json.loads(out.stdout)


def test_someone_elses_change_in_the_same_pulse_is_still_refreshed(client_modules):
    check = {'scope': 'workspace', 'id': ROTA, 'job_id': OWN_JOB}
    edit = {'scope': 'workspace', 'id': ROTA}  # another person's edit to the same rota (no job)
    got = without_own_check(client_modules, [check, edit], moved=True)
    assert got == {'changes': [edit], 'skip': False}
    # Even with nobody's activity counted as moved, a change other than the check is never dropped.
    assert without_own_check(client_modules, [check, edit], moved=False) == {'changes': [edit], 'skip': False}
    # Another check's job (another tab, another rota) is not this page's: it stays.
    other = {'scope': 'workspace', 'id': ROTA, 'job_id': 'job-other'}
    assert without_own_check(client_modules, [check, other], moved=False) == {'changes': [other], 'skip': False}


def test_a_pulse_that_is_only_this_pages_check_is_skipped(client_modules):
    check = {'scope': 'workspace', 'id': ROTA, 'job_id': OWN_JOB}
    assert without_own_check(client_modules, [check], moved=False) == {'changes': [], 'skip': True}
    # Somebody's activity moved: not skipped (the rest of the pulse logic decides what to read).
    assert without_own_check(client_modules, [check], moved=True) == {'changes': [], 'skip': False}


def test_a_pulse_without_the_check_is_untouched(client_modules):
    changes = [{'scope': 'requests', 'id': None}]
    assert without_own_check(client_modules, changes, moved=True) == {'changes': changes, 'skip': False}
    assert without_own_check(client_modules, None, moved=False) == {'changes': None, 'skip': False}
