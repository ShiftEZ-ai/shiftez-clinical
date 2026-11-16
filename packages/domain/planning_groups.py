"""Dated scheduling responsibility, using the canonical Conditions evaluator."""
from datetime import date

from .conditions import scope_segments, tag_scope
from .time import aware, local_midnight


class GroupResolver:
    """One acquired version set and condition preparation per snapshot, with no database access."""
    def __init__(self, data, groups):
        from .validation import index_person_rows
        data = {**data, '_person_rows': data.get('_person_rows') or index_person_rows(data)}
        self.data = data
        self.groups = {}
        self.cache = {}
        for row in groups:
            definition = row.get('definition', row)
            group = {**definition, 'group_id': row['group_id'], 'version_id': row.get('version_id', row.get('id')),
                     'revision': row['revision']}
            group['scope'] = tag_scope(data, {'conditions': definition['membership']})
            group['start'] = local_midnight(date.fromisoformat(str(definition['starts_on'])), data['organisation']['timezone'])
            group['end'] = local_midnight(date.fromisoformat(str(definition['ends_on'])), data['organisation']['timezone']) if definition.get('ends_on') else None
            self.groups.setdefault(group['group_id'], []).append(group)

    def primary(self, person_id, shift, participant=None):
        """One primary throughout a duty; ties, unowned or changing responsibility go to the coordinator."""
        interval = participant if participant is not None else shift
        a, b = aware(interval['start']), aware(interval['end'])
        key = (person_id, shift.get('id'), a, b)
        if key in self.cache:
            return self.cache[key]
        if person_id not in self.data['people'] or a >= b:
            return None
        points = {a, b}
        segments = {}
        for versions in self.groups.values():
            for group in versions:
                lo, hi = max(a, group['start']), min(b, group['end'] or b)
                if lo >= hi:
                    continue
                points.update((lo, hi))
                spans = list(scope_segments(self.data, group['scope'], person_id, shift, lo, hi))
                segments[group['version_id']] = spans
                points.update(t for start, end, _ in spans for t in (start, end))
        ordered = sorted(points)
        primary, version_ids = None, set()
        for lo, hi in zip(ordered, ordered[1:]):
            matching = []
            for versions in self.groups.values():
                effective = [g for g in versions if g['start'] <= lo and (g['end'] is None or hi <= g['end'])]
                current = max(effective, key=lambda g: g['revision'], default=None)
                if current and current.get('active', True) and any(start <= lo and hi <= end and matched
                        for start, end, matched in segments.get(current['version_id'], ())):
                    matching.append(current)
            if not matching:
                primary = None
                break
            priority = min(g['primary_priority'] for g in matching)
            winners = [g for g in matching if g['primary_priority'] == priority]
            if len(winners) != 1 or primary is not None and primary['group_id'] != winners[0]['group_id']:
                primary = None
                break
            current = winners[0]
            # Owners can change mid-duty; require the intersection of those grants.
            editors = set(current['owner_identity_ids']) | set(current.get('co_editor_identity_ids', []))
            if primary is None:
                primary = {**current, 'editor_identity_ids': editors}
            else:
                primary['editor_identity_ids'] &= editors
            version_ids.add(current['version_id'])
        result = {**primary, 'editor_identity_ids': sorted(primary['editor_identity_ids']),
                  'responsibility_version_ids': sorted(version_ids)} if primary is not None else None
        self.cache[key] = result
        return result


def primary_group(data, groups, person_id, shift, participant=None):
    return GroupResolver(data, groups).primary(person_id, shift, participant)


def attribute_cause(data, groups, *, person_ids, shift_ids, selected_group_id=None,
                    required_group_ids=(), complete=True):
    """A disjoint presentation projection; never removes or weakens a canonical finding."""
    resolver = GroupResolver(data, groups)
    shifts = {s['id']: s for s in data['shifts']}
    responsible, versions, unassigned = set(), set(), False
    selected = [shifts[sid] for sid in set(shift_ids) if sid in shifts]
    complete = complete and len(selected) == len(set(shift_ids)) and all(pid in data['people'] for pid in person_ids)
    if not selected or not person_ids:
        unassigned = True
        complete = False
    for shift in selected:
        participants = {p['person_id']: p for p in shift.get('participants', [])}
        for person_id in set(person_ids):
            group = resolver.primary(person_id, shift, participants.get(person_id))
            if group is None:
                unassigned = True
            else:
                responsible.add(group['group_id'])
                versions.update(group['responsibility_version_ids'])
    required = set(required_group_ids)
    involved = responsible | required
    classification = ('coupling' if len(involved) > 1 or involved and unassigned else
                      'unassigned' if unassigned or not involved else
                      'mine' if selected_group_id in involved else 'other_pending')
    return {'classification': classification, 'group_ids': sorted(involved),
            'responsible_group_ids': sorted(responsible), 'required_group_ids': sorted(required),
            'responsibility_version_ids': sorted(versions), 'complete': complete}
