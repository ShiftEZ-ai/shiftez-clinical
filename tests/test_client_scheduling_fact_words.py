"""D-094 from the real compiled client in Node: a person-fact approval's Dates read its requested days (ends_on is
exclusive) through the shared date formatter, and its Details read the weekdays in the department's week order —
never the server's ISO dates, Monday-first weekday words or "Proposed <date>". Title, times and numbers come from the
structured fields (title, start_time/end_time, numbers in the Rules' one style), never the legacy `summary`."""
import json
import re
import subprocess

from .test_client_staff_states import SHIM


def words(client_modules, week_start, row):
    base = {'source': 'scheduling_fact', 'kind': 'protected_time', 'proposer_name': 'Coordinator · Demo',
            'proposed_at': '2026-11-05T08:00:00Z', 'weekdays': [], 'starts_on': None, 'ends_on': None,
            'calendar_entry_name': None, 'start_time': None, 'end_time': None, 'numbers': {},
            'summary': 'LEGACY SUMMARY · Maximum 9 · 2026-01-01'}  # never read (D-094)
    script = (SHIM + f"const d=await import({json.dumps((client_modules / 'dom.js').as_uri())});"
              f"const m=await import({json.dumps((client_modules / 'requests.js').as_uri())});"
              f"d.setWeekStart({week_start},'UTC');const r={json.dumps({**base, **row})};"
              "console.log(JSON.stringify([m.requestWhen(r,'Asia/Riyadh'),m.factWords(r).join(' · '),m.requestTitle(r)]));")
    out = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, check=True, timeout=15)
    got = json.loads(out.stdout)
    assert not any('LEGACY' in text or 'Maximum 9' in text for text in got), got
    return got


PROTECTION = {'title': 'Calendar protection', 'catalogue_id': 'protected_time', 'weekdays': [0, 6], 'start_time': '09:00',
              'end_time': '11:00', 'numbers': {'apply': {'value': None, 'level': 'mandatory'}}, 'calendar_entry_name': 'Block'}


def test_dates_show_the_requested_days_with_an_exclusive_end(client_modules):
    assert words(client_modules, 0, {**PROTECTION, 'starts_on': '2026-12-20', 'ends_on': '2026-12-22'})[0] == '20–21 Dec 2026'
    assert words(client_modules, 0, {**PROTECTION, 'starts_on': '2026-12-20', 'ends_on': '2026-12-21'})[0] == '20 Dec 2026'
    assert words(client_modules, 0, {**PROTECTION, 'starts_on': '2026-12-31', 'ends_on': '2027-01-02'})[0] == '31 Dec 2026 – 1 Jan 2027'
    assert words(client_modules, 0, {**PROTECTION, 'starts_on': '2026-12-28', 'ends_on': '2027-01-04'})[0] == '28 Dec 2026 – 3 Jan 2027'
    assert words(client_modules, 0, {**PROTECTION, 'starts_on': '2026-09-28', 'ends_on': '2026-10-04'})[0] == '28 Sep 2026 – 3 Oct 2026'


def test_open_ended_dates(client_modules):
    assert words(client_modules, 0, {**PROTECTION, 'starts_on': '2026-12-20'})[0] == 'From 20 Dec 2026'
    assert words(client_modules, 0, {**PROTECTION, 'ends_on': '2027-03-01'})[0] == 'Until 28 Feb 2027'
    assert words(client_modules, 0, PROTECTION)[0] == ''   # no dates: an empty cell, as other undated rows


def test_details_follow_the_departments_week_order_and_never_show_iso(client_modules):
    row = {**PROTECTION, 'starts_on': '2026-12-20', 'ends_on': '2026-12-22'}
    when, detail, _ = words(client_modules, 6, row)
    assert detail == 'Sun, Mon 09:00–11:00'          # Sunday-first department; the entry name is the Person column's
    assert words(client_modules, 0, row)[1] == 'Mon, Sun 09:00–11:00'
    for text in (when, detail):
        assert not re.search(r'\d{4}-\d{2}-\d{2}', text) and 'Proposed' not in text and 'before' not in text


def test_title_is_the_structured_title(client_modules):
    assert words(client_modules, 0, PROTECTION)[2] == 'Calendar protection'
    assert words(client_modules, 0, {**PROTECTION, 'title': 'Stop calendar protection'})[2] == 'Stop calendar protection'


