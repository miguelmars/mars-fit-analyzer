// V7.3 (Guía UI §5.4) — Sesión detalle: bloques, intención, progresión, lectura honesta
async function loadSesion(){
  const el=$('sesion-data');
  const id=window._epochSesionId;
  if(!id){
    el.innerHTML='<div class="card"><div style="font-size:13px;line-height:1.6">Open a session from <b>Coach</b>, <b>My Plan</b> or <b>Activities</b> to see its full read: blocks, intent, what it built and how it compares to its repetitions.</div></div>';
    return;
  }
  try{
    const [wa,laps,prog,ssum,dem,svy]=(await Promise.allSettled([
      fetch(API+'/gpt/session/'+id+'/workout-analysis').then(r=>r.json()),
      fetch(API+'/gpt/session/'+id+'/laps').then(r=>r.json()),
      fetch(API+'/gpt/session/'+id+'/progression').then(r=>r.json()),
      fetch(API+'/gpt/session/'+id+'/streams-summary').then(r=>r.json()),
      fetch(API+'/gpt/session/'+id+'/demand').then(r=>r.json()),
      fetch(API+'/api/session/'+id+'/survey').then(r=>r.json())
    ])).map(r=>r.status==='fulfilled'?r.value:{});

    if(!wa.ok){el.innerHTML='<div class="card" style="color:var(--muted)">Could not read this session.</div>';return;}
    const ses=wa.session||{};
    const sum=wa.summary||{};
    const wtLabel={intervals:'Intervals',tempo:'Sustained tempo',high_intensity:'High intensity',climb:'Long climbs',endurance:'Endurance / Z2',recovery:'Recovery'}[wa.workout_type]||wa.workout_type;

    // ── Insight primero: ¿cómo estuvo esta sesión?
    const confPct=Math.round((wa.confidence_score||0)*100);
    const confCls=confPct>=75?'alta':confPct>=50?'media':'baja';
    const hero='<div style="background:linear-gradient(135deg,rgba(34,211,238,.12),rgba(34,211,238,.03));border:1px solid rgba(34,211,238,.3);border-radius:18px;padding:16px 18px;margin-bottom:12px">'+
      '<div class="q-kicker">How was this session?</div>'+
      '<div style="font-size:15px;font-weight:650;line-height:1.5">'+esc(ses.name||wtLabel)+' · '+fmtDate(ses.date)+'</div>'+
      '<div style="font-size:13px;line-height:1.55;margin-top:6px">This was a <b style="color:#22d3ee">'+wtLabel.toLowerCase()+'</b> session'+(wa.structured?' with clear block structure':'')+'. It built: <b>'+(wa.capacity_built||'').toLowerCase()+'</b>.</div>'+
      '<div style="margin-top:8px">'+
        (function(){const m={Ride:'road/street',VirtualRide:'trainer · clean data',MountainBikeRide:'MTB',GravelRide:'gravel',EBikeRide:'e-bike'}[ses.sport_type];return m?'<span class="chip ok">'+m+'</span>':''})()+
        '<span class="chip">'+(ses.distance_km!=null?ses.distance_km+' km':'—')+'</span>'+
        '<span class="chip">'+hms(ses.duration_s)+'</span>'+
        (ses.avg_hr_bpm?'<span class="chip">FC '+Math.round(ses.avg_hr_bpm)+'</span>':'<span class="chip warn">no HR</span>')+
        (sum.total_blocks>1?'<span class="chip ok">'+sum.total_blocks+' blocks</span>':'<span class="chip warn">no laps</span>')+
        ((ssum&&ssum.pauses_count>=4)?'<span class="chip warn">urban · '+ssum.pauses_count+' stops</span>':'')+
        ((ssum&&ssum.temp_avg_c!=null&&ssum.temp_avg_c>=28)?'<span class="chip warn">heat '+ssum.temp_avg_c+'°C</span>':'')+
        ((ssum&&ssum.wind_kmh!=null&&ssum.wind_kmh>=25)?'<span class="chip warn">wind '+ssum.wind_kmh+' km/h</span>':'')+
        ((ssum&&ssum.reading_quality)?'<span class="chip" style="color:'+(ssum.reading_quality==='high'?'#3dd68c':ssum.reading_quality==='medium'?'#f59e0b':'#e8593c')+';border-color:rgba(255,255,255,.18)">reading '+ssum.reading_quality+'</span>':'')+
        '<span class="conf '+confCls+'">confidence '+confCls+'</span>'+
      '</div>'+
      (wa.confidence_reason?'<div style="font-size:10px;color:var(--muted);margin-top:5px">'+wa.confidence_reason+'</div>':'')+
    '</div>';

    // ── Bloques visuales (barras por lap: work/recovery/steady)
    let bloquesCard='';
    const blocks=wa.blocks||[];
    if(blocks.length>1){
      const maxDur=Math.max(...blocks.map(function(b){return b.duration_s||0}),1);
      const cols={work:'#e8593c',recovery:'#3dd68c',steady:'#4a9eff',warmup:'#8e95a3',cooldown:'#8e95a3'};
      const bars=blocks.map(function(b){
        const w=Math.max(6,Math.round((b.duration_s||0)/maxDur*100));
        const col=cols[b.block_role]||cols[b.lap_type]||'#8e95a3';
        return '<div style="display:grid;grid-template-columns:26px 1fr 86px;gap:8px;align-items:center;padding:3px 0">'+
          '<div style="font-size:10px;color:var(--muted)">#'+(b.lap_index+1)+'</div>'+
          '<div style="height:14px;border-radius:7px;background:rgba(255,255,255,.05)"><div style="height:100%;width:'+w+'%;background:'+col+';border-radius:7px"></div></div>'+
          '<div style="text-align:right;font-size:10px;color:var(--muted)">'+hms(b.duration_s)+(b.avg_hr_bpm?' · '+Math.round(b.avg_hr_bpm)+' bpm':'')+'</div>'+
        '</div>';
      }).join('');
      bloquesCard='<div class="card"><div class="head"><h3>The blocks</h3><span>'+(sum.work_intervals||0)+' work · '+(sum.recoveries||0)+' recovery</span></div>'+
        '<div style="display:flex;gap:10px;font-size:9px;color:var(--muted);margin-bottom:8px"><span><span style="color:#e8593c">■</span> work</span><span><span style="color:#3dd68c">■</span> recovery</span><span><span style="color:#4a9eff">■</span> steady</span></div>'+
        bars+
        (wa.interval_quality?'<div style="font-size:11px;margin-top:8px">Interval quality: <b style="color:'+(wa.interval_quality.score>=75?'#3dd68c':wa.interval_quality.score>=50?'#f59e0b':'#e8593c')+'">'+wa.interval_quality.label+' ('+wa.interval_quality.score+'/100)</b></div>':'')+
      '</div>';
    }

    // ── Veredicto: qué salió bien / qué se degradó / qué falta
    const v=wa.verdict||{};
    function vList(arr,col,tit){
      if(!(arr||[]).length)return '';
      return '<div style="margin-bottom:8px"><b style="font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:'+col+'">'+tit+'</b>'+
        arr.map(function(x){return '<div style="font-size:12px;line-height:1.5;padding:2px 0">· '+x+'</div>'}).join('')+'</div>';
    }
    const veredictoCard=((v.went_well||[]).length||(v.degraded||[]).length||(v.missing_context||[]).length)?
      '<div class="card"><div class="head"><h3>Honest read</h3></div>'+
        vList(v.went_well,'#3dd68c','What went well')+
        vList(v.degraded,'#f59e0b','What degraded')+
        vList(v.missing_context,'var(--muted)','Missing for a better read')+
      '</div>':'';

    // ── Progresión: ¿mejoré en este workout?
    let progCard='';
    if(prog&&prog.ok&&prog.comparison&&(prog.repetitions||[]).length){
      const pv=prog.comparison.verdict;
      const pCol=pv==='mejorando'?'#3dd68c':pv==='por_debajo'?'#f59e0b':'#8e95a3';
      const lvl=prog.comparison.level||'route_lab';
      const lvlChip=lvl==='route_lab'?'<span class="chip ok">route lab</span>'
        :lvl==='effort_class'?'<span class="chip" style="color:#f59e0b;border-color:rgba(245,158,11,.35)">similar efforts · medium confidence</span>'
        :'<span class="chip" style="color:#8e95a3">intent baseline · low confidence</span>';
      progCard='<div class="card" style="border-left:3px solid '+pCol+'">'+
        '<div class="head"><h3>Did I improve at this workout?</h3><span>'+prog.repetitions.length+' comparable</span></div>'+
        '<div style="margin-bottom:6px">'+lvlChip+'</div>'+
        '<div style="font-size:12px;line-height:1.6">'+prog.explanation_text+'</div>'+
      '</div>';
    }

    // ── V9.2 Session Demand: qué tan demandante fue PARA TI
    let demandCard='';
    if(dem&&dem.ok&&dem.demand_score!=null){
      const ds=dem.demand_score;
      const dCol=ds>=85?'#e8593c':ds>=65?'#f59e0b':ds>=45?'#22d3ee':'#3dd68c';
      const comps=dem.components||{};
      const compBars=Object.keys(comps).map(function(k){
        const v=Math.round(comps[k]*100);
        return '<div style="display:grid;grid-template-columns:80px 1fr 34px;gap:8px;align-items:center;padding:3px 0">'+
          '<div style="font-size:11px;color:var(--muted)">'+k+'</div>'+
          '<div style="height:4px;background:rgba(255,255,255,.07);border-radius:2px"><div style="height:100%;width:'+v+'%;background:'+dCol+';border-radius:2px"></div></div>'+
          '<div style="text-align:right;font-size:11px;font-weight:800">'+v+'</div></div>';
      }).join('');
      demandCard='<div class="card" style="border-left:3px solid '+dCol+'">'+
        '<div class="head"><h3>Session demand</h3><span style="color:'+dCol+';font-weight:900">'+ds+'/100 · '+dem.demand_label+'</span></div>'+
        '<div style="font-size:12px;line-height:1.55;margin-bottom:8px">Relative to <b>your</b> last 60 days — not an external table. Mainly taxed: <b>'+(dem.capacity_taxed||'')+'</b>.</div>'+
        compBars+
        ((dem.modifiers||[]).length?'<div style="font-size:11px;color:var(--muted);margin-top:6px">'+dem.modifiers.join(' · ')+'</div>':'')+
        '<div style="font-size:10px;color:var(--muted);margin-top:6px">confidence '+dem.confidence+' — '+(dem.confidence_reason||'')+'</div>'+
      '</div>';
    }

    // ── V9.3 Survey: ¿cómo se sintió? (un tap; calibra demanda vs percepción)
    let surveyCard='';
    {
      const feels=[['fresh','😊 Fresh'],['normal','🙂 Normal'],['tough','😮‍💨 Tough'],['very_tough','🥵 Very tough'],['emptied','🫠 Emptied']];
      if(svy&&svy.exists){
        const lbl=(feels.find(function(f){return f[0]===svy.feel})||['',svy.feel])[1];
        surveyCard='<div class="card"><div class="head"><h3>How it felt</h3><span>'+lbl+' · RPE '+svy.rpe+'/10</span></div>'+
          '<div style="font-size:11px;color:var(--muted)">Answered — tap another option to change it.</div>'+
          '<div id="svy-btns" style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px">'+
          feels.map(function(f){return '<button onclick="sendSurvey(\''+id+'\',\''+f[0]+'\')" style="background:'+(f[0]===svy.feel?'rgba(61,214,140,.18)':'rgba(255,255,255,.06)')+';border:1px solid '+(f[0]===svy.feel?'rgba(61,214,140,.4)':'rgba(255,255,255,.12)')+';border-radius:10px;padding:7px 10px;color:var(--text);font-size:11px;font-weight:800;cursor:pointer">'+f[1]+'</button>'}).join('')+
          '</div><div id="svy-cal" style="font-size:11px;line-height:1.5;color:var(--muted);margin-top:8px"></div></div>';
      } else {
        surveyCard='<div class="card"><div class="head"><h3>How did it feel?</h3><span>one tap</span></div>'+
          '<div style="font-size:11px;color:var(--muted);margin-bottom:8px">How it felt counts as much as what was measured — this calibrates your reads.</div>'+
          '<div id="svy-btns" style="display:flex;gap:6px;flex-wrap:wrap">'+
          feels.map(function(f){return '<button onclick="sendSurvey(\''+id+'\',\''+f[0]+'\')" style="background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.12);border-radius:10px;padding:7px 10px;color:var(--text);font-size:11px;font-weight:800;cursor:pointer">'+f[1]+'</button>'}).join('')+
          '</div><div id="svy-cal" style="font-size:11px;line-height:1.5;color:var(--muted);margin-top:8px"></div></div>';
      }
    }

    // ── Streams: deriva, calor, pausas (si existen)
    let streamsCard='';
    if(ssum&&ssum.available){
      const bits=[];
      if(ssum.reading_quality_note)bits.push('<b>Reading quality: '+(ssum.reading_quality||'')+'.</b> '+ssum.reading_quality_note);
      if(ssum.decoupling_reading)bits.push(ssum.decoupling_reading);
      if(ssum.temp_avg_c!=null)bits.push('Temperature: '+ssum.temp_avg_c+'°C average'+(ssum.temp_max_c?' (max '+ssum.temp_max_c+'°C)':'')+'.');
      if(ssum.wind_reading)bits.push(ssum.wind_reading);
      else if(ssum.wind_kmh!=null&&ssum.wind_kmh>=10)bits.push('Wind: '+ssum.wind_kmh+' km/h'+(ssum.wind_gust_kmh?' (gusts '+ssum.wind_gust_kmh+')':'')+'.');
      if(ssum.weather_label)bits.push('Conditions: '+ssum.weather_label+(ssum.country?' · '+({MX:'Mexico',IE:'Ireland',GB:'United Kingdom',US:'USA',ES:'Spain'}[ssum.country]||ssum.country):'')+'.');
      if(ssum.pauses_count)bits.push(ssum.pauses_count+' real stops ('+Math.round((ssum.paused_time_s||0)/60)+' min) — traffic, bumps or lights. Epoch measures your moving speed: the street does not punish you.');
      if(bits.length)streamsCard='<div class="card" style="border-left:3px solid #22d3ee"><div class="head"><h3>What your body said</h3><span>from the full stream</span></div>'+
        '<div style="font-size:12px;line-height:1.6">'+bits.join('<br>')+'</div>'+
        (ssum.temp_avg_c!=null?'<div style="font-size:10px;color:var(--muted);margin-top:8px;line-height:1.5">ℹ️ Temperature comes from the Garmin sensor during the ride. Epoch uses it as CONTEXT: in heat your HR runs higher and your body spends energy cooling down; in cool air HR is harder to raise and you ride faster. <b>It never stains your table — it explains your day.</b></div>':'')+
        '</div>';
    }

    // ── Detalle completo colapsado
    const detalle='<div style="font-size:12px;line-height:1.65">'+(wa.explanation_text||'')+'</div>'+
      (wa.degradation?'<div style="font-size:11px;color:var(--muted);margin-top:8px">Primer bloque: '+wa.degradation.first_work.speed_kmh+' km/h @ '+Math.round(wa.degradation.first_work.hr||0)+' bpm → Último: '+wa.degradation.last_work.speed_kmh+' km/h @ '+Math.round(wa.degradation.last_work.hr||0)+' bpm</div>':'');

    el.innerHTML=hero+bloquesCard+veredictoCard+demandCard+surveyCard+progCard+streamsCard+
      evd('Full detail',detalle)+
      (wa.planned_target&&wa.planned_target.description?'<div class="card" style="border-left:3px solid #fb923c"><div class="head"><h3>Lo planeado</h3></div><div style="font-size:12px">'+esc(wa.planned_target.description)+'</div></div>':'');
  }catch(e){el.innerHTML='<div class="card" style="color:var(--muted)">'+e.message+'</div>';}
}

// V9.3 — enviar survey de sensación (calibra demanda medida vs percibida)
async function sendSurvey(id,feel){
  try{
    const r=await fetch(API+'/api/session/'+id+'/survey?feel='+feel,{method:'POST'});
    const d=await r.json();
    if(d.ok){
      toast('Saved');
      const cal=document.getElementById('svy-cal');
      if(cal&&d.calibration)cal.textContent=d.calibration;
      const btns=document.querySelectorAll('#svy-btns button');
      btns.forEach(function(b){b.style.background='rgba(255,255,255,.06)';b.style.border='1px solid rgba(255,255,255,.12)'});
      const map={fresh:0,normal:1,tough:2,very_tough:3,emptied:4};
      const i=map[feel];
      if(btns[i]){btns[i].style.background='rgba(61,214,140,.18)';btns[i].style.border='1px solid rgba(61,214,140,.4)';}
    } else { toast('Could not save'); }
  }catch(e){toast('Could not save');}
}
