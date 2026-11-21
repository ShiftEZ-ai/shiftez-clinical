"""The legend guard reviewed.

Every mark a view draws goes through the one key component (apps/web/src/dom.ts `legend().mark(node, …)`), which
stamps the drawn element with `data-lg="<key id>:<entry>"` and adds that entry; each entry is an `li[data-lg-entry]`.
`assert_legend_matches` renders nothing itself: a view's browser test draws the view with data that produces its marks,
then calls it. It checks, for the view's root:

1. every stamped mark on screen has its entry in the visible key or its overflow popover, and every entry has a mark
   on screen (a key lists nothing that is not drawn);
2. no unstamped mark: an element of the shared mark vocabulary (`MARKS`) outside any stamped mark is a mark drawn
   without its key, so a new view or mark that skips the key fails here.

`scrollers`: virtualised containers (the timeline) are scrolled through so every laid-out row is seen once.
"""
from contextlib import contextmanager

from playwright.sync_api import Page, expect

# The shared mark vocabulary: every element drawn in a meaning-carrying hue, pattern or glyph. A drawn element of one of
# these, or inside a stamped mark, is covered; one that is neither is a mark without its key. `data-lg-self` (rare,
# explicit) marks an element whose own visible words are its whole meaning (a tag chip "#Name"), never a pattern.
MARKS = ", ".join([
    # duty, area, rank and tag hues (chips, pills, bars, swatches): a class that *starts* with duty-, so a structural
    # name that only contains it (home-duty-list, home-duty-more) is not taken for a hue.
    "[class^='duty-']", "[class*=' duty-']",
    ".iss-mark",                   # the issue marker (must fix, could improve)
    ".tl-bar", ".tl-zone",         # timeline stretches and conflict zones
    ".tl-heatmap",                 # duty density
    ".ra-strip > i",               # Who's available strip marks
    ".sd-dot",                     # Year dots
    ".wk-tag", ".clash-tag",       # chip tags (TL, OT) and double booking
    ".pchip.absent", ".pchip.gap", ".pchip.proposed-add", ".pchip.proposed-remove", ".sd-greychip",
    ".pg-chip", ".pg-leave", ".pg-wish", ".pg-bar.over",
    ".cov-cell", ".wk-wish", ".team-box", ".team-tree",
    ".sd-yday.today", ".sd-yday.has-date-mark",  # Year's today ring and a named date's underline
    ".fix-mark.must", ".fix-mark.could",  # a cause's level (fix_list.ts fixRow): the issue entries
    ".rk-mark", ".rk-rules .rk-dot",  # the rule map's exception fork on any view, a rule row's state dot
    # A schedule view's weekend and today shading, and the agenda's open places (live check 6cea25859 found them unkeyed).
    ".pg-day.weekend", ".pg-day.today", ".pg-cell.weekend", ".pg-cell.today", ".wk-day.weekend", ".wk-cell.weekend",
    ".coverage-table .weekend", ".ag-open",
    ".pg-cell.leave", ".pg-cell.leave-soft",  # People × Days and codes grid: a day of leave (hatched or dashed cell)
    ".sg-count.short",             # Schedule grid: a count cell red when short of staff
    ".tl-more",                    # Schedule grid: "+N" more sessions in a cell
    ".pg-breach",                  # People × Days: a breach of the person's own rule
    ".wk-lock",                    # Week/Month: a locked session's lock
    ".clash-mark",                 # Week/Month: a session's double-booked mark
    ".wk-wish-mark",               # Week/Month: a chip's wish star (grants, goes against)
    ".pchip.others",               # Week/Month: "+N" others a person display leaves out
    ".rc-level", ".rc-inh",        # rule views: a number's strength badge and an inherited number (↰)
    ".rc-pills .rp:is(.person, .group, .place):not(.except)",  # rule views: a who or where pill's hue
    ".rk-list :is(.rk-dot, .rk-mark, .rk-lockmark)",  # the Planner’s rules panel: a rule’s state dot, safety lock and an exception’s mark
    ".wk-day.sd-ctx",              # Week/Month: a day outside the draft's dates (one grey, "Outside this rota")
])

