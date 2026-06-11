// ── E26B GOAL REGISTRY ──────────────────────────────────
async function loadMetas(){
  const el=document.getElementById('metas-data');
  el.innerHTML='<div class="loading"><span class="spin"></span>Loading goals...</div>';
  try{
    const r=await fetch(API+'/gpt/goals');
    if(!r.ok)throw new Error('Could not load goals');
    const d=await r.json();
    const goals=d.goals||[];
    const typeIcon={cycling:'🚴',running:'🏃',climbing:'⛰️',gravel:'🚵',other:'🏅'};
    const priLabel={1:'Primary',2:'Secondary',3:'Background'};
    // Readiness: map goal type → event key
    const typeToEvento={cycling:'gran_fondo_150',gravel:'gran_fondo_150',running:'medio_maraton',climbing:'escalera_al_infierno',other:'gran_fondo_150'};
    const readinessStatusColor=s=>({listo:'#3dd68c',bien_encaminado:'#22d3ee',forma_en_desarrollo:'#f59e0b',base_en_construccion:'#e8593c',inicio:'#8b929f'}[s]||'#8b929f');
    const readinessStatusLabel=s=>({listo:'Ready',bien_encaminado:'On track',forma_en_desarrollo:'Developing',base_en_construccion:'Building base',inicio:'Starting out'}[s]||s);
    // Fetch readiness for first active goal (or default escalada)
    let readinessCard='';
    try{
      const topGoal=goals[0];
      const eventoKey=topGoal?typeToEvento[topGoal.event_type]||'gran_fondo_150':'escalera_al_infierno';
      const rr=await fetch(API+'/gpt/readiness?evento='+eventoKey).then(r=>r.ok?r.json():{}).catch(()=>({}));
      if(rr.ok){
        const rCol=readinessStatusColor(rr.status);
        const rLabel=readinessStatusLabel(rr.status);
        const pct=v=>Math.max(0,Math.min(100,Number(v||0)));
        const limitante=(rr.components||[]).filter(c=>c.score!=null).sort((a,b)=>a.weighted_contribution-b.weighted_contribution)[0];
        const limitanteNombre=limitante?limitante.nombre:'—';
        const gaps=rr.data_gaps&&rr.data_gaps.length?'<div style="font-size:10px;color:var(--muted);margin-top:6px">Sin datos: '+rr.data_gaps.join(', ')+'</div>':'';
        readinessCard=`<div class="card" style="border-left:3px solid ${rCol};margin-bottom:12px">
          <div class="head"><h3>Current readiness</h3><span style="color:${rCol}">${rLabel}</span></div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px">
            <div style="text-align:center;padding:10px;background:rgba(255,255,255,.04);border-radius:10px">
              <div style="font-size:28px;font-weight:950;color:${rCol}">${Number(rr.readiness_score).toFixed(0)}</div>
              <div style="font-size:10px;color:var(--muted)">of 100 · ${rr.event||eventoKey}</div>
            </div>
            <div style="text-align:center;padding:10px;background:rgba(255,255,255,.04);border-radius:10px">
              <div style="font-size:18px;font-weight:800;color:#f59e0b">${limitanteNombre}</div>
              <div style="font-size:10px;color:var(--muted)">limiting capacity</div>
            </div>
          </div>
          <div style="height:8px;background:rgba(255,255,255,.07);border-radius:4px;margin-bottom:6px"><div style="height:100%;width:${pct(rr.readiness_score)}%;background:${rCol};border-radius:4px"></div></div>
          ${(rr.components||[]).filter(c=>c.score!=null).map(c=>{
            const barCol=c.score>=75?'#3dd68c':c.score>=50?'#f59e0b':'#e8593c';
            return `<div style="display:grid;grid-template-columns:minmax(0,1fr) 36px;gap:6px;align-items:center;padding:4px 0;border-bottom:1px solid var(--line)">
              <div><div style="font-size:11px;margin-bottom:2px">${c.nombre}</div><div style="height:3px;background:rgba(255,255,255,.07);border-radius:2px"><div style="height:100%;width:${pct(c.score)}%;background:${barCol}"></div></div></div>
              <div style="text-align:right;font-size:12px;font-weight:900;color:${barCol}">${Number(c.score).toFixed(0)}</div></div>`;
          }).join('')}
          ${gaps}
          <div style="font-size:10px;color:var(--muted);margin-top:8px">Confidence ${Math.round(Number(rr.confidence||0)*100)}% · Add a goal below for personalized coaching</div>
        </div>`;
      }
    }catch(e2){}
    let rows='';
    if(goals.length===0){
      rows='<div class="loading">Sin metas activas. Agrega tu primera meta abajo.</div>';
    } else {
      goals.forEach(g=>{
        const ico=typeIcon[g.event_type]||'🏅';
        const fecha=g.event_date?new Date(g.event_date+'T12:00:00').toLocaleDateString('en-US',{day:'numeric',month:'short',year:'numeric'}):'No date';
        const meta=[g.distance_km?g.distance_km+'km':'',g.elevation_m?g.elevation_m+'m↑':''].filter(Boolean).join(' · ');
        rows+=`<div class="row">
          <div class="r-ico">${ico}</div>
          <div class="r-main">
            <div class="r-title">${g.event_name}</div>
            <div class="r-sub">${fecha}${meta?' · '+meta:''} · ${priLabel[g.priority]||'Goal'}</div>
            ${g.notes?`<div class="r-sub" style="margin-top:3px;font-style:italic">${g.notes}</div>`:''}
          </div>
          <div style="display:flex;gap:6px;flex-shrink:0">
            <button onclick="completeGoal(${g.id})" style="background:none;border:none;font-size:20px;cursor:pointer" title="Completed">✅</button>
            <button onclick="deleteGoal(${g.id})" style="background:none;border:none;font-size:20px;cursor:pointer" title="Delete">🗑️</button>
          </div>
        </div>`;
      });
    }
    const form=`<div class="card" style="margin-top:14px">
      <div class="head"><h3>New goal</h3><span style="font-size:10px;color:var(--muted)">ADR-011</span></div>
      <div class="field"><label>Event</label><input id="g-name" placeholder="Gran Fondo, CDMX Marathon..."></div>
      <div class="grid2">
        <div class="field"><label>Type</label>
          <select id="g-type"><option value="cycling">Cycling</option><option value="gravel">Gravel</option><option value="running">Running</option><option value="climbing">Climbing</option><option value="other">Other</option></select>
        </div>
        <div class="field"><label>Priority</label>
          <select id="g-priority"><option value="1">Primary</option><option value="2">Secondary</option><option value="3">Background</option></select>
        </div>
      </div>
      <div class="grid2">
        <div class="field"><label>Target date</label><input type="date" id="g-date"></div>
        <div class="field"><label>Distance km</label><input type="number" id="g-dist" placeholder="200"></div>
      </div>
      <div class="grid2">
        <div class="field"><label>Elevation m</label><input type="number" id="g-elev" placeholder="3500"></div>
        <div class="field"><label>Notes</label><input id="g-notes" placeholder="Optional"></div>
      </div>
      <button class="btn" onclick="saveGoal()">Register goal</button>
    </div>`;
    el.innerHTML=readinessCard+`<div class="card">${rows}</div>`+form;
  }catch(e){el.innerHTML='<div class="loading">'+e.message+'</div>';}
}

async function saveGoal(){
  const name=(document.getElementById('g-name').value||'').trim();
  if(!name){toast('Enter the event name');return;}
  const payload={
    event_name:name,
    event_type:document.getElementById('g-type').value,
    priority:parseInt(document.getElementById('g-priority').value),
    event_date:document.getElementById('g-date').value||null,
    distance_km:parseFloat(document.getElementById('g-dist').value)||null,
    elevation_m:parseFloat(document.getElementById('g-elev').value)||null,
    notes:document.getElementById('g-notes').value||null,
  };
  const r=await fetch(API+'/gpt/goals',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  if(r.ok){toast('Goal registered ✅');loadMetas();}else{toast('Save failed');}
}

async function completeGoal(id){
  await fetch(API+'/gpt/goals/'+id,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:'completed'})});
  toast('Goal completed 🏆');loadMetas();
}

async function deleteGoal(id){
  if(!confirm('Delete this goal?'))return;
  await fetch(API+'/gpt/goals/'+id,{method:'DELETE'});
  toast('Goal deleted');loadMetas();
}
