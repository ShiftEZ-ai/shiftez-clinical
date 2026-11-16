/**
 * The one list picker behind every dropdown (single and multiple choice).
 *
 * - A search box on every list reviewed: case- and accent-insensitive, every typed word must match
 *   (label, group, hint or keywords), ranked exact › starts with › word starts with › contains › matches other text,
 *   matches marked. Lists longer than 7 (LONG_LIST) also lead with recent picks (unless `recent: false`) and offer
 *   group chips. `meta` adds trailing content to a row (a load bar beside its words).
 * - Groups with sticky headers and counts; while searching, one ranked list with the group shown beside each row.
 * - Multiple choice stays open, offers "Select all N shown" (the filtered rows only) and "Clear", and lists what
 *   was already chosen first (fixed while open, so rows never jump under the pointer).
 * - Keyboard (WAI-ARIA combobox/listbox): arrows move the highlight only, Enter picks, Page keys jump 10, Esc clears
 *   the search then closes, Tab leaves; a key typed on the closed field starts the search. A polite status announces counts.
 * - At most 200 rows are drawn ("type to narrow" beyond that); phones get a bottom sheet with 16 px text.
 * - The panel lives inside its own field (so it works inside modal dialogs) and is shown in the top layer.
 *
 * Every native <select> in the app opens through `enhanceSelects()` below, so the <select> stays the single
 * holder of the value (forms, tests and screen readers keep working) while people get the searchable list.
 */
import {el, icon, phoneQuery, placeNear, type Choice} from './dom.js';

export type PickerConfig = {
  label: string;
  choices: Choice[];
  multiple: boolean;
  /** Current value(s): one for single choice, any number for multiple. */
  selected: Set<string>;
  onPick: (value: string) => void;
  onToggle?: (values: string[], on: boolean) => void;
  creatable?: boolean;
  onCreate?: (text: string) => void;
  query?: string;
  /** Remember recent picks under this name (defaults to the label); they are listed first next time. */
  recentKey?: string;
  /** false: no Recent section and nothing remembered (a list whose order means something, such as fullest load first). */
  recent?: boolean;
  /** Trailing content for a row, after its hint (a load bar with its numbers). Any meaning it carries must be in text. */
  meta?: (choice: Choice) => Node | null;
  /** Typed text is understood elsewhere (a date's short forms, the server's parse-date): while the search has text,
   *  only these matches are listed, the first one highlighted (Enter picks it). */
  lookup?: (query: string) => Promise<Choice[] | {choices: Choice[]; more?: boolean}>;
  /** Only the lookup's matches are listed (choices are only what it found): typing waits for it, never a local guess. */
  lookupOnly?: boolean;
  /** Below the list: the exact value (a date field with its calendar). */
  extra?: HTMLElement;
  /** A line under everything ("Last / Next N always include today"). */
  footer?: string;
  /** Single choice with a value to clear: Clear and Done under the list (the phone sheet's footer). */
  onClear?: () => void;
  /** Said when there is nothing to list and nothing typed ("Type a value"). */
  emptyText?: string;
};

/** One threshold for every dropdown: a list longer than this gets recent picks, group chips and (in forms) a picker. */
export const LONG_LIST = 7;
const LIMIT = 200;
let current: {close: () => void; anchor: HTMLElement} | null = null;
let sequence = 0;

// Recently used values per list, kept in this browser only (cleared at sign-out) and never sent anywhere.
const RECENT = 'rota.recent.', RECENT_MAX = 5;
function recentOf(key: string): string[] {
  try { return JSON.parse(localStorage.getItem(RECENT + key) || '[]'); } catch { return []; }
}
function remember(key: string, values: string[]) {
  if (!values.length) return;
  try { localStorage.setItem(RECENT + key, JSON.stringify([...values, ...recentOf(key).filter(v => !values.includes(v))].slice(0, RECENT_MAX))); } catch { /* not kept */ }
}
/** Forget every list's recent picks (sign-out). */
export function forgetRecent() {
  try { for (const k of Object.keys(localStorage)) if (k.startsWith(RECENT)) localStorage.removeItem(k); } catch { /* nothing kept */ }
}

