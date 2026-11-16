"""maintenance: a rule notice's words and address come from one function, routes.ts ruleNotice, run here in Node from the
compiled client: the rule's name from the policies the client holds, the Rules page's open-a-rule address, and a
neutral "a rule" (never a raw id) when the rule is not held."""
import json
import shutil
import subprocess

import pytest


@pytest.fixture(scope='module')
def notice(client_modules):
    node = shutil.which('node')
    module = (client_modules / 'routes.js').as_uri()

    def run(policy_id, held, sent=None):
        script = (f"const r=await import({json.dumps(module)});const [id,held,sent]=JSON.parse(process.argv[1]);"
                  "console.log(JSON.stringify(r.ruleNotice(id??undefined,x=>held[x],sent??undefined)));")
        out = subprocess.run([node, '--input-type=module', '-e', script, json.dumps([policy_id, held, sent])],
                             capture_output=True, text=True, check=True, timeout=30)
        return json.loads(out.stdout)
    return run


def test_a_held_rule_is_named_and_opens_its_rule_page(notice):
    held = {'p-1': {'name': 'Minimum rest'}}
    assert notice('p-1', held) == {'text': 'A proposal or decision on Minimum rest', 'hash': '#rules?source=p-1'}


def test_the_current_name_wins_over_the_name_the_notice_was_sent_with(notice):
    assert notice('p-1', {'p-1': {'name': 'Minimum rest'}}, 'Old rest name')['text'] == 'A proposal or decision on Minimum rest'
    # Retired or not held: the name the server sent, and the rule's own address (the server checks access there).
    assert notice('p-2', {}, 'Night cap') == {'text': 'A proposal or decision on Night cap', 'hash': '#rules?source=p-2'}


def test_markup_in_a_name_is_kept_as_words(notice):
    # The item renders the text with el() (textContent), so a name is never markup; the function passes it through.
    assert notice('p-3', {}, '<b>x</b>')['text'] == 'A proposal or decision on <b>x</b>'


def test_a_rule_without_a_name_is_a_rule_and_never_shows_its_id(notice):
    assert notice('p-gone', {}) == {'text': 'A proposal or decision on a rule', 'hash': '#rules?source=p-gone'}
    assert notice('p-blank', {'p-blank': {'name': '  '}}, ' ')['text'] == 'A proposal or decision on a rule'
    assert notice('p-blank', {'p-blank': {'name': '  '}}, 'Night cap')['text'] == 'A proposal or decision on Night cap'
    assert notice(None, {}) == {'text': 'A proposal or decision on a rule', 'hash': '#rules'}
