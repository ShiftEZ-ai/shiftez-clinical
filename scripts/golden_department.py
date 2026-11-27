"""Build and seed one versioned synthetic department through public commands.

Run from the repository root: python -m scripts.golden_department --size department --api-base "$API_BASE"

Use a dedicated synthetic staging department with catalogue v2 and no existing
rotas. Supply existing session and CSRF values in GOLDEN_<ROLE>_SESSION and
GOLDEN_<ROLE>_CSRF for admin, credentialer, coordinator and approver. Sign in
separately as approver. Replay with the same identities and size to resume.
Use a separate department for each size. The receipt prints the draft ID,
input digest and generation options used by the checks.
"""
import argparse
from collections import defaultdict
from contextlib import ExitStack
from datetime import date, datetime, timedelta
import json
import os
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from scripts.golden_scenarios import PACK, command_key, compile_scenario


PACK_DIR = PACK.with_name('department-v1')
REASON = 'Versioned synthetic department fixture'
START = date(2027, 3, 1)
SIZES = {
    'micro': (1, 14, 0, 12),
    'department': (8, 31, 0, 60),
    'stress': (24, 31, 12, 90),
}
WEEKDAYS = (0, 1, 2, 3, 4)
EVERY_DAY = tuple(range(7))
ROLES = ('admin', 'credentialer', 'coordinator', 'approver')
STREAMS = {
    'early': ('08:00', '12:00', 'room'),
    'mid': ('12:30', '16:30', 'side'),
    'late': ('17:00', '21:00', 'room'),
    'teaching': ('09:00', '13:00', 'side'),
}
RESPONSE = {
    'response-am': ('07:00', '11:00', 'response-room'),
    'response-pm': ('12:00', '16:00', 'response-room'),
    'night': ('20:00', '06:00', 'response-room'),
}


def ref(key):
    return '$' + key


def condition(**fields):
    return {'conditions': [{'field': field, 'op': 'any', 'values': values} for field, values in fields.items()]}


def number(value=None, level='mandatory'):
    return {'level': level, **({'value': value} if value is not None else {})}


def day(offset):
    return (START + timedelta(days=offset)).isoformat()


