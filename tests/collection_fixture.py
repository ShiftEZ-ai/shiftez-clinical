"""A fictional list drawn by the one shared collection (table_view.ts dataTable), mounted on a signed-in page, so its
toolbar, filter, cards, paging and drawing cost are tested on the component itself rather than through one screen."""
from playwright.sync_api import expect

MOUNT = """async (o) => {
  const {dataTable} = await window.importBundle('table_view.js');
  const {el} = await window.importBundle('dom.js');
  const rowsOf = (from, n) => Array.from({length: n}, (_, i) => {const k = from + i;
    return {id: 'r' + k, name: 'Fictional row ' + String(k).padStart(4, '0'), status: k % 3 ? 'Active' : 'Inactive', tag: k % 2 ? '' : 'Tagged', rank: (k * 7919) % 1000};});
  const rows = rowsOf(0, o.rows);
  window.fixture = {rows, more: n => rows.push(...rowsOf(rows.length, n)), sortCalls: 0, searches: [], draws: 0};
  const spec = {id: o.id, noun: ['row', 'rows'], rows, rowId: r => r.id, pageSize: o.pageSize || 50,
    defaults: o.defaults || {},
    columns: [
      {key: 'name', label: 'Name', text: r => r.name, cell: r => r.name},
      {key: 'status', label: 'Status', text: r => r.status, facet: r => r.status, cell: r => r.status},
      {key: 'tag', label: 'Tag', text: r => r.tag, facet: r => r.tag || [], cell: r => r.tag || '—'},
      {key: 'rank', label: 'Rank', num: true, sort: r => {window.fixture.sortCalls++; return r.rank;}, cell: r => String(r.rank)},
    ]};
  if (o.views) spec.views = () => o.views;
  if (o.bulk) spec.bulk = () => [{label: 'Mark', icon: 'check', run: () => undefined}];
  if (o.serverSearch) spec.onSearch = q => {window.fixture.searches.push(q);
    setTimeout(() => {const all = rowsOf(0, o.rows); rows.splice(0, rows.length, ...all.filter(r => r.name.toLowerCase().includes(q.toLowerCase()))); table.refresh(true);}, 150);};
  // A server search that fails (the page reports it): its promise rejects and nothing is drawn again.
  if (o.serverSearch === 'fails') spec.onSearch = q => {window.fixture.searches.push(q);
    return new Promise((_, no) => {window.fixture.failSearch = () => no(new Error('Fictional search failure'));}).catch(e => {window.fixture.failed = e.message; throw e;});};
  const table = dataTable(spec);
  window.fixture.table = table;
  const host = el('section', {id: 'fixture', class: 'panel'}, table.root);
  document.querySelector('#main').prepend(host);
}"""


def mount(page, list_id, rows=12, **options):
    """Mount the fixture list on the current page (its own id keeps its view state apart from real lists)."""
    page.evaluate(MOUNT, {'id': list_id, 'rows': rows, **options})
    fixture = page.locator('#fixture')
    expect(fixture.locator('.dt-body')).to_be_visible()
    return fixture
