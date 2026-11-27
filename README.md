# jobs-scraper

Recolecta ofertas de empleo de varias páginas, las filtra por área y las compara con mi CV
usando Ollama (Qwen) en local. El resultado es un ranking de las ofertas a las que más
conviene aplicar.

## Uso diario

```bash
git pull                          # solo si el recolector corre en GitHub Actions
python recolector.py              # si no, recolectar a mano
python filtrar_ofertas.py         # paso 1: filtro por área (minutos)
python analizar_ofertas.py        # paso 2: análisis con el CV (lento)
```

Lo que quieres ver está en **`resultados/analisis.json`**: las ofertas del área ordenadas por
score, con `apply: true/false`, y al final la lista de descartadas.

El paso 2 se puede cortar con **Ctrl+C** y seguir otro día: lo analizado queda guardado y el
resto sigue `"pendiente"`. Para avanzar solo una parte cada noche:

```bash
python analizar_ofertas.py --limit 20     # las 20 pendientes más nuevas
python analizar_ofertas.py --ranking      # ver el ranking en consola sin analizar nada
```

> No corras dos scripts a la vez sobre los mismos archivos. Uno después del otro, sin problema.

## Cómo funciona

Solo hay **dos JSON**:

```
 recolector.py            filtrar_ofertas.py              analizar_ofertas.py
 (cada 10 min)            (paso 1, rápido)                (paso 2, lento)

 empleo.cr ─┐
 Telegram CR├─> data/jobs.json ───> ¿es del área? ───> resultados/analisis.json ───> score vs CV
 Telegram   │   status "nuevo"      (Ollama, lotes,     (SE SOBRESCRIBE:              (Ollama, 1 por
  LATAM ────┘   (historial)          sin CV)             ofertas "pendiente"           oferta; escribe
                                                         + descartadas)                score y apply)
```

- **`data/jobs.json`**: el historial de todo lo recolectado. Cada oferta tiene un `status`.
- **`resultados/analisis.json`**: el resultado. Se **sobrescribe** cada vez que corres
  `filtrar_ofertas.py` (si hay ofertas nuevas) y `analizar_ofertas.py` le va completando el
  score y el `apply`. El historial de scores queda en `jobs.json`.

## Qué lee y qué escribe cada script

| Script | Lee | Escribe |
|---|---|---|
| `recolector.py` | `data/jobs.json`, las páginas web | `data/jobs.json` (agrega las nuevas con `status: "nuevo"`) |
| `filtrar_ofertas.py` | `data/jobs.json` (+ `FILTRO_AREA`) | `data/jobs.json` (`nuevo` → `en_cola` / `descartado`), **sobrescribe** `resultados/analisis.json` |
| `analizar_ofertas.py` | `cv/*.pdf`, `resultados/analisis.json`, `data/jobs.json`, la página de cada oferta | `resultados/analisis.json` (score, apply, motivo…), `data/jobs.json` (`analizado` + score + apply) |

### `status` en `data/jobs.json`

| status | Significa | Lo toma |
|---|---|---|
| `nuevo` | recién recolectada | `filtrar_ofertas.py` |
| `descartado` | el filtro dijo que no es del área | nadie |
| `en_cola` | pasó el filtro, falta el CV | `analizar_ofertas.py` (vía `analisis.json`) |
| `analizado` | tiene score (también guarda `score` y `apply`) | nadie |

### `resultados/analisis.json`

```jsonc
{
  "area": "Informática y tecnología: ...",
  "filtrado": "2026-11-26T19:30:00",
  "resumen": { "en_cola": 11, "descartadas": 33, "analizadas": 5, "apply": 2 },
  "ofertas": [            // analizadas primero (por score), luego pendientes (más nuevas primero)
    { "id": "...", "title": "DevOps Engineer", "source": "telegram_cr", "url": "...",
      "status": "analizado", "score": 78, "apply": true, "reason": "...",
      "missing_skills": ["Terraform"], "salary_detected": "", "remote": true, ... },
    { "id": "...", "title": "Senior Back-End Engineer", ..., "status": "pendiente" }
  ],
  "descartadas": [ { "id": "...", "title": "Chofer lavador", "url": "..." } ]
}
```

