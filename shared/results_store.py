"""
shared/results_store.py — In-memory cache para resultados FIT recientes
=======================================================================
TD-010A: extraído de main.py para que routers/activities.py pueda
acceder al mismo dict sin importaciones circulares.

Uso:
    from shared.results_store import RESULTS_STORE
"""

RESULTS_STORE: dict = {}
"""
Clave: session_id (str)
Valor: {"result": <dict con session/records/zones/derived_insights>}

Máximo RESULTS_STORE_MAX entradas (definido en db.py).
La función _save_result() en main.py gestiona el FIFO de entradas.
"""
