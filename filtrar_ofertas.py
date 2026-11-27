"""
Paso 1: filtro por área (rápido).

  python filtrar_ofertas.py

Toma las ofertas con status "nuevo" de data/jobs.json y Ollama las clasifica por
ÁREA usando solo título / categoría / inicio del mensaje, en lotes (sin CV).

  - en jobs.json:   nuevo -> en_cola  o  nuevo -> descartado
  - SOBRESCRIBE resultados/analisis.json con:
      "ofertas":     las del área (las nuevas + las que seguían en cola), pendientes
      "descartadas": las descartadas en esta corrida
    Después analizar_ofertas.py completa el score y el apply de cada una ahí mismo.

El área se define con variables de entorno (o --area):
  FILTRO_AREA      texto libre con el área (default: Informática y tecnología...)
  FILTRO_CRITERIO  criterio adicional opcional (default: vacío)

Opciones:
  --area "Ingeniería industrial"   usa esa área solo en esta corrida
  --refiltrar                      vuelve a clasificar descartadas y en cola (si cambiaste el área)
  --model qwen3:latest
Variables de entorno: FILTRO_AREA, FILTRO_CRITERIO, FILTRO_LOTE (ofertas por llamada,
  default 20) + las de Ollama
"""
import argparse
import os
import sys
import time
from datetime import datetime

from analizar_ofertas import DEFAULT_MODEL, _as_bool, check_ollama, ollama_json, source_label
from datos import (
    ANALYSIS_FILE, DESCARTADO, EN_COLA, NUEVO,
    load_jobs, save_json, save_jobs, set_status,
)

FILTRO_AREA = os.getenv("FILTRO_AREA", "Informática y tecnología: desarrollo de software, BI, datos, QA/testing")
FILTRO_CRITERIO = os.getenv("FILTRO_CRITERIO", "")
FILTRO_LOTE = int(os.getenv("FILTRO_LOTE", "20"))

FILTER_PROMPT = """Clasifica ofertas de empleo según si pertenecen a esta área profesional:

Área: {area}
{criterio}
Usa tu conocimiento del mercado laboral: un puesto pertenece al área si su trabajo
principal es de esa área, aunque el título use otros nombres, siglas, herramientas
o esté en inglés. Un puesto de ventas, atención al cliente, docencia, compras o
administración NO pertenece al área solo porque el producto, cliente o sector sea de
esa área (ej. "vendedor de tecnología" no es Informática).
Si hay duda razonable, marca true (es preferible no perder una oferta).

Ofertas:
{ofertas}

Devuelve exclusivamente JSON con una entrada por cada oferta, usando su número:
{{"resultados": [{{"i": 1, "relevante": true}}, {{"i": 2, "relevante": false}}]}}
"""

FILTER_SCHEMA = {
    "type": "object",
    "properties": {
        "resultados": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"i": {"type": "integer"}, "relevante": {"type": "boolean"}},
                "required": ["i", "relevante"],
            },
        }
    },
    "required": ["resultados"],
}


def load_filter(area_arg: str | None) -> dict:
    if area_arg:
        return {"area": area_arg.strip(), "criterio": ""}
    return {"area": FILTRO_AREA.strip(), "criterio": FILTRO_CRITERIO.strip()}


def _filter_line(n: int, job: dict) -> str:
    parts = [f"[{n}] {job['title']}"]
    if job.get("category"):
        parts.append(f"Categoría: {job['category']}")
    if job.get("text"):  # Telegram: inicio del mensaje, da contexto barato
        snippet = " ".join(job["text"].split())[:250]
        parts.append(f"Texto: {snippet}")
    return " | ".join(parts)


def classify_batch(model: str, batch: list[dict], cfg: dict) -> dict[int, bool]:
    system = "Eres un clasificador de ofertas laborales. Respondes solo con JSON válido."
    criterio = f"Criterio adicional: {cfg['criterio']}\n" if cfg["criterio"] else ""
    prompt = FILTER_PROMPT.format(
        area=cfg["area"], criterio=criterio,
        ofertas="\n".join(_filter_line(n, j) for n, j in enumerate(batch, 1)),
    )
    for attempt in range(2):
        try:
            data = ollama_json(model, prompt, system, FILTER_SCHEMA)
            decisions = {}
            for item in data.get("resultados", []):
                try:
                    decisions[int(item["i"])] = _as_bool(item.get("relevante", True))
                except (KeyError, TypeError, ValueError):
                    pass
            return decisions
        except Exception as exc:
            print(f"    lote falló ({exc.__class__.__name__}), "
                  f"{'reintentando' if attempt == 0 else 'pasan todas'}")
    return {}


