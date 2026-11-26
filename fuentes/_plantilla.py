"""
PLANTILLA para una página nueva.

1. Copia este archivo SIN el guion bajo, ej.:  fuentes/computrabajo.py
2. Ajusta los atributos y escribe scrape().
3. Prueba solo esa fuente:  python recolector.py --sources computrabajo --dry-run

Elige el estilo según la página:
  - HTML normal  -> requests + BeautifulSoup (rápido, ver abajo)
  - usa JavaScript -> Playwright (ver empleo_cr.py)
  - otro canal de Telegram -> agrega una línea a CANALES en telegram.py
"""
import requests
from bs4 import BeautifulSoup

from .base import HEADERS, Fuente, normalize_url, short_hash


class MiPagina(Fuente):
    key = "mipagina"            # para --sources mipagina
    source = "mipagina"         # lo que queda en el JSON
    label = "Mi Página"
    region = "Costa Rica"
    description_selectors = [".job-description"]  # dónde está el texto en el detalle

    def scrape(self, known_urls):
        resp = requests.get("https://ejemplo.com/empleos", headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        results = []
        for card in soup.select(".job-card"):
            link = card.select_one("a[href]")
            title = card.select_one(".title")
            if not (link and title):
                continue
            url = normalize_url(requests.compat.urljoin(resp.url, link["href"]))
            results.append(self.job(
                uid=short_hash(url),
                title=title.get_text(strip=True),
                url=url,
                date=None,          # si la página trae fecha, ponla aquí
            ))
        return results
