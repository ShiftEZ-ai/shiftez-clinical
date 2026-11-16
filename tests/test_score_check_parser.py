import io
import zipfile
from uuid import uuid4
from xml.etree import ElementTree as ET

import pytest

from apps.api.services import score_check_parser as parser


def extract(text, **kwargs):
    return parser.parse_score_check_file(text.encode('utf-8'), 'rota.csv', **kwargs)


def workbook(sheets, *, shared=(), date1904=False, target=None):
    """Small OOXML fixtures include rich strings, cached formulas and styled numbers."""
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    relns = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    book = ET.Element('workbook', xmlns=ns, attrib={'xmlns:r': relns})
    ET.SubElement(book, 'workbookPr', date1904='1' if date1904 else '0')
    sheet_list = ET.SubElement(book, 'sheets')
    relationships = ET.Element('Relationships', xmlns='http://schemas.openxmlformats.org/package/2006/relationships')
    parts = {}
    for index, (name, cells) in enumerate(sheets.items(), 1):
        ET.SubElement(sheet_list, 'sheet', name=name, sheetId=str(index), attrib={'r:id': f'rId{index}'})
        ET.SubElement(relationships, 'Relationship', Id=f'rId{index}', Type=relns + '/worksheet',
                      Target=target or f'worksheets/sheet{index}.xml')
        sheet = ET.Element('worksheet', xmlns=ns)
        data = ET.SubElement(sheet, 'sheetData')
        rows = {}
        for ref, value in cells.items():
            row_number, _ = parser._coordinates(ref)
            if row_number not in rows:
                rows[row_number] = ET.SubElement(data, 'row', r=str(row_number))
            row = rows[row_number]
            cell = ET.SubElement(row, 'c', r=ref)
            if isinstance(value, dict):
                cell.attrib.update({key: str(val) for key, val in value.items() if key not in ('formula', 'value')})
                if 'formula' in value:
                    ET.SubElement(cell, 'f').text = value['formula']
                ET.SubElement(cell, 'v').text = str(value.get('value', ''))
            else:
                cell.set('t', 'inlineStr')
                ET.SubElement(ET.SubElement(cell, 'is'), 't').text = str(value)
        parts[f'xl/worksheets/sheet{index}.xml'] = ET.tostring(sheet)
    parts['xl/workbook.xml'] = ET.tostring(book)
    parts['xl/_rels/workbook.xml.rels'] = ET.tostring(relationships)
    parts['xl/styles.xml'] = (f'<styleSheet xmlns="{ns}"><cellXfs>'
        '<xf numFmtId="0"/><xf numFmtId="14"/><xf numFmtId="20"/></cellXfs></styleSheet>').encode()
    if shared:
        root = ET.Element('sst', xmlns=ns)
        for value in shared:
            ET.SubElement(ET.SubElement(root, 'si'), 't').text = value
        parts['xl/sharedStrings.xml'] = ET.tostring(root)
    return zip_parts(parts)


def zip_parts(parts):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return output.getvalue()


def test_session_list_infers_metadata_refs_and_candidate_suggestions():
    person_id, duty_id, clinic_id = (str(uuid4()) for _ in range(3))
    result = extract('Group,Fellows\nName,Date,Duty code,Clinic,Start,End,Overtime,Team lead\n'
        'Mújapoju Example,2026-11-02,AM,North,08:00,12:00,yes,no\n',
        people=[{'id': person_id, 'display_name': 'Maple Example'}],
        duty_types=[{'id': duty_id, 'name': 'Morning', 'code': 'AM', 'kind': 'duty'}],
        clinics=[{'id': clinic_id, 'name': 'North'}])
    assert result['files'] == [{'file_ref': 'rota.csv', 'group': 'Fellows', 'layout': 'sessions_list',
        'rows_read': 1, 'confidence': 1.0, 'starts_on': '2026-11-02', 'ends_on': '2026-11-03'}]
    assert result['people'][0]['suggested_person_id'] == person_id
    assert result['people'][0]['cell_refs'] == ['CSV!A3']
    assert result['duty_codes'][0]['suggested_type_id'] == duty_id
    assert result['duty_codes'][0]['kind'] == 'duty'
    assert result['clinics'][0]['suggested_service_id'] == clinic_id
    assert result['assignments'][0] == {'file_ref': 'rota.csv', 'cell_ref': 'CSV!C3', 'person': 'Mújapoju Example',
        'date': '2026-11-02', 'duty_code': 'AM', 'clinic': 'North', 'start': '08:00', 'end': '12:00',
        'overtime': True, 'team_lead': False}
    assert not result['unparsed']


