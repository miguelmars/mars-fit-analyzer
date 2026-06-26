async function loadPerfil(){
  const el=$('perfil-data');
  try{
    const [p,w,bf,sy,sq]=(await Promise.allSettled([
      fetch(API+'/gpt/mars-context').then(r=>r.json()),
      fetch(API+'/weight/history?limit=1').then(r=>r.json()),
      fetch(API+'/api/strava/backfill-status').then(r=>r.json()),
      fetch(API+'/api/strava/yearly-summary').then(r=>r.json()),
      fetch(API+'/api/strava/stream-completeness').then(r=>r.json())
    ])).map(r=>r.status==='fulfilled'?r.value:{});
    const a=p.athlete||{};
    const zc=p.zonas_ciclismo||{};
    const zr=p.zonas_running||{};
    const bici=p.bici||{};
    const plan=p.plan_garmin||{};
    const nut=p.nutricion||{};
    const objetivos=p.objetivos||[];
    const rutas=p.rutas||[];
    const peso=w.current_kg||a.peso_actual_kg||89.1;
    const pesoObj=a.peso_objetivo_kg||80;
    const pesoDiff=+(peso-pesoObj).toFixed(1);
    const effActual=p.eff_actual||p.eff_base||0.1483;
    const effObj=p.eff_obj||0.155;
    const effPct=Math.min(100,Math.round((effActual/effObj)*100));

    function zoneBar(label,lo,hi,col){
      return '<div style="display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid rgba(255,255,255,.07)">'+
        '<div style="width:8px;height:8px;border-radius:50%;background:'+col+';flex-shrink:0"></div>'+
        '<div style="flex:1;font-size:12px;color:var(--muted)">'+label+'</div>'+
        '<div style="font-size:13px;font-weight:800;color:'+col+'">'+lo+'–'+hi+' bpm</div></div>';
    }

    // Resumen Strava para Perfil
    const syYears=(sy.years_active||[]);
    const stravaPerfilCard=sy.total_activities?`
      <div class="card" onclick="go('activities')" style="cursor:pointer;border-color:rgba(34,197,94,.25)">
        <div class="head"><h3 style="color:#22c55e">Strava</h3><span>${bf.pct_streams||0}% streams</span></div>
        <div class="grid2" style="margin-bottom:0">
          ${metric((sy.total_activities||0).toLocaleString(),'activities',syYears[0]+(syYears.length>1?' → '+syYears[syYears.length-1]:''))}
          ${metric((sy.total_km||0).toLocaleString(),'km totales',(Math.round((sy.total_hours||0))).toLocaleString()+' h moving')}
        </div>
      </div>`:'';

    // Stream quality card (v6.2)
    function streamBar(pct,col){return `<div style="display:flex;align-items:center;gap:8px"><div style="flex:1;height:5px;background:rgba(255,255,255,.07);border-radius:3px;overflow:hidden"><div style="height:100%;width:${Math.round(pct*100)}%;background:${col};border-radius:3px"></div></div><div style="font-size:11px;font-weight:800;color:${col};width:36px;text-align:right">${Math.round(pct*100)}%</div></div>`;}
    const sqCard=sq.total_activities?`<div class="card" style="margin-bottom:4px;border-color:rgba(74,158,255,.2)">
      <div class="head"><h3>Data quality</h3><span style="color:#4a9eff">${Math.round((sq.avg_stream_quality||0)*100)}% general</span></div>
      <div style="display:grid;gap:8px">
        <div><div style="display:flex;justify-content:space-between;margin-bottom:3px"><span style="font-size:11px;color:var(--muted)">FC (HR)</span></div>${streamBar(sq.avg_hr_coverage||0,'#e8593c')}</div>
        <div><div style="display:flex;justify-content:space-between;margin-bottom:3px"><span style="font-size:11px;color:var(--muted)">GPS</span></div>${streamBar(sq.avg_gps_coverage||0,'#3dd68c')}</div>
        <div><div style="display:flex;justify-content:space-between;margin-bottom:3px"><span style="font-size:11px;color:var(--muted)">Cadencia</span></div>${streamBar(sq.avg_cadence_coverage||0,'#f59e0b')}</div>
        <div><div style="display:flex;justify-content:space-between;margin-bottom:3px"><span style="font-size:11px;color:var(--muted)">Potencia (POW)</span></div>${streamBar(sq.avg_power_coverage||0,'#a78bfa')}</div>
      </div>
      <div style="font-size:10px;color:var(--muted);margin-top:8px">${(sq.total_activities||0).toLocaleString()} actividades analizadas</div>
    </div>`:'';

    // V8 UI — What does Epoch know about me? (resumen humano primero)
    const _resumen='<div style="background:linear-gradient(135deg,rgba(61,214,140,.12),rgba(61,214,140,.03));border:1px solid rgba(61,214,140,.3);border-radius:18px;padding:16px 18px;margin-bottom:12px">'+
      '<div style="font-size:10px;font-weight:900;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin-bottom:6px">What does Epoch know about me?</div>'+
      '<div style="font-size:13px;line-height:1.6">You weigh <b>'+peso+' kg</b> (target '+pesoObj+'). You follow <b>'+esc(plan.nombre||'Garmin Coach')+'</b> in the '+esc(plan.fase||'Base')+' phase. Your bike Z2 is '+(zc.z2?zc.z2[0]+'–'+zc.z2[1]:'134–150')+' bpm and you ride the '+esc(bici.nombre||'Rarotonga')+'.</div>'+
    '</div>';
    const _peso=`
      <!-- PESO -->
      <div class="card">
        <div class="head"><h3>Weight</h3><span style="color:#3dd68c">${peso} kg</span></div>
        <div class="pbar"><div class="pfill" style="width:${Math.max(0,100-Math.round((pesoDiff/20)*100))}%;background:#3dd68c"></div></div>
        <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--muted);margin-top:4px">
          <span>Current: ${peso} kg</span>
          <span>Target: ${pesoObj} kg · ${Math.max(0,pesoDiff)} kg to go</span>
        </div>
      </div>

`;
    const _zonas=`
      <!-- ZONAS CICLISMO -->
      <div class="card">
        <div class="head"><h3>Cycling HR zones</h3><span style="color:var(--theme)">LT ${zc.lt_bpm||168} bpm</span></div>
        ${zoneBar('Z1 Recovery',0,zc.z1?zc.z1[1]:109,'#8b929f')}
        ${zoneBar('Z2 Aerobic',zc.z2?zc.z2[0]:109,zc.z2?zc.z2[1]:134,'#3dd68c')}
        ${zoneBar('Z3 Tempo',zc.z3?zc.z3[0]:134,zc.z3?zc.z3[1]:150,'#f59e0b')}
        ${zoneBar('Z4 Threshold',zc.z4?zc.z4[0]:150,zc.z4?zc.z4[1]:160,'#e8593c')}
        ${zoneBar('Z5 Max',zc.z5?zc.z5[0]:160,zc.z5?zc.z5[1]:168,'#c026d3')}
        <div style="font-size:11px;color:var(--muted);margin-top:8px">Max HR: ${zc.max_hr||196} bpm · based on % of LT</div>
      </div>

      <!-- ZONAS RUNNING -->
      <div class="card">
        <div class="head"><h3>Running HR zones</h3><span style="color:var(--theme)">LT ${zr.lt_bpm||173} bpm</span></div>
        ${zoneBar('Z1 Recovery',0,zr.z1?zr.z1[1]:112,'#8b929f')}
        ${zoneBar('Z2 Aerobic',zr.z2?zr.z2[0]:112,zr.z2?zr.z2[1]:138,'#3dd68c')}
        ${zoneBar('Z3 Tempo',zr.z3?zr.z3[0]:138,zr.z3?zr.z3[1]:154,'#f59e0b')}
        ${zoneBar('Z4 Threshold',zr.z4?zr.z4[0]:154,zr.z4?zr.z4[1]:164,'#e8593c')}
        ${zoneBar('Z5 Max',zr.z5?zr.z5[0]:164,zr.z5?zr.z5[1]:173,'#c026d3')}
        <div style="font-size:11px;color:var(--muted);margin-top:8px">Max HR: ${zr.max_hr||194} bpm</div>
      </div>

`;
    const _equipo=`
      <!-- BICI + GEAR -->
      <div class="card">
        <div class="head"><h3>Current bike</h3><span style="color:#f59e0b">${esc(bici.nombre||'Rarotonga')}</span></div>
        <div class="row"><div class="r-main"><div class="r-title">${bici.marca||'Orbea Avant Aluminio 2019'}</div><div class="r-sub">${bici.llantas||'Vittoria Corsa N.EXT 700C x26'}</div><div class="r-sub">First ride: ${fmtDate(bici.primer_uso)}</div></div><div class="r-val" style="color:#f59e0b">${bici.km||716.6} km</div></div>
      </div>

`;
    const _plan=`
      <!-- PLAN GARMIN -->
      <div class="card">
        <div class="head"><h3>Garmin Plan</h3><span style="color:#4a9eff">${esc(plan.nombre||'Garmin Coach')}</span></div>
        <div style="padding:8px 0">
          <div style="font-size:13px;font-weight:800;margin-bottom:4px">${esc(plan.fase||'Aerobic base')}</div>
          <div style="font-size:12px;color:var(--muted)">${esc(plan.desc||'')}</div>
        </div>
        <div style="font-size:12px;color:#4a9eff;font-weight:700">Target cadence: ${p.cadencia_obj||100} rpm</div>
        <button class="btn btn2" style="font-size:11px;padding:8px;margin-top:10px" onclick="deactivatePlan()">Retire this plan</button>
        <div style="font-size:10px;color:var(--muted);margin-top:4px">History is preserved: sessions, matches and compliance stay on record.</div>
      </div>

`;
    const _detalle=`
      <!-- EFICIENCIA -->
      <div class="card">
        <div class="head"><h3>Aerobic efficiency</h3><span style="color:#3dd68c">${effPct}%</span></div>
        <div class="pbar"><div class="pfill" style="width:${effPct}%;background:#3dd68c"></div></div>
        <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--muted);margin-top:4px">
          <span>Current: ${effActual.toFixed(4)}</span><span>Base: ${(p.eff_base||0.1483).toFixed(4)}</span><span>Target: ${(p.eff_obj||0.155).toFixed(3)}+</span>
        </div>
      </div>

      <!-- RUTAS -->
      <div class="card">
        <div class="head"><h3>Reference routes</h3></div>
        ${rutas.map(r=>'<div class="row"><div class="r-main"><div class="r-title">'+esc(r.nombre)+'</div><div class="r-sub">'+esc(r.desc)+'</div></div><div class="r-val" style="color:var(--theme)">'+r.km+' km</div></div>').join('')||'<div style="color:var(--muted);font-size:12px">No routes defined</div>'}
      </div>

      <!-- NUTRICION -->
      <div class="card">
        <div class="head"><h3>Gel strategy</h3><span style="color:#f59e0b">homemade</span></div>
        <div style="font-size:13px;margin-bottom:8px">${esc(nut.gel||'60% apple juice + 40% agave + sal')}</div>
        <div class="grid2">
          ${metric('Carbs',nut.carbos_g||40,'g / gel')}
          ${metric('Water',nut.agua_ml_h||500,'ml/h')}
        </div>
        <div style="font-size:11px;color:var(--muted)">${esc(nut.timing||'Every 45-60 min')}</div>
      </div>

      <!-- OBJETIVOS -->
      <div class="card">
        <div class="head"><h3>Notes & goals</h3></div>
        ${objetivos.map((o,i)=>'<div style="display:flex;gap:10px;align-items:center;padding:7px 0;border-bottom:1px solid rgba(255,255,255,.07)"><div style="width:20px;height:20px;border-radius:50%;background:rgba(61,214,140,.15);border:1px solid rgba(61,214,140,.3);display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:900;color:#3dd68c;flex-shrink:0">'+(i+1)+'</div><div style="font-size:13px;flex:1">'+(o.o||o.objetivo||o)+'</div></div>').join('')}
      </div>
    `;
    // Bloque A — llave de acceso: protege todas las escrituras (X-Epoch-Key)
    const _hasKey=!!localStorage.getItem('epoch_key');
    const _keyCard='<div class="card" style="border:1px solid rgba(245,158,11,.25)">'+
      '<div class="head"><h3>Access key</h3><span style="color:'+(_hasKey?'#3dd68c':'#f59e0b')+'">'+(_hasKey?'set ✓':'not set')+'</span></div>'+
      '<div style="font-size:11px;color:var(--muted);line-height:1.5;margin-bottom:8px">Protects every write (logs, deletes, syncs). Stored only on this device — never in the code.</div>'+
      '<div style="display:flex;gap:8px"><input type="password" id="pk-key" placeholder="Paste your key" style="flex:1;padding:9px;border-radius:9px;background:var(--surface);color:var(--text);border:1px solid rgba(255,255,255,.12)">'+
      '<button class="btn" style="margin:0;width:auto;padding:9px 16px" onclick="saveEpochKey()">Save</button></div>'+
    '</div>';
    el.innerHTML=_resumen+_peso+_plan+
      _keyCard+
      evd('Heart-rate zones',_zonas)+
      evd('Gear, data & detail',stravaPerfilCard+sqCard+_equipo+_detalle)+
      '<div style="text-align:center;padding:14px 0"><a href="/legal" style="font-size:11px;color:var(--muted);text-decoration:underline">Legal · Privacy notice & disclaimer (Spanish)</a></div>';
  }catch(e){el.innerHTML='<div class="card" style="color:var(--muted)">'+esc(e.message)+'</div>'}
}

