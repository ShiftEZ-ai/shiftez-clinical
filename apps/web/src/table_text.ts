// Tab-separated text as spreadsheets put it on the clipboard (Excel's quoting), read and written by one pair of
// functions so a Sheet copy pastes back exactly. No DOM: tests run it in Node.

/** A pasted block as rows of cells: Excel copies tab-separated text, a CSV uses commas; "quoted" cells may hold the
 *  separator, line breaks and "" for a quote. Blank lines are dropped.
 *  `grid`: pasted into cells by position (Excel's clipboard): only tabs split columns (a comma stays in its cell) and a
 *  blank row keeps its place; one line break ends the last row (it is not a blank row of its own). */
export function parseTable(text:string,grid=false):string[][]{
  const sep=grid||text.includes('\t')?'\t':',',rows:string[][]=[];
  // `has`: a cell has begun (a character or a quote), so a quoted-empty last cell ("") still counts.
  let row:string[]=[],cell='',quoted=false,has=false;
  for(let i=0;i<text.length;i++){const ch=text[i];
    if(quoted){if(ch!=='"')cell+=ch;else if(text[i+1]==='"'){cell+='"';i++;}else quoted=false;continue;}
    if(ch==='"'&&!has){quoted=true;has=true;}
    else if(ch===sep){row.push(cell);cell='';has=false;}
    else if(ch==='\n'||ch==='\r'){if(ch==='\r'&&text[i+1]==='\n')i++;row.push(cell);rows.push(row);row=[];cell='';has=false;}
    else{cell+=ch;has=true;}}
  if(has||row.length){row.push(cell);rows.push(row);}
  return grid?rows:rows.filter(r=>r.some(c=>c.trim()));
}

/** Rows of cells as Excel copies them: tab between cells, a line break after every row (so a blank last row and an
 *  empty single cell survive), and a cell holding a tab, line break or quote "quoted" with "" for a quote. */
export const toTable=(rows:string[][])=>rows.map(r=>r.map(c=>/[\t\r\n"]/.test(c)?'"'+c.replace(/"/g,'""')+'"':c).join('\t')+'\n').join('');

/** Several values in one cell ("#student, North"), as the Sheet shows, copies and reads them: comma separated, and a
 *  value holding a comma, semicolon or quote is "quoted" ("" for a quote), so a tag named "North, East" stays one. */
export const joinValues=(values:string[])=>values.map(v=>/[,;"]/.test(v)?'"'+v.replace(/"/g,'""')+'"':v).join(', ');
export function splitValues(text:string):string[]{
  const out:string[]=[];let cur='',quoted=false,has=false;
  for(let i=0;i<text.length;i++){const ch=text[i];
    if(quoted){if(ch!=='"')cur+=ch;else if(text[i+1]==='"'){cur+='"';i++;}else quoted=false;continue;}
    if(ch==='"'&&!cur.trim()){quoted=true;has=true;cur='';}
    else if(ch===','||ch===';'){out.push(cur.trim());cur='';has=false;}
    else{cur+=ch;if(ch.trim())has=true;}}
  if(has||cur.trim())out.push(cur.trim());
  return out.filter(Boolean);
}
