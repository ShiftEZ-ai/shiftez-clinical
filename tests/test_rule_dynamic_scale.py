"""Dynamic catalogue applicability at department scale, using the existing in-memory rule-map benchmark."""
import gc
import time

import pytest

from packages.domain.conditions import active_rules
from packages.domain.rule_map import rule_map
from packages.domain.time import aware
from packages.domain.validation import policy_applies
from .test_rule_lines import line, person, rule
from .test_rule_lines_inherit import n
from .test_rule_map import U, big_department
from .test_performance_budget import python_budget


def dynamic_department():
    data = big_department()
    people = [U(f'p{i:03}') for i in range(150)]
    data['people'].update({pid: {'id': pid, 'active': True, 'tags': []} for pid in people[142:]})
    for i, pid in enumerate(people[142:]):
        shift = data['shifts'][i]
        shift['participants'].append({'person_id': pid, 'start': shift['start'], 'end': shift['end']})
    ranks = [U('R0'), U('R1')]
    clinics = [U(f'svc{i}') for i in range(5)]
    data['memberships'] = [
        {'id': f'dynamic-{i}-{half}', 'person_id': pid, 'department_id': 'dept',
         'category_id': ranks[(i + half) % 2], 'starts_on': start, 'ends_on': end}
        for i, pid in enumerate(people)
        for half, start, end in ((0, '2020-01-01', '2026-10-15'), (1, '2026-10-15', None))
    ]
    where = lambda clinic: {'conditions': [{'field': 'service_ids', 'values': [clinic]}]}
    lines = [
        line(id=U(f'dynamic-source-{clinic}-{rank}'),
             who={'conditions': [{'field': 'category_ids', 'values': [rank]}]}, where=where(clinic),
             numbers={'max': n(8, level)})
        for clinic in clinics for rank, level in zip(ranks, ('low', 'mandatory'))
    ]
    # Six date slices per person; split the final slice for 90 people: 10 explicit + 990 blank-strength lines.
    # The rank switch on the 15th lies INSIDE the 11th–16th slice, so line dates cannot select its strength for us.
    for i, pid in enumerate(people):
        cuts = ['2026-10-01', '2026-10-06', '2026-10-11', '2026-10-16', '2026-10-21', '2026-10-26']
        if i < 90:
            cuts.append('2026-10-29')
        cuts.append('2026-11-01')
        lines.extend(line(id=U(f'dynamic-own-{i}-{start}'), who=person(pid), where=where(clinics[i % 5]),
                          numbers={'max': n(3, None)}, starts_on=start, ends_on=end)
                     for start, end in zip(cuts, cuts[1:]))
    row = rule('duties_per_week', *lines, id=U('dynamic-scale-catalogue'), department_id='dept')
    data['policies'] = [row]
    assert len(lines) == 1000 and len(data['people']) == 150 and len(data['shifts']) == 531
    return data


@pytest.fixture(scope='module')
def department_dynamic_rules():
    data = dynamic_department()
    lines = data['policies'][0]['definition']['rule']['lines']
    people = [U(f'p{i:03}') for i in range(150)]
    clinics = [U(f'svc{i}') for i in range(5)]

    gc.collect()  # Collect earlier fixtures before timing; expansion still runs with GC enabled.
    began = time.process_time()
    copies = active_rules(data['policies'])
    expansion_seconds = time.process_time() - began
    began = time.process_time()
    result = rule_map(data)
    map_seconds = time.process_time() - began
    print('dynamic catalogue scale', {'lines': len(lines), 'people': len(people), 'sessions': len(data['shifts']),
                                      'expansion_seconds': expansion_seconds, 'map_seconds': map_seconds})
    return data, copies, result, people, clinics, expansion_seconds, map_seconds


def test_thousand_dynamic_lines_map_150_people_and_531_sessions_within_existing_budgets(department_dynamic_rules):
    data, copies, result, people, clinics, _, _ = department_dynamic_rules
    [mapped] = result['rules']
    assert len(mapped['lines']) == 1000
    assert mapped['people'] == {pid: [[0, 31]] for pid in people}
    assert mapped['days'] == list(range(31))
    assert mapped['sessions'] == [i for i, shift in enumerate(data['shifts']) if shift['service_ids'][0] in clinics]

    expanded = {**data, 'policies': copies}
    # Two people change rank in opposite directions. The person's number stays 3 while its strength follows rank;
    # at a different clinic the unrelated personal line must not replace that clinic's broad number of 8.
    for i, pid in enumerate(people[:2]):
        for day, half in (('2026-10-14', 0), ('2026-10-15', 1)):
            level = ('low', 'mandatory')[(i + half) % 2]
            for clinic, maximum in ((clinics[i], 3), (clinics[2], 8)):
                shift = next(s for s in data['shifts'] if s['start'].startswith(day) and s['service_ids'] == [clinic])
                actual = []
                for copy in copies:
                    if policy_applies(expanded, copy, pid, shift, aware(shift['start']), aware(shift['end'])):
                        definition = copy['definition']
                        actual.append((definition['rule']['maximum'],
                                       'mandatory' if definition['severity'] == 'mandatory' else definition['strength']))
                assert actual == [(maximum, level)], (pid, day, clinic, actual)


@pytest.mark.cpu_budget
def test_thousand_dynamic_lines_map_150_people_and_531_sessions_within_existing_budgets_cpu_budget(department_dynamic_rules):
    *_, expansion_seconds, map_seconds = department_dynamic_rules
    # The pre-maintenance map baseline was 0.82 s. Allow ~2x for the dynamic fixture and shared CI; tracing is ~7x slower.
    assert expansion_seconds < python_budget(0.5), expansion_seconds
    assert map_seconds < python_budget(1.8), map_seconds