Las que siguen `pendiente` cuando vuelves a filtrar se pasan al nuevo `analisis.json`; las ya
analizadas solo quedan en `jobs.json` (con su score y apply).

## Configuración

### Área del filtro

Se define con variables de entorno (o `--area` para una sola corrida):

- `FILTRO_AREA`: texto libre con el área. Es el contexto que recibe Ollama para decidir.
  Default: `Informática y tecnología: desarrollo de software, BI, datos, QA/testing, DevOps`
- `FILTRO_CRITERIO` (opcional): para afinar, por ejemplo `"excluir soporte presencial nivel 1"`.

```bash
FILTRO_AREA="Ingeniería industrial: procesos, calidad, manufactura" python filtrar_ofertas.py
```

Para no escribirlo cada vez, cambia el default en `filtrar_ofertas.py` o agrégalo a tu
`~/.bashrc`: `export FILTRO_AREA="..."`.

Si cambias el área, vuelve a clasificar lo ya filtrado con `python filtrar_ofertas.py --refiltrar`.

### CV

Pon tu CV en PDF dentro de `cv/` (está en `.gitignore`, no se sube a GitHub).
Se usa el primer PDF de la carpeta, o el que indiques con `--cv ruta.pdf`.

### Variables de entorno

En Git Bash se ponen delante del comando: `FILTRO_LOTE=10 python filtrar_ofertas.py`

| Variable | Default | Para qué |
|---|---|---|
| `OLLAMA_MODEL` | `qwen3:latest` | modelo de Ollama (o `--model`) |
| `OLLAMA_URL` | `http://localhost:11434` | dónde corre Ollama |
| `OLLAMA_TIMEOUT` | `900` | segundos máximos de espera por llamada |
| `OLLAMA_NUM_CTX` | `8192` | contexto del modelo (CV + descripción) |
| `FILTRO_AREA` | Informática y tecnología… | área del filtro |
| `FILTRO_CRITERIO` | vacío | criterio adicional del filtro |
| `FILTRO_LOTE` | `15` | ofertas por llamada en el filtro |
| `APPLY_MIN` | `65` | score mínimo para `apply: true` |
| `EMPLEO_MAX_PAGES` | `0` (todas) | páginas máximas de empleo.cr |
| `TELEGRAM_PAGES` | `2` | páginas hacia atrás por canal (~20 mensajes c/u) |
| `MAX_WORKERS` | `4` | fuentes recolectadas en paralelo |
| `MAX_AGE_DAYS` | `60` | borra de `jobs.json` las descartadas/analizadas más viejas que esto |

`APPLY_MIN` decide `apply` al analizar. En `--ranking` se recalcula la marca APLICAR con el
umbral actual: `APPLY_MIN=75 python analizar_ofertas.py --ranking`

## Opciones de cada script

**`recolector.py`**
- `--sources cr,latam,empleo`: solo algunas fuentes
- `--list`: ver las fuentes disponibles
- `--dry-run`: probar sin guardar nada

**`filtrar_ofertas.py`**
- `--area "..."`: usar otra área solo en esta corrida
- `--refiltrar`: volver a clasificar las `descartado` y `en_cola`
- `--model ...`

**`analizar_ofertas.py`**
- `--limit N`: analizar solo las N pendientes más nuevas
- `--ranking`: mostrar en consola el ranking de `analisis.json`
- `--top N`: cuántas mostrar en el ranking (default 15)
- `--reanalizar-ceros`: devolver a pendiente las que quedaron con score 0
- `--cv ruta.pdf`, `--model ...`

## Fuentes (páginas)

Cada página es un archivo en `fuentes/`. El recolector y el analizador los detectan solos.

