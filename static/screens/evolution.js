function setProgressMode(mode){
  progressMode=mode;
  document.querySelectorAll('#progress-tabs .tab').forEach(function(button){
    button.classList.toggle('on',button.dataset.mode===mode);
  });
  $('progress-data').innerHTML='<div class="loading"><span class="spin"></span>Cargando...</div>';
  loadProgress(mode);
}

async function loadProgress(mode){
  mode=mode||progressMode||'general';
  progressMode=mode;
  const el=$('progress-data');
  try{
    const sport=mode==='running'?'running':'cycling';
    const [hist,mp,tr,perf,wh,corr,tend,athletic,stravaYearly,stravaWeeks]=(await Promise.allSettled([
      fetch(API+'/gpt/athletic-history').then(r=>r.json()),
      fetch(API+'/gpt/mars-context').then(r=>r.json()),
      fetch(API+'/gpt/trends?weeks=8').then(r=>r.json()),
      fetch(API+'/gpt/performance-profile?sport='+sport).then(r=>r.json()),
      fetch(API+'/weight/history?limit=10').then(r=>r.json()),
      mode==='general'?Promise.resolve({}):fetch(API+'/gpt/correlaciones?sport='+sport).then(r=>r.json()),
      mode==='general'?Promise.resolve({}):fetch(API+'/gpt/tendencia?sport='+sport).then(r=>r.json()),
      fetch(API+'/gpt/athletic-status').then(r=>r.json()),
      fetch(API+'/api/strava/yearly-summary').then(r=>r.json()),
      fetch(API+'/api/strava/recent-weeks?weeks=60').then(r=>r.json())
    ])).map(r=>r.status==='fulfilled'?r.value:{});
    // Strava data como fuente primaria para el timeline anual
    const stravaYearlyRowsAll=stravaYearly.yearly||[];
    const stravaWeekRows=stravaWeeks.weeks||[];
    const totals=hist.totals||{},groups=hist.by_group||[],yearly=hist.yearly||[];
    const running=hist.running||{},cycling=hist.cycling||{},swimming=hist.swimming||{},strength=hist.strength||{};
    function groupName(g){return {Ride:'Bici',Run:'Correr',VirtualRide:'Bici virtual',Walk:'Caminata',Swim:'Nadar',WeightTraining:'Fuerza',Workout:'Workout',Hike:'Senderismo',running:'Correr',cycling:'Bici',indoor_cardio:'Cardio indoor',walking:'Caminata',strength:'Fuerza',swimming:'Nadar',mobility:'Movilidad',multi_sport:'Multi-sport',other:'Otros'}[g]||g}
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
        return '<div title="'+groupName(sp)+' '+num(x.km,1)+' km · '+num(x.sessions||x.sesiones||0)+' ses" style="height:10px;width:'+w+'%;background:'+groupColor(sp)+';border-radius:999px;margin:3px 0"></div>';
      }).join('');
      const km=rows.reduce(function(a,b){return a+Number(b.km||0)},0);
      const ses=rows.reduce(function(a,b){return a+Number(b.sessions||b.sesiones||0)},0);
      const hrs=rows.reduce(function(a,b){return a+Number(b.hours||b.horas||0)},0);
      return '<div style="display:grid;grid-template-columns:44px 1fr 54px;gap:8px;align-items:center;margin:9px 0" onclick="this.nextSibling&&(this.nextSibling.style.display=this.nextSibling.style.display===\'none\'?\'block\':\'none\')" style="cursor:pointer">'+
        '<div style="font-weight:950">'+y+'</div><div>'+bars+'</div>'+
        '<div style="text-align:right;color:var(--muted);font-size:11px">'+num(km,0)+' km</div></div>'+
        '<div style="display:none;padding:0 0 6px;font-size:11px;color:var(--muted)">'+ses+' sesiones · '+num(hrs,1)+' h</div>';
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
        '<div class="r-sub">'+num(g.sessions||g.sesiones||0)+' sesiones · '+num(g.hours||g.horas||0,1)+' h</div></div>'+
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
    const trendTitle={mejorando:'Mejorando',continuidad_solida:'Continuidad sólida',construyendo_continuidad:'Construyendo continuidad',descarga_o_pausa:'Descarga o pausa',pico_de_carga:'Pico de carga',transicion_de_disciplina:'Transición de disciplina',sin_actividad_reciente:'Sin actividad reciente',regreso_tras_pausa:'Regreso tras pausa',pico_atipico:'Pico atípico',respuesta_positiva_carga_alta:'Respuesta positiva · carga alta',carga_en_observacion:'Carga en observación',posible_sobrecarga:'Posible sobrecarga',retrocediendo:'Retrocediendo',estable:'Estable'}[currentStatus.estado]||'Sin lectura';
    const rest=(corr.descanso_optimo||{}).mejor_rango||{};
    const volume=(corr.volumen_optimo||{}).mejor_rango||{};
    const heat=corr.costo_calor||{};
    const weightCorr=corr.peso_rendimiento||{};
    const best=tend.mejor_version||{};
    const loadAlert=tend.alerta_carga||{};
    const continuity=athletic.continuidad||{};
    const athleticWeek=athletic.semana_actual||{};
    function signed(v,d,suffix){if(v==null)return '--';const n=Number(v);return(n>0?'+':'')+n.toFixed(d)+(suffix||'')}
    function confidenceLabel(level){return {baja:'Señal débil',media:'Señal moderada',alta:'Señal fuerte',insuficiente:'Datos insuficientes',baja_por_confusores:'No concluyente'}[level]||level||'Sin evidencia'}
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
        '<div class="head"><h3>Esta semana vs hace 1 año</h3><span style="color:#a78bfa">'+fmtDate(current.week_start)+' · '+fmtDate(past.week_start)+'</span></div>'+
        '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:0;margin-bottom:4px">'+
          '<div style="text-align:center;padding:8px 4px;border-right:1px solid var(--line)">'+
            '<div style="font-size:10px;color:var(--muted);margin-bottom:3px">Km</div>'+
            '<div style="font-size:16px;font-weight:900;margin-bottom:2px">'+(curKm>0?curKm.toFixed(0):'—')+'</div>'+
            '<div style="font-size:11px">'+delta(curKm,pastKm,0,' km')+'</div>'+
            '<div style="font-size:10px;color:var(--muted)">año pasado: '+(pastKm>0?pastKm.toFixed(0):'—')+'</div>'+
          '</div>'+
          '<div style="text-align:center;padding:8px 4px;border-right:1px solid var(--line)">'+
            '<div style="font-size:10px;color:var(--muted);margin-bottom:3px">Horas</div>'+
            '<div style="font-size:16px;font-weight:900;margin-bottom:2px">'+(curH>0?curH.toFixed(1):'—')+'</div>'+
            '<div style="font-size:11px">'+delta(curH,pastH,1,' h')+'</div>'+
            '<div style="font-size:10px;color:var(--muted)">año pasado: '+(pastH>0?pastH.toFixed(1):'—')+'</div>'+
          '</div>'+
          '<div style="text-align:center;padding:8px 4px">'+
            '<div style="font-size:10px;color:var(--muted);margin-bottom:3px">Sesiones</div>'+
            '<div style="font-size:16px;font-weight:900;margin-bottom:2px">'+(curSes||'—')+'</div>'+
            '<div style="font-size:11px">'+delta(curSes,pastSes,0,'')+'</div>'+
            '<div style="font-size:10px;color:var(--muted)">año pasado: '+(pastSes||'—')+'</div>'+
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
      '<span style="background:rgba(255,255,255,.06);color:var(--muted);padding:3px 8px;border-radius:8px;font-size:9px;font-weight:900">'+arcYearsN+' año'+(arcYearsN!==1?'s':'')+' · '+arcSessions+' sesiones</span>'+
    '</div>';
    const stravaArcHtml=arcKm>0?
      '<div class="card"><div class="head"><h3>Arco · '+arcLabel+'</h3><span style="color:#22c55e;font-size:10px;font-weight:900">Alta confianza</span></div>'+
        stravaConfBadge+
        '<div class="grid2">'+
          metric('Kilómetros',num(Math.round(arcKm),0)+' km','en '+arcYearsN+' año'+(arcYearsN!==1?'s':''))+
          metric('Horas',num(Math.round(arcH),0)+' h','tiempo real activo')+
          metric('Elevación',num(Math.round(arcElev/1000),1)+' mil m','acumulado')+
          metric('Actividades',arcSessions.toLocaleString(),'sesiones')+
        '</div></div>':'';
    function spark(vals,col,h){
      if(!vals||vals.length<2)return '';
      const mx=Math.max(...vals),mn=Math.min(...vals),rng=mx-mn||0.001,W=100;
      const pts=vals.map(function(v,i){return Math.round(i/(vals.length-1)*W)+','+Math.round(h-(v-mn)/rng*(h-4)-2)}).join(' ');
      return '<svg viewBox="0 0 '+W+' '+h+'" style="width:100%;height:'+h+'px"><polyline points="'+pts+'" fill="none" stroke="'+col+'" stroke-width="1.8" stroke-linecap="round"/></svg>';
    }
    const athleticLoadVals=(athletic.historial||[]).map(function(w){return Number(w.load_equivalent_hours||0)});
    const generalTrendHtml=
      '<div class="card" style="border-left:3px solid '+trendColor+'"><div class="head"><h3>Qué está pasando</h3><span style="color:'+trendColor+'">'+trendTitle+'</span></div>'+
        '<div style="font-size:13px;line-height:1.55;margin-bottom:10px">'+(athletic.explicacion||'Aún no hay lectura general suficiente.')+'</div>'+
        '<div class="grid2">'+
          metric('Carga total',signed(trendChanges.carga_pct,0,'%'),'4 semanas vs 4 anteriores')+
          metric('Días activos',signed(trendChanges.dias_activos,1,''),'cambio semanal promedio')+
          metric('Racha',continuity.racha_semanas_activas||0,'semanas activas')+
          metric('Esta semana',Number(athletic.carga_actual_equivalente_h||0).toFixed(1)+' h','carga equivalente')+
        '</div>'+
        (athleticLoadVals.length>=2?spark(athleticLoadVals,'#a78bfa',42):'')+
      '</div>'+
      '<div class="card"><div class="head"><h3>Hacia dónde vas</h3><span>'+((athletic.disciplinas_actuales||[]).length)+' '+((athletic.disciplinas_actuales||[]).length===1?'disciplina':'disciplinas')+' esta semana</span></div>'+
        '<div style="font-size:14px;font-weight:850;line-height:1.5">'+(athletic.recomendacion||'Mantener continuidad antes de aumentar carga.')+'</div>'+
        '<div style="font-size:11px;color:var(--muted);margin-top:8px">Carga combinada de bici, correr, cardio, fuerza, natación, caminata y movilidad.</div>'+
      '</div>'+
      '<div class="card"><div class="head"><h3>Semana actual</h3><span>'+(athleticWeek.week_start?fmtDate(athleticWeek.week_start):'--')+'</span></div><div class="grid2">'+
        metric('Sesiones',athleticWeek.sessions||0,'todas las disciplinas')+
        metric('Días activos',athleticWeek.active_days||0,'de 7 días')+
        metric('Horas reales',Number(athleticWeek.hours||0).toFixed(1)+' h','duración total')+
        metric('Carga equivalente',Number(athleticWeek.load_equivalent_hours||0).toFixed(1)+' h','ajustada por deporte')+
      '</div></div>';
    const sportTrendHtml=
      '<div class="card" style="border-left:3px solid '+trendColor+'"><div class="head"><h3>Tendencia '+(mode==='running'?'de correr':'de bici')+'</h3><span style="color:'+trendColor+'">'+trendTitle+'</span></div>'+
        '<div style="font-size:13px;line-height:1.55;margin-bottom:8px">'+(tend.explicacion||'Aún no hay ocho semanas completas para comparar.')+'</div>'+
        '<div class="grid2">'+
          metric('Eficiencia',signed(trendChanges.eficiencia_pct,1,'%'),'4 semanas vs 4 anteriores')+
          metric('FC',signed(trendChanges.fc_bpm,1,' bpm'),'bajar suele ser favorable')+
          metric('Volumen',signed(trendChanges.volumen_horas_pct,0,'%'),loadAlert.contexto==='regreso_tras_pausa'?'regreso tras pausa':loadAlert.contexto==='pico_atipico'?'pico atípico':'horas por semana')+
          metric('Dirección',tend.direccion||'--',tend.actividad_reciente===false?'sin sesiones recientes':(tend.senales||[]).length+' señales')+
        '</div></div>'+
      '<div class="card"><div class="head"><h3>Tu mejor versión</h3><span>'+(best.semana?fmtDate(best.semana):'histórico')+'</span></div>'+
        '<div class="grid2">'+
          metric('Mejor eficiencia',best.eficiencia_historica!=null?Number(best.eficiencia_historica).toFixed(4):'--','referencia sostenible')+
          metric('Hoy',best.eficiencia_actual!=null?Number(best.eficiencia_actual).toFixed(4):'--',best.porcentaje_mejor_forma!=null?Number(best.porcentaje_mejor_forma).toFixed(0)+'% de tu mejor forma':'sin comparación')+
        '</div>'+
        (best.porcentaje_mejor_forma!=null?'<div class="pbar" style="margin-top:10px"><div class="pfill" style="width:'+Math.min(100,Number(best.porcentaje_mejor_forma))+'%;background:#3dd68c"></div></div>':'')+
      '</div>'+
      '<div class="card"><div class="head"><h3>Lo que produce tu mejor respuesta</h3><span>'+(mode==='running'?'correr':'bici')+'</span></div>'+
        insightRow('Descanso',rest.rango?rest.rango.replaceAll('_',' '):'--',confidenceLabel((corr.descanso_optimo||{}).confianza),'#3dd68c')+
        insightRow('Volumen previo',volume.km_semana_anterior?volume.km_semana_anterior+' km':'--',confidenceLabel((corr.volumen_optimo||{}).confianza),'#e8593c')+
        (mode==='cycling'?'<div class="row"><div class="r-main"><div class="r-title">Costo de +10 °C</div>'+confidenceBar(heat.confianza,heat.muestra_sesiones,heat.r2)+'</div><div class="r-val" style="color:#f59e0b">'+(heat.bpm_por_10c_a_misma_velocidad!=null?signed(heat.bpm_por_10c_a_misma_velocidad,1,' bpm'):'--')+'</div></div>':'')+
        '<div class="row"><div class="r-main"><div class="r-title">Peso vs rendimiento</div>'+confidenceBar(weightCorr.confianza,weightCorr.muestra_ajustada_semanas,null)+'</div><div class="r-val" style="color:#a78bfa">'+(tend.actividad_reciente!==false&&weightCorr.usable_para_recomendacion&&weightCorr.cambio_estimado_al_bajar_1kg!=null?signed(weightCorr.cambio_estimado_al_bajar_1kg,5,' efic.'):'No concluyente')+'</div></div>'+
      '</div>';
    el.innerHTML=
      stravaArcHtml+
      yoyCard()+
      (mode==='general'?generalTrendHtml:sportTrendHtml)+
      '<div class="card"><div class="head"><h3>Línea de tiempo</h3><span>'+(arcFirst!=='—'?arcFirst+' → '+arcLast:'2018 → hoy')+'</span></div>'+timelineHtml+'</div>'+
      '<div class="card"><div class="head"><h3>Por disciplina</h3><span>'+groupsSource.length+'</span></div>'+groupsHtml+'</div>'+
      '<div class="card"><div class="head"><h3>Peso</h3><span style="color:#3dd68c">'+peso+' kg</span></div><div class="pbar"><div class="pfill" style="width:'+pesoPct+'%;background:#3dd68c"></div></div><div style="display:flex;justify-content:space-between;font-size:10px;color:var(--muted);margin-top:4px;margin-bottom:6px"><span>Actual: '+peso+' kg</span><span>Obj: '+pesoObj+' kg · '+(pesoDiff>0?'faltan '+pesoDiff+' kg':'meta lograda')+'</span></div>'+spark(wvals,'#3dd68c',28)+'</div>'+
      (mode==='cycling'?'<div class="card"><div class="head"><h3>Eficiencia vel/FC</h3><span style="color:#a78bfa">'+effPct+'%</span></div><div class="pbar"><div class="pfill" style="width:'+effPct+'%;background:#a78bfa"></div></div><div style="display:flex;justify-content:space-between;font-size:10px;color:var(--muted);margin-top:4px;margin-bottom:6px"><span>Actual: '+effActual.toFixed(4)+'</span><span>Obj: '+effObj+'+</span></div>'+spark(effVals,'#a78bfa',28)+'</div>':'')+
      (kmVals.length>=2?'<div class="card"><div class="head"><h3>Km por semana</h3><span>'+(kmVals[kmVals.length-1]||0).toFixed(0)+' km</span></div>'+spark(kmVals,'#e8593c',36)+'<div style="display:flex;justify-content:space-between;font-size:10px;color:var(--muted);margin-top:4px"><span>'+weeksSource.length+' semanas</span><span>Max: '+Math.max(...kmVals).toFixed(0)+' km</span><span>Prom: '+(kmVals.reduce(function(a,b){return a+b},0)/kmVals.length).toFixed(0)+' km</span></div></div>':'')+
      (mode==='cycling'?'<div class="card"><div class="head"><h3>Zonas ciclismo</h3><span style="color:#3dd68c">LT '+(z.lt_bpm||168)+' bpm</span></div>'+
        [['Z1','0–108','#8b929f'],['Trans','109–133','#f59e0b'],['Z2',z2[0]+'–'+z2[1],'#3dd68c'],['Z3',(z.z3?z.z3[0]:151)+'-'+(z.z3?z.z3[1]:160),'#f59e0b'],['Z4',(z.z4?z.z4[0]:161)+'-'+(z.z4?z.z4[1]:168),'#e8593c'],['Z5','169+','#c026d3']].map(function(zz){return'<div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid rgba(255,255,255,.05)"><span style="font-size:11px">'+zz[0]+'</span><span style="font-size:11px;font-weight:800;color:'+zz[2]+'">'+zz[1]+' bpm</span></div>';}).join('')+
      '</div>':'')+
      (Object.keys(recs).length?'<div class="card"><div class="head"><h3>Records</h3></div>'+
        (recs.max_distance?'<div class="row"><div class="r-main"><div class="r-title">Mayor distancia</div><div class="r-sub">'+fmtDate((recs.max_distance||{}).date)+'</div></div><div class="r-val" style="color:#e8593c">'+Number(((recs.max_distance||{}).value)||0).toFixed(1)+' km</div></div>':'')+
        (recs.max_speed?'<div class="row"><div class="r-main"><div class="r-title">Mayor velocidad</div><div class="r-sub">'+fmtDate((recs.max_speed||{}).date)+'</div></div><div class="r-val" style="color:#a78bfa">'+Number(((recs.max_speed||{}).value)||0).toFixed(1)+' km/h</div></div>':'')+
      '</div>':'');
  }catch(e){el.innerHTML='<div class="card" style="color:var(--muted)">'+e.message+'</div>'}
}
