const THEME={home:'#c8cbd2',perfil:'#3dd68c',coach:'#4a9eff',progress:'#a78bfa',capacidades:'#22d3ee',dashboard:'#e8593c',activities:'#e8593c',gear:'#f59e0b',calendar:'#22d3ee',performance:'#a78bfa',fuerza:'#c8f135',wellness:'#4a9eff',eficiencia:'#3dd68c',correlaciones:'#a78bfa',nutricion:'#f59e0b',metas:'#fb923c'};
const TITLE={home:['Epoch','Home'],perfil:['Perfil','sistema'],coach:['Coach','recomendacion'],progress:['Evolución','atlética'],capacidades:['Capacidades','atléticas'],dashboard:['Dashboard','stats'],activities:['Data','sesiones'],gear:['Gear','mantenimiento'],calendar:['Calendario','heatmap'],performance:['Récords','personales'],fuerza:['Fuerza','Compex'],wellness:['Wellness','recuperación'],eficiencia:['Eficiencia','aeróbica'],correlaciones:['Correlaciones','FC · Peso'],nutricion:['Nutrición','geles'],metas:['Metas','activas']};
let current='home';
let navStack=[];
let progressMode='general';
function $(id){return document.getElementById(id)}
function setTheme(s){document.documentElement.style.setProperty('--theme',THEME[s]||'#fff');$('kicker').textContent=TITLE[s][0];$('title').textContent=TITLE[s][1]}
function updateBack(){const b=$('backBtn');if(!b)return;b.title=current==='home'?'Inicio':'Regresar';b.style.visibility=current==='home'?'hidden':'visible'}
function appPathFor(s){return s==='home'?'/home':'/'+s}
function go(s,push=true){
  if(!TITLE[s])s='home';
  if(push&&current&&current!==s)navStack.push(current);
  current=s;
  setTheme(s);
  document.querySelectorAll('.screen').forEach(x=>x.classList.toggle('active',x.id==='s-'+s));
  document.querySelectorAll('.nav').forEach(x=>x.classList.toggle('active',x.dataset.s===s));
  updateBack();
  if(push){
    const path=appPathFor(s);
    if(location.pathname!==path)history.pushState({screen:s},'',path);
  }
  return load(s);
}
function appBack(){
  const b=$('backBtn');
  if(b){b.disabled=true;b.style.transform='translateX(-2px)';setTimeout(()=>{b.disabled=false;b.style.transform=''},180)}
  const prev=navStack.pop();
  if(prev){go(prev,false);return}
  if(current!=='home'){go('home',false);return}
  if(history.length>1){history.back();return}
  toast('Inicio');
}
window.addEventListener('popstate',()=>{
  const s=screenFromPath();
  current=s;
  setTheme(s);
  document.querySelectorAll('.screen').forEach(x=>x.classList.toggle('active',x.id==='s-'+s));
  document.querySelectorAll('.nav').forEach(x=>x.classList.toggle('active',x.dataset.s===s));
  updateBack();
  load(s);
});
// Auto-refresh cuando la app vuelve del background (iOS)
let _lastLoadTime=0;
document.addEventListener('visibilitychange',()=>{
  if(!document.hidden&&current&&(Date.now()-_lastLoadTime>120000)){
    load(current);
    _lastLoadTime=Date.now();
  }
});
window.addEventListener('focus',()=>{
  if(current&&(Date.now()-_lastLoadTime>120000)){
    load(current);
    _lastLoadTime=Date.now();
  }
});
function toast(m){const t=$('toast');t.textContent=m;t.style.display='block';setTimeout(()=>t.style.display='none',2400)}
function date(v){return fmtDate(v)}
// Manejo global de errores: nunca dejar pantalla muda
window.addEventListener('error',e=>{console.error('Epoch error:',e.message);});
window.addEventListener('unhandledrejection',e=>{console.error('Epoch promise:',e.reason);});
// Watchdog: si una pantalla sigue en "Cargando..." tras 15s, ofrecer reintento
const _SCREEN_EL={home:'home-data',dashboard:'dash-data',activities:'act-list',gear:'gear-data',calendar:'cal-data',performance:'perf-data',fuerza:'fuerza-data',wellness:'well-data',eficiencia:'eff-data',correlaciones:'corr-data',nutricion:'nutri-summary',perfil:'perfil-data',coach:'coach-data',capacidades:'cap-data',metas:'metas-data',progress:'progress-data'};
function _watchdog(s){
  const id=_SCREEN_EL[s];if(!id)return;
  setTimeout(()=>{
    const el=$(id);
    if(el&&el.querySelector&&el.querySelector('.spin')){
      el.innerHTML='<div class="card" style="text-align:center;color:var(--muted);font-size:13px">No se pudo cargar esta pantalla.<br><button class="btn btn2" style="margin-top:10px;width:auto;padding:10px 20px" onclick="load(\''+s+'\')">Reintentar</button></div>';
    }
  },15000);
}
async function load(s){
  _watchdog(s);
  try{
    if(s==='home')return await loadHome();if(s==='dashboard')return await loadDash();if(s==='activities')return loadActs();if(s==='gear')return await loadGear();if(s==='calendar')return await loadCal();if(s==='performance')return await loadPerf();if(s==='fuerza')return await loadFuerza();if(s==='wellness')return await loadWell();if(s==='eficiencia')return await loadEficiencia();if(s==='correlaciones')return await loadCorrelaciones();if(s==='nutricion')return await loadNutricion();if(s==='perfil'){loadPerfil();loadStravaStatus();return;}if(s==='coach')return await loadCoach();if(s==='capacidades')return await loadCapacidades();if(s==='metas')return await loadMetas();if(s==='progress')return await loadProgress(progressMode);
  }catch(e){
    const id=_SCREEN_EL[s];const el=id&&$(id);
    if(el)el.innerHTML='<div class="card" style="color:var(--muted)">Error: '+(e.message||e)+'</div>';
  }
}

async function loadDash(){
  try{
    const d=await fetch(API+'/gpt/dashboard').then(r=>r.json());
    const a=d.athlete||{},s=d.semana_actual||{},c=d.carga||{},z=d.z2_check||{},ca=d.calorias_audit||{};
    const icoKm='<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><circle cx="7" cy="7" r="5" stroke="currentColor" stroke-width="1.5"/><path d="M5 7h4M9 7l-2-2M9 7l-2 2" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    const icoTime='<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><circle cx="7" cy="7" r="5" stroke="currentColor" stroke-width="1.5"/><path d="M7 4v3l2 1.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>';
    const icoCal='<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M7 2c0 0-4 3.5-4 6.5a4 4 0 008 0C11 5.5 7 2 7 2z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>';
    const calories = Number(s.calorias||0) > 0 ? Number(s.calorias).toFixed(0) : 'sin dato';
    $('dash-data').innerHTML=`<div class="grid2">${metric('Fitness',a.fitness||'—','Índice Epoch '+(a.mars_index||'—'))}${metric('Fatiga',a.fatiga||'—','TSB '+(c.tsb||0))}${metric('Carga',c.estado||'—','actual')}${metric('Z2',Number(z.pct_z2_4_semanas||0).toFixed(1)+'%','4 semanas')}</div><div class="card"><div class="head"><h3>Semana actual</h3><span>${s.sesiones||0} sesiones</span></div>${row(icoKm,'Distancia semanal','Acumulado Garmin',Number(s.km||0).toFixed(1)+' km')}${row(icoTime,'Tiempo semanal','Horas de carga',Number(s.horas||0).toFixed(1)+' h')}${row(icoCal,'Calorías','Garmin export',calories)}</div><div class="card"><div class="head"><h3>Calorías históricas</h3><span>${ca.validacion==='ok'?'OK':'revisar'}</span></div>${row(icoCal,'Total histórico','Capa clean_sessions',Number(ca.calorias_historicas||0).toLocaleString('es-MX')+' kcal')}${row('', 'Promedio kcal/km', ca.nota||'', (ca.kcal_por_km_promedio||'—'))}</div>`;
  }catch(e){
    $('dash-data').innerHTML='<div class="card">Error: '+e.message+'</div>';
  }
}
// ── Historial Strava (pantalla principal de actividades — Nivel 2) ────────────
let _stravaOffset=0,_stravaSport=null;
function loadActs(){_stravaSport=null;_stravaOffset=0;loadStravaActs();}
async function loadStravaActs(reset=true){
  if(reset)_stravaOffset=0;
  const url=API+'/api/strava/activities?limit=20&offset='+_stravaOffset+(_stravaSport?'&sport='+encodeURIComponent(_stravaSport):'');
  $('act-list').innerHTML='<div class="loading"><span class="spin"></span>Cargando...</div>';
  try{
    const d=await fetch(url).then(r=>r.json());
    const arr=d.activities||[];
    const total=d.total||0;
    $('act-count').textContent=(total).toLocaleString()+' actividades';
    const h1Act=document.querySelector('#s-activities h1');
    if(h1Act&&total>0)h1Act.innerHTML='Tus '+total.toLocaleString()+'<br>actividades.';
    const sports=[{k:null,l:'Todo'},{k:'Ride',l:'🚴 Bici'},{k:'Run',l:'🏃 Correr'},{k:'VirtualRide',l:'⚡ Virtual'},{k:'Walk',l:'🚶 Caminar'},{k:'Swim',l:'🏊 Nadar'}];
    const filterBar='<div style="display:flex;gap:6px;flex-wrap:wrap;padding:0 0 10px">'+
      sports.map(sp=>{
        const act=_stravaSport===sp.k;
        return `<button onclick="_stravaSport=${sp.k===null?'null':"'"+sp.k+"'"};loadStravaActs(true)" style="background:${act?'rgba(34,197,94,.2)':'rgba(255,255,255,.06)'};border:1px solid ${act?'rgba(34,197,94,.4)':'rgba(255,255,255,.1)'};border-radius:10px;padding:7px 10px;color:${act?'#22c55e':'var(--text)'};font-weight:900;font-size:11px;cursor:pointer">${sp.l}</button>`;
      }).join('')+'</div>';
    if(!arr.length){$('act-list').innerHTML=filterBar+'<div style="color:var(--muted);padding:20px;text-align:center">Sin actividades</div>';return;}
    const rows=arr.map(s=>{
      const qual=s.stream_quality!=null?Math.round(s.stream_quality*100):null;
      const hrCov=s.hr_coverage!=null?Math.round(s.hr_coverage*100):null;
      const qualBadge=s.has_streams
        ?`<span style="background:rgba(34,197,94,.15);color:#22c55e;padding:2px 6px;border-radius:6px;font-size:9px;font-weight:800">📡 ${qual||0}%</span>`
        :`<span style="background:rgba(255,255,255,.06);color:var(--muted);padding:2px 6px;border-radius:6px;font-size:9px">sin streams</span>`;
      const sportIcon={Ride:'🚴',Run:'🏃',VirtualRide:'⚡',Walk:'🚶',Swim:'🏊'}[s.sport_type]||'🏅';
      return `<div class="row" style="align-items:flex-start;cursor:pointer" onclick="showStravaDetail(${s.strava_id})">
        <div class="r-ico" style="background:rgba(34,197,94,.1);width:42px;height:42px;border-radius:13px;display:flex;align-items:center;justify-content:center;flex-shrink:0;font-size:20px;margin-top:2px">${sportIcon}</div>
        <div class="r-main" style="flex:1;min-width:0">
          <div class="r-title" style="white-space:normal">${s.name||s.sport_type}</div>
          <div class="r-sub">${s.start_date} · ${s.duration_hms||'—'} · FC ${s.avg_hr||'—'} bpm · +${s.elevation_m||0} m</div>
          <div style="display:flex;gap:5px;margin-top:4px;flex-wrap:wrap">${qualBadge}${hrCov!=null?`<span style="background:rgba(74,158,255,.12);color:#4a9eff;padding:2px 6px;border-radius:6px;font-size:9px;font-weight:800">HR ${hrCov}%</span>`:''}${s.cadence_coverage?`<span style="background:rgba(200,241,53,.1);color:#c8f135;padding:2px 6px;border-radius:6px;font-size:9px;font-weight:800">CAD ${Math.round(s.cadence_coverage*100)}%</span>`:''}${s.power_coverage?`<span style="background:rgba(245,158,11,.12);color:#f59e0b;padding:2px 6px;border-radius:6px;font-size:9px;font-weight:800">POW ${Math.round(s.power_coverage*100)}%</span>`:''}</div>
        </div>
        <div style="text-align:right;flex-shrink:0"><div style="font-size:16px;font-weight:950;color:#22c55e">${s.distance_km||'—'} km</div></div>
      </div>`;
    }).join('');
    const rem=total-_stravaOffset-arr.length;
    const more=rem>0?`<button onclick="_stravaOffset+=${arr.length};loadStravaActs(false)" class="btn btn2" style="margin-top:8px">Cargar más (${rem.toLocaleString()} restantes)</button>`:'';
    $('act-list').innerHTML=filterBar+rows+more;
  }catch(e){$('act-list').innerHTML='<div style="color:var(--muted)">'+e.message+'</div>';}
}

