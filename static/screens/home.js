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
      fetch(API+'/api/strava/recent-weeks?weeks=2').then(r=>r.json()),
      fetch(API+'/gpt/training-context').then(r=>r.json()),
      fetch(API+'/gpt/today-adaptation').then(r=>r.json()),
      fetch(API+'/gpt/today-options').then(r=>r.json()),
      fetch(API+'/gpt/options-outcome').then(r=>r.json())
    ]);
    const _hv=(i,fb)=>_homeSettled[i].status==='fulfilled'?_homeSettled[i].value:fb;
    const d=_hv(0,{}),w=_hv(1,{}),mp=_hv(2,{}),wh=_hv(3,{}),wr=_hv(4,[]),bf=_hv(5,{}),lastAct=_hv(6,{}),ac=_hv(7,{}),rw=_hv(8,{}),tc=_hv(9,{}),ta=_hv(10,{}),topt=_hv(11,{}),outc=_hv(12,{});
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
          <div style="flex:1"><div style="font-size:13px;font-weight:800;color:#4a9eff">Morning check</div>
          <div style="font-size:11px;color:var(--muted)">Complete it here · unlocks E25C</div></div>
          <span onclick="go('wellness')" style="color:#4a9eff;font-size:18px;cursor:pointer" title="Open Wellness">›</span>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px">
          <div><label style="font-size:10px;color:var(--muted);display:block;margin-bottom:3px">Resting HR</label><input id="h-hr" type="number" placeholder="48" min="30" max="100" style="width:100%;box-sizing:border-box;font-size:18px;font-weight:800;color:#4a9eff;background:rgba(74,158,255,.08);border:1px solid rgba(74,158,255,.25);border-radius:8px;padding:6px;text-align:center"></div>
          <div><label style="font-size:10px;color:var(--muted);display:block;margin-bottom:3px">Fatigue 1-10</label><input id="h-fat" type="number" placeholder="3" min="1" max="10" style="width:100%;box-sizing:border-box;font-size:18px;font-weight:800;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);border-radius:8px;padding:6px;text-align:center"></div>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px">
          <div><label style="font-size:10px;color:var(--muted);display:block;margin-bottom:3px">Sleep h</label><input id="h-slp" type="number" step="0.5" placeholder="7.5" min="2" max="12" style="width:100%;box-sizing:border-box;font-size:18px;font-weight:800;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);border-radius:8px;padding:6px;text-align:center"></div>
          <div><label style="font-size:10px;color:var(--muted);display:block;margin-bottom:3px">State 1-10</label><input id="h-estado" type="number" placeholder="8" min="1" max="10" style="width:100%;box-sizing:border-box;font-size:18px;font-weight:800;color:#3dd68c;background:rgba(61,214,140,.08);border:1px solid rgba(61,214,140,.2);border-radius:8px;padding:6px;text-align:center"></div>
        </div>
        <button onclick="saveMorningCheckHome()" style="width:100%;padding:10px;background:#4a9eff;border:none;border-radius:10px;color:#fff;font-size:13px;font-weight:800;cursor:pointer">Save morning check</button>
      </div>`;
    const e25cCard=`<div onclick="go('wellness')" style="background:rgba(74,28,107,.16);border:1px solid rgba(74,28,107,.38);border-radius:12px;padding:10px 14px;margin-bottom:10px;cursor:pointer;display:flex;align-items:center;gap:12px">
      <span style="font-size:20px">${e25cActive?'✅':'🎯'}</span>
      <div style="flex:1;min-width:0">
        <div style="font-size:12px;font-weight:800;color:#a78bfa">E25C Recovery · ${checkCount}/21 days</div>
        <div style="height:4px;border-radius:2px;background:rgba(74,28,107,.3);margin-top:5px;overflow:hidden"><div style="height:100%;width:${e25cPct}%;background:${e25cActive?'#22c55e':'#a78bfa'};border-radius:2px"></div></div>
      </div>
      <div style="text-align:right;flex-shrink:0"><div style="font-size:14px;font-weight:900;color:#4a9eff">🔥 ${streak}d</div><div style="font-size:9px;color:var(--muted)">streak</div></div>
    </div>`;
    // ── Capacidad limitante (v6.1 ADR-018) ──────────────────────────────────
    const limCap=ac.limiting_capability||null;
    const limScore=ac.limiting_score!=null?ac.limiting_score:null;
    const phColors={base:'#3dd68c',build:'#f59e0b',peak:'#a78bfa',taper:'#4a9eff'};
    const limCol=phColors[ac.phase]||'#a78bfa';
    const limCard=limCap?`<div onclick="go('coach')" style="background:rgba(167,139,250,.1);border:1px solid rgba(167,139,250,.3);border-radius:12px;padding:10px 14px;margin-bottom:10px;cursor:pointer;display:flex;align-items:center;gap:10px">
      <span style="font-size:20px">⚡</span>
      <div style="flex:1;min-width:0">
        <div style="font-size:10px;font-weight:900;text-transform:uppercase;letter-spacing:.07em;color:#a78bfa;margin-bottom:2px">Limiting capacity</div>
        <div style="font-size:14px;font-weight:950;color:var(--text)">${limCap}</div>
        <div style="font-size:10px;color:var(--muted);margin-top:1px">${limScore!=null?limScore+' pts · ':''}Tap to see what to work on</div>
      </div>
      <span style="color:#a78bfa;font-size:18px">›</span>
    </div>`:
    (ac.prescription?`<div onclick="go('coach')" style="background:rgba(74,158,255,.08);border:1px solid rgba(74,158,255,.2);border-radius:12px;padding:10px 14px;margin-bottom:10px;cursor:pointer;display:flex;align-items:center;gap:10px">
      <span style="font-size:20px">🧠</span>
      <div style="flex:1;min-width:0">
        <div style="font-size:10px;font-weight:900;text-transform:uppercase;letter-spacing:.07em;color:#4a9eff;margin-bottom:2px">Coach today</div>
        <div style="font-size:13px;font-weight:800">${ac.prescription.foco_semana||'See guidance'}</div>
      </div>
      <span style="color:#4a9eff;font-size:18px">›</span>
    </div>`:'');
    const bfPct=bf.pct_streams||0;
    const stravaCard=!bf.total_activities?'':bfPct>=100
      ?`<div onclick="go('activities')" style="background:rgba(34,197,94,.07);border:1px solid rgba(34,197,94,.22);border-radius:12px;padding:9px 14px;margin-bottom:10px;cursor:pointer;display:flex;align-items:center;gap:10px">
          <span style="font-size:20px">✅</span>
          <div style="flex:1"><div style="font-size:12px;font-weight:800;color:#22c55e">Strava · ${(bf.total_activities||0).toLocaleString()} activities</div>
          <div style="font-size:10px;color:var(--muted)">Full streams · HR · GPS · CAD · POW</div></div>
          <span style="color:#22c55e;font-size:18px">›</span>
        </div>`
      :`<div style="background:rgba(34,197,94,.07);border:1px solid rgba(34,197,94,.18);border-radius:12px;padding:9px 14px;margin-bottom:10px">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px">
            <div style="font-size:12px;font-weight:800;color:#22c55e">📡 Strava streams · ${bfPct}%</div>
            <div style="font-size:10px;color:var(--muted)">${bf.streams_downloaded||0} / ${bf.total_activities||0}</div>
          </div>
          <div style="height:3px;border-radius:2px;background:rgba(34,197,94,.15);overflow:hidden"><div style="height:100%;width:${bfPct}%;background:#22c55e;border-radius:2px"></div></div>
          ${bf.eta_days?`<div style="font-size:10px;color:var(--muted);margin-top:3px">ETA ~${bf.eta_days} day${bf.eta_days!==1?'s':''} · running automatically</div>`:''}
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
    let latestWa=null;
    if(la&&la.strava_id){
      latestWa=await fetch(API+'/gpt/session/strava_'+la.strava_id+'/workout-analysis').then(r=>r.ok?r.json():null).catch(()=>null);
    }
    const lastActOpen=(latestWa&&latestWa.ok&&latestWa.session&&latestWa.session.clean_session_id)?"window._epochSesionId='"+latestWa.session.clean_session_id+"';go('sesion')":"go('activities')";
    const lastInsight=(latestWa&&latestWa.ok&&latestWa.training_effect_summary)?'<div style="font-size:10px;color:#22d3ee;line-height:1.4;margin-top:3px">'+esc(latestWa.training_effect_summary)+(latestWa.next_action&&latestWa.next_action.label?' · '+esc(latestWa.next_action.label):'')+'</div>':'';
    const lastActCard=la?`<div onclick="${lastActOpen}" style="background:rgba(232,89,60,.07);border:1px solid rgba(232,89,60,.18);border-radius:12px;padding:10px 14px;margin-bottom:10px;cursor:pointer;display:flex;align-items:center;gap:10px">
      <span style="font-size:22px">${{Ride:'🚴',Run:'🏃',VirtualRide:'⚡',Walk:'🚶',Swim:'🏊'}[la.sport_type]||'🏅'}</span>
      <div style="flex:1;min-width:0">
        <div style="font-size:12px;font-weight:800;color:#e8593c;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(la.name||la.sport_type)}</div>
        <div style="font-size:11px;color:var(--muted)">${la.start_date} · ${la.duration_hms||'—'} · ${la.distance_km||'—'} km · FC ${la.avg_hr||'—'} bpm</div>
        ${lastInsight}
      </div>
      <span style="color:#e8593c;font-size:18px">›</span>
    </div>`:'';
    // ── ADR Insight First: responder "How am I today?" antes de cualquier métrica
    const _fat=a.fatiga||'';
    const _laDays=la?Math.round((Date.now()-new Date(la.start_date+'T12:00:00').getTime())/86400000):null;
    let _insight='';
    if(pains>0){
      _insight='Your body is asking for attention: an active niggle. Today, recovering is worth more than training.';
    } else if(_fat==='high'){
      _insight='You woke up with high fatigue. Today, rest IS the training — not a pause.';
    } else if(_laDays===0){
      _insight='You already trained today. What follows — eating and resting — is where your body builds.';
    } else if(_laDays===1){
      _insight='Yesterday you put in work. Your aerobic base keeps growing'+(Number(s.km||0)>0?' — '+Number(s.km).toFixed(0)+' km so far this week':'')+'.';
    } else if(_laDays!=null&&_laDays>=3){
      _insight='It has been '+_laDays+' days without a session. Consistency is the hardest capacity to win back.';
    } else {
      _insight='Everything in balance. Your process is moving'+(Number(s.km||0)>0?' — '+Number(s.km).toFixed(0)+' km this week':'')+'.';
    }
    // Countdown al evento si hay meta activa (clic → coach)
    const _planW=(tc&&tc.plan_week)||null;
    const _planPhase=(tc&&tc.plan&&tc.plan.current_phase)||null;
    const _planBadge=_planW?'<div style="margin-top:8px"><span class="chip ok">Plan · week '+_planW.week_number+'/'+_planW.total_weeks+'</span>'+(_planPhase?'<span class="chip">'+_planPhase+' phase</span>':'')+'</div>':'';
    const _todaySes=((tc&&tc.today)||[])[0];
    const _todayLine=_todaySes&&_todaySes.status==='planned'?
      '<div style="font-size:11px;color:var(--muted);margin-top:6px">Today per the plan: <b style="color:var(--text)">'+esc(_todaySes.description||_todaySes.session_type)+'</b></div>':'';
    const _goalLine=(ac&&ac.goal&&ac.weeks_to_event!=null)?
      '<div onclick="go(\'plan\')" style="font-size:11px;color:#fb923c;font-weight:800;margin-top:8px;cursor:pointer">⏱ '+ac.weeks_to_event+' week'+(ac.weeks_to_event!==1?'s':'')+' to '+esc(ac.goal.event_name)+' →</div>':'';
    setPhaseAccent(tc&&tc.plan&&tc.plan.current_phase);
    // U5 (UI v2) — capas de contexto: la app se adapta a lo que el atleta da.
    // "Valor primero, precision despues" — nunca se exige contexto completo.
    const _hasData=(s.sesiones||0)>0||(lastAct&&lastAct.activities&&lastAct.activities.length>0);
    const _hasGoal=!!(tc&&tc.goal);
    const _hasPlan=!!(tc&&tc.plan&&tc.plan.nombre)||!!(_planW&&_planW.week_number);
    const _hasWellness=checkedToday;
    const _layerChip=function(on,label){return '<span style="font-size:9px;font-weight:900;padding:2px 8px;border-radius:8px;margin-right:4px;'+(on?'background:rgba(61,214,140,.14);color:#3dd68c':'background:rgba(255,255,255,.05);color:var(--muted)')+'">'+(on?'✓ ':'')+label+'</span>'};
    const _nextLayer=!_hasData?'Connect or upload training and Epoch will start explaining what you are building.'
      :!_hasWellness?'The 20-second morning check raises today\u2019s read from pattern to body.'
      :!_hasGoal?'Add a goal whenever you want direction — until then Epoch reads patterns, not compliance.'
      :!_hasPlan?'A plan would let Epoch measure alignment, not just direction.'
      :'';
    const _layersStrip='<div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--line)">'+
      _layerChip(_hasData,'Data')+_layerChip(_hasGoal,'Goal')+_layerChip(_hasPlan,'Plan')+_layerChip(_hasWellness,'Check today')+
      (_nextLayer?'<div style="font-size:10px;color:var(--muted);margin-top:5px">'+_nextLayer+'</div>':'')+
    '</div>';
    const insightHero='<div class="phase-hero" style="border:1px solid var(--line);border-radius:18px;padding:16px 18px;margin-bottom:12px">'+
      '<div style="font-size:10px;font-weight:900;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin-bottom:6px">How am I today?</div>'+
      '<div style="font-size:15px;line-height:1.5;font-weight:650">'+_insight+'</div>'+
      _todayLine+
      _planBadge+
      _goalLine+
      _layersStrip+
    '</div>';
    // V8 UI: esencial visible · evidencia colapsada (ADR Insight-First)
    const _estadoAtletaCard='<div class="card"><div class="head"><h3>Athlete state</h3><span>'+(a.fitness||'—')+'</span></div>'+
        row('<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><circle cx="7" cy="7" r="5" stroke="currentColor" stroke-width="1.5"/><path d="M5 7l1.5 1.5L9 5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>','Fitness',d.recommendation||'No guidance',a.mars_index?Number(a.mars_index).toFixed(1):'—')+
        row('<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M7 2v5l3 2" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><circle cx="7" cy="7" r="5" stroke="currentColor" stroke-width="1.5"/></svg>','Fatigue',a.fatiga||'—','')+
        row('<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M7 3c0 0-4 3-4 6a4 4 0 008 0c0-3-4-6-4-6z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>','Niggles',pains?pains+' active':'No alerts','')+
        '<div class="row"><div class="r-ico">·</div><div class="r-main"><div class="r-title">Weight</div><div class="r-sub">M1: '+pesoObj+' kg · Final: '+((mp.athlete||{}).peso_meta_final_kg||70)+' kg'+(pesoDiff>0?' · '+pesoDiff+' kg to go':' M1 goal reached')+'</div></div><div class="r-val" style="color:#3dd68c">'+peso+' kg</div></div>'+
        '<div class="row"><div class="r-ico">·</div><div class="r-main"><div class="r-title">Efficiency (speed per heartbeat)</div><div class="r-sub">how much speed each beat produces — higher = a more efficient engine</div></div><div style="text-align:right"><div style="font-size:13px;font-weight:800;color:#a78bfa">'+effActual.toFixed(4)+'</div><div style="font-size:10px;color:var(--muted)">'+effPct+'% of target</div></div></div>'+
      '</div>';
    const _evidenciaHome=
      e25cCard+
      stravaCard+
      '<div class="card" style="margin-bottom:8px">'+
        '<div style="display:flex;flex-wrap:wrap;gap:6px">'+
          '<span style="background:rgba(74,158,255,.1);color:#4a9eff;padding:3px 8px;border-radius:12px;font-size:11px;font-weight:800">'+esc(plan.fase||'Base')+'</span>'+
          '<span style="background:rgba(61,214,140,.1);color:#3dd68c;padding:3px 8px;border-radius:12px;font-size:11px;font-weight:800">Z2: '+z2[0]+'–'+z2[1]+' bpm</span>'+
          '<span style="background:rgba(245,158,11,.1);color:#f59e0b;padding:3px 8px;border-radius:12px;font-size:11px;font-weight:800">Cad: '+(mp.cadencia_obj||100)+' rpm</span>'+
          '<span style="background:rgba(232,89,60,.1);color:#e8593c;padding:3px 8px;border-radius:12px;font-size:11px;font-weight:800">'+esc(bici.nombre||'Rarotonga')+' '+(bici.km||716)+' km</span>'+
        '</div>'+
      '</div>'+
      _estadoAtletaCard;
    // V8.0: la adaptación de hoy — el check matutino se vuelve accionable
    let adaptCard='';
    if(ta&&ta.ok&&ta.status&&ta.status!=='sin_lectura'){
      const aCol=ta.status==='mantener'?'#3dd68c':ta.status==='precaucion'?'#f59e0b':'#e8593c';
      const aTit=ta.status==='mantener'?'Green light':ta.status==='precaucion'?'One eye on the body':'Today calls for adapting';
      adaptCard='<div class="card" style="border-left:3px solid '+aCol+'">'+
        '<div class="q-kicker">The plan, read with your body today</div>'+
        '<div class="head" style="margin-bottom:6px"><h3 style="color:'+aCol+'">'+aTit+'</h3>'+(ta.planned?'<span>'+esc(ta.planned.description||'')+'</span>':'')+'</div>'+
        '<div style="font-size:12px;line-height:1.6">'+esc(ta.explanation_text)+'</div>'+
        fbBtns('today-adaptation',new Date().toISOString().slice(0,10))+
      '</div>';
    }
    // V9.4 Today Options: 2-3 opciones con razon — propone, no ordena
    let optionsCard='';
    if(topt&&topt.ok&&(topt.options||[]).length){
      const demCol={light:'#3dd68c',moderate:'#22d3ee',demanding:'#f59e0b'};
      const rows=topt.options.map(function(o){
        const border=o.recommended?'1px solid rgba(61,214,140,.45)':'1px solid rgba(255,255,255,.09)';
        const bg=o.recommended?'rgba(61,214,140,.07)':'rgba(255,255,255,.03)';
        return '<div style="border:'+border+';background:'+bg+';border-radius:12px;padding:10px 12px;margin-bottom:6px">'+
          '<div style="display:flex;justify-content:space-between;align-items:center;gap:8px">'+
            '<div style="font-size:13px;font-weight:800">'+esc(o.title)+(o.recommended?' <span class="chip ok" style="margin-left:4px">suggested</span>':'')+'</div>'+
            '<span style="font-size:10px;color:'+(demCol[o.demand_estimate]||'#8e95a3')+';font-weight:800;white-space:nowrap">'+o.demand_estimate+'</span>'+
          '</div>'+
          '<div style="font-size:11px;color:var(--muted);line-height:1.5;margin-top:3px">'+o.why+'</div>'+
        '</div>';
      }).join('');
      optionsCard='<div class="card">'+
        '<div class="head"><h3>Today, you could…</h3><span>'+(topt.confidence==='high'?'read with your body':'no check yet · low confidence')+'</span></div>'+
        rows+
        '<div style="font-size:10px;color:var(--muted);margin-top:4px">Options, not orders — your body has the casting vote.</div>'+
        (outc&&outc.ok&&outc.outcome&&outc.outcome!=='no_log'?
          '<div style="font-size:11px;line-height:1.5;margin-top:8px;padding:7px 10px;background:rgba(255,255,255,.04);border-radius:8px;border-left:2px solid '+(outc.outcome==='followed'?'#3dd68c':'#22d3ee')+'">'+
          '<b style="color:'+(outc.outcome==='followed'?'#3dd68c':'#22d3ee')+'">Yesterday\u2019s loop:</b> '+esc(outc.explanation_text)+
          (outc.followed_rate_30d!=null?' <span style="color:var(--muted)">('+outc.followed_rate_30d+'% followed · 30d)</span>':'')+'</div>':'')+
        fbBtns('today-options',new Date().toISOString().slice(0,10))+
      '</div>';
    }
    $('home-data').innerHTML=
      insightHero+
      morningBanner+
      adaptCard+
      optionsCard+
      limCard+
      lastActCard+
      '<div class="grid2">'+
        metric('Week km',Number(s.km||0).toFixed(0),'km'+dKm)+
        metric('Hours',Number(s.horas||0).toFixed(1),'h'+dH)+
        metric('Sessions',s.sesiones||0,'week'+dSes)+
        metric('Z2',Number(z.pct_z2_4_semanas||0).toFixed(0)+'%','4 weeks')+
      '</div>'+
      (usingFallback?'<div style="background:rgba(232,89,60,.1);border:1px solid rgba(232,89,60,.3);border-radius:10px;padding:8px 12px;font-size:11px;color:#e8593c;margin-bottom:8px">⚠ Using fallback data — check connection</div>':'')+
      evd('Evidence and detail',_evidenciaHome);
  }catch(e){$('home-data').innerHTML='<div class="card" style="color:var(--muted)">Error: '+esc(e.message)+'</div>';}
}

async function saveMorningCheckHome(){
  const today=new Date().toISOString().slice(0,10);
  const hr=parseInt($('h-hr').value)||null;
  const fatigue=parseInt($('h-fat').value)||null;
  const sleep=parseFloat($('h-slp').value)||null;
  const estado=parseInt($('h-estado').value)||null;
  if(!hr&&!fatigue&&!sleep&&!estado){toast('Fill at least one field');return;}
  const body={date:today,category:'sleep',hr_rest:hr,fatigue:fatigue,sleep_hours:sleep,stress_level:estado};
  const d=await fetch(API+'/wellness',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json());
  if(d.ok){toast('☀️ Morning check saved');loadHome();}else{toast('Save failed');}
}
