"""Read-only, bounded CSV/XLSX extraction; no workbook code or formula is executed.

Supported layouts: named person/date/code columns (optional clinic and times),
people down rows with dates across columns, and the transposed grid. Dates use
ISO, unambiguous day/month/year, or named months; day numbers need one known
month. A grid cell is one duty code, optionally ``code @ clinic``. Complex cells
and ambiguous dates remain unparsed instead of inventing assignments.
"""
from __future__ import annotations

import calendar
import csv
import hashlib
import io
import json
import math
import posixpath
import re
import zipfile
import zlib
from dataclasses import dataclass, field
from datetime import date, timedelta
from difflib import SequenceMatcher
from pathlib import PurePath
from xml.etree import ElementTree as ET

from packages.contracts.schemas import SCORE_CHECK_LIMITS
from .staff_import import match_key


MAX_FILE_BYTES = SCORE_CHECK_LIMITS['file_bytes']
MAX_EXPANDED_BYTES = 40 * 1024 * 1024
MAX_PART_BYTES = 10 * 1024 * 1024
MAX_ZIP_PARTS = 512
MAX_ROWS = 10000
MAX_COLUMNS = 512
MAX_CELLS = 100000
MAX_CELL_CHARS = 10000


class ScoreCheckParseError(ValueError):
    """An upload is invalid, unsafe, too large, or an unsupported file format."""


@dataclass
class PreparedScoreCheckFile:
    file_ref: str
    sha256: str
    sheets: dict[str, dict[tuple[int, int], str]] = field(repr=False)
    unparsed: list[dict] = field(default_factory=list, repr=False)

    @property
    def cell_values(self):
        return {f"{sheet}!{_column(col)}{row}": value
                for sheet, cells in self.sheets.items() for (row, col), value in cells.items()}

    @property
    def cell_refs(self):
        return frozenset(self.cell_values)

    @property
    def text(self):
        return json.dumps(self.cell_values, ensure_ascii=False)


def _column(number):
    result = ''
    while number:
        number, letter = divmod(number - 1, 26)
        result = chr(65 + letter) + result
    return result


def _coordinates(value):
    match = re.fullmatch(r'([A-Z]+)([1-9][0-9]*)', value)
    if not match or len(value) > 12:
        raise ScoreCheckParseError('Invalid worksheet cell reference')
    col = 0
    for letter in match[1]:
        col = col * 26 + ord(letter) - 64
    row = int(match[2])
    if row > MAX_ROWS or col > MAX_COLUMNS:
        raise ScoreCheckParseError('The spreadsheet has too many rows or columns')
    return row, col


def _store(prepared, sheet, row, col, value, reason=''):
    if row > MAX_ROWS or col > MAX_COLUMNS:
        raise ScoreCheckParseError('The spreadsheet has too many rows or columns')
    value = str(value).strip()
    if not value:
        return
    if len(value) > MAX_CELL_CHARS or '\x00' in value:
        raise ScoreCheckParseError('A spreadsheet cell is too long or contains NUL characters')
    cells = prepared.sheets[sheet]
    if (row, col) in cells:
        raise ScoreCheckParseError('Duplicate worksheet cell reference')
    cells[row, col] = value
    if sum(len(c) for c in prepared.sheets.values()) > MAX_CELLS:
        raise ScoreCheckParseError('The spreadsheet has too many populated cells')
    if reason or value.startswith('='):
        prepared.unparsed.append({'file_ref': prepared.file_ref,
            'cell_ref': f'{sheet}!{_column(col)}{row}', 'value': value,
            'reason': reason or 'Formula cells are not evaluated; replace with a value'})


def _xml(archive, name):
    try:
        with archive.open(name) as stream:
            content = stream.read(MAX_PART_BYTES + 1)
    except (KeyError, RuntimeError, zipfile.BadZipFile, NotImplementedError, zlib.error) as exc:
        raise ScoreCheckParseError('The XLSX contains an unreadable workbook part') from exc
    if len(content) > MAX_PART_BYTES:
        raise ScoreCheckParseError('An expanded workbook part is too large')
    if b'\x00' in content or re.search(br'<!\s*(?:DOCTYPE|ENTITY)\b', content, re.I):
        raise ScoreCheckParseError('Workbook XML declarations or encoding are unsafe')
    try:
        return ET.fromstring(content)
    except (ET.ParseError, ValueError, RecursionError) as exc:
        raise ScoreCheckParseError('The XLSX contains malformed XML') from exc