| key | Archivo | Página |
|---|---|---|
| `empleo` | `fuentes/empleo_cr.py` | empleo.cr (con Playwright, ordenado por más nuevas) |
| `cr` | `fuentes/telegram.py` | t.me/s/stemjobscr |
| `latam` | `fuentes/telegram.py` | t.me/s/stemjobslatam |

**Agregar otro canal de Telegram:** sumar una línea a `CANALES` en `fuentes/telegram.py`.

**Agregar otra página:**
1. Copiar `fuentes/_plantilla.py` como `fuentes/mi_pagina.py` (sin el guion bajo).
2. Ajustar `key`, `source`, `label`, `region` y escribir `scrape()`.
3. En `description_selectors`, poner los selectores CSS donde está la descripción en la
   página de detalle de la oferta (los usa el analizador).
4. Probar: `python recolector.py --sources mi_pagina --dry-run`

Los archivos que empiezan con `_` no se cargan.

## GitHub Actions (opcional)

`.github/workflows/recolector.yml` corre el recolector cada 10 minutos entre las 6:00 y las
22:00 hora de Costa Rica y hace commit de `data/jobs.json` si hay ofertas nuevas.

- La primera vez, lánzalo a mano desde la pestaña **Actions** con `empleo_max_pages = 0`
  para traer los 30 días completos. Después revisa 3 páginas por corrida.
- En un repo **privado**, ojo con los 2.000 minutos gratis al mes; en uno público no hay límite.
- El análisis nunca corre en GitHub: `cv/` y `resultados/` están en `.gitignore`.
- **Importante:** como filtrar y analizar cambian `status` en `data/jobs.json`, ese archivo lo
  modifican tanto GitHub como tu PC. Para no tener conflictos en `git pull`, sube tus cambios
  apenas termines:
  ```bash
  git pull
  python filtrar_ofertas.py && python analizar_ofertas.py --limit 20
  git commit -am "estado de ofertas" && git push
  ```

## Instalación

```bash
pip install -r requirements.txt
python -m playwright install chromium
ollama pull qwen3:latest
```

## Problemas comunes

| Síntoma | Causa probable | Qué hacer |
|---|---|---|
| `ReadTimeout` en el filtro o el análisis | el modelo tarda más que `OLLAMA_TIMEOUT` (sobre todo con razonamiento) | bajar `FILTRO_LOTE`, subir `OLLAMA_TIMEOUT` o usar un modelo más chico |
| Cada oferta tarda varios minutos | Ollama corre en CPU | revisar `ollama ps`; si no dice GPU, usar `qwen3:4b` |
| Muchos `0/100` | descripción vacía o equivocada | mirar `desc N car.` en la salida; si es muy bajo, revisar `description_selectors` de la fuente; luego `--reanalizar-ceros` |
| El filtro descarta ofertas que sí te sirven | el área está muy cerrada | ampliar `FILTRO_AREA` y correr `--refiltrar` |
| `No hay ofertas pendientes` | no corriste el paso 1 o ya analizaste todo | `python filtrar_ofertas.py` |
| `analisis.json ... formato anterior` | archivo de una versión vieja | `python filtrar_ofertas.py` lo regenera |
| `Ollama no responde` | Ollama cerrado | abrir Ollama o correr `ollama serve` |
| empleo.cr se detiene en la página 1 | normal: esa página ya era conocida y las ofertas vienen ordenadas por más nuevas | nada |
| `git pull` dice que `jobs.json` tiene cambios locales | filtraste/analizaste sin subir | `git commit -am "estado" && git pull` |

## Limpieza

No hace falta limpiar nada a mano:

- `data/jobs.json` se limpia solo: borra las `descartado`/`analizado` con más de
  `MAX_AGE_DAYS` días. Las `nuevo` y `en_cola` nunca se borran.
- `resultados/analisis.json` se reemplaza en cada filtrado.
- Para volver a evaluar todo con otra área: `python filtrar_ofertas.py --refiltrar`.