@pytest.mark.parametrize('header', ['Name', ''])
def test_people_by_days_accepts_month_title_and_embedded_clinic(header):
    result = extract(f'November 2026\n{header},1,2\nAlex,AM @ North,PM\nBea,,OFF\n')
    assert result['files'][0]['layout'] == 'people_by_days'
    assert [(a['person'], a['date'], a['duty_code'], a['clinic'], a['cell_ref']) for a in result['assignments']] == [
        ('Alex', '2026-11-01', 'AM', 'North', 'CSV!B3'), ('Alex', '2026-11-02', 'PM', '', 'CSV!C3'),
        ('Bea', '2026-11-02', 'OFF', '', 'CSV!C4')]
    assert all(c['kind'] == 'other' for c in result['duty_codes'])  # No clinical meaning guessed from codes.


@pytest.mark.parametrize('header', ['Date', ''])
def test_days_by_people_grid_uses_header_person_refs(header):
    result = extract(f'{header},Alex,Bea\n2026-11-01,AM,PM\n2026-11-02,,AM\n')
    assert result['files'][0]['layout'] == 'days_by_people'
    assert len(result['assignments']) == 3
    assert result['people'][0]['cell_refs'] == ['CSV!B1']


@pytest.mark.parametrize('day', ['01/02/2026', '02-03-2026', '2026-02-30', 'soon'])
def test_unreadable_or_ambiguous_dates_never_make_assignments(day):
    result = extract(f'Person,Date,Code\nAlex,{day},AM\n')
    assert result['assignments'] == []
    assert 'ambiguous' in result['unparsed'][0]['reason']


@pytest.mark.parametrize('day,expected', [('13/11/2026', '2026-11-13'), ('11/13/2026', '2026-11-13'),
    ('1-Nov-2026', '2026-11-01'), ('Nov 1 2026', '2026-11-01'), ('12/12/2026', '2026-12-12')])
def test_documented_date_formats(day, expected):
    assert extract(f'Person,Date,Code\nAlex,{day},AM\n')['assignments'][0]['date'] == expected


def test_single_month_context_and_selected_period_bounds():
    result = extract('Name,1,2,30\nAlex,AM,PM,AM\n', period_start='2026-11-02', period_end='2026-11-30')
    assert [a['date'] for a in result['assignments']] == ['2026-11-02']
    assert len(result['unparsed']) == 2
    assert all('outside' in item['reason'] for item in result['unparsed'])
    assert not extract('Name,1,2\nAlex,AM,PM\n')['assignments']


def test_invalid_grid_column_is_reported_without_dropping_its_duties():
    result = extract('Name,2026-11-01,02/03/2026\nAlex,AM,PM\n')
    assert len(result['assignments']) == 1
    assert result['unparsed'][0]['cell_ref'] == 'CSV!C2'


@pytest.mark.parametrize('extra', ['"AM; PM"', '"AM|PM"', '"AM\nPM"'])
def test_multi_duty_cells_require_review(extra):
    result = extract(f'Name,2026-11-01\nAlex,{extra}\n')
    assert result['assignments'] == []
    assert 'Multiple duty codes' in result['unparsed'][0]['reason']


@pytest.mark.parametrize('row', ['Alex,2026-11-01,AM,08:00,', 'Alex,2026-11-01,AM,25:00,12:00'])
def test_invalid_or_incomplete_times_are_unparsed(row):
    result = extract('Person,Date,Code,Start,End\n' + row)
    assert not result['assignments']
    assert 'both start and end' in result['unparsed'][0]['reason']


def test_times_only_list_and_location_candidate():
    result = extract('Person,Date,Start,End,Location\nAlex,2026-11-01,08:00,12:00,North\n',
        clinics=[{'id': str(uuid4()), 'name': 'North', 'kind': 'location'}])
    assert result['assignments'][0]['duty_code'] == '08:00–12:00'
    assert result['clinics'][0]['suggested_location_id']
    assert result['clinics'][0]['suggested_service_id'] is None


def test_invalid_flag_and_duplicate_headers_are_explicit():
    result = extract('Person,Date,Code,Overtime\nAlex,2026-11-01,AM,perhaps\n')
    assert not result['assignments']
    assert 'must be yes/no' in result['unparsed'][0]['reason']
    duplicate = extract('Person,Date,Code,Code\nAlex,2026-11-01,AM,PM\n')
    assert not duplicate['assignments']
    assert any('duplicate header' in w['message'] for w in duplicate['warnings'])


