"""Print presets ⇄ settings (apps/web/src/print_presets.ts) and the page plan (schedule_export.ts `plan`), run in Node
from the compiled client: stored settings come back complete and valid, a dialog opens with the saved view's last
print, else the session's, else the defaults (personal → department → Standard), and rows go to pages with repeated
headers, kept groups and group breaks."""
import csv
import io
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from packages.contracts import schemas as contract

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def run(client_modules):
    node = shutil.which('node')
    folder = client_modules

    def call(body, payload=None):
        script = (f"const p=await import({json.dumps((folder / 'print_presets.js').as_uri())});"
                  f"const x=await import({json.dumps((folder / 'schedule_export.js').as_uri())});"
                  f"const input=JSON.parse(process.argv[1]);const out=(()=>{{{body}}})();console.log(JSON.stringify(out));")
        out = subprocess.run([node, '--input-type=module', '-e', script, json.dumps(payload)],
                             capture_output=True, text=True, check=True, timeout=30)
        return json.loads(out.stdout)
    return call


def test_stored_settings_come_back_complete_and_valid(run):
    out = run('return p.normalise(input);', {'paper': 'a3', 'scale': 'custom', 'scale_percent': 300, 'margins': 'huge',
                                            'include': ['signature', 'bogus', 'title'], 'view_id': ''})
    assert out['paper'] == 'a3' and out['margins'] == 'normal'  # unknown values fall back to Standard's
    assert out['scale_percent'] == 200                          # custom zoom kept within 25–200
    assert out['include'] == ['title', 'signature']             # known only, in the dialog's order
    assert out['view_id'] is None and out['schema_version'] == 1
    assert run('return p.normalise({scale:"fit_page",scale_percent:80}).scale_percent;') is None
    standard = run('return p.normalise(null);')
    assert standard == run('return p.STANDARD;')


def test_every_built_in_is_valid_and_same_print_ignores_order(run):
    assert run('return p.BUILT_INS.every(b=>p.samePrint(b.print,p.normalise(b.print)));')
    assert run('return p.samePrint({...p.STANDARD,include:["key","title"]},{...p.STANDARD,include:["title","key"]});')
    assert not run('return p.samePrint(p.STANDARD,{...p.STANDARD,colour:"greyscale"});')


def test_defaults_resolve_personal_then_department_then_standard(run):
    views = [{'id': 'a', 'revision': 1, 'name': 'A', 'definition': {'print': {'paper': 'a3'}}},
             {'id': 'b', 'revision': 1, 'name': 'B', 'definition': {'print': {'paper': 'legal'}}}]
    pick = 'const s=p.startingPreset(input.views,input.defaults);return [s.id,s.print.paper];'
    assert run(pick, {'views': views, 'defaults': {'personal_set': True, 'personal_view_id': 'b', 'department_set': True,
                                                   'department_view_id': 'a'}}) == ['b', 'legal']
    assert run(pick, {'views': views, 'defaults': {'department_set': True, 'department_view_id': 'a'}}) == ['a', 'a3']
    # Set to null: Standard chosen explicitly, before the department's choice.
    assert run(pick, {'views': views, 'defaults': {'personal_set': True, 'personal_view_id': None, 'department_set': True,
                                                   'department_view_id': 'a'}}) == ['builtin:standard', 'a4']
    # A default no longer visible follows the next level.
    assert run(pick, {'views': views, 'defaults': {'personal_set': True, 'personal_view_id': 'gone'}}) == ['builtin:standard', 'a4']


PLAN = 'return x.plan(input.m,input.budget,input.repeat,i=>input.breaks.includes(i));'


def test_a_saved_view_print_comes_before_the_session_and_the_defaults(run):
    opening = 'const o=p.openingPrint(input.kept,input.last);return o&&[o.id,o.print.paper,o.print.colour];'
    session = {'id': 'mine', 'print': {'paper': 'legal'}}
    # The settings last used from the view on screen win over this session's.
    assert run(opening, {'kept': {'paper': 'a3', 'colour': 'greyscale'}, 'last': session}) == ['mine', 'a3', 'greyscale']
    # Settings equal to a built-in name it, so the dialog does not call them changed.
    notice = run('return p.BUILT_INS[1].print;')
    assert run(opening, {'kept': notice, 'last': session})[0] == 'builtin:notice'
    assert run(opening, {'kept': {'paper': 'a3'}, 'last': None})[0] == 'builtin:standard'
    # No settings kept on the view: this session's, else nothing, so the personal and department defaults apply.
    assert run(opening, {'kept': None, 'last': session}) == ['mine', 'legal', None]
    assert run(opening, {'kept': None, 'last': None}) is None