const fold = (s: string) => s.normalize('NFD').replace(/\p{M}/gu, '').toLowerCase();

type Item = {c: Choice; index: number; label: string; extra: string};

function rank(item: Item, words: string[], whole: string): number {
  if (!words.every(w => item.label.includes(w) || item.extra.includes(w))) return -1;
  if (item.label === whole) return 0;
  if (item.label.startsWith(whole)) return 1;
  if (item.label.split(/[\s·,()\-/]+/).some(part => part.startsWith(words[0]))) return 2;
  return words.every(w => item.label.includes(w)) ? 3 : 4;
}

/** Wrap the matched parts of a label in <mark>. */
function marked(text: string, words: string[]): Node[] {
  const folded = fold(text), hits: [number, number][] = [];
  // Positions come from the folded text; only mark when folding kept every character in place.
  if (!words.length || folded.length !== text.length) return [document.createTextNode(text)];
  for (const w of words) { let at = folded.indexOf(w); while (at >= 0) { hits.push([at, at + w.length]); at = folded.indexOf(w, at + w.length); } }
  if (!hits.length) return [document.createTextNode(text)];
  hits.sort((a, b) => a[0] - b[0]);
  const out: Node[] = []; let pos = 0;
  for (const [s, e] of hits) { if (s < pos) continue; if (s > pos) out.push(document.createTextNode(text.slice(pos, s))); out.push(el('mark', {}, text.slice(s, e))); pos = e; }
  if (pos < text.length) out.push(document.createTextNode(text.slice(pos)));
  return out;
}

/** The head of every bottom sheet on a phone (the picker, the date calendar): what the sheet is for, and a 44 px Close,
 *  so a touch user without Esc always has a way out (X-10, X-15). */
export function sheetHead(label: string, close: () => void): HTMLElement {
  const id = `sheet-title-${++sequence}`, x = el('button', {type: 'button', class: 'icon-button bare sheet-close', 'aria-label': 'Close'}, icon('close'));
  x.addEventListener('pointerdown', e => e.preventDefault());
  x.addEventListener('click', close);
  return el('div', {class: 'sheet-head'}, el('h2', {class: 'sheet-title', id}, label), x);
}