def _part_path(target):
    target = target.replace('\\', '/')
    result = posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'xl/' + target)
    if not result.startswith('xl/') or '?' in result or '#' in result or ':' in result:
        raise ScoreCheckParseError('Invalid workbook part path')
    return result


def _xlsx(data, prepared):
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ScoreCheckParseError('The file is not a valid XLSX workbook') from exc
    with archive:
        parts = archive.infolist()
        if (len(parts) > MAX_ZIP_PARTS or len({p.filename for p in parts}) != len(parts)
                or sum(p.file_size for p in parts) > MAX_EXPANDED_BYTES
                or any(p.file_size > MAX_PART_BYTES or p.flag_bits & 1
                       or p.file_size > max(p.compress_size, 1) * 200 for p in parts)):
            raise ScoreCheckParseError('The expanded workbook exceeds safe archive limits')
        workbook = _xml(archive, 'xl/workbook.xml')
        relationships = _xml(archive, 'xl/_rels/workbook.xml.rels')
        targets = {}
        for rel in relationships:
            if rel.get('TargetMode') == 'External':
                raise ScoreCheckParseError('External workbook links are not supported')
            if not rel.get('Id') or rel.get('Id') in targets:
                raise ScoreCheckParseError('Workbook relationship identifiers must be unique')
            targets[rel.get('Id')] = _part_path(rel.get('Target', ''))
        shared = []
        shared_path = next((targets[r.get('Id')] for r in relationships
                            if r.get('Type', '').endswith('/sharedStrings')), 'xl/sharedStrings.xml')
        if shared_path in archive.namelist():
            shared = [''.join(t.text or '' for t in el.findall('.//{*}t')) for el in _xml(archive, shared_path)]
        styles_path = next((targets[r.get('Id')] for r in relationships
                            if r.get('Type', '').endswith('/styles')), 'xl/styles.xml')
        date_styles, time_styles = set(), set()
        if styles_path in archive.namelist():
            styles = _xml(archive, styles_path)
            formats = {int(el.get('numFmtId')): el.get('formatCode', '')
                       for el in styles.findall('./{*}numFmts/{*}numFmt')}
            for index, style in enumerate(styles.findall('./{*}cellXfs/{*}xf')):
                fmt = int(style.get('numFmtId', '0'))
                pattern = re.sub(r'"[^"]*"|\\.|\[[^]]*\]', '', formats.get(fmt, '')).lower()
                if fmt in {*range(14, 18), 22, *range(27, 37), *range(50, 59)} or re.search('[yd]', pattern):
                    date_styles.add(index)
                elif fmt in {*range(18, 22), 45, 46, 47} or re.search('[hs]', pattern):
                    time_styles.add(index)
        props = workbook.find('./{*}workbookPr')
        epoch = date(1904, 1, 1) if props is not None and props.get('date1904') in ('1', 'true') else None
        for sheet in workbook.findall('./{*}sheets/{*}sheet'):
            name = sheet.get('name', '')
            if not name or len(name) > 100 or '!' in name or name in prepared.sheets:
                raise ScoreCheckParseError('Workbook sheet names must be unique and unambiguous')
            prepared.sheets[name] = {}
            rel_id = next((v for k, v in sheet.attrib.items() if k.endswith('}id')), None)
            if rel_id not in targets:
                raise ScoreCheckParseError('The workbook is missing a worksheet')
            for cell in _xml(archive, targets[rel_id]).findall('./{*}sheetData/{*}row/{*}c'):
                row, col = _coordinates(cell.get('r', ''))
                kind = cell.get('t', '')
                raw = cell.findtext('./{*}v', '')
                reason = ''
                if cell.find('./{*}f') is not None:
                    raw = '=' + cell.findtext('./{*}f', '')
                    reason = 'Formula cells are not evaluated; replace with a value'
                elif kind == 's':
                    try:
                        index = int(raw)
                        if index < 0:
                            raise ValueError
                        raw = shared[index]
                    except (ValueError, IndexError) as exc:
                        raise ScoreCheckParseError('Invalid workbook shared string reference') from exc
                elif kind == 'inlineStr':
                    raw = ''.join(t.text or '' for t in cell.findall('./{*}is//{*}t'))
                elif kind == 'e':
                    reason = 'Workbook cell contains an error'
                elif raw and kind in ('', 'n'):
                    style = int(cell.get('s', '0'))
                    if style in date_styles or style in time_styles:
                        try:
                            number = float(raw)
                            if not math.isfinite(number) or number < 0:
                                raise ValueError
                            if style in date_styles:
                                if epoch is None and int(number) == 60:
                                    raise ValueError  # Excel's fictional 1900-02-29.
                                base = epoch or (date(1899, 12, 31) if number < 60 else date(1899, 12, 30))
                                raw = (base + timedelta(days=int(number))).isoformat()
                            elif 0 <= number <= 1 and abs(number * 1440 - round(number * 1440)) < .00001:
                                minutes = round(number * 1440)
                                raw = f'{minutes // 60:02}:{minutes % 60:02}'
                            else:
                                raise ValueError
                        except (ValueError, OverflowError):
                            reason = 'Unsupported spreadsheet date or time value'
                _store(prepared, name, row, col, raw, reason)


