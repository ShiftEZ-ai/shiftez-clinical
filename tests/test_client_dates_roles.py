"""Real compiled client helpers in Node (logic only, no DOM): Copy's clamped "Next month" and role-grant overlaps
checked against the chosen dates."""
import json
import subprocess

import pytest



def run(client_modules, module, expression, payload=None):
    script = (f"globalThis.document={{cookie:''}};const m=await import({json.dumps((client_modules/module).as_uri())});"
              f"const input=JSON.parse(process.argv[1]);console.log(JSON.stringify({expression}));")
    result = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps(payload)],
                            capture_output=True, text=True, check=True, timeout=15)
    return json.loads(result.stdout)


@pytest.mark.parametrize('day,expected', [
    ('2026-01-31', '2026-02-28'), ('2028-01-31', '2028-02-29'), ('2026-05-31', '2026-06-30'),
    ('2026-03-31', '2026-04-30'), ('2026-10-15', '2026-11-15'), ('2026-12-31', '2027-01-31')])
def test_next_month_keeps_the_day_or_clamps_to_the_month_end(client_modules, day, expected):
    assert run(client_modules, 'date_picker.js', 'm.shiftMonth(input,1)', day) == expected


GRANTS = [
    {'starts_on': '2026-01-01', 'ends_on': '2026-03-01'},  # finite: ends (exclusive) before the later periods
    {'starts_on': '2026-06-01', 'ends_on': None, 'revoked': True},  # revoked: never counts
]


@pytest.mark.parametrize('start,until,expected', [
    ('2026-03-01', '2026-04-01', ''),  # starts the day the finite grant ends: free
    ('2026-05-01', None, ''),  # open-ended, after the finite grant; the revoked one is ignored
    ('2026-02-15', '2026-04-01', 'overlap'),  # meets the finite grant's last two weeks
    ('2025-12-01', '2026-01-02', 'overlap'),
    ('2026-01-10', '2026-02-10', 'covered'),
    ('2026-01-10', None, 'overlap'),  # open-ended against a finite grant: only part is covered
])
def test_role_grant_overlap_uses_the_chosen_dates(client_modules, start, until, expected):
    payload = {'grants': GRANTS, 'from': start, 'until': until}
    assert run(client_modules, 'planner_logic.js', 'm.grantOverlap(input.grants,input.from,input.until)', payload) == expected


def test_an_open_ended_current_grant_covers_every_later_period(client_modules):
    payload = {'grants': [{'starts_on': '2026-01-01', 'ends_on': None}], 'from': '2026-10-24', 'until': None}
    assert run(client_modules, 'planner_logic.js', 'm.grantOverlap(input.grants,input.from,input.until)', payload) == 'covered'


def test_format_defaults_to_department_zone(client_modules):
    """maintenance: an instant formatted without a zone reads in the department's zone, never a hard-coded one (London here: the
    evening of 4 Oct, already 5 Oct in Riyadh)."""
    got = run(client_modules, 'dom.js', "(m.setWeekStart(1,'Europe/London'),[m.formatDate(input),m.formatDay(input)])",
              '2026-10-04T21:30:00Z')
    assert got == ['4 Oct 2026', 'Sun 4 Oct']


@pytest.mark.parametrize('zone', ['Pacific/Kiritimati', 'Pacific/Tongatapu', 'Pacific/Pago_Pago', 'Asia/Riyadh'])
def test_plain_days_keep_their_calendar_day_in_every_zone(client_modules, zone):
    """A date-only value (rule/grant/effective date) is a calendar day, never shifted by the
    department zone (UTC+14 would read the next day, UTC-11 the previous one); instants still use the zone."""
    got = run(client_modules, 'dom.js', f"(m.setWeekStart(1,{json.dumps(zone)}),[m.formatDate(input),m.formatDay(input),m.formatDay(input,undefined,true)])",
              '2026-10-04')
    assert got == ['4 Oct 2026', 'Sun 4 Oct', 'Sun 4 Oct 2026']


def test_instants_still_read_in_the_department_zone(client_modules):
    got = run(client_modules, 'dom.js', "(m.setWeekStart(1,'Pacific/Kiritimati'),m.formatDate(input))", '2026-10-04T12:00:00Z')
    assert got == '5 Oct 2026'
