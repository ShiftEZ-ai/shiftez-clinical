"""Per-account reads in the web client are forgotten on sign-out or account switch (review #571 item 3).

Every module keeps its account-specific memory in module state; api.ts clearSessionState() runs each module's
onSessionReset callback. This checks the known per-account caches are registered there, so a new sign-in never draws
the previous account's data (the Access read was not)."""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "apps" / "web" / "src"

# module -> caches its onSessionReset callbacks must forget
CACHES = {
    "app.ts": ["keptAccess", "accessRead", "academicCache", "liveChecks", "pillCounts", "leaveRead", "draftChoice", "baselineDriftCache"],
    "baseline_drift.ts": ["seenInputs"],  # its epoch is a counter: keys from a past account are cleared with baselineDriftCache
    "planner_week.ts": ["ruleCache"],  # held fill asks: asks.reset(), guarded in test_web_ssot_guards.py
    "planner_why.ts": ["cache"],
    "planner_people.ts": ["leaveCache"],
    "person_timeline.ts": ["available"],
    "requests.ts": ["choiceNames"],
    "rule_catalogue.ts": ["proposedDrafts", "pendingLines", "schema", "drafts", "checked"],
}

# module -> caches its reset forgets through a helper another module owns: (helper module, helper, caches)
FORGETTERS = {
    "app.ts": [("person_label.ts", "forgetNames", ["codeCache", "levelCache"])],
}


def resets(text):
    """What follows every onSessionReset( call in a module (its callback, which is short), joined."""
    return " ".join(text[m.end():m.end() + 300] for m in re.finditer(r"onSessionReset\(", text))


def test_per_account_caches_are_forgotten_on_session_reset():
    for module, names in CACHES.items():
        body = resets((SRC / module).read_text(encoding="utf-8"))
        missing = [n for n in names if not re.search(rf"\b{n}(\s*=\s*null|\.clear\(\))", body)]
        assert not missing, f"{module}: not forgotten on session reset: {missing}"
    for module, helpers in FORGETTERS.items():
        body = resets((SRC / module).read_text(encoding="utf-8"))
        for owner, helper, names in helpers:
            assert f"{helper}()" in body, f"{module}: session reset does not call {helper}()"
            src = (SRC / owner).read_text(encoding="utf-8")
            definition = src[src.index(f"const {helper}="):][:300].split(chr(10), 1)[0]
            missing = [n for n in names if not re.search(rf"\b{n}\s*=\s*null", definition)]
            assert not missing, f"{owner}: {helper}() does not forget {missing}"


def test_sign_in_screen_clears_the_session():
    app = (SRC / "app.ts").read_text(encoding="utf-8")
    show_login = app[app.index("async function showLogin("):]
    assert "forgetSession()" in show_login[:show_login.index("\nasync function ", 1)]
    forget = app[app.index("function forgetSession("):]
    assert "clearSessionState()" in forget[:forget.index("\n/**")]
