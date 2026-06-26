# Subida Bloque A — instrucciones para la computadora con Git

## 1. Archivos a ELIMINAR explícitamente (routers duplicados)
En la otra computadora, además de copiar los modificados, hay que borrarlos
del repo y registrar la eliminación (no basta con que no se copien):

```bash
git rm routers/gpt_dashboard.py routers/gpt_coaching.py \
       routers/gpt_history.py routers/gpt_patterns.py
```

Estos 4 duplicaban al 100% endpoints de gpt_analytics (registrado antes) y
nunca servían tráfico. Recuperables del historial de Git si hicieran falta.

## 2. Variable de entorno en Railway
```
EPOCH_API_KEY = <una llave fuerte, p.ej. openssl rand -hex 24>
```
- Mientras NO esté configurada: la app funciona igual que antes (warning en logs).
- Una vez configurada: toda escritura exige la llave. El auto-sync interno ya
  la lee de la misma env var, así que no se rompe.

## 3. Tras el deploy
1. Abrir la app → Perfil → "Access key" → pegar la misma llave (una vez por dispositivo).
2. Verificar: `EPOCH_API_KEY=<llave> python3 scripts/smoke_test.py` → 61/61.
3. Correr los tests: `pytest tests/ -q` → 168/168.

## 4. Orden seguro
Subir todo → confirmar que la app levanta → configurar EPOCH_API_KEY →
pegar la llave en Perfil → smoke con llave.
