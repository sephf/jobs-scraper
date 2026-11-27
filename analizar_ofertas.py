"""
Paso 2: análisis con el CV (lento). Antes corre filtrar_ofertas.py.

  python filtrar_ofertas.py     # paso 1: filtro por área -> escribe resultados/analisis.json
  python analizar_ofertas.py    # paso 2: completa score y apply en resultados/analisis.json

1. Carga el CV (PDF) de la carpeta cv/.
2. Toma las ofertas "pendiente" de resultados/analisis.json (las más nuevas primero).
3. Abre cada enlace y extrae la descripción.
4. Consulta Ollama con el CV.
5. Escribe en analisis.json score, apply, motivo... (ordenado por score) y en
   data/jobs.json status "analizado" + score + apply (historial).

Se puede cortar con Ctrl+C y seguir otro día: lo analizado queda guardado.

Opciones útiles:
  --limit 20             analizar solo N pendientes (las más nuevas)
  --top 15   --cv cv/mi_cv.pdf   --model qwen3:latest
  --ranking              solo mostrar el ranking de analisis.json
  --reanalizar-ceros     volver a analizar las que quedaron con score 0
Variables de entorno: OLLAMA_URL, OLLAMA_MODEL, OLLAMA_NUM_CTX,
  OLLAMA_TIMEOUT (seg, default 900), APPLY_MIN (score mínimo para apply=true, default 65)
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

import fuentes
from datos import ANALIZADO, ANALYSIS_FILE, BASE, EN_COLA, load_json, load_jobs, save_json, set_status
from fuentes.base import HEADERS

CV_DIR = BASE / "cv"

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
DEFAULT_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:latest")
NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "8192"))
TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "900"))
# score mínimo para marcar una oferta como "APLICAR"
APPLY_MIN = int(os.getenv("APPLY_MIN", "65"))

MAX_CV_CHARS = 6000
MAX_DESC_CHARS = 6000

GENERIC_SELECTORS = [
    ".job-detail-description", ".job-description", ".job-detail",
    "[class*='job-description']", "[class*='description']",
    "article", "main", "[role='main']",
]


def source_label(source: str) -> str:
    cls = fuentes.por_source(source)
    return cls.label if cls else source

PROMPT = """Analiza esta oferta laboral respecto a mi perfil.

Mi perfil (CV):
\"\"\"
{cv}
\"\"\"

Oferta:
Título: {title}
Fuente: {source}
Enlace: {url}
Descripción:
\"\"\"
{description}
\"\"\"

Devuelve exclusivamente JSON con estos campos:
- "score": entero de 0 a 100, qué tan bien encaja mi perfil
- "reason": una o dos frases en español, concretas (qué coincide y qué no)
- "missing_skills": lista de habilidades pedidas que NO aparecen en mi CV
- "salary_detected": el salario si la oferta lo menciona, si no ""
- "remote": true solo si la oferta indica remoto o híbrido