def test_times_are_omitted_when_the_line_has_none(client_modules):
    assert words(client_modules, 0, {**PROTECTION, 'start_time': None, 'end_time': None})[1] == 'Mon, Sun'
    offer = {**PROTECTION, 'kind': 'overtime_offer', 'title': 'Overtime offer', 'weekdays': [1, 2, 3], 'start_time': '16:00', 'end_time': '20:00'}
    assert words(client_modules, 0, offer)[1:] == ['Tue–Thu 16:00–20:00 · No offer cap', 'Overtime offer']
    assert words(client_modules, 0, {**offer, 'overtime_duty_cap': 0})[1] == (
        'Tue–Thu 16:00–20:00 · At most 0 overtime duties in this period')
    assert words(client_modules, 0, {**offer, 'overtime_duty_cap': 1})[1] == (
        'Tue–Thu 16:00–20:00 · At most 1 overtime duty in this period')


def test_workload_and_target_numbers_use_the_rules_line_sentence(client_modules):
    """A quota reads as the Rules' line sentence, in words a screen reader says (never "≤"/"→"): numbers, measure,
    period and strength (the old summary said "Maximum 3 duties per calendar week"); a cleared number reads as the
    Rules' None, never "Maximum None"."""
    quota = {'kind': 'workload', 'title': 'Personal workload', 'catalogue_id': 'workload_limit', 'measure': 'duties',
             'period': {'kind': 'calendar_week'}}
    said = lambda row: words(client_modules, 0, row)[1]  # noqa: E731
    assert words(client_modules, 0, {**quota, 'numbers': {'max': {'value': 3, 'level': 'mandatory'}}}) ==         ['', 'At most 3 duties per calendar week · Mandatory', 'Personal workload']
    assert said({**quota, 'numbers': {'min': {'value': 2, 'level': 'mandatory'}}}) == 'At least 2 duties per calendar week · Mandatory'
    hours = {**quota, 'measure': 'hours', 'period': {'kind': 'rolling', 'days': 14}}
    assert said({**hours, 'numbers': {'target': {'value': 4, 'level': 'medium'}}}) == 'About 4 hours in any 14 days · Advisory · medium'
    assert said({**quota, 'numbers': {'min': {'value': 1, 'level': 'mandatory'}, 'max': {'value': 3, 'level': 'mandatory'}}}) ==         'Between 1 and 3 duties per calendar week · Mandatory'
    # Cleared (was "Remove personal maximum"): the Rules' None chip says "No maximum for this line".
    assert said({**quota, 'numbers': {'max': {'value': None, 'level': None}}}) == 'No maximum'
    # No measure: a personal target says its number (its goal's metric is not on the row), a no-duties line its words.
    target = {'kind': 'target', 'title': 'Personal target', 'catalogue_id': 'fair_share', 'numbers': {'target': {'value': 4, 'level': 'medium'}}}
    assert said(target) == 'Target 4 · Advisory · medium'
    assert said({**quota, 'measure': None, 'period': None, 'numbers': {'apply': {'value': None, 'level': 'mandatory'}}}) ==         'No duties · Mandatory'
    for row in (target, {**hours, 'numbers': {'target': {'value': 4, 'level': 'medium'}}}):
        assert not any(sign in said(row) for sign in '≤≥→')


def test_dates_filter_and_sort_never_act_on_the_proposed_day(client_modules):
    """Dates · Start / End and the Dates sort read the requested days (as the server's filter does), never proposed_at."""
    rows = [{**PROTECTION, 'starts_on': '2026-12-20', 'ends_on': '2026-12-22'}, {**PROTECTION, 'starts_on': '2026-12-20'},
            {**PROTECTION, 'ends_on': '2027-03-01'}, PROTECTION]
    script = (SHIM + f"const m=await import({json.dumps((client_modules / 'requests.js').as_uri())});"
              f"const rows={json.dumps(rows)}.map(r=>({{source:'scheduling_fact',proposed_at:'2026-11-05T08:00:00Z',...r}}));"
              "console.log(JSON.stringify(rows.map(r=>[m.requestFirstDay(r,'Asia/Riyadh'),m.requestLastDay(r,'Asia/Riyadh'),m.requestSortKey(r)])));")
    got = json.loads(subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True,
                                    check=True, timeout=15).stdout)
    assert got == [['2026-12-20', '2026-12-21', '2026-12-20'], ['2026-12-20', '', '2026-12-20'],
                   ['', '2027-02-28', ''], ['', '', '']]
