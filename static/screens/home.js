async function loadHome(){
  try{
    const _homeSettled=await Promise.allSettled([
      fetch(API+'/gpt/dashboard').then(r=>r.json()),
      fetch(API+'/gpt/wellness-summary?weeks=1').then(r=>r.json()),
      fetch(API+'/gpt/mars-context').then(r=>r.json()),
      fetch(API+'/weight/history?limit=1').then(r=>r.json()),
      fetch(API+'/api/wellness-records?limit=30').then(r=>r.json()),
      fetch(API+'/api/strava/backfill-status').then(r=>r.json()),
      fetch(API+'/api/strava/activities?limit=1').then(r=>r.json()),
      fetch(API+'/gpt/adaptive-coach').then(r=>r.json()),
      fetch(API+'/api/strava/recent-weeks?weeks=2').then(r=>r.json())
    ]);
    const _hv=(i,fb)=>_homeSettled[i].status==='fulfilled'?_homeSettled[i].value:fb;
    const d=_hv(0,{}),w=_hv(1,{}),mp=_hv(2,{}),wh=_hv(3,{}),wr=_hv(4,[]),bf=_hv(5,{}),lastAct=_hv(6,{}),ac=_hv(7,{}),rw=_hv(8,{});
    const today=new Date().toISOString().slice(0,10);
    const wrList=(wr&&wr.records)?wr.records:(Array.isArray(wr)?wr:[]);
    const checkedToday=wrList.some(r=>r.date&&r.date.slice(0,10)===today&&r.hr_rest);
    // ── E25C streak ──────────────────────────────────────────────────────
    const datesWithCheck=new Set(wrList.filter(r=>r.hr_rest).map(r=>(r.date||'').slice(0,10)));
    const checkCount=datesWithCheck.size;
    let streak=0;
    for(let i=0;i<30;i++){const dd=new Date();dd.setDate(dd.getDate()-i);if(datesWithCheck.has(dd.toISOString().slice(0,10)))streak++;else break;}
    const e25cActive=checkCount>=21;
    const e25cPct=Math.min(100,Math.round(checkCount/21*100));
    // ── Cards de estado ──────────────────────────────────────────────────
    const morningBanner=checkedToday?'':
      `<div style="background:rgba(74,158,255,.12);border:1px solid rgba(74,158,255,.35);border-radius:12px;padding:12px 14px;margin-bottom:10px">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
          <span style="font-size:22px">☀️</span>
          <div style="flex:1"><div style="font-size:13px;font-weight:800;color:#4a9eff">Check matutino</div>
          <div style="font-size:11px;color:var(--muted)">Complétalo aquí · activa E25C</div></div>
          <span onclick="go('wellness')" style="color:#4a9eff;font-size:18px;cursor:pointer" title="Abrir Wellness">›</span>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px">
          <div><label style="font-size:10px;color:var(--muted);display:block;margin-bottom:3px">FC reposo</label><input id="h-hr" type="number" placeholder="48" min="30" max="100" style="width:100%;box-sizing:border-box;font-size:18px;font-weight:800;color:#4a9eff;background:rgba(74,158,255,.08);border:1px solid rgba(74,158,255,.25);border-radius:8px;padding:6px;text-align:center"></div>
          <div><label style="font-size:10px;color:var(--muted);display:block;margin-bottom:3px">Fatiga 1-10</label><input id="h-fat" type="number" placeholder="3" min="1" max="10" style="width:100%;box-sizing:border-box;font-size:18px;font-weight:800;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);border-radius:8px;padding:6px;text-align:center"></div>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px">
          <div><label style="font-size:10px;color:var(--muted);display:block;margin-bottom:3px">Sueño h</label><input id="h-slp" type="number" step="0.5" placeholder="7.5" min="2" max="12" style="width:100%;box-sizing:border-box;font-size:18px;font-weight:800;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);border-radius:8px;padding:6px;text-align:center"></div>
          <div><label style="font-size:10px;color:var(--muted);display:block;margin-bottom:3px">Estado 1-10</label><input id="h-estado" type="number" placeholder="8" min="1" max="10" style="width:100%;box-sizing:border-box;font-size:18px;font-weight:800;color:#3dd68c;background:rgba(61,214,140,.08);border:1px solid rgba(61,214,140,.2);border-radius:8px;padding:6px;text-align:center"></div>
        </div>
        <button onclick="saveMorningCheckHome()" style="width:100%;padding:10px;background:#4a9eff;border:none;border-radius:10px;color:#fff;font-size:13px;font-weight:800;cursor:pointer">Guardar check matutino</button>
      </div>`;
    const e25cCard=`<div onclick="go('wellness')" style="background:rgba(74,28,107,.16);border:1px solid rgba(74,28,107,.38);border-radius:12px;padding:10px 14px;margin-bottom:10px;cursor:pointer;display:flex;align-items:center;gap:12px">
      <span style="font-size:20px">${e25cActive?'✅':'🎯'}</span>
      <div style="flex:1;min-width:0">
        <div style="font-size:12px;font-weight:800;color:#a78bfa">E25C Recuperación · ${checkCount}/21 días</div>
        <div style="height:4px;border-radius:2px;background:rgba(74,28,107,.3);margin-top:5px;overflow:hidden"><div style="height:100%;width:${e25cPct}%;background:${e25cActive?'#22c55e':'#a78bfa'};border-radius:2px"></div></div>
      </div>
      <div style="text-align:right;flex-shrink:0"><div style="font-size:14px;font-weight:900;color:#4a9eff">🔥 ${streak}d</div><div style="font-size:9px;color:var(--muted)">racha</div></div>
    </div>`;
    // ── Capacidad limitante (v6.1 ADR-018) ──────────────────────────────────
    const limCap=ac.limiting_capability||null;
    const limScore=ac.limiting_score!=null?ac.limiting_score:null;
    const phColors={base:'#3dd68c',build:'#f59e0b',peak:'#a78bfa',taper:'#4a9eff'};
    const limCol=phColors[ac.phase]||'#a78bfa';
    const limCard=limCap?`<div onclick="go('coach')" style="background:rgba(167,139,250,.1);border:1px solid rgba(167,139,250,.3);border-radius:12px;padding:10px 14px;margin-bottom:10px;cursor:pointer;display:flex;align-items:center;gap:10px">
      <span style="font-size:20px">⚡</span>
      <div style="flex:1;min-width:0">
        <div style="font-size:10px;font-weight:900;text-transform:uppercase;letter-spacing:.07em;color:#a78bfa;margin-bottom:2px">Capacidad limitante</div>
        <div style="font-size:14px;font-weight:950;color:var(--text)">${limCap}</div>
        <div style="font-size:10px;color:var(--muted);margin-top:1px">${limScore!=null?limScore+' pts · ':''}Toca para ver qué trabajar</div>
      </div>
      <span style="color:#a78bfa;font-size:18px">›</span>
    </div>`:
    (ac.prescription?`<div onclick="go('coach')" style="background:rgba(74,158,255,.08);border:1px solid rgba(74,158,255,.2);border-radius:12px;padding:10px 14px;margin-bottom:10px;cursor:pointer;display:flex;align-items:center;gap:10px">
      <span style="font-size:20px">🧠</span>
      <div style="flex:1;min-width:0">
        <div style="font-size:10px;font-weight:900;text-transform:uppercase;letter-spacing:.07em;color:#4a9eff;margin-bottom:2px">Coach hoy</div>
        <div style="font-size:13px;font-weight:800">${ac.prescription.foco_semana||'Ver recomendación'}</div>
      </div>
      <span style="color:#4a9eff;font-size:18px">›</span>
    </div>`:'');
    const bfPct=bf.pct_streams||0;
    const stravaCard=!bf.total_activities?'':bfPct>=100
      ?`<div onclick="go('activities')" style="background:rgba(34,197,94,.07);border:1px solid rgba(34,197,94,.22);border-radius:12px;padding:9px 14px;margin-bottom:10px;cursor:pointer;display:flex;align-items:center;gap:10px">
          <span style="font-size:20px">✅</span>
          <div style="flex:1"><div style="font-size:12px;font-weight:800;color:#22c55e">Strava · ${(bf.total_activities||0).toLocaleString()} actividades</div>
          <div style="font-size:10px;color:var(--muted)">Streams completos · HR · GPS · CAD · POW</div></div>
          <span style="color:#22c55e;font-size:18px">›</span>
        </div>`
      :`<div style="background:rgba(34,197,94,.07);border:1px solid rgba(34,197,94,.18);border-radius:12px;padding:9px 14px;margin-bottom:10px">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px">
            <div style="font-size:12px;font-weight:800;color:#22c55e">📡 Strava streams · ${bfPct}%</div>
            <div style="font-size:10px;color:var(--muted)">${bf.streams_downloaded||0} / ${bf.total_activities||0}</div>
          </div>
          <div style="height:3px;border-radius:2px;background:rgba(34,197,94,.15);overflow:hidden"><div style="height:100%;width:${bfPct}%;background:#22c55e;border-radius:2px"></div></div>
          ${bf.eta_days?`<div style="font-size:10px;color:var(--muted);margin-top:3px">ETA ~${bf.eta_days} día${bf.eta_days!==1?'s':''} · corriendo automático</div>`:''}
        </div>`;
    const a=d.athlete||{},s=d.semana_actual||{},z=d.z2_check||{};
    // Delta vs semana anterior (v6.3)
    const rwWeeks=(rw.weeks||[]).slice(-2);
    const _curW=rwWeeks[rwWeeks.length-1]||{};
    const _prevW=rwWeeks[rwWeeks.length-2]||{};
    function homeDelta(cur,prev,dec,suf){
      const d=Number(cur||0)-Number(prev||0);
      if(!prev||Math.abs(d)<0.05)return '';
      const col=d>0?'#3dd68c':'#e8593c';
      return ' <span style="font-size:9px;color:'+col+';font-weight:900">'+(d>0?'↑':'↓')+Math.abs(d).toFixed(dec)+(suf||'')+'</span>';
    }
    const dKm=homeDelta(Number(_curW.distance_km||0),Number(_prevW.distance_km||0),0,' km');
    const dH=homeDelta(Number(_curW.hours||0),Number(_prevW.hours||0),1,' h');
    const dSes=homeDelta(Number(_curW.sessions||0),Number(_prevW.sessions||0),0);
    const mpz=mp.zonas_ciclismo||{},z2=mpz.z2||[134,150];
    const usingFallback=!mp.zonas_ciclismo;
    const plan=mp.plan_garmin||{},bici=mp.bici||{};
    const peso=wh.current_kg||(mp.athlete||{}).peso_actual_kg||89.1;
    const pesoObj=(mp.athlete||{}).peso_objetivo_kg||80;
    const effActual=mp.eff_actual||mp.eff_base||0.1483;
    const effPct=Math.min(100,Math.round((effActual/(mp.eff_obj||0.155))*100));
    const pesoDiff=+(peso-pesoObj).toFixed(1);
    const pains=(w.molestias_activas||[]).length;
    // Última actividad Strava
    const la=(lastAct.activities||[])[0]||null;
    const lastActCard=la?`<div onclick="go('activities')" style="background:rgba(232,89,60,.07);border:1px solid rgba(232,89,60,.18);border-radius:12px;padding:10px 14px;margin-bottom:10px;cursor:pointer;display:flex;align-items:center;gap:10px">
      <span style="font-size:22px">${{Ride:'🚴',Run:'🏃',VirtualRide:'⚡',Walk:'🚶',Swim:'🏊'}[la.sport_type]||'🏅'}</span>
      <div style="flex:1;min-width:0">
        <div style="font-size:12px;font-weight:800;color:#e8593c;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${la.name||la.sport_type}</div>
        <div style="font-size:11px;color:var(--muted)">${la.start_date} · ${la.duration_hms||'—'} · ${la.distance_km||'—'} km · FC ${la.avg_hr||'—'} bpm</div>
      </div>
      <span style="color:#e8593c;font-size:18px">›</span>
    </div>`:'';
    // ── ADR Insight First: responder "¿Cómo estoy hoy?" antes de cualquier métrica
    const _fat=a.fatiga||'';
    const _laDays=la?Math.round((Date.now()-new Date(la.start_date+'T12:00:00').getTime())/86400000):null;
    let _insight='';
    if(pains>0){
      _insight='Tu cuerpo pide atención: hay molestia activa. Hoy, recuperar vale más que entrenar.';
    } else if(_fat==='alta'){
      _insight='Llegas con fatiga alta. El descanso de hoy es parte del entrenamiento, no una pausa.';
    } else if(_laDays===0){
      _insight='Ya entrenaste hoy. Lo que sigue —comer y descansar— es donde tu cuerpo construye.';
    } else if(_laDays===1){
      _insight='Ayer sumaste trabajo. Tu base aeróbica sigue creciendo'+(Number(s.km||0)>0?' — van '+Number(s.km).toFixed(0)+' km esta semana':'')+'.';
    } else if(_laDays!=null&&_laDays>=3){
      _insight='Llevas '+_laDays+' días sin sesión. La constancia es la capacidad que más cuesta recuperar.';
    } else {
      _insight='Todo en equilibrio. Tu proceso avanza'+(Number(s.km||0)>0?' — '+Number(s.km).toFixed(0)+' km esta semana':'')+'.';
    }
    // Countdown al evento si hay meta activa (clic → coach)
    const _goalLine=(ac&&ac.goal&&ac.weeks_to_event!=null)?
      '<div onclick="go(\'coach\')" style="font-size:11px;color:#fb923c;font-weight:800;margin-top:8px;cursor:pointer">⏱ '+ac.weeks_to_event+' semana'+(ac.weeks_to_event!==1?'s':'')+' para '+ac.goal.event_name+' →</div>':'';
    const insightHero='<div style="background:linear-gradient(135deg,rgba(255,255,255,.06),rgba(255,255,255,.02));border:1px solid var(--line);border-radius:18px;padding:16px 18px;margin-bottom:12px">'+
      '<div style="font-size:10px;font-weight:900;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin-bottom:6px">¿Cómo estoy hoy?</div>'+
      '<div style="font-size:15px;line-height:1.5;font-weight:650">'+_insight+'</div>'+
      _goalLine+
    '</div>';
    $('home-data').innerHTML=
      insightHero+
      morningBanner+
      limCard+
      lastActCard+
      e25cCard+
      stravaCard+
      '<div class="grid2">'+
        metric('Km semana',Number(s.km||0).toFixed(0),'km'+dKm)+
        metric('Horas',Number(s.horas||0).toFixed(1),'h'+dH)+
        metric('Sesiones',s.sesiones||0,'semana'+dSes)+
        metric('Z2',Number(z.pct_z2_4_semanas||0).toFixed(0)+'%','4 semanas')+
      '</div>'+
      (usingFallback?'<div style="background:rgba(232,89,60,.1);border:1px solid rgba(232,89,60,.3);border-radius:10px;padding:8px 12px;font-size:11px;color:#e8593c;margin-bottom:8px">⚠ Usando datos de respaldo — revisar conexion</div>':'')+'<div class="card" style="margin-bottom:8px">'+
        '<div style="display:flex;flex-wrap:wrap;gap:6px">'+
          '<span style="background:rgba(74,158,255,.1);color:#4a9eff;padding:3px 8px;border-radius:12px;font-size:11px;font-weight:800">'+(plan.fase||'Base')+'</span>'+
          '<span style="background:rgba(61,214,140,.1);color:#3dd68c;padding:3px 8px;border-radius:12px;font-size:11px;font-weight:800">Z2: '+z2[0]+'–'+z2[1]+' bpm</span>'+
          '<span style="background:rgba(245,158,11,.1);color:#f59e0b;padding:3px 8px;border-radius:12px;font-size:11px;font-weight:800">Cad: '+(mp.cadencia_obj||100)+' rpm</span>'+
          '<span style="background:rgba(232,89,60,.1);color:#e8593c;padding:3px 8px;border-radius:12px;font-size:11px;font-weight:800">'+(bici.nombre||'Rarotonga')+' '+(bici.km||716)+' km</span>'+
        '</div>'+
      '</div>'+
      '<div class="card"><div class="head"><h3>Estado del atleta</h3><span>'+(a.fitness||'—')+'</span></div>'+
        row('<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><circle cx="7" cy="7" r="5" stroke="currentColor" stroke-width="1.5"/><path d="M5 7l1.5 1.5L9 5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>','Fitness',d.recommendation||'Sin recomendacion',a.mars_index?Number(a.mars_index).toFixed(1):'—')+
        row('<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M7 2v5l3 2" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><circle cx="7" cy="7" r="5" stroke="currentColor" stroke-width="1.5"/></svg>','Fatiga',a.fatiga||'—','')+
        row('<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M7 3c0 0-4 3-4 6a4 4 0 008 0c0-3-4-6-4-6z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>','Molestias',pains?pains+' activa(s)':'Sin alertas','')+
        '<div class="row"><div class="r-ico">·</div><div class="r-main"><div class="r-title">Peso</div><div class="r-sub">M1: '+pesoObj+' kg · Final: '+((mp.athlete||{}).peso_meta_final_kg||70)+' kg'+(pesoDiff>0?' · faltan '+pesoDiff+' kg':' meta M1 lograda')+'</div></div><div class="r-val" style="color:#3dd68c">'+peso+' kg</div></div>'+
        '<div class="row"><div class="r-ico">·</div><div class="r-main"><div class="r-title">Eficiencia vel/FC</div><div class="r-sub">base 0.1483 → obj 0.155</div></div><div style="text-align:right"><div style="font-size:13px;font-weight:800;color:#a78bfa">'+effActual.toFixed(4)+'</div><div style="font-size:10px;color:var(--muted)">'+effPct+'%</div></div></div>'+
      '</div>';
  }catch(e){$('home-data').innerHTML='<div class="card" style="color:var(--muted)">Error: '+e.message+'</div>';}
}

async function saveMorningCheckHome(){
  const today=new Date().toISOString().slice(0,10);
  const hr=parseInt($('h-hr').value)||null;
  const fatigue=parseInt($('h-fat').value)||null;
  const sleep=parseFloat($('h-slp').value)||null;
  const estado=parseInt($('h-estado').value)||null;
  if(!hr&&!fatigue&&!sleep&&!estado){toast('Llena al menos un campo');return;}
  const body={date:today,category:'sleep',hr_rest:hr,fatigue:fatigue,sleep_hours:sleep,stress_level:estado};
  const d=await fetch(API+'/wellness',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json());
  if(d.ok){toast('☀️ Check matutino guardado');loadHome();}else{toast('Error al guardar');}
}