def load_department(size='department', timezone='UTC'):
    groups, days, history_months, seconds = SIZES[size]
    pack = json.loads((PACK_DIR / 'spec.json').read_text(encoding='utf-8'))
    begins, ends = day(0), day(days)
    previous_month = START - timedelta(days=1)
    zone = ZoneInfo(timezone)
    spec = {'id': f'golden-department-{size}', 'revision': pack['revision'], 'commands': [],
            'people': {}, 'series': {}, 'history_months': history_months, 'expect': pack['expect'],
            'inputs': {'generation_options': {'mode': 'rebalance', 'diagnostic_seconds': 1,
                       'stop_without_improvement_seconds': 3, 'fairness_plateau_seconds': 3,
                       'allocation_order': 'tiered'}}}
    spec['expect']['search_seconds'] = seconds
    commands = spec['commands']
    rules = defaultdict(list)

    def instant(offset, clock):
        return datetime.fromisoformat(f'{day(offset)}T{clock}').replace(tzinfo=zone).isoformat()

    def add(key, role, kind, payload, **metadata):
        commands.append({'key': key, 'role': role, 'kind': kind, 'payload': payload, **metadata})

    def catalogue_item(catalogue, **value):
        return {'catalogue': catalogue, 'value': value, 'reason': REASON}

    def catalogue(key, item, **value):
        add(key, 'admin', 'catalogue.create', catalogue_item(item, **value))

    def bulk_catalogue(key, item, records):
        records = list(records)
        add(key, 'admin', 'bulk.apply', {'item_kind': 'catalogue.create', 'reason': REASON,
            'items': [{'payload': catalogue_item(item, **value)} for _, value in records]},
            bulk_records=[name for name, _ in records])

    def access(key, kind, people, **fields):
        role = 'admin' if kind == 'membership' else 'credentialer'
        add(key, role, kind + '.bulk_create',
            {'person_ids': [ref(person) for person in people], 'reason': REASON, **fields})

    def line(rule, key, numbers, *, who=None, where=None, kind=None, settings=None, **scope):
        entry = {'id': command_key(spec, f'line:{rule}:{key}'), 'numbers': numbers,
                 'starts_on': begins, 'ends_on': ends, **scope}
        if who is not None:
            entry['who'] = who
        if where is not None:
            entry['where'] = where
        if kind is not None:
            entry['settings'] = {kind: settings}
        rules[rule].append(entry)

    base = {'starts_on': '2025-01-01'}
    for key, title in pack['ranks'].items():
        catalogue(key, 'categories', name=title, **base)
    for key, title in pack['skills'].items():
        catalogue(key, 'capabilities', name=title, **base)
    for key, title in pack['programmes'].items():
        catalogue(key, 'programmes', name=title, **base)
        for cohort in range(2):
            catalogue(f'{key}-{cohort}', 'cohorts', name=f'{title} cohort {cohort + 1}',
                      programme_id=ref(key), **base, ends_on=ends)
    for key, title in pack['rotation_types'].items():
        catalogue(key, 'rotation_types', name=title, **base, leave_policy='allowed')

    catalogue('space', 'location_types', name='Fixture clinical space', **base)
    for key, title in [('site', 'Fixture main site'), ('annex', 'Fixture second site')]:
        catalogue(key, 'locations', name=title, location_type_id='$space', capacity=1000, **base)
    catalogue('response-room', 'locations', name='Fixture response room', location_type_id='$space',
              parent_id='$site', capacity=8, **base)
    catalogue('response', 'services', name='Fixture urgent response', **base)

    for key, title in pack['duty_types'].items():
        definition = {'requirements': [{'minimum': 1}]}
        if key == 'teaching':
            definition['supervision'] = {'trainee_category_ids': ['$resident'],
                                         'capability_id': '$supervisor', 'trainees_per_supervisor': 2}
        catalogue(key, 'assignment_types', name=title, definition=definition, **base)

    def series(key, duty, service, location, timing, weekdays, excluded=()):
        start, end = timing
        definition = {'timezone': timezone, 'anchor': begins, 'service_ids': [ref(service)],
                      'excluded_dates': list(excluded),
                      'days': [{'weekday': weekday, 'start_time': start, 'end_time': end} for weekday in weekdays]}
        add(key, 'admin', 'catalogue.create', catalogue_item('recurring_series', name=f'Fixture {key}',
            assignment_type_id=ref(duty), location_id=ref(location), starts_on=begins, ends_on=ends,
            definition=definition))
        spec['series'][key] = [day(offset) for offset in range(days)
                               if (START + timedelta(days=offset)).weekday() in weekdays and day(offset) not in excluded]

    for group in range(groups):
        prefix = f'clinic-{group}'
        catalogue(prefix, 'services', name=f'Fixture clinic {group + 1}', **base)
        for room, site in (('room', 'site'), ('side', 'annex')):
            catalogue(f'{prefix}-{room}', 'locations', name=f'Fixture room {group + 1} {room}',
                      location_type_id='$space', parent_id=ref(site), capacity=8, **base)

        staff = {}
        for member in range(20):
            key = f'p{group * 20 + member:03}'
            profile = {**pack['profiles'][str(member)], **pack['personas'].get(key, {})}
            for field in ('first_day', 'last_day'):
                if field in profile:
                    profile[field] = days * profile[field] // SIZES['department'][1]
            staff[key] = profile
        spec['people'].update(staff)
        bulk_catalogue(f'{prefix}-staff', 'people', (
            (key, {'display_name': f"{pack['ranks'][profile['rank']]} {int(key[1:]) + 1:03} (test)",
                   'tags': profile.get('tags', []),
                   **({'active': False, 'inactive_from': day(profile['last_day'])} if 'last_day' in profile else {})})
            for key, profile in staff.items()))

        memberships = defaultdict(list)
        qualifications = defaultdict(list)
        teaching = []
        for key, profile in staff.items():
            start = day(profile['first_day']) if 'first_day' in profile else base['starts_on']
            memberships[profile['rank'], start].append(key)
            qualifications['clinic', start].append(key)
            if 'response' in profile:
                qualifications['response', start].append(key)
            for skill in profile.get('skills', []):
                qualifications[skill, start].append(key)
            if programme := profile.get('programme'):
                teaching.append((key, programme, start, profile))

        for (rank, start), members in memberships.items():
            access(f'{prefix}-rank-{rank}-{start}', 'membership', members, category_id=ref(rank), starts_on=start)
        for (qual, start), members in qualifications.items():
            if qual in {'clinic', 'response'}:
                access(f'{prefix}-access-{qual}-{start}', 'eligibility', members,
                       service_ids=[ref(prefix if qual == 'clinic' else qual)], starts_on=start)
            else:
                access(f'{prefix}-skill-{qual}-{start}', 'grant', members, capability_ids=[ref(qual)],
                       evidence=REASON, starts_on=start)

        bulk_catalogue(f'{prefix}-programmes', 'programme_memberships', (
            (f'{key}-programme', {'person_id': ref(key), 'programme_id': ref(programme),
                'starts_on': start, 'ends_on': ends}) for key, programme, start, _ in teaching))
        bulk_catalogue(f'{prefix}-rotations', 'rotations', (
            (f'{key}-rotation', {'person_id': ref(key), 'cohort_id': ref(f"{programme}-{profile['cohort']}"),
                'rotation_type_id': ref(profile['rotation']), 'service_id': ref(prefix),
                'starts_on': begins, 'ends_on': ends}) for key, programme, _, profile in teaching))

        for key, profile in staff.items():
            for index, interval in enumerate(profile.get('availability', [])):
                first, last, from_time, to_time, kind = interval
                add(f'{key}-availability-{index}', 'coordinator', 'availability.record',
                    {'person_id': ref(key), 'kind': kind, 'start': instant(first, from_time), 'end': instant(last, to_time), 'private_reason': REASON})
            if wish := profile.get('wish'):
                add(f'{key}-wish', 'coordinator', 'wish.create', {'person_id': ref(key), 'kind': wish,
                    'first_day': begins, 'last_day': day(6), 'assignment_type_id': '$night', 'points': 1})

        def at(duty):
            return condition(service_ids=[ref(prefix)], assignment_type_ids=[ref(duty)])

        for duty, (a, b, room) in STREAMS.items():
            series(f'{prefix}-{duty}', duty, prefix, f'{prefix}-{room}', (a, b), WEEKDAYS)
            for weekday, (minimum, maximum) in enumerate(pack['staffing'][duty]):
                line('people_per_session', f'{prefix}-{duty}-{weekday}',
                     {'min': number(minimum), 'max': number(maximum)}, where=at(duty), weekdays=[weekday])

        seniors = condition(category_ids=['$consultant'])
        trainees = condition(category_ids=['$resident'])
        line('team_leads', prefix, {'min': number(1), 'max': number(1)}, who=seniors, where=at('early'),
             kind='team_lead_requirement', settings={'continuity': 'whole_duty'})
        line('people_per_session', f'{prefix}-trainee', {'min': number(1)}, who=trainees, where=at('teaching'))
        line('supervision', prefix, {'max': number(1)}, who=seniors, where=at('teaching'))
        line('spread_evenly', prefix, {'max': number(1)}, who=trainees, where=at('teaching'))
        for n, (who, maximum) in enumerate([
            (None, 6), (trainees, 4), (condition(programme_ids=['$programme-a']), 5),
            (condition(rotation_type_ids=['$rotation-b']), 5), (condition(cohort_ids=['$programme-b-1']), 5),
        ]):
            line('workload_limit', f'{prefix}-weekly-{n}', {'max': number(maximum)}, who=who,
                 where=condition(service_ids=[ref(prefix)]), measure='duties', period={'kind': 'calendar_week'})
        for key, profile in staff.items():
            if 'clinic_priority' in profile:
                line('preferences', key, {'priority': number(profile['clinic_priority'], 'medium')},
                     who=condition(person_ids=[ref(key)]), where=at(profile['preferred_stream']))

    for duty, (a, b, room) in RESPONSE.items():
        excluded = [day(days - 1)] if duty == 'night' else []
        series(f'daily-{duty}', duty, 'response', room, (a, b), EVERY_DAY, excluded)
        line('people_per_session', duty, {'min': number(1), 'max': number(1)},
             where=condition(assignment_type_ids=[ref(duty)]))
    series('language-cover', 'language', 'clinic-0', 'clinic-0-room', ('14:00', '16:00'),
           [2], [day(offset) for offset in range(2, days, 7) if offset != 9])

    person = lambda *members: condition(person_ids=[ref(member) for member in members])
    duty = lambda *types: condition(assignment_type_ids=[ref(key) for key in types])
    local = condition(service_ids=['$clinic-0'])
    night = duty('night')
    learners = condition(tags=['fixture-teaching'])
    line('people_per_session', 'language-shortfall', {'target': number(1, 'high')},
         who=condition(capability_ids=['$language-skill']), where=duty('language'))
    shortage_night = {'conditions': [*night['conditions'], {'field': 'date', 'op': 'is', 'value': day(8)}]}
    line('people_per_session', 'night-shortfall', {'target': number(2, 'high')},
         who=condition(category_ids=['$consultant']), where=shortage_night)
    line('people_per_session', 'night-senior', {'min': number(1)},
         who=condition(category_ids=['$consultant']), where=shortage_night)
    line('workload_limit', 'person-override', {'max': number(5)}, who=person('p005'), where=local,
         measure='duties', period={'kind': 'calendar_week'})
    line('workload_limit', 'part-time', {'max': number(3)}, who=person('p003'),
         measure='duties', period={'kind': 'calendar_week'})
    line('workload_limit', 'night-refusal', {'apply': number()}, who=person('p004'), where=night)
    line('workload_limit', 'part-time-days', {'apply': number()}, who=person('p003'), weekdays=[3, 4, 5, 6])
    line('workload_limit', 'protected-teaching', {'apply': number()}, who=learners,
         where=condition(location_ids=['$annex']), weekdays=[2], start_time='09:00', end_time='13:00')
    line('days_in_a_row', 'run', {'max': number(3)}, who=person('p010'))
    line('days_off_together', 'break', {'min': number(2, 'medium')}, who=person('p011'))
    weekend = {'weekend_start': 5, 'weekend_days': 2}
    line('weekends_in_a_row', 'weekends', {'max': number(1)}, who=person('p011'),
         kind='max_consecutive_weekends', settings=weekend)
    line('weekend_shape', 'whole', {'apply': number(level='medium')}, who=person('p012'),
         kind='weekend_shape', settings=weekend)
    line('shift_order', 'forwards', {'apply': number(level='medium')}, who=person('p013'),
         kind='shift_succession', settings={'type_order': ['$early', '$late', '$night']})
    line('duty_spacing', 'rest', {'min': number(10)}, who=person('p010', 'p013'))
    line('duty_spacing', 'next-day', {'apply': number()}, who=person('p010'),
         kind='forbidden_transition', settings={'from_type_ids': ['$night'], 'to_type_ids': ['$early']})
    line('rest_after_series', 'night-block', {'apply': number(level='medium')}, who=person('p010'),
         kind='shift_succession', settings={'from_type_ids': ['$night'], 'series_length': 2, 'rest_days': 2})
    line('travel_between_places', 'sites', {'min': number(60)}, who=person('p014'),
         kind='travel_buffer', settings={'from_location_id': '$clinic-0-room', 'to_location_id': '$clinic-0-side'})
    line('together_apart', 'separate', {'apply': number(level='medium')}, where=duty('teaching'),
         kind='together_apart', settings={'mode': 'apart', 'level': 'session', 'person_ids': ['$p005', '$p006']})
    line('companion_cover', 'extra-cover', {'min': number(2)}, where=duty('language'),
         kind='minimum_cover', settings={'when_person_ids': ['$p014']})
    line('allocation_order', 'priority', {'session_priority': number(3, 'medium')}, where=night)
    line('preferences', 'night-conflict', {'priority': number(4, 'high')}, who=person('p004'), where=night)
    line('preferences', 'night-seeker', {'priority': number(3, 'medium')}, who=person('p010'), where=night)
    line('fair_share', 'night-share', {'apply': number(level='medium')}, who=person('p010', 'p011'),
         kind='balance_goal', settings={'metric': 'duties', 'period': 'rolling', 'period_months': 12, 'aim': 'equal_share',
                                       'tolerance_percent': 0, 'tolerance_units': 0, 'assignment_type_ids': ['$night']})

    for rule, lines in rules.items():
        add(rule, 'coordinator', 'rule.propose',
            {'catalogue_id': rule, 'catalogue_version': 2, 'lines': lines, 'reason': REASON})
    add('activate-rules', 'approver', 'policy.activate_many', {'reason': REASON,
        'items': [{'policy_id': ref(rule), 'expected_revision': ref(rule + '.revision')} for rule in rules]})

    historical = []
    for month in reversed(range(history_months)):
        year, month_index = divmod(previous_month.year * 12 + previous_month.month - 1 - month, 12)
        start = date(year, month_index + 1, 1)
        end = (start + timedelta(days=32)).replace(day=1)
        historical.append((start, end))

    if historical:
        first_day, last_day = historical[0][0], historical[-1][1]
        for key, weekdays, member in [('night-history-a', (0, 2, 4, 6), '$p010'),
                                       ('night-history-b', (1, 3, 5), '$p011')]:
            catalogue(key, 'recurring_series', name=f'Fixture {key}', assignment_type_id='$night',
                      location_id='$response-room', starts_on=first_day.isoformat(), ends_on=last_day.isoformat(),
                      definition={'timezone': timezone, 'anchor': first_day.isoformat(), 'service_ids': ['$response'],
                          'default_person_ids': [member], 'usual_assignees_fixed': True,
                          'excluded_dates': [(end - timedelta(days=1)).isoformat() for _, end in historical],
                          'days': [{'weekday': weekday, 'start_time': '20:00', 'end_time': '06:00'}
                                   for weekday in weekdays]})
    spec['historical_sessions'] = sum((end - start).days - 1 for start, end in historical)
    for index, (start, end) in enumerate(historical, 1):
        key = f'history-{index}'
        add(key, 'coordinator', 'workspace.create',
            {'name': f'Fixture past rota {index}', 'starts_on': start.isoformat(), 'ends_on': end.isoformat()})
        add(key + '-expand', 'coordinator', 'series.expand', {'workspace_id': '$workspace',
            'series_ids': ['$night-history-a', '$night-history-b']}, revision='$workspace.revision')
        for action, role in [('submit', 'coordinator'), ('approve', 'approver'), ('publish', 'coordinator')]:
            add(f'{key}-{action}', role, 'workspace.' + action, {'reason': REASON},
                target='$workspace', revision='$workspace.revision')

    add('rota', 'coordinator', 'workspace.create',
        {'name': f'Golden department {size} (test)', 'starts_on': begins, 'ends_on': ends})
    add('expand', 'coordinator', 'series.expand',
        {'workspace_id': '$workspace', 'series_ids': [ref(key) for key in spec['series']]},
        revision='$workspace.revision')
    spec['session_count'] = sum(map(len, spec['series'].values()))
    return spec