_COLLECT = """([root, marks]) => {
  const r = document.querySelector(root); if (!r) return null;
  const shown = n => n.checkVisibility ? n.checkVisibility({checkVisibilityCSS: true}) : !!(n.offsetWidth || n.offsetHeight || n.getClientRects().length);
  // A key's own swatches, and cards or popovers opened over the view, are not the view's marks (the view may be a panel).
  const away = n => { const a = n.closest('.view-legend, [role=tooltip], .hovercard, [popover], dialog, .tl-card'); return !!a && a !== r && r.contains(a); };
  const stamped = [...r.querySelectorAll('[data-lg]')].filter(n => shown(n) && !away(n)).flatMap(n => n.getAttribute('data-lg').split(' ').filter(Boolean));
  const keys = [...r.querySelectorAll('.view-legend:not([hidden])')].filter(shown);
  const entries = keys.flatMap(k => [...k.querySelectorAll('li[data-lg-entry]')].map(n => n.getAttribute('data-lg-entry')));
  const bare = [...r.querySelectorAll(marks)].filter(n => shown(n) && !away(n) && !n.closest('[data-lg], [data-lg-self]'))
    .map(n => n.outerHTML.slice(0, 160));
  // The key stays on one line without scrolling; hidden entries must be usable through its overflow popover.
  const clipped = keys.filter(k => k.scrollWidth > k.clientWidth + 1 || k.scrollHeight > k.clientHeight + 1).map(k => k.getAttribute('data-lg-key'));
  // Only an overlong first label may be ellipsised; the popover below must expose its full words and swatch.
  const cut = keys.flatMap(k => [...k.querySelectorAll('li[data-lg-entry]')].filter(shown).filter(li => li.getBoundingClientRect().right > k.getBoundingClientRect().right + 1
    || [...li.querySelectorAll('.lg-words, .lg-act, .truncate')].some(n => n.scrollWidth > n.clientWidth + 1) && !(li === k.firstElementChild && k.querySelector('.lg-more')))
    .map(li => li.getAttribute('data-lg-entry') + ' ' + JSON.stringify(li.textContent.trim().slice(0, 60))));
  // A swatch never overlaps its entry's words: its painted extent (its box and everything in it, which may overflow a
  // collapsed box) ends before the words start on the same line (live 12dc48a22: the team swatch over "Supervision").
  const overlaps = keys.flatMap(k => [...k.querySelectorAll('li[data-lg-entry]')].filter(shown).filter(li => {
    const words = li.querySelector('.lg-words'), entry = li.querySelector(':scope > .lg-act') || li, sw = entry.firstElementChild;
    if (!words || !sw || sw === words) return false;
    const w = words.getBoundingClientRect(), parts = [sw, ...sw.querySelectorAll('*')].map(n => n.getBoundingClientRect()).filter(r => r.width || r.height);
    return parts.some(r => r.right > w.left + .5 && r.left < w.right && r.top < w.bottom && r.bottom > w.top);
  }).map(li => li.getAttribute('data-lg-entry') + ' ' + JSON.stringify(li.textContent.trim().slice(0, 60))));
  // An exemption names itself: a data-lg-self element says its meaning in its own words.
  const mute = [...r.querySelectorAll('[data-lg-self]')].filter(n => shown(n) && !n.textContent.trim()).map(n => n.outerHTML.slice(0, 160));
  return {stamped, entries, bare, clipped, cut, overlaps, mute, keys: keys.map(k => k.getAttribute('data-lg-key'))};
}"""

_FRAME = "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"


@contextmanager
def legend_entries(page: Page, key):
    """Entries a reader can use: the visible row plus every item in its +N popover, with the same words and symbols."""
    expect(key).to_be_visible()
    page.evaluate(_FRAME)
    assert key.evaluate("k => getComputedStyle(k).flexWrap === 'nowrap' && k.scrollWidth <= k.clientWidth + 1 && k.scrollHeight <= k.clientHeight + 1")
    assert key.locator(':scope > li:visible').evaluate_all("ns => new Set(ns.map(n => {const r=n.getBoundingClientRect();return Math.round((r.top+r.bottom)/2)})).size === 1")
    assert key.locator(':scope > li:visible').evaluate_all("""ns => ns.every(n => {
        const r=n.getBoundingClientRect(), k=n.parentElement.getBoundingClientRect();
        return r.left>=k.left-1 && r.right<=k.right+1 && r.top>=k.top-1 && r.bottom<=k.bottom+1 && r.right<=innerWidth+1;
    })""")
    entries = key.locator(':scope > li[data-lg-entry]')
    visible = key.locator(':scope > li[data-lg-entry]:visible')
    expected = entries.evaluate_all('ns => ns.map(n => n.innerHTML)')
    shown = visible.evaluate_all('ns => ns.map(n => n.innerHTML)')
    more = key.locator('.lg-more > button')
    if not more.count():
        assert shown == expected
        yield visible
        return
    hidden = len(expected) - len(shown)
    expect(more).to_have_text(f'+{hidden}')
    noun = 'legend item' if hidden == 1 else 'legend items'
    expect(more).to_have_attribute('aria-label', f'{hidden} more {noun}: show all {len(expected)}')
    label = key.get_attribute('aria-label')
    more.click()
    panel = page.get_by_role('dialog', name=f'All {label}', exact=True)
    expect(panel).to_be_visible()
    overflow = panel.locator('ol > li > .lg-pop')
    try:
        expect(overflow).to_have_count(len(expected))
        listed = overflow.evaluate_all('ns => ns.map(n => n.innerHTML)')
        assert listed == expected, 'Overflow must preserve every entry, its order, words and symbol'
        assert set(shown) | set(listed) == set(expected)
        assert overflow.locator('.lg-words').evaluate_all('ns => ns.every(n => n.scrollWidth <= n.clientWidth + 1 && n.scrollHeight <= n.clientHeight + 1)')
        assert overflow.evaluate_all("""ns => ns.every(n => {
            const words=n.querySelector('.lg-words'), sw=(n.querySelector('.lg-act') || n).firstElementChild;
            if (!words || !sw || sw===words) return false;
            const w=words.getBoundingClientRect();
            return [sw, ...sw.querySelectorAll('*')].every(s => {const r=s.getBoundingClientRect();
                return !(r.right>w.left+.5 && r.left<w.right && r.top<w.bottom && r.bottom>w.top);});
        })""")
        yield overflow
    finally:
        page.keyboard.press('Escape')
        expect(panel).not_to_be_visible()