async function rebuildSnapshots(){
  const btn=event.target;
  btn.disabled=true;btn.textContent='⏳ Calculating...';
  try{
    const r=await fetch(API+'/gpt/rebuild-snapshots',{method:'POST'});
    const d=await r.json();
    if(d.ok){
      toast(`✅ ${d.count} weeks calculated (${d.first_week} → ${d.last_week})`);
    } else {
      toast('⚠️ '+d.message);
    }
  }catch(e){toast('Recalculation failed');}
  finally{btn.disabled=false;btn.textContent='⚙️ Recalcular capacidades (snapshots)';}
}

async function loadStravaStatus(){
  try{
    const r=await fetch(API+'/api/strava/transform-status');
    if(!r.ok)throw new Error('sin datos');
    const d=await r.json();
    const el=document.getElementById('strava-status-body');
    const pct=document.getElementById('strava-pct');
    if(!el)return;
    pct.textContent=d.pct_done+'% done';
    const barColor=d.errors>0?'#ef4444':d.pending===0?'#22c55e':'var(--theme)';
    el.innerHTML=`
      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:12px;text-align:center">
        <div><div style="font-size:22px;font-weight:950;color:#22c55e">${d.done||0}</div><div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.07em">Done</div></div>
        <div><div style="font-size:22px;font-weight:950;color:var(--theme)">${d.pending||0}</div><div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.07em">Pending</div></div>
        <div><div style="font-size:22px;font-weight:950;color:${d.errors>0?'#ef4444':'var(--muted)'}">${d.errors||0}</div><div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.07em">Errors</div></div>
      </div>
      <div style="height:6px;border-radius:4px;background:#303642;overflow:hidden;margin-bottom:12px">
        <div style="height:100%;width:${d.pct_done}%;background:${barColor};border-radius:4px;transition:width .4s"></div>
      </div>
      ${d.pending>0
        ? `<button class="btn" onclick="triggerStravaTransform()" id="btn-transform">🔄 Transform ${d.pending} activities → Epoch</button>`
        : d.done>0
          ? `<div style="text-align:center;font-size:13px;color:#22c55e;padding:8px 0">✅ All synced — ${d.done} sessions in Epoch</div>`
          : `<div style="text-align:center;font-size:12px;color:var(--muted);padding:4px 0 8px">Staging empty — backfill did not complete</div>
             <button class="btn" onclick="triggerStravaBackfill()" id="btn-backfill">📥 Load history from Strava</button>`
      }
      ${d.errors>0?`<div style="margin-top:8px;font-size:11px;color:#ef4444;text-align:center">⚠️ ${d.errors} con error — se pueden re-intentar</div>`:''}
      <div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--line)">
        <button class="btn btn2" style="font-size:12px;padding:10px" onclick="rebuildSnapshots()">⚙️ Recalculate capacities (snapshots)</button>
      </div>
    `;
  }catch(e){
    const el=document.getElementById('strava-status-body');
    if(el)el.innerHTML='<div style="color:var(--muted);font-size:12px">Pipeline no disponible</div>';
  }
}

