"""
Revisión de ortografía y redacción de los campos de texto libre, y utilidades
para comparar textos de forma aproximada (por términos, no literal).

Enfoque (sin dependencias externas ni modelos):
  1. Se construye un léxico de referencia = léxico base de español/comercio exterior
     + todas las palabras de la factura comercial (la fuente de verdad).
  2. Cada palabra de la declaración que no esté en el léxico se compara con las
     palabras del léxico por similitud (difflib). Si hay una muy parecida, se
     reporta con la sugerencia; si sólo cambian las tildes, se clasifica como
     "acentuacion", si no, como "error_tipografico".
  3. Redacción: se listan los términos con contenido de la factura que la
     declaración omite (p. ej. modelo o potencia), como observación de baja severidad.

En producción esto se reemplazaría por un corrector real (hunspell/LanguageTool)
o por un LLM con instrucciones de "sólo reportar, nunca corregir", pero el
contrato de entrada/salida de este módulo se mantendría.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable

from .normalizacion import quitar_acentos

# Léxico base (escrito correctamente, con tildes). Se amplía con el vocabulario de la factura.
LEXICO_BASE = set("""
a al ante bajo con contra de del desde durante en entre hacia hasta mediante para por según sin sobre tras
el la los las un una unos unas y o u e ni que se su sus lo como más menos
mercancía mercancías motor motores eléctrico eléctrica eléctricos eléctricas trifásico trifásica trifásicos trifásicas
monofásico monofásicos bifásico potencia potencias baja bajo alta alto media medio uso usos industrial industriales
doméstico domésticos comercial comerciales
unidad unidades caja cajas bulto bultos cartón cartones paquete paquetes pieza piezas juego juegos
peso pesos bruto neto kilogramo kilogramos kilo kilos tonelada toneladas
valor valores total totales flete fletes seguro seguros precio precios unitario unitarios subtotal
factura facturas fecha número exportador importador país origen embarque destino puerto moneda negociación forma pago
descripción cantidad cantidades incoterm incoterms modelo marca referencia serie tipo clase material materiales
nuevo nueva nuevos nuevas usado usada usados usadas
acero aluminio plástico hierro cobre madera vidrio textil
""".split())

PALABRAS_VACIAS = {"de", "la", "el", "los", "las", "y", "o", "a", "en", "con", "por", "para", "del", "al",
                   "un", "una", "uso", "the", "of", "and"}

_PATRON_PALABRA = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+")
_PATRON_TERMINO = re.compile(r"[\wÁÉÍÓÚÑáéíóúñ][\w.\-ÁÉÍÓÚÑáéíóúñ]*")   # incluye "0.75kW" y "PTM-075T"


@dataclass
class HallazgoOrtografia:
    palabra: str
    sugerencia: str | None
    subtipo: str          # acentuacion | error_tipografico
    similitud: float


def palabras(texto: str | None) -> list[str]:
    return _PATRON_PALABRA.findall(texto or "")


def terminos(texto: str | None) -> list[str]:
    """Tokens con contenido (incluye códigos como PTM-075T y cifras como 0.75kW)."""
    return [t.strip(".,") for t in _PATRON_TERMINO.findall(texto or "") if t.strip(".,")]


def construir_lexico(vocabulario_referencia: Iterable[str]) -> set[str]:
    lexico = set(LEXICO_BASE)
    lexico.update(p.lower() for p in vocabulario_referencia)
    return lexico


def vocabulario_de(textos: Iterable[str | None]) -> set[str]:
    """Conjunto de palabras (minúsculas) presentes en una colección de textos."""
    vocab: set[str] = set()
    for t in textos:
        vocab.update(p.lower() for p in palabras(t))
    return vocab


def _similitud(a: str, b: str) -> float:
    return SequenceMatcher(None, quitar_acentos(a), quitar_acentos(b)).ratio()


def _mas_parecida(palabra: str, lexico: set[str]) -> tuple[str | None, float]:
    mejor, mejor_ratio = None, 0.0
    for candidata in lexico:
        if abs(len(candidata) - len(palabra)) > 3:
            continue
        r = _similitud(palabra, candidata)
        if r > mejor_ratio:
            mejor, mejor_ratio = candidata, r
    return mejor, mejor_ratio


def _es_variante_morfologica(palabra: str, sugerencia: str) -> bool:
    """'motor' frente a 'motores': no es un error, es singular/plural."""
    return palabra + "s" == sugerencia or palabra + "es" == sugerencia or \
        sugerencia + "s" == palabra or sugerencia + "es" == palabra


def revisar_texto(texto: str | None, lexico: set[str], umbral: float = 0.8) -> list[HallazgoOrtografia]:
    """Devuelve los hallazgos de ortografía de un campo de texto."""
    hallazgos: list[HallazgoOrtografia] = []
    for i, palabra in enumerate(palabras(texto)):
        if len(palabra) < 3 or palabra.isupper():          # siglas y partículas muy cortas
            continue
        if i > 0 and palabra[0].isupper():                  # nombre propio (no al inicio de la frase)
            continue
        w = palabra.lower()
        if w in lexico:
            continue
        sugerencia, ratio = _mas_parecida(w, lexico)
        if sugerencia is None or ratio < umbral or _es_variante_morfologica(w, sugerencia):
            continue
        subtipo = "acentuacion" if quitar_acentos(w) == quitar_acentos(sugerencia) else "error_tipografico"
        hallazgos.append(HallazgoOrtografia(palabra, sugerencia, subtipo, round(ratio, 3)))
    return hallazgos


# ---------------------------------------------------------------------------
# Comparación aproximada de textos (usada por la casilla "Descripción")
# ---------------------------------------------------------------------------

def _tiene_equivalente(termino: str, candidatos: list[str], umbral: float) -> bool:
    t = quitar_acentos(termino.lower())
    return any(SequenceMatcher(None, t, quitar_acentos(c.lower())).ratio() >= umbral for c in candidatos)


def cobertura_terminos(texto: str | None, referencia: str | None, umbral: float = 0.8) -> float:
    """Fracción de términos con contenido del texto que aparecen (aproximadamente) en la referencia."""
    propios = [t for t in terminos(texto) if t.lower() not in PALABRAS_VACIAS]
    if not propios:
        return 0.0
    ref = terminos(referencia)
    encontrados = sum(1 for t in propios if _tiene_equivalente(t, ref, umbral))
    return encontrados / len(propios)


def terminos_omitidos(texto: str | None, referencia: str | None, umbral: float = 0.8) -> list[str]:
    """Términos con contenido de la referencia que el texto no menciona (ni aproximadamente)."""
    propios = terminos(texto)
    omitidos = []
    for t in terminos(referencia):
        if t.lower() in PALABRAS_VACIAS or len(t) < 2:
            continue
        if not _tiene_equivalente(t, propios, umbral):
            omitidos.append(t)
    return omitidos