def prepare_score_check_file(data: bytes, filename: str, *, file_ref: str | None = None):
    """Keep bounded source values and exact references for deterministic/AI validation."""
    if not data or len(data) > MAX_FILE_BYTES:
        raise ScoreCheckParseError('Files must contain data and be at most 10 MiB')
    file_ref = file_ref or filename
    if not file_ref or len(file_ref) > 300:
        raise ScoreCheckParseError('File reference must contain at most 300 characters')
    prepared = PreparedScoreCheckFile(file_ref, hashlib.sha256(data).hexdigest(), {})
    extension = PurePath(filename).suffix.casefold()
    if extension == '.xlsx':
        try:
            _xlsx(data, prepared)
        except (ValueError, OverflowError, TypeError, zipfile.BadZipFile) as exc:
            if isinstance(exc, ScoreCheckParseError):
                raise
            raise ScoreCheckParseError('The XLSX contains invalid workbook values') from exc
    elif extension in ('.csv', '.tsv'):
        try:
            text = data.decode('utf-8-sig')
            if '\x00' in text:
                raise ScoreCheckParseError('CSV files cannot contain NUL characters')
            try:
                dialect = csv.Sniffer().sniff(text[:8192], delimiters=',;\t')
                delimiter = dialect.delimiter
            except csv.Error:
                delimiter = '\t' if extension == '.tsv' else max(',;\t', key=text.count)
            prepared.sheets['CSV'] = {}
            for row, values in enumerate(csv.reader(io.StringIO(text, newline=''), delimiter=delimiter, strict=True), 1):
                if row > MAX_ROWS or len(values) > MAX_COLUMNS:
                    raise ScoreCheckParseError('The spreadsheet has too many rows or columns')
                for col, value in enumerate(values, 1):
                    _store(prepared, 'CSV', row, col, value)
        except (UnicodeDecodeError, csv.Error) as exc:
            raise ScoreCheckParseError('The CSV must be valid UTF-8 delimited text') from exc
    else:
        raise ScoreCheckParseError('Deterministic extraction supports CSV, TSV and XLSX files')
    if not any(prepared.sheets.values()):
        raise ScoreCheckParseError('The spreadsheet contains no populated cells')
    return prepared


def match_score_check_label(value, candidates):
    """Suggest an existing record; duplicates/ties never select an arbitrary ID."""
    key = match_key(value)
    scores = {}
    for item in candidates:
        if not item.get('id') or item.get('active') is False:
            continue
        aliases = [item.get(k, '') for k in ('display_name', 'name', 'short_code', 'code', 'staff_number', 'rota_name')]
        aliases += list(item.get('rota_aliases') or []) + list(item.get('aliases') or [])
        keys = [match_key(alias) for alias in aliases if isinstance(alias, str) and alias.strip()]
        score = max((SequenceMatcher(None, key, alias, autojunk=False).ratio() for alias in keys), default=0)
        scores[str(item['id'])] = max(scores.get(str(item['id']), 0), score)
    ranked = sorted(scores.items(), key=lambda entry: (-entry[1], entry[0]))
    if not key or not ranked or ranked[0][1] < .6:
        return {'suggested_id': None, 'confidence': 0.0, 'reason': 'No sufficiently similar existing record'}
    ident, score = ranked[0]
    if len(ranked) > 1 and ranked[1][1] >= score - .05:
        return {'suggested_id': None, 'confidence': min(round(score, 3), .89),
                'reason': 'Several existing records match; choose one explicitly'}
    return {'suggested_id': ident, 'confidence': 1.0 if score == 1 else min(round(score, 3), .89),
            'reason': 'Unique normalized name, code or alias match' if score == 1 else 'Similar spelling; confirm this suggestion'}


