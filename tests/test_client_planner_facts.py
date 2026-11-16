"""The Planner hover card's hours and rest, from the real compiled planner_week.ts in Node (logic only, no DOM)."""
import json
from pathlib import Path
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[1]
P = 'p1'


def facts(client_modules, shifts, shift_id):
    script = (f"const m=await import({json.dumps((client_modules/'planner_week.js').as_uri())});const i=JSON.parse(process.argv[1]);"
              "const s=i.shifts.find(x=>x.id===i.id);"
              "console.log(JSON.stringify(m.workFacts(m.attendance(i.shifts,'p1'),s,x=>x.start.slice(0,10),0)));")
    out = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps({'shifts': shifts, 'id': shift_id})],
                         capture_output=True, text=True, check=True, timeout=15)
    return json.loads(out.stdout)


def session(sid, start, end, attend=None, segments=None):
    part = {'person_id': P, 'start': f'{attend[0] if attend else start}+03:00', 'end': f'{attend[1] if attend else end}+03:00'}
    if segments:
        part['segments'] = [{'start': a+'+03:00', 'end': b+'+03:00'} for a, b in segments]
    return {'id': sid, 'start': start+'+03:00', 'end': end+'+03:00', 'participants': [part]}


A = session('a', '2026-10-05T08:00:00', '2026-10-05T16:00:00', attend=('2026-10-05T08:00:00', '2026-10-05T12:00:00'))
B = session('b', '2026-10-05T08:00:00', '2026-10-05T16:00:00', attend=('2026-10-05T12:00:00', '2026-10-05T16:00:00'))
OTHER_CLINIC = session('c', '2026-10-04T00:00:00', '2026-10-04T08:00:00')


def test_partial_attendance_counts_the_attended_hours_not_whole_sessions(client_modules):
    assert facts(client_modules, [A, B], 'b') == {'week': 8, 'rota': 8, 'rest': None}


def test_back_to_back_attendance_rests_before_the_stretch_and_counts_a_filtered_out_clinic(client_modules):
    # The other clinic's night is hidden by the toolbar's filter but still counts: 4+4+8 hours, rested 24 h before 08:00.
    assert facts(client_modules, [A, B, OTHER_CLINIC], 'b') == {'week': 16, 'rota': 16, 'rest': 24}


def test_presence_segments_are_the_attended_intervals(client_modules):
    split = session('s', '2026-10-05T08:00:00', '2026-10-05T16:00:00',
                    segments=[('2026-10-05T08:00:00', '2026-10-05T10:00:00'), ('2026-10-05T14:00:00', '2026-10-05T16:00:00')])
    assert facts(client_modules, [split], 's')['rota'] == 4


def test_the_card_reads_the_whole_rota_before_filters():
    source = (ROOT/'apps/web/src/planner_week.ts').read_text(encoding='utf-8')
    assert 'rota=hooks.rota||hooks.unfiltered||data.shifts' in source and 'attendance(rota,pid)' in source
    app = (ROOT/'apps/web/src/app.ts').read_text(encoding='utf-8')
    assert 'const {rota,unfiltered}=plannerSessions(shown.shifts,' in app and 'weekHooks.rota=rota;' in app


def test_my_duties_hides_a_coworkers_other_session_from_the_grid_but_not_from_their_facts(client_modules):
    # The viewer (me) shares a 4 h session with p1; p1's 8 h night before it is not the viewer's, so My duties hides it.
    night = session('n', '2026-10-04T08:00:00', '2026-10-04T16:00:00')
    shared = session('x', '2026-10-05T08:00:00', '2026-10-05T12:00:00')
    shared['participants'].append({'person_id': 'me', 'start': shared['start'], 'end': shared['end']})
    script = (f"const m=await import({json.dumps((client_modules/'planner_week.js').as_uri())});const i=JSON.parse(process.argv[1]);"
              "const r=m.plannerSessions(i.shifts,'me',false);const s=r.rota.find(x=>x.id==='x');"
              "console.log(JSON.stringify({grid:r.unfiltered.map(x=>x.id),facts:m.workFacts(m.attendance(r.rota,'p1'),s,x=>x.start.slice(0,10),0)}));")
    out = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps({'shifts': [night, shared]})],
                         capture_output=True, text=True, check=True, timeout=15)
    assert json.loads(out.stdout) == {'grid': ['x'], 'facts': {'week': 12, 'rota': 12, 'rest': 16}}
