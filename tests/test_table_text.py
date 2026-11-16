"""The Sheet's clipboard text (apps/web/src/table_text.ts), run in Node from the compiled client: what toTable writes,
parseTable reads back as the same cells (Excel's quoting), and grid pastes keep their shape."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def table_text(client_modules):
    node = shutil.which('node')
    folder = client_modules

    def run(expression, payload):
        script = (f"const m=await import({json.dumps((folder / 'table_text.js').as_uri())});"
                  f"const input=JSON.parse(process.argv[1]);console.log(JSON.stringify({expression}));")
        out = subprocess.run([node, '--input-type=module', '-e', script, json.dumps(payload)],
                             capture_output=True, text=True, check=True, timeout=15)
        return json.loads(out.stdout)
    return run


ROUND_TRIPS = [
    [['']],                                   # one empty cell
    [['say "hi"']],                           # a quote
    [['a\tb', 'c']],                          # a tab inside a cell
    [['North\nSouth'], ['next']],             # a line break inside a cell
    [['one\r\ntwo']],                         # CRLF inside a cell
    [['a', '']],                              # a trailing empty cell (trailing tab)
    [['a'], ['']],                            # a trailing blank row
    [['x'], [''], ['z']],                     # a blank row in the middle
    [['"', '""', '"quoted"']],                # quotes only
    [['Main, Floor 2']],                      # a comma stays in its cell
]


@pytest.mark.parametrize('rows', ROUND_TRIPS, ids=[json.dumps(r) for r in ROUND_TRIPS])
def test_what_the_sheet_copies_pastes_back_as_the_same_cells(table_text, rows):
    assert table_text('m.parseTable(m.toTable(input),true)', rows) == rows


@pytest.mark.parametrize('text,cells', [
    ('""', [['']]),                           # a quoted-empty last cell is a cell (it clears its destination)
    ('a\t""', [['a', '']]),
    ('111\n\n333\n', [['111'], [''], ['333']]),
    ('a\r\nb\r\n', [['a'], ['b']]),
    ('"North\nSouth"\tx\n', [['North\nSouth', 'x']]),
    ('Main, Floor 2', [['Main, Floor 2']]),
    ('', []),
])
def test_a_grid_paste_keeps_its_shape(table_text, text, cells):
    assert table_text('m.parseTable(input,true)', text) == cells


def test_the_matching_dialog_still_reads_csv_and_drops_blank_lines(table_text):
    assert table_text('m.parseTable(input)', 'Name,Clinic\n\nAlex,"North, East"\n') == [['Name', 'Clinic'], ['Alex', 'North, East']]


@pytest.mark.parametrize('values', [['#student'], ['#student', '#North, East'], ['say "hi"', 'a;b'], []])
def test_several_values_in_one_cell_read_back_as_written(table_text, values):
    assert table_text('m.splitValues(m.joinValues(input))', values) == values


def test_a_cell_of_values_survives_the_clipboard_too(table_text):
    cell = table_text('m.joinValues(input)', ['#student', '#North, East'])
    assert table_text('m.splitValues(m.parseTable(m.toTable([[input]]),true)[0][0])', cell) == ['#student', '#North, East']


def test_a_hash_named_tag_label_survives_the_clipboard(table_text):
    labels = ['##student', '#student', '#North, East']
    cell = table_text('m.joinValues(input)', labels)
    assert table_text('m.splitValues(m.parseTable(m.toTable([[input]]),true)[0][0])', cell) == labels
