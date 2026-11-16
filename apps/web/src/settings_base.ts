/** The record a settings form's shown values were read from, taken when the form is DRAWN: its save sends this
 *  revision, never one read again at the click. A background refresh replaces the account (app.ts refreshOnce) without
 *  redrawing a form in use, so a revision read at the click belongs to values the form never showed, and the server's
 *  stale check (services/common.py expected) would pass a save that overwrites someone else's change. A copy, so that
 *  neither a replaced nor a patched account moves it. No DOM (Node tests). */
export const drawnBase=(row:{id:string;revision:number})=>({id:String(row.id),revision:Number(row.revision)});
