"""
Extracción: convierte cada PDF en un diccionario de Python con los datos crudos,
tal como aparecen escritos en el documento (la normalización viene después).

- La declaración se lee en modo "layout" de pypdf, que conserva las columnas
  (número | etiqueta | valor separadas por varios espacios). Cada casilla se
  captura con una expresión regular; si la línea no tiene valor, la casilla
  queda en None. También se lee la lista de documentos soporte (✔ / ✘) que
  aparece al pie, porque de ella depende qué casillas se pueden validar.

- La factura se lee en modo normal (una línea por renglón lógico). Los campos
  se ubican por "etiquetas ancla" (País de origen, Incoterm, Moneda, ...), la
  tabla de ítems por expresión regular y los totales por sus rótulos.
"""
from __future__ import annotations

import re
from pathlib import Path

from pypdf import PdfReader

from .normalizacion import extraer_nit, normalizar_texto


def leer_pdf(ruta: str | Path, modo: str = "plain") -> str:
    """Texto completo del PDF. modo='layout' conserva la disposición en columnas."""
    lector = PdfReader(str(ruta))
    paginas = []
    for pagina in lector.pages:
        if modo == "layout":
            paginas.append(pagina.extract_text(extraction_mode="layout"))
        else:
            paginas.append(pagina.extract_text())
    return "\n".join(paginas)


def _buscar(patron: str, texto: str, flags: int = 0) -> str | None:
    m = re.search(patron, texto, flags)
    return m.group(1).strip() if m else None


# ---------------------------------------------------------------------------
# Declaración de importación
# ---------------------------------------------------------------------------

# "     1       Número de factura comercial                    FC-2026-00147"
_CASILLA_CON_VALOR = re.compile(r"^\s*(\d{1,2})\s{2,}([A-Za-zÁÉÍÓÚÑáéíóúñ][^\n]*?)\s{2,}(\S[^\n]*?)\s*$")
# "    10       Número de bultos"  (sin valor)
_CASILLA_SIN_VALOR = re.compile(r"^\s*(\d{1,2})\s{2,}([A-Za-zÁÉÍÓÚÑáéíóúñ][^\n]*?)\s*$")
# "       ✔ Factura comercial — FC-2026-00417.pdf"
_CHECKLIST = re.compile(r"^\s*([✔✓☑✘✗☒×])\s*(.+?)\s+[—–-]\s+(.+?)\s*$")
_MARCAS_DISPONIBLE = {"✔", "✓", "☑"}

# Cómo se llama cada documento en la carpeta -> clave interna (ver reglas.DOCUMENTOS)
_CLAVES_DOCUMENTO = (
    ("factura comercial", "factura_comercial"),
    ("lista de empaque", "lista_empaque"),
    ("packing list", "lista_empaque"),
    ("certificado de origen", "certificado_origen"),
    ("documento de transporte", "documento_transporte"),
    ("conocimiento de embarque", "documento_transporte"),
    ("guia aerea", "documento_transporte"),
    ("bl", "documento_transporte"),
)


def _clave_documento(etiqueta: str) -> str:
    t = normalizar_texto(etiqueta)
    for patron, clave in _CLAVES_DOCUMENTO:
        if re.search(rf"\b{patron}\b", t):
            return clave
    return re.sub(r"\s+", "_", t)


