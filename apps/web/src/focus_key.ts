/** A focused control as app.ts keepFocus remembers it across a redraw: what it is (role, else element and input type),
 *  what it is called (its accessible name) and the row it sits in (a stable id such as data-shift; '' outside one). */
export type ControlKey={kind:string;name:string;row:string};
/** Which of `found` is the control `had` (its index, or -1): the same kind and name; where that name repeats, the one
 *  in the same row. A repeated name is never guessed, in a row or outside one (-1: focus is not moved). */
export function sameControl(had:ControlKey,found:readonly ControlKey[]):number{
  const same=found.flatMap((k,i)=>k.kind===had.kind&&k.name===had.name?[i]:[]);
  const inRow=had.row?same.filter(i=>found[i].row===had.row):[];
  return inRow[0]??(same.length===1?same[0]:-1);
}
