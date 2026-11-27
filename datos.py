"""
Los dos JSON del proyecto y los estados de cada oferta.

data/jobs.json           historial de TODO lo recolectado, con "status" por oferta
resultados/analisis.json se sobrescribe en cada filtrar_ofertas.py; analizar_ofertas.py
                         completa ahí el score y el apply

Estados en jobs.json:
  nuevo       recién recolectada, falta filtrar
  descartado  el filtro dijo que no es del área
  en_cola     pasó el filtro, falta analizar con el CV
  analizado   ya tiene score (se guarda también "score" y "apply" como historial)
"""
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
JOBS_FILE = BASE / "data" / "jobs.json"
ANALYSIS_FILE = BASE / "resultados" / "analisis.json"

NUEVO, DESCARTADO, EN_COLA, ANALIZADO = "nuevo", "descartado", "en_cola", "analizado"

# archivos del formato anterior (solo para migrar una vez)
_OLD_PROCESSED = BASE / "data" / "processed.json"


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
    jobs = load_json(JOBS_FILE, [])
    if any("status" not in j for j in jobs):
        _migrate(jobs)
    return jobs


def save_jobs(jobs: list[dict]) -> None:
    save_json(JOBS_FILE, jobs)


def set_status(changes: dict[str, str], extra: dict[str, dict] | None = None) -> None:
    """Cambia el status (y opcionalmente otros campos) de varias ofertas releyendo
    jobs.json del disco, para no pisar ofertas que el recolector haya agregado."""
    if not changes:
        return
    extra = extra or {}
    jobs = load_jobs()
    for job in jobs:
        if job["id"] in changes:
            job["status"] = changes[job["id"]]
            job.update(extra.get(job["id"], {}))
    save_jobs(jobs)


def _migrate(jobs: list[dict]) -> None:
    """Formato anterior (processed + processed.json + analisis por id) -> status."""
    old_analysis = load_json(ANALYSIS_FILE, {})
    if not isinstance(old_analysis, dict) or "ofertas" in old_analysis:
        old_analysis = {}
    old_processed = set(load_json(_OLD_PROCESSED, []))
    for job in jobs:
        if "status" in job:
            continue
        a = old_analysis.get(job["id"], {})
        if "score" in a:
            job["status"] = ANALIZADO
        elif a.get("filtered"):
            job["status"] = DESCARTADO
        elif a.get("filter_pass"):
            job["status"] = EN_COLA
        elif job.get("processed") or job["id"] in old_processed:
            job["status"] = ANALIZADO
        else:
            job["status"] = NUEVO
        job.pop("processed", None)
    for job in jobs:
        job.pop("processed", None)
    save_jobs(jobs)
    if _OLD_PROCESSED.exists():
        _OLD_PROCESSED.replace(_OLD_PROCESSED.with_suffix(".json.old"))
    print("(jobs.json migrado al formato con 'status'; processed.json ya no se usa)")
