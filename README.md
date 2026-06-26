# FIT Analyzer API — Mars Edition

Esta versión está ajustada para el GPT de Mars.

## Qué hace

Recibe un `.fit` o `.zip` de Garmin y devuelve JSON con:

- resumen real de sesión desde FIT
- laps reales
- zonas oficiales Mars por bpm
- cadencia, FC, velocidad, altitud y temperatura resumidas
- respuesta compacta para no saturar la Action del GPT

## Cambio importante respecto a la versión anterior

La versión anterior calculaba zonas con porcentajes genéricos de LT.

Esta versión usa las zonas oficiales de Mars:

- Z1: <109 bpm
- Z2: 134–150 bpm
- Z3: 151–160 bpm
- Z4: 161–168 bpm
- Z5: >168 bpm

El rango 109–133 bpm se reporta como "Entre Z1 y Z2 oficial", porque existe un hueco en la configuración oficial.

## Despliegue rápido en Railway

1. Crea un repositorio en GitHub.
2. Sube estos archivos:
   - `main.py`
   - `requirements.txt`
   - `Procfile`
   - `openapi_schema.yaml`
3. En Railway:
   - New Project
   - Deploy from GitHub repo
4. Railway generará una URL parecida a:
   `https://fit-analyzer-api-production.up.railway.app`
5. Abre esa URL. Debe responder:
   `{"status":"ok",...}`

## Configurar Action en GPT

1. Edita tu GPT.
2. Ve a Actions.
3. Create new action.
4. Pega el contenido de `openapi_schema.yaml`.
5. Cambia:
   `https://TU-DOMINIO-RAILWAY.up.railway.app`
   por tu URL real de Railway.
6. Guarda y prueba.

## Instrucción recomendada para el GPT

Pega esto en Instructions:

```
Cuando el usuario suba un archivo .fit o .zip de Garmin, utiliza la Action analyzeFit antes de responder.

Si analyzeFit devuelve datos, usa esos datos como fuente principal del análisis de la sesión.

No inventes:
- causa de picos de FC
- cumplimiento de intervalos
- tráfico
- viento
- fatiga
- sensaciones

Después del análisis pregunta máximo 3 cosas:
1. ¿Cómo te sentiste del 1 al 10?
2. ¿Hubo algo fuera de lo normal?
3. ¿Qué comiste o tomaste durante la ruta?

Si el usuario pregunta "¿cómo me fue hoy?" sin archivo, usa primero Get Activities de Amalgama.
Si el usuario sube FIT/ZIP, usa analyzeFit.
```

## Prueba con curl

```bash
curl -X POST https://TU-URL.up.railway.app/analyze-fit \
  -F "file=@23033230280.zip"
```

## Nota sobre bitácora

Esta API analiza archivos, pero no guarda una bitácora permanente por sí sola.

Para bitácora persistente habría que añadir una base de datos, Google Sheets, Airtable o almacenamiento en Drive.
