"""One verb and one name per thing (UX-11, UX-17): "New <thing>" starts a record everywhere ("Invite" only for sign-in
accounts), and the Rules page is called Rules whichever rules mode the department uses."""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / 'apps/web/src'


def source(name):
    return (SRC / name).read_text(encoding='utf-8')


def test_primary_verbs_consistent():
    everything = {p.name: p.read_text(encoding='utf-8') for p in SRC.glob('*.ts') if p.name != 'generated.ts'}
    # Starting a record reads "New …": no "Create <record>" or "Add person" button or form title is left.
    nouns = re.findall(r"singular:'([^']+)'", source('config_kinds.ts'))
    stale = [(f, m.group(0)) for f, text in everything.items() for m in re.finditer(r"['`](?:Create|Add) (" + '|'.join(map(re.escape, nouns)) + r")['`]", text)]
    stale = [x for x in stale if not x[1].startswith("'Add ") or x[1] == "'Add person'"]  # "Add <line>" stays for lines inside a record
    assert not stale, stale
    assert "'New '+singularOf(kind)" in source('config_kinds.ts')
    assert "`${row?'Edit':'New'} ${titleNames[kind]||kind}`" in source('forms.ts')
    assert "'Invite person'" in source('app.ts')  # accounts are invited


# One vocabulary (DESIGN.md › Fix list, Words; UIUX-AUDIT-CODE C15, C27): the Fix list's levels are "Must fix" and
# "Could improve" (issue_marker.ts SEVERITY_WORDS), open places are "open places", the list is "Fix list", a rota slot is
# a "session". Each phrase below is a synonym a reader once met for one of them. Checked in prose (a literal with a
# space), so keys, classes and keyboard names ("Shift+F10") pass.
BANNED = {
    'Blocking (say Must fix)': r'(?<![\w-])[Bb]locking(?![\w-])',
    'Rule issue / Rule broken (say Must fix)': r'(?i)\brule\s+(?:issues?|broken)\b',
    'Needs review (say Must fix)': r'(?i)\bneeds\s+review\b',
    'need staff (say open places)': r'(?i)\bneeds?\s+staff\b',
    'fix list (say Fix list)': r'(?<![\w-])fix\s+list\b',
    'shift (say session)': r'(?<![\w.?&/-])shifts?(?![\w=/-])',
}
# A ratchet: (file, phrase) → count still allowed while another lane owns the line. It may only shrink.
ALLOWED = {
    ('fix_list.ts', 'fix list (say Fix list)'): 2,  # FIXLIST-UX: two search/foot labels remain until the next wording pass
    ('app.ts', 'shift (say session)'): 1,  # a search synonym: typing "shift" still finds Requests
}


def test_no_banned_synonyms_in_user_text():
    from .web_strings import web_literals
    found = {}
    for name, values in web_literals().items():
        for line, value in values:
            if ' ' not in value.strip() or re.search(r'^[/#\[.]|data-rk|/api/', value.strip()):
                continue  # not prose: a path, a selector
            for word, pattern in BANNED.items():
                if re.search(pattern, value):
                    found.setdefault((name, word), []).append((line, value.strip()[:80]))
    grown = {k: v for k, v in found.items() if len(v) > ALLOWED.get(k, 0)}
    stale = {k: n for k, n in ALLOWED.items() if len(found.get(k, [])) < n}
    assert not grown, grown
    assert not stale, f'fixed: lower the allowance {stale}'


def test_audit_words_never_show_a_raw_action():
    # QA-060 (the QA-051 rule): a history chip and sentence come from the contract's AUDIT_ACTION_CATALOGUE, else the
    # client's AUDIT_NOUN / AUDIT_VERB beside the audit view; an action neither knows reads "Other · changed a record".
    import json
    app, gen = source('app.ts'), source('generated.ts')
    catalogue = json.loads(re.search(r'export const AUDIT_ACTION_CATALOGUE = (\{.*?\}) as const;', gen).group(1))
    assert all(w[k].strip() for w in catalogue.values() for k in ('noun', 'plural', 'verb'))
    words = app[app.index('function auditWords'):app.index('const AUDIT_GROUPS')]
    assert 'AUDIT_ACTION_CATALOGUE' in words and "replaceAll('_',' ')" not in words and "rest.join(' ')" not in words
    assert 'const {verb,what,chip,colour}=auditWords(e.action,n,e)' in app  # the audit log's chip is the same one
    # Every action the server writes has words, so none reaches a chip as a lowercase kind ("account", "command").
    nouns = set(re.findall(r"(\w+):\['", app[app.index('const AUDIT_NOUN'):app.index('const AUDIT_VERB')]))
    api = SRC.parents[1] / 'api'
    written = {a for p in api.rglob('*.py') for a in re.findall(r"""\baction\s*=\s*['"]([a-z_]+(?:\.[a-z_]+)+)['"]""", p.read_text(encoding='utf-8'))}
    assert written, 'no audit actions found: the pattern no longer matches the server'
    written.add('command.refused')  # command_refusals.py writes it behind a conditional the pattern does not read
    assert {a for a in written if a not in catalogue and a.split('.')[0] not in nouns} == set()


def test_rules_page_has_one_name():
    app = source('app.ts')
    assert 'Rules & requirements' not in app
    assert re.search(r"rules:\{title:'Rules'", app)
