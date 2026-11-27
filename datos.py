"""
Archivos del proyecto y estado de cada oferta.

data/jobs.json            historial de todo lo recolectado. Lo escribe SOLO el recolector
                          (GitHub Actions). Los scripts locales nunca lo modifican, así
                          git pull no choca.
resultados/estado.json    LOCAL (fuera de git): status de cada oferta por id
resultados/analisis.json  LOCAL: se sobrescribe en cada filtrar_ofertas.py;
                          analizar_ofertas.py completa ahí el score y el apply

Estados (en estado.json; si un id no aparece, es "nuevo"):
  nuevo       recién recolectada, falta filtrar
  descartado  el filtro dijo que no es del área
  en_cola     pasó el filtro, falta analizar con el CV
  analizado   ya tiene score (estado.json guarda también score y apply)
"""
import json
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
JOBS_FILE = BASE / "data" / "jobs.json"
STATE_FILE = BASE / "resultados" / "estado.json"
ANALYSIS_FILE = BASE / "resultados" / "analisis.json"

NUEVO, DESCARTADO, EN_COLA, ANALIZADO = "nuevo", "descartado", "en_cola", "analizado"

# campos que versiones anteriores guardaban dentro de jobs.json
LEGACY_FIELDS = ("status", "score", "apply", "processed")


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_jobs() -> list[dict]:
    """Lee data/jobs.json (solo lectura para los scripts locales)."""
    if not JOBS_FILE.exists():
        return []
    try:
        return json.loads(JOBS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        sys.exit("data/jobs.json no es un JSON válido (¿quedó un conflicto de git?).\n"
                 "Recupéralo con:  git checkout -- data/jobs.json  y luego  git pull")


def load_state() -> dict[str, dict]:
    if STATE_FILE.exists():
        return load_json(STATE_FILE, {})
    # primera vez: tomar los status que versiones anteriores dejaron en jobs.json
    state = {}
    for job in load_jobs():
        if job.get("status") and job["status"] != NUEVO:
            state[job["id"]] = {k: job[k] for k in ("status", "score", "apply") if k in job}
    save_json(STATE_FILE, state)
    return state


def status_of(state: dict, job_id: str) -> str:
    return state.get(job_id, {}).get("status", NUEVO)


def set_status(changes: dict[str, str], extra: dict[str, dict] | None = None) -> None:
    """Cambia el status de varias ofertas en estado.json (relee del disco antes)."""
    if not changes:
        return
    extra = extra or {}
    state = load_state()
    stamp = datetime.now().isoformat(timespec="seconds")
    for job_id, status in changes.items():
        if status == NUEVO:
            state.pop(job_id, None)
        else:
            state[job_id] = {"status": status, **extra.get(job_id, {}), "updated": stamp}
    save_json(STATE_FILE, state)