def test_fit_on_one_page_shrinks_until_the_drawn_height_fits(run):
    # Measured 1 Nov 2026 UTC (Month compact, A4 landscape): 765.75 px unzoomed, 627.5 px room, but drawn at 0.8195 the
    # body was 645.7 px: zoomed text wraps and rounds differently, so unzoomed height × zoom undermeasures.
    drawn = 'k=>k>=0.8?765.75*k+18.2:765.75*k+9'
    out = run(f'const tried=[];const z=x.fitZoom(0.819458,627.5,k=>{{tried.push(k);return ({drawn})(k);}});return {{z,tried}};')
    assert len(out['tried']) == 2 and out['tried'][0] == 0.819458   # measured, shrunk by the overflow, measured again
    assert 765.75 * out['z'] + 9 <= 627.5 and out['z'] > 0.79      # the drawn height fits, barely smaller
    assert run('return x.fitZoom(0.5,627.5,k=>600);') == 0.5        # fits already: drawn once, unchanged
    # A height that never fits stops after a few tries; the page plan then moves the rest to a next page.
    assert run('let n=0;x.fitZoom(1,100,k=>{n++;return 1000;});return n;') == 4


def plan(run, kinds, heights, budget, repeat=True, breaks=(), pre=0, post=0):
    return run(PLAN, {'m': {'units': [{'kind': k} for k in kinds], 'heights': heights, 'pre': pre, 'post': post},
                      'budget': {'first': budget[0], 'next': budget[1]}, 'repeat': repeat, 'breaks': list(breaks)})


def test_rows_fill_pages_without_splitting_and_headers_repeat(run):
    pages = plan(run, ['head', 'row', 'row', 'row', 'row'], [10, 40, 40, 40, 40], (100, 100))
    assert [p['rows'] for p in pages] == [[1, 2], [3, 4]]       # 10 + 40 + 40 fits; the next row moves whole
    assert [p['first'] for p in pages] == [True, False]


def test_a_group_header_stays_with_its_first_row_and_is_carried(run):
    kinds = ['head', 'group', 'row', 'row', 'group', 'row', 'row']
    pages = plan(run, kinds, [10, 10, 30, 30, 10, 30, 30], (100, 100))
    # The second group's header would end page 1 alone: it opens page 2 with its first row.
    assert pages[0]['rows'] == [1, 2, 3] and pages[1]['rows'][0] == 4
    pages = plan(run, ['head', 'group', 'row', 'row', 'row'], [10, 10, 40, 40, 40], (100, 100))
    assert pages[1]['carry'] == 1                               # "continued" above its rows on the next page


def test_page_break_per_group_and_trailing_key(run):
    kinds = ['head', 'group', 'row', 'group', 'row']
    pages = plan(run, kinds, [10, 10, 10, 10, 10], (1000, 1000), breaks=[3])
    assert [p['rows'] for p in pages] == [[1, 2], [3, 4]]
    # The key after the rows gets its own page only when it does not fit under them.
    assert len(plan(run, ['head', 'row'], [10, 80], (100, 100), post=20)) == 2
    assert len(plan(run, ['head', 'row'], [10, 60], (100, 100), post=20)) == 1


def test_page_box_sizes(run):
    assert run('return x.pageBox({paper:"a4",orientation:"landscape",margins:"normal"});') == \
        {'pw': 297, 'ph': 210, 'm': 12, 'width': 1031, 'height': 702}
    assert run('return x.pageBox({paper:"legal",orientation:"portrait",margins:"narrow"}).ph;') == 355.6


