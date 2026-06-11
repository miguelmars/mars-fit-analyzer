async function loadCoach(){
  const el=$('coach-data');
  try{
    const [mp,d,w,wh,ac,lastAct,tc,wi]=(await Promise.allSettled([
      fetch(API+'/gpt/mars-context').then(r=>r.json()),
      fetch(API+'/gpt/dashboard').then(r=>r.json()),
      fetch(API+'/gpt/wellness-summary?weeks=1').then(r=>r.json()),
      fetch(API+'/weight/history?limit=1').then(r=>r.json()),
      fetch(API+'/gpt/adaptive-coach').then(r=>r.json()),
      fetch(API+'/api/strava/activities?limit=1').then(r=>r.json()),
      fetch(API+'/gpt/training-context').then(r=>r.json()),
      fetch(API+'/gpt/weekly-intelligence').then(r=>r.json())
    ])).map(r=>r.status==='fulfilled'?r.value:{});
    // v6.5: si la última sesión tiene bloques, pedir su análisis (1 fetch extra)
    const _tcLast=(tc&&tc.last_session)||{};
    let wa=null;
    if(_tcLast.clean_session_id&&_tcLast.laps_count>1){
      wa=await fetch(API+'/gpt/session/'+_tcLast.clean_session_id+'/workout-analysis').then(r=>r.ok?r.json():null).catch(()=>null);
    }
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
    // ── v6.4: presente + meta/plan → explicación → sugerencia. Historial = contexto.
    const hasGoal=!!(ac&&ac.goal);
    const hasPlan=!!(plan&&plan.nombre);
    const fullContext=hasGoal||hasPlan;
    // obs = qué sabemos hoy · why = qué significa · sug = sugerencia
    let obs='',why='',sug='',recCol='#4a9eff',recTitle='Lectura de hoy';
    if(pains>0){
      obs='Molestia(s) activa(s): '+molestias.map(function(m){return m.pain_zone||'zona'}).join(', ')+'.';
      why='Entrenar con dolor suele alargar la recuperación y enmascarar la causa.';
      sug='Hoy conviene priorizar recuperación: sesión suave Z1 (max 108 bpm) o descanso. Compex '+(mp.compex?mp.compex.recovery[0]:'Active Recovery')+' puede ayudar.';
      recCol='#e8593c';recTitle='Recuperación primero';
    } else if(fatiga==='alta'){
      obs='Tu fatiga registrada está alta.';
      why='Con fatiga alta el cuerpo asimila peor la carga; insistir hoy aporta poco.';
      sug='Una sesión corta y suave (FC max 108 bpm, 30-40 min) o descanso completo mantienen el proceso sin frenar la adaptación.';
      recCol='#f59e0b';recTitle='Cuerpo pidiendo pausa';
    } else if(pctZ2<60){
      obs='Tu trabajo en Z2 está en '+pctZ2.toFixed(0)+'% de las últimas 4 semanas (referencia: 70-80%).';
      why='Z2 es la base aeróbica: la zona donde el cuerpo aprende a usar grasa y sostener esfuerzo. Es lo que estás construyendo ahora.';
      sug='Si hoy toca rodar: salida en Z2 ('+z2[0]+'–'+z2[1]+' bpm, cadencia '+(mp.cadencia_obj||100)+' rpm)'+(rutas[0]?', por ejemplo '+rutas[0].nombre+' ~'+rutas[0].km+' km':'')+'.';
      recCol='#3dd68c';recTitle='Base aeróbica en construcción';
    } else if(effActual<(mp.eff_obj||0.155)*0.95){
      obs='Tu eficiencia (velocidad por latido) está en '+effActual.toFixed(4)+', bajo el objetivo de '+(mp.eff_obj||0.155)+'.';
      why='La eficiencia mejora con volumen Z2 constante y cadencia alta — no con más intensidad.';
      sug='Sesión Z2 con cadencia '+(mp.cadencia_obj||100)+' rpm, FC '+z2[0]+'–'+z2[1]+' bpm.';
      recCol='#a78bfa';recTitle='Eficiencia en progreso';
    } else if(marsIndex>20){
      obs='Tu índice de fitness está en '+marsIndex.toFixed(1)+' y la base se ve sólida.';
      why='Cuando la base aguanta, añadir algo de tempo estimula sin romper el proceso.';
      sug='Si te sientes bien: agrega Tempo Z3 ('+z3[0]+'–'+z3[1]+' bpm) en los últimos 20-30 min de tu próxima salida.';
      recCol='#a78bfa';recTitle='Espacio para estimular';
    } else {
      obs='Carga, fatiga y recuperación se ven en equilibrio.';
      why='No hay señal que pida cambiar el rumbo.';
      sug=hasPlan?('Continuar '+plan.nombre+' fase '+(plan.fase||'Base')+': FC '+z2[0]+'–'+z2[1]+' bpm, cadencia '+(mp.cadencia_obj||100)+' rpm.'):('Mantener el patrón actual: rodar en Z2 ('+z2[0]+'–'+z2[1]+' bpm) sigue siendo lo más rentable.');
      recCol='#4a9eff';recTitle='Proceso estable';
    }
    // Sin meta ni plan: bajar fuerza de la prescripción
    if(!fullContext&&pains===0&&fatiga!=='alta'){
      sug='Sugerencia (no prescripción): '+sug.charAt(0).toLowerCase()+sug.slice(1);
    }
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
    // v6.5.4: Countdown al evento — "no llegues improvisando".
    // Conecta meta activa + fase del plan + qué exige el evento (capacidades).
    let countdownCard='';
    if(ac&&ac.goal&&ac.weeks_to_event!=null){
      const _g=ac.goal;
      const _wks=ac.weeks_to_event;
      const _phase=(tc&&tc.plan&&tc.plan.current_phase)||ac.phase||null;
      const _phaseFocus=(tc&&tc.plan&&tc.plan.phase_focus)||null;
      // Qué exige el evento, en capacidades humanas (por tipo, sin inventar targets)
      const _demands={cycling:'resistencia (la distancia) y fuerza-resistencia (las subidas)',
                      gravel:'resistencia, fuerza-resistencia y manejo',
                      running:'resistencia y economía de carrera',
                      climbing:'fuerza-resistencia sobre todo',
                      other:'resistencia y consistencia'}[_g.event_type]||'resistencia y consistencia';
      countdownCard='<div style="background:linear-gradient(135deg,rgba(251,146,60,.16),rgba(251,146,60,.04));border:1px solid rgba(251,146,60,.35);border-radius:18px;padding:16px 18px;margin-bottom:12px">'+
        '<div style="display:flex;align-items:baseline;gap:8px;margin-bottom:4px">'+
          '<span style="font-size:26px;font-weight:950;color:#fb923c">'+_wks+'</span>'+
          '<span style="font-size:13px;font-weight:800">semana'+(_wks!==1?'s':'')+' para '+_g.event_name+'</span>'+
        '</div>'+
        (_phase?'<div style="font-size:11px;color:var(--muted);margin-bottom:6px">Fase actual: <b style="color:#fb923c">'+_phase+'</b>'+(_phaseFocus?' — '+_phaseFocus:'')+'</div>':'')+
        '<div style="font-size:12px;line-height:1.55">Ese día va a exigir '+_demands+'. Cada semana de aquí a entonces construye —o no— esa base. La meta no es llegar: es llegar entendiendo qué construiste.</div>'+
      '</div>';
    }
    // v6.5.3: "Qué construiste esta semana" — desde /gpt/weekly-intelligence.
    // Si el endpoint falla (wi={}), la card simplemente no se muestra.
    let wiCard='';
    if(wi&&wi.ok&&wi.totals&&wi.totals.sessions>0){
      const capLabel={aerobic_fitness:'motor aeróbico',endurance:'resistencia',power:'potencia',strength_endurance:'fuerza-resistencia',recovery:'recuperación',tempo:'umbral'};
      const caps=Object.entries(wi.capacities_built_pct||{});
      const capsTxt=caps.map(function(e){return capLabel[e[0]]||e[0]}).slice(0,3).join(' · ');
      const dom=capLabel[wi.dominant_capacity]||wi.dominant_capacity||'—';
      const domPct=(wi.capacities_built_pct||{})[wi.dominant_capacity]||0;
      const structN=(wi.sessions||[]).filter(function(s){return s.structured}).length;
      const withHr=(wi.sessions||[]).filter(function(s){return s.avg_hr_bpm}).length;
      const nS=wi.totals.sessions;
      const confPct=nS?Math.round(withHr/nS*100):0;
      const confLabel=confPct>=80?'alta':confPct>=50?'media':'baja';
      const confCol=confPct>=80?'#3dd68c':confPct>=50?'#f59e0b':'#e8593c';
      const falta=[];
      if(!wi.has_goal)falta.push('meta activa');
      if(!wi.has_plan)falta.push('plan estructurado');
      const withLaps=(wi.sessions||[]).filter(function(s){return s.laps>0}).length;
      if(withLaps<nS)falta.push('laps en '+(nS-withLaps)+' sesiones');
      const capBars=caps.map(function(e){
        const col=e[0]===wi.dominant_capacity?'#3dd68c':'#4a9eff';
        return '<div style="display:grid;grid-template-columns:110px 1fr 34px;gap:8px;align-items:center;padding:3px 0">'+
          '<div style="font-size:11px;color:var(--muted)">'+(capLabel[e[0]]||e[0])+'</div>'+
          '<div style="height:5px;background:rgba(255,255,255,.07);border-radius:3px;overflow:hidden"><div style="height:100%;width:'+e[1]+'%;background:'+col+';border-radius:3px"></div></div>'+
          '<div style="text-align:right;font-size:11px;font-weight:800;color:'+col+'">'+e[1]+'%</div></div>';
      }).join('');
      wiCard='<div class="card" style="border-left:3px solid #3dd68c;margin-bottom:12px">'+
        '<div class="head"><h3 style="color:#3dd68c">Qué construiste esta semana</h3><span>'+nS+' sesiones · '+wi.totals.km+' km</span></div>'+
        '<div style="font-size:12px;line-height:1.6;margin-bottom:8px">'+
          '<b style="font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted)">Observación</b><br>'+
          nS+' sesiones, '+wi.totals.hours+' h. Semana previa: '+((wi.previous_week||{}).sessions||0)+' sesiones, '+((wi.previous_week||{}).km||0)+' km.<br>'+
          '<b style="font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted)">Interpretación</b><br>'+
          'El tiempo se concentró en '+dom+' ('+domPct+'%). '+structN+' de '+nS+' sesiones con estructura de bloques.'+
        '</div>'+
        '<div style="margin-bottom:8px"><b style="font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:#3dd68c">Capacidad construida</b>'+capBars+'</div>'+
        '<div style="display:flex;gap:10px;flex-wrap:wrap;font-size:10px;color:var(--muted)">'+
          '<span>Evidencia: '+withHr+'/'+nS+' con FC · '+withLaps+'/'+nS+' con laps</span>'+((wi.plan_compliance&&wi.plan_compliance.planned)?'<span>Plan: '+wi.plan_compliance.completed+'/'+wi.plan_compliance.planned+' cumplidas</span>':'')+
          '<span>Confianza: <b style="color:'+confCol+'">'+confLabel+'</b></span>'+
        '</div>'+
        (falta.length?'<div style="font-size:10px;color:var(--muted);margin-top:5px">Qué falta: '+falta.join(', ')+'</div>':'')+
      '</div>';
    }
    // v6.5: bloque de sesión estructurada (solo si hay análisis con laps)
    const waCard=(wa&&wa.ok&&wa.structured!=null&&(wa.summary||{}).total_blocks>1)?`
      <div class="card" style="border-left:3px solid #22d3ee;margin-bottom:12px">
        <div class="head"><h3 style="color:#22d3ee">${wa.structured?'Sesión estructurada':'Última sesión por bloques'}</h3><span>${wa.workout_type}</span></div>
        <div style="font-size:12px;line-height:1.6">${wa.explanation_text}</div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px;font-size:10px;color:var(--muted)">
          <span>${wa.summary.total_blocks} bloques</span>
          <span>${wa.summary.work_intervals} trabajo</span>
          <span>${wa.summary.recoveries} recuperación</span>
          ${wa.summary.work_recovery_ratio?`<span>ratio ${wa.summary.work_recovery_ratio}:1</span>`:''}
          <span>confianza ${Math.round((wa.confidence_score||0)*100)}%</span>
        </div>
        <div style="font-size:11px;color:#22d3ee;margin-top:6px">Construyó: ${wa.capacity_built}</div>
      </div>`:'';
    // ── v6.1: limitante primero, luego por qué, luego qué hacer ────────────
    const limCapCoach=ac.limiting_capability||null;
    const limScoreCoach=ac.limiting_score!=null?ac.limiting_score:null;
    const phColCoach={base:'#3dd68c',build:'#f59e0b',peak:'#a78bfa',taper:'#4a9eff'}[ac.phase]||'#a78bfa';
    // "Escalada" explicada en lenguaje claro, historial presentado como contexto
    const _capLabel={escalada:'Fuerza-resistencia',motor_aerobico:'Motor aeróbico',composicion_corporal:'Composición corporal'}[limCapCoach]||limCapCoach;
    const _capExplain={escalada:'Capacidad de sostener esfuerzo contra resistencia (subidas, viento, carga). Se mide con los metros de ascenso que sostienes por semana.',motor_aerobico:'Mide tu capacidad de sostener esfuerzo largo a FC controlada — la base de todo lo demás.',composicion_corporal:'Relación peso/potencia. En subidas, cada kg cuenta.'}[limCapCoach]||'';
    const limitanteHero=limCapCoach?`
      <div style="background:linear-gradient(135deg,rgba(167,139,250,.18),rgba(167,139,250,.06));border:1px solid rgba(167,139,250,.35);border-radius:18px;padding:18px;margin-bottom:12px">
        <div style="font-size:10px;font-weight:900;text-transform:uppercase;letter-spacing:.1em;color:#a78bfa;margin-bottom:6px">Tu área con más espacio de mejora</div>
        <div style="font-size:20px;font-weight:950;margin-bottom:4px">${_capLabel}</div>
        ${_capExplain?`<div style="font-size:11px;color:var(--muted);line-height:1.5;margin-bottom:4px">${_capExplain}</div>`:''}
        ${limScoreCoach!=null?`<div style="font-size:11px;color:var(--muted)">Score actual: ${limScoreCoach} pts — contexto, no orden de trabajo</div>`:''}
        ${ac.goal?`<div style="font-size:11px;color:var(--muted);margin-top:6px">🎯 ${ac.goal.event_name}${ac.weeks_to_event!=null?' · '+ac.weeks_to_event+' semanas':''}</div>`:''}
      </div>`:'';
    // Contexto incompleto: sin meta activa ni plan, el Coach no prescribe fuerte
    const contextCard=!fullContext?`
      <div class="card" style="border-left:3px solid #f59e0b;margin-bottom:12px">
        <div class="head"><h3 style="color:#f59e0b">Contexto incompleto</h3><span>Coach</span></div>
        <div style="font-size:13px;line-height:1.6;margin-bottom:10px">Puedo interpretar tu historial y tu estado de hoy, pero sin una meta activa o un plan actual no puedo decir con seguridad qué te toca hoy. Lo que ves abajo es lectura + sugerencia prudente.</div>
        <button onclick="go('metas')" style="background:rgba(245,158,11,.15);border:1px solid rgba(245,158,11,.35);color:#f59e0b;border-radius:10px;padding:8px 14px;font-size:12px;font-weight:800;cursor:pointer">+ Agregar meta activa →</button>
      </div>`:'';
    // Historical context: today vs best era for the limitante
    const _limBest=limHistory.best_period||{};
    const histCtx=(_limBest.score!=null&&limScoreCoach!=null)?
      `<div style="background:rgba(167,139,250,.08);border:1px solid rgba(167,139,250,.18);border-radius:10px;padding:10px 14px;margin-bottom:12px">
        <div style="font-size:10px;font-weight:900;color:#a78bfa;margin-bottom:5px">CONTEXTO HISTÓRICO · ${_capLabel.toUpperCase()}</div>
        <div style="display:flex;gap:12px;flex-wrap:wrap;font-size:12px">
          <span>Hoy: <b style="color:${phColCoach}">${limScoreCoach} pts</b></span>
          <span>Tu mejor época: <b style="color:#3dd68c">${Number(_limBest.score).toFixed(1)} pts</b>${_limBest.year?' · '+_limBest.year:''}</span>
        </div>
        <div style="font-size:10px;color:var(--muted);margin-top:4px">Ya estuviste ahí — referencia de lo que tu cuerpo puede construir, no una deuda.</div>
        ${_limBest.avg_weekly_ascent_m!=null?`<div style="font-size:10px;color:var(--muted);margin-top:2px">En esa época: ${_limBest.avg_weekly_ascent_m} m/sem de ascenso promedio</div>`:''}
      </div>`:'';
    const recCard=`<div class="card" style="border-left:3px solid ${recCol};margin-bottom:12px">
      <div class="head"><h3 style="color:${recCol}">${recTitle}</h3><span style="color:var(--muted)">${plan.fase||'hoy'}</span></div>
      <div style="font-size:13px;line-height:1.65;margin-top:6px">
        <div style="margin-bottom:8px"><b style="font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted)">Qué sabemos hoy</b><br>${obs}</div>
        <div style="margin-bottom:8px"><b style="font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted)">Qué significa</b><br>${why}</div>
        <div><b style="font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:${recCol}">Sugerencia</b><br>${sug}</div>
        ${!fullContext?'<div style="margin-top:8px;font-size:11px;color:var(--muted)">Para una lectura más precisa falta: meta activa o plan actual.</div>':''}
      </div>
    </div>`;
    el.innerHTML=
      countdownCard+
      contextCard+
      recCard+
      wiCard+
      waCard+
      limitanteHero+
      histCtx+
      acBlock+
      (pains?'<div class="card" style="border-color:rgba(232,89,60,.3);margin-bottom:12px"><div class="head"><h3 style="color:#e8593c">Molestias activas</h3><span>'+pains+'</span></div>'+molestias.map(function(m){return'<div class="row"><div class="r-main"><div class="r-title">'+(m.pain_zone||'—')+'</div></div><div class="r-val" style="color:#e8593c">Nivel '+(m.pain_level||'?')+'/10</div></div>';}).join('')+'</div>':'')+
      lastActCtx+
      '<div class="grid2" style="margin-bottom:12px">'+metric('Índice Epoch',marsIndex.toFixed(1),'fitness '+(atleta.fitness||'—'))+metric('Fatiga',fatiga,pctZ2.toFixed(0)+'% Z2')+metric('Peso',peso+' kg','→ '+pesoObj+' kg')+metric('Efic.',effActual.toFixed(4),'obj '+(mp.eff_obj||0.155)+'+')+
      '</div>'+
      '<div class="card"><div class="head"><h3>Zonas hoy</h3></div>'+
        [['Z2 Aerobico',z2[0]+'–'+z2[1],'#3dd68c'],['Z3 Tempo',z3[0]+'-'+z3[1],'#f59e0b'],['Z4 Umbral',(z.z4?z.z4[0]:161)+'-'+(z.z4?z.z4[1]:168),'#e8593c']].map(function(zz){return'<div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid rgba(255,255,255,.05)"><span style="font-size:12px">'+zz[0]+'</span><span style="font-size:12px;font-weight:800;color:'+zz[2]+'">'+zz[1]+' bpm</span></div>';}).join('')+
        '<div style="padding:7px 0;font-size:12px;color:#f59e0b;font-weight:800">Cadencia: '+(mp.cadencia_obj||100)+' rpm</div>'+
      '</div>';
  }catch(e){el.innerHTML='<div class="card" style="color:var(--muted)">'+e.message+'</div>'}
}
