# Garmin Export Workflow

Objetivo: usar el export completo de Garmin como fuente maestra sin contaminar la base actual.

## 1. Auditar ZIP

```bash
.venv/bin/python tools/garmin_export_audit.py /ruta/al/garmin_export.zip --out-dir reports
```

Genera:

- `reports/garmin_export_audit.md`
- `reports/garmin_export_audit.json`

## 2. Normalizar A Staging Local

```bash
.venv/bin/python tools/garmin_export_normalize.py /ruta/al/garmin_export.zip --out-dir reports/staging
```

Genera:

- `reports/staging/garmin_activities_clean.json`
- `reports/staging/garmin_gear_clean.json`
- `reports/staging/garmin_sleep_clean.json`

## 3. Simular Importacion

```bash
.venv/bin/python tools/garmin_export_import_staging.py --staging-dir reports/staging
```

Esto no toca la base.

## 4. Importar A Tablas Staging

Requiere `DATABASE_URL`.

```bash
.venv/bin/python tools/garmin_export_import_staging.py --staging-dir reports/staging --execute
```

Tablas:

- `garmin_export_activities`
- `garmin_export_gear`
- `garmin_export_sleep`

## 5. Comparar Contra Sesiones Actuales

```bash
.venv/bin/python tools/garmin_staging_compare.py --out reports/garmin_staging_compare.json
```

## Regla Importante

No importar ciegamente los FIT internos del export. Garmin incluye muchos FIT de `monitoring`, stress y archivos internos del reloj. El indice confiable inicial es `summarizedActivitiesExport`.

## Privacidad

No subir a GitHub:

- `reports/`
- ZIP de Garmin
- FIT/GPX/TCX
- cualquier archivo exportado de Garmin
