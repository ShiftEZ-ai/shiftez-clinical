/** The Schedule's dimensions: one list for every axis (Rows, categorical Columns, Group by, Then by) and one reader of a
 *  dimension's values for a session, or for one person's duty on it on that day. The grid, View settings and the
 *  toolbar's Group all read this list (PlannerViewSettings rows / columns / group_by / subgroup). */
import {labelOf,formatTime,departmentTimeZone} from './dom.js';
import {activeOn} from './forms.js';
import type {Row} from './types.js';
import {PLANNER_DIMENSIONS,PLANNER_TIME_AXES,type PlannerViewSettings} from './generated.js';

// The contract names the dimensions (PlannerViewSettings rows / columns); NONE below must name each one.
export type Dim=Exclude<NonNullable<PlannerViewSettings['rows']>,'none'>;
export type Column=NonNullable<PlannerViewSettings['columns']>;
export type TimeAxis=Exclude<Column,Dim>;
/** The contract's dimensions and date axes (generated PLANNER_DIMENSIONS / PLANNER_TIME_AXES, one list with the server).
 *  `person`: read from the person on the duty, as on that day (their rank, programme, tags). */
export const DIMENSIONS:readonly {key:Dim;label:string;one:string;many:string;person:boolean;empty:string}[]=PLANNER_DIMENSIONS;
export const TIME_AXES=PLANNER_TIME_AXES as unknown as [TimeAxis,string][];
export const isDim=(k:unknown):k is Dim=>DIMENSIONS.some(d=>d.key===k);
export const isTime=(k:unknown):k is TimeAxis=>TIME_AXES.some(([t])=>t===k);
export const dimOf=(k:Dim)=>DIMENSIONS.find(d=>d.key===k)!;
export const personDim=(k:string)=>isDim(k)&&dimOf(k).person;
const NONE=Object.fromEntries(DIMENSIONS.map(d=>[d.key,d.empty])) as Record<Dim,string>;
/** "No …" values sort last; the rest by name (`way` −1: Z → A, a group level's own order). */
export const byValue=(a:string,b:string,way=1)=>Number(a.startsWith('No '))-Number(b.startsWith('No '))||way*a.localeCompare(b);

/** A session's duty time: its own start–end in the department's zone ("07:00–15:00"), never a named part of the day.
 *  The 24-hour clock sorts by start, then end, as text. */
export const dutyTime=(s:Row,zone=departmentTimeZone())=>`${formatTime(s.start,zone)}–${formatTime(s.end,zone)}`;
/** The values of `dim` for session `s` (and person `pid` when the dimension is a person's) on `day`. A session with two
 *  services is one "A + B" value (counted once); a person with two tags is in both. */
export function dimensionReader(catalogues:Record<string,Row[]>,name:(p:Row|undefined)=>string){
  const index=(kind:string)=>new Map((catalogues[kind]||[]).map(r=>[r.id as string,r]));
  const people=index('people'),services=index('services'),locations=index('locations'),categories=index('categories'),programmes=index('programmes');
  const byPerson=(kind:string)=>{const m=new Map<string,Row[]>();for(const r of catalogues[kind]||[])m.set(r.person_id,[...(m.get(r.person_id)||[]),r]);return m;};
  const ranks=byPerson('memberships'),members=byPerson('programme_memberships');
  return (dim:Dim,s:Row,pid:string,day:string):string[]=>{
    const out=dim==='type'?[String(s.type_snapshot?.name||'')]
      :dim==='service'?[((s.service_ids||[]) as string[]).map(id=>labelOf(services.get(id))).sort().join(' + ')]
      :dim==='location'?[labelOf(locations.get(s.location_id))]
      :dim==='time'?[dutyTime(s)]
      :dim==='person'?[pid?name(people.get(pid)):'']
      :dim==='rank'?(ranks.get(pid)||[]).filter(m=>activeOn(m,day)).map(m=>labelOf(categories.get(m.category_id)))
      :dim==='programme'?(members.get(pid)||[]).filter(m=>activeOn(m,day)).map(m=>labelOf(programmes.get(m.programme_id)))
      :(people.get(pid)?.tags||[]) as string[];
    const kept=[...new Set(out.filter(Boolean))];
    return kept.length?kept:[NONE[dim]];
  };
}
/** Each person's active person tags (Configuration › Tags records and their member links), built once per draw so a chip
 *  mark or Colour by tag never scans every link. Archived tags and tags that do not label people are left out. */
export function personTags(catalogues:Record<string,Row[]|undefined>):Map<string,Row[]>{
  const tags=new Map((catalogues.tags||[]).filter(t=>t.active!==false&&((t.labels||[]) as string[]).includes('people')).map(t=>[t.id as string,t]));
  const out=new Map<string,Row[]>();
  for(const m of catalogues.tag_members||[]){const t=m.kind==='people'?tags.get(m.tag_id):undefined;if(!t)continue;
    const held=out.get(m.member_id);if(!held)out.set(m.member_id,[t]);else if(!held.includes(t))held.push(t);}
  for(const held of out.values())held.sort((a,b)=>String(a.name).localeCompare(String(b.name)));
  return out;
}
/** Colour by tag (PlannerViewSettings.colour_tags): the first tag in the viewer's chosen order that the person holds.
 *  None of them held is undefined, drawn neutral: never the person's first tag by chance. */
export function priorityTag(held:readonly Row[]|undefined,order:readonly string[]):Row|undefined{
  if(!held?.length)return undefined;
  for(const id of order){const t=held.find(x=>x.id===id);if(t)return t;}
  return undefined;
}
