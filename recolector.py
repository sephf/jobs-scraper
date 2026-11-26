"""
Recolector de ofertas (corre en GitHub Actions cada 10 min).

Las páginas viven en fuentes/ (un archivo por página). Este script solo:
  1. corre las fuentes en paralelo (un hilo por fuente),
  2. detecta las ofertas nuevas,
  3. las agrega a data/jobs.json con processed=false,
  4. sincroniza "processed" desde data/processed.json (lo escribe el analizador).

Uso:
  python recolector.py                     # todas las fuentes
  python recolector.py --sources cr,latam  # algunas
  python recolector.py --list              # ver fuentes disponibles
  python recolector.py --sources empleo --dry-run   # probar sin guardar
Variables de entorno:
  MAX_AGE_DAYS  borra ofertas ya procesadas con más de N días (default 60)
  MAX_WORKERS   fuentes en paralelo como máximo (default 4)
  (cada fuente puede leer las suyas, ej. EMPLEO_MAX_PAGES, TELEGRAM_PAGES)
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fuentes
from fuentes.base import now_iso

BASE = Path(__file__).resolve().parent
JOBS_FILE = BASE / "data" / "jobs.json"
PROCESSED_FILE = BASE / "data" / "processed.json"
EXTRA_FIELDS = ("category", "post_url", "text")


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


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    disponibles = fuentes.todas()
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", default="all",
                    help=f"separadas por coma: {','.join(disponibles)} (default: all)")
    ap.add_argument("--list", action="store_true", help="listar fuentes y salir")
    ap.add_argument("--dry-run", action="store_true", help="no guardar jobs.json")
    args = ap.parse_args()

    if args.list:
        for key, cls in disponibles.items():
            print(f"{key:12} {cls.source:18} {cls.label} ({cls.region})")
        return 0

    if args.sources == "all":
        keys = list(disponibles)
    else:
        keys = [k.strip() for k in args.sources.split(",") if k.strip()]
        desconocidas = [k for k in keys if k not in disponibles]
        if desconocidas:
            sys.exit(f"Fuentes desconocidas: {desconocidas}. Disponibles: {list(disponibles)}")

    jobs: list[dict] = load_json(JOBS_FILE, [])
    by_id = {j["id"]: j for j in jobs}
    urls_by_source: dict[str, set] = {}
    for j in jobs:
        urls_by_source.setdefault(j["source"], set()).add(j["url"])
    processed_ids = set(load_json(PROCESSED_FILE, []))

    # cada fuente corre en su propio hilo (Playwright incluido: cada una abre
    # su propio navegador dentro de su hilo, nunca se comparte)
    def run(key: str):
        cls = disponibles[key]
        t0 = time.time()
        found = cls().scrape(set(urls_by_source.get(cls.source, set())))
        return found, time.time() - t0

    workers = max(1, min(int(os.getenv("MAX_WORKERS", "4")), len(keys)))
    results: dict[str, list] = {}
    errors = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run, key): key for key in keys}
        for fut in as_completed(futures):
            cls = disponibles[futures[fut]]
            try:
                found, secs = fut.result()
                results[futures[fut]] = found
                print(f"[{cls.label}] {len(found)} encontradas ({secs:.1f}s)")
            except Exception as exc:  # una fuente caída no frena las demás
                errors += 1
                print(f"[{cls.label}] ERROR {exc.__class__.__name__}: {exc}", file=sys.stderr)

    # orden estable (el mismo de --sources), sin importar cuál terminó primero
    collected = [job for key in keys for job in results.get(key, [])]

    # detectar nuevas
    stamp = now_iso()
    new_jobs = []
    for job in collected:
        known = urls_by_source.setdefault(job["source"], set())
        if job["id"] in by_id or job["url"] in known:
            continue
        job = {
            "id": job["id"],
            "source": job["source"],
            "title": job["title"],
            "url": job["url"],
            "date": job.get("date") or stamp,
            "first_seen": stamp,
            "processed": False,
            **{k: job[k] for k in EXTRA_FIELDS if job.get(k)},
        }
        by_id[job["id"]] = job
        known.add(job["url"])
        new_jobs.append(job)

    # sincronizar processed desde el analizador
    for job in by_id.values():
        if job["id"] in processed_ids:
            job["processed"] = True

    # limpiar viejas ya procesadas
    max_age = int(os.getenv("MAX_AGE_DAYS", "60"))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age)).isoformat()
    kept = [j for j in by_id.values()
            if not (j["processed"] and j.get("first_seen", stamp) < cutoff)]
    kept.sort(key=lambda j: (j.get("first_seen", ""), j["id"]))

    print(f"\nNuevas: {len(new_jobs)} | Total en JSON: {len(kept)}")
    for j in new_jobs[:30]:
        print(f"  + [{j['source']}] {j['title']}")

    if args.dry_run:
        print("(dry-run: no se guardó nada)")
    else:
        save_json(JOBS_FILE, kept)

    gh_out = os.getenv("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as fh:
            fh.write(f"new_count={len(new_jobs)}\n")

    return 1 if errors and not collected else 0


if __name__ == "__main__":
    sys.exit(main())