Escala del score (úsala completa, no solo 0 o 100):
- 85-100: cumplo casi todos los requisitos técnicos y de experiencia
- 65-84: mismo tipo de puesto; me faltan algunas herramientas o años de experiencia
- 40-64: misma área (tecnología) pero otro rol o stack bastante distinto
- 15-39: relación lejana; aprovecho poco de mi experiencia
- 0-14: nada que ver con mi perfil (ventas, operario, salud, etc.)
Pedir más años de experiencia de los que tengo baja el score, pero no lo lleva a 0
si el rol y las tecnologías coinciden.
Si la descripción es corta o incompleta, evalúa con el título y lo que haya.
"""

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "reason": {"type": "string"},
        "missing_skills": {"type": "array", "items": {"type": "string"}},
        "salary_detected": {"type": "string"},
        "remote": {"type": "boolean"},
    },
    "required": ["score", "reason", "missing_skills", "salary_detected", "remote"],
}

# ----------------------------------------------------------------- utilidades
def squeeze(text: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    out, blank = [], False
    for ln in lines:
        if ln:
            out.append(ln)
            blank = False
        elif not blank:
            out.append("")
            blank = True
    return "\n".join(out).strip()


# ------------------------------------------------------------------------- CV
def load_cv(cv_arg: str | None) -> str:
    from pypdf import PdfReader

    if cv_arg:
        path = Path(cv_arg)
    else:
        pdfs = sorted(CV_DIR.glob("*.pdf")) or sorted(BASE.glob("cv*.pdf"))
        if not pdfs:
            sys.exit("No encontré el CV. Pon tu PDF en la carpeta cv/ o usa --cv ruta.pdf")
        path = pdfs[0]
    reader = PdfReader(str(path))
    text = squeeze("\n".join(page.extract_text() or "" for page in reader.pages))
    if len(text) < 200:
        sys.exit(f"El PDF {path.name} casi no tiene texto (¿es escaneado?).")
    print(f"CV cargado: {path.name} ({len(text)} caracteres)")
    return text[:MAX_CV_CHARS]


# ------------------------------------------------------ descripción de oferta
def extract_main_text(html: str, extra_selectors: list[str] | None = None) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer",
                     "nav", "aside", "form", "svg", "iframe"]):
        tag.decompose()
    # selectores propios de la fuente: se juntan todos los que existan y se
    # confían aunque el texto sea corto (hay ofertas de una sola línea)
    if extra_selectors:
        parts = [squeeze(el.get_text("\n")) for sel in extra_selectors
                 for el in soup.select(sel)[:1]]
        parts = [p for p in parts if p]
        if parts:
            return "\n\n".join(parts)
    for sel in GENERIC_SELECTORS:
        el = soup.select_one(sel)
        if el:
            text = squeeze(el.get_text("\n"))
            if len(text) > 300:
                return text
    body = soup.body or soup
    return squeeze(body.get_text("\n"))


class Fetcher:
    """requests primero; Playwright si la página necesita JavaScript."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self._pw = None
        self._browser = None

    def _playwright_html(self, url: str) -> str:
        if self._browser is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
        page = self._browser.new_page(user_agent=HEADERS["User-Agent"])
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=25000)
            page.wait_for_timeout(2000)
            return page.content()
        finally:
            page.close()

    def get_text(self, url: str, selectors: list[str] | None = None) -> str:
        text = ""
        min_len = 40 if selectors else 300
        try:
            resp = self.session.get(url, timeout=20)
            if resp.ok and "html" in resp.headers.get("content-type", "html"):
                text = extract_main_text(resp.text, selectors)
        except requests.RequestException:
            pass
        if len(text) < min_len:
            try:
                text = extract_main_text(self._playwright_html(url), selectors) or text
            except Exception as exc:
                print(f"    (no pude abrir con Playwright: {exc.__class__.__name__})")
        return text

    def close(self):
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()


def build_description(job: dict, fetcher: Fetcher) -> str:
    parts = []
    if job.get("text"):  # mensaje de Telegram
        parts.append(job["text"])
    url = job.get("url", "")
    if url and "t.me/" not in url:
        cls = fuentes.por_source(job["source"])
        page_text = fetcher.get_text(url, cls.description_selectors if cls else None)
        if page_text:
            parts.append(page_text)
    desc = "\n\n---\n\n".join(parts) or job.get("title", "")
    return desc[:MAX_DESC_CHARS]


# --------------------------------------------------------------------- Ollama
_schema_supported = True


def ollama_json(model: str, prompt: str, system: str, schema: dict | None = None) -> dict:
    global _schema_supported
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "format": schema if (schema and _schema_supported) else "json",
        "stream": False,
        "options": {"temperature": 0.1, "num_ctx": NUM_CTX},
    }
    resp = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=TIMEOUT)
    if resp.status_code == 400 and isinstance(payload["format"], dict):
        # Ollama viejo (< 0.5) no acepta esquema: usar "json" simple
        _schema_supported = False
        payload["format"] = "json"
        resp = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=TIMEOUT)
    resp.raise_for_status()
    content = resp.json()["message"]["content"]
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S)
    match = re.search(r"\{.*\}", content, flags=re.S)
    if not match:
        raise ValueError(f"Respuesta sin JSON: {content[:200]}")
    return json.loads(match.group(0))


