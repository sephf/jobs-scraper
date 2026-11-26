"""
Fuentes de ofertas. Cada archivo .py de esta carpeta es una página.

Agregar una página nueva:
  1. Copia _plantilla.py como mi_pagina.py
  2. Cambia key / source / label / region y escribe scrape()
  3. Listo: el recolector y el analizador la detectan solos.

Los archivos que empiezan con "_" no se cargan (plantillas, borradores).
"""
import importlib
import pkgutil

from .base import _REGISTRY, Fuente

for _mod in pkgutil.iter_modules(__path__):
    if not _mod.name.startswith("_") and _mod.name != "base":
        importlib.import_module(f"{__name__}.{_mod.name}")


def todas() -> dict[str, type[Fuente]]:
    """key -> clase, en orden alfabético."""
    return dict(sorted(_REGISTRY.items()))


def por_source(source: str) -> type[Fuente] | None:
    """Busca la clase por el valor 'source' guardado en el JSON."""
    return next((c for c in _REGISTRY.values() if c.source == source), None)
