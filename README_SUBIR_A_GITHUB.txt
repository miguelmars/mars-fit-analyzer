CARPETA LISTA PARA SUBIR A GITHUB / RAILWAY

Sube el CONTENIDO de esta carpeta a la raíz del repo mars-fit-analyzer.
No subas esta carpeta como subcarpeta; abre la carpeta y sube lo de adentro.

Ruta esperada en GitHub:

mars-fit-analyzer/
  main.py
  db.py
  requirements.txt
  routers/
  strava/
  tools/
  data/garmin_reference_2026_06_08/

IMPORTANTE:
- Esta carpeta NO incluye .env
- NO incluye private_data/
- NO incluye el ZIP original de Garmin
- SÍ incluye data/garmin_reference_2026_06_08 con los 3 .json.gz normalizados

Después de subir:
1. Haz commit en GitHub.
2. Railway debería redeployar automático.
3. Si no redeploya: Railway -> Deploy latest commit.

Variables que deben estar en Railway:
- DATABASE_URL
- SUPABASE_URL
- SUPABASE_SERVICE_ROLE_KEY
- STRAVA_CLIENT_ID
- STRAVA_CLIENT_SECRET
- STRAVA_VERIFY_TOKEN
- STRAVA_CALLBACK_URL
- STRAVA_WEBHOOK_URL
- STRAVA_ALLOWED_ATHLETE_ID
- EPOCH_API_KEY
- ADMIN_TOKEN

Flujo esperado:
- Garmin export cubre historia hasta 2026-06-04/05.
- Strava API actualiza actividades nuevas después del corte.
