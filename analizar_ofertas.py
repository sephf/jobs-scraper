"""
Analizador nocturno de ofertas (corre en tu PC con Ollama).

  git pull
  python analizar_ofertas.py

1. Carga el CV (PDF) de la carpeta cv/.
2. Lee solo ofertas nuevas (processed=false) de data/jobs.json.
3. Abre cada enlace y extrae la descripción.
4. Consulta Ollama (Qwen) y obtiene un JSON por oferta.
5. Genera el ranking (consola + resultados/ranking_*.md).

El estado "procesada" se guarda en data/processed.json (el recolector de
GitHub Actions nunca toca ese archivo, así git pull no choca).
Usa --push para subirlo y que jobs.json quede con processed=true.

Opciones útiles:
  --model qwen2.5:7b   --limit 20   --top 15   --cv cv/mi_cv.pdf   --push
Variables de entorno: OLLAMA_URL, OLLAMA_MODEL, OLLAMA_NUM_CTX
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

import fuentes
from fuentes.base import HEADERS

BASE = Path(__file__).resolve().parent
JOBS_FILE = BASE / "data" / "jobs.json"
PROCESSED_FILE = BASE / "data" / "processed.json"
RESULTS_DIR = BASE / "resultados"
ANALYSIS_FILE = RESULTS_DIR / "analisis.json"
CV_DIR = BASE / "cv"

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
DEFAULT_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:latest")
NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "8192"))

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

Devuelve exclusivamente JSON con esta forma:
{{
  "score": 0-100,
  "apply": true/false,
  "reason": "...",
  "missing_skills": [],
  "salary_detected": "",
  "remote": true/false
}}

Reglas:
- "score": qué tan bien encaja mi perfil (100 = encaje perfecto).
- "reason": una o dos frases en español, concretas (tecnologías que coinciden o no).
- "missing_skills": habilidades pedidas que NO aparecen en mi CV.
- "salary_detected": el salario si la oferta lo menciona, si no "".
- "remote": true solo si la oferta indica remoto o híbrido-remoto.
- Si el texto no es una oferta de empleo, score 0 y apply false.
"""


# ----------------------------------------------------------------- utilidades
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
    for sel in (extra_selectors or []) + GENERIC_SELECTORS:
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
        try:
            resp = self.session.get(url, timeout=20)
            if resp.ok and "html" in resp.headers.get("content-type", "html"):
                text = extract_main_text(resp.text, selectors)
        except requests.RequestException:
            pass
        if len(text) < 300:
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
def ask_ollama(model: str, prompt: str) -> dict:
    payload = {
        "model": model,
        "messages": [
            {"role": "system",
             "content": "Eres un reclutador técnico. Respondes solo con JSON válido."},
            {"role": "user", "content": prompt},
        ],
        "format": "json",
        "stream": False,
        "options": {"temperature": 0, "num_ctx": NUM_CTX},
    }
    resp = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=600)
    resp.raise_for_status()
    content = resp.json()["message"]["content"]
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S)
    match = re.search(r"\{.*\}", content, flags=re.S)
    if not match:
        raise ValueError(f"Respuesta sin JSON: {content[:200]}")
    return normalize(json.loads(match.group(0)))


def _as_bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "si", "sí", "yes", "1")
    return bool(value)


