// Sticky guard (tests/test_browser_sticky.py): every sticky header, tab bar, toolbar or footer, with its scroll container
// scrolled, is one opaque block with what it stacks against — nothing scrolled shows above, under or through it.
// Each scroll container that holds a sticky block (and each that carries one away inside an inner scroller) is scrolled
// a little and halfway, checked, and put back. Returns [{kind, where, detail}]: kind is
//   gap        a stuck block leaves room between it and the scrollport edge (or the block it stacks on), and scrolled
//              content shows there (a scroll container's padding: sticky offsets start inside it)
//   covered    something scrolled paints over the stuck block (elementFromPoint at its edges hits foreign content)
//   see-through a stuck block (or what it sits in, up to its scroller) has no opaque background: content shows through
//   lost       a sticky header is carried away by an outer scroller while the content it heads is still on screen (an
//              inner overflow box between them traps it), or it sits under another sticky block
// `allow` is a list of CSS selectors: a sticky element matching one (or inside one) is not checked.
({allow}) => {
  const found = [], seen = new Set();
  const allowed = el => allow.some(s => el.closest(s));
  const where = el => {
    const parts = [];
    for (let n = el; n && n !== document.body && parts.length < 4; n = n.parentElement) {
      const cls = [...n.classList].filter(c => !/^(duty-|is-|has-)/.test(c)).slice(0, 2).join('.');
      parts.unshift(n.tagName.toLowerCase() + (n.id && !/\d/.test(n.id) ? '#' + n.id : '') + (cls ? '.' + cls : ''));
    }
    return parts.join(' > ');
  };
  const name = el => (el.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 40);
  const report = (kind, el, detail) => {
    const key = kind + where(el);
    if (seen.has(key)) return;
    seen.add(key);
    found.push({kind, where: where(el), detail});
  };
  const doc = document.scrollingElement;
  const pinned = n => /sticky|fixed/.test(getComputedStyle(n).position);
  // Floating layers drawn over everything on purpose — unless the block itself is in that layer (a modal dialog's tabs).
  // A docked panel drawn as a phone sheet (position: fixed, over the page) floats too, as a modal dialog does.
  const sheet = n => { const d = n.closest('.dock-panel,.rk-panel'); return d && getComputedStyle(d).position === 'fixed' ? d : null; };
  const floating = (n, el) => { const f = n.closest('.sidebar,.picker,.popover,[role=tooltip],.tip,dialog[open],.toast,.toasts,.selection-bar,.unsaved-bar') || sheet(n); return !!f && !(el && f.contains(el)); };
  const scrollerOf = el => {
    for (let p = el.parentElement; p && p !== document.body && p !== document.documentElement; p = p.parentElement) {
      const cs = getComputedStyle(p);
      if (!/visible|clip/.test(cs.overflowX) || !/visible|clip/.test(cs.overflowY)) return p;
    }
    return doc;
  };
  const scrolls = s => s.scrollHeight - s.clientHeight > 24 && (s === doc || /auto|scroll/.test(getComputedStyle(s).overflowY));
  const port = s => {
    if (s === doc) return {top: 0, bottom: innerHeight, left: 0, right: document.documentElement.clientWidth};
    const r = s.getBoundingClientRect();
    return {top: r.top + s.clientTop, bottom: r.top + s.clientTop + s.clientHeight, left: r.left + s.clientLeft, right: r.left + s.clientLeft + s.clientWidth};
  };
  // The nearest scroller that moves `s` vertically.
  const outer = s => { for (let p = s === doc ? null : scrollerOf(s); p; p = p === doc ? null : scrollerOf(p)) if (scrolls(p)) return p; return null; };

  const stickies = [];
  for (const el of document.body.querySelectorAll('*')) {
    const cs = getComputedStyle(el);
    if (cs.position !== 'sticky' || (cs.top === 'auto' && cs.bottom === 'auto') || allowed(el) || (floating(el) && !el.closest('dialog[open]') && !sheet(el))) continue;
    if (!el.checkVisibility({visibilityProperty: true}) || el.getClientRects().length === 0) continue;
    const s = scrollerOf(el), r = el.getBoundingClientRect();
    if (r.width < 8 || r.height < 4) continue;
    stickies.push({el, s, top: cs.top !== 'auto', bottom: cs.bottom !== 'auto'});
  }
  // Points along a horizontal line across the block, inside the scrollport and the window.
  const xs = (r, p) => {
    const a = Math.max(r.left, p.left, 0) + 3, b = Math.min(r.right, p.right, innerWidth) - 3;
    return b <= a ? [] : [0.1, 0.3, 0.5, 0.7, 0.9].map(f => a + (b - a) * f);
  };
  // What is at a point is foreign when it belongs to the scrolled content: not the block, not another pinned block,
  // not an ancestor of the block (its own background), not a floating layer.
  const foreign = (hit, el, s) => {
    if (!hit || hit === el || el.contains(hit) || hit.contains(el) || floating(hit, el)) return false;
    if (s !== doc && !s.contains(hit)) return false;
    for (let n = hit; n && n !== s && n !== document.body; n = n.parentElement) if (n !== el && !n.contains(el) && pinned(n)) return false;
    return true;
  };
  const sideways = (hit, el) => { for (let n = hit; n && n !== document.body; n = n.parentElement) if (pinned(n) && !n.contains(el)) return n.parentElement === el.parentElement; return false; };
  const check = ({el, s, top, bottom}) => {
    const r = el.getBoundingClientRect(), p = port(s), cs = getComputedStyle(el);
    if (r.bottom <= p.top + 1 || r.top >= p.bottom - 1 || r.height > (p.bottom - p.top) * 0.6) return;
    const pad = s === doc ? 0 : parseFloat(getComputedStyle(s).paddingTop), padB = s === doc ? 0 : parseFloat(getComputedStyle(s).paddingBottom);
    const stuckTop = top && Math.abs(r.top - (p.top + pad + parseFloat(cs.top))) <= 1;
    const stuckBottom = bottom && Math.abs(r.bottom - (p.bottom - padB - parseFloat(cs.bottom))) <= 1;
    // A header (spanning its scroller; a sticky side column has nothing scrolling behind it) needs an opaque background.
    if ((stuckTop || stuckBottom) && r.width >= 0.6 * (p.right - p.left)) {
      let n = el, bg = '';
      for (; n && n !== s && n !== document.body; n = n.parentElement) { bg = getComputedStyle(n).backgroundColor; if (!/(?:rgba\(.*,|\/) 0\)$|transparent/.test(bg)) break; }
      const alpha = /(?:rgba\(.*,|\/) ([\d.]+)\)$/.exec(bg);
      if (!n || n === s || n === document.body || (alpha && Number(alpha[1]) < 1)) report('see-through', el, `"${name(el)}" background ${n && n !== s && n !== document.body ? bg : 'none'}`);
    }
    for (const x of xs(r, p)) {
      if (stuckTop && r.top - p.top > 1) {
        const hit = document.elementFromPoint(x, r.top - 1);
        if (foreign(hit, el, s)) report('gap', el, `${Math.round(r.top - p.top)}px above "${name(el)}" shows ${where(hit).split(' > ').pop()} "${name(hit)}"`);
      }
      if (stuckBottom && p.bottom - r.bottom > 1) {
        const hit = document.elementFromPoint(x, r.bottom + 1);
        if (foreign(hit, el, s)) report('gap', el, `${Math.round(p.bottom - r.bottom)}px below "${name(el)}" shows ${where(hit).split(' > ').pop()} "${name(hit)}"`);
      }
      if (stuckTop || stuckBottom) for (const y of [r.top + 1, (r.top + r.bottom) / 2, r.bottom - 1]) {
        const hit = document.elementFromPoint(x, y);
        if (!hit || floating(hit, el) || hit === el || el.contains(hit) || hit.contains(el)) continue;
        if (foreign(hit, el, s)) report('covered', el, `"${name(el)}" is covered by ${where(hit).split(' > ').pop()} "${name(hit)}"`);
        // Under another pinned block — not a cell of its own row pinned sideways (a sticky first or actions column).
        else if (y !== r.top + 1 && y !== r.bottom - 1 && !sideways(hit, el)) report('lost', el, `"${name(el)}" sits under ${where(hit).split(' > ').pop()} "${name(hit)}"`);
      }
    }
  };
  // A sticky header that an outer scroller carries away: off its top (or under that scroller's pinned blocks) while
  // the content it heads is still on screen below.
  const lost = ({el, s, top}, o) => {
    if (!top) return;
    const r = el.getBoundingClientRect(), p = port(o), body = s.getBoundingClientRect();
    if (body.bottom - Math.max(r.bottom, p.top) < 80 || body.top > p.top) return;
    const x = xs(r, p)[2];
    if (x === undefined) return;
    const hit = r.bottom <= p.top ? null : document.elementFromPoint(x, Math.max(r.top, p.top) + r.height / 2);
    if (!hit || !el.contains(hit)) report('lost', el, `"${name(el)}" scrolls away (inside ${where(s).split(' > ').pop()}) while what it heads stays on screen`);
  };

  const groups = new Map(), add = (s, kind, item) => { if (!groups.has(s)) groups.set(s, []); groups.get(s).push([kind, item]); };
  for (const it of stickies) {
    if (scrolls(it.s)) add(it.s, 'check', it);
    else { const o = outer(it.s); if (o && it.s !== doc) add(o, 'lost', it); }
  }
  for (const [s, items] of groups) {
    const before = s.scrollTop, most = s.scrollHeight - s.clientHeight;
    for (const at of [Math.min(most, 160), Math.round(most / 2)]) {
      s.scrollTop = at;
      for (const [kind, it] of items) kind === 'check' ? check(it) : lost(it, s);
    }
    s.scrollTop = before;
  }
  return found;
}