/** Open the picker under `anchor`; the panel is placed inside `host` (a descendant of any open dialog). */
export function openPicker(anchor: HTMLElement, host: HTMLElement, cfg: PickerConfig): () => void {
  current?.close();
  const id = `picker-${++sequence}`;
  const itemsOf = () => cfg.choices.map((c, index) => ({c, index, label: fold(c.label), extra: fold([c.group, c.hint, c.keywords].filter(Boolean).join(' '))}));
  let items: Item[] = itemsOf();
  // A short list (LONG_LIST or fewer) keeps its own order with no sections: the tick shows what is chosen (X-37).
  const short = cfg.choices.length <= LONG_LIST && !cfg.creatable;
  const firstChosen = new Set(cfg.multiple && !short ? cfg.selected : []);  // "already chosen" section, fixed while open
  const recentKey = cfg.recentKey || cfg.label;
  // Recent picks lead longer lists (when not searching); anything already chosen is shown under Chosen instead.
  const recent = new Set(cfg.recent !== false && cfg.choices.length > LONG_LIST ? recentOf(recentKey).filter(v => cfg.choices.some(c => c.value === v) && !firstChosen.has(v)) : []);
  // Group chips narrow a longer list to one or more groups (for example a rank), then search and select within them.
  const groupNames = [...new Set(cfg.choices.map(c => c.group).filter(Boolean) as string[])];
  const onlyGroups = new Set<string>();
  const chips = el('div', {class: 'picker-chips', role: 'group', 'aria-label': 'Show only'});
  const long = items.length > LONG_LIST || !!cfg.creatable;
  const search = el('input', {type: 'search', class: 'picker-search', placeholder: 'Search', autocomplete: 'off', spellcheck: 'false',
    role: 'combobox', 'aria-expanded': 'true', 'aria-controls': id + '-list', 'aria-autocomplete': 'list', 'aria-label': 'Search ' + cfg.label}) as HTMLInputElement;
  // A listbox of options either way: multiple choice is aria-multiselectable, each option selected (and checked) or not,
  // so the combobox's highlighted option is announced while ↑ ↓ move (ARIA 1.2 combobox; X-36). The tick box is drawing.
  const list = el('div', {class: 'picker-list', id: id + '-list', role: 'listbox', ...(cfg.multiple ? {'aria-multiselectable': 'true'} : {}), 'aria-label': cfg.label, tabindex: '-1'});
  const status = el('div', {class: 'sr-only', role: 'status', 'aria-live': 'polite'});
  const actions = el('div', {class: 'picker-actions'});
  // A phone (the one phone breakpoint) gets a bottom sheet with a head naming the field and a Close.
  const sheet = phoneQuery.matches, head = sheet ? sheetHead(cfg.label, () => close(true)) : null;
  const panel = el('div', {class: 'picker' + (sheet ? ' sheet' : ''), role: 'dialog', 'aria-label': cfg.label, ...(head ? {'aria-labelledby': head.firstElementChild!.id} : {})},  // the heading names a sheet; the label still finds it (app.ts Drafts)
    // Keyboard order: the search, then the actions, then the list.
    ...(head ? [head] : []), search, ...(long && groupNames.length > 1 ? [chips] : []), ...(cfg.multiple ? [actions] : []), list,
    ...(cfg.extra ? [cfg.extra] : []), ...(cfg.footer ? [el('p', {class: 'picker-foot hint'}, cfg.footer)] : []), ...(!cfg.multiple && cfg.onClear ? [actions] : []), status);
  if (!cfg.multiple && cfg.onClear) {
    const clear = el('button', {type: 'button', class: 'button ghost small'}, 'Clear'), done = el('button', {type: 'button', class: 'button secondary small picker-done'}, 'Done');
    clear.addEventListener('click', () => { cfg.onClear!(); close(true); }); done.addEventListener('click', () => close(true));
    actions.append(clear, el('span', {class: 'grow'}), done);
  }
  if (cfg.query) search.value = cfg.query;
  let rows: HTMLElement[] = [], active = -1, shown: Item[] = [], announce = 0;

  const setActive = (i: number, scroll = true) => {
    rows[active]?.classList.remove('active');
    active = rows.length ? Math.max(0, Math.min(i, rows.length - 1)) : -1;
    const row = rows[active];
    if (!row) { search.removeAttribute('aria-activedescendant'); return; }
    row.classList.add('active');
    search.setAttribute('aria-activedescendant', row.id);
    if (scroll) row.scrollIntoView({block: 'nearest'});
  };

  function rowFor(it: Item, words: string[], withGroup: boolean): HTMLElement {
    const chosen = cfg.selected.has(it.c.value), hinted = !!(it.c.hint || (withGroup && it.c.group));
    const row = el('div', {class: 'picker-row' + (chosen ? ' chosen' : '') + (it.c.disabled ? ' disabled' : ''), id: `${id}-o${it.index}`,
      role: 'option', 'aria-selected': String(chosen), ...(it.c.disabled ? {'aria-disabled': 'true'} : {}),
      // Multiple choice: named by its value (its hint describes it) and checked like the box it draws.
      ...(cfg.multiple ? {'aria-checked': String(chosen), 'aria-label': it.c.label, ...(hinted ? {'aria-describedby': `${id}-h${it.index}`} : {})} : {})});
    // A tick for "chosen" and a background for "highlighted": never the same look.
    const mark = cfg.multiple
      ? el('input', {type: 'checkbox', tabindex: '-1', checked: chosen, disabled: !!it.c.disabled, 'aria-hidden': 'true'}) as HTMLInputElement
      : el('span', {class: 'picker-tick', 'aria-hidden': 'true'}, chosen ? icon('check') : '');
    if (it.c.divider) row.classList.add('divider-before');
    row.append(mark, ...(it.c.glyph ? [el('span', {class: 'picker-glyph', 'aria-hidden': 'true'}, icon(it.c.glyph))] : []), el('span', {class: 'picker-label'}, ...marked(it.c.label, words)),
      ...(hinted ? [el('span', {class: 'picker-hint', id: `${id}-h${it.index}`}, [withGroup ? it.c.group : '', it.c.hint].filter(Boolean).join(' · '))] : []));
    const meta = cfg.meta?.(it.c);
    if (meta) row.append(el('span', {class: 'picker-meta'}, meta));
    row.addEventListener('pointerdown', e => e.preventDefault());  // keep focus in the search box
    row.addEventListener('click', e => { if (e.target !== mark) e.preventDefault(); choose(it); });
    return row;
  }

  /** Several absolute values at once (Select all, a group's box): an exclusive value chosen before ("Me") gives way, as
   *  when one of them is chosen alone, so what is ticked is what is kept. */
  function addAll(values: string[]) {
    const off = cfg.choices.filter(c => c.exclusive && cfg.selected.has(c.value)).map(c => c.value);
    for (const v of off) cfg.selected.delete(v);
    if (off.length) cfg.onToggle?.(off, false);
    values.forEach(v => cfg.selected.add(v)); cfg.onToggle?.(values, true);
  }

  function choose(it: Item) {
    if (it.c.disabled) return;
    if (!cfg.multiple) { if (cfg.recent !== false) remember(recentKey, [it.c.value]); cfg.onPick(it.c.value); close(true); return; }
    const on = !cfg.selected.has(it.c.value);
    // An exclusive value (a relative one, "Me") stands alone: choosing it clears the others, and another clears it.
    const off = on ? cfg.choices.filter(c => c.value !== it.c.value && cfg.selected.has(c.value) && (it.c.exclusive || c.exclusive)).map(c => c.value) : [];
    for (const v of off) cfg.selected.delete(v);
    if (off.length) cfg.onToggle?.(off, false);
    on ? cfg.selected.add(it.c.value) : cfg.selected.delete(it.c.value);
    if (on && cfg.recent !== false) remember(recentKey, [it.c.value]);
    cfg.onToggle?.([it.c.value], on);
    draw(false);
  }

  function draw(reset = true) {
    const q = search.value.trim(), words = fold(q).split(/\s+/).filter(Boolean), whole = fold(q);
    const keep = active;
    if (words.length && looked?.query === q) shown = looked.items;
    else if (words.length && cfg.lookupOnly) shown = [];
    else if (words.length) {
      shown = items.map(it => [it, rank(it, words, whole)] as const).filter(([, r]) => r >= 0).sort((a, b) => a[1] - b[1] || a[0].index - b[0].index).map(([it]) => it);
    } else shown = [...items.filter(it => firstChosen.has(it.c.value)), ...items.filter(it => recent.has(it.c.value)),
      ...items.filter(it => !firstChosen.has(it.c.value) && !recent.has(it.c.value))];
    if (onlyGroups.size) shown = shown.filter(it => onlyGroups.has(it.c.group || ''));
    chips.replaceChildren(...groupNames.map(name => {
      const on = onlyGroups.has(name), n = items.filter(it => it.c.group === name).length;
      const chip = el('button', {type: 'button', class: 'picker-chip' + (on ? ' on' : ''), 'aria-pressed': String(on)}, name, el('span', {class: 'picker-count'}, String(n)));
      chip.addEventListener('pointerdown', e => e.preventDefault());
      chip.addEventListener('click', () => { on ? onlyGroups.delete(name) : onlyGroups.add(name); draw(); });
      return chip;
    }));
    list.replaceChildren(); rows = [];
    const drawn = shown.slice(0, LIMIT);
    let group: string | null = null, section: HTMLElement = list;
    const startGroup = (name: string, members: Item[]) => {
      const head = el('div', {class: 'picker-group', id: `${id}-g${list.children.length}`, role: 'presentation'});
      const open = members.filter(m => !m.c.disabled && !m.c.exclusive);
      if (cfg.multiple && name !== 'Chosen' && name !== 'Recent' && open.length) {
        const all = open.length > 0 && open.every(m => cfg.selected.has(m.c.value)), some = open.some(m => cfg.selected.has(m.c.value));
        const box = el('input', {type: 'checkbox', tabindex: '-1', checked: all, 'aria-hidden': 'true'}) as HTMLInputElement;
        box.indeterminate = some && !all;
        // An option of the listbox too (never a button inside it): choosing it chooses or clears the whole group.
        // On the same keyboard path as the people in it: ↓ reaches it (active-descendant) and Enter chooses the group.
        const toggle = el('div', {role: 'option', id: head.id + '-all', 'aria-selected': String(all), 'aria-checked': some && !all ? 'mixed' : String(all), 'aria-label': `All in ${name}`,
          class: 'picker-group-toggle', 'data-tip': 'Choose or clear everyone in this group'}, box, el('span', {}, name), el('span', {class: 'picker-count'}, String(members.length)));
        toggle.addEventListener('pointerdown', e => e.preventDefault());
        toggle.addEventListener('click', () => { const values = open.map(m => m.c.value); const on = !all;
          if (on) addAll(values); else { for (const v of values) cfg.selected.delete(v); cfg.onToggle?.(values, false); } draw(false); });
        head.append(toggle); rows.push(toggle);
      } else head.append(el('span', {}, name), el('span', {class: 'picker-count'}, String(members.length)));
      section = el('div', {role: 'group', 'aria-labelledby': head.id});
      list.append(head, section);
    };
    const sectionOf = (it: Item) => firstChosen.has(it.c.value) ? 'Chosen' : recent.has(it.c.value) && !onlyGroups.size ? 'Recent' : it.c.group || 'Others';
    // A short list keeps the groups its values have (Relative values, a rank) but no Chosen / Recent sections.
    // One section alone ("Others · 26" and nothing else) is no grouping: its header is left out (F-R8).
    const grouped = !words.length && drawn.some(it => it.c.group || firstChosen.has(it.c.value) || recent.has(it.c.value)) && new Set(drawn.map(sectionOf)).size > 1;
    for (const it of drawn) {
      if (grouped) {
        const name = sectionOf(it);
        if (name !== group) { group = name; startGroup(name, shown.filter(s => sectionOf(s) === name)); }
      }
      // SW3-D-11: a row names its group only when the list mixes groups (one group said under every row is noise).
      const row = rowFor(it, words, !grouped && groupNames.length > 1); rows.push(row); section.append(row);
    }
    const typedText = q && cfg.creatable && !items.some(it => it.label === whole) ? q : '';
    if (typedText) {
      const add = el('button', {type: 'button', class: 'picker-row picker-add', id: id + '-add'}, icon('plus'), el('span', {}, `Add “${typedText}”`));
      add.addEventListener('pointerdown', e => e.preventDefault());
      add.addEventListener('click', () => { cfg.onCreate?.(typedText); items = itemsOf(); search.value = ''; draw(); });
      rows.push(add); list.append(add);
    }
    if (looked?.query === q && rows[0]) rows[0].classList.add('best');
    // A lookup on its way or failed says so, never "Nothing matches" before the answer.
    const waiting = !!cfg.lookup && !!q && looked?.query !== q;
    if (!shown.length && !typedText) list.append(el('p', {class: 'picker-empty'}, waiting && lookFailed === q ? 'Could not search. Type again to retry.' : waiting && cfg.lookupOnly ? 'Searching…' : q ? `Nothing matches “${q}”` : cfg.emptyText || 'Nothing to choose yet'));
    if (looked?.query === q && looked.more) list.append(el('p', {class: 'picker-more'}, 'More matches · type to narrow'));
    if (shown.length > LIMIT) list.append(el('p', {class: 'picker-more'}, `Showing ${LIMIT} of ${shown.length} · type to narrow`));
    if (cfg.multiple) {
      const visible = shown.filter(it => !it.c.disabled && !it.c.exclusive), allOn = visible.length > 0 && visible.every(it => cfg.selected.has(it.c.value));
      const pick = el('button', {type: 'button', class: 'button ghost small', disabled: allOn || !visible.length}, words.length ? `Select all ${visible.length} shown` : `Select all ${visible.length}`);
      pick.addEventListener('click', () => { addAll(visible.map(it => it.c.value)); draw(false); });
      const clear = el('button', {type: 'button', class: 'button ghost small', disabled: !cfg.selected.size}, cfg.selected.size ? `Clear ${cfg.selected.size}` : 'Clear');
      clear.addEventListener('click', () => { const values = [...cfg.selected]; cfg.selected.clear(); cfg.onToggle?.(values, false); draw(false); });
      const done = el('button', {type: 'button', class: 'button secondary small picker-done'}, 'Done');
      done.addEventListener('click', () => close(true));
      for (const b of [pick, clear, done]) b.addEventListener('pointerdown', e => e.preventDefault());
      const had = [...actions.children].indexOf(document.activeElement as Element);
      // Select all and Clear help on a list of more than three; on fewer, a tick is one tap (X-37).
      actions.replaceChildren(...(cfg.choices.length > 3 ? [pick, clear] : []), el('span', {class: 'grow'}), done);
      // Keyboard users keep their place on the action row; if that button is now disabled, back to the search.
      const again = actions.children[had] as HTMLButtonElement | undefined;
      const next = [...actions.querySelectorAll<HTMLButtonElement>("button")].slice(Math.max(0, had)).find(b => !b.disabled);
      if (had >= 0) (again && !again.disabled ? again : next || search).focus();
    }
    // Announce the count once typing pauses.
    clearTimeout(announce);
    announce = window.setTimeout(() => { status.textContent = q ? `${shown.length} ${shown.length === 1 ? 'result' : 'results'}` : ''; }, 400);
    const current = cfg.multiple ? -1 : rows.findIndex(r => r.classList.contains('chosen'));
    setActive(reset ? (words.length ? 0 : Math.max(0, current)) : keep, reset);
    // Re-placed at once: a list opened upwards keeps its edge on the field while typing changes its height.
    place();
  }

  function onKey(e: KeyboardEvent) {
    const k = e.key;
    if (k === 'ArrowDown' || k === 'ArrowUp') { e.preventDefault(); setActive(active + (k === 'ArrowDown' ? 1 : -1)); return; }
    if (k === 'PageDown' || k === 'PageUp') { e.preventDefault(); setActive(active + (k === 'PageDown' ? 10 : -10)); return; }
    if ((k === 'Home' || k === 'End') && e.ctrlKey) { e.preventDefault(); setActive(k === 'Home' ? 0 : rows.length - 1); return; }
    if (k === 'Enter') { e.preventDefault(); rows[active]?.click(); }
    // Backspace in an empty search takes back the last value chosen (as in a chip field).
    if (k === 'Backspace' && !search.value && cfg.multiple && cfg.selected.size) {
      e.preventDefault(); const last = [...cfg.selected].pop()!; cfg.selected.delete(last); cfg.onToggle?.([last], false); draw(false);
    }
  }
  // Esc is handled before dialogs and outer popovers: first clear the search, then close this list only.
  function onEscape(e: KeyboardEvent) {
    if (!panel.isConnected) { close(false); return; }
    if (e.key !== 'Escape') return;
    e.preventDefault(); e.stopPropagation();
    if (search.value) { search.value = ''; draw(); return; }
    close(true);
  }
  // A tap on the sheet's dimmed backdrop reaches the panel itself, outside its box: that closes it too.
  const onBackdrop = (e: Event) => { const p = e as PointerEvent, r = panel.getBoundingClientRect();
    return e.target === panel && (p.clientY < r.top || p.clientY > r.bottom || p.clientX < r.left || p.clientX > r.right); };
  const onDown = (e: Event) => { const t = e.target as Node; if (!panel.isConnected || (!panel.contains(t) && !anchor.contains(t)) || onBackdrop(e)) close(false); };
  const dialog = host.closest('dialog');
  const keepDialog = (e: Event) => e.preventDefault();
  // The one placement engine (dom.ts placeNear): at least 300px or the field's width, 180–420px tall, below while 260px
  // fit there; a phone sheet is laid out by CSS. Started once the panel is shown.
  let placer: {place: () => void; dispose: () => void} | null = null;
  const place = () => placer?.place();
  function close(refocus: boolean) {
    if (current?.anchor !== anchor) return;
    current = null;
    window.removeEventListener('keydown', onEscape, true); document.removeEventListener('pointerdown', onDown, true);
    window.removeEventListener('scroll', reposition, true);
    dialog?.removeEventListener('cancel', keepDialog);
    anchor.setAttribute('aria-expanded', 'false'); anchor.classList.remove('is-open'); placer?.dispose();
    try { (panel as HTMLElement & {hidePopover?: () => void}).hidePopover?.(); } catch { /* already hidden */ }
    panel.remove();
    if (refocus) anchor.focus();
  }
  // A page redraw that took the panel away closes it (the engine follows scrolling itself).
  const reposition = () => { if (!panel.isConnected) close(false); };
  // Tab moves through the search, the action buttons and out; leaving the panel closes it (no focus trap).
  const onLeave = (e: FocusEvent) => { const to = e.relatedTarget as Node | null; if (to && !panel.contains(to) && to !== anchor) close(false); };

  // Matches from cfg.lookup replace the list while the search has text; an older answer never overwrites a newer one.
  let looked: {query: string; items: Item[]; more?: boolean} | null = null, asked = 0, lookTimer = 0, lookFailed = '';
  const lookUp = () => {
    const q = search.value.trim(), mine = ++asked;
    clearTimeout(lookTimer); lookFailed = '';
    if (!cfg.lookup || !q) { looked = null; return; }
    lookTimer = window.setTimeout(() => void cfg.lookup!(q).then(answer => {
      if (mine !== asked || !panel.isConnected) return;
      const found = Array.isArray(answer) ? answer : answer.choices;
      looked = {query: q, items: found.map((c, i) => ({c, index: 10000 + i, label: fold(c.label), extra: ''})), more: !Array.isArray(answer) && !!answer.more};
      cfg.choices.push(...found.filter(c => !cfg.choices.some(x => x.value === c.value)));  // so a pick can be read back
      draw();
    }).catch(() => { if (mine === asked && panel.isConnected) { lookFailed = q; draw(); } }), 150);
  };
  search.addEventListener('input', () => { lookUp(); draw(); });
  // Typing in the search or ticking inside the panel is not a change to the surrounding form.
  panel.addEventListener('input', e => e.stopPropagation());
  panel.addEventListener('change', e => e.stopPropagation());
  panel.addEventListener('focusout', onLeave);
  search.addEventListener('keydown', onKey);
  host.append(panel);
  if ('showPopover' in panel) { panel.setAttribute('popover', 'manual'); (panel as HTMLElement & {showPopover: () => void}).showPopover(); }
  if (cfg.query) lookUp();
  placer = placeNear(panel, anchor, {gap: 8, width: {min: 300}, room: [180, 420], enough: 260}, () => current?.anchor === anchor); draw();
  anchor.setAttribute('aria-expanded', 'true');
  window.addEventListener('keydown', onEscape, true); document.addEventListener('pointerdown', onDown, true);
  window.addEventListener('scroll', reposition, true);
  dialog?.addEventListener('cancel', keepDialog);
  current = {close: () => close(false), anchor};
  search.focus();
  return () => close(false);
}

