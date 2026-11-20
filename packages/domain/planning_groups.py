"""Dated scheduling responsibility, using the canonical Conditions evaluator."""
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from .conditions import INTERNAL, SESSION_FIELDS, has_session_lines, scope_segments, scope_tree, tag_scope
from .time import aware, local_midnight


class GroupResolver:
    """One acquired version set and condition preparation per snapshot, with no database access."""
    def __init__(self, data, groups):
        from .validation import index_person_rows
        data = {**data, '_person_rows': data.get('_person_rows') or index_person_rows(data)}
        self.data = data
        self.groups = {}
        self.cache = {}
        self.day_cache = {}
        self.person_segments = {}
        self.day_bounds = {}
        # Empty group projections intentionally carry only shifts and focus ids.
        self.zone = None
        self.shift_bounds = [(aware(shift['start']), aware(shift['end']))
                             for shift in data.get('shifts', []) if shift.get('start') and shift.get('end')]
        self.day_fast_path = True
        for row in groups:
            definition = row.get('definition', row)
            group = {**definition, 'group_id': row['group_id'], 'version_id': row.get('version_id', row.get('id')),
                     'revision': row['revision']}
            group['scope'] = tag_scope(data, {'conditions': definition['membership']})
            group['start'] = local_midnight(date.fromisoformat(str(definition['starts_on'])), data['organisation']['timezone'])
            group['end'] = local_midnight(date.fromisoformat(str(definition['ends_on'])), data['organisation']['timezone']) if definition.get('ends_on') else None
            self.groups.setdefault(group['group_id'], []).append(group)
            # Skills and internal selectors may depend on the duty's clinic or type.
            self.day_fast_path &= not has_session_lines(scope_tree(group['scope']),
                                                        SESSION_FIELDS | INTERNAL | {'capability_ids'})
        self.day_fast_path &= bool(self.groups)

    def primary(self, person_id, shift, participant=None):
        """One primary throughout a duty; ties, unowned or changing responsibility go to the coordinator."""
        interval = participant if participant is not None else shift
        a, b = aware(interval['start']), aware(interval['end'])
        key = (person_id, shift.get('id'), a, b)
        if key in self.cache:
            return self.cache[key]
        if person_id not in self.data['people'] or a >= b:
            return None
        day_info = self._day_bounds(shift, a, b) if self.day_fast_path else None
        result = (self._primary_days(person_id, shift, a, b, day_info) if day_info is not None
                  else self._primary_interval(person_id, shift, a, b))
        self.cache[key] = result
        return result

    def _day_bounds(self, shift, a, b):
        key = (shift.get('id'), a, b)
        if key not in self.day_bounds:
            if self.zone is None:
                self.zone = ZoneInfo(self.data['organisation']['timezone'])
            try:
                day = a.astimezone(self.zone).date()
                last_day = b.astimezone(self.zone).date()
            except OverflowError:
                return None
            if date.max in (day, last_day):
                return None
            start = local_midnight(day, self.data['organisation']['timezone'])
            self.day_bounds[key] = day, start, local_midnight(day + timedelta(days=1), self.data['organisation']['timezone'])
        return self.day_bounds[key]

    def _primary_days(self, person_id, shift, a, b, day_info):
        """Resolve person-only memberships once per local day, then combine duty days."""
        first_day, day_start, day_end = day_info
        result = None
        editors = None
        versions = set()
        cursor = a
        if b <= day_end:
            return self._day_result(person_id, shift, first_day, day_start, day_end)
        while cursor < b:
            current = self._day_result(person_id, shift, first_day, day_start, day_end)
            if current is None:
                return None
            if result is None:
                result = {**current}
                editors = set(current['editor_identity_ids'])
            elif result['group_id'] != current['group_id']:
                return None
            editors &= set(current['editor_identity_ids'])
            versions.update(current['responsibility_version_ids'])
            cursor = min(b, day_end)
            if cursor < b:
                first_day += timedelta(days=1)
                day_start, day_end = day_end, local_midnight(first_day + timedelta(days=1), self.data['organisation']['timezone'])
        if result is None:
            return None
        result['editor_identity_ids'] = sorted(editors)
        result['responsibility_version_ids'] = sorted(versions)
        return result

    def _day_result(self, person_id, shift, day, day_start, day_end):
        current = self.day_cache.get((person_id, day))
        if current is None and (person_id, day) not in self.day_cache:
            segments = self._person_segments(person_id, shift, day_start, day_end)
            current = self._primary_from_segments(day_start, day_end, segments)
            self.day_cache[(person_id, day)] = current
        return current

    def _person_segments(self, person_id, shift, a, b):
        cached = self.person_segments.get(person_id)
        if cached is not None and cached[0] <= a and b <= cached[1]:
            return cached[2]
        bounds = [a, b, *(group['start'] for versions in self.groups.values() for group in versions)]
        bounds.extend(start for start, _ in self.shift_bounds)
        ends = [group['end'] for versions in self.groups.values() for group in versions if group['end'] is not None]
        lo, hi = min(bounds), max([b, *ends, *(bound for _, bound in self.shift_bounds)] if self.shift_bounds else [b, *ends])
        segments = {}
        for versions in self.groups.values():
            for group in versions:
                start, end = max(lo, group['start']), min(hi, group['end'] or hi)
                if start < end:
                    segments[group['version_id']] = list(scope_segments(self.data, group['scope'], person_id,
                                                                        shift, start, end))
        self.person_segments[person_id] = (lo, hi, segments)
        return segments

    def _primary_interval(self, person_id, shift, a, b):
        segments = {}
        for versions in self.groups.values():
            for group in versions:
                lo, hi = max(a, group['start']), min(b, group['end'] or b)
                if lo >= hi:
                    continue
                spans = list(scope_segments(self.data, group['scope'], person_id, shift, lo, hi))
                segments[group['version_id']] = spans
        return self._primary_from_segments(a, b, segments)

    def _primary_from_segments(self, a, b, segments):
        points = {a, b}
        for versions in self.groups.values():
            for group in versions:
                lo, hi = max(a, group['start']), min(b, group['end'] or b)
                if lo < hi:
                    points.update((lo, hi))
        for spans in segments.values():
            points.update(t for start, end, _ in spans if start < b and a < end for t in (max(a, start), min(b, end)))
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