def extraer_declaracion(ruta: str | Path) -> dict:
    texto = leer_pdf(ruta, modo="layout")
    casillas: dict[int, dict] = {}
    documentos: dict[str, dict] = {}
    abierta: dict | None = None      # última casilla leída, por si su valor continúa en la línea siguiente
    columna_valor = 0                # posición horizontal donde empieza la columna de valores

    for linea in texto.splitlines():
        m = _CASILLA_CON_VALOR.match(linea)
        if m:
            n = int(m.group(1))
            casillas[n] = {"numero": n, "etiqueta": m.group(2).strip(), "valor": m.group(3).strip()}
            abierta, columna_valor = casillas[n], m.start(3)
            continue
        m = _CASILLA_SIN_VALOR.match(linea)
        if m:
            n = int(m.group(1))
            casillas[n] = {"numero": n, "etiqueta": m.group(2).strip(), "valor": None}
            abierta = None
            continue
        m = _CHECKLIST.match(linea)
        if m:
            documentos[_clave_documento(m.group(2))] = {
                "etiqueta": m.group(2).strip(),
                "disponible": m.group(1) in _MARCAS_DISPONIBLE,
                "detalle": m.group(3).strip(),
            }
            abierta = None
            continue
        if not linea.strip():
            continue
        # Un valor largo que ocupó dos renglones: la continuación aparece alineada con la columna de valores
        sangria = len(linea) - len(linea.lstrip())
        if abierta is not None and sangria >= columna_valor - 2:
            abierta["valor"] = f"{abierta['valor']} {linea.strip()}"
            continue
        abierta = None   # cualquier otra línea (título de sección, pie) cierra la casilla

    lineas = [l.strip() for l in texto.splitlines() if l.strip()]
    encabezado = {
        "titulo": lineas[0] if lineas else None,
        "declarante": re.split(r"\s*[—–|]\s*", lineas[1])[0].strip() if len(lineas) > 1 else None,
        "nit_declarante": extraer_nit(texto),
        "referencia_interna": _buscar(r"Referencia interna:?\s*([A-Z0-9\-]+)", texto),
        "estado": _buscar(r"Estado:\s*([^\n]+?)\s*$", texto, re.M),
    }
    return {
        "archivo": str(ruta),
        "encabezado": encabezado,
        "casillas": casillas,
        "documentos_soporte": documentos,
        "texto": texto,
    }


# ---------------------------------------------------------------------------
# Factura comercial
# ---------------------------------------------------------------------------

_ETIQUETAS_CONDICIONES = ["País de origen", "País de embarque", "Incoterm", "Puerto de destino", "Moneda", "Forma de pago"]

# "1 Motores eléctricos ... modelo PTM-075T 500 unidades 36.90 18,450.00"
_ITEM = re.compile(
    r"^(\d+)\s+(.+?)\s+(\d[\d.,]*)\s*(unidades|unidad|units|pcs|piezas|pieces|und|sets?|kg)\s+([\d.,]+)\s+([\d.,]+)\s*$",
    re.I,
)


def _bloque(texto: str, inicio: str, fin: str) -> dict | None:
    """Líneas entre dos rótulos (p. ej. entre 'EXPORTADOR / SHIPPER' e 'IMPORTADOR / CONSIGNEE')."""
    m_ini = re.search(inicio, texto)
    if not m_ini:
        return None
    resto = texto[m_ini.end():]
    m_fin = re.search(fin, resto)
    bloque = resto[:m_fin.start()] if m_fin else resto
    lineas = [l.strip() for l in bloque.splitlines() if l.strip()]
    if not lineas:
        return None
    return {"nombre": lineas[0], "nit": extraer_nit(bloque), "direccion": lineas[1:], "texto": " ".join(lineas)}


def _campos_anclados(texto: str, etiquetas: list[str], fin: str | None = None) -> dict[str, str | None]:
    """
    Para líneas como 'Incoterm CIF Cartagena, Colombia (Incoterms 2020) Puerto de destino Cartagena, Colombia':
    ubica cada etiqueta y toma como valor el texto hasta la siguiente etiqueta.
    """
    segmento = texto
    if fin:
        m = re.search(fin, texto)
        if m:
            segmento = texto[:m.start()]
    posiciones = []
    for etiqueta in etiquetas:
        m = re.search(r"(?<![A-Za-z])" + re.escape(etiqueta) + r"(?![a-z])", segmento)
        if m:
            posiciones.append((m.start(), m.end(), etiqueta))
    posiciones.sort()
    resultado: dict[str, str | None] = {e: None for e in etiquetas}
    for i, (_, fin_etiqueta, etiqueta) in enumerate(posiciones):
        siguiente = posiciones[i + 1][0] if i + 1 < len(posiciones) else len(segmento)
        valor = re.sub(r"\s+", " ", segmento[fin_etiqueta:siguiente]).strip(" :")
        resultado[etiqueta] = valor or None
    return resultado


