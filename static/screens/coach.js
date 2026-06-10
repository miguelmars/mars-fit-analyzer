async function loadCoach(){
  const el=$('coach-data');
  try{
    const [mp,d,w,wh,ac,lastAct]=(await Promise.allSettled([
      fetch(API+'/gpt/mars-context').then(r=>r.json()),
      fetch(API+'/gpt/dashboard').then(r=>r.json()),
      fetch(API+'/gpt/wellness-summary?weeks=1').then(r=>r.json()),
      fetch(API+'/weight/history?limit=1').then(r=>r.json()),
      fetch(API+'/gpt/adaptive-coach').then(r=>r.json()),
      fetch(API+'/api/strava/activities?limit=1').then(r=>r.json())
    ])).map(r=>r.status==='fulfilled'?r.value:{});
    // Fetch limitante history sequentially (needs ac first)
    const _limCapKey=ac.limiting_capability||null;
    const limHistory=_limCapKey?await fetch(API+'/gpt/capacidad/'+_limCapKey+'/history').then(r=>r.ok?r.json():{}).catch(()=>({})):{};
    const z=mp.zonas_ciclismo||{};const _fallback=!mp.zonas_ciclismo,z2=z.z2||[134,150],z3=z.z3||[151,160];
    const plan=mp.plan_garmin||{},a=mp.athlete||{},bici=mp.bici||{};
    const nut=mp.nutricion||{},rutas=mp.rutas||[];
    const atleta=d.athlete||{};
    const pctZ2=parseFloat((d.z2_check||{}).pct_z2_4_semanas||0);
    const peso=wh.current_kg||a.peso_actual_kg||89.1;
    const pesoObj=a.peso_objetivo_kg||80;
    const fatiga=atleta.fatiga||'—';
    const marsIndex=parseFloat(atleta.mars_index||0);
    const effActual=mp.eff_actual||mp.eff_base||0.1483;
    const molestias=w.molestias_activas||[];
    const pains=molestias.length;
    let rec='',recCol='#4a9eff',recTitle='Seguir plan';
    if(pains>0){rec='Molestia(s): '+molestias.map(function(m){return m.pain_zone||'zona'}).join(', ')+'. Sesion Z1 max 108 bpm. Compex '+(mp.compex?mp.compex.recovery[0]:'Active Recovery')+'.';recCol='#e8593c';recTitle='Recuperar — molestias';}
    else if(fatiga==='alta'){rec='Fatiga alta. Compex '+(mp.compex?mp.compex.recovery[0]:'Active Recovery')+'. FC max 108 bpm, 30-40 min max.';recCol='#f59e0b';recTitle='Recuperar — fatiga';}
    else if(pctZ2<60){rec='Z2 en '+pctZ2.toFixed(0)+'% — bajo 70-80% objetivo. Salida Z2: '+(rutas[0]?rutas[0].nombre+' ~'+rutas[0].km+' km':'~21 km')+'. FC '+z2[0]+'–'+z2[1]+' bpm. Cadencia '+(mp.cadencia_obj||100)+' rpm. Gel '+(nut.gel||'agave casero')+' cada 45 min.';recCol='#3dd68c';recTitle='Construir Z2';}
    else if(effActual<(mp.eff_obj||0.155)*0.95){rec='Eficiencia '+effActual.toFixed(4)+' bajo obj '+(mp.eff_obj||0.155)+'. Sesion Z2 cadencia '+(mp.cadencia_obj||100)+' rpm, '+(rutas[0]?rutas[0].nombre:'~21 km')+'. FC '+z2[0]+'–'+z2[1]+' bpm.';recCol='#a78bfa';recTitle='Mejorar eficiencia';}
    else if(marsIndex>20){rec='Mars Index '+marsIndex.toFixed(1)+'. Agrega Tempo Z3 '+z3[0]+'–'+z3[1]+' bpm en los ultimos 20-30 min.';recCol='#a78bfa';recTitle='Subir intensidad';}
    else{rec='Continua '+( plan.nombre||'Garmin Coach')+' fase '+(plan.fase||'Base')+'. '+(rutas[0]?rutas[0].nombre+' ~'+rutas[0].km+' km':'~21 km')+'. FC '+z2[0]+'–'+z2[1]+' bpm, cadencia '+(mp.cadencia_obj||100)+' rpm.';recCol='#4a9eff';recTitle='Seguir plan';}
    // ── E29 Adaptive Coach block ─────────────────────────────────
    const phColors={base:'#3dd68c',build:'#f59e0b',peak:'#a78bfa',taper:'#4a9eff'};
    const phIcons={base:'🏗️',build:'⚡',peak:'🎯',taper:'🪫'};
    let acBlock='';
    if(ac && ac.prescription){
      const pr=ac.prescription;
      const phCol=phColors[ac.phase]||'#4a9eff';
      const phIco=phIcons[ac.phase]||'🎯';
      const goalLine=ac.goal?`<div style="font-size:12px;color:var(--muted);margin-bottom:8px">🎯 ${ac.goal.event_name}${ac.weeks_to_event!=null?' · '+ac.weeks_to_event+' semanas':''}</div>`:`<div style="font-size:12px;margin-bottom:8px;display:flex;align-items:center;gap:8px"><span style="color:var(--muted)">Sin meta activa</span><button onclick="go('metas')" style="background:rgba(167,139,250,.15);border:1px solid rgba(167,139,250,.35);color:#a78bfa;border-radius:8px;padding:3px 10px;font-size:11px;font-weight:800;cursor:pointer">+ Agregar meta →</button></div>`;
      const limScore=ac.limiting_score!=null?` · ${ac.limiting_score} pts`:'';
      const limLine=ac.limiting_capability?`<div style="font-size:11px;color:var(--muted);margin-top:8px">Capacidad limitante: <b style="color:${phCol}">${ac.limiting_capability}</b>${limScore}</div>`
        :`<div style="font-size:11px;color:var(--muted2);margin-top:8px">Sin datos de capacidades aún — backfill pendiente</div>`;
      acBlock=`<div class="card" style="border-left:3px solid ${phCol};margin-bottom:12px">
        <div class="head"><h3 style="color:${phCol}">${phIco} ${pr.fase_label}</h3><span>E29</span></div>
        ${goalLine}
        <div style="font-size:13px;line-height:1.7">
          ${pr.km_objetivo?`<b>${pr.km_objetivo} km/sem</b> · `:''}${pr.sesiones} sesiones · ${pr.z2_pct_objetivo}% Z2<br>
          <span style="color:var(--muted)">${pr.intensidad}</span><br>
          <span style="font-size:12px">${pr.foco_semana}</span>
        </div>
        ${limLine}
      </div>`;
    }
    // ────────────────────────────────────────────────────────────
    // Última actividad Strava como contexto del coach
    const la=(lastAct.activities||[])[0]||null;
    const daysAgo=la?Math.round((Date.now()-new Date(la.start_date+'T12:00:00').getTime())/(1000*86400)):null;
    const lastActCtx=la?
      `<div style="background:rgba(232,89,60,.07);border:1px solid rgba(232,89,60,.15);border-radius:12px;padding:9px 14px;margin-bottom:12px;display:flex;align-items:center;gap:10px">
        <span style="font-size:18px">${{Ride:'🚴',Run:'🏃',VirtualRide:'⚡',Walk:'🚶',Swim:'🏊'}[la.sport_type]||'🏅'}</span>
        <div style="flex:1;min-width:0">
          <div style="font-size:11px;font-weight:800;color:#e8593c">Última actividad${daysAgo===0?' · hoy':daysAgo===1?' · ayer':daysAgo!=null?' · hace '+daysAgo+'d':''}</div>
          <div style="font-size:11px;color:var(--muted)">${la.name||la.sport_type} · ${la.distance_km||'—'} km · ${la.duration_hms||'—'} · FC ${la.avg_hr||'—'} bpm</div>
        </div>
      </div>`:
      '<div style="font-size:11px;color:var(--muted);margin-bottom:12px">Sin actividades Strava recientes</div>';
    // ── v6.1: limitante primero, luego por qué, luego qué hacer ────────────
    const limCapCoach=ac.limiting_capability||null;
    const limScoreCoach=ac.limiting_score!=null?ac.limiting_score:null;
    const phColCoach={base:'#3dd68c',build:'#f59e0b',peak:'#a78bfa',taper:'#4a9eff'}[ac.phase]||'#a78bfa';
    const limitanteHero=limCapCoach?`
      <div style="background:linear-gradient(135deg,rgba(167,139,250,.18),rgba(167,139,250,.06));border:1px solid rgba(167,139,250,.35);border-radius:18px;padding:18px;margin-bottom:12px">
        <div style="font-size:10px;font-weight:900;text-transform:uppercase;letter-spacing:.1em;color:#a78bfa;margin-bottom:6px">¿Qué me está limitando?</div>
        <div style="font-size:22px;font-weight:950;margin-bottom:4px">${limCapCoach}</div>
        ${limScoreCoach!=null?`<div style="font-size:11px;color:var(--muted)">${limScoreCoach} pts · prioridad de trabajo ahora mismo</div>`:''}
        ${ac.goal?`<div style="font-size:11px;color:var(--muted);margin-top:6px">🎯 ${ac.goal.event_name}${ac.weeks_to_event!=null?' · '+ac.weeks_to_event+' semanas':''}</div>`:''}
      </div>`:'';
    // Historical context: today vs best era for the limitante
    const _limBest=limHistory.best_period||{};
    const histCtx=(_limBest.score!=null&&limScoreCoach!=null)?
      `<div style="background:rgba(167,139,250,.08);border:1px solid rgba(167,139,250,.18);border-radius:10px;padding:10px 14px;margin-bottom:12px">
        <div style="font-size:10px;font-weight:900;color:#a78bfa;margin-bottom:5px">HISTORIAL · ${(limCapCoach||'').toUpperCase()}</div>
        <div style="display:flex;gap:12px;flex-wrap:wrap;font-size:12px">
          <span>Hoy: <b style="color:${phColCoach}">${limScoreCoach} pts</b></span>
          <span>Mejor época: <b style="color:#3dd68c">${Number(_limBest.score).toFixed(1)} pts</b>${_limBest.year?' · '+_limBest.year:''}</span>
          <span style="color:var(--muted)">Gap: ${(Number(_limBest.score)-Number(limScoreCoach)).toFixed(1)} pts</span>
        </div>
        ${_limBest.avg_weekly_ascent_m!=null?`<div style="font-size:10px;color:var(--muted);margin-top:4px">En mejor época: ${_limBest.avg_weekly_ascent_m} m/sem de ascenso promedio</div>`:''}
      </div>`:'';
    const recCard=`<div class="card" style="border-left:3px solid ${recCol};margin-bottom:12px">
      <div class="head"><h3 style="color:${recCol}">${recTitle}</h3><span style="color:var(--muted)">${plan.fase||'Base'}</span></div>
      <div style="font-size:13px;line-height:1.65;margin-top:6px">${rec}</div>
    </div>`;
    el.innerHTML=
      limitanteHero+
      histCtx+
      recCard+
      acBlock+
      (pains?'<div class="card" style="border-color:rgba(232,89,60,.3);margin-bottom:12px"><div class="head"><h3 style="color:#e8593c">Molestias activas</h3><span>'+pains+'</span></div>'+molestias.map(function(m){return'<div class="row"><div class="r-main"><div class="r-title">'+(m.pain_zone||'—')+'</div></div><div class="r-val" style="color:#e8593c">Nivel '+(m.pain_level||'?')+'/10</div></div>';}).join('')+'</div>':'')+
      lastActCtx+
      '<div class="grid2" style="margin-bottom:12px">'+metric('Mars Index',marsIndex.toFixed(1),'fitness '+(atleta.fitness||'—'))+metric('Fatiga',fatiga,pctZ2.toFixed(0)+'% Z2')+metric('Peso',peso+' kg','→ '+pesoObj+' kg')+metric('Efic.',effActual.toFixed(4),'obj '+(mp.eff_obj||0.155)+'+')+
      '</div>'+
      '<div class="card"><div class="head"><h3>Zonas hoy</h3></div>'+
        [['Z2 Aerobico',z2[0]+'–'+z2[1],'#3dd68c'],['Z3 Tempo',z3[0]+'-'+z3[1],'#f59e0b'],['Z4 Umbral',(z.z4?z.z4[0]:161)+'-'+(z.z4?z.z4[1]:168),'#e8593c']].map(function(zz){return'<div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid rgba(255,255,255,.05)"><span style="font-size:12px">'+zz[0]+'</span><span style="font-size:12px;font-weight:800;color:'+zz[2]+'">'+zz[1]+' bpm</span></div>';}).join('')+
        '<div style="padding:7px 0;font-size:12px;color:#f59e0b;font-weight:800">Cadencia: '+(mp.cadencia_obj||100)+' rpm</div>'+
      '</div>';
  }catch(e){el.innerHTML='<div class="card" style="color:var(--muted)">'+e.message+'</div>'}
}
