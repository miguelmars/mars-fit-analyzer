"""
FIT Analyzer API — Mars Edition v3.0
=====================================
Endpoints:
  GET  /                     → página web para subir desde el celular
  POST /analyze-fit          → procesa archivo, guarda resultado, devuelve session_id + JSON
  GET  /result/{session_id}  → el GPT consulta el resultado por ID
  GET  /sessions             → lista sesiones en memoria (debug)
"""

from fastapi import FastAPI, UploadFile, File, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
import tempfile, os, zipfile, math, statistics, uuid
from datetime import datetime, timezone

try:
    import fitparse
except ImportError:
    raise RuntimeError("Instala fitparse: pip install fitparse")

app = FastAPI(title="FIT Analyzer API — Mars Edition", version="3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SEMICIRCLES_TO_DEG = 180 / 2**31
RESULTS_STORE: dict = {}

MARS_ZONES = [
    {"zone": 1, "name": "Z1 Recuperación", "bpm_low": 0,   "bpm_high": 108},
    {"zone": 2, "name": "Z2 Aeróbico",     "bpm_low": 134, "bpm_high": 150},
    {"zone": 3, "name": "Z3 Tempo",        "bpm_low": 150, "bpm_high": 160},
    {"zone": 4, "name": "Z4 Umbral",       "bpm_low": 160, "bpm_high": 168},
    {"zone": 5, "name": "Z5 Máximo",       "bpm_low": 169, "bpm_high": 999},
]

HTML_PAGE = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>FIT Uploader — Mars</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=DM+Sans:wght@300;400;500&display=swap');
  :root{--bg:#0f0f0f;--surface:#1a1a1a;--border:#2a2a2a;--accent:#e8593c;--accent2:#f2a623;--text:#e8e6e0;--muted:#6b6b6b;--success:#3dd68c;--mono:'DM Mono',monospace;--sans:'DM Sans',sans-serif}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:var(--sans);min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:24px 20px}
  .container{width:100%;max-width:420px}
  .header{margin-bottom:36px;text-align:center}
  .logo{font-family:var(--mono);font-size:11px;letter-spacing:.2em;color:var(--muted);text-transform:uppercase;margin-bottom:12px}
  .title{font-size:28px;font-weight:300;letter-spacing:-.02em;line-height:1.2}
  .title span{color:var(--accent);font-weight:500}
  .drop-zone{border:1.5px dashed var(--border);border-radius:16px;padding:48px 24px;text-align:center;cursor:pointer;transition:border-color .2s,background .2s;position:relative;background:var(--surface)}
  .drop-zone:hover,.drop-zone.dragover{border-color:var(--accent);background:#1f1a18}
  .drop-zone input[type=file]{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%}
  .drop-icon{width:48px;height:48px;margin:0 auto 16px;border-radius:12px;background:#2a1f1a;display:flex;align-items:center;justify-content:center}
  .drop-icon svg{width:24px;height:24px;stroke:var(--accent);fill:none;stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round}
  .drop-label{font-size:15px;color:var(--text);margin-bottom:6px}
  .drop-hint{font-size:12px;color:var(--muted);font-family:var(--mono)}
  .file-selected{margin-top:20px;padding:14px 16px;background:#1f1f1f;border-radius:10px;border:1px solid var(--border);display:none;align-items:center;gap:12px}
  .file-selected.show{display:flex}
  .file-icon{width:36px;height:36px;background:#2a1f1a;border-radius:8px;display:flex;align-items:center;justify-content:center;flex-shrink:0}
  .file-icon svg{width:18px;height:18px;stroke:var(--accent2);fill:none;stroke-width:1.5;stroke-linecap:round}
  .file-info{flex:1;min-width:0}
  .file-name{font-family:var(--mono);font-size:12px;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .file-size{font-size:11px;color:var(--muted);margin-top:2px}
  .btn-upload{width:100%;margin-top:16px;padding:16px;background:var(--accent);color:#fff;border:none;border-radius:12px;font-family:var(--sans);font-size:15px;font-weight:500;cursor:pointer;transition:opacity .2s,transform .1s;display:none;letter-spacing:.01em}
  .btn-upload.show{display:block}
  .btn-upload:active{transform:scale(.98);opacity:.9}
  .btn-upload:disabled{opacity:.4;cursor:not-allowed}
  .progress-wrap{margin-top:16px;display:none}
  .progress-wrap.show{display:block}
  .progress-bar-bg{height:3px;background:var(--border);border-radius:2px;overflow:hidden;margin-bottom:10px}
  .progress-bar-fill{height:100%;background:linear-gradient(90deg,var(--accent),var(--accent2));border-radius:2px;width:0%;transition:width .3s}
  .progress-label{font-family:var(--mono);font-size:11px;color:var(--muted);text-align:center}
  .result-card{margin-top:20px;padding:24px;background:#111f17;border:1px solid #1e3d2a;border-radius:16px;display:none}
  .result-card.show{display:block}
  .result-label{font-family:var(--mono);font-size:10px;letter-spacing:.15em;text-transform:uppercase;color:var(--success);margin-bottom:12px}
  .session-id-box{background:#0d1a12;border:1px solid #1e3d2a;border-radius:10px;padding:16px;display:flex;align-items:center;gap:12px;cursor:pointer;transition:border-color .2s}
  .session-id-box:hover{border-color:var(--success)}
  .session-id-value{font-family:var(--mono);font-size:22px;font-weight:500;color:var(--success);letter-spacing:.08em;flex:1}
  .copy-btn{padding:8px 14px;background:#1e3d2a;border:none;border-radius:8px;font-family:var(--mono);font-size:11px;color:var(--success);cursor:pointer;transition:background .2s;white-space:nowrap}
  .copy-btn:hover{background:#2a5a3a}
  .copy-btn.copied{color:#fff;background:var(--success)}
  .result-meta{margin-top:16px;display:grid;grid-template-columns:1fr 1fr;gap:10px}
  .meta-item{background:#0d1a12;border-radius:8px;padding:10px 12px}
  .meta-key{font-family:var(--mono);font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.1em;margin-bottom:4px}
  .meta-val{font-size:14px;font-weight:500;color:var(--text)}
  .gpt-hint{margin-top:16px;padding:12px 14px;background:#1a1a1a;border-radius:10px;border-left:3px solid var(--accent2)}
  .gpt-hint p{font-size:12px;color:var(--muted);line-height:1.5}
  .gpt-hint code{font-family:var(--mono);color:var(--accent2);font-size:12px}
  .error-card{margin-top:16px;padding:16px;background:#1f1212;border:1px solid #3d1e1e;border-radius:12px;display:none}
  .error-card.show{display:block}
  .error-card p{font-family:var(--mono);font-size:12px;color:#f07070;line-height:1.5}
  .reset-btn{margin-top:20px;width:100%;padding:12px;background:transparent;border:1px solid var(--border);border-radius:10px;color:var(--muted);font-family:var(--sans);font-size:13px;cursor:pointer;transition:border-color .2s,color .2s;display:none}
  .reset-btn.show{display:block}
  .reset-btn:hover{border-color:var(--text);color:var(--text)}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div class="logo">Mars Fit Analyzer</div>
    <h1 class="title">Sube tu<br><span>entrenamiento</span></h1>
  </div>
  <div class="drop-zone" id="dropZone">
    <input type="file" id="fileInput" accept=".zip,.fit"/>
    <div class="drop-icon"><svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg></div>
    <p class="drop-label">Toca para seleccionar archivo</p>
    <p class="drop-hint">.zip o .fit de Garmin Connect</p>
  </div>
  <div class="file-selected" id="fileSelected">
    <div class="file-icon"><svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/></svg></div>
    <div class="file-info">
      <div class="file-name" id="fileName">—</div>
      <div class="file-size" id="fileSize">—</div>
    </div>
  </div>
  <button class="btn-upload" id="btnUpload" onclick="uploadFile()">Analizar sesión →</button>
  <div class="progress-wrap" id="progressWrap">
    <div class="progress-bar-bg"><div class="progress-bar-fill" id="progressFill"></div></div>
    <p class="progress-label" id="progressLabel">Procesando archivo...</p>
  </div>
  <div class="result-card" id="resultCard">
    <div class="result-label">✓ Listo — copia este ID al GPT</div>
    <div class="session-id-box" onclick="copyId()">
      <div class="session-id-value" id="sessionIdValue">—</div>
      <button class="copy-btn" id="copyBtn">Copiar</button>
    </div>
    <div class="result-meta" id="resultMeta"></div>
    <div class="gpt-hint"><p>Pega esto al GPT:<br><code>Analiza mi sesión. session_id: <span id="hintId">—</span></code></p></div>
  </div>
  <div class="error-card" id="errorCard"><p id="errorMsg">Error desconocido</p></div>
  <button class="reset-btn" id="resetBtn" onclick="reset()">Subir otro archivo</button>
</div>
<script>
const API='';
let selectedFile=null;
const $=id=>document.getElementById(id);
$('fileInput').addEventListener('change',e=>{const f=e.target.files[0];if(f)selectFile(f);});
$('dropZone').addEventListener('dragover',e=>{e.preventDefault();$('dropZone').classList.add('dragover');});
$('dropZone').addEventListener('dragleave',()=>$('dropZone').classList.remove('dragover'));
$('dropZone').addEventListener('drop',e=>{e.preventDefault();$('dropZone').classList.remove('dragover');const f=e.dataTransfer.files[0];if(f)selectFile(f);});
function selectFile(f){selectedFile=f;$('fileName').textContent=f.name;$('fileSize').textContent=(f.size/1024).toFixed(1)+' KB';$('fileSelected').classList.add('show');$('btnUpload').classList.add('show');hide('resultCard');hide('errorCard');hide('resetBtn');}
async function uploadFile(){
  if(!selectedFile)return;
  $('btnUpload').disabled=true;show('progressWrap');
  $('progressFill').style.width='30%';$('progressLabel').textContent='Enviando...';
  const form=new FormData();form.append('file',selectedFile);
  try{
    $('progressFill').style.width='60%';$('progressLabel').textContent='Procesando .fit...';
    const res=await fetch(API+'/analyze-fit',{method:'POST',body:form});
    $('progressFill').style.width='90%';
    if(!res.ok){const err=await res.json().catch(()=>({detail:'Error del servidor'}));throw new Error(err.detail||'HTTP '+res.status);}
    const data=await res.json();
    $('progressFill').style.width='100%';$('progressLabel').textContent='¡Listo!';
    setTimeout(()=>{hide('progressWrap');showResult(data);},400);
  }catch(err){
    hide('progressWrap');$('btnUpload').disabled=false;
    $('errorMsg').textContent='Error: '+err.message;show('errorCard');show('resetBtn');
  }
}
function showResult(data){
  const sid=data.session_id,s=data.session||{};
  $('sessionIdValue').textContent=sid;$('hintId').textContent=sid;
  const meta=[{key:'Fecha',val:(s.start_time||'').slice(0,10)},{key:'Distancia',val:s.distance_km?s.distance_km+' km':'—'},{key:'Duración',val:s.duration_hms||'—'},{key:'FC prom.',val:s.avg_hr_bpm?s.avg_hr_bpm+' bpm':'—'}];
  $('resultMeta').innerHTML=meta.map(m=>`<div class="meta-item"><div class="meta-key">${m.key}</div><div class="meta-val">${m.val}</div></div>`).join('');
  show('resultCard');show('resetBtn');hide('btnUpload');
}
function copyId(){
  const sid=$('sessionIdValue').textContent;
  navigator.clipboard.writeText(sid).then(()=>{const b=$('copyBtn');b.textContent='✓ Copiado';b.classList.add('copied');setTimeout(()=>{b.textContent='Copiar';b.classList.remove('copied');},2000);});
}
function reset(){selectedFile=null;$('fileInput').value='';hide('fileSelected');hide('btnUpload');hide('resultCard');hide('errorCard');hide('resetBtn');hide('progressWrap');$('btnUpload').disabled=false;$('progressFill').style.width='0%';}
function show(id){$(id).classList.add('show');}
function hide(id){$(id).classList.remove('show');}
</script>
</body>
</html>"""


# ── helpers ──────────────────────────────────────────────────────────────────

def extract_fit_from_zip(zip_bytes: bytes) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
        f.write(zip_bytes); zpath = f.name
    try:
        with zipfile.ZipFile(zpath) as zf:
            fits = [n for n in zf.namelist() if n.lower().endswith(".fit")]
            if not fits:
                raise HTTPException(400, "El ZIP no contiene ningún .fit")
            return zf.read(fits[0])
    finally:
        os.unlink(zpath)

def percentile(values, p):
    values = sorted([v for v in values if v is not None])
    if not values: return None
    k = (len(values)-1)*(p/100); f = math.floor(k); c = math.ceil(k)
    if f == c: return values[int(k)]
    return values[f]*(c-k)+values[c]*(k-f)

def zone_for_hr(hr):
    if hr is None: return None
    if 109 <= hr <= 133: return 0
    for z in MARS_ZONES:
        if z["bpm_low"] <= hr <= z["bpm_high"]: return z["zone"]
    return None

def summarize_records(records):
    hrs  = [r["heart_rate_bpm"] for r in records if r.get("heart_rate_bpm") is not None]
    cads = [r["cadence_rpm"]    for r in records if r.get("cadence_rpm")    is not None]
    spds = [r["speed_kmh"]      for r in records if r.get("speed_kmh")      is not None]
    alts = [r["altitude_m"]     for r in records if r.get("altitude_m")     is not None]
    return {
        "records_count": len(records),
        "hr":      {"min":min(hrs) if hrs else None,"max":max(hrs) if hrs else None,"avg":round(statistics.mean(hrs),1) if hrs else None,"p90":round(percentile(hrs,90),1) if hrs else None},
        "cadence": {"min":min(cads) if cads else None,"max":max(cads) if cads else None,"avg":round(statistics.mean(cads),1) if cads else None,"p90":round(percentile(cads,90),1) if cads else None},
        "speed":   {"min_kmh":round(min(spds),1) if spds else None,"max_kmh":round(max(spds),1) if spds else None,"avg_kmh":round(statistics.mean(spds),1) if spds else None},
        "altitude":{"min_m":round(min(alts),1) if alts else None,"max_m":round(max(alts),1) if alts else None},
    }

def compute_zones(records):
    counts = {z["zone"]: 0 for z in MARS_ZONES}; gap_count = 0
    for rec in records:
        hr = rec.get("heart_rate_bpm")
        if hr is None: continue
        z = zone_for_hr(hr)
        if z == 0: gap_count += 1
        elif z is not None: counts[z] += 1
    total = sum(counts.values()) + gap_count or 1
    zones = []
    for z in MARS_ZONES:
        secs = counts[z["zone"]]
        zones.append({"zone":z["zone"],"name":z["name"],"bpm_low":z["bpm_low"],"bpm_high":None if z["bpm_high"]==999 else z["bpm_high"],"seconds":secs,"minutes":round(secs/60,1),"percent":round(secs/total*100,1)})
    zones.append({"zone":0,"name":"Entre Z1 y Z2 oficial","bpm_low":109,"bpm_high":133,"seconds":gap_count,"minutes":round(gap_count/60,1),"percent":round(gap_count/total*100,1)})
    return zones

def parse_fit(fit_bytes: bytes, include_records: bool = False) -> dict:
    with tempfile.NamedTemporaryFile(suffix=".fit", delete=False) as f:
        f.write(fit_bytes); fpath = f.name
    try:
        fit = fitparse.FitFile(fpath)
        sr = {}
        for msg in fit.get_messages("session"):
            for d in msg:
                if d.value is not None: sr[d.name] = d.value
        et = sr.get("total_elapsed_time",0) or 0
        session = {
            "start_time": str(sr.get("start_time","")),
            "duration_seconds": round(et),
            "duration_hms": f"{int(et//3600):02d}h {int((et%3600)//60):02d}m {int(et%60):02d}s",
            "distance_km": round((sr.get("total_distance",0) or 0)/1000,2),
            "calories_kcal": sr.get("total_calories"),
            "ascent_m": sr.get("total_ascent"), "descent_m": sr.get("total_descent"),
            "avg_hr_bpm": sr.get("avg_heart_rate"), "max_hr_bpm": sr.get("max_heart_rate"),
            "avg_speed_kmh": round((sr.get("avg_speed",0) or 0)*3.6,1),
            "max_speed_kmh": round((sr.get("max_speed",0) or 0)*3.6,1),
            "avg_cadence_rpm": sr.get("avg_cadence"), "max_cadence_rpm": sr.get("max_cadence"),
            "avg_temperature_c": sr.get("avg_temperature"), "max_temperature_c": sr.get("max_temperature"),
            "training_effect_aerobic": sr.get("total_training_effect"),
            "training_effect_anaerobic": sr.get("total_anaerobic_training_effect"),
            "sport": str(sr.get("sport","")), "sub_sport": str(sr.get("sub_sport","")),
        }
        laps = []
        for i, msg in enumerate(fit.get_messages("lap"),1):
            r = {d.name:d.value for d in msg if d.value is not None}
            t = r.get("total_elapsed_time",0) or 0
            laps.append({"lap":i,"duration_s":round(t),"duration_mmss":f"{int(t//60)}m{int(t%60):02d}s","distance_km":round((r.get("total_distance",0) or 0)/1000,2),"avg_hr_bpm":r.get("avg_heart_rate"),"max_hr_bpm":r.get("max_heart_rate"),"avg_speed_kmh":round((r.get("avg_speed",0) or 0)*3.6,1),"avg_cadence_rpm":r.get("avg_cadence"),"calories_kcal":r.get("total_calories")})
        records = []
        for msg in fit.get_messages("record"):
            rec = {d.name:d.value for d in msg if d.value is not None}
            lat = rec.get("position_lat"); lon = rec.get("position_long")
            spd = rec.get("speed", rec.get("enhanced_speed",0)) or 0
            records.append({"timestamp":str(rec.get("timestamp","")),"heart_rate_bpm":rec.get("heart_rate"),"speed_kmh":round(spd*3.6,2),"cadence_rpm":rec.get("cadence"),"altitude_m":rec.get("enhanced_altitude",rec.get("altitude")),"distance_m":round(rec.get("distance",0),1),"temperature_c":rec.get("temperature"),"lat":round(lat*SEMICIRCLES_TO_DEG,6) if lat else None,"lon":round(lon*SEMICIRCLES_TO_DEG,6) if lon else None})
        result = {
            "athlete": "Mars / Miguel Ángel Ramírez Sousa",
            "zone_model": "Zonas oficiales Mars por bpm",
            "zones_definition": MARS_ZONES,
            "session": session, "laps": laps,
            "zones": compute_zones(records),
            "record_summary": summarize_records(records),
            "analysis_guidance": {"use_as_primary_data":True,"do_not_invent":True,"notes":["Usar estos datos para analizar la sesión.","No asumir causa de picos de FC sin contexto del usuario.","Pedir al usuario sensación, ruta, tráfico y nutrición después del análisis."]}
        }
        if include_records: result["records"] = records
        return result
    finally:
        os.unlink(fpath)


# ── endpoints ────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def root():
    """Página web para subir archivos desde el celular."""
    return HTML_PAGE

@app.get("/api", include_in_schema=False)
def api_status():
    return {"status":"ok","service":"FIT Analyzer API — Mars Edition","version":"3.0","endpoints":{"upload":"POST /analyze-fit","query":"GET /result/{session_id}","sessions":"GET /sessions"}}

@app.post("/analyze-fit")
async def analyze_fit(
    file: UploadFile = File(...),
    include_records: bool = Query(False),
):
    content  = await file.read()
    filename = (file.filename or "").lower()
    fit_bytes = extract_fit_from_zip(content) if filename.endswith(".zip") else content
    result = parse_fit(fit_bytes, include_records=include_records)
    sid = str(uuid.uuid4())[:8]
    RESULTS_STORE[sid] = {"session_id":sid,"filename":file.filename,"uploaded_at":datetime.now(timezone.utc).isoformat(),"result":result}
    return {"session_id":sid,"message":f"Resultado guardado. Pasa el session_id '{sid}' al GPT para que lo analice.",**result}

@app.get("/result/{session_id}")
def get_result(session_id: str):
    entry = RESULTS_STORE.get(session_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"No se encontró resultado para session_id '{session_id}'. Puede haber expirado si el servidor reinició. Vuelve a subir el archivo.")
    return entry["result"]

@app.get("/sessions")
def list_sessions():
    return [{"session_id":k,"filename":v["filename"],"uploaded_at":v["uploaded_at"],"distance_km":v["result"]["session"].get("distance_km"),"start_time":v["result"]["session"].get("start_time")} for k,v in RESULTS_STORE.items()]
