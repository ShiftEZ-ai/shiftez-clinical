/** The account last signed in on this device, for the sign-in page's "Continue as <name>": a name and a username,
 *  never a password or session. Kept until the person removes it on the sign-in page (shared computers), or for 14 days
 *  after the last sign-in here (SEC-10). */
const KEY='rota.account',KEPT_MS=14*864e5;
export interface KnownAccount {identity: string; name: string; username: string}

let typed='';
/** The username of a password sign-in in progress: kept only if that sign-in opens the app. */
export function signingInAs(username:string){typed=username.trim();}

export function rememberedAccount():KnownAccount|null{
  try{const v=JSON.parse(localStorage.getItem(KEY)||'null');
    // Older entries carry no date: they are kept until the next sign-in dates them.
    if(v&&typeof v.at==='number'&&Date.now()-v.at>KEPT_MS){localStorage.removeItem(KEY);return null;}
    return v&&typeof v.name==='string'&&v.name?{identity:String(v.identity||''),name:v.name,username:String(v.username||'')}:null;}
  catch{return null;/* storage unavailable: nothing remembered */}
}
/** After the app opens: the signed-in person, with the username this app manages, else the one just typed, else the
 *  one already remembered for the same identity. */
export function rememberAccount(identity:string,name:string,username?:string|null){
  const known=rememberedAccount();
  if(known?.identity&&known.identity!==identity)forgetDeviceMemory();  // another account on this device starts afresh
  const value={identity,name,username:username||typed||(known?.identity===identity?known.username:'')};typed='';
  try{localStorage.setItem(KEY,JSON.stringify({...value,at:Date.now()}));}catch{/* not kept */}
}
/** An invitation or a password reset just finished on this device (maintenance): that account is the one sign-in offers,
 *  replacing any other remembered here, whose device memory goes with it. A name and a username only; its identity
 *  is kept only when it is the same account, and the sign-in that follows records it. */
export function accountSetUp(name:string,username:string){
  const known=rememberedAccount(),same=!!known&&known.username===username;
  if(known&&!same)forgetDeviceMemory();
  try{localStorage.setItem(KEY,JSON.stringify({identity:same?known!.identity:'',name,username,at:Date.now()}));}catch{/* not kept */}
}
export function forgetAccount(){try{localStorage.removeItem(KEY);}catch{/* nothing kept */}forgetDeviceMemory();}
/** Everything else this device keeps for an account (rota.* keys: views, layouts, recent picks, panel widths), forgotten
 *  at sign-out and when another account signs in, so none of it reaches the next person (LIVE-50). */
export function forgetDeviceMemory(){try{for(const k of Object.keys(localStorage))if(k.startsWith('rota.')&&k!==KEY)localStorage.removeItem(k);}catch{/* nothing kept */}}