def seed_department(spec, command, read, committed):
    version = read('coordinator', '/api/me')['catalogue_version']
    if version != 2:
        raise ValueError('The golden department requires catalogue v2')
    rotas = read('coordinator', '/api/workspaces?limit=1&include_archived=true')['workspaces']
    first_rota = 'history-1' if spec['history_months'] else 'rota'
    if rotas and committed('coordinator', command_key(spec, first_rota)) is None:
        raise ValueError('The golden department requires a dedicated department without rotas')
    return compile_scenario(command, spec, read=read, committed=committed)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--api-base', required=True)
    parser.add_argument('--size', choices=SIZES, default='department')
    args = parser.parse_args()
    url = urlsplit(args.api_base)
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.path not in ('', '/') or url.query or url.fragment:
        parser.error('Use an HTTPS staging origin')
    origin = f'{url.scheme}://{url.netloc}'

    def result(response, operation, missing=False):
        if response.is_success:
            return response.json()
        error = response.json().get('error', {}).get('code') if response.headers.get('content-type', '').startswith('application/json') else None
        if missing and response.status_code == 404 and error == 'RECEIPT_NOT_FOUND':
            return None
        raise ValueError(f'{operation} failed with HTTP {response.status_code} ({error or "no error code"})')

    with ExitStack() as stack:
        clients = {}
        for role in ROLES:
            prefix = 'GOLDEN_' + role.upper()
            token, csrf = os.getenv(prefix + '_SESSION'), os.getenv(prefix + '_CSRF')
            if not token or not csrf:
                parser.error(f'Set {prefix}_SESSION and {prefix}_CSRF from a signed-in session')
            clients[role] = stack.enter_context(httpx.Client(base_url=origin, timeout=180,
                cookies={'__Host-rota': token, 'rota_session': token, 'rota_csrf': csrf},
                headers={'Origin': origin, 'X-CSRF-Token': csrf}))

        def read(role, path):
            return result(clients[role].get(path), 'Seed read')

        def committed(role, key):
            reply = result(clients[role].get('/api/commands/' + key), 'Read seed receipt', missing=True)
            return reply['result'] if reply else None

        def command(role, kind, payload, *, target, revision, key):
            headers = {'Idempotency-Key': key}
            body = {'kind': kind, 'payload': payload, 'target_id': target, 'expected_revision': revision}
            client = clients[role]
            reply = client.post('/api/commands', headers=headers, json=body)
            delay = reply.headers.get('Retry-After', '')
            if reply.status_code == 429 and delay.isdecimal() and 0 < int(delay) <= 60:
                time.sleep(int(delay))
                reply = client.post('/api/commands', headers=headers, json=body)
            return result(reply, kind)

        try:
            actors = {role: read(role, '/api/me') for role in ROLES}
            if any(actor['deployment'] != 'staging' or role not in actor['roles'] for role, actor in actors.items()):
                raise ValueError('All sessions must have their named role on staging')
            if len({actor['department']['id'] for actor in actors.values()}) != 1:
                raise ValueError('All sessions must use the same department')
            if actors['coordinator']['identity_id'] == actors['approver']['identity_id']:
                raise ValueError('Rule author and approver must sign in separately')
            timezone = actors['coordinator']['organisation']['timezone']
            receipt = seed_department(load_department(args.size, timezone), command, read, committed)
        except httpx.HTTPError:
            parser.exit(1, 'Seed transport failed. Resume with the same department, size and identities.\n')
        except ValueError as error:
            parser.exit(1, f'{error}\n')
    print(json.dumps({'workspace_id': receipt['workspace']['id'], 'input_sha256': receipt['input_sha256'],
                      'generation_options': receipt['generation_options']}))


if __name__ == '__main__':
    main()
