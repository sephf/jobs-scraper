"""
Canales públicos de Telegram (t.me/s/<canal>).

Todos usan el mismo lector; para agregar un canal solo suma una línea a CANALES.
"""
import os
import time

import requests
from bs4 import BeautifulSoup

from .base import HEADERS, Fuente, clean_title


class CanalTelegram(Fuente):
    channel: str = ""  # nombre del canal en t.me/s/<channel>

    def __init__(self):
        # cada página trae ~20 mensajes
        self.pages = int(os.getenv("TELEGRAM_PAGES", "2"))

    @staticmethod
    def _text(el) -> str:
        for br in el.find_all("br"):
            br.replace_with("\n")
        lines = (ln.strip() for ln in el.get_text().splitlines())
        return "\n".join(ln for ln in lines if ln)

    def scrape(self, known_urls):
        session = requests.Session()
        session.headers.update(HEADERS)
        results, before = [], None

        for _ in range(max(1, self.pages)):
            url = f"https://t.me/s/{self.channel}" + (f"?before={before}" if before else "")
            resp = session.get(url, timeout=20)
            resp.raise_for_status()
            messages = BeautifulSoup(resp.text, "html.parser").select(
                ".tgme_widget_message[data-post]")
            if not messages:
                break

            ids = []
            for msg in messages:
                post = msg["data-post"]  # "canal/1234"
                try:
                    msg_id = int(post.split("/")[-1])
                except ValueError:
                    continue
                if before is not None and msg_id >= before:
                    continue  # ya visto en la página anterior
                ids.append(msg_id)

                text_el = msg.select_one(".tgme_widget_message_text")
                if not text_el:
                    continue  # foto/sticker sin texto
                external = [a["href"] for a in text_el.select("a[href]")
                            if a["href"].startswith("http") and "t.me/" not in a["href"]]
                text = self._text(text_el)
                if len(text) < 15:
                    continue
                first = next((ln for ln in text.splitlines() if clean_title(ln)), text)
                time_el = msg.select_one(".tgme_widget_message_date time[datetime]")
                post_url = f"https://t.me/{post}"

                results.append(self.job(
                    uid=str(msg_id),
                    title=first,
                    url=external[0] if external else post_url,
                    date=time_el["datetime"] if time_el else None,
                    post_url=post_url,
                    text=text[:4000],
                ))

            if not ids:
                break
            before = min(ids)
            time.sleep(0.5)
        return results


# ------------------------------------------------------------------ canales
# key (--sources) | canal en t.me | source en el JSON | nombre | región
CANALES = [
    ("cr",    "stemjobscr",    "telegram_cr",    "STEM Jobs CR",    "Costa Rica"),
    ("latam", "stemjobslatam", "telegram_latam", "STEM Jobs LATAM", "LATAM"),
]

# crea y registra una subclase por canal
for _key, _channel, _source, _label, _region in CANALES:
    type(f"Telegram_{_key}", (CanalTelegram,), {
        "key": _key, "channel": _channel, "source": _source,
        "label": _label, "region": _region,
    })
