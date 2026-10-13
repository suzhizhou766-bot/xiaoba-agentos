import type { Shot } from './core';
export function exportShot(source:Shot):Shot {
  const s=structuredClone(source);
  const last=Math.max(0,...s.keys.map(k=>k.time));
  if(s.exportRange!=='manual'&&last>s.in)s.out=Math.min(s.out,last);
  if(s.scaleMotion!==true)for(const o of s.objects){
    const first=s.keys.filter(k=>k.target===o.id&&k.scale!==undefined).sort((a,b)=>a.time-b.time)[0];
    const scale=first?.scale??o.scale;o.scale=scale;
    for(const k of s.keys)if(k.target===o.id&&k.scale!==undefined)k.scale=scale;
  }
  return s;
}
export function exportTimes(s:Shot){return Array.from({length:3},(_,i)=>Math.round((s.in+(s.out-s.in)*(i+1)/4)*24)/24).filter((t,i,a)=>t>s.in&&t<s.out&&a.indexOf(t)===i);}