def test_image_pixels_follow_paper_and_quality_not_the_screen(run):
    # A page's PNG size is its paper in millimetres at the chosen dpi (the screen's pixel density plays no part).
    assert run('return x.pagePixels({paper:"a4",orientation:"landscape"},300);') == {'width': 3508, 'height': 2480}
    assert run('return x.pagePixels({paper:"a3",orientation:"portrait"},200);') == {'width': 2339, 'height': 3307}
    assert run('return x.pagePixels({paper:"a4",orientation:"portrait"},96);') == {'width': 794, 'height': 1123}


def test_image_quality_is_a_preset_setting_with_the_generated_default(run):
    # An older preset (no field) and an explicit default read and compare the same: the default is stored absent.
    assert run('const o=p.normalise({paper:"a3"});return [o.image_dpi,p.dpiOf(o)];') == [None, contract.PRINT_IMAGE_DPI_DEFAULT]
    explicit = {'image_dpi': contract.PRINT_IMAGE_DPI_DEFAULT}
    assert run('return p.normalise(input).image_dpi;', explicit) is None
    assert run('return p.samePrint(p.STANDARD,{...p.STANDARD,...input});', explicit)
    # Every other generated quality is kept, differs from the preset and survives a stored round trip.
    for dpi in (int(k) for k in contract.PRINT_IMAGE_DPI if int(k) != contract.PRINT_IMAGE_DPI_DEFAULT):
        out = run('const o=p.normalise(JSON.parse(JSON.stringify(p.normalise(input))));'
                  'return [o.image_dpi,p.dpiOf(o),p.samePrint(p.STANDARD,o)];', {'image_dpi': dpi})
        assert out == [dpi, dpi, False]
    # An unknown quality falls back to the default (as the contract refuses it).
    assert run('return p.normalise({image_dpi:72}).image_dpi;') is None
    assert run('return p.BUILT_INS.map(b=>b.print.image_dpi);') == [None] * 4


@pytest.mark.parametrize('stored', [True, False, None, 'line'])
def test_a_stored_long_names_choice_is_read_and_ignored(run, stored):
    """DL-014 reviewed: the Long names choice is retired. A preset saved with it reads without
    error as the same options as one without, compares equal (never "changed"), and is never written back."""
    preset = {'paper': 'a3', 'orientation': 'portrait', 'image_dpi': 300, 'include': ['title']}
    with_old, without = run('return [p.normalise({...input.preset,wrap:input.stored}),p.normalise(input.preset)];',
                            {'preset': preset, 'stored': stored})
    assert with_old == without and 'wrap' not in with_old
    assert run('return p.samePrint({...input.preset,wrap:input.stored},input.preset);', {'preset': preset, 'stored': stored})
    assert run('return p.BUILT_INS.every(b=>!("wrap" in b.print));')


FORMULA = tuple('=+-@\t\r\n') + ('\uff1d', '\uff0b', '\uff0d', '\uff20')


def test_csv_keeps_formula_text_as_text_and_rows_intact(run):
    cells = ['=1+1', '+SUM(A1:A2)', '-2+3', '@cmd', '\tx', '\rx', '\uff1d1+1', '\uff0bx', 'a;=1+1', 'a,=1+1',
             'say "hi"', 'two\nlines', 'Zoë · Dr. Al-Tamsin', 'مستشفى', '', 'plain', '-5', '3.5', '-0.25', '12']
    numbers = {'-5', '3.5', '-0.25', '12'}
    text = run('return x.csvText(input);', [cells, ['When', 'People']])
    assert text.endswith('\r\n') and text.count('\r\n') == 2  # CRLF ends each row; a line break in a field is quoted
    parsed = list(csv.reader(io.StringIO(text, newline='')))
    # Row equivalence: every field comes back whole; only formula-leading text gains the apostrophe that keeps it text.
    assert parsed == [[c if c in numbers or not c.startswith(FORMULA) else "'" + c for c in cells], ['When', 'People']]
    assert not any(c.startswith(FORMULA) for c in parsed[0] if c not in numbers)
    # Plain numbers (negative too) stay numbers; a field with any locale's separator is quoted.
    assert run('return ["-5","a;b","a\\tb","x"].map(x.csvCell);') == ['-5', '"a;b"', '"a\tb"', 'x']
