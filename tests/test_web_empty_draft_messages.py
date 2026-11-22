"""A department with no recurring sessions: a new rota needs no recurring-session choice, and auto-schedule on an empty
draft says it has no sessions, never that every session is locked. In source: no browser."""
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / 'apps/web/src'


def test_use_all_recurring_sessions_never_asks_for_a_choice():
    forms = (WEB / 'forms.ts').read_text(encoding='utf-8')
    # Only a hand-picked, empty list asks for a recurring session; "Use all" with none active creates an empty rota.
    assert ("setup.input.value==='patterns'&&!allTemplates.input.checked&&!templates.values().length)"
            "throw new Error('Choose at least one recurring session.')") in forms
    assert "&&!templateIds().length)throw" not in forms


def test_run_reason_on_an_empty_draft_says_it_has_no_sessions():
    app = (WEB / 'app.ts').read_text(encoding='utf-8')
    reason = app[app.index('const runReason=()=>'):app.index('const syncRun=()=>')]
    assert ("live.length?'Every session is locked, so there is nothing to fill.'"
            ":'This draft has no sessions, so there is nothing to fill.'") in reason
