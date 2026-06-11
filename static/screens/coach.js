async function loadCoach(){
  const el=$('coach-data');
  try{
    const [mp,d,w,wh,ac,lastAct,tc,wi,testRec,ta]=(await Promise.allSettled([
      fetch(API+'/gpt/mars-context').then(r=>r.json()),
      fetch(API+'/gpt/dashboard').then(r=>r.json()),
      fetch(API+'/gpt/wellness-summary?weeks=1').then(r=>r.json()),
      fetch(API+'/weight/history?limit=1').then(r=>r.json()),
      fetch(API+'/gpt/adaptive-coach').then(r=>r.json()),
      fetch(API+'/api/strava/activities?limit=1').then(r=>r.json()),
      fetch(API+'/gpt/training-context').then(r=>r.json()),
      fetch(API+'/gpt/weekly-intelligence').then(r=>r.json()),
      fetch(API+'/gpt/test-recommendation').then(r=>r.json()),
      fetch(API+'/gpt/today-adaptation').then(r=>r.json())
    ])).map(r=>r.status==='fulfilled'?r.value:{});
    // v6.5: si la última sesión tiene bloques, pedir su análisis (1 fetch extra)
    const _tcLast=(tc&&tc.last_session)||{};
    let wa=null,prog=null;
    if(_tcLast.clean_session_id&&_tcLast.laps_count>1){
      wa=await fetch(API+'/gpt/session/'+_tcLast.clean_session_id+'/workout-analysis').then(r=>r.ok?r.json():null).catch(()=>null);
    }
    // V7.1: progresión vs repetitions del mismo workout (ruta + intención)
    let ssum=null;
    if(_tcLast.clean_session_id){
      [prog,ssum]=await Promise.all([
        fetch(API+'/gpt/session/'+_tcLast.clean_session_id+'/progression').then(r=>r.ok?r.json():null).catch(()=>null),
        fetch(API+'/gpt/session/'+_tcLast.clean_session_id+'/streams-summary').then(r=>r.ok?r.json():null).catch(()=>null)
      ]);
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
      obs='Active niggle(s): '+molestias.map(function(m){return m.pain_zone||'area'}).join(', ')+'.';
      why='Training through pain tends to lengthen recovery and mask the cause.';
      sug='Today, prioritize recovery: easy Z1 (max 108 bpm) or rest. Compex '+(mp.compex?mp.compex.recovery[0]:'Active Recovery')+' can help.';
      recCol='#e8593c';recTitle='Recovery first';
    } else if(fatiga==='high'){
      obs='Your reported fatigue is high.';
      why='With high fatigue the body absorbs load poorly; pushing today adds little.';
      sug='A short easy session (HR max 108 bpm, 30-40 min) or full rest keeps the process going without blocking adaptation.';
      recCol='#f59e0b';recTitle='Body asking for a pause';
    } else if(pctZ2<60){
      obs='Your Z2 work sits at '+pctZ2.toFixed(0)+'% over the last 4 weeks (reference: 70-80%).';
      why='Z2 is the aerobic base: the zone where the body learns to burn fat and sustain effort. It is what you are building now.';
      sug='If today is a riding day: Z2 ride ('+z2[0]+'–'+z2[1]+' bpm, cadence '+(mp.cadencia_obj||100)+' rpm)'+(rutas[0]?', e.g. '+rutas[0].nombre+' ~'+rutas[0].km+' km':'')+'.';
      recCol='#3dd68c';recTitle='Aerobic base under construction';
    } else if(effActual<(mp.eff_obj||0.155)*0.95){
      obs='Your efficiency (speed per heartbeat) is '+effActual.toFixed(4)+', below the '+(mp.eff_obj||0.155)+' target.';
      why='Efficiency improves with steady Z2 volume and high cadence — not with more intensity.';
      sug='Z2 session at '+(mp.cadencia_obj||100)+' rpm cadence, HR '+z2[0]+'–'+z2[1]+' bpm.';
      recCol='#a78bfa';recTitle='Efficiency in progress';
    } else if(marsIndex>20){
      obs='Your fitness index is at '+marsIndex.toFixed(1)+' and the base looks solid.';
      why='When the base holds, adding some tempo stimulates without breaking the process.';
      sug='If you feel good: add Z3 tempo ('+z3[0]+'–'+z3[1]+' bpm) in the last 20-30 min of your next ride.';
      recCol='#a78bfa';recTitle='Room to stimulate';
    } else {
      obs='Load, fatigue and recovery look balanced.';
      why='No signal asking for a change of course.';
      sug=hasPlan?('Continue '+plan.nombre+' phase '+(plan.fase||'Base')+': HR '+z2[0]+'–'+z2[1]+' bpm, cadence '+(mp.cadencia_obj||100)+' rpm.'):('Keep the current pattern: riding Z2 ('+z2[0]+'–'+z2[1]+' bpm) is still your best return.');
      recCol='#4a9eff';recTitle='Steady process';
    }
    // V8.3 — Fuente única: si hay adaptación de hoy (check + plan), ELLA es la lectura
    let _taOverride=false;
    if(ta&&ta.ok&&ta.status&&ta.status!=='sin_lectura'&&pains===0){
      // (con molestia activa, la lectura de molestias SIEMPRE gana sobre la adaptación)
      _taOverride=true;
      recTitle=ta.status==='mantener'?'Green light for what is planned':ta.status==='precaucion'?'One eye on the body':'Today calls for adapting';
      recCol=ta.status==='mantener'?'#3dd68c':ta.status==='precaucion'?'#f59e0b':'#e8593c';
    }
    // Sin meta ni plan: bajar fuerza de la prescripción
    if(!fullContext&&pains===0&&fatiga!=='high'){
      sug='Suggestion (not a prescription): '+sug.charAt(0).toLowerCase()+sug.slice(1);
    }
    // ── E29 Adaptive Coach block ─────────────────────────────────
    const phColors={base:'#3dd68c',build:'#f59e0b',peak:'#a78bfa',taper:'#4a9eff'};
    const phIcons={base:'🏗️',build:'⚡',peak:'🎯',taper:'🪫'};
    let acBlock='';
    if(ac && ac.prescription){
      const pr=ac.prescription;
      const phCol=phColors[ac.phase]||'#4a9eff';
      const phIco=phIcons[ac.phase]||'🎯';
      const goalLine=ac.goal?`<div style="font-size:12px;color:var(--muted);margin-bottom:8px">🎯 ${ac.goal.event_name}${ac.weeks_to_event!=null?' · '+ac.weeks_to_event+' weeks':''}</div>`:`<div style="font-size:12px;margin-bottom:8px;display:flex;align-items:center;gap:8px"><span style="color:var(--muted)">Sin meta activa</span><button onclick="go('metas')" style="background:rgba(167,139,250,.15);border:1px solid rgba(167,139,250,.35);color:#a78bfa;border-radius:8px;padding:3px 10px;font-size:11px;font-weight:800;cursor:pointer">+ Add goal →</button></div>`;
      const limScore=ac.limiting_score!=null?` · ${ac.limiting_score} pts`:'';
      const _acCapLbl={escalada:'Strength-endurance',motor_aerobico:'Aerobic engine',composicion_corporal:'Body composition'}[ac.limiting_capability]||ac.limiting_capability;
      const limLine=ac.limiting_capability?`<div style="font-size:11px;color:var(--muted);margin-top:8px">Limiting capacity: <b style="color:${phCol}">${_acCapLbl}</b>${limScore}</div>`
        :`<div style="font-size:11px;color:var(--muted2);margin-top:8px">No capacity data yet — backfill pending</div>`;
      acBlock=`<div class="card" style="border-left:3px solid ${phCol};margin-bottom:12px">
        <div class="head"><h3 style="color:${phCol}">${phIco} ${pr.fase_label}</h3><span>E29</span></div>
        ${goalLine}
        <div style="font-size:13px;line-height:1.7">
          ${pr.km_objetivo?`<b>${pr.km_objetivo} km/sem</b> · `:''}${pr.sesiones} sessions · ${pr.z2_pct_objetivo}% Z2<br>
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
          <div style="font-size:11px;font-weight:800;color:#e8593c">Latest activity${daysAgo===0?' · today':daysAgo===1?' · yesterday':daysAgo!=null?' · '+daysAgo+'d ago':''}</div>
          <div style="font-size:11px;color:var(--muted)">${la.name||la.sport_type} · ${la.distance_km||'—'} km · ${la.duration_hms||'—'} · FC ${la.avg_hr||'—'} bpm</div>
          <div id="coach-ssum-line"></div>
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
      const _demands={cycling:'endurance (the distance) and strength-endurance (the climbs)',
                      gravel:'endurance, strength-endurance and handling',
                      running:'endurance and running economy',
                      climbing:'strength-endurance above all',
                      other:'endurance and consistency'}[_g.event_type]||'endurance and consistency';
      setPhaseAccent(_phase);
      countdownCard='<div onclick="go(\'plan\')" class="phase-hero" style="cursor:pointer;border:1px solid var(--line);border-radius:18px;padding:16px 18px;margin-bottom:12px">'+
        '<div style="display:flex;align-items:baseline;gap:8px;margin-bottom:4px">'+
          '<span class="phase-text" style="font-size:26px;font-weight:950">'+_wks+'</span>'+
          '<span style="font-size:13px;font-weight:800">semana'+(_wks!==1?'s':'')+' para '+_g.event_name+'</span>'+
        '</div>'+
        (_phase?'<div style="font-size:11px;color:var(--muted);margin-bottom:6px">Fase actual: <b style="color:#fb923c">'+_phase+'</b>'+(_phaseFocus?' — '+_phaseFocus:'')+'</div>':'')+
        '<div style="font-size:12px;line-height:1.55">That day will demand '+_demands+'. Every week between now and then builds that base — or does not. The goal is not just to arrive: it is to arrive understanding what you built.</div>'+
      '</div>';
    }
    // V7.3: Epoch Test — propuesta (no orden) cuando hay razón
    let testCard='';
    if(testRec&&testRec.ok&&testRec.recommended&&testRec.test){
      testCard='<div class="card" style="border-left:3px solid #c8f135;margin-bottom:12px">'+
        '<div class="head"><h3 style="color:#c8f135">Want to know where you stand?</h3><span>Epoch Test</span></div>'+
        '<div style="font-size:12px;line-height:1.6;margin-bottom:8px">'+testRec.explanation_text+'</div>'+
        '<div style="background:rgba(200,241,53,.07);border:1px solid rgba(200,241,53,.2);border-radius:10px;padding:10px 12px;font-size:11px;line-height:1.6">'+
          '<b>'+testRec.test.name+'</b><br>'+testRec.test.protocol+'<br>'+
          '<span style="color:var(--muted)">Where: '+testRec.test.where+' · '+testRec.test.updates+'</span>'+
        '</div></div>';
    }
    // v6.5.3: "Qué construiste esta semana" — desde /gpt/weekly-intelligence.
    // Si el endpoint falla (wi={}), la card simplemente no se muestra.
    let wiCard='';
    if(wi&&wi.ok&&wi.totals&&wi.totals.sessions>0){
      const capLabel={aerobic_fitness:'aerobic engine',endurance:'endurance',power:'power',strength_endurance:'strength-endurance',recovery:'recovery',tempo:'threshold'};
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
      if(!wi.has_goal)falta.push('active goal');
      if(!wi.has_plan)falta.push('structured plan');
      const withLaps=(wi.sessions||[]).filter(function(s){return s.laps>0}).length;
      if(withLaps<nS)falta.push('laps en '+(nS-withLaps)+' sessions');
      const capBars=caps.map(function(e){
        const col=e[0]===wi.dominant_capacity?'#3dd68c':'#4a9eff';
        return '<div style="display:grid;grid-template-columns:110px 1fr 34px;gap:8px;align-items:center;padding:3px 0">'+
          '<div style="font-size:11px;color:var(--muted)">'+(capLabel[e[0]]||e[0])+'</div>'+
          '<div style="height:5px;background:rgba(255,255,255,.07);border-radius:3px;overflow:hidden"><div style="height:100%;width:'+e[1]+'%;background:'+col+';border-radius:3px"></div></div>'+
          '<div style="text-align:right;font-size:11px;font-weight:800;color:'+col+'">'+e[1]+'%</div></div>';
      }).join('');
      wiCard='<div class="card" style="border-left:3px solid #3dd68c;margin-bottom:12px">'+
        '<div class="head"><h3 style="color:#3dd68c">What you built this week</h3><span>'+nS+' sessions · '+wi.totals.km+' km</span></div>'+
        '<div style="font-size:12px;line-height:1.6;margin-bottom:8px">'+
          '<b style="font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted)">Observation</b><br>'+
          nS+' sessions, '+wi.totals.hours+' h. Previous week: '+((wi.previous_week||{}).sessions||0)+' sessions, '+((wi.previous_week||{}).km||0)+' km.<br>'+
          '<b style="font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted)">Interpretation</b><br>'+
          'Time concentrated on '+dom+' ('+domPct+'%). '+structN+' of '+nS+' sessions had block structure.'+
        '</div>'+
        '<div style="margin-bottom:8px"><b style="font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:#3dd68c">Capacity built</b>'+capBars+'</div>'+
        '<div style="display:flex;gap:10px;flex-wrap:wrap;font-size:10px;color:var(--muted)">'+
          '<span>Evidence: '+withHr+'/'+nS+' with HR · '+withLaps+'/'+nS+' with laps</span>'+((wi.plan_compliance&&wi.plan_compliance.planned)?'<span>Plan: '+wi.plan_compliance.completed+'/'+wi.plan_compliance.planned+' cumplidas</span>':'')+
          '<span>Confidence: <b style="color:'+confCol+'">'+confLabel+'</b></span>'+
        '</div>'+
        (falta.length?'<div style="font-size:10px;color:var(--muted);margin-top:5px">Missing: '+falta.join(', ')+'</div>':'')+
      '</div>';
    }
    // V7.1: card de progresión — ¿mejoré en ESTE workout?
    let progCard='';
    if(prog&&prog.ok&&prog.comparison&&(prog.repetitions||[]).length>0){
      const v=prog.comparison.verdict;
      const vCol=v==='mejorando'?'#3dd68c':v==='por_debajo'?'#f59e0b':'#8e95a3';
      const vLabel=v==='mejorando'?'Improving':v==='por_debajo'?'Off day (not a trend)':'Consistent';
      progCard='<div class="card" style="border-left:3px solid '+vCol+';margin-bottom:12px">'+
        '<div class="head"><h3 style="color:'+vCol+'">Did I improve at this workout?</h3><span>'+(prog.repetitions.length)+' repetitions</span></div>'+
        '<div style="font-size:12px;line-height:1.6">'+prog.explanation_text+'</div>'+
        '<div style="font-size:10px;color:var(--muted);margin-top:6px">Verdict: <b style="color:'+vCol+'">'+vLabel+'</b> · compared only against days with the same intent on the same route</div>'+
      '</div>';
    }
    // v6.5: bloque de sesión estructurada (solo si hay análisis with laps)
    const waCard=(wa&&wa.ok&&wa.structured!=null&&(wa.summary||{}).total_blocks>1)?`
      <div class="card" style="border-left:3px solid #22d3ee;margin-bottom:12px">
        <div class="head"><h3 style="color:#22d3ee">${wa.structured?'Structured session':'Latest session by blocks'}</h3><span>${wa.workout_type}</span></div>
        <div style="font-size:12px;line-height:1.6">${wa.explanation_text}</div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px;font-size:10px;color:var(--muted)">
          <span>${wa.summary.total_blocks} blocks</span>
          <span>${wa.summary.work_intervals} work</span>
          <span>${wa.summary.recoveries} recovery</span>
          ${wa.summary.work_recovery_ratio?`<span>ratio ${wa.summary.work_recovery_ratio}:1</span>`:''}
          <span>confidence ${Math.round((wa.confidence_score||0)*100)}%</span>
        </div>
        <div style="font-size:11px;color:#22d3ee;margin-top:6px">It built: ${wa.capacity_built}</div>
        <div onclick="openSesion('${_tcLast.clean_session_id}')" style="font-size:11px;color:#22d3ee;font-weight:800;margin-top:8px;cursor:pointer">View full session →</div>
      </div>`:'';
    // ── v6.1: limitante primero, luego por qué, luego qué hacer ────────────
    const limCapCoach=ac.limiting_capability||null;
    const limScoreCoach=ac.limiting_score!=null?ac.limiting_score:null;
    const phColCoach={base:'#3dd68c',build:'#f59e0b',peak:'#a78bfa',taper:'#4a9eff'}[ac.phase]||'#a78bfa';
    // "Escalada" explicada en lenguaje claro, historial presentado como contexto
    const _capLabel={escalada:'Strength-endurance',motor_aerobico:'Aerobic engine',composicion_corporal:'Body composition'}[limCapCoach]||limCapCoach;
    const _capExplain={escalada:'The ability to sustain effort against resistance (climbs, wind, load). Measured by the meters of climbing you sustain per week.',motor_aerobico:'Your ability to sustain long effort at controlled HR — the base for everything else.',composicion_corporal:'Weight-to-power ratio. On climbs, every kg counts.'}[limCapCoach]||'';
    const limitanteHero=limCapCoach?`
      <div style="background:linear-gradient(135deg,rgba(167,139,250,.18),rgba(167,139,250,.06));border:1px solid rgba(167,139,250,.35);border-radius:18px;padding:18px;margin-bottom:12px">
        <div style="font-size:10px;font-weight:900;text-transform:uppercase;letter-spacing:.1em;color:#a78bfa;margin-bottom:6px">Your area with the most room to grow</div>
        <div style="font-size:20px;font-weight:950;margin-bottom:4px">${_capLabel}</div>
        ${_capExplain?`<div style="font-size:11px;color:var(--muted);line-height:1.5;margin-bottom:4px">${_capExplain}</div>`:''}
        ${limScoreCoach!=null?`<div style="font-size:11px;color:var(--muted)">Score actual: ${limScoreCoach} pts — contexto, no orden de trabajo</div>`:''}
        ${ac.goal?`<div style="font-size:11px;color:var(--muted);margin-top:6px">🎯 ${ac.goal.event_name}${ac.weeks_to_event!=null?' · '+ac.weeks_to_event+' weeks':''}</div>`:''}
      </div>`:'';
    // Contexto incompleto: sin meta activa ni plan, el Coach no prescribe fuerte
    const contextCard=!fullContext?`
      <div class="card" style="border-left:3px solid #f59e0b;margin-bottom:12px">
        <div class="head"><h3 style="color:#f59e0b">Incomplete context</h3><span>Coach</span></div>
        <div style="font-size:13px;line-height:1.6;margin-bottom:10px">I can read your history and your state today, but without an active goal or current plan I cannot say with certainty what today should be. What you see below is a read + a prudent suggestion.</div>
        <button onclick="go('metas')" style="background:rgba(245,158,11,.15);border:1px solid rgba(245,158,11,.35);color:#f59e0b;border-radius:10px;padding:8px 14px;font-size:12px;font-weight:800;cursor:pointer">+ Add an active goal →</button>
      </div>`:'';
    // Historical context: today vs best era for the limitante
    const _limBest=limHistory.best_period||{};
    const histCtx=(_limBest.score!=null&&limScoreCoach!=null)?
      `<div style="background:rgba(167,139,250,.08);border:1px solid rgba(167,139,250,.18);border-radius:10px;padding:10px 14px;margin-bottom:12px">
        <div style="font-size:10px;font-weight:900;color:#a78bfa;margin-bottom:5px">HISTORICAL CONTEXT · ${_capLabel.toUpperCase()}</div>
        <div style="display:flex;gap:12px;flex-wrap:wrap;font-size:12px">
          <span>Today: <b style="color:${phColCoach}">${limScoreCoach} pts</b></span>
          <span>Your best era: <b style="color:#3dd68c">${Number(_limBest.score).toFixed(1)} pts</b>${_limBest.year?' · '+_limBest.year:''}</span>
        </div>
        <div style="font-size:10px;color:var(--muted);margin-top:4px">You have been there before — a reference of what your body can build, not a debt.</div>
        ${_limBest.avg_weekly_ascent_m!=null?`<div style="font-size:10px;color:var(--muted);margin-top:2px">Back then: ${_limBest.avg_weekly_ascent_m} m/week average climbing</div>`:''}
      </div>`:'';
    const recCard=`<div class="card" style="border-left:3px solid ${recCol};margin-bottom:12px">
      <div class="q-kicker">What should I understand today?</div>
      <div class="head" style="margin-bottom:8px"><h3 style="color:${recCol}">${recTitle}</h3><span style="color:var(--muted)">${plan.fase||'hoy'}</span></div>
      <div style="font-size:13px;line-height:1.65;margin-top:6px">
        ${_taOverride?ta.explanation_text:
        '<div style="margin-bottom:8px"><b style="font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted)">What we know today</b><br>'+obs+'</div>'+
        '<div style="margin-bottom:8px"><b style="font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted)">What it means</b><br>'+why+'</div>'+
        '<div><b style="font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:'+recCol+'">Suggestion</b><br>'+sug+'</div>'+
        (!fullContext?'<div style="margin-top:8px;font-size:11px;color:var(--muted)">For a sharper read, missing: an active goal or current plan.</div>':'')}
      </div>
      ${fbBtns('coach-lectura')}
    </div>`;
    // V8 UI: esencial visible · evidencia colapsada · sin duplicados
    const _evidenciaCoach=
      acBlock+
      limitanteHero+
      histCtx+
      lastActCtx+
      '<div class="grid2" style="margin-bottom:12px">'+metric('Epoch Index',marsIndex.toFixed(1),'fitness '+(atleta.fitness||'—'))+metric('Fatigue',fatiga,pctZ2.toFixed(0)+'% Z2')+metric('Weight',peso+' kg','→ '+pesoObj+' kg')+metric('Efficiency',effActual.toFixed(4),'speed per heartbeat')+
      '</div>'+
      '<div class="card"><div class="head"><h3>Zones today</h3></div>'+
        [['Z2 Aerobic',z2[0]+'–'+z2[1],'#3dd68c'],['Z3 Tempo',z3[0]+'-'+z3[1],'#f59e0b'],['Z4 Threshold',(z.z4?z.z4[0]:161)+'-'+(z.z4?z.z4[1]:168),'#e8593c']].map(function(zz){return'<div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid rgba(255,255,255,.05)"><span style="font-size:12px">'+zz[0]+'</span><span style="font-size:12px;font-weight:800;color:'+zz[2]+'">'+zz[1]+' bpm</span></div>';}).join('')+
        '<div style="padding:7px 0;font-size:12px;color:#f59e0b;font-weight:800">Cadencia: '+(mp.cadencia_obj||100)+' rpm</div>'+
      '</div>';
    el.innerHTML=
      countdownCard+
      contextCard+
      recCard+
      (pains?'<div class="card" style="border-color:rgba(232,89,60,.3);margin-bottom:12px"><div class="head"><h3 style="color:#e8593c">Active niggles</h3><span>'+pains+'</span></div>'+molestias.map(function(m){return'<div class="row"><div class="r-main"><div class="r-title">'+(m.pain_zone||'—')+'</div></div><div class="r-val" style="color:#e8593c">Nivel '+(m.pain_level||'?')+'/10</div></div>';}).join('')+'</div>':'')+
      wiCard+
      testCard+
      progCard+
      waCard+
      evd('Evidence and detail',_evidenciaCoach);
    // V7.5: lectura de streams de la última sesión (decoupling/calor/pausas)
    if(ssum&&ssum.available){
      const lineEl=document.getElementById('coach-ssum-line');
      if(lineEl){
        const bits=[];
        if(ssum.decoupling_reading)bits.push(ssum.decoupling_reading);
        if(ssum.temp_avg_c!=null&&ssum.temp_avg_c>=27)bits.push('Heat: '+ssum.temp_avg_c+'°C average — HR runs higher in heat; not everything is fatigue.');
        if(ssum.pauses_count!=null&&ssum.pauses_count>2)bits.push(ssum.pauses_count+' stops ('+Math.round((ssum.paused_time_s||0)/60)+' min stopped).');
        if(bits.length)lineEl.innerHTML='<div style="font-size:10px;color:#22d3ee;line-height:1.5;margin-top:4px">'+bits.join(' ')+'</div>';
      }
    }
  }catch(e){el.innerHTML='<div class="card" style="color:var(--muted)">'+e.message+'</div>'}
}
