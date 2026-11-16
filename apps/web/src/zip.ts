/** A ZIP of files stored as they are (no compression: PNGs are compressed already), with no library (CSP and the
 *  no-dependency rule). PKZIP APPNOTE 6.3.x: a local header and the bytes per file, then the central directory and its
 *  end record; names are UTF-8 (general-purpose bit 11), CRC-32 over each file. tests/test_web_zip.py reads the result
 *  with Python's zipfile and checks every CRC.
 *  shortcut: no ZIP64, so at most 65,535 files and 4 GB in all; a Schedule export is far below either. */
const CRC=(()=>{const t=new Uint32Array(256);for(let n=0;n<256;n++){let c=n;for(let k=0;k<8;k++)c=c&1?0xEDB88320^(c>>>1):c>>>1;t[n]=c>>>0;}return t;})();
export function crc32(data:Uint8Array):number{
  let c=0xFFFFFFFF;for(let i=0;i<data.length;i++)c=CRC[(c^data[i])&0xFF]^(c>>>8);
  return (c^0xFFFFFFFF)>>>0;
}
/** The ZIP's bytes for `files`, in their order; `when` dates every entry (local time, as ZIP stores it). */
export function zipStore(files:{name:string;data:Uint8Array}[],when=new Date()):Uint8Array<ArrayBuffer>{
  if(files.length>0xFFFF)throw new Error('Too many files for one ZIP.');
  const enc=new TextEncoder(),time=(when.getHours()<<11)|(when.getMinutes()<<5)|(when.getSeconds()>>1);
  const date=((Math.max(1980,when.getFullYear())-1980)<<9)|((when.getMonth()+1)<<5)|when.getDate();
  const parts:Uint8Array[]=[],central:Uint8Array[]=[];let offset=0;
  for(const f of files){
    const name=enc.encode(f.name),crc=crc32(f.data),size=f.data.length;
    if(offset+size>0xFFFFFFFF)throw new Error('The files are too large for one ZIP. Save fewer pages.');
    const local=new Uint8Array(30+name.length),l=new DataView(local.buffer);
    l.setUint32(0,0x04034b50,true);l.setUint16(4,20,true);l.setUint16(6,0x0800,true);l.setUint16(8,0,true);
    l.setUint16(10,time,true);l.setUint16(12,date,true);l.setUint32(14,crc,true);l.setUint32(18,size,true);l.setUint32(22,size,true);
    l.setUint16(26,name.length,true);l.setUint16(28,0,true);local.set(name,30);
    const dir=new Uint8Array(46+name.length),d=new DataView(dir.buffer);
    d.setUint32(0,0x02014b50,true);d.setUint16(4,20,true);d.setUint16(6,20,true);d.setUint16(8,0x0800,true);d.setUint16(10,0,true);
    d.setUint16(12,time,true);d.setUint16(14,date,true);d.setUint32(16,crc,true);d.setUint32(20,size,true);d.setUint32(24,size,true);
    d.setUint16(28,name.length,true);d.setUint32(42,offset,true);dir.set(name,46);  // extra, comment, disk, attributes: 0
    parts.push(local,f.data);central.push(dir);offset+=local.length+size;
  }
  const dirSize=central.reduce((n,c)=>n+c.length,0),end=new Uint8Array(22),e=new DataView(end.buffer);
  e.setUint32(0,0x06054b50,true);e.setUint16(8,files.length,true);e.setUint16(10,files.length,true);e.setUint32(12,dirSize,true);e.setUint32(16,offset,true);
  const out=new Uint8Array(offset+dirSize+22);let at=0;
  for(const p of [...parts,...central,end]){out.set(p,at);at+=p.length;}
  return out;
}