def ask_ollama(model: str, prompt: str) -> dict:
    system = "Eres un reclutador técnico. Respondes solo con JSON válido."
    return normalize(ollama_json(model, prompt, system, ANALYSIS_SCHEMA))


def _as_bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "si", "sí", "yes", "1")
    return bool(value)


def normalize(data: dict) -> dict:
    data = {str(k).lower(): v for k, v in data.items()}
    match = re.search(r"\d+(\.\d+)?", str(data.get("score", "")))
    if not match:
        raise ValueError(f"respuesta sin score válido: {str(data)[:150]}")
    score = int(round(float(match.group(0))))
    skills = data.get("missing_skills") or []
    if isinstance(skills, str):
        skills = [s.strip() for s in skills.split(",") if s.strip()]
    return {
        "score": max(0, min(100, score)),
        "apply": score >= APPLY_MIN,
        "reason": str(data.get("reason", "")).strip(),
        "missing_skills": [str(s) for s in skills],
        "salary_detected": str(data.get("salary_detected") or "").strip(),
        "remote": _as_bool(data.get("remote", False)),
    }


def check_ollama(model: str) -> None:
    try:
        tags = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5).json()
    except requests.RequestException:
        sys.exit(f"Ollama no responde en {OLLAMA_URL}. ¿Está abierto? (ollama serve)")
    names = {m["name"] for m in tags.get("models", [])}
    if model not in names and f"{model}:latest" not in names:
        disponibles = ", ".join(sorted(names)) or "ninguno"
        sys.exit(f"El modelo '{model}' no está descargado. Usa: ollama pull {model}\n"
                 f"Disponibles: {disponibles}")


# -------------------------------------------------------------------- ranking
def location_line(job: dict, res: dict) -> str:
    cls = fuentes.por_source(job["source"])
    bits = []
    if res["remote"]:
        bits.append("Remoto")
    if cls and cls.region:
        bits.append(cls.region)
    bits.append(source_label(job["source"]))
    return " · ".join(bits)


def render_ranking(items: list[dict], top: int) -> str:
    items = sorted(items, key=lambda o: o["score"], reverse=True)[:top]
    sep = "-" * 48
    txt = ["", "TOP OFERTAS", ""]
    for o in items:
        mark = "APLICAR" if o["score"] >= APPLY_MIN else "no aplicar"
        txt += [f"{o['score']}/100  ({mark})", o["title"], location_line(o, o)]
        if o.get("salary_detected"):
            txt.append(f"Salario: {o['salary_detected']}")
        txt += ["", "Motivo:", o.get("reason", "")]
        if o.get("missing_skills"):
            txt.append("Te falta: " + ", ".join(o["missing_skills"]))
        txt += [o["url"], "", sep, ""]
    return "\n".join(txt)


def save_analysis(data: dict) -> None:
    """Ordena (analizadas por score, luego pendientes de la más nueva a la más vieja),
    recalcula el resumen y guarda."""
    done = [o for o in data["ofertas"] if o.get("status") == ANALIZADO]
    todo = [o for o in data["ofertas"] if o.get("status") != ANALIZADO]
    done.sort(key=lambda o: o.get("score", 0), reverse=True)
    todo.sort(key=lambda o: o.get("date") or "", reverse=True)
    data["ofertas"] = done + todo
    data["resumen"] = {
        "en_cola": len(data["ofertas"]) - len(done),
        "descartadas": len(data.get("descartadas", [])),
        "analizadas": len(done),
        "apply": sum(1 for o in done if o.get("apply")),
    }
    save_json(ANALYSIS_FILE, data)


