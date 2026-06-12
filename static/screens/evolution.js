function setProgressMode(mode){
  progressMode=mode;
  document.querySelectorAll('#progress-tabs .tab').forEach(function(button){
    button.classList.toggle('on',button.dataset.mode===mode);
  });
  $('progress-data').innerHTML='<div class="loading"><span class="spin"></span>Loading...</div>';
  loadProgress(mode);
}

async function loadProgress(mode){
  mode=mode||progressMode||'general';
  progressMode=mode;
  const el=$('progress-data');
  try{
    const sport=mode==='running'?'running':'cycling';
    const [hist,mp,tr,perf,wh,corr,tend,athletic,stravaYearly,stravaWeeks,wgroups,epochsData]=(await Promise.allSettled([
      fetch(API+'/gpt/athletic-history').then(r=>r.json()),
      fetch(API+'/gpt/mars-context').then(r=>r.json()),
      fetch(API+'/gpt/trends?weeks=8').then(r=>r.json()),
      fetch(API+'/gpt/performance-profile?sport='+sport).then(r=>r.json()),
      fetch(API+'/weight/history?limit=10').then(r=>r.json()),
      mode==='general'?Promise.resolve({}):fetch(API+'/gpt/correlaciones?sport='+sport).then(r=>r.json()),
      mode==='general'?Promise.resolve({}):fetch(API+'/gpt/tendencia?sport='+sport).then(r=>r.json()),
      fetch(API+'/gpt/athletic-status').then(r=>r.json()),
      fetch(API+'/api/strava/yearly-summary').then(r=>r.json()),
      fetch(API+'/api/strava/recent-weeks?weeks=60').then(r=>r.json()),
      fetch(API+'/gpt/workout-groups?min_reps=4').then(r=>r.json()),
      fetch(API+'/gpt/epochs').then(r=>r.json())
    ])).map(r=>r.status==='fulfilled'?r.value:{});
    // Strava data como fuente primaria para el timeline anual
    const stravaYearlyRowsAll=stravaYearly.yearly||[];
    const stravaWeekRows=stravaWeeks.weeks||[];
    const totals=hist.totals||{},groups=hist.by_group||[],yearly=hist.yearly||[];
    const running=hist.running||{},cycling=hist.cycling||{},swimming=hist.swimming||{},strength=hist.strength||{};
    function groupName(g){return {Ride:'Ride',Run:'Run',VirtualRide:'Virtual ride',Walk:'Walk',Swim:'Swim',WeightTraining:'Strength',Workout:'Workout',Hike:'Hike',running:'Run',cycling:'Ride',indoor_cardio:'Indoor cardio',walking:'Walk',strength:'Strength',swimming:'Swim',mobility:'Mobility',multi_sport:'Multi-sport',other:'Other'}[g]||g}
    function groupColor(g){return {Ride:'#e8593c',Run:'#3dd68c',VirtualRide:'#f59e0b',Walk:'#22d3ee',Swim:'#38bdf8',WeightTraining:'#c8f135',Workout:'#a78bfa',Hike:'#4a9eff',running:'#3dd68c',cycling:'#e8593c',indoor_cardio:'#4a9eff',walking:'#22d3ee',strength:'#c8f135',swimming:'#38bdf8',mobility:'#f59e0b',multi_sport:'#a78bfa',other:'#8b929f'}[g]||'#8b929f'}
    function num(v,d){return Number(v||0).toLocaleString('es-MX',{maximumFractionDigits:d||0})}
    // Filtrar Strava por modo (tabs General / Bici / Correr)
    const modeSports={cycling:['Ride','VirtualRide'],running:['Run']};
    const stravaYearlyRows=modeSports[mode]
      ?stravaYearlyRowsAll.filter(function(r){return modeSports[mode].includes(r.sport_type)})
      :stravaYearlyRowsAll;

    // Timeline anual — fuente primaria: Strava; fallback: legacy
    const useStravaTimeline=stravaYearlyRows.length>0;
    const timelineSource=useStravaTimeline?stravaYearlyRows:yearly;
    const timelineYears=[...new Set(timelineSource.map(function(x){return x.year}))].sort();
    const maxYearKm=Math.max(1,...timelineYears.map(function(y){
      return timelineSource.filter(function(x){return String(x.year)===String(y)}).reduce(function(a,b){return a+Number(b.km||0)},0);
    }));
    const timelineHtml=timelineYears.map(function(y){
      const rows=timelineSource.filter(function(x){return String(x.year)===String(y)});
      const sport_key=useStravaTimeline?'sport_type':'grupo';
      const bars=rows.map(function(x){
        const w=Math.max(2,Math.round(Number(x.km||0)/maxYearKm*100));
        const sp=x[sport_key];
        return '<div title="'+groupName(sp)+' '+num(x.km,1)+' km · '+num(x.sessions||x.sesiones||0)+' sess" style="height:10px;width:'+w+'%;background:'+groupColor(sp)+';border-radius:999px;margin:3px 0"></div>';
      }).join('');
      const km=rows.reduce(function(a,b){return a+Number(b.km||0)},0);
      const ses=rows.reduce(function(a,b){return a+Number(b.sessions||b.sesiones||0)},0);
      const hrs=rows.reduce(function(a,b){return a+Number(b.hours||b.horas||0)},0);
      return '<div style="display:grid;grid-template-columns:44px 1fr 54px;gap:8px;align-items:center;margin:9px 0" onclick="this.nextSibling&&(this.nextSibling.style.display=this.nextSibling.style.display===\'none\'?\'block\':\'none\')" style="cursor:pointer">'+
        '<div style="font-weight:950">'+y+'</div><div>'+bars+'</div>'+
        '<div style="text-align:right;color:var(--muted);font-size:11px">'+num(km,0)+' km</div></div>'+
        '<div style="display:none;padding:0 0 6px;font-size:11px;color:var(--muted)">'+ses+' sessions · '+num(hrs,1)+' h</div>';
    }).join('');

    // Grupos por deporte — fuente primaria: Strava agrupado
    const groupsSource=useStravaTimeline
      ? Object.values(stravaYearlyRows.reduce(function(acc,r){
          const k=r.sport_type;
          if(!acc[k])acc[k]={sport_type:k,sessions:0,km:0,hours:0};
          acc[k].sessions+=r.sessions;acc[k].km+=r.km;acc[k].hours+=r.hours;
          return acc;
        },{})).sort(function(a,b){return b.km-a.km})
      : groups;
    const groupsHtml=groupsSource.map(function(g){
      const sp=g.sport_type||g.grupo;
      return '<div class="row"><div class="r-main"><div class="r-title">'+groupName(sp)+'</div>'+
        '<div class="r-sub">'+num(g.sessions||g.sesiones||0)+' sessions · '+num(g.hours||g.horas||0,1)+' h</div></div>'+
        '<div class="r-val" style="color:'+groupColor(sp)+'">'+num(g.km,1)+' km</div></div>';
    }).join('');
    const a=mp.athlete||{},z=mp.zonas_ciclismo||{},plan=mp.plan_garmin||{};
    const z2=z.z2||[134,150];
    const peso=wh.current_kg||a.peso_actual_kg||89.1;
    const pesoObj=a.peso_objetivo_kg||80;
    const pesoDiff=+(peso-pesoObj).toFixed(1);
    const pesoPct=Math.max(0,Math.min(100,Math.round(100-(pesoDiff/20)*100)));
    const wvals=(wh.historial||[]).slice(-8).map(function(e){return e.weight_kg}).filter(Boolean);
    const effActual=mp.eff_actual||mp.eff_base||0.1483;
    const effObj=mp.eff_obj||0.155;
    const effPct=Math.min(100,Math.round((effActual/effObj)*100));
    const recs=(perf.records)||{};
    const weeks=tr.weeks||tr.tendencia||[];
    const currentStatus=mode==='general'?athletic:tend;
    const trendChanges=currentStatus.cambios||{};
    const trendColor=['mejorando','continuidad_solida'].includes(currentStatus.estado)?'#3dd68c':['posible_sobrecarga','retrocediendo','pico_de_carga'].includes(currentStatus.estado)?'#e8593c':'#f59e0b';
    const trendTitle={mejorando:'Improving',continuidad_solida:'Solid consistency',construyendo_continuidad:'Building consistency',descarga_o_pausa:'Deload or pause',pico_de_carga:'Load peak',transicion_de_disciplina:'Discipline transition',sin_actividad_reciente:'No recent activity',regreso_tras_pausa:'Return after a pause',pico_atipico:'Atypical peak',respuesta_positiva_carga_alta:'Positive response · high load',carga_en_observacion:'Load under watch',posible_sobrecarga:'Possible overload',retrocediendo:'Regressing',estable:'Steady'}[currentStatus.estado]||'No read yet';
    const rest=(corr.descanso_optimo||{}).mejor_rango||{};
    const volume=(corr.volumen_optimo||{}).mejor_rango||{};
    const heat=corr.costo_calor||{};
    const weightCorr=corr.peso_rendimiento||{};
    const best=tend.mejor_version||{};
    const loadAlert=tend.alerta_carga||{};
    const continuity=athletic.continuidad||{};
    const athleticWeek=athletic.semana_actual||{};
    function signed(v,d,suffix){if(v==null)return '--';const n=Number(v);return(n>0?'+':'')+n.toFixed(d)+(suffix||'')}
    function confidenceLabel(level){return {baja:'Weak signal',media:'Moderate signal',alta:'Strong signal',insuficiente:'Insufficient data',baja_por_confusores:'Inconclusive'}[level]||level||'No evidence'}
    function confidenceBar(level,sample,r2){
      const pct={alta:100,media:66,baja:33,baja_por_confusores:18,insuficiente:8}[level]||8;
      const color={alta:'#3dd68c',media:'#f59e0b',baja:'#e8593c',baja_por_confusores:'#8b929f',insuficiente:'#8b929f'}[level]||'#8b929f';
      const details=(sample?num(sample)+' '+(sample===1?'dato':'datos'):'')+(r2!=null?' · R² '+Number(r2).toFixed(3):'');
      return '<div style="margin-top:4px"><div style="display:flex;justify-content:space-between;gap:8px;font-size:10px;color:var(--muted)"><span>'+confidenceLabel(level)+'</span><span>'+details+'</span></div><div class="pbar" style="height:4px;margin-top:4px"><div class="pfill" style="width:'+pct+'%;background:'+color+'"></div></div></div>';
    }
    function insightRow(title,value,note,color){
      return '<div class="row"><div class="r-main"><div class="r-title">'+title+'</div><div class="r-sub">'+note+'</div></div><div class="r-val" style="color:'+(color||'#a78bfa')+'">'+value+'</div></div>';
    }
    // Semanas recientes — fuente primaria: Strava; fallback: legacy
    const weeksSource=stravaWeekRows.length>0?stravaWeekRows:(tr.weeks||tr.tendencia||[]);
    const kmVals=weeksSource.map(function(w){return parseFloat(w.km||w.distance_km||0)}).filter(function(v){return v>0});
    // ── Año vs año (v6.3) ────────────────────────────────────────────────────
    function yoyCard(){
      if(stravaWeekRows.length<10)return '';
      const sorted=[...stravaWeekRows].sort(function(a,b){return a.week_start<b.week_start?-1:1});
      const current=sorted[sorted.length-1]||{};
      const nowDate=new Date(current.week_start+'T12:00:00');
      const yearAgoTarget=new Date(nowDate);yearAgoTarget.setFullYear(yearAgoTarget.getFullYear()-1);
      const yearAgoStr=yearAgoTarget.toISOString().slice(0,10);
      const past=sorted.reduce(function(best,w){
        if(!best)return w;
        const db=Math.abs(new Date(best.week_start+'T12:00:00')-yearAgoTarget);
        const dw=Math.abs(new Date(w.week_start+'T12:00:00')-yearAgoTarget);
        return dw<db?w:best;
      },null);
      if(!past||past.week_start===current.week_start)return '';
      const curKm=Number(current.km||current.distance_km||0);
      const pastKm=Number(past.km||past.distance_km||0);
      const curH=Number(current.hours||0);
      const pastH=Number(past.hours||0);
      const curSes=Number(current.sessions||0);
      const pastSes=Number(past.sessions||0);
      function delta(cur,past,dec,suf){
        if(!past)return '--';
        const d=cur-past;
        const col=d>0?'#3dd68c':d<0?'#e8593c':'#8b929f';
        return '<span style="color:'+col+';font-weight:800">'+(d>0?'+':'')+d.toFixed(dec)+(suf||'')+'</span>';
      }
      return '<div class="card" style="border-left:3px solid #a78bfa;margin-bottom:8px">'+
        '<div class="head"><h3>This week vs 1 year ago</h3><span style="color:#a78bfa">'+fmtDate(current.week_start)+' · '+fmtDate(past.week_start)+'</span></div>'+
        '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:0;margin-bottom:4px">'+
          '<div style="text-align:center;padding:8px 4px;border-right:1px solid var(--line)">'+
            '<div style="font-size:10px;color:var(--muted);margin-bottom:3px">Km</div>'+
            '<div style="font-size:16px;font-weight:900;margin-bottom:2px">'+(curKm>0?curKm.toFixed(0):'—')+'</div>'+
            '<div style="font-size:11px">'+delta(curKm,pastKm,0,' km')+'</div>'+
            '<div style="font-size:10px;color:var(--muted)">last year: '+(pastKm>0?pastKm.toFixed(0):'—')+'</div>'+
          '</div>'+
          '<div style="text-align:center;padding:8px 4px;border-right:1px solid var(--line)">'+
            '<div style="font-size:10px;color:var(--muted);margin-bottom:3px">Horas</div>'+
            '<div style="font-size:16px;font-weight:900;margin-bottom:2px">'+(curH>0?curH.toFixed(1):'—')+'</div>'+
            '<div style="font-size:11px">'+delta(curH,pastH,1,' h')+'</div>'+
            '<div style="font-size:10px;color:var(--muted)">last year: '+(pastH>0?pastH.toFixed(1):'—')+'</div>'+
          '</div>'+
          '<div style="text-align:center;padding:8px 4px">'+
            '<div style="font-size:10px;color:var(--muted);margin-bottom:3px">Sesiones</div>'+
            '<div style="font-size:16px;font-weight:900;margin-bottom:2px">'+(curSes||'—')+'</div>'+
            '<div style="font-size:11px">'+delta(curSes,pastSes,0,'')+'</div>'+
            '<div style="font-size:10px;color:var(--muted)">last year: '+(pastSes||'—')+'</div>'+
          '</div>'+
        '</div>'+
      '</div>';
    }
    const effVals=(tr.weeks||tr.tendencia||[]).map(function(w){return parseFloat(w.efficiency||w.eficiencia||0)}).filter(function(v){return v>0});
    // Totales Strava para arco atlético — calculados sobre datos ya filtrados por modo
    const arcKm=stravaYearlyRows.reduce(function(a,r){return a+r.km},0);
    const arcH=stravaYearlyRows.reduce(function(a,r){return a+r.hours},0);
    const arcElev=stravaYearlyRows.reduce(function(a,r){return a+r.elevation_m},0);
    const arcYears=[...new Set(stravaYearlyRows.map(function(r){return String(r.year)}))].sort();
    const arcFirst=arcYears[0]||'—',arcLast=arcYears[arcYears.length-1]||'—';
    const arcYearsN=arcYears.length;
    const arcSessions=stravaYearlyRows.reduce(function(a,r){return a+r.sessions},0);
    const arcLabel=mode==='cycling'?'Bici':mode==='running'?'Correr':'Total';
    // v6.1: confidence badges — fuente de datos y cobertura
    const stravaConfBadge='<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px">'+
      '<span style="background:rgba(34,197,94,.15);color:#22c55e;padding:3px 8px;border-radius:8px;font-size:9px;font-weight:900">⚡ Strava</span>'+
      '<span style="background:rgba(74,158,255,.12);color:#4a9eff;padding:3px 8px;border-radius:8px;font-size:9px;font-weight:900">HR ✓</span>'+
      '<span style="background:rgba(200,241,53,.1);color:#c8f135;padding:3px 8px;border-radius:8px;font-size:9px;font-weight:900">GPS ✓</span>'+
      (mode==='cycling'?'<span style="background:rgba(245,158,11,.1);color:#f59e0b;padding:3px 8px;border-radius:8px;font-size:9px;font-weight:900">CAD ✓</span>':'')+
      '<span style="background:rgba(255,255,255,.06);color:var(--muted);padding:3px 8px;border-radius:8px;font-size:9px;font-weight:900">'+arcYearsN+' year'+(arcYearsN!==1?'s':'')+' · '+arcSessions+' sessions</span>'+
    '</div>';
    const stravaArcHtml=arcKm>0?
      '<div class="card"><div class="head"><h3>Arco · '+arcLabel+'</h3><span style="color:#22c55e;font-size:10px;font-weight:900">Alta confianza</span></div>'+
        stravaConfBadge+
        '<div class="grid2">'+
          metric('Kilometers',num(Math.round(arcKm),0)+' km','across '+arcYearsN+' year'+(arcYearsN!==1?'s':''))+
          metric('Hours',num(Math.round(arcH),0)+' h','real active time')+
          metric('Climbing',num(Math.round(arcElev/1000),1)+'k m','accumulated')+
          metric('Activities',arcSessions.toLocaleString(),'sessions')+
        '</div></div>':'';
    function spark(vals,col,h){
      if(!vals||vals.length<2)return '';
      const mx=Math.max(...vals),mn=Math.min(...vals),rng=mx-mn||0.001,W=100;
      const pts=vals.map(function(v,i){return Math.round(i/(vals.length-1)*W)+','+Math.round(h-(v-mn)/rng*(h-4)-2)}).join(' ');
      return '<svg viewBox="0 0 '+W+' '+h+'" style="width:100%;height:'+h+'px"><polyline points="'+pts+'" fill="none" stroke="'+col+'" stroke-width="1.8" stroke-linecap="round"/></svg>';
    }
    const athleticLoadVals=(athletic.historial||[]).map(function(w){return Number(w.load_equivalent_hours||0)});
    const generalTrendHtml=
      '<div class="card" style="border-left:3px solid '+trendColor+'"><div class="head"><h3>What is happening</h3><span style="color:'+trendColor+'">'+trendTitle+'</span></div>'+
        '<div style="font-size:13px;line-height:1.55;margin-bottom:10px">'+(athletic.explicacion||'Not enough overall signal yet.')+'</div>'+
        '<div class="grid2">'+
          metric('Total load',signed(trendChanges.carga_pct,0,'%'),'4 weeks vs previous 4')+
          metric('Active days',signed(trendChanges.dias_activos,1,''),'avg weekly change')+
          metric('Streak',continuity.racha_semanas_activas||0,'active weeks')+
          metric('This week',Number(athletic.carga_actual_equivalente_h||0).toFixed(1)+' h','equivalent load')+
        '</div>'+
        (athleticLoadVals.length>=2?spark(athleticLoadVals,'#a78bfa',42):'')+
      '</div>'+
      '<div class="card"><div class="head"><h3>Where you are heading</h3><span>'+((athletic.disciplinas_actuales||[]).length)+' '+((athletic.disciplinas_actuales||[]).length===1?'discipline':'disciplines')+' this week</span></div>'+
        '<div style="font-size:14px;font-weight:850;line-height:1.5">'+(athletic.recomendacion||'Keep consistency before adding load.')+'</div>'+
        '<div style="font-size:11px;color:var(--muted);margin-top:8px">Combined load from cycling, running, cardio, strength, swimming, walking and mobility.</div>'+
      '</div>'+
      '<div class="card"><div class="head"><h3>Current week</h3><span>'+(athleticWeek.week_start?fmtDate(athleticWeek.week_start):'--')+'</span></div><div class="grid2">'+
        metric('Sessions',athleticWeek.sessions||0,'all disciplines')+
        metric('Active days',athleticWeek.active_days||0,'of 7 days')+
        metric('Real hours',Number(athleticWeek.hours||0).toFixed(1)+' h','total duration')+
        metric('Equivalent load',Number(athleticWeek.load_equivalent_hours||0).toFixed(1)+' h','sport-adjusted')+
      '</div></div>';
    const sportTrendHtml=
      '<div class="card" style="border-left:3px solid '+trendColor+'"><div class="head"><h3>'+(mode==='running'?'Running':'Cycling')+' trend</h3><span style="color:'+trendColor+'">'+trendTitle+'</span></div>'+
        '<div style="font-size:13px;line-height:1.55;margin-bottom:8px">'+(tend.explicacion||'Not yet eight full weeks to compare.')+'</div>'+
        '<div class="grid2">'+
          metric('Efficiency',signed(trendChanges.eficiencia_pct,1,'%'),'4 weeks vs previous 4')+
          metric('HR',signed(trendChanges.fc_bpm,1,' bpm'),'lower is usually good')+
          metric('Volume',signed(trendChanges.volumen_horas_pct,0,'%'),loadAlert.contexto==='regreso_tras_pausa'?'return after pause':loadAlert.contexto==='pico_atipico'?'atypical peak':'hours per week')+
          metric('Direction',tend.direccion||'--',tend.actividad_reciente===false?'no recent sessions':(tend.senales||[]).length+' signals')+
        '</div></div>'+
      '<div class="card"><div class="head"><h3>Your best self</h3><span>'+(best.semana?fmtDate(best.semana):'all-time')+'</span></div>'+
        '<div class="grid2">'+
          metric('Best efficiency',best.eficiencia_historica!=null?Number(best.eficiencia_historica).toFixed(4):'--','sustainable reference')+
          metric('Today',best.eficiencia_actual!=null?Number(best.eficiencia_actual).toFixed(4):'--',best.porcentaje_mejor_forma!=null?Number(best.porcentaje_mejor_forma).toFixed(0)+'% of your best form':'no comparison')+
        '</div>'+
        (best.porcentaje_mejor_forma!=null?'<div class="pbar" style="margin-top:10px"><div class="pfill" style="width:'+Math.min(100,Number(best.porcentaje_mejor_forma))+'%;background:#3dd68c"></div></div>':'')+
      '</div>'+
      '<div class="card"><div class="head"><h3>What produces your best response</h3><span>'+(mode==='running'?'running':'cycling')+'</span></div>'+
        insightRow('Rest',rest.rango?rest.rango.replaceAll('_',' '):'--',confidenceLabel((corr.descanso_optimo||{}).confianza),'#3dd68c')+
        insightRow('Prior volume',volume.km_semana_anterior?volume.km_semana_anterior+' km':'--',confidenceLabel((corr.volumen_optimo||{}).confianza),'#e8593c')+
        (mode==='cycling'?'<div class="row"><div class="r-main"><div class="r-title">Cost of +10 °C</div>'+confidenceBar(heat.confianza,heat.muestra_sesiones,heat.r2)+'</div><div class="r-val" style="color:#f59e0b">'+(heat.bpm_por_10c_a_misma_velocidad!=null?signed(heat.bpm_por_10c_a_misma_velocidad,1,' bpm'):'--')+'</div></div>':'')+
        '<div class="row"><div class="r-main"><div class="r-title">Weight vs performance</div>'+confidenceBar(weightCorr.confianza,weightCorr.muestra_ajustada_semanas,null)+'</div><div class="r-val" style="color:#a78bfa">'+(tend.actividad_reciente!==false&&weightCorr.usable_para_recomendacion&&weightCorr.cambio_estimado_al_bajar_1kg!=null?signed(weightCorr.cambio_estimado_al_bajar_1kg,5,' efic.'):'Inconclusive')+'</div></div>'+
      '</div>';
    // V7.1 fase 2: workouts repetidos (ruta+intención) con tendencia bajo demanda
    function labsCard(){
      const gs=(wgroups&&wgroups.groups)||[];
      if(!gs.length)return '';
      const rows=gs.slice(0,6).map(function(g,i){
        return '<div class="row" style="cursor:pointer" onclick="loadGroupTrend(this,\''+g.route_id+'\',\''+g.sport_type+'\')">'+
          '<div class="r-ico">'+({Ride:'🚴',Run:'🏃',VirtualRide:'⚡'}[g.sport_type]||'🏅')+'</div>'+
          '<div class="r-main"><div class="r-title">'+(g.route_name||('Ruta '+g.route_id.slice(0,8)))+' · '+g.sport_type+' <span onclick="event.stopPropagation();renameRoute(\''+g.route_id+'\')" style="font-size:11px;color:var(--muted);cursor:pointer" title="Name route">✏️</span></div>'+
          '<div class="r-sub">'+g.repetitions+' repetitions · '+fmtDate(g.first)+' → '+fmtDate(g.last)+'</div>'+
          '<div class="grp-trend" style="display:none;font-size:11px;line-height:1.55;margin-top:6px;color:var(--text)"></div></div>'+
          '<div class="r-val" style="color:#22d3ee">'+(g.avg_speed_kmh||'—')+' km/h</div></div>';
      }).join('');
      return '<div class="card" style="border-left:3px solid #22d3ee">'+
        '<div class="head"><h3>Your laboratories</h3><span>same workout, repeated — tap for trend</span></div>'+
        '<div style="font-size:11px;color:var(--muted);margin-bottom:8px">Routes you repeat with the same intent. Here you see whether the SAME effort improves — not against your fast days, against equal days.</div>'+
        rows+'</div>';
    }
    window.loadGroupTrend=async function(rowEl,routeId,sport){
      const box=rowEl.querySelector('.grp-trend');
      if(!box)return;
      if(box.style.display==='block'){box.style.display='none';return;}
      box.style.display='block';box.textContent='Reading repetitions…';
      try{
        const d=await fetch(API+'/gpt/workout-group/trend?route_id='+encodeURIComponent(routeId)+'&sport='+encodeURIComponent(sport)).then(r=>r.json());
        if(!d.ok){box.textContent='Not enough data';return;}
        const v=(d.trend||{}).verdict;
        const col=v==='mejorando'?'#3dd68c':v==='retrocediendo'?'#f59e0b':'#8e95a3';
        box.innerHTML='<span style="color:'+col+';font-weight:800">'+(v?v.toUpperCase():'FEW REPS')+'</span> · '+d.explanation_text+
          ((d.available_intents&&Object.keys(d.available_intents).length>1)?'<br><span style="color:var(--muted)">Intenciones en esta ruta: '+Object.entries(d.available_intents).map(function(e){return e[0]+' ('+e[1]+')'}).join(' · ')+'</span>':'');
      }catch(e){box.textContent='Could not load trend';}
    };
    // V7.4: Tus épocas — la historia atlética como eras
    function epochsCard(){
      const eps=(epochsData&&epochsData.epochs)||[];
      if(eps.length<2)return '';
      const ico={bici:'🚴',correr:'🏃',fuerza:'🏋️',caminar:'🚶',nadar:'🏊',mixto:'🔀'};
      const rows=eps.map(function(ep){
        if(ep.is_pause){
          return '<div style="display:flex;align-items:center;gap:10px;padding:5px 0 5px 20px;opacity:.55">'+
            '<div style="width:2px;height:22px;background:var(--line);border-radius:1px"></div>'+
            '<div style="font-size:10px;color:var(--muted)">pausa · '+fmtShort(ep.start+'-01')+' → '+fmtShort(ep.end+'-01')+'</div></div>';
        }
        const col=ep.is_current?'#fb923c':'#a78bfa';
        return '<div class="row">'+
          '<div class="r-ico">'+(ico[ep.dominant]||'🏅')+'</div>'+
          '<div class="r-main"><div class="r-title" style="color:'+col+'">'+ep.name+(ep.is_current?' · now':'')+'</div>'+
          '<div class="r-sub">'+fmtShort(ep.start+'-01')+' → '+fmtShort(ep.end+'-01')+' · '+ep.months+' months · '+ep.sessions+' sessions</div></div>'+
          '<div class="r-val" style="color:'+col+'">'+ep.km.toLocaleString()+' km</div></div>';
      }).join('');
      return '<div class="card" style="border-left:3px solid #a78bfa">'+
        '<div class="head"><h3>Your epochs</h3><span>the sport changes · the body is the same</span></div>'+
        '<div style="font-size:11px;color:var(--muted);line-height:1.55;margin-bottom:8px">'+(epochsData.explanation_text||'')+'</div>'+
        rows+'</div>';
    }
    // V8 UI: lo diferencial arriba (épocas, laboratorios, tendencia) · números colapsados
    const _historiaNumeros=
      stravaArcHtml+
      '<div class="card"><div class="head"><h3>Timeline</h3><span>'+(arcFirst!=='—'?arcFirst+' → '+arcLast:'2018 → today')+'</span></div>'+timelineHtml+'</div>'+
      '<div class="card"><div class="head"><h3>By discipline</h3><span>'+groupsSource.length+'</span></div>'+groupsHtml+'</div>';
    // Guía UI §5: cada pantalla abre con una conclusión, no con datos
    const _evoInsight='<div style="background:linear-gradient(135deg,rgba(167,139,250,.12),rgba(167,139,250,.03));border:1px solid rgba(167,139,250,.3);border-radius:18px;padding:16px 18px;margin-bottom:12px">'+
      '<div class="q-kicker">Am I improving?</div>'+
      '<div style="font-size:15px;line-height:1.5;font-weight:650"><span style="color:'+trendColor+'">'+trendTitle+'.</span> '+
      ((mode==='general'?athletic.explicacion:tend.explicacion)||'Not enough signal yet — keep stacking weeks.')+'</div>'+
    '</div>';
    el.innerHTML=
      _evoInsight+
      epochsCard()+
      (mode==='general'?generalTrendHtml:sportTrendHtml)+
      labsCard()+
      yoyCard()+
      '<div class="card"><div class="head"><h3>Weight</h3><span style="color:#3dd68c">'+peso+' kg</span></div><div class="pbar"><div class="pfill" style="width:'+pesoPct+'%;background:#3dd68c"></div></div><div style="display:flex;justify-content:space-between;font-size:10px;color:var(--muted);margin-top:4px;margin-bottom:6px"><span>Actual: '+peso+' kg</span><span>Obj: '+pesoObj+' kg · '+(pesoDiff>0?'faltan '+pesoDiff+' kg':'meta lograda')+'</span></div>'+spark(wvals,'#3dd68c',28)+'</div>'+
      evd('Efficiency, weeks and zones',(mode==='cycling'?'<div class="card"><div class="head"><h3>Efficiency (speed per heartbeat)</h3><span style="color:#a78bfa">'+effPct+'%</span></div><div class="pbar"><div class="pfill" style="width:'+effPct+'%;background:#a78bfa"></div></div><div style="display:flex;justify-content:space-between;font-size:10px;color:var(--muted);margin-top:4px;margin-bottom:6px"><span>Actual: '+effActual.toFixed(4)+'</span><span>Obj: '+effObj+'+</span></div>'+spark(effVals,'#a78bfa',28)+'</div>':'')+
      (kmVals.length>=2?'<div class="card"><div class="head"><h3>Km per week</h3><span>'+(kmVals[kmVals.length-1]||0).toFixed(0)+' km</span></div>'+spark(kmVals,'#e8593c',36)+'<div style="display:flex;justify-content:space-between;font-size:10px;color:var(--muted);margin-top:4px"><span>'+weeksSource.length+' weeks</span><span>Max: '+Math.max(...kmVals).toFixed(0)+' km</span><span>Prom: '+(kmVals.reduce(function(a,b){return a+b},0)/kmVals.length).toFixed(0)+' km</span></div></div>':'')+
      (mode==='cycling'?'<div class="card"><div class="head"><h3>Cycling zones</h3><span style="color:#3dd68c">LT '+(z.lt_bpm||168)+' bpm</span></div>'+
        [['Z1','0–108','#8b929f'],['Trans','109–133','#f59e0b'],['Z2',z2[0]+'–'+z2[1],'#3dd68c'],['Z3',(z.z3?z.z3[0]:151)+'-'+(z.z3?z.z3[1]:160),'#f59e0b'],['Z4',(z.z4?z.z4[0]:161)+'-'+(z.z4?z.z4[1]:168),'#e8593c'],['Z5','169+','#c026d3']].map(function(zz){return'<div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid rgba(255,255,255,.05)"><span style="font-size:11px">'+zz[0]+'</span><span style="font-size:11px;font-weight:800;color:'+zz[2]+'">'+zz[1]+' bpm</span></div>';}).join('')+
      '</div>':''))+
      evd('Your story in numbers',_historiaNumeros+
      (Object.keys(recs).length?'<div class="card"><div class="head"><h3>Records</h3></div>'+
        (recs.max_distance?'<div class="row"><div class="r-main"><div class="r-title">Longest distance</div><div class="r-sub">'+fmtDate((recs.max_distance||{}).date)+'</div></div><div class="r-val" style="color:#e8593c">'+Number(((recs.max_distance||{}).value)||0).toFixed(1)+' km</div></div>':'')+
        (recs.max_speed?'<div class="row"><div class="r-main"><div class="r-title">Top speed</div><div class="r-sub">'+fmtDate((recs.max_speed||{}).date)+'</div></div><div class="r-val" style="color:#a78bfa">'+Number(((recs.max_speed||{}).value)||0).toFixed(1)+' km/h</div></div>':'')+
      '</div>':''));
  }catch(e){el.innerHTML='<div class="card" style="color:var(--muted)">'+e.message+'</div>'}
}


window.renameRoute=async function(routeId){
  const name=prompt('Name for this route (e.g. "Atizapan base"):');
  if(!name)return;
  try{
    const d=await fetch(API+'/api/route/'+encodeURIComponent(routeId)+'/rename?name='+encodeURIComponent(name),{method:'POST'}).then(r=>r.json());
    toast(d.ok?'Route named: '+name:'Error');
    loadProgress(progressMode);
  }catch(e){toast('Network error');}
};
