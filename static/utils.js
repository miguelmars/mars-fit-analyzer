const API = window.location.origin;

function metric(l,v,u){const val=(v??'—');const long=String(val).length>7?' long':'';return `<div class="mini${long}"><div class="label">${l}</div><div class="value">${val}</div><div class="unit">${u||''}</div></div>`}
function row(i,t,s,v){return `<div class="row"><div class="r-ico">${i}</div><div class="r-main"><div class="r-title">${t||'—'}</div><div class="r-sub">${s||''}</div></div><div class="r-val">${v||''}</div></div>`}
function fmtDate(v){
  if(!v)return '—';
  try{
    const d=new Date((v+'').slice(0,10)+'T12:00:00');
    const M=['enero','febrero','marzo','abril','mayo','junio','julio','agosto','septiembre','octubre','noviembre','diciembre'];
    return d.getDate()+' '+M[d.getMonth()]+' '+d.getFullYear();
  }catch(e){return (v+'').slice(0,10)}
}
function fmtShort(v){return fmtDate(v);}
function hms(sec){sec=parseInt(sec)||0;const h=Math.floor(sec/3600),m=Math.floor((sec%3600)/60);return h?`${h}h ${String(m).padStart(2,'0')}m`:`${m}m`}