HEADERS = {
    'person': {'person', 'name', 'staff', 'staff name', 'employee', 'display name', 'staff number'},
    'date': {'date', 'day'},
    'duty_code': {'code', 'duty', 'duty code', 'duty type', 'assignment type', 'shift', 'session'},
    'clinic': {'clinic', 'service', 'location'},
    'start': {'start', 'start time', 'from'}, 'end': {'end', 'end time', 'to'},
    'overtime': {'overtime'}, 'team_lead': {'team lead', 'lead'},
}


def _header(value):
    key = match_key(value).replace('_', ' ').replace('-', ' ')
    return next((field for field, names in HEADERS.items() if key in names), None)


def _date(value, month=None):
    value = value.strip()
    try:
        return date.fromisoformat(value)
    except ValueError:
        pass
    numeric = re.fullmatch(r'(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})', value)
    if numeric:
        first, second, year = map(int, numeric.groups())
        if first <= 12 and second <= 12 and first != second:
            return None
        day, mon = (second, first) if second > 12 else (first, second)
        try:
            return date(year, mon, day)
        except ValueError:
            return None
    named = re.fullmatch(r'(\d{1,2})[\s\-/]+([A-Za-z]+)[\s,\-/]+(\d{4})', value)
    reverse = re.fullmatch(r'([A-Za-z]+)[\s\-/]+(\d{1,2})[\s,\-/]+(\d{4})', value)
    if named or reverse:
        day, mon, year = named.groups() if named else (reverse[2], reverse[1], reverse[3])
        names = {name.casefold(): i for i in range(1, 13) for name in (calendar.month_name[i], calendar.month_abbr[i])}
        try:
            return date(int(year), names[mon.casefold()], int(day))
        except (ValueError, KeyError):
            return None
    if month and re.fullmatch(r'\d{1,2}', value):
        try:
            return date(*month, int(value))
        except ValueError:
            pass
    return None


def _month(cells, period_start, period_end):
    last_day = period_end - timedelta(days=1) if period_end else None
    if period_start and last_day and (period_start.year, period_start.month) == (last_day.year, last_day.month):
        return period_start.year, period_start.month
    months = set()
    for (row, _), value in cells.items():
        if row > 20:
            continue
        for mon in range(1, 13):
            found = re.search(r'\b(?:' + calendar.month_name[mon] + '|' + calendar.month_abbr[mon] + r')\s+(\d{4})\b', value, re.I)
            if found:
                months.add((int(found[1]), mon))
    return next(iter(months)) if len(months) == 1 else None


def _clock(value):
    return value if re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d|24:00', value) else None