def _extraer_items(texto: str) -> list[dict]:
    lineas = texto.splitlines()
    ini = next((i for i, l in enumerate(lineas) if re.search(r"Descripci[oó]n de la mercanc[ií]a", l)), None)
    fin = next((i for i, l in enumerate(lineas) if re.search(r"Valor FOB", l)), len(lineas))
    if ini is None:
        return []
    items: list[dict] = []
    pendiente = ""
    for linea in lineas[ini + 1:fin]:
        linea = linea.strip()
        if not linea:
            continue
        if re.match(r"^\d+\s", linea):      # empieza un ítem nuevo
            pendiente = ""
        candidata = f"{pendiente} {linea}".strip() if pendiente else linea
        m = _ITEM.match(candidata)
        if m:
            items.append({
                "item": int(m.group(1)),
                "descripcion": m.group(2).strip(),
                "cantidad": m.group(3),
                "unidad": m.group(4).lower(),
                "precio_unitario": m.group(5),
                "subtotal": m.group(6),
            })
            pendiente = ""
        else:
            pendiente = candidata            # descripción partida en varias líneas
    return items


def extraer_factura(ruta: str | Path) -> dict:
    texto = leer_pdf(ruta)
    condiciones = _campos_anclados(texto, _ETIQUETAS_CONDICIONES, fin=r"\n\s*[IÍ]tem\b")
    items = _extraer_items(texto)

    unidades = {it["unidad"] for it in items}
    if items and len(unidades) == 1:
        total = sum(int(re.sub(r"\D", "", it["cantidad"]) or 0) for it in items)
        cantidad = f"{total} {items[0]['unidad']}"
    else:
        cantidad = "; ".join(f"{it['cantidad']} {it['unidad']}" for it in items) or None

    return {
        "archivo": str(ruta),
        "numero": _buscar(r"(?:COMMERCIAL\s+INVOICE|FACTURA\s+COMERCIAL)\s*(?:No\.?|N[º°]|#)?\s*([A-Z0-9][A-Z0-9\-/]*\d)", texto, re.I),
        "fecha": _buscar(r"Fecha de emisi[oó]n:?\s*([^\n]+)", texto) or _buscar(r"(?:Date|Fecha):?\s*([^\n]+)", texto),
        "exportador": _bloque(texto, r"EXPORTADOR\s*/\s*SHIPPER|SHIPPER|EXPORTADOR", r"IMPORTADOR\s*/\s*CONSIGNEE|CONSIGNEE|IMPORTADOR"),
        "importador": _bloque(texto, r"IMPORTADOR\s*/\s*CONSIGNEE|CONSIGNEE|IMPORTADOR", r"Pa[ií]s de origen"),
        "pais_origen": condiciones["País de origen"],
        "pais_embarque": condiciones["País de embarque"],
        "incoterm": condiciones["Incoterm"],
        "puerto_destino": condiciones["Puerto de destino"],
        "moneda": condiciones["Moneda"],
        "forma_pago": condiciones["Forma de pago"],
        "items": items,
        "descripcion": " | ".join(it["descripcion"] for it in items) or None,
        "cantidad": cantidad,
        "fob": _buscar(r"Valor FOB\s*(?:[A-Z]{3})?\s*([\d.,]+)", texto),
        "flete": _buscar(r"Flete(?:\s+internacional)?\s*(?:[A-Z]{3})?\s*([\d.,]+)", texto),
        "seguro": _buscar(r"Seguro\s*(?:[A-Z]{3})?\s*([\d.,]+)", texto),
        "cif": _buscar(r"Valor CIF(?:\s+total)?\s*(?:[A-Z]{3})?\s*([\d.,]+)", texto),
        "bultos": _buscar(r"(\d+)\s+(?:cajas|bultos|cartones|paquetes|pallets|estibas)", texto, re.I),
        "unidades_por_bulto": _buscar(r"(\d+)\s+unidades\s+por\s+(?:caja|bulto)", texto, re.I),
        "peso_bruto": _buscar(r"peso bruto(?:\s+total)?[:\s]*([\d.,]+)\s*kg", texto, re.I),
        "remite_lista_empaque": bool(re.search(r"lista de empaque|packing list", texto, re.I)),
        # nota de embalaje; la continuación se toma sólo si no es el pie de página
        "observaciones": _buscar(r"(Mercanc[ií]a embalada[^\n]*(?:\n(?![^\n]*(?:P[áa]gina|Invoice))[^\n]*)?)", texto),
        "texto": texto,
    }
