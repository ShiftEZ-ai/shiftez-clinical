"""maintenance and maintenance guards (fast, no browser).

maintenance: the bell said 525 unread while the inbox menu said 100 (it counted the rows of its one page). The bell, its name
and tooltip, the menu's header and the inbox page's counts all read the server's unread total through one function,
`unreadCount` (notifications.ts).

maintenance: while a Configuration section loaded, the whole page kept on screen was inert, its section nav too, so pointer
clicks on Names & codes did nothing. Only the section left (its pane) may be inert."""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / 'apps/web/src'
APP = (SRC / 'app.ts').read_text(encoding='utf-8')


def body(name):
    """The source of one top-level function in app.ts, up to the next top-level declaration."""
    start = re.search(rf'^(?:async )?function {name}\(', APP, re.M)
    assert start, name
    end = re.compile(r'^(?:async function|function|const|let|/\*\*)', re.M).search(APP, start.end())
    return APP[start.start():end.start() if end else None]


def test_bell_and_inbox_header_read_one_count():
    notifications = (SRC / 'notifications.ts').read_text(encoding='utf-8')
    assert re.search(r"export const unreadCount=\(n:number\)=>\(\{text:n>99\?'99\+'", notifications)
    bell, menu, page = body('showUnread'), body('notificationsMenu'), body('inboxPage')
    assert 'navBadges.inbox=unreadCount(' in bell
    assert 'showUnread(' in body('inboxPulse')
    # The menu's header and the bell read the same value: the server's unread_total, through unreadCount.
    assert 'unread_total' in menu and 'showUnread(left)' in menu and 'unreadCount(left).label' in menu
    assert 'unread_total' in page
    # The bell's badge before the first pulse reads the server's total too, never a page of rows.
    assert "next.inbox=unreadCount(" in APP
    # No second way of saying it: no count of unread rows, no copied 99+ cap or "N unread" words for the inbox.
    assert not re.search(r'filter\(\w+=>!noteRead\(\w+\)\)(?:\.length)?', APP)
    assert not re.search(r"navBadges\.inbox=\{", APP)
    assert not re.search(r"`\$\{(?:unread|left|n)(?:\.length)?\} unread`", APP)


def test_rule_notice_text_and_address_come_from_one_function():
    # The contract: link.policy_id and link.policy_name (RULES-CONFIG, maintenance); no other place to read them from.
    assert "const ruleNoticeOf=(n:Row)=>ruleNotice(n.link?.policy_id,id=>find('policies',id),n.link?.policy_name);" in APP
    assert "n.kind==='rule'?ruleNoticeOf(n).text" in APP          # the item's title
    assert "navigate(ruleNoticeOf(n).hash)" in body('openNoteTarget')  # where it leads


def test_bell_shows_the_capped_number():
    assert "el('span',{class:'count','aria-hidden':'true'},n.text)" in body('applyNavBadges')


def test_configuration_nav_is_never_inert_while_a_section_loads():
    render = body('renderPage')
    assert 'body.inert=waited' not in render
    assert 'staleShown(body).inert=waited' in render
    assert re.search(r"const staleShown=\(kept:HTMLElement\)=>kept\.querySelector<HTMLElement>\('\.config-pane'\)\|\|kept;", APP)


def test_person_label_switch_sits_with_the_names_default():
    names = body('namesPage')
    # The switch is in the top panel for an admin, not in a section after the people list.
    assert "isAdmin?personLabelsPanel():'')," in names
    assert "el('h3',{},'Person label'),personLabelsPanel())" not in names