/** Options of a native <select> as choices (the select stays the one place the value lives). */
function choicesOf(select: HTMLSelectElement): Choice[] {
  const out: Choice[] = [];
  for (const node of select.children) {
    const add = (o: HTMLOptionElement, group?: string, off = false) => { if (!o.hidden) out.push({value: o.value, label: o.textContent || o.value, group, hint: o.dataset.hint, divider: o.dataset.divider !== undefined, disabled: o.disabled || off}); };
    if (node instanceof HTMLOptionElement) add(node);
    else if (node instanceof HTMLOptGroupElement) for (const o of node.children) add(o as HTMLOptionElement, node.label, node.disabled);
  }
  return out;
}

const labelOf = (select: HTMLSelectElement) =>
  (select.id && document.querySelector(`label[for="${CSS.escape(select.id)}"]`)?.textContent?.trim()) || select.getAttribute('aria-label') || select.name || 'Choose';

function openFor(select: HTMLSelectElement, query = '') {
  if (select.disabled || select.multiple) return;
  const host = select.parentElement || document.body;
  openPicker(select, host, {label: labelOf(select), choices: choicesOf(select), multiple: false, selected: new Set([select.value]), query,
    onPick: value => { if (select.value === value) return; select.value = value; select.dispatchEvent(new Event('input', {bubbles: true})); select.dispatchEvent(new Event('change', {bubbles: true})); }});
}

