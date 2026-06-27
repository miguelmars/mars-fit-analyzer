// V7.2 — My Plan: dónde voy y qué construyo (ADR Insight-First)
async function loadPlan(){
  const el=$('plan-data');
  try{
    const [tp,tc,erg,reb,proj,align,lei,wi]=(await Promise.allSettled([
      fetch(API+'/gpt/training-plan').then(r=>r.json()),
      fetch(API+'/gpt/training-context').then(r=>r.json()),
      fetch(API+'/gpt/event-readiness-gap').then(r=>r.json()),
      fetch(API+'/gpt/week-rebalance').then(r=>r.json()),
      fetch(API+'/gpt/event-projection').then(r=>r.json()),
      fetch(API+'/gpt/training-alignment').then(r=>r.json()),
      fetch(API+'/gpt/life-event-impact').then(r=>r.json()),
      fetch(API+'/gpt/weekly-intelligence').then(r=>r.json())
    ])).map(r=>r.status==='fulfilled'?r.value:{});

    if(!tp.ok||!tp.plan){
      // U3 (UI v2) — Semana libre: sin plan no hay carencia, hay lectura.
      // Jamás se habla de cumplimiento aquí: no hay contra qué cumplir.
      let freeWeek='';
      if(wi&&wi.ok){
        const t=wi.totals||{};
        const caps=wi.capacities_built_pct||{};
        const capLbl={aerobic_fitness:'Aerobic engine',endurance:'Endurance',power:'Power',strength_endurance:'Strength-endurance',recovery:'Recovery',tempo:'Threshold'};
        const capBars=Object.keys(caps).map(function(k){
          return '<div style="display:grid;grid-template-columns:120px 1fr 36px;gap:8px;align-items:center;padding:3px 0">'+
            '<div style="font-size:11px;color:var(--muted)">'+(capLbl[k]||k)+'</div>'+
            '<div style="height:5px;background:rgba(255,255,255,.07);border-radius:3px"><div style="height:100%;width:'+caps[k]+'%;background:#3dd68c;border-radius:3px"></div></div>'+
            '<div style="text-align:right;font-size:11px;font-weight:800">'+caps[k]+'%</div></div>';
        }).join('');
        freeWeek='<div class="phase-hero" style="border:1px solid var(--line);border-radius:18px;padding:16px 18px;margin-bottom:12px">'+
          '<div class="q-kicker">Free week — what am I building?</div>'+
          '<div style="margin:6px 0 8px">'+
            '<span class="chip">'+(t.sessions||0)+' sessions</span>'+
            '<span class="chip">'+(t.km||0)+' km</span>'+
            '<span class="chip">'+(t.hours||0)+' h</span>'+
            (wi.dominant_capacity?'<span class="chip ok">'+(capLbl[wi.dominant_capacity]||wi.dominant_capacity)+'</span>':'')+
          '</div>'+
          '<div style="font-size:12px;line-height:1.6">'+(wi.explanation_text||'')+'</div>'+
        '</div>'+
        (Object.keys(caps).length?'<div class="card"><div class="head"><h3>What this week built</h3><span>share of training time</span></div>'+capBars+
          '<div style="font-size:10px;color:var(--muted);margin-top:6px">No plan, no compliance talk — Epoch reads what you actually built. Add a goal or a plan whenever you want a direction to measure against.</div></div>':'');
      }
      el.innerHTML=freeWeek+'<div class="card"><div style="font-size:13px;line-height:1.6">No active plan — and that is fine. '+
        'If you want one, build it from your own evidence: Epoch reads your real volume and your best historical block, never a generic template.</div>'+
        '<div style="font-size:11px;color:var(--muted);margin-top:8px">Past plans stay saved with their full history.</div></div>'+
        '<div class="card"><div class="head"><h3>Build a plan</h3><span>from your evidence</span></div>'+
        '<div class="field"><label>Event name</label><input id="pb-name" placeholder="Gran Fondo, Time Trial..."></div>'+
        '<div class="grid2">'+
          '<div class="field"><label>Event date</label><input type="date" id="pb-date"></div>'+
          '<div class="field"><label>Days per week</label><select id="pb-days"><option value="3">3</option><option value="4" selected>4</option><option value="5">5</option><option value="6">6</option></select></div>'+
        '</div>'+
        '<button class="btn" onclick="pbPreview(false)">Preview plan</button>'+
        '<div id="pb-preview" style="margin-top:10px"></div></div>';
      return;
    }
    const p=tp.plan;
    const wk=p.current_week||1, total=p.total_weeks||1;
    const pct=Math.min(100,Math.round(wk/total*100));
    const goal=(tc&&tc.goal)||null;

    // Insight primero: ¿dónde voy y qué estoy construyendo?
    const curPhase=(p.phases||[]).find(function(f){return f.is_current})||{};
    setPhaseAccent(p.current_phase);
    const insight='<div class="phase-hero" style="border:1px solid var(--line);border-radius:18px;padding:16px 18px;margin-bottom:12px">'+
      '<div style="font-size:10px;font-weight:900;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin-bottom:6px">Where am I?</div>'+
      '<div style="font-size:14px;line-height:1.55;font-weight:650">Week '+wk+' of '+total+' of the '+esc(p.name)+'. '+
      (curPhase.name?('Phase <b class="phase-text">'+esc(curPhase.name)+'</b>'+(curPhase.focus?' — building '+esc(curPhase.focus):'')+'.'):'')+
      '</div>'+
      (goal?'<div class="phase-text" style="font-size:11px;font-weight:800;margin-top:6px">⏱ '+goal.weeks_to_event+' weeks to '+esc(goal.event_name)+'</div>':'')+
    '</div>';

    // Anillo de progreso del plan
    const ring='<div class="card" style="text-align:center">'+
      '<div class="ring" style="--p:'+pct+'%"><span>'+wk+'</span></div>'+
      '<div style="font-size:12px;color:var(--muted);margin-top:8px">of '+total+' weeks · '+fmtDate(p.start_date)+' → '+fmtDate(p.end_date)+'</div>'+
    '</div>';

    // Fases
    const phaseRows=(p.phases||[]).map(function(f){
      const col=f.is_current?'#fb923c':'var(--muted)';
      const done=f.end<new Date().toISOString().slice(0,10);
      return '<div class="row">'+
        '<div class="r-ico">'+(f.is_current?'▶️':done?'✅':'·')+'</div>'+
        '<div class="r-main"><div class="r-title" style="color:'+col+'">'+esc((f.name||'').toUpperCase())+(f.is_current?' · now':'')+'</div>'+
        '<div class="r-sub">'+fmtDate(f.start)+' → '+fmtDate(f.end)+(f.focus?' · '+esc(f.focus):'')+'</div></div>'+
        (done||f.is_current?'<button onclick="showPhaseReport(this,\''+escJs(f.name)+'\')" style="background:rgba(251,146,60,.12);border:1px solid rgba(251,146,60,.3);color:#fb923c;border-radius:8px;padding:4px 10px;font-size:10px;font-weight:800;cursor:pointer;flex-shrink:0">Report</button>':'')+
      '</div>';
    }).join('');
    const phasesCard='<div class="card"><div class="head"><h3>Phases</h3><span>'+esc(p.event||'')+'</span></div>'+phaseRows+
      '<div id="phase-report-box" style="display:none;font-size:12px;line-height:1.6;margin-top:10px;padding-top:10px;border-top:1px solid var(--line)"></div></div>';

    // Esta semana: sesiones con estado real
    const stIcon={completed:'✅',skipped:'⏭️',planned:'⏳'};
    const stLabel={completed:'completed',skipped:'moved/skipped — no guilt',planned:'pending'};
    // U4 (UI v2) — la semana día por día: estructura Garmin + interpretación Epoch
    const intentCol=function(st){st=st||'';return /interval|tempo|threshold|vo2/.test(st)?'#f59e0b':/recovery/.test(st)?'#4a9eff':/long/.test(st)?'#a78bfa':'#3dd68c'};
    const _mon=(function(){const d=new Date();const dow=(d.getDay()+6)%7;d.setDate(d.getDate()-dow);d.setHours(12,0,0,0);return d})();
    const dayNames=['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
    const byDate={};(tp.this_week||[]).forEach(function(s2){if(s2.planned_date){(byDate[s2.planned_date]=byDate[s2.planned_date]||[]).push(s2)}});
    const todayISO=new Date().toISOString().slice(0,10);
    let dayCards='';
    for(let i=0;i<7;i++){
      const d=new Date(_mon);d.setDate(_mon.getDate()+i);
      const iso=d.toISOString().slice(0,10);
      const isToday=iso===todayISO;
      const ss=byDate[iso]||[];
      const inner=ss.length?ss.map(function(s2){
        const col=intentCol(s2.session_type);
        const past=iso<todayISO;
        const st=s2.status==='completed'?'<b style="color:#3dd68c">✓ done</b>'
          :s2.status==='skipped'?'<span style="color:#f59e0b">not done — the week does not break; it reduces one stimulus</span>'
          :s2.moved_from?'<span style="color:#4a9eff">↔ moved</span>'
          :past?'<span style="color:#f59e0b">pending from earlier this week</span>'
          :'<span style="color:var(--muted)">planned</span>';
        return '<div style="border-left:3px solid '+col+';padding-left:8px;margin:4px 0">'+
          '<div style="font-size:12px;font-weight:700">'+esc(s2.description||s2.session_type)+'</div>'+
          '<div style="font-size:10px;margin-top:2px">'+st+(s2.matched_clean_session_id?' · <span onclick="openSesion(\''+s2.matched_clean_session_id+'\')" style="color:#3dd68c;cursor:pointer">view ›</span>':'')+'</div>'+
        '</div>';
      }).join(''):'<div style="font-size:10px;color:var(--muted);padding:2px 0">rest · absorbing load</div>';
      dayCards+='<div style="display:grid;grid-template-columns:40px 1fr;gap:10px;padding:6px 8px;border-radius:10px'+(isToday?';background:rgba(255,255,255,.05);outline:1px solid rgba(255,255,255,.12)':'')+'">'+
        '<div style="font-size:10px;font-weight:'+(isToday?'900':'600')+';color:'+(isToday?'var(--text)':'var(--muted)')+';padding-top:4px">'+dayNames[i]+(isToday?' ←':'')+'</div>'+
        '<div>'+inner+'</div></div>';
    }
    // Resumen semanal Epoch — no los totales Garmin: qué construyó, qué tan confiable
    const twAll=(tp.this_week||[]);
    const twDone=twAll.filter(function(x){return x.status==='completed'}).length;
    const wiT=(wi&&wi.totals)||{};
    const capLbl2={aerobic_fitness:'Aerobic engine',endurance:'Endurance',power:'Power',strength_endurance:'Strength-endurance',recovery:'Recovery',tempo:'Threshold'};
    const domCap=wi&&wi.dominant_capacity?(capLbl2[wi.dominant_capacity]||wi.dominant_capacity):null;
    const confNote=(wi&&wi.wellness_checks===0)?'medium — morning checks missing':'good — body signals present';
    const missedIntense=twAll.some(function(x){return x.status==='skipped'&&/interval|tempo|threshold/.test(x.session_type||'')});
    const weekPhrase=missedIntense?'The week stays aligned with the phase, but an intensity stimulus was missed — it can be retaken if recovery allows.'
      :(twDone===twAll.length&&twAll.length)?'The week landed complete — everything the phase asked for.'
      :'The week is in progress — what lands matters more than what was listed.';
    const weekCard='<div class="card"><div class="head"><h3>This week, day by day</h3><span>week '+wk+' · '+twDone+' of '+twAll.length+' landed</span></div>'+
      dayCards+
      '<div style="border-top:1px solid var(--line);margin-top:8px;padding-top:8px">'+
        '<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:6px">'+
          '<span class="chip">'+(wiT.sessions||0)+' done · '+(wiT.km||0)+' km · '+(wiT.hours||0)+' h</span>'+
          (domCap?'<span class="chip ok">built: '+domCap+'</span>':'')+
          '<span class="chip">confidence '+confNote+'</span>'+
        '</div>'+
        '<div style="font-size:12px;line-height:1.5">'+weekPhrase+'</div>'+
      '</div></div>';

    // Cumplimiento acumulado (solo lo registrado, honesto)
    const c=tp.compliance||{};
    const compCard=c.registered?('<div class="grid2">'+
      metric('Registered',c.registered,'plan sessions')+
      metric('Completed',c.completed,'linked to real rides')+
    '</div>'+
    '<div style="font-size:10px;color:var(--muted);margin:-6px 2px 10px">'+(tp.nota||'')+'</div>'):'';

    // V7.3: el evento exige X, hoy tienes Y, la fase Z ataca el gap
    let gapCard='';
    if(erg&&erg.ok&&erg.goal&&erg.readiness_score!=null){
      const comps=(erg.components||[]);
      const gapKey=(erg.gap||{}).nombre||'';
      const bars=comps.map(function(c){
        const isGap=c.nombre===gapKey;
        const col=isGap?'#f59e0b':(c.score>=75?'#3dd68c':'#4a9eff');
        return '<div style="display:grid;grid-template-columns:130px 1fr 34px;gap:8px;align-items:center;padding:3px 0">'+
          '<div style="font-size:11px;color:'+(isGap?'#f59e0b':'var(--muted)')+'">'+esc(c.nombre)+(isGap?' ← gap':'')+'</div>'+
          '<div style="height:5px;background:rgba(255,255,255,.07);border-radius:3px;overflow:hidden"><div style="height:100%;width:'+Math.max(0,Math.min(100,c.score))+'%;background:'+col+'"></div></div>'+
          '<div style="text-align:right;font-size:11px;font-weight:800;color:'+col+'">'+Math.round(c.score)+'</div></div>';
      }).join('');
      gapCard='<div class="card" style="border-left:3px solid #f59e0b">'+
        '<div class="head"><h3>What am I missing for the event?</h3><span>'+Math.round(erg.readiness_score)+'/100</span></div>'+
        '<div style="font-size:12px;line-height:1.6;margin-bottom:8px">'+esc(erg.explanation_text)+'</div>'+bars+
        ((erg.data_gaps||[]).length?'<div style="font-size:10px;color:var(--muted);margin-top:6px">Missing data: '+erg.data_gaps.join(', ')+'</div>':'')+
      '</div>';
    }
    // V8.1: la semana que se reacomoda — sin culpa, con un tap
    let rebCard='';
    const movibles=((reb&&reb.proposals)||[]).filter(function(p){return p.proposed_date});
    if(reb&&reb.ok&&(reb.proposals||[]).length){
      const rows2=(reb.proposals||[]).map(function(p){
        return '<div class="row"><div class="r-ico">🔁</div>'+
          '<div class="r-main"><div class="r-title">'+esc(p.description||p.session_type)+'</div>'+
          '<div class="r-sub">era '+fmtShort(p.original_date)+(p.proposed_date?' → proposed '+fmtShort(p.proposed_date):'')+' · '+p.reason+'</div></div>'+
          (p.proposed_date?'<button onclick="acceptMove(this,'+p.plan_session_id+',\''+p.proposed_date+'\')" style="background:rgba(61,214,140,.15);border:1px solid rgba(61,214,140,.35);color:#3dd68c;border-radius:8px;padding:5px 12px;font-size:11px;font-weight:800;cursor:pointer;flex-shrink:0">Accept</button>':'')+
        '</div>';
      }).join('');
      rebCard='<div class="card" style="border-left:3px solid #3dd68c">'+
        '<div class="q-kicker">The week rearranges itself</div>'+
        '<div style="font-size:12px;line-height:1.6;margin-bottom:6px">'+esc(reb.explanation_text)+'</div>'+rows2+
        fbBtns('week-rebalance')+
      '</div>';
    }
    // V8.2: proyección al evento
    let projCard='';
    if(proj&&proj.ok&&proj.available){
      projCard='<div class="card" style="border-left:3px solid #a78bfa">'+
        '<div class="q-kicker">How do I arrive if I keep this up?</div>'+
        '<div style="display:flex;align-items:baseline;gap:8px;margin-bottom:4px">'+
          '<span style="font-size:24px;font-weight:950;color:#a78bfa">~'+Math.round(proj.projected_readiness)+'</span>'+
          '<span style="font-size:11px;color:var(--muted)">readiness proyectado (hoy '+Math.round(proj.readiness_now)+', banda ±'+Math.round(proj.band)+')</span>'+
        '</div>'+
        '<div style="font-size:12px;line-height:1.6">'+esc(proj.explanation_text)+'</div>'+
        fbBtns('event-projection')+
      '</div>';
    }
    // V9.5 Training Alignment: cumplimiento visible, sin culpa
    let alignCard='';
    if(align&&align.ok&&align.alignment_pct!=null){
      const t=align.totals||{};
      const aPct=align.alignment_pct;
      const aCol=aPct>=75?'#3dd68c':aPct>=50?'#f59e0b':'#8e95a3';
      const wkBars=(align.weeks||[]).slice(-6).map(function(w){
        const p2=w.planned?Math.round(w.completed/w.planned*100):0;
        return '<div style="text-align:center;flex:1"><div style="height:34px;display:flex;align-items:flex-end"><div style="width:100%;height:'+Math.max(8,Math.round(p2*0.34))+'px;background:'+(p2>=75?'#3dd68c':p2>=50?'#f59e0b':'rgba(255,255,255,.15)')+';border-radius:4px 4px 0 0"></div></div><div style="font-size:9px;color:var(--muted);margin-top:2px">w'+w.week+'</div></div>';
      }).join('');
      alignCard='<div class="card">'+
        '<div class="head"><h3>Plan vs reality</h3><span style="color:'+aCol+';font-weight:900">'+aPct+'% landed</span></div>'+
        '<div style="display:flex;gap:6px;align-items:flex-end;margin-bottom:8px">'+wkBars+'</div>'+
        '<div style="font-size:11px;color:var(--muted);line-height:1.5">'+t.completed+' of '+t.planned+' sessions landed · '+t.moved+' moved · '+t.skipped+' skipped. Moving a session is not failing — it is the plan breathing with your life. The phase is judged by what it built, never by a perfect checklist.</div>'+
      '</div>';
    }
    // V10.2 — Calendario completo del plan: las semanas como mapa, no como lista
    let calCard='';
    {
      const allSess=tp.sessions||[];
      const phaseCol={base:'#3dd68c',build:'#f59e0b',peak:'#a78bfa',taper:'#4a9eff'};
      const stIcon={completed:['✓','#3dd68c'],planned:['·','#8e95a3'],skipped:['—','#f59e0b'],moved:['→','#4a9eff']};
      function weekPhase(n){
        if(!p.start_date)return null;
        const ws=new Date(p.start_date+'T12:00:00');ws.setDate(ws.getDate()+(n-1)*7);
        const d=ws.toISOString().slice(0,10);
        const ph=(p.phases||[]).find(function(f){return f.start<=d&&d<=f.end});
        return ph?ph.name:null;
      }
      let rows='';
      for(let n=1;n<=total;n++){
        const ph=weekPhase(n);
        const col=phaseCol[ph]||'#8e95a3';
        const wkSess=allSess.filter(function(x){return x.week_number===n});
        const chips=wkSess.map(function(x){
          const ic=stIcon[x.status]||stIcon.planned;
          return '<span title="'+esc(x.description||x.session_type||'')+'" style="display:inline-flex;align-items:center;gap:3px;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);border-radius:7px;padding:2px 7px;font-size:10px;margin:1px"><b style="color:'+ic[1]+'">'+ic[0]+'</b>'+(x.session_type||'')+'</span>';
        }).join('');
        const isCur=n===wk;
        rows+='<div style="display:grid;grid-template-columns:44px 10px 1fr;gap:8px;align-items:center;padding:4px 6px;border-radius:8px'+(isCur?';background:rgba(255,255,255,.06);outline:1px solid rgba(255,255,255,.12)':'')+'">'+
          '<div style="font-size:10px;font-weight:'+(isCur?'900':'600')+';color:'+(isCur?'var(--text)':'var(--muted)')+'">W'+n+(isCur?' ←':'')+'</div>'+
          '<div style="width:8px;height:8px;border-radius:50%;background:'+col+'"></div>'+
          '<div>'+(chips||'<span style="font-size:9px;color:var(--muted)">'+(n<wk?'no sessions registered':'ahead')+'</span>')+'</div>'+
        '</div>';
      }
      const legend='<div style="display:flex;gap:10px;flex-wrap:wrap;font-size:9px;color:var(--muted);margin-bottom:8px">'+
        Object.keys(phaseCol).map(function(k){return '<span><span style="color:'+phaseCol[k]+'">●</span> '+k+'</span>'}).join('')+
        '<span style="margin-left:auto"><b style="color:#3dd68c">✓</b> done · <b style="color:#4a9eff">→</b> moved · <b style="color:#f59e0b">—</b> skipped</span></div>';
      calCard=evd('Full plan calendar · '+total+' weeks',legend+rows);
    }
    // V10.6 — Life happens: marcar viaje/enfermedad y reacomodar ANTES del skipped
    let lifeCard='';
    {
      const impacts=(lei&&lei.impacts)||[];
      const impactRows=impacts.map(function(im){
        return '<div style="border:1px solid rgba(255,255,255,.09);background:rgba(255,255,255,.03);border-radius:10px;padding:8px 10px;margin-bottom:6px;display:flex;justify-content:space-between;align-items:center;gap:8px">'+
          '<div><div style="font-size:12px;font-weight:700">'+esc(im.description||im.session_type)+'</div>'+
          '<div style="font-size:10px;color:var(--muted);margin-top:2px">'+im.planned_date+' falls inside '+im.life_event.kind+' · '+im.proposal+'</div></div>'+
          (im.proposed_date?'<button onclick="acceptMove(this,'+im.plan_session_id+',\''+im.proposed_date+'\')" style="background:rgba(61,214,140,.15);border:1px solid rgba(61,214,140,.35);color:#3dd68c;border-radius:8px;padding:5px 12px;font-size:11px;font-weight:800;cursor:pointer;flex-shrink:0">Accept</button>':'')+
        '</div>';
      }).join('');
      const form='<div class="grid2" style="margin-top:8px">'+
          '<div class="field"><label>What</label><select id="le-kind"><option value="travel">Travel</option><option value="illness">Illness</option><option value="work">Work</option><option value="family">Family</option><option value="other">Other</option></select></div>'+
          '<div class="field"><label>&nbsp;</label><button class="btn" style="margin:0" onclick="addLifeEvent()">Mark it</button></div>'+
        '</div>'+
        '<div class="grid2">'+
          '<div class="field"><label>From</label><input type="date" id="le-start"></div>'+
          '<div class="field"><label>To</label><input type="date" id="le-end"></div>'+
        '</div>'+
        '<div style="font-size:10px;color:var(--muted)">Training fits around life, not the other way around. Epoch proposes moves BEFORE sessions become skipped.</div>';
      lifeCard=evd('Life happens · '+(impacts.length?impacts.length+' session(s) to rearrange':'mark travel, illness, work'),
        (impacts.length?impactRows:'')+form, impacts.length>0);
    }
    el.innerHTML=insight+rebCard+lifeCard+ring+gapCard+projCard+phasesCard+weekCard+alignCard+calCard+compCard;
  }catch(e){el.innerHTML='<div class="card" style="color:var(--muted)">'+esc(e.message)+'</div>';}
}

window.showPhaseReport=async function(btn,phase){
  const box=document.getElementById('phase-report-box');
  if(!box)return;
  if(box.style.display==='block'&&box.dataset.phase===phase){box.style.display='none';return;}
  box.style.display='block';box.dataset.phase=phase;box.textContent='Reading the phase…';
  try{
    const d=await fetch(API+'/gpt/phase-review?phase='+encodeURIComponent(phase)).then(r=>r.json());
    if(!d.ok){box.textContent='No data for this phase';return;}
    const t=d.totals||{};
    box.innerHTML='<b style="color:#fb923c">Reporte · '+phase.toUpperCase()+(d.in_progress?' (in progress)':'')+'</b><br>'+esc(d.explanation_text)+
      '<div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:6px;font-size:10px;color:var(--muted)">'+
      '<span>'+t.sessions+' sessions</span><span>'+t.km+' km</span><span>'+t.hours+' h</span><span>'+t.ascent_m+' m↑</span>'+
      (d.efficiency_delta_pct!=null?'<span style="color:'+(d.efficiency_delta_pct>0?'#3dd68c':'#f59e0b')+'">eficiencia '+(d.efficiency_delta_pct>0?'+':'')+d.efficiency_delta_pct+'%</span>':'')+
      '</div>';
  }catch(e){box.textContent='Could not load the report';}
};


window.acceptMove=async function(btn,psId,newDate){
  btn.disabled=true;btn.textContent='...';
  try{
    const d=await fetch(API+'/api/plan-session/'+psId+'/move?new_date='+newDate+'&reason='+encodeURIComponent('rearrangement suggested by Epoch'),{method:'POST'}).then(r=>r.json());
    toast(d.ok?'Moved — this is the week now':'Error');
    loadPlan();
  }catch(e){toast('Network error');btn.disabled=false;}
};

// V10.5 — Plan Builder: preview primero, confirmar después
window.pbPreview=async function(confirm){
  const name=encodeURIComponent((document.getElementById('pb-name').value||'My event').trim());
  const d=document.getElementById('pb-date').value;
  const days=document.getElementById('pb-days').value;
  const box=document.getElementById('pb-preview');
  if(!d){toast('Pick the event date');return;}
  box.innerHTML='<div class="loading"><span class="spin"></span>Reading your history...</div>';
  try{
    const r=await fetch(API+'/api/plan-builder?event_name='+name+'&event_date='+d+'&days_per_week='+days+'&dry_run='+(confirm?'false':'true'),{method:'POST'});
    const p=await r.json();
    if(!p.ok&&p.conflict){box.innerHTML='<div style="font-size:12px;color:#f59e0b;line-height:1.5">'+p.message+'</div>';return;}
    if(!p.ok){box.innerHTML='<div style="font-size:12px;color:#e8593c">'+(p.detail||p.message||'Could not build')+'</div>';return;}
    if(confirm){toast('Plan created');loadPlan();return;}
    const df=p.derived_from||{};
    const curve=(p.volume_curve||[]);
    const mx=Math.max.apply(null,curve.map(function(v){return v.km_target}).concat([1]));
    const phCol={base:'#3dd68c',build:'#f59e0b',peak:'#a78bfa',taper:'#4a9eff'};
    const bars='<div style="display:flex;align-items:flex-end;gap:1px;height:44px;margin:8px 0">'+curve.map(function(v){
      return '<div title="W'+v.week+' '+v.km_target+'km" style="flex:1;height:'+Math.max(8,Math.round(v.km_target/mx*100))+'%;background:'+(phCol[v.phase]||'#8e95a3')+';border-radius:2px 2px 0 0;opacity:.85"></div>';
    }).join('')+'</div>';
    box.innerHTML='<div style="border:1px solid rgba(255,255,255,.12);border-radius:12px;padding:12px">'+
      '<div style="font-size:13px;font-weight:800;margin-bottom:4px">'+esc(p.plan.name)+' · '+p.plan.total_weeks+' weeks · '+p.n_sessions+' sessions</div>'+
      bars+
      '<div style="font-size:11px;color:var(--muted);line-height:1.55;margin-bottom:8px">'+esc(p.explanation_text)+'</div>'+
      '<div style="font-size:10px;color:var(--muted);margin-bottom:10px">Starts at ~'+df.start_volume_kmwk+' km/wk → peaks ~'+df.peak_volume_kmwk+' km/wk (your ceiling: '+df.historical_ceiling_kmwk+')</div>'+
      '<button class="btn" onclick="pbPreview(true)">Confirm — create this plan</button>'+
    '</div>';
  }catch(e){box.innerHTML='<div style="font-size:12px;color:#e8593c">'+esc(e.message)+'</div>';}
}

// V10.6 — marcar evento de vida
window.addLifeEvent=async function(){
  const kind=document.getElementById('le-kind').value;
  const sd=document.getElementById('le-start').value;
  const ed=document.getElementById('le-end').value;
  if(!sd||!ed){toast('Pick the dates');return;}
  try{
    const r=await fetch(API+'/api/life-event?kind='+kind+'&start_date='+sd+'&end_date='+ed,{method:'POST'});
    const d=await r.json();
    if(d.ok){toast(d.affected_sessions?d.affected_sessions+' session(s) to rearrange':'Marked');loadPlan();}
    else toast('Could not save');
  }catch(e){toast('Could not save');}
}
