"""LEAD-QA WCAG pass 2 (v11) fixes, fast and non-browser: the shared form cue rule and server refusal words run in Node
from the compiled client; source guards keep each fix in its one helper (dom.ts)."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / 'apps/web/src'
DOM = (SRC / 'dom.ts').read_text(encoding='utf-8')


@pytest.fixture(scope='module')
def run(client_modules):
    node, module = shutil.which('node'), (client_modules / 'dom.js').as_uri()

    def call(expr, *args):
        script = f"const d=await import({json.dumps(module)});const a=JSON.parse(process.argv[1]);console.log(JSON.stringify({expr}));"
        out = subprocess.run([node, '--input-type=module', '-e', script, json.dumps(args)], capture_output=True, text=True, check=True, timeout=30)
        return json.loads(out.stdout)
    return call


@pytest.mark.parametrize('required,optional,cue', [
    (0, 0, None), (0, 5, None),            # nothing required: nothing to mark
    (8, 3, 'optional'), (3, 1, 'optional'),  # most are required: the few optional ones say so
    (4, 0, 'required'), (1, 0, 'required'),  # all required: they say so (there is no optional one to mark)
    (2, 2, 'required'), (1, 5, 'required'),  # a tie or most optional: the required ones say so
])
def test_the_fewer_kind_of_field_is_marked(run, required, optional, cue):
    assert run('d.fieldCue(a[0],a[1])', required, optional) == cue


def test_a_schema_refusal_says_the_servers_field_messages(run):
    refusal = {'code': 'SCHEMA_INVALID', 'message': 'Review the highlighted fields',
               'details': [{'field': 'body.payload.maximum_duration_minutes', 'message': 'Input should be greater than or equal to 15'},
                           {'field': 'body.payload.other', 'message': 'Input should be greater than or equal to 15'}]}
    assert run('d.refusalWords(a[0])', refusal) == 'Input should be greater than or equal to 15'
    assert run('d.refusalWords(a[0])', {'code': 'VALUE_INVALID', 'message': 'Name is taken'}) == 'Name is taken'
    assert run('d.refusalWords(a[0])', {**refusal, 'details': []}) == 'Review the highlighted fields'
    # A model check's sentence is said without the validation library's prefix, as api.ts says it (plainWords).
    model = {**refusal, 'details': [{'field': 'body.payload', 'code': 'value_error',
                                     'message': 'Value error, Opening windows must be ordered within one day'}]}
    assert run('d.refusalWords(a[0])', model) == 'Opening windows must be ordered within one day'
    # D-069: a field is named by its title, never its key or path; an unnamed field says the sentence alone.
    timed = {**refusal, 'details': [{'field': 'body.payload.options.search_minutes', 'code': 'greater_than_equal',
                                     'message': 'Input should be greater than or equal to 1'},
                                    {'field': 'body.payload.unknown_key', 'code': 'value_error', 'message': 'Value error, Too many'}]}
    said = run('d.refusalWords(a[0])', timed)
    assert said == 'Search time: Input should be greater than or equal to 1 · Too many', said
    assert 'search_minutes' not in said and 'unknown_key' not in said


def test_forms_mark_fields_only_through_the_shared_helper():
    """maintenance: one marking rule. No asterisk span and no second copy of the label sweep outside dom.ts."""
    bad = [p.name for p in SRC.glob('*.ts') if 'required-mark' in p.read_text(encoding='utf-8')]
    bad += [p.name for p in SRC.glob('*.ts') if p.name != 'dom.ts' and re.search(r":required'\)\)\s*\w+\.append\(", p.read_text(encoding='utf-8'))]
    assert not bad, 'Mark required/optional fields with markFields (dom.ts):\n' + '\n'.join(bad)
    assert 'markFields(form)' in DOM[DOM.index('export function formInto('):DOM.index('function buildSteps(')]


def test_wrong_values_are_marked_on_leaving_and_said_on_save():
    """maintenance: the one error watcher marks a value left wrong on focusout and says Save's first problem politely."""
    watcher = DOM[DOM.index('function watchErrors()'):DOM.index('const problemHost')]
    assert "addEventListener('focusout'" in watcher and 'valueMissing' in watcher and 'showProblem(c)' in watcher
    assert re.search(r'focusProblem\(f\);\s*announce\([^;]*,\s*true\)', watcher)


def test_every_cut_text_class_shows_its_full_text():
    """maintenance (WCAG 1.4.12): the shared tooltip shows what .truncate and .clamp-2 cut; no screen needs its own title."""
    assert re.search(r"CUT_TEXT\s*=\s*'\.truncate,\.clamp-2'", DOM)
    hover = DOM[DOM.index('function hoverContent('):DOM.index('function covered(')]
    assert 'n.matches(CUT_TEXT)' in hover and 'scrollWidth' in hover


def test_no_control_promises_a_dialog_it_does_not_open():
    """maintenance: the waiting proposal's button opens the effect review, so it says so; the decision is the review's own."""
    text = (SRC / 'rule_catalogue.ts').read_text(encoding='utf-8')
    assert 'Approve and activate…' not in text
    assert "button('Review and approve',review," in text
