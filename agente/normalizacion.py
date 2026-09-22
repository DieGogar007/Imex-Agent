"""
Normalización de valores extraídos de los documentos.

Cada documento escribe los mismos datos de forma distinta: la declaración usa
formato colombiano ("18.540,00") y la factura formato anglosajón ("18,450.00");
la factura dice "República Popular China" donde la declaración podría decir
"China"; la factura escribe "CIF Cartagena, Colombia (Incoterms 2020)" y la
declaración sólo "CIF Cartagena, Colombia".

Antes de comparar, ambos lados se llevan a una forma canónica. Así el contraste
es justo: sólo se alerta cuando el *significado* difiere, no cuando difiere la
forma de escribirlo (esto evita falsos positivos).
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation

# ---------------------------------------------------------------------------
# Texto
# ---------------------------------------------------------------------------

def quitar_acentos(texto: str) -> str:
    """'eléctricos' -> 'electricos'."""
    descompuesto = unicodedata.normalize("NFD", texto)
    return "".join(c for c in descompuesto if unicodedata.category(c) != "Mn")


def normalizar_texto(texto: str | None) -> str:
    """Minúsculas, sin acentos, sin puntuación y con espacios simples."""
    if not texto:
        return ""
    t = quitar_acentos(texto).lower()
    t = re.sub(r"[^\w\s]", " ", t)  # puntuación -> espacio
    t = t.replace("_", " ")
    return re.sub(r"\s+", " ", t).strip()


VALORES_VACIOS = {"", "sin diligenciar", "n a", "na", "null", "none", "pendiente", "vacio"}


def es_valor_vacio(valor: str | None) -> bool:
    """True si la casilla está en blanco o contiene un marcador de 'sin diligenciar'."""
    if valor is None:
        return True
    return normalizar_texto(valor) in VALORES_VACIOS


# ---------------------------------------------------------------------------
# Números y montos
# ---------------------------------------------------------------------------

_PATRON_NUMERO = re.compile(r"-?\d[\d.,\s]*")


def _a_decimal(texto: str) -> Decimal | None:
    try:
        return Decimal(texto)
    except InvalidOperation:
        return None


def normalizar_numero(valor) -> Decimal | None:
    """
    Convierte '18.540,00', '18,450.00', 'USD 620.00', '1.250,00 kg' o '500 unidades'
    en un Decimal. Regla para decidir el separador decimal:
      - el último separador (punto o coma) es el decimal si lo siguen 1 o 2 dígitos;
      - si lo siguen exactamente 3 dígitos y sólo hay un tipo de separador
        ("1.250", "18,450"), se interpreta como separador de miles.
    """
    if valor is None:
        return None
    if isinstance(valor, (Decimal, int, float)):
        return Decimal(str(valor))
    m = _PATRON_NUMERO.search(str(valor))
    if not m:
        return None
    crudo = re.sub(r"\s+", "", m.group(0)).rstrip(".,")
    ultimo_punto, ultima_coma = crudo.rfind("."), crudo.rfind(",")
    if ultimo_punto == -1 and ultima_coma == -1:
        return _a_decimal(crudo)
    if ultimo_punto > ultima_coma:
        sep_decimal, sep_miles, pos = ".", ",", ultimo_punto
    else:
        sep_decimal, sep_miles, pos = ",", ".", ultima_coma
    fraccion = crudo[pos + 1:]
    if len(fraccion) == 3 and sep_miles not in crudo:
        # "1.250" / "1.250.000" / "18,450": separador de miles, sin decimales
        return _a_decimal(crudo.replace(sep_decimal, ""))
    entero = crudo[:pos].replace(sep_miles, "").replace(sep_decimal, "")
    return _a_decimal(f"{entero}.{fraccion}")


def formatear_monto(valor: Decimal | None, decimales: int = 2) -> str:
    """Decimal('18540') -> '18.540,00' (formato colombiano, para mostrar en los detalles)."""
    if valor is None:
        return "—"
    q = valor.quantize(Decimal(10) ** -decimales)
    s = f"{q:,.{decimales}f}"  # 18,540.00
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


UNIDADES = {
    "unidades": ("unidad", "unidades", "und", "un", "u", "units", "unit", "pcs", "pc", "piezas", "pieza", "pieces", "piece", "uds", "ud"),
    "kg": ("kg", "kgs", "kilogramo", "kilogramos", "kilo", "kilos"),
    "cajas": ("caja", "cajas", "bulto", "bultos", "carton", "cartones", "box", "boxes"),
    "juegos": ("juego", "juegos", "set", "sets", "kit", "kits"),
}
_ALIAS_A_UNIDAD = {alias: unidad for unidad, aliases in UNIDADES.items() for alias in aliases}


def normalizar_unidad(unidad: str | None) -> str | None:
    """'und' / 'pcs' / 'unidad' -> 'unidades'; 'kgs' -> 'kg'. Si no se conoce, devuelve el texto normalizado."""
    if not unidad:
        return None
    t = normalizar_texto(unidad)
    return _ALIAS_A_UNIDAD.get(t, t or None)


def extraer_unidad(valor: str | None) -> str | None:
    """'500 unidades' -> 'unidades'; '500 und.' -> 'unidades'; '1.250,00 kg' -> 'kg'."""
    if not valor:
        return None
    m = re.search(r"\d[\d.,\s]*\s*([A-Za-zÁÉÍÓÚÑáéíóúñ]+)", valor)
    return normalizar_unidad(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Fechas
# ---------------------------------------------------------------------------

MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


def normalizar_fecha(valor: str | None) -> date | None:
    """'15 de junio de 2026', 'June 15, 2026', '2026-06-15' o '15/06/2026' -> date."""
    if not valor:
        return None
    crudo = str(valor)
    t = normalizar_texto(crudo)
    try:
        m = re.search(r"(\d{1,2}) de ([a-z]+) de (\d{4})", t)
        if m and m.group(2) in MESES:
            return date(int(m.group(3)), MESES[m.group(2)], int(m.group(1)))
        m = re.search(r"([a-z]+) (\d{1,2}) (\d{4})", t)
        if m and m.group(1) in MESES:
            return date(int(m.group(3)), MESES[m.group(1)], int(m.group(2)))
        m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", crudo)
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = re.search(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", crudo)
        if m:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None
    return None


# ---------------------------------------------------------------------------
# Moneda
# ---------------------------------------------------------------------------

CODIGOS_MONEDA = {"USD", "EUR", "COP", "CNY", "GBP", "JPY", "KRW", "MXN", "BRL", "CHF", "CAD", "PEN", "CLP"}
NOMBRES_MONEDA = {
    "dolares de los estados unidos": "USD", "dolar estadounidense": "USD", "dolares": "USD",
    "dolar": "USD", "us dollar": "USD", "dollar": "USD",
    "euros": "EUR", "euro": "EUR",
    "pesos colombianos": "COP", "peso colombiano": "COP",
    "yuan": "CNY", "renminbi": "CNY", "libra": "GBP", "yen": "JPY",
}


def normalizar_moneda(valor: str | None) -> str | None:
    """'Dólares de los Estados Unidos (USD)' -> 'USD'; 'USD' -> 'USD'."""
    if not valor:
        return None
    m = re.search(r"\b([A-Z]{3})\b", valor)
    if m and m.group(1) in CODIGOS_MONEDA:
        return m.group(1)
    t = normalizar_texto(valor)
    for nombre, codigo in NOMBRES_MONEDA.items():
        if nombre in t:
            return codigo
    return valor.strip().upper() or None


# ---------------------------------------------------------------------------
# Incoterm
# ---------------------------------------------------------------------------

TERMINOS_INCOTERM = ("EXW", "FCA", "FAS", "FOB", "CFR", "CIF", "CPT", "CIP",
                     "DAP", "DPU", "DDP", "DAT", "DAF", "DES", "DEQ", "DDU")


def normalizar_incoterm(valor: str | None) -> dict | None:
    """
    'CIF Cartagena, Colombia (Incoterms 2020)' ->
    {'termino': 'CIF', 'lugar': 'cartagena colombia', 'version': '2020'}
    La versión de las reglas no forma parte de la comparación.
    """
    if not valor:
        return None
    version = re.search(r"incoterms?\s*(\d{4})", valor, re.I)
    sin_version = re.sub(r"\(?\s*incoterms?\s*\d{4}\s*\)?", " ", valor, flags=re.I)
    m = re.search(r"\b(" + "|".join(TERMINOS_INCOTERM) + r")\b", sin_version.upper())
    if not m:
        return {"termino": None, "lugar": normalizar_texto(sin_version), "version": version.group(1) if version else None}
    lugar = normalizar_texto(sin_version[:m.start()] + " " + sin_version[m.end():])
    return {"termino": m.group(1), "lugar": lugar, "version": version.group(1) if version else None}


# ---------------------------------------------------------------------------
# País
# ---------------------------------------------------------------------------

# Alias (ya normalizados: minúsculas, sin acentos) -> código ISO 3166-1 alfa-2.
# En producción esta tabla vendría de una lista maestra (p. ej. la tabla de países de la DIAN).
PAISES = {
    "CN": ("china", "republica popular china", "republica popular de china", "people s republic of china", "pr china", "prc"),
    "KR": ("corea del sur", "republica de corea", "south korea", "korea republic of", "korea"),
    "KP": ("corea del norte", "north korea", "republica popular democratica de corea"),
    "CO": ("colombia", "republica de colombia"),
    "US": ("estados unidos", "estados unidos de america", "united states", "united states of america", "usa", "eeuu", "ee uu"),
    "MX": ("mexico", "estados unidos mexicanos"),
    "DE": ("alemania", "germany"), "JP": ("japon", "japan"), "IN": ("india",),
    "BR": ("brasil", "brazil"), "ES": ("espana", "spain"), "IT": ("italia", "italy"),
    "TW": ("taiwan",), "VN": ("vietnam", "viet nam"), "TH": ("tailandia", "thailand"),
    "PE": ("peru",), "CL": ("chile",), "EC": ("ecuador",), "PA": ("panama",),
    "CA": ("canada",), "FR": ("francia", "france"), "GB": ("reino unido", "united kingdom", "uk", "gran bretana"),
    "NL": ("paises bajos", "netherlands", "holanda"), "TR": ("turquia", "turkey"),
    "HK": ("hong kong",), "SG": ("singapur", "singapore"), "MY": ("malasia", "malaysia"), "ID": ("indonesia",),
}
_ALIAS_A_ISO = {alias: iso for iso, aliases in PAISES.items() for alias in aliases}


def normalizar_pais(valor: str | None) -> str | None:
    """'República Popular China' -> 'CN'; 'Corea del Sur' -> 'KR'. Si no se conoce, devuelve el texto normalizado."""
    if not valor:
        return None
    t = normalizar_texto(valor)
    if t in _ALIAS_A_ISO:
        return _ALIAS_A_ISO[t]
    if len(t) == 2 and t.upper() in PAISES:
        return t.upper()
    return t or None


# ---------------------------------------------------------------------------
# Empresas y NIT
# ---------------------------------------------------------------------------

def extraer_nit(texto: str | None) -> str | None:
    """'... NIT 900.123.456-7' -> '9001234567' (sólo dígitos)."""
    if not texto:
        return None
    m = re.search(r"NIT\.?\s*[:\-]?\s*(\d[\d.\-\s]*\d)", texto, re.I)
    return re.sub(r"\D", "", m.group(1)) if m else None


def normalizar_empresa(texto: str | None) -> str:
    """'Shenzhen PowerTech Motors Co., Ltd.' -> 'shenzhen powertech motors co ltd'."""
    t = normalizar_texto(texto)
    t = re.sub(r"\b(s a s|sas)\b", "sas", t)
    t = re.sub(r"\b(s a|sa)\b", "sa", t)
    t = re.sub(r"\b(co ltd|company limited|co limited)\b", "co ltd", t)
    return t.strip()


def separar_empresa_nit(texto: str | None) -> tuple[str, str | None]:
    """'Distribuidora Andina S.A.S. — NIT 900.123.456-7' -> ('Distribuidora Andina S.A.S.', '9001234567')."""
    if not texto:
        return "", None
    nombre = re.split(r"\s*[—–|]\s*|\s*\bNIT\b", texto, maxsplit=1, flags=re.I)[0]
    return nombre.strip(" ,"), extraer_nit(texto)