def test_csv_delimiters_bom_quoted_newlines_and_prepared_inventory():
    prepared = parser.prepare_score_check_file(b'\xef\xbb\xbfPerson;Date;Code;Note\r\nAlex;2026-11-01;AM;"two\nlines"', 'rota.csv', file_ref='f1')
    assert prepared.file_ref == 'f1'
    assert prepared.cell_values['CSV!D2'] == 'two\nlines'
    assert 'CSV!C2' in prepared.cell_refs
    assert len(prepared.sha256) == 64
    assert 'CSV!C2' in prepared.text
    assert parser.parse_score_check_file(prepared)['assignments'][0]['duty_code'] == 'AM'
    assert parser.parse_score_check_file(b'Person\tDate\tCode\nAlex\t2026-11-01\tAM', 'rota.tsv')['assignments']


def test_xlsx_shared_strings_dates_times_and_formula_cached_value():
    data = workbook({'Residents': {'A1': 'Person', 'B1': 'Date', 'C1': 'Code', 'D1': 'Start', 'E1': 'End',
        'A2': {'t': 's', 'value': 0}, 'B2': {'s': 1, 'value': 46327}, 'C2': 'AM',
        'D2': {'s': 2, 'value': .5}, 'E2': {'s': 2, 'value': .75},
        'A3': 'Bea', 'B3': '2026-11-02', 'C3': {'formula': 'D3', 'value': 'PM'},
        'D3': '08:00', 'E3': '12:00'}}, shared=['Alex'])
    prepared = parser.prepare_score_check_file(data, 'rota.xlsx')
    assert prepared.cell_values['Residents!B2'] == '2026-11-01'
    assert prepared.cell_values['Residents!D2'] == '12:00'
    assert prepared.cell_values['Residents!C3'] == '=D3'
    result = parser.parse_score_check_file(prepared)
    assert len(result['assignments']) == 1
    assert result['assignments'][0]['start'] == '12:00'
    assert result['unparsed'][0]['cell_ref'] == 'Residents!C3'


def test_xlsx_1904_epoch_and_multi_sheet_file_summary():
    data = workbook({'One': {'A1': 'Name', 'B1': {'s': 1, 'value': 44865}, 'A2': 'Alex', 'B2': 'AM'},
                     'Two': {'A1': 'Name', 'B1': '2026-11-02', 'A2': 'Bea', 'B2': 'PM'}}, date1904=True)
    result = parser.parse_score_check_file(data, 'rota.xlsx')
    assert len(result['files']) == 1
    assert result['files'][0]['starts_on'] == '2026-11-01'
    assert len(result['assignments']) == 2


def test_fuzzy_matching_normalizes_aliases_but_never_confidently_accepts_typoes():
    ident = str(uuid4())
    candidates = [{'id': ident, 'display_name': 'Maple Example', 'rota_aliases': ['S Ex']}]
    assert parser.match_score_check_label(' Mújapoju  EXAMPLE ', candidates)['confidence'] == 1
    assert parser.match_score_check_label('S Ex', candidates)['suggested_id'] == ident
    fuzzy = parser.match_score_check_label('Maple Exampl', candidates)
    assert fuzzy['suggested_id'] == ident and 0 < fuzzy['confidence'] < .9
    assert 'confirm' in fuzzy['reason']
    assert parser.match_score_check_label('Unrelated', candidates)['suggested_id'] is None


def test_duplicate_near_tied_and_inactive_matches_never_choose_an_arbitrary_record():
    candidates = [{'id': str(uuid4()), 'name': name} for name in ('Alex Example', 'Alex Example')]
    assert parser.match_score_check_label('Alex Example', candidates)['suggested_id'] is None
    candidates[1]['name'] = 'Alec Example'
    assert parser.match_score_check_label('Ale Example', candidates)['suggested_id'] is None
    candidates[0]['active'] = False
    assert parser.match_score_check_label('Alex Example', candidates)['confidence'] < .9


@pytest.mark.parametrize('data,name', [(b'', 'a.csv'), (b'\xff', 'a.csv'), (b'Name\x00', 'a.csv'),
    (b'garbage', 'a.xlsx'), (b'abc', 'a.xls'), (b'Person,Date,Code\n"unterminated', 'a.csv')])
def test_invalid_uploads_raise_actionable_parse_error(data, name):
    with pytest.raises(parser.ScoreCheckParseError):
        parser.prepare_score_check_file(data, name)


