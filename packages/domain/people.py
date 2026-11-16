"""Current people facts, using the same dated conditions as scheduling rules."""
from datetime import timedelta

from .conditions import rule_holds
from .time import local_midnight


def person_aliases(person, imported=()):
    """Search labels include editable names and retained import aliases, without changing the display name."""
    return sorted({value for value in (person.get('rota_name', ''), *person.get('rota_aliases', []), *imported) if value})


def current_people(data, day):
    """Organisation people, with ranks/programmes in the selected department, on its local day.

    Active means the person's active flag. No current rank means an active person has no
    effective membership in an active, effective rank. Stored titles remain explicit data.
    """
    from .validation import index_person_rows
    data = {**data, '_person_rows': index_person_rows(data)}
    zone = data['organisation']['timezone']
    start, end = local_midnight(day, zone), local_midnight(day + timedelta(days=1), zone)
    people = {}
    for pid, person in data['people'].items():
        active = person.get('active', True)
        held = rule_holds(data, pid, None, start, end)
        facts = {f'current_{field}': sorted(held(field)) if active else []
                 for field in ('category_ids', 'programme_ids', 'rotation_type_ids', 'cohort_ids')}
        ranks = sorted(data['categories'][rid]['name'] for rid in facts['current_category_ids'])
        people[pid] = {'display_title': person.get('title') or ' / '.join(ranks), **facts}
    active_ids = [pid for pid, person in data['people'].items() if person.get('active', True)]
    return {'active_people': len(active_ids),
            'people_without_current_rank': sum(not people[pid]['current_category_ids'] for pid in active_ids),
            'people': people}
