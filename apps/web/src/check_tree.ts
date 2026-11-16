/** Tri-state checkboxes: a box over several others is ticked when all are, empty when none are and mixed otherwise
 *  (`indeterminate` plus aria-checked="mixed"). Line → rule → group → header select-all all roll up the same way. */
export type TriState='on'|'off'|'mixed';

/** Show a state on a native checkbox (aria-checked only while mixed: the native state speaks for itself otherwise). */
export function setTriState(box:HTMLInputElement,state:TriState){
  box.checked=state==='on';box.indeterminate=state==='mixed';
  if(state==='mixed')box.setAttribute('aria-checked','mixed');else box.removeAttribute('aria-checked');
}
/** The state of a box over `on` ticked children out of `total` (any child mixed makes it mixed). */
export const triStateOf=(on:number,total:number,mixed=false):TriState=>mixed||(on>0&&on<total)?'mixed':total>0&&on===total?'on':'off';

const kids=new WeakMap<HTMLInputElement,HTMLInputElement[]>(),parentOf=new WeakMap<HTMLInputElement,HTMLInputElement>();
let cascading=false;
function rollup(box:HTMLInputElement){
  const c=kids.get(box);if(!c)return;
  let on=0,mixed=false;for(const k of c){if(k.indeterminate)mixed=true;else if(k.checked)on++;}
  setTriState(box,triStateOf(on,c.length,mixed));
  const up=parentOf.get(box);if(up)rollup(up);
}
function setAll(box:HTMLInputElement,value:boolean){
  for(const k of kids.get(box)||[]){
    if(kids.has(k)){setAll(k,value);setTriState(k,value?'on':'off');continue;}
    // Leaves announce the change like a click would, so whatever keeps the selection (a Set) hears it.
    if(k.checked!==value||k.indeterminate){k.checked=value;k.indeterminate=false;k.dispatchEvent(new Event('change',{bubbles:true}));}
  }
}
/**
 * Ties `parent` to its `children` (which may be parents themselves): ticking the parent ticks every leaf below it,
 * ticking a child rolls the state up to the top. Leaves fire their own `change`, so a selection Set stays the source of
 * truth. Call again after a redraw with the new boxes; returns a refresh for changes made in code.
 */
export function checkTree(parent:HTMLInputElement,children:HTMLInputElement[]):()=>void{
  kids.set(parent,children);for(const c of children)parentOf.set(c,parent);
  if(!parent.dataset.tree){parent.dataset.tree='1';
    parent.addEventListener('change',()=>{const was=cascading;cascading=true;
      try{setAll(parent,parent.checked);}finally{cascading=was;}rollup(parent);});}
  for(const c of children)if(!c.dataset.treeChild){c.dataset.treeChild='1';
    c.addEventListener('change',()=>{if(cascading)return;const up=parentOf.get(c);if(up)rollup(up);});}
  rollup(parent);
  return ()=>rollup(parent);
}
