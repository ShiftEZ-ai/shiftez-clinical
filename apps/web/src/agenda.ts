import {get} from './api.js';
import {toLocal} from './dom.js';
import {addDays} from './planner_logic.js';
import type {Context} from './forms.js';
import type {Row} from './types.js';

export type Upcoming={duties:Row[];pairs:Record<string,string>};

/** The signed-in person's next published duties (Home): the next 14 days, else the three months after them read together
 *  (H-22: at most two round trips, not four in sequence; /api/calendar reads up to 31 days at a time, so one read needs a
 *  "next duties" read on the server). Page-scoped: leaving Home aborts them. */
export async function upcoming(ctx:Context):Promise<Upcoming>{
  const me=ctx.me.person_id!,now=Date.now(),today=toLocal(new Date().toISOString(),ctx.me.organisation.timezone).slice(0,10);
  const read=async(start:string,end:string):Promise<Upcoming>=>{
    const data=await get<Row>('/api/calendar?'+new URLSearchParams({start,end,person_id:me}));
    return {duties:(data.events as Row[]).filter(e=>e.kind==='duty'&&e.person_id===me&&!e.cancelled&&Date.parse(e.end)>now)
      .sort((a,b)=>a.start.localeCompare(b.start)),pairs:data.pair_names||{}};
  };
  const soon=await read(today,addDays(today,14));
  if(soon.duties.length)return soon;
  // Read together, judged in date order: the first window with a duty wins even if a later one failed; an earlier failure is
  // still reported (a duty might be hiding in it).
  for(const w of await Promise.allSettled([0,1,2].map(step=>read(addDays(today,14+31*step),addDays(today,14+31*(step+1)))))){
    if(w.status==='rejected')throw w.reason;
    if(w.value.duties.length)return w.value;
  }
  return soon;
}
