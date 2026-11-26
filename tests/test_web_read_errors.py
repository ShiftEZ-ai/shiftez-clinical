"""DL-045: every failed read says it in one way, dom.ts readFailed()/couldntLoad(): "Couldn’t load <its name>." with the
server's message and Retry, or the timeout's "Didn’t load in 1:00.". No screen writes its own read-error words."""
import json
import re
import subprocess
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / 'apps/web/src'

# Read-error wording in either apostrophe, any case: the variants hand-made notes used.
WORDS = re.compile(r"did(?:n['’]t| not) load|could(?:n['’]t| not) (?:load|be loaded)|failed to load"
                   r"|this section", re.I)
# The only places the words are written: the builder, the wait words and start-up's pinned copy (before any module).
ALLOWED = {
    'dom.ts': ['Couldn’t load ${name}.'],
    'loading.ts': ["timedOut:'Didn’t load in'"],
    'startup.ts': ["TIMED_OUT_WORDS = 'Didn’t load in'"],
}


def _code(text):
    """The source without comments (a comment may describe the words); `//` inside a URL string is kept."""
    text = re.sub(r'/\*[\s\S]*?\*/', '', text)
    return re.sub(r'(?:^|(?<=[\s;{})]))//[^\n]*', '', text, flags=re.M)


def _found(name, text):
    code = _code(text)
    for line in code.splitlines():
        if WORDS.search(line) and not any(a in line for a in ALLOWED.get(name, [])):
            yield line.strip()[:160]


def test_read_errors_use_the_one_builder():
    found = {p.name: lines for p in sorted(SRC.glob('*.ts')) if (lines := list(_found(p.name, p.read_text(encoding='utf-8'))))}
    assert not found, ('A failed read is dom.ts readFailed(name, e, retry) (a toast: couldntLoad(name, e)), never its own '
                       f'words (DESIGN.md › Loading, DL-045): {found}')


def test_the_allowed_words_are_still_there():
    # A renamed builder must not leave the allowance pointing at nothing.
    for name, needles in ALLOWED.items():
        code = _code((SRC / name).read_text(encoding='utf-8'))
        assert all(n in code for n in needles), name


def test_the_guard_sees_the_variants():
    for line in ("note('The rotation board could not be loaded. '+m,'error',e)", "readFailed('This section didn’t load.',e)",
                 "notify(`Couldn't load the rota's checks.`)", "el('p',{},'Details could not be loaded')",
                 "'Some results failed to load.'", "note(`History did not load: ${m}`,'warning')",
                 "note('Rota scores couldn’t load, so …')"):
        assert WORDS.search(line), line
    for line in ("readFailed('the score review',e,load)", "couldntLoad('the inbox',e)", "'Still loading'"):
        assert not WORDS.search(line), line


def test_the_words_show_a_server_sentence_never_a_bare_status(client_modules):
    """A server's problem detail or the client's connection words follow "Couldn’t load"; a bare status phrase, a code
    or one word is dropped (DESIGN.md › Interaction contract)."""
    source = (f"import assert from 'node:assert/strict';const d=await import({json.dumps((client_modules / 'dom.js').as_uri())});"
              """const w=m=>d.couldntLoad('the rotas',new Error(m));
for(const m of ['Service unavailable','Service Unavailable.','Bad gateway','Internal Server Error','Gateway Timeout',
  'unavailable','SCORE_ISOLATION_UNAVAILABLE','Down',''])assert.equal(w(m),'Couldn’t load the rotas.',m);
for(const m of ['The server is busy.','Connection lost. The server could not be reached.',
  'The service had a problem. Try again in a moment.'])assert.equal(w(m),'Couldn’t load the rotas. '+m,m);
assert.equal(d.couldntLoad('the rotas'),'Couldn’t load the rotas.');
assert.equal(d.couldntLoad('the rotas','Service unavailable'),'Couldn’t load the rotas.');""")
    done = subprocess.run(['node', '--input-type=module', '-e', source], capture_output=True, text=True, timeout=15)
    assert done.returncode == 0, done.stderr
