"""A session's places as the Schedule grid reads them (the real compiled planner_week.ts coverOf in Node, logic only):
someone absent now fills no place, whether or not the last check knew of the absence."""
import json
import subprocess


def cover(client_modules, shift, row, *, participants_filtered=False):
    data = {'workspace': {'validation': {'coverage': [row]}}, 'shifts': [shift],
            'participantsFiltered': participants_filtered}
    script = (f"const m=await import({json.dumps((client_modules/'planner_week.js').as_uri())});"
              "const c=m.coverOf(JSON.parse(process.argv[1])).get('s');"
              "console.log(JSON.stringify({available:c.available,open:c.open,gap:c.gap}));")
    out = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps(data)],
                         capture_output=True, text=True, check=True, timeout=15)
    return json.loads(out.stdout)


ONE = {'shift_id': 's', 'status': 'covered', 'required': 1, 'scheduled': 1, 'available_eligible': 1,
       'places_needed': 1, 'places_filled': 1, 'places_open': 0}
LEAVE = [{'person_id': 'p1', 'code': 'UNAVAILABLE'}]


def session(**flags):
    return {'id': 's', 'participants': [{'id': 'a1', 'person_id': 'p1'}], **flags}


def test_the_only_assignee_on_leave_after_the_check_leaves_the_place_open(client_modules):
    assert cover(client_modules, session(double_booking_conflicts=LEAVE), ONE) == {'available': 0, 'open': 1, 'gap': True}


def test_a_reported_absence_also_leaves_the_place_open(client_modules):
    assert cover(client_modules, session(absence_reports=[{'person_id': 'p1'}]), ONE) == {'available': 0, 'open': 1, 'gap': True}


def test_an_absence_the_check_already_counted_is_not_counted_twice(client_modules):
    known = {**ONE, 'status': 'gap', 'available_eligible': 0, 'places_filled': 0, 'places_open': 1}
    assert cover(client_modules, session(double_booking_conflicts=LEAVE), known) == {'available': 0, 'open': 1, 'gap': True}


def test_nobody_absent_keeps_the_checked_places(client_modules):
    assert cover(client_modules, session(), ONE) == {'available': 1, 'open': 0, 'gap': False}
    # Another kind of conflict (a double booking) is not an absence: the person still fills the place.
    other = session(double_booking_conflicts=[{'person_id': 'p1', 'code': 'TIME_OVERLAP'}])
    assert cover(client_modules, other, ONE) == {'available': 1, 'open': 0, 'gap': False}


def test_filtered_staff_participants_keep_the_authoritative_coverage(client_modules):
    full = {**ONE, 'required': 3, 'scheduled': 3, 'available_eligible': 3, 'places_needed': 3, 'places_filled': 3}
    gap = {**full, 'status': 'gap', 'available_eligible': 2, 'places_filled': 2, 'places_open': 1}
    assert cover(client_modules, session(), full, participants_filtered=True) == {'available': 3, 'open': 0, 'gap': False}
    assert cover(client_modules, session(), gap, participants_filtered=True) == {'available': 2, 'open': 1, 'gap': True}
