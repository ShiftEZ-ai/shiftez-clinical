"""Department date spans, resolved once for conditions, calendars and markers.

All stored and resolved endpoints are inclusive. Hijri dates use the upstream
Umm al-Qura table; an override identifies the recurrence's native calendar year.
"""
import calendar
import csv
import io
import json
import re
from datetime import date, timedelta
from functools import lru_cache
from uuid import uuid4

from hijridate import Gregorian, Hijri


HIJRI_FIRST = date(1924, 8, 1)
HIJRI_LAST = date(2077, 11, 16)


class DateSetRangeError(ValueError):
    pass


def _parts(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('Dates must use YYYY-MM-DD')
    return tuple(int(part) for part in value.split('-'))


def _native(value, hijri=False, month_end=False):
    year, month, day = _parts(value)
    if not 1 <= year <= 9999:
        raise DateSetRangeError('Gregorian years must be from 1 to 9999')
    if hijri and not 1343 <= year <= 1500:
        raise DateSetRangeError('Umm al-Qura supports 1343-1500 AH (1924-08-01 to 2077-11-16 CE)')
    if not 1 <= day <= (30 if hijri else 31):
        raise ValueError('Native calendar day is outside its allowed range')
    try:
        if hijri:
            first = Hijri(year, month, 1)
            return Hijri(year, month, first.month_length() if month_end else day)
        return date(year, month, calendar.monthrange(year, month)[1] if month_end else day)
    except (ValueError, OverflowError) as error:
        raise ValueError(f'Invalid {"Umm al-Qura" if hijri else "Gregorian"} date: {value}; '
                         'Umm al-Qura supports 1343-1500 AH (1924-08-01 to 2077-11-16 CE)') from error


def validate_entry(entry):
    """Validate native dates without depending on API/contracts modules."""
    repeat = entry.get('repeat', 'once')
    if repeat not in {'once', 'yearly_gregorian', 'yearly_hijri'}:
        raise ValueError('Unknown date-set repeat')
    start_year = entry.get('start_year')
    if start_year is not None:
        if isinstance(start_year, bool) or not isinstance(start_year, int) or not 1 <= start_year <= 9999:
            raise ValueError('Start year must be an integer from 1 to 9999')
    if (start_year is not None or entry.get('fixed_duration')) and repeat != 'yearly_gregorian':
        raise ValueError('Start year and fixed duration apply only to yearly Gregorian repeats')
    if entry.get('fixed_duration') and entry.get('last_day_of_month'):
        raise ValueError('A fixed duration cannot also end on the last day of the month')
    hijri = repeat == 'yearly_hijri'
    if hijri and _parts(entry['first_day'])[2] == 30:
        raise ValueError('Not every Hijri month has a 30th; start on the 29th or earlier')
    if hijri and _parts(entry['last_day'])[2] == 30 and not entry.get('last_day_of_month'):
        raise ValueError('A Hijri day-30 end requires last_day_of_month; Hijri months can have 29 days')
    first = _native(entry['first_day'], hijri)
    last = _native(entry['last_day'], hijri, entry.get('last_day_of_month', False))
    if last < first:
        raise ValueError('The last day must be on or after the first day')
    if repeat != 'once' and (last.year - first.year > 1 or
            (last.year != first.year and (last.month, last.day) >= (first.month, first.day))):
        raise ValueError('A yearly date span must be shorter than one native calendar year')
    years = set()
    for override in entry.get('overrides', []):
        year = override['year']
        if isinstance(year, bool) or not isinstance(year, int) or not 1 <= year <= 9999:
            raise ValueError('Override year must be an integer from 1 to 9999')
        if year in years:
            raise ValueError('Only one date override per native recurrence year')
        years.add(year)
        if repeat == 'once' and year != first.year:
            raise ValueError('A once-only entry can override only its original year')
        if hijri and not 1343 <= year <= 1500:
            raise ValueError('Umm al-Qura override year must be within 1343-1500 AH')
        a, b = _native(str(override['first_day'])), _native(str(override['last_day']))
        if b < a:
            raise ValueError('The override last day must be on or after the first day')
        if repeat != 'once' and (b - a).days > 365:
            raise ValueError('A yearly override must contain at most 366 days')


def validate_date_set(value):
    for entry in value.get('entries', []):
        validate_entry(entry)


def _gregorian(value):
    return date(value.year, value.month, value.day)


def _occurrence(entry, year):
    hijri = entry.get('repeat') == 'yearly_hijri'
    start_year, start_month, start_day = _parts(entry['first_day'])
    end_year, end_month, end_day = _parts(entry['last_day'])
    if entry.get('repeat', 'once') == 'once':
        year = start_year
    first = _native(f'{year:04}-{start_month:02}-{start_day:02}', hijri)
    if entry.get('fixed_duration'):
        duration = date.fromisoformat(entry['last_day']) - date.fromisoformat(entry['first_day'])
        try:
            return first, first + duration
        except OverflowError as error:
            raise DateSetRangeError('The fixed-duration occurrence ends outside Gregorian years 1 to 9999') from error
    last = _native(f'{year + end_year - start_year:04}-{end_month:02}-{end_day:02}',
                   hijri, entry.get('last_day_of_month', False))
    return (_gregorian(first.to_gregorian()), _gregorian(last.to_gregorian())) if hijri else (first, last)


@lru_cache(maxsize=4096)
def _resolved_entry(encoded, first, last):
    entry = json.loads(encoded)
    repeat = entry.get('repeat', 'once')
    overrides = {item['year']: item for item in entry.get('overrides', [])}
    if repeat == 'yearly_hijri':
        if first < HIJRI_FIRST or last > HIJRI_LAST:
            raise DateSetRangeError('Umm al-Qura resolution supports 1924-08-01 to 2077-11-16 CE')
        a, b = Gregorian(first.year, first.month, first.day).to_hijri(), Gregorian(last.year, last.month, last.day).to_hijri()
        start_year = _parts(entry['first_day'])[0]
        end_year, end_month, end_day = _parts(entry['last_day'])
        if a.year == 1343 and end_year > start_year:
            try:
                tail = _native(f'1343-{end_month:02}-{end_day:02}', True,
                               entry.get('last_day_of_month', False)).to_gregorian()
            except ValueError:
                pass  # A nonexistent native endpoint cannot form an occurrence.
            else:
                if _gregorian(tail) >= first:
                    raise DateSetRangeError('The overlapping Umm al-Qura occurrence starts before supported 1343 AH')
        years = set(range(max(1343, a.year - 1), b.year + 1))
    elif repeat == 'yearly_gregorian':
        years = set(range(max(1, first.year - 1), last.year + 1))
    else:
        years = {_parts(entry['first_day'])[0]}
    years.update(overrides)
    result = []
    for year in sorted(years):
        if entry.get('start_year') is not None and year < entry['start_year']:
            continue
        override = overrides.get(year)
        if override:
            a, b = date.fromisoformat(str(override['first_day'])), date.fromisoformat(str(override['last_day']))
        else:
            try:
                a, b = _occurrence(entry, year)
            except DateSetRangeError:
                _, month, day = _parts(entry['first_day'])
                start = _native(f'{year:04}-{month:02}-{day:02}', repeat == 'yearly_hijri')
                if (_gregorian(start.to_gregorian()) if repeat == 'yearly_hijri' else start) <= last:
                    raise
                continue
            except ValueError:
                if repeat == 'yearly_gregorian' and any(
                        _parts(entry[key])[1:] == (2, 29) for key in ('first_day', 'last_day')):
                    continue  # Only Gregorian leap-day repeats omit a nonexistent year.
                raise
        if a <= last and b >= first:
            result.append((a.isoformat(), b.isoformat(), year, bool(override)))
    return tuple(result)


def resolve_date_sets(data, first_day, last_day, set_ids=None, entry_names=None, *, include_inactive=False, errors=None):
    """Return overlapping occurrences; supplying an errors dict isolates and records failed entries."""
    first = date.fromisoformat(first_day) if isinstance(first_day, str) else first_day
    last = date.fromisoformat(last_day) if isinstance(last_day, str) else last_day
    if last < first:
        raise ValueError('The range last day must be on or after its first day')
    selected = set(set_ids) if set_ids is not None else None
    names = set(entry_names or [])
    rows = []
    for key, date_set in data.get('date_sets', {}).items():
        set_id = date_set.get('id', key)
        if (not include_inactive and not date_set.get('active', True)) or (selected is not None and set_id not in selected):
            continue
        for entry in date_set.get('entries', []):
            if names and entry['name'] not in names:
                continue
            encoded = json.dumps(entry, sort_keys=True, separators=(',', ':'), default=str)
            try:
                occurrences = _resolved_entry(encoded, first, last)
            except ValueError as error:
                if errors is None:
                    raise
                errors[set_id, entry['name']] = error
                continue
            for a, b, year, adjusted in occurrences:
                rows.append({'set_id': set_id, 'set_name': date_set['name'], 'entry_id': entry['id'],
                             'name': entry['name'], 'first_day': a, 'last_day': b, 'year': year, 'adjusted': adjusted})
    return sorted(rows, key=lambda row: (row['first_day'], row['set_name'], row['name'], row['entry_id']))


def prepare_date_sets(data, first=None, last=None):
    """Return a content-keyed day index without changing the supplied snapshot."""
    from zoneinfo import ZoneInfo
    from .time import aware
    bounds = [value for value in (first, last) if value is not None]
    workspace = data.get('workspace') or {}
    if workspace:
        bounds.extend((date.fromisoformat(str(workspace['starts_on'])),
                       date.fromisoformat(str(workspace['ends_on'])) - timedelta(days=1)))
    zone = ZoneInfo(data.get('organisation', {}).get('timezone', 'UTC'))
    for shift in data.get('shifts', []) if data.get('date_sets') else ():
        bounds.extend((aware(shift['start']).astimezone(zone).date(),
                       (aware(shift['end']) - timedelta(microseconds=1)).astimezone(zone).date()))
    if not bounds:
        return
    bounds = [date.fromisoformat(value) if isinstance(value, str) else value for value in bounds]
    first, last = min(bounds), max(bounds)
    encoded = json.dumps(data.get('date_sets', {}), sort_keys=True, separators=(',', ':'), default=str)
    key = (encoded, first, last)
    previous = data.get('_date_set_cache')
    if previous and previous['key'][0] == encoded:
        if previous['first'] <= first and last <= previous['last']:
            return previous
        first, last = min(first, previous['first']), max(last, previous['last'])
        key = (encoded, first, last)
    days, markers, errors = {}, {}, {}
    for row in resolve_date_sets(data, first, last, errors=errors):
        a, b = max(first, date.fromisoformat(row['first_day'])), min(last, date.fromisoformat(row['last_day']))
        marker = {key: row[key] for key in ('set_id', 'set_name', 'entry_id', 'name')}
        for n in range((b - a).days + 1):
            day = a + timedelta(days=n)
            days.setdefault(day, set()).add((row['set_id'], row['name']))
            markers.setdefault(day, []).append(marker)
    cache = {'key': key, 'first': first, 'last': last, 'errors': errors,
             'days': {day: frozenset(values) for day, values in days.items()},
             'set_ids': {day: frozenset(set_id for set_id, _ in values) for day, values in days.items()},
             'error_days': {},
             'markers': {day: tuple(sorted(values, key=lambda row: (row['set_name'], row['name'], row['entry_id'])))
                         for day, values in markers.items()}}
    return cache


def date_set_days(data, on, set_ids=None, entry_names=None, *, ignore_errors=False):
    """Read the immutable per-day memberships; selectors raise only their own resolution errors."""
    day = date.fromisoformat(on) if isinstance(on, str) else on
    cache = data.get('_date_set_cache')
    if cache is None or not cache['first'] <= day <= cache['last']:
        cache = prepare_date_sets(data, day, day)
        data['_date_set_cache'] = cache
    selected = set(set_ids) if set_ids is not None else None
    names = set(entry_names or [])
    for (set_id, name), error in cache['errors'].items():
        if (selected is None or set_id in selected) and (not names or name in names):
            key = (day, set_id, name)
            if key not in cache['error_days']:
                errors = {}
                rows = resolve_date_sets(data, day, day, [set_id], [name], errors=errors)
                cache['error_days'][key] = errors.get((set_id, name))
                cache['days'][day] = cache['days'].get(day, frozenset()) | frozenset(
                    (row['set_id'], row['name']) for row in rows)
                cache['set_ids'][day] = frozenset(sid for sid, _ in cache['days'][day])
                markers = cache['markers'].get(day, ()) + tuple(
                    {field: row[field] for field in ('set_id', 'set_name', 'entry_id', 'name')} for row in rows)
                unique = {(marker['set_id'], marker['entry_id']): marker for marker in markers}
                cache['markers'][day] = tuple(sorted(unique.values(), key=lambda row: (
                    row['set_name'], row['name'], row['entry_id'])))
            if not ignore_errors and cache['error_days'][key] is not None:
                raise cache['error_days'][key]
    return cache['days'].get(day, frozenset())


def matching_date_sets(data, on, set_ids=None, entry_names=None):
    """Membership checks scale with the selector, never with all configured entries."""
    day = date.fromisoformat(on) if isinstance(on, str) else on
    selected = frozenset(set_ids) if set_ids is not None else None
    names = frozenset(entry_names or [])
    held = date_set_days(data, day, selected, names)
    sets = data['_date_set_cache']['set_ids'].get(day, frozenset())
    wanted = sets if selected is None else sets & selected
    return frozenset(sid for sid in wanted if any((sid, name) in held for name in names)) if names else wanted


def date_set_markers(data, on):
    date_set_days(data, on, ignore_errors=True)
    day = date.fromisoformat(on) if isinstance(on, str) else on
    return [dict(marker) for marker in data['_date_set_cache']['markers'].get(day, ())]


def on_date_set(data, set_id, on, entry_names=None):
    data = {**data, '_date_set_cache': prepare_date_sets(data, on, on)}
    return bool(matching_date_sets(data, on, [set_id], entry_names))


def _imported(entry):
    entry = {'id': str(uuid4()), 'repeat': 'once', 'last_day_of_month': False, 'overrides': [], **entry}
    if not isinstance(entry.get('name'), str) or not entry['name'].strip():
        raise ValueError('Each imported date entry needs a name')
    validate_entry(entry)
    return entry


def _csv_entries(content):
    reader = csv.DictReader(io.StringIO(content.lstrip('\ufeff')), strict=True)
    required = {'name', 'first_day', 'last_day'}
    allowed = required | {'repeat', 'last_day_of_month'}
    if not reader.fieldnames or not required <= set(reader.fieldnames) or set(reader.fieldnames) - allowed:
        raise ValueError('CSV needs name,first_day,last_day; optional columns are repeat,last_day_of_month')
    if len(set(reader.fieldnames)) != len(reader.fieldnames):
        raise ValueError('CSV columns must be unique')
    result = []
    try:
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError('CSV rows must have the same columns as the header')
            row = {key: value.strip() for key, value in row.items()}
            flag = row.pop('last_day_of_month', '').lower()
            if flag not in {'', 'false', 'true'}:
                raise ValueError('last_day_of_month must be true or false')
            row['last_day_of_month'] = flag == 'true'
            row['repeat'] = row.get('repeat') or 'once'
            result.append(_imported(row))
    except csv.Error as error:
        raise ValueError('Invalid CSV document') from error
    return result


def _ical_date(key, value):
    if key.upper() != 'DTSTART;VALUE=DATE' and key.upper() != 'DTEND;VALUE=DATE':
        raise ValueError('iCal imports require all-day DTSTART/DTEND with VALUE=DATE')
    if not re.fullmatch(r'\d{8}', value):
        raise ValueError('Invalid iCal all-day date')
    return date(int(value[:4]), int(value[4:6]), int(value[6:8]))


def _ical_entries(content):
    unfolded = re.sub(r'\r?\n[ \t]', '', content)
    lines = unfolded.replace('\r\n', '\n').strip().split('\n')
    if not lines or lines[0].upper() != 'BEGIN:VCALENDAR' or lines[-1].upper() != 'END:VCALENDAR':
        raise ValueError('iCal import needs a VCALENDAR document')
    result, event, event_number = [], None, 0
    for line in lines[1:-1]:
        try:
            key, separator, value = line.partition(':')
            if not separator:
                raise ValueError('Malformed iCal content line')
            prop = key.split(';')[0].upper()
            if key.upper() == 'BEGIN' and value.upper() == 'VEVENT':
                if event is not None:
                    raise ValueError('Nested iCal events are unsupported')
                event, event_number = {}, event_number + 1
            elif event is not None and prop in {'BEGIN', 'END'} and value.upper() != 'VEVENT':
                raise ValueError('Nested iCal event components are unsupported')
            elif key.upper() == 'END' and value.upper() == 'VEVENT':
                if event is None or 'DTSTART' not in event or 'SUMMARY' not in event:
                    raise ValueError('iCal events need DTSTART and SUMMARY')
                first = event['DTSTART']
                try:
                    last = event['DTEND'] - timedelta(days=1) if 'DTEND' in event else first
                except OverflowError as error:
                    raise ValueError('The iCal exclusive end is outside the supported Gregorian range') from error
                yearly = event.get('repeat') == 'yearly_gregorian'
                result.append(_imported({'name': event['SUMMARY'], 'first_day': first.isoformat(),
                                         'last_day': last.isoformat(), 'repeat': event.get('repeat', 'once'),
                                         **({'start_year': first.year, 'fixed_duration': True} if yearly else {})}))
                event = None
            elif event is not None:
                if prop in {'RDATE', 'EXDATE', 'EXRULE', 'RECURRENCE-ID', 'DURATION'}:
                    raise ValueError(f'Unsupported iCal property: {prop}')
                if prop == 'STATUS' and value.upper() not in {'CONFIRMED', 'TENTATIVE'}:
                    raise ValueError('Cancelled or unknown-status iCal events cannot be imported')
                if prop in {'DTSTART', 'DTEND', 'SUMMARY', 'RRULE'}:
                    if prop in event:
                        raise ValueError(f'Duplicate iCal property: {prop}')
                    if prop in {'DTSTART', 'DTEND'}:
                        event[prop] = _ical_date(key, value)
                    elif prop == 'SUMMARY':
                        event[prop] = re.sub(r'\\([nN,;\\])', lambda match: '\n' if match[1] in 'nN' else match[1], value)
                    else:
                        rule = value.upper().split(';')
                        if ('FREQ=YEARLY' not in rule or set(rule) - {'FREQ=YEARLY', 'INTERVAL=1'}
                                or len(rule) != len(set(rule))):
                            raise ValueError('Unsupported RRULE: only FREQ=YEARLY with optional INTERVAL=1 can be imported')
                        event['RRULE'], event['repeat'] = value, 'yearly_gregorian'
        except ValueError as error:
            if event is not None:
                raise ValueError(f'iCal event {event_number}: {error}') from error
            raise
    if event is not None:
        raise ValueError(f'iCal event {event_number}: Unclosed iCal event')
    return result


def parse_import(content, format):
    """Parse explicit CSV spans or all-day iCal events, rejecting lost semantics."""
    try:
        if format == 'csv':
            entries = _csv_entries(content)
        elif format == 'ical':
            entries = _ical_entries(content)
        else:
            raise ValueError('Import format must be csv or ical')
    except (csv.Error, OverflowError) as error:
        raise ValueError('The import contains malformed content or out-of-range dates') from error
    if not entries or len(entries) > 500:
        raise ValueError('Import between 1 and 500 date entries')
    return entries


def copy_set_to_next_year(date_set, year, name=None):
    """Move once-only dates forward; yearly entries already recur in the source set."""
    if not 1 <= year <= 9998:
        raise ValueError('Copy source year must be from 1 to 9998')
    name = name if name is not None else f'{date_set["name"]} {year + 1}'
    if len(name) > 160:
        raise ValueError('The copied set name exceeds 160 characters; shorten the source name')
    entries = []
    source = {**date_set, 'active': True, 'entries': [entry for entry in date_set.get('entries', [])
              if entry.get('repeat', 'once') == 'once']}
    rows = resolve_date_sets({'date_sets': {'source': source}}, date(year, 1, 1), date(year, 12, 31))
    for row in rows:
        first, last = date.fromisoformat(row['first_day']), date.fromisoformat(row['last_day'])
        try:
            first, last = first.replace(year=first.year + 1), last.replace(year=last.year + 1)
        except ValueError as error:
            raise ValueError('A copied date has no date next year; adjust leap days or range before copying') from error
        entries.append(_imported({'name': row['name'], 'first_day': first.isoformat(), 'last_day': last.isoformat()}))
    if len(entries) > 500:
        raise ValueError('The copied set exceeds 500 resolved date entries')
    return {'name': name, 'active': True, 'entries': entries}