def test_upload_expansion_xml_and_coordinate_limits(monkeypatch):
    monkeypatch.setattr(parser, 'MAX_FILE_BYTES', 8)
    with pytest.raises(parser.ScoreCheckParseError, match='10 MiB'):
        parser.prepare_score_check_file(b'x' * 9, 'a.csv')
    monkeypatch.setattr(parser, 'MAX_FILE_BYTES', 10 * 1024 * 1024)
    with pytest.raises(parser.ScoreCheckParseError, match='archive limits'):
        parser.prepare_score_check_file(zip_parts({'bomb': b'x' * 100000}), 'a.xlsx')
    with pytest.raises(parser.ScoreCheckParseError, match='unsafe'):
        parser.prepare_score_check_file(zip_parts({'xl/workbook.xml': b'<!DOCTYPE x [<!ENTITY a "x">]><x/>'}), 'a.xlsx')
    data = workbook({'One': {'A1': 'Name', 'B1': '2026-11-01', 'A2': 'Alex', 'B2': 'AM'}})
    monkeypatch.setattr(parser, 'MAX_ROWS', 1)
    with pytest.raises(parser.ScoreCheckParseError, match='rows or columns'):
        parser.prepare_score_check_file(data, 'a.xlsx')


def test_archive_relationship_traversal_and_shared_string_index_are_rejected():
    for data in (workbook({'One': {'A1': 'Name'}}, target='../../outside.xml'),
                 workbook({'One': {'A1': {'t': 's', 'value': -1}}}, shared=['Name'])):
        with pytest.raises(parser.ScoreCheckParseError):
            parser.prepare_score_check_file(data, 'a.xlsx')


def test_csv_formula_and_long_cell_stay_unparsed():
    result = extract('Person,Date,Code\nAlex,2026-11-01,=SUM(1)\nBea,2026-11-02,' + 'X' * 1001)
    assert not result['assignments']
    assert len(result['unparsed']) == 2


def test_unknown_layout_preserves_source_cells_for_manual_review():
    result = extract('Who,Someday\nAlex,Tuesday\n')
    assert result['files'][0]['layout'] == 'other'
    assert len(result['unparsed']) == 4
    assert result['warnings']


def test_deterministic_output_uses_shared_extraction_contract():
    from packages.contracts.schemas import RotaExtraction
    result = extract('Person,Date,Code\nAlex,2026-11-01,AM\n')
    parsed = RotaExtraction.model_validate(result)
    assert parsed.assignments[0].person == 'Alex'


def test_exact_real_duty_type_candidate_without_kind_is_a_duty():
    result = extract('Person,Date,Code\nAlex,2026-11-01,Morning\n',
        duty_types=[{'id': str(uuid4()), 'name': 'Morning', 'definition': {'purpose': 'staffed'}}])
    assert result['duty_codes'][0]['kind'] == 'duty'


def test_full_month_end_is_exclusive_and_inferred_end_follows_last_assignment():
    result = extract('Person,Date,Code\nAlex,2026-11-30,AM\nAlex,2026-12-01,PM\n',
        period_start='2026-11-01', period_end='2026-12-01')
    assert len(result['assignments']) == 1
    assert result['files'][0]['ends_on'] == '2026-12-01'
    assert result['unparsed'][0]['cell_ref'] == 'CSV!B3'
    grid = extract('Name,1,30\nAlex,AM,PM\n', period_start='2026-11-01', period_end='2026-12-01')
    assert [row['date'] for row in grid['assignments']] == ['2026-11-01', '2026-11-30']
    maximum = extract('Person,Date,Code\nAlex,9999-12-31,AM\n')
    assert not maximum['assignments'] and maximum['unparsed']


def test_declaration_refs_use_clinic_column_and_group_is_not_silently_truncated():
    result = extract('Group,' + 'X' * 161 + '\nPerson,Date,Code,Clinic\nAlex,2026-11-01,AM,North\n')
    assert result['clinics'][0]['cell_refs'] == ['CSV!D3']
    assert result['files'][0]['group'] == ''
    assert result['warnings'] and result['files'][0]['confidence'] < 1


def test_extraction_cardinality_and_populated_cell_limits(monkeypatch):
    monkeypatch.setitem(parser.SCORE_CHECK_LIMITS, 'assignments', 1)
    with pytest.raises(parser.ScoreCheckParseError, match='assignment limit'):
        extract('Name,2026-11-01,2026-11-02\nAlex,AM,PM\n')
    monkeypatch.setattr(parser, 'MAX_CELLS', 2)
    with pytest.raises(parser.ScoreCheckParseError, match='populated cells'):
        parser.prepare_score_check_file(b'Person,Date,Code', 'a.csv')


def test_department_scale_grid_preserves_every_assignment():
    header = 'Name,' + ','.join(f'2026-11-{day:02}' for day in range(1, 31))
    rows = [f'Person {person},' + ','.join('AM' for _ in range(30)) for person in range(150)]
    result = extract('\n'.join([header, *rows]))
    assert len(result['assignments']) == 4500
    assert len(result['people']) == 150
    assert result['files'][0]['rows_read'] == 150
