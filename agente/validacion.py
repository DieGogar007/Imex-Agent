"""
Motor de validación: recorre el catálogo de casillas, decide con qué documento
se puede validar cada una, compara y genera las alertas.

Política de incertidumbre (lo que hace el agente cuando NO puede validar):
  - Si la casilla está vacía y es obligatoria         -> campo_faltante
      (si algún documento disponible trae un valor de referencia, se incluye como ayuda).
  - Si ninguno de sus documentos soporte está disponible -> sin_documento_soporte
      (no se inventa un valor; se dice qué documento hace falta).
  - Si el soporte formal falta pero otro documento sí trae el dato (p. ej. la
    factura menciona el país de origen) -> se compara contra ese documento
    y ADEMÁS se emite sin_documento_soporte para dejar claro que la validación
    es provisional.
  - Si los valores no coinciden                         -> discrepancia_valor
  - Reglas entre casillas (CIF = FOB + flete + seguro)  -> inconsistencia_interna
  - Campos de texto libre                               -> ortografia
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from . import ortografia
from .lenguaje import AnalisisTexto, RevisorHeuristico, RevisorTexto
from .normalizacion import (
    es_valor_vacio, extraer_unidad, formatear_monto, normalizar_empresa, normalizar_fecha,
    normalizar_incoterm, normalizar_moneda, normalizar_numero, normalizar_pais, normalizar_texto,
    separar_empresa_nit,
)
from .reglas import (
    CATALOGO, CASILLAS_POR_NUMERO, DOCUMENTOS, EXTRACTORES_DISPONIBLES, REGLAS_CRUZADAS,
    SEVERIDAD_POR_TIPO, Casilla,
)

TOLERANCIA_MONTO = Decimal("0.005")


# ---------------------------------------------------------------------------
# Estructuras de salida
# ---------------------------------------------------------------------------

@dataclass
class Alerta:
    casilla: int | None
    campo: str
    tipo: str
    valor_declaracion: str | None
    valor_fuente: str | None
    fuente: str | None
    detalle: str
    sugerencia: str | None = None
    casillas_relacionadas: list[int] | None = None
    analisis: str | None = None          # quién analizó el texto: "heuristico" o "llm:<modelo>"

    @property
    def severidad(self) -> str:
        return SEVERIDAD_POR_TIPO[self.tipo]

    def a_dict(self) -> dict:
        d = {
            "casilla": self.casilla,
            "campo": self.campo,
            "tipo": self.tipo,
            "severidad": self.severidad,
            "valor_declaracion": self.valor_declaracion,
            "valor_fuente": self.valor_fuente,
            "fuente": self.fuente,
            "detalle": self.detalle,
        }
        if self.sugerencia is not None:
            d["sugerencia"] = self.sugerencia
        if self.casillas_relacionadas:
            d["casillas_relacionadas"] = self.casillas_relacionadas
        if self.analisis:
            d["analisis"] = self.analisis
        return d


@dataclass
class Resultado:
    """Resultado de comparar el valor de una casilla con el de su fuente."""
    coincide: bool
    declaracion_normalizada: str
    fuente_normalizada: str
    detalle: str = ""


# ---------------------------------------------------------------------------
# Comparadores por tipo de dato
# ---------------------------------------------------------------------------

def _mostrar(valor) -> str | None:
    """Representación legible de un valor de la factura (puede ser dict para empresas)."""
    if valor is None:
        return None
    if isinstance(valor, dict):
        nombre = valor.get("nombre") or ""
        return f"{nombre} — NIT {valor['nit']}" if valor.get("nit") else nombre
    return str(valor)


def _misma_composicion_de_digitos(a: str, b: str) -> bool:
    da, db = re.sub(r"\D", "", a), re.sub(r"\D", "", b)
    return da != db and sorted(da) == sorted(db)


def _cmp_identificador(d: str, f) -> Resultado:
    dn, fn = normalizar_texto(d).replace(" ", ""), normalizar_texto(str(f)).replace(" ", "")
    detalle = ""
    if dn != fn and _misma_composicion_de_digitos(dn, fn):
        detalle = "Los dígitos son los mismos en distinto orden: posible transposición al digitar."
    return Resultado(dn == fn, dn, fn, detalle)


def _cmp_fecha(d: str, f) -> Resultado:
    dn, fn = normalizar_fecha(d), normalizar_fecha(str(f))
    if dn is None or fn is None:
        return Resultado(False, str(dn), str(fn), "No fue posible interpretar la fecha en alguno de los documentos.")
    return Resultado(dn == fn, dn.isoformat(), fn.isoformat())


def _cmp_empresa(d: str, f) -> Resultado:
    nombre_f = f.get("nombre") if isinstance(f, dict) else str(f)
    dn, fn = normalizar_empresa(separar_empresa_nit(d)[0]), normalizar_empresa(nombre_f)
    return Resultado(dn == fn or dn in fn or fn in dn, dn, fn)


def _cmp_empresa_nit(d: str, f) -> Resultado:
    nombre_d, nit_d = separar_empresa_nit(d)
    if isinstance(f, dict):
        nombre_f, nit_f = f.get("nombre") or "", f.get("nit")
    else:
        nombre_f, nit_f = separar_empresa_nit(str(f))
    nombre_ok = _cmp_empresa(nombre_d, nombre_f).coincide
    nit_ok = (nit_d == nit_f) if (nit_d and nit_f) else True
    partes = []
    if not nombre_ok:
        partes.append("la razón social difiere")
    if not nit_ok:
        partes.append(f"el NIT difiere ({nit_d} vs {nit_f})")
    return Resultado(nombre_ok and nit_ok,
                     f"{normalizar_empresa(nombre_d)} | nit {nit_d}",
                     f"{normalizar_empresa(nombre_f)} | nit {nit_f}",
                     "; ".join(partes).capitalize() + "." if partes else "")


def _cmp_pais(d: str, f) -> Resultado:
    dn, fn = normalizar_pais(d), normalizar_pais(str(f))
    return Resultado(dn == fn, str(dn), str(fn))


def _cmp_incoterm(d: str, f) -> Resultado:
    dn, fn = normalizar_incoterm(d), normalizar_incoterm(str(f))
    mismo_termino = dn["termino"] == fn["termino"]
    mismo_lugar = dn["lugar"] == fn["lugar"] or dn["lugar"] in fn["lugar"] or fn["lugar"] in dn["lugar"]
    detalle = ""
    if not mismo_termino:
        detalle = f"El término difiere ({dn['termino']} vs {fn['termino']})."
    elif not mismo_lugar:
        detalle = "El lugar convenido difiere."
    return Resultado(mismo_termino and mismo_lugar,
                     f"{dn['termino']} {dn['lugar']}", f"{fn['termino']} {fn['lugar']}", detalle)


def _cmp_moneda(d: str, f) -> Resultado:
    dn, fn = normalizar_moneda(d), normalizar_moneda(str(f))
    return Resultado(dn == fn, str(dn), str(fn))


def _cmp_descripcion(d: str, f) -> Resultado:
    """Comparador básico (se usa cuando no hay revisor de texto; el motor normalmente pasa por el revisor)."""
    cobertura = ortografia.cobertura_terminos(d, str(f))
    detalle = f"{cobertura:.0%} de los términos de la declaración aparecen en la descripción de la factura."
    return Resultado(cobertura >= 0.6, normalizar_texto(d), normalizar_texto(str(f)), detalle)


def _resultado_desde_analisis(d: str, f, analisis: AnalisisTexto) -> Resultado:
    """Traduce el veredicto del revisor de texto (heurístico o LLM) al formato de los comparadores."""
    detalle = analisis.explicacion
    if analisis.origen.startswith("llm"):
        detalle = f"Según el modelo de lenguaje: {analisis.explicacion} (confianza {analisis.confianza:.0%})."
    return Resultado(analisis.compatible, normalizar_texto(d), normalizar_texto(str(f)), detalle)


def _cmp_numero(d: str, f) -> Resultado:
    dn, fn = normalizar_numero(d), normalizar_numero(str(f))
    if dn is None or fn is None:
        return Resultado(False, str(dn), str(fn), "No fue posible interpretar el número en alguno de los documentos.")
    coincide = abs(dn - fn) <= TOLERANCIA_MONTO
    detalle = ""
    if not coincide:
        detalle = f"Diferencia: {formatear_monto(dn - fn)}."
        if _misma_composicion_de_digitos(str(dn), str(fn)):
            detalle += " Los dígitos son los mismos en distinto orden: posible transposición al digitar."
    return Resultado(coincide, formatear_monto(dn), formatear_monto(fn), detalle)


def _cmp_cantidad(d: str, f) -> Resultado:
    r = _cmp_numero(d, f)
    ud, uf = extraer_unidad(d), extraer_unidad(str(f))
    if r.coincide and ud and uf and ud != uf:
        return Resultado(False, f"{r.declaracion_normalizada} {ud}", f"{r.fuente_normalizada} {uf}",
                         f"La unidad de medida difiere ({ud} vs {uf}).")
    return r


COMPARADORES = {
    "identificador": _cmp_identificador,  # cada comparador recibe (valor_declaracion, valor_fuente) y devuelve Resultado
    "fecha": _cmp_fecha,
    "empresa": _cmp_empresa,
    "empresa_nit": _cmp_empresa_nit,
    "pais": _cmp_pais,
    "incoterm": _cmp_incoterm,
    "moneda": _cmp_moneda,
    "descripcion": _cmp_descripcion,
    "cantidad": _cmp_cantidad,
    "entero": _cmp_numero,
    "peso": _cmp_numero,
    "monto": _cmp_numero,
}


def comparar(casilla: Casilla, valor_declaracion: str, valor_fuente) -> Resultado:
    return COMPARADORES[casilla.tipo](valor_declaracion, valor_fuente)


# ---------------------------------------------------------------------------
# Motor principal
# ---------------------------------------------------------------------------

def _nombres(claves) -> str:
    return ", ".join(DOCUMENTOS.get(c, c) for c in claves)


def _documentos_disponibles(declaracion: dict, factura: dict | None) -> dict[str, bool]:
    """Qué documentos hay en la carpeta. La factura cuenta como disponible si se pudo leer."""
    disponibles = {clave: False for clave in DOCUMENTOS}
    for clave, info in declaracion.get("documentos_soporte", {}).items():
        disponibles[clave] = bool(info.get("disponible"))
    if factura is not None:
        disponibles["factura_comercial"] = True
    return disponibles


def _valor_en_factura(factura: dict | None, casilla: Casilla):
    if factura is None or not casilla.campo_factura:
        return None
    return factura.get(casilla.campo_factura)


def _revisar_casilla(casilla: Casilla, valor: str | None, factura: dict | None,
                     disponibles: dict[str, bool], declaracion: dict, encontrada: bool = True,
                     revisor: RevisorTexto | None = None, vocabulario: set[str] | None = None,
                     analisis_texto: dict[int, AnalisisTexto] | None = None) -> tuple[str, str | None, list[Alerta]]:
    """Devuelve (estado, fuente_usada, alertas) para una casilla. `encontrada` es False si la casilla ni siquiera aparece en el PDF."""
    alertas: list[Alerta] = []
    soporte_formal = casilla.soportes[0]
    faltantes = [s for s in casilla.soportes if not disponibles.get(s)]

    # 1) Campo obligatorio sin diligenciar (o que no se pudo leer del documento)
    if es_valor_vacio(valor):
        if not casilla.obligatoria:
            return "vacia_opcional", None, alertas
        referencia = _valor_en_factura(factura, casilla) if disponibles.get("factura_comercial") and "factura_comercial" in casilla.soportes else None
        if encontrada:
            detalle = "La casilla es obligatoria y quedó sin diligenciar."
        else:
            detalle = ("La casilla es obligatoria y no se encontró en el documento: puede estar sin diligenciar "
                       "o el formato del PDF no permitió leerla.")
        if referencia is not None:
            detalle += f" Referencia disponible en la factura comercial: {_mostrar(referencia)}"
            if casilla.campo_factura == "bultos" and factura.get("unidades_por_bulto"):
                detalle += f" ({factura['unidades_por_bulto']} unidades por bulto)"
            detalle += "."
        if soporte_formal in faltantes:
            detalle += f" El soporte formal de esta casilla ({DOCUMENTOS[soporte_formal]}) no está disponible, así que el valor debe confirmarse con ese documento."
        alertas.append(Alerta(casilla.numero, casilla.nombre, "campo_faltante", valor,
                              _mostrar(referencia), "factura_comercial" if referencia is not None else None, detalle))
        return ("faltante" if encontrada else "no_encontrada"), None, alertas

    # 2) ¿Con qué documento se puede validar? (el primero disponible que el agente sepa leer y que traiga el dato)
    usables = [s for s in casilla.soportes if disponibles.get(s) and s in EXTRACTORES_DISPONIBLES]
    disponibles_sin_extractor = [s for s in casilla.soportes if disponibles.get(s) and s not in EXTRACTORES_DISPONIBLES]
    fuente = usables[0] if usables else None
    valor_fuente = _valor_en_factura(factura, casilla) if fuente else None
    if fuente is None or valor_fuente is None:
        motivos = []
        if faltantes:
            motivos.append(f"los documentos que respaldan esta casilla ({_nombres(faltantes)}) no están en la carpeta de soporte")
        if disponibles_sin_extractor:
            motivos.append(f"esta versión del agente no procesa {_nombres(disponibles_sin_extractor)}")
        if factura is not None and _valor_en_factura(factura, casilla) is None:
            nota = "la factura comercial no contiene este dato"
            if casilla.tipo == "peso" and factura.get("remite_lista_empaque"):
                nota += " (remite explícitamente a la lista de empaque)"
            motivos.append(nota)
        detalle = f"No se pudo validar el valor declarado ({valor}): " + "; ".join(motivos) + "."
        alertas.append(Alerta(casilla.numero, casilla.nombre, "sin_documento_soporte", valor, None, None, detalle))
        return "no_validable", None, alertas

    # 3) Comparación (la descripción pasa por el revisor de texto; el resto, por su comparador determinista)
    origen_analisis = None
    if casilla.tipo == "descripcion" and revisor is not None:
        analisis = revisor.analizar(valor, str(valor_fuente), vocabulario or set())
        if analisis_texto is not None:
            analisis_texto[casilla.numero] = analisis
        r = _resultado_desde_analisis(valor, valor_fuente, analisis)
        origen_analisis = analisis.origen
    else:
        r = comparar(casilla, valor, valor_fuente)
    if r.coincide:
        estado = "ok"
    else:
        estado = "discrepancia"
        detalle = (f"El valor declarado ({valor}) no coincide con el de la {DOCUMENTOS[fuente].lower()} "
                   f"({_mostrar(valor_fuente)}).")
        if r.detalle:
            detalle += " " + r.detalle
        if casilla.tipo in ("pais", "moneda", "incoterm", "fecha"):
            detalle += f" [normalizado: {r.declaracion_normalizada} vs {r.fuente_normalizada}]"
        # Evidencia adicional para el número de factura: el nombre del archivo en la carpeta
        if casilla.campo_factura == "numero":
            nombre_archivo = declaracion.get("documentos_soporte", {}).get("factura_comercial", {}).get("detalle") or ""
            if r.fuente_normalizada and r.fuente_normalizada in normalizar_texto(nombre_archivo).replace(" ", ""):
                detalle += f" El archivo de la carpeta de soporte se llama «{nombre_archivo}», lo que confirma el número de la factura."
        alertas.append(Alerta(casilla.numero, casilla.nombre, "discrepancia_valor", valor, _mostrar(valor_fuente), fuente, detalle,
                              analisis=origen_analisis))

    # 4) La validación fue contra un documento secundario: dejar constancia
    if fuente != soporte_formal:
        if soporte_formal in faltantes:
            motivo = f"su soporte formal ({DOCUMENTOS[soporte_formal]}) no está en la carpeta"
        else:
            motivo = (f"su soporte formal ({DOCUMENTOS[soporte_formal]}) está en la carpeta pero esta versión del agente "
                      f"sólo procesa la factura comercial")
        detalle = (f"La casilla se validó contra la {DOCUMENTOS[fuente].lower()} porque {motivo}. "
                   f"El resultado es provisional hasta contrastarla con ese documento.")
        alertas.append(Alerta(casilla.numero, casilla.nombre, "sin_documento_soporte", valor, _mostrar(valor_fuente), fuente, detalle))
        if estado == "ok":
            estado = "ok_provisional"
    return estado, fuente, alertas


def _revisar_reglas_cruzadas(valores: dict[int, Decimal | None], estados: dict[int, str]) -> list[Alerta]:
    alertas: list[Alerta] = []
    for regla in REGLAS_CRUZADAS:
        implicadas = (*regla.casillas_sumandos, regla.casilla_resultado)
        if any(valores.get(n) is None for n in implicadas):
            continue  # falta algún dato: no se puede evaluar (ya habrá alerta de campo_faltante)
        suma = sum(valores[n] for n in regla.casillas_sumandos)
        resultado = valores[regla.casilla_resultado]
        if abs(suma - resultado) <= TOLERANCIA_MONTO:
            continue
        operandos = " + ".join(f"{formatear_monto(valores[n])} (casilla {n})" for n in regla.casillas_sumandos)
        detalle = (f"{regla.nombre}: {operandos} = {formatear_monto(suma)}, pero la casilla {regla.casilla_resultado} "
                   f"declara {formatear_monto(resultado)}. Diferencia: {formatear_monto(suma - resultado)}.")
        con_discrepancia = [n for n in implicadas if estados.get(n) == "discrepancia"]
        if con_discrepancia:
            detalle += (f" La(s) casilla(s) {', '.join(map(str, con_discrepancia))} ya presenta(n) discrepancia frente a la factura; "
                        f"el error probablemente está allí.")
        alertas.append(Alerta(regla.casilla_resultado, CASILLAS_POR_NUMERO[regla.casilla_resultado].nombre,
                              "inconsistencia_interna", formatear_monto(resultado), formatear_monto(suma),
                              "declaracion", detalle, casillas_relacionadas=list(implicadas)))
    return alertas


def _vocabulario_factura(factura: dict | None) -> set[str]:
    """Palabras de la factura: sirven de léxico de referencia para la revisión ortográfica."""
    if not factura:
        return set()
    textos = [v for k, v in factura.items() if isinstance(v, str) and k != "texto"]
    textos += [it["descripcion"] for it in factura.get("items", [])]
    return ortografia.vocabulario_de(textos)


_FRASES_HALLAZGO = {
    "acentuacion": "«{palabra}» debería llevar tilde: «{sugerencia}».",
    "error_tipografico": "«{palabra}» parece un error de digitación; la palabra esperada es «{sugerencia}».",
    "gramatica": "«{palabra}» presenta un problema gramatical; forma sugerida: «{sugerencia}».",
    "redaccion": "«{palabra}» podría redactarse mejor; forma sugerida: «{sugerencia}».",
}


def _revisar_ortografia(declaracion: dict, factura: dict | None, estados: dict[int, str],
                        revisor: RevisorTexto, vocabulario: set[str],
                        analisis_texto: dict[int, AnalisisTexto]) -> list[Alerta]:
    alertas: list[Alerta] = []
    for casilla in CATALOGO:
        if not casilla.texto_libre:
            continue
        valor = (declaracion["casillas"].get(casilla.numero) or {}).get("valor")
        if es_valor_vacio(valor):
            continue
        referencia = _valor_en_factura(factura, casilla)
        analisis = analisis_texto.get(casilla.numero)
        if analisis is None:  # la casilla no se comparó (p. ej. la factura no trae descripción): sólo ortografía
            analisis = revisor.analizar(valor, str(referencia) if referencia else None, vocabulario)
            analisis_texto[casilla.numero] = analisis

        for h in analisis.hallazgos:
            frase = _FRASES_HALLAZGO.get(h.tipo, _FRASES_HALLAZGO["redaccion"]).format(palabra=h.palabra, sugerencia=h.sugerencia or "—")
            if h.explicacion:
                frase += f" {h.explicacion}"
            alertas.append(Alerta(casilla.numero, casilla.nombre, "ortografia", h.palabra, None, None,
                                  f"[{h.tipo}] " + frase, sugerencia=h.sugerencia, analisis=analisis.origen))

        # Redacción: ¿la declaración omite información relevante que sí trae la factura, o es ambigua?
        # Sólo tiene sentido cuando la descripción es compatible; si ya es una discrepancia, sería ruido.
        if referencia and estados.get(casilla.numero) in ("ok", "ok_provisional"):
            if analisis.terminos_omitidos:
                detalle = ("[redaccion] La descripción es compatible con la factura pero menos específica: omite "
                           + ", ".join(f"«{t}»" for t in analisis.terminos_omitidos)
                           + ". Se recomienda incluir modelo y características técnicas para facilitar la clasificación arancelaria.")
                alertas.append(Alerta(casilla.numero, casilla.nombre, "ortografia", valor, str(referencia), "factura_comercial",
                                      detalle, analisis=analisis.origen))
            elif analisis.equivalencia == "parcial":
                alertas.append(Alerta(casilla.numero, casilla.nombre, "ortografia", valor, str(referencia), "factura_comercial",
                                      f"[redaccion] La descripción es compatible con la factura pero ambigua o incompleta. {analisis.explicacion}",
                                      analisis=analisis.origen))
    return alertas


def _verificar_factura(factura: dict | None) -> tuple[list[dict], list[Alerta]]:
    """La 'fuente de verdad' también se revisa: si sus propios totales no cuadran, se avisa."""
    verificaciones: list[dict] = []
    alertas: list[Alerta] = []
    if not factura:
        return verificaciones, alertas

    def registrar(regla: str, cumple: bool, detalle: str):
        verificaciones.append({"regla": regla, "cumple": cumple, "detalle": detalle})
        if not cumple:
            alertas.append(Alerta(None, "Factura comercial", "inconsistencia_interna", None, None, "factura_comercial",
                                  f"La factura comercial no cumple la regla «{regla}»: {detalle}"))

    for it in factura.get("items", []):
        cant, precio, sub = normalizar_numero(it["cantidad"]), normalizar_numero(it["precio_unitario"]), normalizar_numero(it["subtotal"])
        if None not in (cant, precio, sub):
            registrar(f"subtotal ítem {it['item']} = cantidad × precio unitario", abs(cant * precio - sub) <= TOLERANCIA_MONTO,
                      f"{cant} × {precio} = {formatear_monto(cant * precio)} vs subtotal {formatear_monto(sub)}")
    fob = normalizar_numero(factura.get("fob"))
    subtotales = [normalizar_numero(it["subtotal"]) for it in factura.get("items", [])]
    if fob is not None and subtotales and None not in subtotales:
        registrar("FOB = suma de subtotales", abs(sum(subtotales) - fob) <= TOLERANCIA_MONTO,
                  f"suma {formatear_monto(sum(subtotales))} vs FOB {formatear_monto(fob)}")
    flete, seguro, cif = (normalizar_numero(factura.get(k)) for k in ("flete", "seguro", "cif"))
    if None not in (fob, flete, seguro, cif):
        registrar("CIF = FOB + flete + seguro", abs(fob + flete + seguro - cif) <= TOLERANCIA_MONTO,
                  f"{formatear_monto(fob)} + {formatear_monto(flete)} + {formatear_monto(seguro)} = {formatear_monto(fob + flete + seguro)} vs CIF {formatear_monto(cif)}")
    return verificaciones, alertas


ORDEN_SEVERIDAD = {"alta": 0, "media": 1, "baja": 2}


def revisar(declaracion: dict, factura: dict | None, revisor: RevisorTexto | None = None) -> dict:
    """
    Punto de entrada del motor: recibe los dos diccionarios extraídos y devuelve el reporte completo.
    `revisor` es quien interpreta la descripción y revisa la ortografía (heurístico por defecto, o un LLM).
    """
    revisor = revisor or RevisorHeuristico()
    vocabulario = _vocabulario_factura(factura)
    analisis_texto: dict[int, AnalisisTexto] = {}
    disponibles = _documentos_disponibles(declaracion, factura)
    alertas: list[Alerta] = []
    resumen_casillas: list[dict] = []
    valores_numericos: dict[int, Decimal | None] = {}
    estados: dict[int, str] = {}

    for casilla in CATALOGO:
        extraida = declaracion["casillas"].get(casilla.numero)
        valor = extraida["valor"] if extraida else None
        estado, fuente, nuevas = _revisar_casilla(casilla, valor, factura, disponibles, declaracion,
                                                  encontrada=extraida is not None, revisor=revisor,
                                                  vocabulario=vocabulario, analisis_texto=analisis_texto)
        alertas.extend(nuevas)
        estados[casilla.numero] = estado
        if casilla.tipo in ("monto", "entero", "peso", "cantidad") and not es_valor_vacio(valor):
            valores_numericos[casilla.numero] = normalizar_numero(valor)
        resumen_casillas.append({
            "casilla": casilla.numero,
            "campo": casilla.nombre,
            "valor_declaracion": valor,
            "soportes": [DOCUMENTOS[s] for s in casilla.soportes],
            "validado_contra": DOCUMENTOS[fuente] if fuente else None,
            "estado": estado,
            "alertas": len(nuevas),
        })

    alertas.extend(_revisar_reglas_cruzadas(valores_numericos, estados))
    alertas.extend(_revisar_ortografia(declaracion, factura, estados, revisor, vocabulario, analisis_texto))
    verificaciones_factura, alertas_factura = _verificar_factura(factura)
    alertas.extend(alertas_factura)
    avisos_texto = sorted({a.aviso for a in analisis_texto.values() if a.aviso})

    alertas.sort(key=lambda a: (a.casilla if a.casilla is not None else 99, ORDEN_SEVERIDAD[a.severidad]))

    por_tipo: dict[str, int] = {}
    por_severidad: dict[str, int] = {}
    for a in alertas:
        por_tipo[a.tipo] = por_tipo.get(a.tipo, 0) + 1
        por_severidad[a.severidad] = por_severidad.get(a.severidad, 0) + 1
    con_alerta = sorted({a.casilla for a in alertas if a.casilla is not None})

    factura_sin_texto = {k: v for k, v in (factura or {}).items() if k != "texto"}
    return {
        "metadata": {
            "fecha_revision": datetime.now().isoformat(timespec="seconds"),
            "declaracion": declaracion.get("archivo"),
            "factura": (factura or {}).get("archivo"),
            "referencia_interna": declaracion.get("encabezado", {}).get("referencia_interna"),
            "declarante": declaracion.get("encabezado", {}).get("declarante"),
        },
        "documentos_soporte": {
            clave: {"nombre": DOCUMENTOS[clave], "disponible": disponibles[clave],
                    "detalle": declaracion.get("documentos_soporte", {}).get(clave, {}).get("detalle")}
            for clave in DOCUMENTOS
        },
        "extraccion": {
            "declaracion": [declaracion["casillas"].get(c.numero) or {"numero": c.numero, "etiqueta": c.nombre, "valor": None}
                            for c in CATALOGO],
            "factura": factura_sin_texto,
        },
        "resumen": {
            "total_alertas": len(alertas),
            "por_tipo": por_tipo,
            "por_severidad": por_severidad,
            "casillas_con_alerta": con_alerta,
            "casillas_sin_alerta": [c.numero for c in CATALOGO if c.numero not in con_alerta],
        },
        "resumen_casillas": resumen_casillas,
        "verificaciones_factura": verificaciones_factura,
        "analisis_texto": {
            "revisor": revisor.nombre,
            "avisos": avisos_texto,
            "casillas": {n: {"equivalencia": a.equivalencia, "confianza": a.confianza, "explicacion": a.explicacion,
                             "terminos_omitidos": a.terminos_omitidos, "hallazgos": len(a.hallazgos), "origen": a.origen}
                         for n, a in analisis_texto.items()},
        },
        "alertas": [a.a_dict() for a in alertas],
    }


def serializar(obj):
    """Ayuda para json.dumps: Decimal y date no son serializables por defecto."""
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    raise TypeError(f"Tipo no serializable: {type(obj).__name__}")