def entry(job: dict) -> dict:
    """Lo que se guarda de cada oferta en analisis.json."""
    return {
        "id": job["id"],
        "title": job["title"],
        "source": job["source"],
        "url": job["url"],
        "date": job.get("date"),
    }


def write_analysis(cfg: dict, jobs: list[dict], dropped: list[dict]) -> int:
    """Sobrescribe analisis.json con todo lo que está en cola + lo descartado ahora."""
    cola = [j for j in jobs if j.get("status") == EN_COLA]
    cola.sort(key=lambda j: (j.get("first_seen", ""), j.get("date") or ""), reverse=True)
    save_json(ANALYSIS_FILE, {
        "area": cfg["area"],
        "filtrado": datetime.now().isoformat(timespec="seconds"),
        "resumen": {"en_cola": len(cola), "descartadas": len(dropped),
                    "analizadas": 0, "apply": 0},
        "ofertas": [{**entry(j), "status": "pendiente"} for j in cola],
        "descartadas": [entry(j) for j in dropped],
    })
    return len(cola)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--area", help="área del filtro solo para esta corrida (pisa FILTRO_AREA)")
    ap.add_argument("--refiltrar", action="store_true",
                    help="volver a clasificar las descartadas y las que están en cola")
    args = ap.parse_args()

    cfg = load_filter(args.area)
    if not cfg.get("area"):
        sys.exit("Falta el área: define FILTRO_AREA o usa --area")

    jobs = load_jobs()
    if args.refiltrar:
        n = 0
        for j in jobs:
            if j.get("status") in (DESCARTADO, EN_COLA):
                j["status"] = NUEVO
                n += 1
        save_jobs(jobs)
        print(f"Re-clasificando {n} ofertas (descartadas y en cola).")

    pending = [j for j in jobs if j.get("status") == NUEVO]
    if not pending:
        print("No hay ofertas nuevas para filtrar (analisis.json no se tocó).")
        return

    check_ollama(args.model)
    size = max(1, FILTRO_LOTE)
    print(f"Filtrando {len(pending)} ofertas | área: {cfg['area']}")
    print(f"modelo: {args.model} | lotes de {size}\n")

    kept, dropped = [], []
    t_start = time.time()
    try:
        for start in range(0, len(pending), size):
            batch = pending[start:start + size]
            t0 = time.time()
            decisions = classify_batch(args.model, batch, cfg)
            changes = {}
            for n, job in enumerate(batch, 1):
                if decisions.get(n, True):  # ante duda u omisión, pasa
                    changes[job["id"]] = EN_COLA
                    kept.append(job)
                else:
                    changes[job["id"]] = DESCARTADO
                    dropped.append(job)
            set_status(changes)  # guardar en cada lote (Ctrl+C no pierde nada)
            passed = sum(decisions.get(n, True) for n in range(1, len(batch) + 1))
            print(f"  lote {start // size + 1}/{-(-len(pending) // size)}: "
                  f"{len(batch)} -> {passed} pasan ({time.time() - t0:.0f}s)")
    except KeyboardInterrupt:
        print("\nInterrumpido. Lo filtrado hasta ahora quedó guardado.")

    if not (kept or dropped):
        return
    en_cola = write_analysis(cfg, load_jobs(), dropped)

    print(f"\n{len(kept) + len(dropped)} filtradas en {time.time() - t_start:.0f}s: "
          f"{len(kept)} pasan, {len(dropped)} descartadas")
    for job in kept:
        print(f"  + [{source_label(job['source'])}] {job['title'][:90]}")
    print(f"\nresultados/analisis.json: {en_cola} ofertas para analizar "
          f"(+{len(dropped)} descartadas listadas)  ->  python analizar_ofertas.py")


if __name__ == "__main__":
    main()