# ------------------------------------------------------------------------ main
def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser()
    ap.add_argument("--cv", help="ruta del CV en PDF (default: primer PDF en cv/)")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--limit", type=int, default=0, help="analizar solo N pendientes (las más nuevas)")
    ap.add_argument("--top", type=int, default=15, help="cuántas mostrar en el ranking")
    ap.add_argument("--ranking", action="store_true",
                    help="solo mostrar el ranking de resultados/analisis.json")
    ap.add_argument("--reanalizar-ceros", action="store_true",
                    help="volver a analizar con el CV las que quedaron con score 0")
    args = ap.parse_args()

    data = load_json(ANALYSIS_FILE, {})
    if not isinstance(data, dict) or "ofertas" not in data:
        sys.exit("resultados/analisis.json no existe o es del formato anterior.\n"
                 "Corre primero: python filtrar_ofertas.py")
    load_jobs()  # migra jobs.json si todavía es del formato anterior

    if args.ranking:
        done = [o for o in data["ofertas"] if o.get("status") == ANALIZADO]
        print(render_ranking(done, args.top))
        return

    if args.reanalizar_ceros:
        zeros = [o for o in data["ofertas"] if o.get("status") == ANALIZADO and o.get("score") == 0]
        keep = ("id", "title", "source", "url", "date")
        for o in zeros:
            for k in list(o):
                if k not in keep:
                    del o[k]
            o["status"] = "pendiente"
        set_status({o["id"]: EN_COLA for o in zeros})
        save_analysis(data)
        data = load_json(ANALYSIS_FILE, data)
        print(f"{len(zeros)} ofertas con score 0 vuelven a pendiente.")

    pending = [o for o in data["ofertas"] if o.get("status") != ANALIZADO]
    if not pending:
        print("No hay ofertas pendientes en analisis.json. "
              "¿Corriste python filtrar_ofertas.py?")
        return
    total = len(pending)
    if args.limit:
        pending = pending[: args.limit]  # ya vienen de la más nueva a la más vieja

    check_ollama(args.model)
    cv = load_cv(args.cv)
    print(f"Pendientes: {total} | analizando: {len(pending)} | modelo: {args.model}\n")

    by_id = {j["id"]: j for j in load_jobs()}
    fetcher = Fetcher()
    done = []
    t_start = time.time()
    try:
        for n, o in enumerate(pending, 1):
            job = {**o, **by_id.get(o["id"], {})}  # texto/categoría vienen de jobs.json
            print(f"[{n}/{len(pending)}] {o['title'][:80]}")
            t0 = time.time()
            try:
                desc = build_description(job, fetcher)
                prompt = PROMPT.format(
                    cv=cv, title=job["title"], url=job["url"],
                    source=source_label(job["source"]),
                    description=desc,
                )
                res = ask_ollama(args.model, prompt)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                # queda pendiente: se reintenta en la próxima corrida
                print(f"    ERROR: {exc}")
                continue

            o.update(res)
            o["status"] = ANALIZADO
            o["analyzed_at"] = datetime.now().isoformat(timespec="seconds")
            o["desc_chars"] = len(desc)
            done.append(o)
            # guardar en cada oferta (Ctrl+C no pierde nada)
            save_analysis(data)
            set_status({o["id"]: ANALIZADO},
                       {o["id"]: {"score": res["score"], "apply": res["apply"]}})
            eta = (time.time() - t_start) / n * (len(pending) - n)
            print(f"    {res['score']}/100  apply={res['apply']}  "
                  f"(desc {len(desc)} car., {time.time() - t0:.0f}s, faltan ~{eta / 60:.0f} min)")
            print(f"    {res['reason'][:140]}")
    except KeyboardInterrupt:
        print("\nInterrumpido. Lo analizado quedó guardado; el resto sigue pendiente.")
    finally:
        fetcher.close()

    if not done:
        return
    print(render_ranking(done, args.top))
    left = total - len(done)
    r = data["resumen"]
    print(f"analisis.json: {r['analizadas']} analizadas ({r['apply']} con apply=true)"
          + (f", {left} pendientes" if left else ""))


if __name__ == "__main__":
    main()
