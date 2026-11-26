"""Clase base y utilidades compartidas por todas las fuentes."""
import hashlib
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "es-CR,es;q=0.9,en;q=0.8",
}

_REGISTRY: dict[str, type["Fuente"]] = {}


class Fuente:
    """
    Una página/canal de donde salen ofertas.

    Para crear una nueva, hereda de esta clase en un archivo de fuentes/
    y define:
        key     id corto para --sources (ej. "empleo")
        source  valor que queda en el JSON (ej. "empleo_cr")
        label   nombre bonito para el ranking
        region  "Costa Rica", "LATAM", "Global"...
        scrape(known_urls) -> list[dict]  con al menos: id, title, url
    Opcional:
        description_selectors  selectores CSS donde está la descripción
                               en la página de detalle (lo usa el analizador)
    """

    key: str = ""
    source: str = ""
    label: str = ""
    region: str = ""
    description_selectors: list[str] = []

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.key:  # las clases base genéricas (sin key) no se registran
            if cls.key in _REGISTRY:
                raise ValueError(f"key duplicada en fuentes: {cls.key}")
            _REGISTRY[cls.key] = cls

    def scrape(self, known_urls: set[str]) -> list[dict]:
        raise NotImplementedError

    # ayuda para armar el dict con el formato estándar
    def job(self, *, uid: str, title: str, url: str, date: str | None = None, **extra) -> dict:
        data = {
            "id": f"{self.source}:{uid}",
            "source": self.source,
            "title": clean_title(title),
            "url": url,
            "date": date or None,
        }
        data.update({k: v for k, v in extra.items() if v})
        return data


# ----------------------------------------------------------------- utilidades
def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def short_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def clean_title(text: str, max_len: int = 140) -> str:
    text = re.sub(r"^[^\w¿¡(]+", "", text or "", flags=re.UNICODE).strip()
    text = re.sub(r"\s+", " ", text)
    return text[:max_len].rstrip()
