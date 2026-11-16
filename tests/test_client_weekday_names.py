"""Sweep N-02: chosen weekdays read in the department's week order (dom.ts weekdayNames, the one list every request,
wish and rule line uses), never in the order they were stored."""
import json
import subprocess


def names(client_modules, week_start, days, short=False):
    script = (f"const m=await import({json.dumps((client_modules/'dom.js').as_uri())});"
              f"m.setWeekStart({week_start},'UTC');console.log(JSON.stringify(m.weekdayNames({json.dumps(days)},{json.dumps(short)})));")
    out = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, check=True, timeout=15)
    return json.loads(out.stdout)


def test_weekdays_follow_the_departments_first_day(client_modules):
    # Stored Monday-first (0 = Monday … 6 = Sunday): a Sunday-first week says Sunday first.
    assert names(client_modules, 6, [0, 6]) == 'Sunday, Monday'
    assert names(client_modules, 0, [6, 0]) == 'Monday, Sunday'
    assert names(client_modules, 6, [4, 1, 6], short=True) == 'Sun, Tue, Fri'
    assert names(client_modules, 6, []) == ''
