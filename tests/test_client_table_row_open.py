"""Exercise the shared table's compiled row-open logic with controlled list reads."""
from .test_client_app_fragments import cut, node


def test_only_the_latest_row_still_drawn_opens_after_the_current_read(client_modules):
    source = (client_modules / 'table_view.js').read_text(encoding='utf-8')
    row_open = cut(source, r'(let pendingOpen = .*?)\n\s*const opener =')
    opened = node(r"""
const opened = [], root = {isConnected: true};
const first = {id:'first', title:'First'}, second = {id:'second', title:'Second'};
const account = '', tableAccount = account, rowMark = id => id;
let drawn = new Set([first.id, second.id]), loading = false;
const busy = () => loading;
const body = {setAttribute: (_, value) => { loading = value === 'true'; }, querySelector: id => drawn.has(id)};
const spec = {rows: [first, second], rowId: r => r.id, onOpen: (row, position) => opened.push([row.title, position])};
const drawnOrder = () => spec.rows.map(r => ({r}));
const positionOf = (_, index) => index;
""" + row_open + r"""
let finishOld, finishNew;
trackRead(new Promise(resolve => { finishOld = resolve; }));
loading = true;
openRow(first);
trackRead(new Promise(resolve => { finishNew = resolve; }));
openRow(second);
finishOld();
await Promise.resolve();
const whileReading = [loading, opened.length];

spec.rows[1] = {...second, title:'Updated'};
finishNew();
await Promise.resolve();
const afterRead = [loading, [...opened]];

let finishMissing;
trackRead(new Promise(resolve => { finishMissing = resolve; }));
loading = true;
openRow(second);
drawn.delete(second.id);  // still in the records, no longer in the drawn page
finishMissing();
await Promise.resolve();
console.log(JSON.stringify([whileReading, afterRead, [loading, opened]]));
""")
    assert opened == [[True, 0], [False, [['Updated', 1]]], [False, [['Updated', 1]]]]
