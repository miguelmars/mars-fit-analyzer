const API = window.location.origin;

function metric(l,v,u){const val=(v??'—');const long=String(val).length>7?' long':'';return `<div class="mini${long}"><div class="label">${l}</div><div class="value">${val}</div><div class="unit">${u||''}</div></div>`}
function row(i,t,s,v){return `<div class="row"><div class="r-ico">${i}</div><div class="r-main"><div class="r-title">${t||'—'}</div><div class="r-sub">${s||''}</div></div><div class="r-val">${v||''}</div></div>`}
function fmtDate(v){
  if(!v)return '—';
  try{
    const d=new Date((v+'').slice(0,10)+'T12:00:00');
    const M=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    return M[d.getMonth()]+' '+d.getDate()+', '+d.getFullYear();
  }catch(e){return (v+'').slice(0,10)}
}
function fmtShort(v){return fmtDate(v);}
function hms(sec){sec=parseInt(sec)||0;const h=Math.floor(sec/3600),m=Math.floor((sec%3600)/60);return h?`${h}h ${String(m).padStart(2,'0')}m`:`${m}m`}

// V8 UI — evidencia colapsable (ADR Insight-First: comprensión → evidencia → detalle)
function evd(title, innerHtml, open){
  if(!innerHtml) return '';
  return '<details class="evd"'+(open?' open':'')+'><summary>'+title+'</summary><div class="evd-body">'+innerHtml+'</div></details>';
}


// V8 — feedback de lecturas (afina heurísticas con el criterio del atleta)
async function fb(btn,context,verdict,ref){
  try{
    await fetch(API+'/api/feedback?context='+encodeURIComponent(context)+'&verdict='+verdict+(ref?'&ref='+encodeURIComponent(ref):''),{method:'POST'});
    const box=btn.parentElement;if(box)box.innerHTML='<span style="font-size:10px;color:var(--muted)">Thanks — this sharpens the reads.</span>';
  }catch(e){toast('Network error');}
}
function fbBtns(context,ref){
  return '<div style="display:flex;gap:8px;align-items:center;margin-top:8px">'+
    '<span style="font-size:10px;color:var(--muted)">Did this read ring true?</span>'+
    '<button onclick="fb(this,\''+context+'\',\'up\','+(ref?'\''+ref+'\'':'null')+')" style="background:none;border:1px solid var(--line);border-radius:8px;padding:2px 10px;font-size:13px;cursor:pointer">👍</button>'+
    '<button onclick="fb(this,\''+context+'\',\'down\','+(ref?'\''+ref+'\'':'null')+')" style="background:none;border:1px solid var(--line);border-radius:8px;padding:2px 10px;font-size:13px;cursor:pointer">👎</button>'+
  '</div>';
}


// V8 — el color de Epoch sigue tu fase de entrenamiento (identidad personal)
function setPhaseAccent(phase){
  const c={base:'#3dd68c',build:'#f59e0b',peak:'#a78bfa',taper:'#4a9eff'}[phase]||'#fb923c';
  document.documentElement.style.setProperty('--phase',c);
  return c;
}

// Bloque A: escape de texto libre antes de innerHTML (anti-XSS)
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]})}

// Bloque A: escape para valor dentro de string JS de un onclick="fn('VALUE')"
function escJs(s){return String(s==null?'':s).replace(/[\\'"<>&]/g,function(c){return '\\x'+c.charCodeAt(0).toString(16).padStart(2,'0')})}