async function triggerStravaTransform(){
  const statusBody=document.getElementById('strava-status-body');
  let totalTransformed=0;
  let round=1;
  const maxRounds=40; // máx 40×500 = 20,000 actividades

  async function transformNextBatch(){
    if(statusBody)statusBody.innerHTML=`
      <div style="text-align:center;padding:12px 0">
        <span class="spin"></span>
        <div style="margin-top:8px;font-size:13px;color:var(--muted)">Transforming batch ${round}… ${totalTransformed} sessions processed</div>
      </div>`;

    try{
      const r=await fetch(API+'/api/strava/transform?batches=5',{method:'POST'});
      const d=await r.json();
      if(d.status==='error'){toast('Error: '+d.message);await loadStravaStatus();return;}

      totalTransformed+=d.transformed||0;

      if((d.transformed||0)===0){
        toast('✅ Transform complete — '+totalTransformed+' sessions in Epoch');
        await loadStravaStatus();
        return;
      }

      round++;
      if(round>maxRounds){toast('Transform completo — '+totalTransformed+' sessions');await loadStravaStatus();return;}
      await loadStravaStatus();
      setTimeout(transformNextBatch,300);
    }catch(e){toast('Network error — reintenta');}
  }

  await transformNextBatch();
}

async function triggerStravaBackfill(){
  const btn=document.getElementById('btn-backfill');
  const statusBody=document.getElementById('strava-status-body');
  if(btn){btn.disabled=true;}

  // Llamadas encadenadas de 5 páginas (1,000 actividades c/u)
  // La Strava API devuelve en orden cronológico inverso (más recientes primero)
  let totalIngested=0;
  let page=1;
  const maxCalls=20; // max 20,000 actividades

  async function syncNextBatch(){
    if(statusBody)statusBody.innerHTML=`
      <div style="text-align:center;padding:12px 0">
        <span class="spin"></span>
        <div style="margin-top:8px;font-size:13px;color:var(--muted)">Loading page ${page}… ${totalIngested} activities so far</div>
      </div>`;

    try{
      const r=await fetch(API+'/api/strava/sync-now?pages=5&force='+(page===1?'true':'false'),{method:'POST'});
      const d=await r.json();

      if(d.status==='rate_limited'){
        const el=document.getElementById('strava-status-body');
        if(el)el.innerHTML=`<div style="padding:12px 4px">
          <div style="font-size:13px;color:#f87171;font-weight:800;margin-bottom:6px">⛔ Strava rate limit exhausted</div>
          <div style="font-size:12px;color:var(--muted);line-height:1.5;margin-bottom:12px">
            Too many attempts today. Strava allows 2,000 requests/day.<br>
            Try tomorrow morning — the limit resets at midnight UTC.
          </div>
          <button class="btn btn2" onclick="loadStravaStatus()" style="font-size:12px;padding:10px">Check if it cleared</button>
        </div>`;
        return;
      }
      if(d.status==='error'){toast('Error: '+d.message);await loadStravaStatus();return;}

      totalIngested+=d.ingested||0;

      if((d.ingested||0)===0&&(d.duplicates||0)>0){
        // Solo duplicados = ya llegamos al final del historial
        toast('✅ History complete — '+totalIngested+' activities loaded');
        await loadStravaStatus();
        // Auto-transformar si hay pendientes
        setTimeout(()=>{
          const t=document.getElementById('btn-transform');
          if(t)t.click();
        },1000);
        return;
      }

      if((d.ingested||0)+(d.duplicates||0)===0){
        // Sin actividades = terminamos
        toast('✅ History loaded — '+totalIngested+' activities');
        await loadStravaStatus();
        return;
      }

      page++;
      if(page>maxCalls){toast('Load complete — '+totalIngested+' activities');await loadStravaStatus();return;}

      // Actualizar status y continuar
      await loadStravaStatus();
      setTimeout(syncNextBatch,500); // pequeña pausa entre llamadas

    }catch(e){
      toast('Network error — reintenta');
      await loadStravaStatus();
    }
  }

  toast('📥 Loading Strava history…');
  syncNextBatch();
}


