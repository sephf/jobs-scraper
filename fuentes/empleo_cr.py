"""empleo.cr (necesita navegador: Playwright)."""
import os
import time

from .base import HEADERS, Fuente, normalize_url, short_hash


class EmpleoCR(Fuente):
    key = "empleo"
    source = "empleo_cr"
    label = "empleo.cr"
    region = "Costa Rica"
    # cabecera (categoría, lugar, salario) + detalles + descripción
    description_selectors = [".job-detail-header", ".job-detail-detail", ".job-detail-description"]

    # ordenado por más nuevas: así, al llegar a una página ya conocida,
    # todo lo de atrás también es viejo y se puede parar
    URL = (
        "https://empleo.cr/job-list/?filter-orderby=newest&paged={page}"
        "&filter-date-posted=30days&filter-salary-from=0&filter-salary-to=1000000"
    )

    def __init__(self):
        # 0 = todas las páginas
        self.max_pages = int(os.getenv("EMPLEO_MAX_PAGES", "0"))

    def scrape(self, known_urls):
        from playwright.sync_api import sync_playwright
        from playwright.sync_api import TimeoutError as PWTimeout

        results, prev_links = [], set()
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=HEADERS["User-Agent"])
            current = 1
            while not (self.max_pages and current > self.max_pages):
                try:
                    page.goto(self.URL.format(page=current),
                              wait_until="domcontentloaded", timeout=20000)
                    try:
                        page.wait_for_selector(".item-job", timeout=8000)
                    except PWTimeout:
                        break
                    print(f"[empleo.cr] página {current}")
                    jobs = page.evaluate("""
                        () => Array.from(document.querySelectorAll('.item-job')).map(job => ({
                            titulo: job.querySelector('.job-title')?.textContent.trim(),
                            categoria: job.querySelector('.category-job')?.textContent.trim(),
                            link: job.querySelector('.job-title a')?.href,
                            fecha: (job.querySelector('time')?.getAttribute('datetime')
                                    || job.querySelector('.job-date, .date')?.textContent || '').trim()
                        }))
                    """)
                except PWTimeout:
                    print(f"[empleo.cr] timeout en página {current}")
                    break

                links = {j["link"] for j in jobs if j.get("link")}
                if not links or links == prev_links:
                    break  # vacía o el sitio repite la última página
                prev_links = links

                new_on_page = 0
                for j in jobs:
                    if not (j.get("link") and j.get("titulo")):
                        continue
                    url = normalize_url(j["link"])
                    new_on_page += url not in known_urls
                    results.append(self.job(uid=short_hash(url), title=j["titulo"], url=url,
                                            date=j.get("fecha"), category=j.get("categoria")))

                if known_urls and new_on_page == 0:
                    break  # todo lo de aquí para atrás ya lo teníamos
                current += 1
                time.sleep(0.5)
            browser.close()
        return results
