import {bidi,el,type Legend} from './dom.js';
import {issueMarker,openMarkerFrom,type Issue} from './issue_marker.js';
import {spokenName,narrowNames} from './person_label.js';

/** The one person pill reviewed: every person on every surface (Week, Month, Day
 *  bar, session panel, agenda, Home, My duties, person card, published views) is this element, sized by the pill
 *  tokens (--pill-*): the duty's soft tint, the label from personLabel (a rank only where the label adds it), the
 *  supervisor in weight 600, and the person's own issue marker at its end. Only the density differs (the
 *  surrounding .compact / .mo grid or team). One line: the name ends in …, the full name in its tip and name. */
export type PillOptions={
  /** The shown label (personLabel's words). */label:string;
  /** The full name (tip and accessible name) when the label is shorter. */full?:string;
  sup?:boolean;
  /** The duty hue (duty-* class); unset, the pill takes its surroundings' tint. */hue?:string;
  issues?:readonly Issue[];
  /** The marker's Fix / Why. */act?:()=>void;
  /** The issues as they are when the marker opens (issue_marker.ts MarkerOptions.now). */now?:()=>readonly Issue[];
  /** A control to dress as the pill, keeping its listeners and accessible name (a Week chip, a personLink). Unset:
   *  a drawn pill (a span), for places where the person is not a control of their own. */base?:HTMLElement;
  /** Marks after the name (team lead, overtime, wish, the ⋯ menu): drawn before the issue marker. */extra?:readonly (Node|null|undefined|'')[];
  /** The view's key: the issue marker is drawn as its entry (dom.ts legend; every mark has one). */key?:Legend;
  /** The person's id: in a cell too narrow for the label, the pill shows their short form, then their code (L7). */person?:string;
};
export function personPill(o:PillOptions):HTMLElement{
  const full=o.full||o.label,issues=o.issues||[],p=o.base||el('span',{});
  p.classList.add('pchip');if(o.hue)p.classList.add('duty-'+o.hue);if(o.sup)p.classList.add('sup');
  const kids:Node[]=[];
  // Narrower forms, shown by the cell's width (app.css @container pill), never the window's: the label stays the
  // pill's name and the full name its tip and accessible name, whichever form is shown.
  const fit=o.person?narrowNames(o.person,o.label):{narrow:'',tight:''},alt=!!(fit.narrow||fit.tight);
  kids.push(bidi(o.label,{class:'pchip-name'}));
  if(fit.narrow)kids.push(bidi(fit.narrow,{class:'pchip-alt pl-narrow'}));
  if(fit.tight)kids.push(bidi(fit.tight,{class:'pchip-alt pl-tight'}));
  for(const x of o.extra||[])if(x)kids.push(x);
  const mark=issueMarker(issues,{title:full,inside:true,act:o.act,now:o.now,key:o.key});if(mark)kids.push(mark);
  p.replaceChildren(...kids);
  const control=p instanceof HTMLButtonElement||p instanceof HTMLAnchorElement;
  // A control names itself: its own name if it has one, else the label then the full name (WCAG 2.5.3).
  if(control&&!p.hasAttribute('aria-label')&&(full!==o.label||alt))p.setAttribute('aria-label',spokenName(o.label,full));
  if((full!==o.label||alt)&&!p.dataset.tip)p.dataset.tip=full;
  if(control&&mark){openMarkerFrom(p);}
  return p;
}