def legend_state(page: Page, root="#main", scrollers=()):
    """The stamped marks, key entries and unstamped marks under `root`, scrolling each virtualised container through."""
    seen = {"stamped": set(), "entries": set(), "bare": [], "keys": set(), "clipped": set(), "cut": set(), "overlaps": set(), "mute": []}

    def take():
        # Overflow can replace the containing popover; retain the marks and entries it was explaining.
        got = page.evaluate(_COLLECT, [root, MARKS])
        assert got is not None, f"{root} is not on the page"
        for key in page.locator(root).locator(".view-legend:visible").all():
            with legend_entries(page, key):
                pass
        seen["stamped"] |= set(got["stamped"]); seen["entries"] |= set(got["entries"])
        seen["bare"] += [b for b in got["bare"] if b not in seen["bare"]]; seen["keys"] |= set(got["keys"])
        seen["clipped"] |= set(got["clipped"]); seen["mute"] += [m for m in got["mute"] if m not in seen["mute"]]
        seen["cut"] |= set(got["cut"]); seen["overlaps"] |= set(got["overlaps"])
    take()
    for scroller in scrollers:
        for box in page.locator(scroller).all():
            box.evaluate("n => { n.scrollTop = 0; }"); page.evaluate(_FRAME); take()
            while box.evaluate("n => n.scrollTop + n.clientHeight < n.scrollHeight - 1"):
                box.evaluate("n => { n.scrollTop += Math.max(1, n.clientHeight - 40); }"); page.evaluate(_FRAME); take()
            box.evaluate("n => { n.scrollTop = 0; }")
    return seen


def assert_legend_matches(page: Page, root="#main", scrollers=(), expect_marks=None):
    """Every mark on screen has its key entry, every entry a mark on screen, and no mark is drawn without its key.
    `expect_marks`: entry names (after "<key id>:") the fixture is meant to produce, so a view that draws nothing fails."""
    page.evaluate(_FRAME)  # keys fill on a microtask after drawing
    seen = legend_state(page, root, scrollers)
    assert not seen["bare"], f"Marks drawn without a legend entry (use legend().mark): {seen['bare'][:5]}"
    assert not seen["clipped"], f"A key scrolls or clips its row: {sorted(seen['clipped'])}"
    assert not seen["cut"], f"Key entries whose full words are unavailable: {sorted(seen['cut'])[:5]}"
    assert not seen["overlaps"], f"Key entries whose swatch overlaps their words: {sorted(seen['overlaps'])[:5]}"
    assert not seen["mute"], f"data-lg-self on an element with no words of its own: {seen['mute'][:5]}"
    missing = seen["stamped"] - seen["entries"]
    assert not missing, f"Marks on screen without their entry in a visible key: {sorted(missing)[:10]}"
    extra = seen["entries"] - seen["stamped"]
    assert not extra, f"Key entries with no mark on screen: {sorted(extra)[:10]}"
    names = {e.split(":", 1)[1] for e in seen["entries"]}
    if expect_marks is not None:
        assert set(expect_marks) <= names, f"Expected marks not drawn: {sorted(set(expect_marks) - names)}; drawn: {sorted(names)}"
    return names
