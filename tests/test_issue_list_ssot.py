"""One issue list, one Needs-you row, one session-chip model, one rota health .

Source scans, like the other web guards: an issue list, its card and its session chips are built only in
issue_marker.ts (issueList, sessionChips, findingIssue, the one Issue type); the Needs-you row's words and count pill
only in dom.ts (needText, needCount); Must fix and Could improve in the footer only through fix_list.ts fixPills. A
second copy anywhere else fails here, with where it is."""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / 'apps/web/src'


def found(pattern, allowed):
    """Files (other than `allowed`) whose source matches `pattern`, with the first matching line number."""
    out = []
    for path in sorted(SRC.glob('*.ts')):
        if path.name in allowed or path.name == 'generated.ts':
            continue
        text = path.read_text(encoding='utf-8')
        m = re.search(pattern, text)
        if m:
            out.append(f'{path.name}:{text.count(chr(10), 0, m.start()) + 1}')
    return out


def test_issue_lists_and_cards_are_built_only_by_issue_marker():
    for pattern in (r"class:'issue-list'", r"\bissue-card\b", r"\bissueCard\s*\(", r"class:'iss-lines'", r"class:'iss-row"):
        assert not found(pattern, {'issue_marker.ts'}), f'{pattern}: build issue lists with issue_marker.ts issueList: {found(pattern, {"issue_marker.ts"})}'


def test_one_issue_type_and_one_finding_to_issue():
    assert not found(r'(?:function|const)\s+findingIssue\b', {'issue_marker.ts'}), 'a second findingIssue: use issue_marker.ts findingIssue'
    # Other files may name their own unrelated rows "Issue" (bulk changes, the rule catalogue): only a card-shaped one fails.
    assert not found(r'(?:interface|type)\s+Issue\s*=?\s*\{\s*tone\s*:', {'issue_marker.ts'}), 'a second issue card type: use issue_marker.ts Issue'


def test_session_chips_are_built_only_by_issue_marker():
    for pattern in (r"class:'fix-chip", r"class:`fix-chip", r"class:'iss-chip"):
        assert not found(pattern, {'issue_marker.ts'}), f'{pattern}: draw session chips with issue_marker.ts sessionChips: {found(pattern, {"issue_marker.ts"})}'


def test_the_needs_you_row_is_built_only_by_dom():
    for pattern in (r"home-need-text", r"home-count"):
        assert not found(pattern, {'dom.ts'}), f'{pattern}: use dom.ts needText / needCount: {found(pattern, {"dom.ts"})}'


def test_no_second_issue_mark():
    # an issue on a chip or cell is the shared issueMarker / issueGlyph (Must fix, Could improve), never a
    # "!" chip of its own or a key entry or state worded "Rule issue".
    for pattern in (r"pg-chip issue", r"cov-key issue", r"['`]Rule issue['`]", r"Rule issue or double booking", r"rule issue: "):
        assert not found(pattern, set()), f'{pattern}: use issue_marker.ts issueMarker / issueGlyph and SEVERITY_WORDS: {found(pattern, set())}'


def test_one_rota_health_for_draft_and_published():
    # The published footer's Must fix and Could improve come from fixPills: no bespoke "need staff" or blue Could improve.
    assert not found(r"need\$\{gaps===1\?'s':''\} staff", set()), 'a separate "need staff" health pill: count open places as Must fix in fixPills'
    assert not found(r"healthPill\([^)]*'info','help'", set()), 'a blue Could improve pill: Could improve is fixPills\' gold circle'
    assert 'fixPills(' in (SRC / 'app.ts').read_text(encoding='utf-8')


def test_popovers_and_notes_list_issues_only_through_issue_list():
    # UIUX live check F3/F6: the footer's Rota health, the published could-improve list and the auto-schedule results'
    # notes are the one issue list: no third variant (a cause table, a notes list, one note per issue).
    for pattern, why in ((r"id:'rota-warnings'", 'the published warnings table'),
                         (r"others\.map\(issue=>note\(", "one note per run issue (Resolve these first)"),
                         (r"class:'why-list'\},\.\.\.\(r\.warnings", "a run's warnings as a bare list"),
                         (r"severity\+'\\u0000'\+", 'issues grouped by severity and words (group by identity: sameIssue)')):
        assert not found(pattern, set()), f'{why}: use issue_marker.ts issueList: {found(pattern, set())}'
    fix = (SRC / 'fix_list.ts').read_text(encoding='utf-8')
    head = fix[fix.index('function fixHead('):]
    head = head[:head.index('\n}\n')]
    assert 'issueList(' in head and 'fixLevel(' not in head, 'the footer popover lists causes through issueList (causeIssues)'


def test_the_needs_you_row_container_is_built_only_by_dom():
    # count (or mark), words, then action, by construction (dom.ts needRow).
    assert not found(r"class:'fix-row'", {'dom.ts'}), f"build Needs-you rows with dom.ts needRow: {found(r'class:.fix-row.', {'dom.ts'})}"