def normalize(data: dict) -> dict:
    try:
        score = int(round(float(data.get("score", 0))))
    except (TypeError, ValueError):
        score = 0
    skills = data.get("missing_skills") or []
    if isinstance(skills, str):
        skills = [s.strip() for s in skills.split(",") if s.strip()]
    return {
        "score": max(0, min(100, score)),
        "apply": _as_bool(data.get("apply", False)),
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


def render_ranking(items: list[tuple[dict, dict]], top: int) -> tuple[str, str]:
    items = sorted(items, key=lambda it: it[1]["score"], reverse=True)[:top]
    sep = "-" * 48
    txt = ["", "TOP OFERTAS", ""]
    md = [f"# Top ofertas — {datetime.now():%Y-%m-%d %H:%M}", ""]
    for job, res in items:
        mark = "APLICAR" if res["apply"] else "no aplicar"
        txt += [f"{res['score']}/100  ({mark})", job["title"], location_line(job, res)]
        if res["salary_detected"]:
            txt.append(f"Salario: {res['salary_detected']}")
        txt += ["", "Motivo:", res["reason"]]
        if res["missing_skills"]:
            txt.append("Te falta: " + ", ".join(res["missing_skills"]))
        txt += [job["url"], "", sep, ""]

        md += [f"## {res['score']}/100 — [{job['title']}]({job['url']})",
               f"*{location_line(job, res)}* · **{mark}**", ""]
        if res["salary_detected"]:
            md.append(f"- **Salario:** {res['salary_detected']}")
        md.append(f"- **Motivo:** {res['reason']}")
        if res["missing_skills"]:
            md.append("- **Te falta:** " + ", ".join(res["missing_skills"]))
        md += ["", "---", ""]
    return "\n".join(txt), "\n".join(md)


def git_push(count: int) -> None:
    cmds = [
        ["git", "add", str(PROCESSED_FILE.relative_to(BASE))],
        ["git", "commit", "-m", f"analizadas {count} ofertas"],
        ["git", "pull", "--rebase", "--autostash"],
        ["git", "push"],
    ]
    for cmd in cmds:
        r = subprocess.run(cmd, cwd=BASE, capture_output=True, text=True)
        if r.returncode != 0 and cmd[1] != "commit":
            print(f"git falló ({' '.join(cmd)}):\n{r.stderr.strip()}")
            return
    print("processed.json subido a GitHub.")


# ------------------------------------------------------------------------ main
def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser()
    ap.add_argument("--cv", help="ruta del CV en PDF (default: primer PDF en cv/)")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--limit", type=int, default=0, help="máximo de ofertas a analizar")
    ap.add_argument("--top", type=int, default=15, help="cuántas mostrar en el ranking")
    ap.add_argument("--push", action="store_true", help="subir processed.json a GitHub")
    ap.add_argument("--ranking", action="store_true",
                    help="solo mostrar el ranking de todo lo ya analizado")
    args = ap.parse_args()

    jobs = load_json(JOBS_FILE, [])
    processed = set(load_json(PROCESSED_FILE, []))
    analysis = load_json(ANALYSIS_FILE, {})
    by_id = {j["id"]: j for j in jobs}

    if args.ranking:
        items = [(by_id[i], a) for i, a in analysis.items() if i in by_id]
        txt, _ = render_ranking(items, args.top)
        print(txt)
        return

    pending = [j for j in jobs if not j.get("processed") and j["id"] not in processed]
    if args.limit:
        pending = pending[: args.limit]
    if not pending:
        print("No hay ofertas nuevas. ¿Hiciste git pull?")
        return

    check_ollama(args.model)
    cv = load_cv(args.cv)
    print(f"Ofertas nuevas: {len(pending)} | modelo: {args.model}\n")

    fetcher = Fetcher()
    done: list[tuple[dict, dict]] = []
    try:
        for n, job in enumerate(pending, 1):
            print(f"[{n}/{len(pending)}] {job['title'][:80]}")
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
                # no se marca como procesada: se reintenta la próxima noche
                print(f"    ERROR: {exc}")
                continue

            res["analyzed_at"] = datetime.now().isoformat(timespec="seconds")
            analysis[job["id"]] = res
            processed.add(job["id"])
            done.append((job, res))
            # guardar progreso en cada oferta (Ctrl+C no pierde nada)
            save_json(ANALYSIS_FILE, analysis)
            save_json(PROCESSED_FILE, sorted(processed))
            print(f"    {res['score']}/100  apply={res['apply']}  ({time.time() - t0:.0f}s)")
    except KeyboardInterrupt:
        print("\nInterrumpido. Lo analizado hasta ahora quedó guardado.")
    finally:
        fetcher.close()

    if not done:
        return
    txt, md = render_ranking(done, args.top)
    print(txt)
    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"ranking_{datetime.now():%Y-%m-%d_%H%M}.md"
    out.write_text(md, encoding="utf-8")
    print(f"Ranking guardado en {out.relative_to(BASE)}")

    if args.push:
        git_push(len(done))


if __name__ == "__main__":
    main()
