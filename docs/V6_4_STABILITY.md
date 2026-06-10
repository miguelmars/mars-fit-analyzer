# Epoch v6.4 — Surgery, Stability & Cleanup

## Qué cambió

### Arquitectura frontend
- `templates/app.html` ahora es shell puro (~200 líneas): solo HTML + links.
- Todo el JS vive en `static/`:
  - `app.js` — navegación, router, pantallas secundarias (dash, activities, gear, cal, perf, fuerza, wellness, eficiencia, correlaciones, nutricion, capacidades)
  - `utils.js` — API const, metric, row, fmtDate, hms
  - `api.js` — capa API con manejo de errores
  - `config.js` — APP_NAME, USER_DISPLAY_NAME, VERSION
  - `screens/{home,coach,goals,profile,evolution}.js`
- CSS completo en `static/app.css`.

### Estabilidad
- Todas las pantallas usan `Promise.allSettled` — un endpoint caído no tumba la pantalla.
- Watchdog: si una pantalla sigue en "Cargando..." tras 15s, muestra botón Reintentar.
- `load()` envuelto en try/catch global — errores se muestran en la pantalla, nunca loading infinito.
- `window.onerror` + `unhandledrejection` registrados en consola.
- Fix: comillas tipográficas corruptas (`”`) en loadCapacidades rompían atributos HTML.

### Producto
- Coach v6.4: estructura Observación → Qué significa → Sugerencia.
  - Sin meta activa ni plan: card "Contexto incompleto", sugerencia prudente, CTA a Metas.
  - "Escalada" explicada como rendimiento en subidas (qué mide y por qué importa).
  - Historial presentado como contexto ("ya estuviste ahí"), no como orden.
- /capacidades: capacidades actuales primero (calculadas → por calibrar), historia después.
  - Años "sin datos suficientes" compactados en una línea con causa explicada.
  - Composición corporal: aviso cuando hay <12 semanas de peso.
- Branding: "Mars Fit Analyzer" → "Epoch" (upload.html); "Mars Index" → "Índice Epoch";
  "sesiones en Mars" → "sesiones en Epoch". Mars queda solo en config.js como usuario.

### Infraestructura
- `/` → 302 `/home` · `/upload` conserva uploader FIT.
- `GET /admin/healthcheck` → `{status: healthy|degraded|broken, checks:{...}}`.
- `scripts/smoke_test.py` — prueba /, /home, /upload, estáticos, API críticos.
  Uso: `python scripts/smoke_test.py [URL]`
- Service worker cache `epoch-v1` → `epoch-v6.4` (purga JS/CSS viejos).
- `manifest.json` start_url ya es `/home`. Sin dominios hardcoded en links internos.

## Verificar en Railway
1. `python scripts/smoke_test.py` (desde cualquier máquina con Python).
2. Abrir `/home` → consola limpia, sin "Cargando..." infinito.
3. `/coach` sin meta activa → debe aparecer "Contexto incompleto".
4. `/capacidades` → capacidades arriba, sin lista 2017–2026 de "sin datos".
5. `/admin/healthcheck` → `"status": "healthy"`.

## Pendiente / Riesgos
- Dominio app.useepoch.app: solo falta CNAME en Railway + Namecheap (acción manual).
- El backend de /gpt/adaptive-coach sigue calculando limitante desde historial; el
  frontend ya lo presenta como contexto, pero una v6.5 podría ajustar el endpoint.
- SW cachea páginas: tras deploy, refrescar 2 veces o cerrar/abrir PWA para tomar v6.4.