async function deactivatePlan(){
  const reasons={1:'lesion',2:'nuevo_objetivo',3:'ya_no_quiero',4:'otro'};
  const pick=prompt('Why are you retiring the plan?\n1 = Injury\n2 = New goal\n3 = No longer want to follow it\n4 = Other reason\n\nType the number (or cancel):');
  if(!pick)return;
  const reason=reasons[pick.trim()]||'otro';
  if(!confirm('The plan will move to history (nothing is deleted: sessions, matches and compliance are preserved). Confirm?'))return;
  try{
    // Obtener plan activo para conocer su id
    const tc=await fetch(API+'/gpt/training-context').then(r=>r.json());
    const pid=tc&&tc.plan&&tc.plan.plan_id;
    if(!pid){toast('No active structured plan');return;}
    const d=await fetch(API+'/api/training-plan/'+encodeURIComponent(pid)+'/deactivate?reason='+encodeURIComponent(reason),{method:'POST'}).then(r=>r.json());
    toast(d.ok?'Plan retired — history preserved':'Error: '+(d.message||''));
    loadPerfil();
  }catch(e){toast('Network error');}
}

// Bloque A — guardar la llave de acceso en este dispositivo
function saveEpochKey(){
  const v=(document.getElementById('pk-key').value||'').trim();
  if(!v){localStorage.removeItem('epoch_key');toast('Key cleared');loadPerfil();return;}
  localStorage.setItem('epoch_key',v);
  toast('Key saved on this device');
  loadPerfil();
}
