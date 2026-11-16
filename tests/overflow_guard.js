// Layout guard (tests/test_browser_overflow.py): what overflows, overlaps or clips on the page as drawn now.
// Returns [{kind, where, detail}]: kind is
//   page-scroll   the page scrolls sideways
//   overflow      an element's content is wider than its box and spills out (not a scroll container, not an ellipsis)
//   clipped       content wider than an overflow:hidden box that does not end in an ellipsis (text cut mid-letter)
//   no-tooltip    text cut with an ellipsis whose full wording is in no title, accessible name or shared hover tip
//   overlap       two sibling boxes (text, badges, buttons) cover each other
//   focus-ring    a control's focus ring (as drawn when focused from the keyboard) is cut by an ancestor that clips
//   sticky-cut    a sticky-left element (a name column, a group label) left of its scroller's or the viewport's left edge:
//                 it no longer sticks (maintenance: a phone's timeline group labels cut to "…stration")
// `allow` is a list of CSS selectors: an element matching one (or inside one) is not reported.
// `focus`: also focus every visible control (the page must be in keyboard modality: press a key first).
({allow, focus}) => {
  const found = [], seen = new Set();
  const allowed = el => allow.some(s => el.closest(s));
  const where = el => {
    const parts = [];
    for (let n = el; n && n !== document.body && parts.length < 4; n = n.parentElement) {
      const cls = [...n.classList].filter(c => !/^(duty-|is-|has-)/.test(c)).slice(0, 2).join('.');
      const id = n.id && !/\d/.test(n.id) ? '#' + n.id : '';
      parts.unshift(n.tagName.toLowerCase() + id + (cls ? '.' + cls : ''));
    }
    return parts.join(' > ');
  };
  const text = el => (el.innerText || el.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ').slice(0, 60);
  const report = (kind, el, detail) => {
    const key = kind + where(el) + detail;
    if (seen.has(key) || allowed(el)) return;
    seen.add(key);
    found.push({kind, where: where(el), detail: (detail + ' ' + JSON.stringify(text(el))).trim()});
  };
  const shown = (el, cs) => {
    if (!el.checkVisibility({opacityProperty: true, visibilityProperty: true})) return false;
    // Visually hidden on purpose (screen-reader text, a label folded to an icon): a 1px box or a clip.
    if (cs.clip !== 'auto' || /inset\(50%|circle\(0/.test(cs.clipPath)) return false;
    const r = el.getBoundingClientRect();
    return r.width > 1 && r.height > 1;
  };
  const scroller = cs => /auto|scroll/.test(cs.overflowX);
  const clips = cs => cs.overflowX !== 'visible' || cs.overflowY !== 'visible' || /paint|strict|content/.test(cs.contain);
  const doc = document.documentElement;
  if (doc.scrollWidth > doc.clientWidth + 1) found.push({kind: 'page-scroll', where: 'html', detail: `${doc.scrollWidth} > ${doc.clientWidth}`});

  const styles = new Map();
  for (const el of document.body.querySelectorAll('*')) {
    if (el instanceof SVGElement && !(el instanceof SVGSVGElement)) continue;
    const cs = getComputedStyle(el);
    if (shown(el, cs)) styles.set(el, cs);
  }
  // What pushes the page sideways: the innermost boxes that reach past the right edge.
  if (doc.scrollWidth > doc.clientWidth + 1) {
    const past = el => styles.has(el) && el.getBoundingClientRect().right > doc.clientWidth + 1 && !el.closest('.sidebar');
    for (const el of styles.keys()) if (past(el) && ![...el.children].some(past)) report('page-scroll', el, `right edge ${Math.round(el.getBoundingClientRect().right)}`);
  }
  const spills = el => {
    const cs = styles.get(el);
    return cs && !/^(inline|table|table-row|table-row-group|table-header-group|table-footer-group)$/.test(cs.display) && el.clientWidth && el.scrollWidth > el.clientWidth + 1 && !scroller(cs);
  };

  for (const [el, cs] of styles) {
    // (a) content wider than the box: report the innermost box that spills (its children fit inside it).
    if (spills(el)) {
      if (cs.overflowX === 'visible') {
        if (![...el.children].some(spills)) {
          // Name what sticks out: the descendant reaching furthest past the box's right edge.
          let far = null, edge = el.getBoundingClientRect().right;
          for (const d of el.querySelectorAll('*')) { const r = d.getBoundingClientRect(); if (styles.has(d) && r.right > edge + 1) { edge = r.right; far = d; } }
          report('overflow', el, `${el.scrollWidth} > ${el.clientWidth}${far ? ' (' + where(far).split(' > ').pop() + ')' : ''}`);
        }
      } else if (cs.textOverflow !== 'ellipsis' && !/rgba\(\d+, \d+, \d+, 0\)|transparent/.test(cs.color) && el.innerText.trim()) {
        report('clipped', el, `${el.scrollWidth} > ${el.clientWidth}`);
      }
    }
    // Cut with an ellipsis: the full wording must be reachable (a title on it or an ancestor, or an accessible name),
    // or the shared hover tip shows it (dom.ts hoverContent: a compact tip data-ctip, and maintenance's cut-text classes CUT_TEXT).
    if (cs.textOverflow === 'ellipsis' && cs.overflowX !== 'visible' && el.scrollWidth > el.clientWidth + 1
        && !el.closest('[title],[aria-label],[data-tip],[data-ctip]') && !el.matches('.truncate,.clamp-2')) report('no-tooltip', el, '');
  }

  // (b) sibling boxes that cover each other (in-flow siblings only: a positioned badge on an icon is deliberate)
  const parents = new Set([...styles.keys()].map(el => el.parentElement).filter(Boolean));
  for (const parent of parents) {
    const kids = [...parent.children].filter(c => {
      const cs = styles.get(c);
      // An inline run that wraps onto several lines has a bounding box spanning them all: not comparable.
      return cs && !/absolute|fixed|sticky/.test(cs.position) && cs.float === 'none' && !(cs.display === 'inline' && c.getClientRects().length > 1);
    });
    if (kids.length < 2 || kids.length > 400) continue;
    const boxes = kids.map(c => [c, c.getBoundingClientRect()]).sort((a, b) => a[1].left - b[1].left);
    for (let i = 0; i < boxes.length; i++) {
      const [a, ra] = boxes[i];
      for (let j = i + 1; j < boxes.length && boxes[j][1].left < ra.right - 1; j++) {
        const [b, rb] = boxes[j];
        const x = Math.min(ra.right, rb.right) - Math.max(ra.left, rb.left);
        const y = Math.min(ra.bottom, rb.bottom) - Math.max(ra.top, rb.top);
        // A box tucked 1-2px under its neighbour (joined buttons, avatars) is not an overlap.
        if (x > 2 && y > 2) report('overlap', a, `covers ${where(b).split(' > ').pop()} ${JSON.stringify(text(b))} by ${Math.round(x)}x${Math.round(y)}px`);
      }
    }
  }

  // (e) sticky-left elements that no longer stick: one stretched as wide as its containing block cannot move, so it
  // scrolls away with the content (maintenance). Its left edge must stay at or right of its scroller's and the viewport's.
  for (const [el, cs] of styles) {
    if (cs.position !== 'sticky' || cs.left === 'auto' || !shown(el, cs)) continue;
    let edge = 0;
    for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) {
      const acs = styles.get(a);
      if (acs && scroller(acs)) { edge = Math.max(edge, a.getBoundingClientRect().left + a.clientLeft); break; }
    }
    const left = el.getBoundingClientRect().left;
    if (left < edge - .5) report('sticky-cut', el, `left ${Math.round(left)} < ${Math.round(edge)}`);
  }

  // (d) focus rings cut by a clipping ancestor: each visible control is focused and its ring measured (outline +
  // offset, or a box-shadow spread), checked only for controls fully inside the clipping box (one scrolled partly out
  // of view is clipped on purpose).
  if (focus) {
    const before = document.activeElement;
    const ringOf = cs => {
      let ring = cs.outlineStyle !== 'none' ? parseFloat(cs.outlineWidth) + parseFloat(cs.outlineOffset) : 0;
      for (const shadow of cs.boxShadow === 'none' ? [] : cs.boxShadow.split(/,(?![^(]*\))/)) {
        if (shadow.includes('inset')) continue;
        const [x = 0, y = 0, , spread = 0] = (shadow.match(/-?[\d.]+px/g) || []).map(parseFloat);
        if (!x && !y) ring = Math.max(ring, spread);  // a ring is 0 0 0 Npx; an offset drop shadow is depth, not a ring
      }
      return ring;
    };
    const controls = [...styles.keys()].filter(el => el.matches('a[href],button,input:not([type=hidden]),select,textarea,summary,[tabindex]:not([tabindex="-1"])') && !el.disabled);
    for (const el of controls) {
      const r0 = el.getBoundingClientRect();
      if (r0.bottom < 0 || r0.top > innerHeight || r0.right < 0 || r0.left > innerWidth) continue;
      el.focus({preventScroll: true});
      if (document.activeElement !== el || !el.matches(':focus-visible')) continue;
      const ring = ringOf(getComputedStyle(el));
      if (ring <= .5) continue;
      const r = el.getBoundingClientRect();
      for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) {
        const acs = styles.get(a);
        if (!acs || !clips(acs)) continue;
        const ar = a.getBoundingClientRect();
        const box = {left: ar.left + a.clientLeft, top: ar.top + a.clientTop};
        box.right = box.left + a.clientWidth; box.bottom = box.top + a.clientHeight;
        if (r.left < box.left - .5 || r.right > box.right + .5 || r.top < box.top - .5 || r.bottom > box.bottom + .5) break;
        // A side the container can still scroll towards is not a cut: scrolling brings the ring into view.
        const more = {left: a.scrollLeft > 0, top: a.scrollTop > 0,
                      right: a.scrollLeft + a.clientWidth < a.scrollWidth - 1, bottom: a.scrollTop + a.clientHeight < a.scrollHeight - 1};
        const cut = ['left', 'top'].filter(s => r[s] - ring < box[s] - .5).concat(['right', 'bottom'].filter(s => r[s] + ring > box[s] + .5))
          .filter(s => !(more[s] && /auto|scroll/.test(/left|right/.test(s) ? acs.overflowX : acs.overflowY)));
        if (cut.length) { report('focus-ring', el, `${ring}px ring cut on ${cut.join('/')} by ${where(a).split(' > ').pop()}`); break; }
      }
    }
    if (before && before !== document.body && before.focus) before.focus({preventScroll: true}); else document.activeElement?.blur?.();
  }
  return found;
}