def parse_score_check_file(data, filename=None, *, file_ref=None, people=(), duty_types=(), clinics=(),
                           period_start=None, period_end=None, group=''):
    """Return the shared RotaExtraction shape, without persisting or applying suggestions."""
    prepared = data if isinstance(data, PreparedScoreCheckFile) else prepare_score_check_file(data, filename, file_ref=file_ref)
    period_start = date.fromisoformat(period_start) if isinstance(period_start, str) else period_start
    period_end = date.fromisoformat(period_end) if isinstance(period_end, str) else period_end
    if period_start and period_end and period_end <= period_start:
        raise ScoreCheckParseError('The exclusive extraction end must be after its start')
    result = {key: [] for key in ('files', 'people', 'duty_codes', 'clinics', 'assignments', 'unparsed', 'warnings')}
    result['unparsed'] = list(prepared.unparsed)
    blocked = {item['cell_ref'] for item in prepared.unparsed}
    declared = {key: {} for key in ('people', 'duty_codes', 'clinics')}

    def unparsed(ref, value, reason):
        if ref not in blocked:
            result['unparsed'].append({'file_ref': prepared.file_ref, 'cell_ref': ref,
                                       'value': str(value), 'reason': reason})
            blocked.add(ref)

    def declare(kind, value, ref):
        if value in declared[kind]:
            item = declared[kind][value]
            if ref not in item['cell_refs']:
                item['cell_refs'].append(ref)
            return
        if len(declared[kind]) >= 10000:
            raise ScoreCheckParseError('The spreadsheet has too many distinct people, duty codes or clinics')
        candidates = {'people': people, 'duty_codes': duty_types, 'clinics': clinics}[kind]
        match = match_score_check_label(value, candidates)
        item = {'file_ref': prepared.file_ref, 'cell_refs': [ref], 'confidence': match['confidence'], 'reason': match['reason']}
        if kind == 'duty_codes':
            item.update(code=value, meaning_as_written=value, suggested_type_id=match['suggested_id'], kind='other')
            candidate = next((c for c in duty_types if str(c.get('id')) == match['suggested_id']), {})
            if match['confidence'] == 1:
                item['kind'] = candidate.get('kind') if candidate.get('kind') in ('duty', 'leave', 'off', 'other') else 'duty'
        else:
            item['as_written'] = value
            if kind == 'people':
                item['suggested_person_id'] = match['suggested_id']
            else:
                candidate = next((c for c in clinics if str(c.get('id')) == match['suggested_id']), {})
                location = candidate.get('kind') == 'location'
                item['suggested_location_id'] = match['suggested_id'] if location else None
                item['suggested_service_id'] = None if location else match['suggested_id']
        declared[kind][value] = item
        result[kind].append(item)

    file_dates, file_layouts, file_rows, file_groups = [], [], 0, set()
    for sheet, source_cells in prepared.sheets.items():
        cells = {coord: value for coord, value in source_cells.items()
                 if f'{sheet}!{_column(coord[1])}{coord[0]}' not in blocked}
        rows = {}
        for (row, col), value in sorted(cells.items()):
            rows.setdefault(row, {})[col] = value
        month = _month(cells, period_start, period_end)
        inferred_group = group
        if not group:
            for values in list(rows.values())[:20]:
                for col, value in values.items():
                    if match_key(value) == 'group' and values.get(col + 1):
                        inferred_group = values[col + 1]
                    elif re.match(r'^group\s*:', value, re.I):
                        inferred_group = value.split(':', 1)[1].strip()
        layouts = []
        for row, values in list(rows.items())[:30]:
            fields = {}
            duplicate = False
            for col, value in values.items():
                name = _header(value)
                if name in fields:
                    duplicate = True
                if name:
                    fields[name] = col
            if 'person' in fields and 'date' in fields and ('duty_code' in fields or {'start', 'end'} <= fields.keys()):
                layouts.append(('sessions_list', row, fields, duplicate))
                break
            dates = {col: _date(value, month) for col, value in values.items()}
            dates = {col: day for col, day in dates.items() if day}
            if dates and ('person' in fields or (1 not in values and min(dates) > 1
                    and any(r > row and 1 in v for r, v in rows.items()))):
                layouts.append(('people_by_days', row, (fields.get('person', 1), dates), duplicate))
                break
            if ('date' in fields or 1 not in values) and len(values) >= 1:
                later_dates = [_date(v.get(fields.get('date', 1), ''), month) for r, v in rows.items() if r > row]
                if any(later_dates):
                    layouts.append(('days_by_people', row, (fields.get('date', 1), {c: v for c, v in values.items() if c != fields.get('date', 1)}), duplicate))
                    break
        sheet_dates, read_rows = [], set()

        def assignment(person, day_value, code, clinic, ref, person_ref, start='', end='', flags=None,
                       date_ref=None, clinic_ref=None):
            day = day_value if isinstance(day_value, date) else _date(day_value, month)
            if not person or len(person) > 160 or not code or len(code) > 160 or len(clinic) > 160:
                unparsed(ref, source_cells.get(_coordinates(ref.rsplit('!', 1)[1]), code), 'Person and one duty code are required (maximum 160 characters each)')
                return
            if not day:
                unparsed(date_ref or ref, day_value, 'Date is unreadable or ambiguous; use YYYY-MM-DD')
                return
            if day == date.max:
                unparsed(date_ref or ref, day.isoformat(), 'Date cannot have a representable exclusive period end')
                return
            if (period_start and day < period_start) or (period_end and day >= period_end):
                unparsed(date_ref or ref, day.isoformat(), 'Date is outside the selected period')
                return
            if bool(start) != bool(end) or (start and (not _clock(start) or not _clock(end))):
                unparsed(ref, f'{start}–{end}', 'Provide both start and end in HH:MM format')
                return
            if re.search(r'[;|\n]', code):
                unparsed(ref, code, 'Multiple duty codes in one cell need manual review')
                return
            declare('people', person, person_ref)
            declare('duty_codes', code, ref)
            if clinic:
                declare('clinics', clinic, clinic_ref or ref)
            if len(result['assignments']) >= SCORE_CHECK_LIMITS['assignments']:
                raise ScoreCheckParseError('The spreadsheet exceeds the extracted assignment limit')
            result['assignments'].append({'file_ref': prepared.file_ref, 'cell_ref': ref,
                'person': person, 'date': day.isoformat(), 'duty_code': code, 'clinic': clinic,
                'start': start or None, 'end': end or None, **(flags or {})})
            sheet_dates.append(day)

        if layouts:
            layout, header_row, fields, duplicate = layouts[0]
            if duplicate:
                result['warnings'].append({'file_ref': prepared.file_ref, 'message': f'{sheet}: duplicate header names; no assignments extracted'})
                layout = 'other'
            else:
                for row, values in rows.items():
                    if row <= header_row:
                        continue
                    read_rows.add(row)
                    if layout == 'sessions_list':
                        get = lambda name: values.get(fields.get(name), '')
                        if any(f'{sheet}!{_column(col)}{row}' in blocked for col in fields.values()):
                            continue
                        if not any(get(name) for name in ('person', 'date', 'duty_code', 'start', 'end')):
                            continue
                        code = get('duty_code') or (f"{get('start')}–{get('end')}" if get('start') and get('end') else '')
                        ref = f'{sheet}!{_column(fields.get("duty_code", fields["person"]))}{row}'
                        flags = {}
                        invalid_flag = False
                        for flag in ('team_lead', 'overtime'):
                            val = match_key(get(flag))
                            if val and val not in ('yes', 'no', 'true', 'false', '1', '0'):
                                unparsed(f'{sheet}!{_column(fields[flag])}{row}', get(flag), f'{flag} must be yes/no or true/false')
                                invalid_flag = True
                            flags[flag] = val in ('yes', 'true', '1')
                        if not invalid_flag:
                            assignment(get('person'), get('date'), code, get('clinic'), ref,
                                       f'{sheet}!{_column(fields["person"])}{row}', get('start'), get('end'), flags,
                                       date_ref=f'{sheet}!{_column(fields["date"])}{row}',
                                       clinic_ref=f'{sheet}!{_column(fields["clinic"])}{row}' if get('clinic') else None)
                    else:
                        axis_col, axis_values = fields
                        if layout == 'people_by_days':
                            for col in values.keys() - axis_values.keys() - {axis_col}:
                                unparsed(f'{sheet}!{_column(col)}{row}', values[col], 'Column date is unreadable or ambiguous')
                        for col, axis_value in axis_values.items():
                            code = values.get(col, '')
                            if not code:
                                continue
                            ref = f'{sheet}!{_column(col)}{row}'
                            person, day = (values.get(axis_col, ''), axis_value) if layout == 'people_by_days' else (axis_value, values.get(axis_col, ''))
                            person_ref = f'{sheet}!{_column(axis_col if layout == "people_by_days" else col)}{row if layout == "people_by_days" else header_row}'
                            if code.count('@') > 1:
                                unparsed(ref, code, 'Use one duty code, optionally followed by @ clinic')
                                continue
                            code, separator, clinic = code.partition('@')
                            assignment(person, day, code.strip(), clinic.strip() if separator else '', ref, person_ref)
        else:
            layout = 'other'
        if layout == 'other':
            result['warnings'].append({'file_ref': prepared.file_ref,
                'message': f'{sheet}: layout not recognized; use person/date/duty code columns or a labelled date grid'})
            for (row, col), value in cells.items():
                unparsed(f'{sheet}!{_column(col)}{row}', value, 'Unrecognized or ambiguous layout')
        file_dates.extend(sheet_dates)
        file_layouts.append(layout)
        file_rows += len(read_rows)
        if inferred_group:
            if len(inferred_group) > 160:
                result['warnings'].append({'file_ref': prepared.file_ref, 'message': f'{sheet}: group exceeds 160 characters; choose a shorter group'})
            else:
                file_groups.add(inferred_group)
    if len(file_groups) > 1:
        result['warnings'].append({'file_ref': prepared.file_ref, 'message': 'Sheets contain different groups; choose a group explicitly'})
    result['files'].append({'file_ref': prepared.file_ref,
        'group': next(iter(file_groups)) if len(file_groups) == 1 else '',
        'layout': file_layouts[0] if len(set(file_layouts)) == 1 else 'other',
        'rows_read': file_rows, 'confidence': 1.0 if file_dates and not result['unparsed'] and not result['warnings'] else (.5 if file_dates else 0.0),
        'starts_on': min(file_dates).isoformat() if file_dates else None,
        'ends_on': (max(file_dates) + timedelta(days=1)).isoformat() if file_dates and max(file_dates) < date.max else None})
    return result