// HR Sparkline SVG (inline, sin dependencias)
function hrSparkline(hrArr,w=200,h=40){
  if(!hrArr||!hrArr.length)return '';
  const vals=hrArr.filter(v=>v!=null&&v>0);
  if(!vals.length)return '';
  const mn=Math.min(...vals),mx=Math.max(...vals),rng=mx-mn||1;
  const pts=vals.map((v,i)=>`${Math.round(i/(vals.length-1)*w)},${Math.round(h-(v-mn)/rng*h)}`).join(' ');
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" style="display:block">
    <polyline points="${pts}" fill="none" stroke="#4a9eff" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" opacity=".85"/>
    <text x="${w-2}" y="${h-2}" fill="#4a9eff" font-size="9" text-anchor="end" opacity=".7">${mx} bpm</text>
  </svg>`;
}

async function showStravaDetail(stravaId){
  // Mostrar panel de detalle con sparkline
  const existing=document.getElementById('strava-detail-panel');
  if(existing)existing.remove();
  const panel=document.createElement('div');
  panel.id='strava-detail-panel';
  panel.style.cssText='position:fixed;inset:0;background:rgba(7,8,10,.9);z-index:200;display:flex;align-items:flex-end;justify-content:center';
  panel.innerHTML='<div style="background:#11141a;border-radius:24px 24px 0 0;width:100%;max-width:500px;padding:20px 16px 40px;max-height:85vh;overflow-y:auto"><div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px"><div style="font-size:16px;font-weight:800">Stream data</div><button onclick="document.getElementById(\'strava-detail-panel\').remove()" style="background:rgba(255,255,255,.08);border:none;border-radius:10px;padding:6px 12px;color:var(--text);cursor:pointer">✕ cerrar</button></div><div id="strava-detail-body" class="loading"><span class="spin"></span>Cargando streams...</div></div>';
  document.body.appendChild(panel);
  try{
    const d=await fetch(API+'/api/strava/activity/'+stravaId+'/stream-preview').then(r=>r.json());
    if(d.error){document.getElementById('strava-detail-body').innerHTML='<div style="color:var(--muted);font-size:13px">Sin streams descargados aún para esta actividad.</div>';return;}
    const h=Math.floor(d.duration_s/3600),m=Math.floor((d.duration_s%3600)/60);
    const durStr=h?h+'h '+m+'min':m+'min';
    document.getElementById('strava-detail-body').innerHTML=
      `<div style="font-size:12px;color:var(--muted);margin-bottom:12px">${d.n_points} puntos · ${durStr}</div>`+
      (d.hr&&d.hr.length?'<div style="margin-bottom:14px"><div style="font-size:11px;font-weight:800;color:#4a9eff;margin-bottom:5px">❤ Frecuencia Cardíaca</div>'+hrSparkline(d.hr,Math.min(360,window.innerWidth-48),50)+'</div>':'')+
      (d.watts&&d.watts.some(v=>v>0)?'<div style="margin-bottom:14px"><div style="font-size:11px;font-weight:800;color:#f59e0b;margin-bottom:5px">⚡ Potencia</div>'+hrSparkline(d.watts,Math.min(360,window.innerWidth-48),50).replace(/#4a9eff/g,'#f59e0b').replace(/[0-9]+ bpm/,v=>v.replace('bpm','W'))+'</div>':'')+
      (d.cadence&&d.cadence.some(v=>v>0)?'<div style="margin-bottom:14px"><div style="font-size:11px;font-weight:800;color:#c8f135;margin-bottom:5px">🔄 Cadencia</div>'+hrSparkline(d.cadence,Math.min(360,window.innerWidth-48),40).replace(/#4a9eff/g,'#c8f135').replace(/[0-9]+ bpm/,v=>v.replace('bpm','rpm'))+'</div>':'')+
      (d.altitude&&d.altitude.some(v=>v>0)?'<div><div style="font-size:11px;font-weight:800;color:#22c55e;margin-bottom:5px">⛰ Altitud</div>'+hrSparkline(d.altitude,Math.min(360,window.innerWidth-48),40).replace(/#4a9eff/g,'#22c55e').replace(/[0-9]+ bpm/,v=>v.replace('bpm','m'))+'</div>':'');
  }catch(err){document.getElementById('strava-detail-body').innerHTML='<div style="color:var(--muted)">'+err.message+'</div>';}
}

function openMatched(sessionId, routeId){
  if(routeId){
    window.location.href='/route/'+routeId+'/matched';
  }else{
    window.location.href='/gpt/matched-rides/'+sessionId;
  }
}

async function uploadFit(file){
  if(!file)return;
  $('upload-result').innerHTML='';$('upload-result').innerHTML='<div class="card">Procesando '+file.name+'...</div>';
  const fd=new FormData();
  fd.append('file',file);
  try{
    const d=await fetch(API+'/analyze-fit',{method:'POST',body:fd}).then(r=>r.json());
    $('upload-result').innerHTML='';
    const s=d.session||{};
    let html='<div class="card"><div class="head"><h3>'+(d.duplicate?'Sesion existente':'Sesion guardada')+'</h3><span>'+d.session_id+'</span></div>'+
      '<div class="grid2">'+
        metric('Distancia',s.distance_km||'—','km')+
        metric('FC prom.',s.avg_hr_bpm||'—','bpm')+
        metric('Duracion',s.duration_hms||'—','')+
        metric('Ascenso',s.ascent_m||'—','m')+
      '</div>'+
      '<button class="btn" style="margin-top:8px" onclick="window.location.href=\x27/charts/'+d.session_id+'\x27">Ver graficas</button>'+
      '<button class="btn btn2" style="margin-top:4px" onclick="navigator.clipboard.writeText(\x27'+d.session_id+'\x27);toast(\x27ID copiado\x27)">Copiar session_id</button>'+
    '</div>';
    $('upload-result').innerHTML=html;
    // Auto-load matched rides
    try{
      const mr=await fetch(API+'/gpt/matched-rides/'+d.session_id).then(r=>r.json());
      if(mr.matched&&mr.matched.length>0){
        const vs=mr.vs_historical||{};
        const verdictCol=vs.verdict==='mejoraste'?'#3dd68c':vs.verdict==='bajaste'?'#e8593c':'#f59e0b';
        let mrHtml='<div class="card" style="margin-top:8px">'+
          '<div class="head"><h3>Matched Rides</h3><span style="color:'+verdictCol+'">'+vs.verdict+'</span></div>'+
          '<div style="font-size:12px;color:var(--muted);margin-bottom:10px">'+mr.route_label+' · '+mr.count+' ejecuciones anteriores</div>';
        if(vs.speed_delta!=null){
          mrHtml+='<div style="display:flex;gap:10px;margin-bottom:10px">'+
            '<div style="flex:1;background:rgba(61,214,140,.08);border-radius:8px;padding:8px;text-align:center">'+
              '<div style="font-size:10px;color:var(--muted)">VELOCIDAD HOY</div>'+
              '<div style="font-size:16px;font-weight:950;color:#3dd68c">'+(mr.reference.avg_speed_kmh||0).toFixed(1)+' km/h</div>'+
              '<div style="font-size:10px;color:'+(vs.speed_delta>=0?'#3dd68c':'#e8593c')+'">'+(vs.speed_delta>=0?'+':'')+vs.speed_delta+' vs hist.</div>'+
            '</div>'+
            '<div style="flex:1;background:rgba(74,158,255,.08);border-radius:8px;padding:8px;text-align:center">'+
              '<div style="font-size:10px;color:var(--muted)">FC HOY</div>'+
              '<div style="font-size:16px;font-weight:950;color:#4a9eff">'+(mr.reference.avg_hr_bpm||0).toFixed(0)+' bpm</div>'+
              '<div style="font-size:10px;color:var(--muted)">hist: '+(vs.avg_hr_hist||'—')+'</div>'+
            '</div>'+
          '</div>';
        }
        mrHtml+='<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;font-size:11px">'+
          '<tr style="color:var(--muted)"><td style="padding:4px">Fecha</td><td style="padding:4px;text-align:right">km/h</td><td style="padding:4px;text-align:right">FC</td><td style="padding:4px;text-align:right">Efic.</td></tr>'+
          mr.matched.slice(0,6).map(function(m){
            return '<tr style="border-top:1px solid rgba(255,255,255,.06)">'+
              '<td style="padding:4px;color:var(--muted)">'+fmtShort(m.dt)+'</td>'+
              '<td style="padding:4px;text-align:right;font-weight:800">'+(m.avg_speed_kmh||'—')+'</td>'+
              '<td style="padding:4px;text-align:right">'+(m.avg_hr_bpm||'—')+'</td>'+
              '<td style="padding:4px;text-align:right;color:#3dd68c">'+(m.efficiency||'—')+'</td>'+
            '</tr>';
          }).join('')+
        '</table></div></div>';
        $('upload-result').innerHTML+= mrHtml;
      }
    }catch(me){/* matched rides optional */}
    loadActs();
  }catch(e){$('upload-result').innerHTML='<div class="card">Error: '+e.message+'</div>'}
}

async function loadGear(){
  loadGearHistory();
  try{
    const d=await fetch(API+'/gpt/gear-status').then(r=>r.json()).catch(()=>null);
    const a=await fetch(API+'/gpt/gear-alerts').then(r=>r.json()).catch(()=>({alerts:[]}));
    const items=(d&&d.componentes)||d?.components||d?.gear||[];
    const active=(d&&d.garmin_active)||[];
    const retired=(d&&d.garmin_retired)||[];
    const note=(d&&d.gear_model_note)||'Garmin export trae catalogo, pero aun falta enlazar actividad a pieza para kilometraje exacto.';
    function gearRow(g, retiredFlag){
      const used=Number(g.km_used||g.km_current||g.current_km||0);
      const lim=Number(g.km_limit||g.limit_km||g.max_distance_km||0);
      const pct=lim?Math.min(100,Math.round(used/lim*100)):null;
      const sub=retiredFlag?'Retirado · '+(g.type||'pieza'):(lim?used+' / '+lim+' km':(g.type||'pieza')+' · kilometraje por enlazar');
      return `<div class="row"><div class="r-ico">${retiredFlag?'R':'A'}</div><div class="r-main"><div class="r-title">${g.name||g.model||g.type||'Componente'}</div><div class="r-sub">${sub}${pct!=null?`<div class="gearbar"><div class="gearfill" style="width:${pct}%"></div></div>`:''}</div></div><div class="r-val">${pct!=null?pct+'%':(retiredFlag?'ret.':'cat.')}</div></div>`;
    }
    let html=`<div class="card"><div class="head"><h3>Alertas</h3><span>${(a.alerts||[]).length}</span></div>${(a.alerts||[]).length?(a.alerts||[]).map(x=>row('',x.name||x.type||'Alerta',x.message||x.detail||'',x.km_left?x.km_left+' km':'' )).join(''):'Sin alertas de equipo'}</div>`;
    html+=`<div class="card"><div class="head"><h3>Componentes activos</h3><span>${items.length||0}</span></div>${items.length?items.map(g=>gearRow(g,false)).join(''):'Sin componentes activos registrados'}</div>`;
    html+=`<div class="card"><div class="head"><h3>Catálogo Garmin activo</h3><span>${active.length||0}</span></div><div class="r-sub" style="margin-bottom:8px">${note}</div>${active.length?active.map(g=>gearRow(g,false)).join(''):'Sin piezas activas en Garmin export'}</div>`;
    html+=`<div class="card"><div class="head"><h3>Garmin retirado</h3><span>${retired.length||0}</span></div>${retired.length?retired.map(g=>gearRow(g,true)).join(''):'Sin piezas retiradas en Garmin export'}</div>`;
    $('gear-data').innerHTML=html;
  }catch(e){$('gear-data').innerHTML='<div class="card">Error: '+e.message+'</div>'}
}
async function loadCal(){
  try{
    const d=await fetch(API+'/api/strava/daily-heatmap?months=3').then(r=>r.json()).catch(()=>({}));
    const activeDays=new Map((d.days||[]).map(function(x){return[x.date,x]}));
    // Construir grid de 12 semanas completas (84 días) terminando hoy
    const today=new Date();
    // Retroceder hasta el lunes de hace 12 semanas
    const startDay=new Date(today);
    startDay.setDate(today.getDate()-((today.getDay()+6)%7)-11*7); // 12 semanas atrás, lunes
    const cells=[];
    for(let i=0;i<84;i++){
      const dd=new Date(startDay);dd.setDate(startDay.getDate()+i);
      const key=dd.toISOString().slice(0,10);
      cells.push({date:key,day:dd.getDate(),data:activeDays.get(key)||null});
    }
    const active=cells.filter(function(c){return c.data}).length;
    const totalSes=cells.reduce(function(a,c){return a+(c.data?c.data.sessions:0)},0);
    const totalKm=cells.reduce(function(a,c){return a+(c.data?c.data.km:0)},0);
    const sportIcon={Ride:'🚴',Run:'🏃',VirtualRide:'⚡',Walk:'🚶',Swim:'🏊'};
    const DOWS=['L','M','X','J','V','S','D'];
    $('cal-data').innerHTML=
      '<div class="grid2" style="margin-bottom:12px">'+
        '<div class="mini"><div class="label">Días activos</div><div class="value" style="color:#22d3ee">'+active+'</div><div class="unit">12 semanas</div></div>'+
        '<div class="mini"><div class="label">Actividades</div><div class="value">'+totalSes+'</div><div class="unit">'+Math.round(totalKm)+' km</div></div>'+
      '</div>'+
      '<div class="card">'+
        '<div style="display:grid;grid-template-columns:repeat(7,1fr);gap:3px;margin-bottom:6px">'+
          DOWS.map(function(d){return'<div style="text-align:center;font-size:9px;color:var(--muted);font-weight:900">'+d+'</div>'}).join('')+
        '</div>'+
        '<div style="display:grid;grid-template-columns:repeat(7,1fr);gap:3px">'+
          cells.map(function(c){
            const n=c.data?c.data.sessions:0;
            const isToday=c.date===today.toISOString().slice(0,10);
            const sports=(c.data&&c.data.sport_types)||[];
            const mainSport=sports[0]||'';
            const ico=n===1&&sportIcon[mainSport]?sportIcon[mainSport]:'';
            const bg=n===0?'rgba(255,255,255,.04)':n===1?'rgba(34,211,238,.25)':n===2?'rgba(34,211,238,.5)':'rgba(34,211,238,.8)';
            const border=isToday?';outline:2px solid #22d3ee':'';
            const title=c.data?c.date+' · '+n+' ses · '+c.data.km+' km':'';
            return'<div title="'+title+'" style="aspect-ratio:1;border-radius:5px;background:'+bg+border+';display:flex;align-items:center;justify-content:center;font-size:'+(ico?'11':'9')+'px;color:'+(n>0?'#22d3ee':'var(--muted)')+'">'+  (ico||c.day)+'</div>';
          }).join('')+
        '</div>'+
      '</div>';
  }catch(e){$('cal-data').innerHTML='<div class="card" style="color:var(--muted)">'+e.message+'</div>'}
}

async function loadPerf(){
  try{
    const d=await fetch(API+'/gpt/performance-profile?sport=cycling').then(r=>r.json());
    const r=d.records||{},c=d.carga||{},eff=d.eficiencia_aerobica||{};
    function recRow(label,rec,val,col){
      if(!rec)return '';
      return '<div class="row"><div class="r-ico" style="background:color-mix(in srgb,'+col+' 12%,#111);width:38px;height:38px;border-radius:11px;display:flex;align-items:center;justify-content:center;flex-shrink:0"><div style="width:8px;height:8px;border-radius:50%;background:'+col+'"></div></div><div class="r-main"><div class="r-title">'+label+'</div><div class="r-sub">'+fmtDate(rec.date)+'</div></div><div class="r-val" style="color:'+col+'">'+val+'</div></div>';
    }
    $('perf-data').innerHTML='<div class="grid2">'+
      metric('VO2Max',d.vo2max_estimado||'—','estimado')+
      metric('TSB',c.tsb||0,c.estado||'carga')+
      metric('Cadencia',d.cadencia_trend||'—','trend')+
      metric('Eficiencia',eff.delta_pct_6_meses??'—','6 meses')+
    '</div><div class="card"><div class="head"><h3>Records personales</h3><span>ciclismo</span></div>'+
      recRow('Mayor distancia',r.max_distance,Number((r.max_distance||{}).value||0).toFixed(1)+' km','#e8593c')+
      recRow('Mayor ascenso',r.max_ascent,'+'+parseInt((r.max_ascent||{}).value||0)+' m','#c8f135')+
      recRow('Mayor velocidad',r.max_speed,Number((r.max_speed||{}).value||0).toFixed(1)+' km/h','#a78bfa')+
      recRow('Sesion mas larga',r.max_duration,hms((r.max_duration||{}).value||0),'#4a9eff')+
    '</div>';
  // Add route history
    try{
      const rh=await fetch(API+'/gpt/route-history?weeks=26').then(r=>r.json());
      if(rh.routes&&rh.routes.length>0){
        let rhHtml='<div class="card" style="margin-top:8px"><div class="head"><h3>Progresion por ruta</h3><span>'+rh.routes.length+' rutas</span></div>';
        rh.routes.forEach(function(r){
          rhHtml+='<div style="padding:10px 0;border-bottom:1px solid rgba(255,255,255,.07)">'+
            '<div style="display:flex;justify-content:space-between;margin-bottom:4px">'+
              '<div style="font-size:13px;font-weight:800">'+r.label+'</div>'+
              '<div style="font-size:11px;color:var(--muted)">'+parseInt(r.n)+' veces</div>'+
            '</div>'+
            '<div style="display:flex;gap:16px;font-size:11px;color:var(--muted)">'+
              '<span>Mejor: <b style="color:#3dd68c">'+(r.best_speed||'—')+' km/h</b></span>'+
              '<span>Prom: '+(r.avg_speed||'—')+' km/h</span>'+
              '<span>FC: '+(r.avg_hr||'—')+' bpm</span>'+
            '</div>'+
          '</div>';
        });
        rhHtml+='</div>';
        $('perf-data').innerHTML+= rhHtml;
      }
    }catch(re){}
  }catch(e){$('perf-data').innerHTML='<div class="card" style="color:var(--muted)">'+e.message+'</div>'}
}

async function loadFuerza(){
  try{
    const [d,hist]=(await Promise.allSettled([
      fetch(API+'/gpt/fuerza-summary?weeks=8').then(r=>r.json()),
      fetch(API+'/api/fuerza-records?limit=10').then(r=>r.json())
    ])).map(r=>r.status==='fulfilled'?r.value:{});
    const items=hist.records||[];
    $('fuerza-data').innerHTML=
      '<div class="grid2">'+metric('Sesiones 8s',d.total_sesiones||0,'')+metric('Horas total',d.total_horas||0,'')+'</div>'+
      (items.length?'<div class="card"><div class="head"><h3>Ultimas sesiones</h3></div>'+
        items.map(e=>'<div class="row"><div class="r-main"><div class="r-title">'+(e.category||'Fuerza')+(e.subcategory?' · '+e.subcategory:'')+
          (e.intensity?' · Int '+e.intensity:'')+'</div>'+
          '<div class="r-sub">'+fmtDate(e.date)+(e.duration_min?' · '+e.duration_min+' min':'')+'</div></div>'+
          '<button onclick="deleteFuerzaRec('+e.id+')" style="background:rgba(232,89,60,.1);border:none;border-radius:8px;padding:4px 8px;color:#e8593c;font-size:10px;font-weight:800;cursor:pointer;flex-shrink:0">borrar</button>'+
        '</div>').join('')+'</div>':'');
  }catch(e){}
}

async function saveFuerza(){const today=new Date().toISOString().slice(0,10);const body={date:today,category:$('fv-cat').value,muscle_groups:$('fv-muscles').value.split(',').map(x=>x.trim()).filter(Boolean),intensity:parseInt($('fv-intensity').value)||null,duration_min:parseInt($('fv-duration').value)||null};const d=await fetch(API+'/fuerza',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json());toast(d.ok?'Fuerza guardada':'Error');loadFuerza()}
async function loadWell(){
  try{
    const [d,hist]=(await Promise.allSettled([
      fetch(API+'/gpt/wellness-summary?weeks=4').then(r=>r.json()),
      fetch(API+'/api/wellness-records?limit=60').then(r=>r.json())
    ])).map(r=>r.status==='fulfilled'?r.value:{});
    const items=hist.records||hist.registros||hist.data||[];
    const pains=d.molestias_activas||[];
    const sleep=d.sueno_promedio_horas||'—';
    const stress=d.estres_promedio||'—';
    const fc=d.fc_reposo_promedio||'—';

    // ── Bitácora 28 días ──
    const datesWithCheck=new Set();
    const datesWithRecord=new Set();
    items.forEach(e=>{
      const dk=(e.date||'').slice(0,10);
      if(dk){datesWithRecord.add(dk);if(e.hr_rest)datesWithCheck.add(dk);}
    });
    let streakDots='';
    const catLabel={sleep:'Sueño',nap:'Siesta',compex_recovery:'Compex',massage_gun:'Pistola',ceragem:'Ceragem',pain:'Molestia',stress:'Estrés'};
    for(let i=27;i>=0;i--){
      const dd=new Date();dd.setDate(dd.getDate()-i);
      const dk=dd.toISOString().slice(0,10);
      const d2=dd.getDate();
      const hasCheck=datesWithCheck.has(dk);
      const hasRec=datesWithRecord.has(dk);
      const col=hasCheck?'#4a9eff':hasRec?'#f59e0b':'#2a2d36';
      const title=hasCheck?'✅ Check matutino':hasRec?'📝 Registro':dk;
      streakDots+=`<div title="${title}" style="display:flex;flex-direction:column;align-items:center;gap:2px">
        <div style="width:10px;height:10px;border-radius:50%;background:${col}"></div>
        <div style="font-size:8px;color:${hasCheck?'#4a9eff':hasRec?'#f59e0b':'var(--muted)'}">${d2}</div>
      </div>`;
    }
    const checkCount=datesWithCheck.size;
    // Racha consecutiva (días seguidos terminando hoy)
    let streak=0;
    for(let i=0;i<28;i++){
      const dd=new Date();dd.setDate(dd.getDate()-i);
      const dk=dd.toISOString().slice(0,10);
      if(datesWithCheck.has(dk)){streak++;}else{break;}
    }
    const e25cPct=Math.min(100,Math.round(checkCount/21*100));
    const e25cActive=checkCount>=21;
    const streakCard=`<div class="card" style="margin-bottom:8px">
      <div class="head"><h3>Recuperación · 28 días</h3><span style="color:${e25cActive?'#22c55e':'#4a9eff'}">${checkCount}/28 checks</span></div>
      <div style="display:grid;grid-template-columns:repeat(28,1fr);gap:3px;margin-bottom:8px">${streakDots}</div>
      <div style="font-size:11px;color:var(--muted);margin-bottom:8px">
        <span style="color:#4a9eff">●</span> Check matutino &nbsp;
        <span style="color:#f59e0b">●</span> Otro registro &nbsp;
        <span style="color:#2a2d36;border:1px solid #444;border-radius:50%;display:inline-block;width:8px;height:8px"></span> Sin datos
      </div>
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px">
        <div style="font-size:12px;font-weight:800;color:#4a9eff">🔥 Racha: ${streak} día${streak!==1?'s':''}</div>
        <div style="font-size:11px;color:${e25cActive?'#22c55e':'var(--muted)'}">E25C: ${e25cActive?'✅ activo':''+checkCount+'/21 días'}</div>
      </div>
      <div style="height:6px;border-radius:3px;background:#1e2230;overflow:hidden">
        <div style="height:100%;width:${e25cPct}%;background:${e25cActive?'#22c55e':'#4a9eff'};border-radius:3px;transition:width .4s"></div>
      </div>
      ${!e25cActive?'<div style="font-size:10px;color:var(--muted);margin-top:5px">Faltan '+(21-checkCount)+' checks para activar Score Recuperación (E25C)</div>':''}
    </div>`;

    $('well-data').innerHTML=
      '<div class="grid2">'+
        metric('Sueno prom',sleep,'h/noche')+
        metric('FC reposo',fc,'bpm')+
        metric('Estres prom',stress,'/10')+
        metric('Molestias',pains.length,pains.length?'activas':'sin alertas')+
      '</div>'+
      streakCard+
      (pains.length?'<div class="card" style="border-color:rgba(232,89,60,.3)"><div class="head"><h3 style="color:#e8593c">Molestias activas</h3><span>'+pains.length+'</span></div>'+
        pains.map(p=>'<div class="row"><div class="r-main"><div class="r-title">'+(p.pain_zone||'—')+'</div></div><div class="r-val" style="color:#e8593c">Nivel '+(p.pain_level||'?')+'/10</div></div>').join('')+'</div>':'')+
      (items.length?'<div class="card"><div class="head"><h3>Registros</h3></div>'+
        items.slice(0,15).map(e=>'<div class="row"><div class="r-main"><div class="r-title">'+(catLabel[e.category]||e.category||'Wellness')+'</div>'+
          '<div class="r-sub">'+fmtDate(e.date)+
          (e.hr_rest?'<span style="color:#4a9eff;font-weight:800"> · ❤ '+e.hr_rest+' bpm</span>':'')+
          (e.sleep_hours?' · '+e.sleep_hours+'h':'')+
          (e.fatigue?' · fatiga '+e.fatigue+'/10':'')+
          '</div></div>'+
          '<button onclick="deleteWellnessRec('+e.id+')" style="background:rgba(232,89,60,.1);border:none;border-radius:8px;padding:4px 8px;color:#e8593c;font-size:10px;font-weight:800;cursor:pointer;flex-shrink:0">✕</button>'+
        '</div>').join('')+'</div>':'');
  }catch(e){$('well-data').innerHTML='<div class="card" style="color:var(--muted)">'+e.message+'</div>'}
}

async function saveMorningCheck(){
  const today=new Date().toISOString().slice(0,10);
  const hr=parseInt($('wv-hr').value)||null;
  const fatigue=parseInt($('wv-fatigue').value)||null;
  const sleep=parseFloat($('wv-sleep').value)||null;
  const estado=parseInt($('wv-estado').value)||null;
  const painZone=$('wv-painzone').value||null;
  if(!hr&&!fatigue&&!sleep&&!estado){toast('Llena al menos un campo');return;}
  const body={date:today,category:'sleep',hr_rest:hr,fatigue:fatigue,sleep_hours:sleep,
    stress_level:estado,pain_zone:painZone||undefined};
  const d=await fetch(API+'/wellness',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json());
  if(d.ok){
    $('wv-hr').value='';$('wv-fatigue').value='';$('wv-sleep').value='';
    $('wv-estado').value='';$('wv-painzone').value='';
    toast('✅ Check matutino guardado — recuperación activa');
    loadWell();
  }else{toast('Error guardando');}
}
async function saveWellness(){const today=new Date().toISOString().slice(0,10);const cat=$('wv-cat').value;const dur=parseFloat($('wv-duration').value)||null;const body={date:today,category:cat,fatigue:parseInt($('wv-fatigue').value)||null};if(cat==='sleep')body.sleep_hours=dur;else body.duration_min=dur;const d=await fetch(API+'/wellness',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json());toast(d.ok?'Wellness guardado':'Error');loadWell()}

async function loadEficiencia(){
  const el=$('eff-data');
  try{
    const [eff,perf]=(await Promise.allSettled([
      fetch(API+'/gpt/efficiency-trend?sport=cycling').then(r=>r.json()),
      fetch(API+'/gpt/performance-profile?sport=cycling').then(r=>r.json())
    ])).map(r=>r.status==='fulfilled'?r.value:{});
    const trend=eff.trend||eff.eficiencia||[];
    const carga=perf.carga||{};
    const tsbCol=carga.tsb>5?'#3dd68c':carga.tsb<-15?'#e8593c':'#4a9eff';
    function sparkSVG(vals,col,h){
      if(!vals||vals.length<2)return '';
      const mx=Math.max(...vals),mn=Math.min(...vals),rng=mx-mn||0.001,W=280;
      const pts=vals.map((v,i)=>Math.round(i/(vals.length-1)*W)+','+Math.round(h-(v-mn)/rng*(h-8)-4)).join(' ');
      const lx=W,ly=Math.round(h-(vals[vals.length-1]-mn)/rng*(h-8)-4);
      return '<svg viewBox="0 0 '+W+' '+h+'" style="width:100%;height:'+h+'px"><polyline points="'+pts+'" fill="none" stroke="'+col+'" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" opacity=".5"/><circle cx="'+lx+'" cy="'+ly+'" r="4" fill="'+col+'"/></svg>';
    }
    const effVals=trend.map(t=>parseFloat(t.ratio||t.efficiency||t.vel_fc_ratio||0)).filter(v=>v>0);
    const curr=effVals.length?effVals[effVals.length-1]:null;
    const base=0.1483,target=0.155;
    const delta=curr?+(curr-base).toFixed(4):null;
    const pct=curr?Math.min(100,Math.round((curr/target)*100)):0;
    const cHist=carga.history||[];
    const ctlV=cHist.map(h=>h.ctl).filter(Boolean);
    el.innerHTML=`
      <div class="grid2">
        <div class="card-sm"><div class="kl">Ratio actual</div><div class="kv" style="color:#3dd68c">${curr?curr.toFixed(4):'—'}</div><div class="ku">${delta!=null?(delta>=0?'+':'')+delta+' vs base':''}</div></div>
        <div class="card-sm"><div class="kl">Objetivo</div><div class="kv" style="color:#3dd68c">0.155</div><div class="ku">${pct}% logrado</div></div>
      </div>
      <div class="card">
        <div class="head"><h3>Eficiencia vel/FC — ${trend.length} semanas</h3><span style="color:#3dd68c">${pct}%</span></div>
        <div class="pbar"><div class="pfill" style="width:${pct}%;background:#3dd68c"></div></div>
        <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--muted);margin-bottom:10px"><span>Base: 0.1483</span><span>Objetivo: 0.155+</span></div>
        ${sparkSVG(effVals,'#3dd68c',60)}
      </div>
      <div class="card">
        <div class="head"><h3>Carga ATL/CTL/TSB</h3><span style="color:${tsbCol}">${carga.estado||'—'}</span></div>
        <div class="grid2" style="margin-bottom:8px">
          ${metric('ATL agudo',carga.atl||'—','')}${metric('CTL cronico',carga.ctl||'—','')}
          ${metric('TSB','<span style="color:'+tsbCol+'">'+(carga.tsb||0)+'</span>','')}${metric('Estado','<span style="color:'+tsbCol+'">'+(carga.tsb>5?'Listo':carga.tsb<-15?'Recuperar':'Normal')+'</span>','')}
        </div>
        ${ctlV.length>=2?sparkSVG(ctlV,'#4a9eff',50):'<div style="color:var(--muted);font-size:12px">Sin historial de carga</div>'}
      </div>`;
  }catch(e){el.innerHTML='<div class="card" style="color:var(--muted)">'+e.message+'</div>'}
}

async function loadCorrelaciones(){
  const el=$('corr-data');
  try{
    const d=await fetch(API+'/gpt/correlations?weeks=12').then(r=>r.json());
    const weekly=d.weekly||[],weight=d.weight||[];
    const corr=d.correlation_hr_efficiency;
    const corrCol=corr&&corr<-0.3?'#3dd68c':corr&&corr>0.3?'#e8593c':'var(--muted)';
    function scatterSVG(xs,ys,col){
      if(!xs||xs.length<3)return '<div style="color:var(--muted);font-size:12px;padding:8px">Necesitas mas semanas de datos</div>';
      const W=280,H=110,p=20;
      const mxx=Math.max(...xs),mnx=Math.min(...xs),rx=mxx-mnx||1;
      const mxy=Math.max(...ys),mny=Math.min(...ys),ry=mxy-mny||0.001;
      const dots=xs.map((x,i)=>{const px=p+Math.round((x-mnx)/rx*(W-p*2));const py=H-p-Math.round((ys[i]-mny)/ry*(H-p*2));return '<circle cx="'+px+'" cy="'+py+'" r="3" fill="'+col+'" opacity=".7"/>';}).join('');
      return '<svg viewBox="0 0 '+W+' '+H+'" style="width:100%;height:'+H+'px"><line x1="'+p+'" y1="'+(H-p)+'" x2="'+(W-p)+'" y2="'+(H-p)+'" stroke="rgba(255,255,255,.08)"/><line x1="'+p+'" y1="'+p+'" x2="'+p+'" y2="'+(H-p)+'" stroke="rgba(255,255,255,.08)"/>'+dots+'</svg>';
    }
    const hrs=weekly.map(w=>w.avg_hr).filter(Boolean);
    const effs=weekly.map(w=>w.efficiency).filter(Boolean);
    const wkgs=weekly.map(w=>{const cl=weight.reduce((a,b)=>Math.abs(new Date(b.date)-new Date(w.week))<Math.abs(new Date(a.date)-new Date(w.week))?b:a,weight[0]||{});return cl?.kg||null;}).filter(Boolean);
    el.innerHTML=`
      <div class="card">
        <div class="head"><h3>FC vs Eficiencia aerobica</h3><span style="color:${corrCol}">${corr!=null?'r='+corr:'—'}</span></div>
        <div style="font-size:12px;color:var(--muted);margin-bottom:10px">${d.interpretation||''}</div>
        ${scatterSVG(hrs,effs,'#3dd68c')}
        <div style="font-size:10px;color:var(--muted);margin-top:4px">Cada punto = 1 semana · eje X: FC prom · eje Y: vel/FC</div>
      </div>
      ${wkgs.length>=3?`<div class="card"><div class="head"><h3>Peso vs FC promedio</h3></div>${scatterSVG(wkgs,hrs,'#4a9eff')}<div style="font-size:10px;color:var(--muted);margin-top:4px">Eje X: peso kg · Eje Y: FC prom bpm</div></div>`:'<div class="card"><div style="color:var(--muted);font-size:13px;padding:8px">Agrega mas registros de peso para la correlacion</div></div>'}
      <div class="grid2">
        ${metric('FC prom 12s',hrs.length?+(hrs.reduce((a,b)=>a+b,0)/hrs.length).toFixed(1):'—','bpm obj &lt;135')}
        ${metric('Efic. prom',effs.length?+(effs.reduce((a,b)=>a+b,0)/effs.length).toFixed(4):'—','obj 0.155+')}
      </div>`;
  }catch(e){el.innerHTML='<div class="card" style="color:var(--muted)">'+e.message+'</div>'}
}

async function loadNutricion(){
  const el=$('nutri-summary');
  try{
    const d=await fetch(API+'/nutrition/summary?weeks=8').then(r=>r.json());
    const tipos=d.por_tipo||[];
    const GL={agave_casero:'Agave casero',miel_casero:'Miel casero',comercial:'Comercial',fecha:'Fechas/fruta',ninguno:'Sin gel'};
    el.innerHTML=tipos.length?`<div class="card"><div class="head"><h3>Geles 8 semanas</h3><span>${tipos.reduce((a,t)=>a+(t.usos||0),0)} registros</span></div>${tipos.map(t=>row('·',GL[t.gel_type]||t.gel_type,t.usos+' usos'+(t.gi_issues>0?' · '+t.gi_issues+' GI':''),t.avg_carbos?t.avg_carbos.toFixed(0)+'g':'')  ).join('')}</div>`
    :'<div class="card" style="margin-bottom:12px"><div style="color:var(--muted);text-align:center;padding:12px">Sin registros aun</div></div>';
  }catch(e){el.innerHTML=''}
}
async function saveNutricion(){
  const body={date:new Date().toISOString().slice(0,10),gel_type:$('nf-type').value,moment:$('nf-moment').value,gel_count:parseInt($('nf-count').value)||null,agua_ml:parseInt($('nf-agua').value)||null,carbos_g:parseFloat($('nf-carbos').value)||null,gi_response:$('nf-gi').value,energy_response:$('nf-energy').value,notas:$('nf-notes').value||null};
  const d=await fetch(API+'/nutrition',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json());
  toast(d.ok?'Nutricion guardada':'Error','#f59e0b');
  if(d.ok)loadNutricion();
}
async function loadGearHistory(){
  const el=$('gear-history');
  if(!el)return;
  try{
    const d=await fetch(API+'/gear/service-history?limit=15').then(r=>r.json());
    const hist=d.historial||[];
    if(!hist.length){el.innerHTML='';return;}
    const SL={cambio_cadena:'Cambio cadena',cambio_llanta_del:'Llanta del.',cambio_llanta_tra:'Llanta tra.',cambio_cassette:'Cassette',ajuste_transmision:'Ajuste transmision',lubricacion:'Lubricacion'};
    el.innerHTML='<div class="card"><div class="head"><h3>Historial servicios</h3><span>'+hist.length+'</span></div>'+hist.map(s=>'<div class="row"><div class="r-main"><div class="r-title">'+(SL[s.service_type]||s.service_type||'Servicio')+'</div><div class="r-sub">'+fmtDate(s.date)+(s.km_at_service?' · '+Number(s.km_at_service).toLocaleString()+' km':'')+(s.days_since!=null?' · hace '+s.days_since+'d':'')+'</div></div><div style="text-align:right;display:flex;flex-direction:column;align-items:flex-end;gap:4px">'+( s.cost_mxn?'<div style="font-size:12px;font-weight:800;color:#f59e0b">$'+s.cost_mxn+'</div>':'')+' <button onclick="deleteGearServiceRec('+s.id+')" style="background:rgba(232,89,60,.1);border:none;border-radius:8px;padding:4px 8px;color:#e8593c;font-size:10px;font-weight:800;cursor:pointer">borrar</button></div></div>').join('')+'</div>';
  }catch(e){el.innerHTML=''}
}
async function saveGearService(){
  const body={service_type:$('gs-type').value,gear_name:$('gs-comp').value||null,date:new Date().toISOString().slice(0,10),km_at_service:parseFloat($('gs-km').value)||null,cost_mxn:parseFloat($('gs-cost').value)||null,notes:$('gs-notes').value||null};
  const d=await fetch(API+'/gear/service',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json());
  toast(d.ok?'Servicio registrado':'Error','#f59e0b');
  if(d.ok){loadGear();loadGearHistory();}
}


(function(){
  const h=new Date().getHours();
  const g=h<12?'Buenos días,':h<20?'Buenas tardes,':'Buenas noches,';
  const el=document.getElementById('greeting-h1');
  const name=(typeof CONFIG!=='undefined'&&CONFIG.USER_DISPLAY_NAME)||'Mars';
  if(el)el.innerHTML=g+'<br>'+name+'.';
})();


async function deleteFuerzaRec(id){
  if(!confirm('Borrar este registro?'))return;
  const d=await fetch(API+'/fuerza/'+id,{method:'DELETE'}).then(r=>r.json()).catch(()=>({}));
  if(d.ok){toast('Registro borrado');loadFuerza();}
  else toast('Error al borrar');
}
async function deleteWellnessRec(id){
  if(!confirm('Borrar este registro?'))return;
  const d=await fetch(API+'/wellness/'+id,{method:'DELETE'}).then(r=>r.json()).catch(()=>({}));
  if(d.ok){toast('Registro borrado');loadWell();}
  else toast('Error al borrar');
}
async function deleteNutricionRec(id){
  if(!confirm('Borrar?'))return;
  const d=await fetch(API+'/nutrition/'+id,{method:'DELETE'}).then(r=>r.json()).catch(()=>({}));
  if(d.ok){toast('Borrado');loadNutricion();}
  else toast('Error al borrar');
}
async function deleteGearServiceRec(id){
  if(!confirm('Borrar servicio?'))return;
  const d=await fetch(API+'/gear/service/'+id,{method:'DELETE'}).then(r=>r.json()).catch(()=>({}));
  if(d.ok){toast('Borrado');loadGearHistory();}
  else toast('Error al borrar');
}







// ── E26B GOAL REGISTRY ──────────────────────────────────




// ────────────────────────────────────────────────────────

async function loadCapacidades(){
  const el=$('cap-data');
  try{
    const _capSettled=await Promise.allSettled([
      fetch(API+'/gpt/capacidades').then(r=>{if(!r.ok)throw new Error('cap');return r.json();}),
      fetch(API+'/gpt/capacidad/motor_aerobico/history').then(r=>r.ok?r.json():{}),
      fetch(API+'/gpt/capacidad/composicion_corporal/history').then(r=>r.ok?r.json():{}),
      fetch(API+'/gpt/capacidad/escalada/history').then(r=>r.ok?r.json():{}),
      fetch(API+'/gpt/readiness?evento=escalera_al_infierno').then(r=>r.ok?r.json():{}),
      fetch(API+'/gpt/readiness?evento=gran_fondo_150').then(r=>r.ok?r.json():{}),
      fetch(API+'/gpt/readiness?evento=medio_maraton').then(r=>r.ok?r.json():{}),
      fetch(API+'/gpt/academia/glosario').then(r=>r.ok?r.json():{}),
      fetch(API+'/gpt/patron-historico').then(r=>r.ok?r.json():{})
    ]);
    if(_capSettled[0].status==='rejected')throw new Error('No se pudo calcular capacidades');
    const d=_capSettled[0].value;
    const history=_capSettled[1].status==='fulfilled'?_capSettled[1].value:{};
    const weightHistory=_capSettled[2].status==='fulfilled'?_capSettled[2].value:{};
    const climbHistory=_capSettled[3].status==='fulfilled'?_capSettled[3].value:{};
    const readiness=[
      _capSettled[4].status==='fulfilled'?_capSettled[4].value:{},
      _capSettled[5].status==='fulfilled'?_capSettled[5].value:{},
      _capSettled[6].status==='fulfilled'?_capSettled[6].value:{}
    ];
    const glosario=_capSettled[7].status==='fulfilled'?(_capSettled[7].value.glosario||{}):{};
    const patronData=_capSettled[8].status==='fulfilled'?_capSettled[8].value:{};
    const items=d.capabilities||[];
    const calculated=items.filter(function(c){return c.status==='calculated'}).length;
    const highConfidence=items.filter(function(c){return Number(c.confidence||0)>=0.75}).length;
    function capColor(c){
      if(c.status!=='calculated')return '#8b929f';
      if(Number(c.score)>=75)return '#3dd68c';
      if(Number(c.score)>=50)return '#f59e0b';
      return '#e8593c';
    }
    function pct(v){return Math.max(0,Math.min(100,Number(v||0)))}
    function readinessStatusColor(status){
      return {listo:'#3dd68c',bien_encaminado:'#22d3ee',forma_en_desarrollo:'#f59e0b',base_en_construccion:'#e8593c',inicio:'#8b929f'}[status]||'#8b929f';
    }
    function readinessStatusLabel(status){
      return {listo:'Listo',bien_encaminado:'Bien encaminado',forma_en_desarrollo:'En desarrollo',base_en_construccion:'Base en construcción',inicio:'Inicio'}[status]||status;
    }
    function readinessView(){
      if(!readiness.length)return '';
      const cards=readiness.filter(function(r){return r.ok}).map(function(r){
        const color=readinessStatusColor(r.status);
        const label=readinessStatusLabel(r.status);
        const comps=(r.components||[]).map(function(c){
          const s=c.score==null?'--':Number(c.score).toFixed(0);
          const barColor=c.score==null?'rgba(255,255,255,.1)':c.score>=75?'#3dd68c':c.score>=50?'#f59e0b':'#e8593c';
          const gap=c.score==null?' <span style="font-size:9px;color:var(--muted)">[sin datos]</span>':'';
          return '<div style="display:grid;grid-template-columns:minmax(0,1fr) 32px;gap:6px;align-items:center;padding:5px 0;border-bottom:1px solid var(--line)">'+
            '<div><div style="font-size:11px;margin-bottom:3px">'+c.nombre+gap+'</div>'+
            '<div style="height:4px;background:rgba(255,255,255,.07);border-radius:2px"><div style="height:100%;width:'+pct(c.score)+'%;background:'+barColor+'"></div></div></div>'+
            '<div style="text-align:right;font-size:12px;font-weight:900;color:'+barColor+'">'+s+'</div>'+
          '</div>';
        }).join('');
        const gaps=r.data_gaps&&r.data_gaps.length?'<div style="font-size:10px;color:var(--muted);margin-top:6px">Faltan datos: '+r.data_gaps.join(', ')+'</div>':'';
        return '<div class="card" style="border-left:3px solid '+color+'">'+
          '<div class="head"><h3>'+r.event+'</h3><span style="color:'+color+'">'+label+'</span></div>'+
          '<div class="grid2">'+
            metric('Listo',Number(r.readiness_score).toFixed(1),'de 100')+
            metric('Confianza',Math.round(Number(r.confidence)*100)+'%','datos disponibles')+
          '</div>'+
          '<div style="margin:8px 0 4px"><div style="height:8px;background:rgba(255,255,255,.07);border-radius:4px"><div style="height:100%;width:'+pct(r.readiness_score)+'%;background:'+color+';border-radius:4px"></div></div></div>'+
          comps+
          gaps+
          (r.limiting_factor_nombre&&r.score!==null?'<div style="font-size:10px;color:var(--muted);margin-top:6px">Limitante principal: '+r.limiting_factor_nombre+'</div>':'')+
        '</div>';
      }).join('');
      if(!cards)return '';
      return '<div class="head" style="margin:16px 0 4px"><h3>Readiness Scenarios</h3><span>preparación por escenario · no son objetivos activos</span></div>'+
        '<div class="grid2">'+cards+'</div>';
    }
    function historyView(){
      const years=history.years||[];
      if(!years.length)return '';
      const best=history.best_period||{};
      const official=history.current_official||{};
      const rows=years.map(function(y){
        const preliminary=y.best_period_tag==='historia_preliminar';
        const bestYear=y.best_period_tag==='mejor_epoca_historica';
        const sport=y.sport_context==='running'?'Correr':'Bici';
        const color=bestYear?'#3dd68c':y.sport_context==='running'?'#e8593c':'#22d3ee';
        const score=y.score==null?'--':Number(y.score).toFixed(1);
        const sub=preliminary?'preliminar · madurez '+Number(y.maturity||0).toFixed(0)+'%':sport+' · '+(y.active_weeks||0)+' semanas';
        return '<div style="display:grid;grid-template-columns:38px minmax(0,1fr) 42px;gap:9px;align-items:center;padding:8px 0;border-bottom:1px solid var(--line);opacity:'+(preliminary?'.58':'1')+'">'+
          '<div style="font-size:12px;font-weight:900">'+y.year+'</div>'+
          '<div style="min-width:0"><div style="height:7px;background:rgba(255,255,255,.07);overflow:hidden;border-radius:4px"><div style="height:100%;width:'+pct(y.score)+'%;background:'+color+'"></div></div><div style="font-size:10px;color:var(--muted);margin-top:4px;white-space:normal">'+sub+'</div></div>'+
          '<div style="text-align:right;font-size:13px;font-weight:950;color:'+color+'">'+score+'</div>'+
        '</div>';
      }).join('');
      const bestPeriod=best.period_start
        ?fmtDate(best.period_start)+' – '+fmtDate(best.period_end)
        :'Todavía sin una época madura';
      return '<div class="card" style="border-left:3px solid #3dd68c">'+
        '<div class="head"><h3>Historia del Motor Aeróbico</h3><span>bloques de 12 semanas</span></div>'+
        '<div class="grid2">'+
          metric('Hoy oficial',official.score!=null?Number(official.score).toFixed(1):'--','validación aceptada')+
          metric('Mejor época',best.score!=null?Number(best.score).toFixed(1):'--',best.year||'sin referencia')+
        '</div>'+
        '<div style="font-size:12px;line-height:1.5;margin:2px 0 10px"><strong>'+bestPeriod+'</strong><br><span style="color:var(--muted)">'+(best.sport_context==='running'?'Running':'Ciclismo')+' · '+(best.limitante_principal||'')+'</span></div>'+
        rows+
        '<div style="font-size:10px;line-height:1.45;color:var(--muted);padding-top:9px">Los años tenues siguen visibles, pero no compiten por “mejor época” hasta alcanzar 60% de madurez y 70% de confianza.</div>'+
      '</div>';
    }
    function bodyCompositionHistoryView(){
      const yearsAll=weightHistory.years||[];
      if(!yearsAll.length)return '';
      const best=weightHistory.best_period||{};
      const official=weightHistory.current_official||{};
      const ref=weightHistory.personal_reference||{};
      // v6.4: compactar años sin datos — no listarlos como si fueran errores
      const years=yearsAll.filter(function(y){return y.best_period_tag!=='sin_datos_suficientes'});
      const skipped=yearsAll.length-years.length;
      const measuredWeeks=years.reduce(function(a,y){return a+(y.measured_weeks||0)},0);
      const noDataNote=skipped>0?'<div style="font-size:10px;color:var(--muted);padding:7px 0;border-bottom:1px solid var(--line)">'+skipped+' año'+(skipped!==1?'s':'')+' sin registro de peso — no es un error: simplemente no había báscula conectada. Se necesitan ~12 semanas de peso para comparar épocas.</div>':'';
      const fewDataNote=(measuredWeeks>0&&measuredWeeks<12)?'<div style="font-size:10px;color:#f59e0b;padding:7px 0">Solo hay '+measuredWeeks+' semana'+(measuredWeeks!==1?'s':'')+' de peso registrado; se requieren ~12 para comparar épocas con confianza.</div>':'';
      const rows=years.map(function(y){
        const preliminary=y.best_period_tag==='historia_preliminar';
        const bestYear=y.best_period_tag==='mejor_epoca_historica';
        const color=bestYear?'#3dd68c':'#f59e0b';
        const score=y.score==null?'--':Number(y.score).toFixed(1);
        const minW=y.block_min_weight_kg!=null?y.block_min_weight_kg+' kg':'sin dato';
        const sub=preliminary?'preliminar · madurez '+Number(y.maturity||0).toFixed(0)+'%':'mín '+minW+' · '+(y.measured_weeks||0)+' semanas';
        const opacity=preliminary?'.52':'1';
        return '<div style="display:grid;grid-template-columns:38px minmax(0,1fr) 42px;gap:9px;align-items:center;padding:8px 0;border-bottom:1px solid var(--line);opacity:'+opacity+'">'+
          '<div style="font-size:12px;font-weight:900">'+y.year+'</div>'+
          '<div style="min-width:0"><div style="height:7px;background:rgba(255,255,255,.07);overflow:hidden;border-radius:4px"><div style="height:100%;width:'+pct(y.score)+'%;background:'+color+'"></div></div><div style="font-size:10px;color:var(--muted);margin-top:4px;white-space:normal">'+sub+'</div></div>'+
          '<div style="text-align:right;font-size:13px;font-weight:950;color:'+color+'">'+score+'</div>'+
        '</div>';
      }).join('')+noDataNote+fewDataNote;
      const bestPeriod=best.period_start
        ?fmtDate(best.period_start)+' – '+fmtDate(best.period_end)
        :'Todavía sin una época madura';
      const personalMin=ref.all_time_min_kg!=null?ref.all_time_min_kg+' kg':'--';
      return '<div class="card" style="border-left:3px solid #f59e0b">'+
        '<div class="head"><h3>Historia de Composición Corporal</h3><span>bloques de 12 semanas</span></div>'+
        '<div class="grid2">'+
          metric('Hoy oficial',official.score!=null?Number(official.score).toFixed(1):'--','validación aceptada')+
          metric('Mejor época',best.score!=null?Number(best.score).toFixed(1):'--',best.year||'sin referencia')+
          metric('Mínimo histórico',personalMin,'peso personal más bajo')+
          metric('Semanas medidas',ref.total_weeks_measured||0,'registros de peso')+
        '</div>'+
        '<div style="font-size:12px;line-height:1.5;margin:2px 0 10px"><strong>'+bestPeriod+'</strong><br><span style="color:var(--muted)">'+(best.block_min_weight_kg!=null?'Peso mínimo '+best.block_min_weight_kg+' kg':'')+' '+(best.limitante_principal||'')+'</span></div>'+
        rows+
        '<div style="font-size:10px;line-height:1.45;color:var(--muted);padding-top:9px">Score 100 = peso mínimo del bloque igualó el mínimo histórico personal en ese momento. Requiere madurez ≥40% y confianza ≥60% para competir por mejor época.</div>'+
      '</div>';
    }
    function climbingHistoryView(){
      const yearsAll=climbHistory.years||[];
      if(!yearsAll.length)return '';
      const best=climbHistory.best_period||{};
      const official=climbHistory.current_official||{};
      const ref=climbHistory.personal_reference||{};
      const years=yearsAll.filter(function(y){return y.best_period_tag!=='sin_datos_suficientes'});
      const skipped=yearsAll.length-years.length;
      const noDataNote=skipped>0?'<div style="font-size:10px;color:var(--muted);padding:7px 0;border-bottom:1px solid var(--line)">'+skipped+' año'+(skipped!==1?'s':'')+' sin desnivel registrado — omitidos, no son error de carga.</div>':'';
      const rows=years.map(function(y){
        const preliminary=y.best_period_tag==='historia_preliminar';
        const bestYear=y.best_period_tag==='mejor_epoca_historica';
        const color=bestYear?'#3dd68c':y.score>=90?'#22d3ee':'#4a9eff';
        const score=y.score==null?'--':Number(y.score).toFixed(1);
        const avgM=y.avg_weekly_ascent_m!=null?y.avg_weekly_ascent_m+' m/sem':'sin dato';
        const big=y.big_sessions!=null?' · '+y.big_sessions+' sesiones >500m':'';
        const sub=preliminary?'preliminar · madurez '+Number(y.maturity||0).toFixed(0)+'%':avgM+big;
        const opacity=preliminary?'.52':'1';
        return '<div style="display:grid;grid-template-columns:38px minmax(0,1fr) 42px;gap:9px;align-items:center;padding:8px 0;border-bottom:1px solid var(--line);opacity:'+opacity+'">'+
          '<div style="font-size:12px;font-weight:900">'+y.year+'</div>'+
          '<div style="min-width:0"><div style="height:7px;background:rgba(255,255,255,.07);overflow:hidden;border-radius:4px"><div style="height:100%;width:'+pct(y.score)+'%;background:'+color+'"></div></div><div style="font-size:10px;color:var(--muted);margin-top:4px;white-space:normal">'+sub+'</div></div>'+
          '<div style="text-align:right;font-size:13px;font-weight:950;color:'+color+'">'+score+'</div>'+
        '</div>';
      }).join('')+noDataNote;
      const bestPeriod=best.period_start
        ?fmtDate(best.period_start)+' – '+fmtDate(best.period_end)
        :'Todavía sin una época madura';
      const peakWeek=ref.all_time_peak_week_m!=null?ref.all_time_peak_week_m.toLocaleString('es-MX')+' m':'--';
      return '<div class="card" style="border-left:3px solid #22d3ee">'+
        '<div class="head"><h3>Historia de Fuerza-resistencia</h3><span>esfuerzo contra resistencia · bloques de 12 semanas</span></div>'+
        '<div style="font-size:10px;color:var(--muted);margin-bottom:8px">Mide cuánto desnivel sostienes por semana — esfuerzo sostenido contra resistencia.</div>'+
        '<div class="grid2">'+
          metric('Hoy oficial',official.score!=null?Number(official.score).toFixed(1):'--','validación aceptada')+
          metric('Mejor época',best.score!=null?Number(best.score).toFixed(1):'--',best.year||'sin referencia')+
          metric('Semana pico',peakWeek,'mayor desnivel registrado')+
          metric('Semanas escala',ref.total_climbing_weeks||0,'con desnivel > 0')+
        '</div>'+
        '<div style="font-size:12px;line-height:1.5;margin:2px 0 10px"><strong>'+bestPeriod+'</strong><br><span style="color:var(--muted)">'+(best.avg_weekly_ascent_m!=null?best.avg_weekly_ascent_m+' m/sem · ':'')+''+(best.limitante_principal||'')+'</span></div>'+
        rows+
        '<div style="font-size:10px;line-height:1.45;color:var(--muted);padding-top:9px">Score referenciado al p90 semanal histórico en ese momento. Requiere madurez ≥40% y confianza ≥60% para competir por mejor época.</div>'+
      '</div>';
    }
    function patronHistoricoView(){
      if(!patronData.ok||!(patronData.matches||[]).length)return '';
      const patronColor={'Pico de rendimiento':'#3dd68c','Descenso de forma':'#e8593c','Brecha de entrenamiento':'#f59e0b','Época estable':'#22d3ee','Sin datos suficientes':'#8b929f'};
      const patronIcon={'Pico de rendimiento':'↑','Descenso de forma':'↓','Brecha de entrenamiento':'—','Época estable':'→','Sin datos suficientes':'?'};
      const cards=(patronData.matches||[]).map(function(m){
        const col=patronColor[m.patron_label]||'#8b929f';
        const icon=patronIcon[m.patron_label]||'·';
        const sim=Math.round(Number(m.similarity)*100);
        const conf=Math.round(Number(m.confidence)*100);
        const effChange=m.trajectory_stats&&m.trajectory_stats.eficiencia_cambio_pct!=null
          ?(m.trajectory_stats.eficiencia_cambio_pct>0?'+':'')+m.trajectory_stats.eficiencia_cambio_pct+'% eficiencia'
          :'';
        const trajDots=(m.trajectory||[]).slice(0,8).map(function(t){
          const eff=t.cycling_efficiency;
          const h=eff!=null?Math.max(2,Math.min(20,Math.round(Number(eff)*120)))+'px':'2px';
          const c=eff!=null?col:'rgba(255,255,255,.12)';
          return '<div style="width:8px;height:'+h+';background:'+c+';border-radius:2px;align-self:flex-end" title="'+fmtDate(t.week)+'"></div>';
        }).join('');
        const stateItems=Object.entries(m.estado_entonces||{}).map(function(e){
          return e[1]!=null?'<span>'+e[0]+': '+e[1]+'</span>':'';
        }).filter(Boolean).join(' · ');
        return '<div class="card" style="border-left:3px solid '+col+'">'+
          '<div class="head"><h3>'+fmtDate(m.week)+'</h3><span style="color:'+col+'">'+icon+' '+m.patron_label+'</span></div>'+
          '<div style="display:flex;gap:8px;flex-wrap:wrap;margin:4px 0 8px;font-size:10px;color:var(--muted)">'+
            '<span>Similitud '+sim+'%</span>'+
            '<span>Confianza '+conf+'%</span>'+
            (effChange?'<span style="color:'+col+'">'+effChange+'</span>':'')+
          '</div>'+
          '<div style="font-size:10px;color:var(--muted);margin-bottom:8px">'+stateItems+'</div>'+
          '<div style="display:flex;gap:3px;align-items:flex-end;height:24px;padding:0 2px">'+trajDots+'</div>'+
          '<div style="font-size:9px;color:var(--muted);margin-top:4px">— 8 semanas después</div>'+
        '</div>';
      }).join('');
      const distItems=Object.entries(patronData.patron_distribution||{}).map(function(e){
        const col2=patronColor[e[0]]||'#8b929f';
        return '<span style="color:'+col2+'">'+e[0]+' ('+e[1]+')</span>';
      }).join(' · ');
      return '<div class="head" style="margin:16px 0 4px"><h3>Épocas similares en el historial</h3><span>¿qué pasó después?</span></div>'+
        '<div class="card" style="border-left:3px solid #4a9eff">'+
          '<div style="font-size:12px;line-height:1.55;color:var(--text);margin-bottom:10px">'+patronData.mensaje+'</div>'+
          (distItems?'<div style="font-size:10px;color:var(--muted);margin-bottom:4px">'+distItems+'</div>':'')+
        '</div>'+
        '<div class="grid2">'+cards+'</div>';
    }
    function capCard(c){
      const color=capColor(c);
      const score=c.score==null?'Sin score':Number(c.score).toFixed(1);
      const scoreUnit=c.score==null?'datos insuficientes':'de 100';
      const anchor=(c.anchors||{}).historical||{};
      const similar=(c.anchors||{}).similar_era||{};
      const environment=c.environment_context||{};
      const environmentHtml=environment.current_altitude_m!=null
        ?'<div style="margin:10px 0;padding:10px;background:rgba(34,211,238,.06);border:1px solid rgba(34,211,238,.14);border-radius:8px">'+
          '<div style="font-size:10px;font-weight:900;color:#22d3ee;margin-bottom:4px">CONTEXTO DE ALTITUD</div>'+
          '<div style="font-size:12px;line-height:1.55">Actual '+Number(environment.current_altitude_m).toLocaleString('es-MX',{maximumFractionDigits:0})+' m · habitual '+Number(environment.habitual_altitude_m||0).toLocaleString('es-MX',{maximumFractionDigits:0})+' m · '+(environment.comparison_method==='similar_altitude_history'?'comparación con '+environment.reference_sample+' sesiones a altitud semejante':'referencia general por falta de muestra comparable')+'</div>'+
          '<div style="font-size:10px;color:var(--muted);margin-top:3px">'+environment.prior_21d_exposure_days+' días de exposición comparable en 21 días · confianza '+Math.round(Number(environment.altitude_confidence||0)*100)+'%</div>'+
        '</div>'
        :environment.recent_avg_altitude_m!=null
        ?'<div style="margin:10px 0;padding:10px;background:rgba(34,211,238,.06);border:1px solid rgba(34,211,238,.14);border-radius:8px">'+
          '<div style="font-size:10px;font-weight:900;color:#22d3ee;margin-bottom:4px">ALTITUD RECIENTE</div>'+
          '<div style="font-size:12px">Promedio '+Number(environment.recent_avg_altitude_m).toLocaleString('es-MX',{maximumFractionDigits:0})+' m · máxima '+Number(environment.recent_max_altitude_m).toLocaleString('es-MX',{maximumFractionDigits:0})+' m · '+environment.high_altitude_sessions_8w+' sesiones altas</div>'+
        '</div>':'';
      const indicators=(c.indicators||[]).map(function(i){
        const value=i.actual==null?'--':Number(i.actual).toLocaleString('es-MX',{maximumFractionDigits:3})+(i.unit?' '+i.unit:'');
        const reference=i.reference==null?'sin referencia':'ref. '+Number(i.reference).toLocaleString('es-MX',{maximumFractionDigits:3})+(i.unit?' '+i.unit:'');
        return '<div class="row"><div class="r-main"><div class="r-title">'+i.label+'</div><div class="r-sub">'+reference+' · confianza '+Math.round(Number(i.confidence||0)*100)+'%</div></div><div class="r-val" style="color:'+color+'">'+value+'</div></div>';
      }).join('');
      const confPct=Math.round(Number(c.confidence||0)*100);
      const confBadge=confPct>=75
        ?'<span style="background:rgba(61,214,140,.15);color:#3dd68c;padding:2px 7px;border-radius:6px;font-size:9px;font-weight:900;margin-left:6px">Alta '+confPct+'%</span>'
        :confPct>=50
        ?'<span style="background:rgba(245,158,11,.15);color:#f59e0b;padding:2px 7px;border-radius:6px;font-size:9px;font-weight:900;margin-left:6px">Media '+confPct+'%</span>'
        :'<span style="background:rgba(232,89,60,.12);color:#e8593c;padding:2px 7px;border-radius:6px;font-size:9px;font-weight:900;margin-left:6px">Insuf. '+confPct+'%</span>';
      return '<div class="card" style="border-left:3px solid '+color+'">'+
        '<div class="head"><h3>'+c.nombre+confBadge+'</h3><span style="color:'+color+'">'+(c.status==='calculated'?'calculada':'por calibrar')+'</span></div>'+
        '<div class="grid2">'+
          metric('Capacidad',score,scoreUnit)+
          metric('Confianza',Math.round(Number(c.confidence||0)*100)+'%','calidad del dato')+
          metric('Madurez',Number(c.maturity||0).toFixed(0)+'%','calibración del modelo')+
          metric('Ancla similar',similar.similarity!=null?Math.round(Number(similar.similarity)*100)+'%':'--',similar.week?fmtDate(similar.week):'todavía no disponible')+
        '</div>'+
        (c.score!=null?'<div class="pbar" style="margin-top:10px"><div class="pfill" style="width:'+pct(c.score)+'%;background:'+color+'"></div></div>':'')+
        '<div style="display:flex;gap:8px;flex-wrap:wrap;margin:10px 0;font-size:10px;color:var(--muted)">'+
          '<span>Limitante: '+c.limitante_principal+'</span>'+
          '<span>Impulsor: '+c.impulsor_principal+'</span>'+
          (anchor.week?'<span>Mejor ancla: '+fmtDate(anchor.week)+'</span>':'')+
        '</div>'+
        environmentHtml+
        indicators+
        '<div style="font-size:12px;line-height:1.5;color:var(--text);padding-top:10px">'+c.recomendacion+'</div>'+
        '<details style="margin-top:12px;border-top:1px solid rgba(255,255,255,.07);padding-top:10px">'+
          '<summary style="cursor:pointer;font-size:11px;font-weight:700;color:var(--muted);letter-spacing:.05em;list-style:none;display:flex;align-items:center;gap:6px" onclick="loadAcademia(this,\''+c.key+'\')">'+
            '<span style="font-size:14px">📖</span> ¿QUÉ SIGNIFICA ESTE SCORE?'+
          '</summary>'+
          '<div class="academia-content" style="margin-top:10px;font-size:12px;line-height:1.6;color:var(--muted)">Cargando...</div>'+
        '</details>'+
      '</div>';
    }
    window.loadAcademia=function(summaryEl,key){
      const container=summaryEl.parentElement.querySelector('.academia-content');
      if(container.dataset.loaded)return;
      container.dataset.loaded='1';
      fetch(API+'/gpt/academia/'+key).then(function(r){return r.json();}).then(function(data){
        if(!data.ok){container.textContent='Sin información educativa para este indicador.';return;}
        const edu=data.educacion||{};
        const glosarioItems=Object.values(glosario).map(function(g){
          return '<div style="margin-bottom:8px"><strong style="color:var(--text)">'+g.término+':</strong> '+g.definición+'</div>';
        }).join('');
        let html='';
        if(edu.qué_es)html+='<div style="margin-bottom:8px"><strong style="color:var(--text)">¿Qué mide?</strong><br>'+edu.qué_es+'</div>';
        if(edu.qué_significa_score)html+='<div style="margin-bottom:8px"><strong style="color:var(--text)">Score 100 =</strong> '+edu.qué_significa_score+'</div>';
        if(edu.cómo_mejorar)html+='<div style="margin-bottom:8px"><strong style="color:var(--text)">Cómo mejorar:</strong><br>'+edu.cómo_mejorar+'</div>';
        const inds=edu.indicadores||{};
        const indKeys=Object.keys(inds);
        if(indKeys.length){
          html+='<div style="margin-top:8px"><strong style="color:var(--text)">Indicadores:</strong>';
          indKeys.forEach(function(k){html+='<div style="margin-top:4px;padding-left:10px;border-left:2px solid rgba(255,255,255,.1)"><em>'+k+'</em>: '+inds[k]+'</div>';});
          html+='</div>';
        }
        container.innerHTML=html||'Sin descripción disponible.';
      }).catch(function(){container.textContent='No se pudo cargar la información educativa.';});
    };
    // v6.4: capacidades actuales primero (calculadas antes que por calibrar), historia después
    const itemsSorted=[...items].sort(function(a,b){
      const ac2=a.status==='calculated'?0:1, bc=b.status==='calculated'?0:1;
      if(ac2!==bc)return ac2-bc;
      return Number(b.confidence||0)-Number(a.confidence||0);
    });
    el.innerHTML=
      '<div class="grid2">'+
        metric('Calculadas',calculated,'de '+items.length+' capacidades')+
        metric('Alta confianza',highConfidence,'confianza ≥75%')+
        metric('Historia',d.generated_from.snapshot_weeks,'semanas')+
        metric('Modelo','v1','historia personal')+
      '</div>'+
      '<div class="card"><div class="head"><h3>Lectura honesta</h3><span>score ≠ confianza</span></div><div style="font-size:12px;line-height:1.55;color:var(--muted)">Un score mide desarrollo. La confianza mide qué tan bien respaldado está. Cuando faltan datos, Epoch no convierte el vacío en una mala calificación — te dice qué falta y por qué.</div></div>'+
      '<div class="head" style="margin:14px 0 4px"><h3>Tus capacidades hoy</h3><span>score · confianza · evidencia</span></div>'+
      itemsSorted.map(capCard).join('')+
      readinessView()+
      '<div class="head" style="margin:16px 0 4px"><h3>Historia por capacidad</h3><span>bloques de 12 semanas</span></div>'+
      historyView()+
      climbingHistoryView()+
      bodyCompositionHistoryView()+
      patronHistoricoView();
  }catch(e){
    el.innerHTML='<div class="card" style="color:var(--muted)">'+e.message+'</div>';
  }
}




function screenFromPath(){
  const p=(location.pathname||'').replace(/^\/+/,'').replace(/\/+$/,'');
  if(!p||p==='app')return 'home';
  return TITLE[p]?p:'home';
}
go(screenFromPath(),false);