// On a phone a short list is best in the system's own picker; everything else opens the searchable list.
const nativeHere = (select: HTMLSelectElement) => select.dataset.native !== undefined || (matchMedia('(pointer: coarse)').matches && phoneQuery.matches && select.options.length <= LONG_LIST);

// A select too narrow for its choice ends in "…" (CSS); pointing at it or focusing it shows the whole choice as a
// tooltip. The choice itself is already what a screen reader reads. Only a tooltip set here is ever removed.
let ruler: CanvasRenderingContext2D | null = null;
const NATIVE_ARROW = 24;
function selectClipped(s: HTMLSelectElement): boolean {
  const text = s.selectedOptions[0]?.textContent || '', cs = getComputedStyle(s);
  ruler ??= document.createElement('canvas').getContext('2d');
  if (!ruler || !text) return false;
  ruler.font = `${cs.fontStyle} ${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
  return ruler.measureText(text).width + parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight) + (cs.appearance === 'none' ? 0 : NATIVE_ARROW) > s.clientWidth;
}
function tipIfClipped(e: Event) {
  const s = e.target;
  if (!(s instanceof HTMLSelectElement) || (s.title && s.dataset.autoTip === undefined)) return;
  if (selectClipped(s)) { s.title = s.selectedOptions[0].textContent || ''; s.dataset.autoTip = ''; }
  else if (s.dataset.autoTip !== undefined) { s.removeAttribute('title'); delete s.dataset.autoTip; }
}

let installed = false;
/** Route every <select> in the page (present and future) through the picker. Call once at start. */
export function enhanceSelects() {
  if (installed) return; installed = true;
  for (const type of ['pointerover', 'focusin', 'change']) document.addEventListener(type, tipIfClipped, true);
  document.addEventListener('mousedown', e => {
    const s = e.target as HTMLElement;
    if (!(s instanceof HTMLSelectElement) || e.button !== 0 || nativeHere(s)) return;
    e.preventDefault(); s.focus();
    if (current?.anchor === s) { current.close(); return; }
    openFor(s);
  }, true);
  // Touch: phones and tablets open their own list on tap, not on mousedown, so take the tap as well.
  document.addEventListener('touchend', e => {
    const s = e.target as HTMLElement;
    if (!(s instanceof HTMLSelectElement) || nativeHere(s)) return;
    e.preventDefault(); s.focus();
    if (current?.anchor === s) { current.close(); return; }
    openFor(s);
  }, {capture: true, passive: false});
  document.addEventListener('keydown', e => {
    const s = e.target as HTMLElement;
    if (!(s instanceof HTMLSelectElement) || nativeHere(s) || e.ctrlKey || e.metaKey) return;
    const printable = e.key.length === 1 && e.key !== ' ' && !e.altKey;
    if (['Enter', ' ', 'ArrowDown', 'ArrowUp', 'F4'].includes(e.key) || printable) { e.preventDefault(); openFor(s, printable ? e.key : ''); }
  }, true);
}
