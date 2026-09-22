"""
Catálogo de casillas y reglas: la "tabla de conocimiento" del agente.

Aquí NO hay lógica de comparación; sólo se describe, por cada casilla:
  - qué tipo de dato contiene (para saber cómo normalizarla y compararla),
  - si es obligatoria,
  - qué documentos la respaldan y en qué orden de prioridad,
  - con qué campo de la factura se contrasta (cuando la factura es uno de sus soportes).

Ventaja: para pasar de 15 a ~50 casillas basta con agregar filas a este catálogo
(y, si aparece un tipo de dato nuevo, un comparador para ese tipo). Las reglas
de diligenciamiento de la DIAN se modelan como reglas cruzadas entre casillas.
"""
from __future__ import annotations

from dataclasses import dataclass

# Documentos soporte que puede haber en la carpeta de la declaración.
DOCUMENTOS = {
    "factura_comercial": "Factura comercial",
    "lista_empaque": "Lista de empaque",
    "certificado_origen": "Certificado de origen",
    "documento_transporte": "BL / documento de transporte",
}

# Documentos para los que esta versión del agente tiene extractor implementado.
EXTRACTORES_DISPONIBLES = {"factura_comercial"}


@dataclass(frozen=True)
class Casilla:
    numero: int
    nombre: str
    tipo: str                       # identificador | fecha | empresa | empresa_nit | pais | incoterm | moneda | descripcion | cantidad | entero | peso | monto
    soportes: tuple[str, ...] = ("factura_comercial",)   # documentos que respaldan la casilla, el primero es el soporte formal
    campo_factura: str | None = None                     # campo del dict de la factura con el que se compara
    obligatoria: bool = True
    texto_libre: bool = False                            # se revisa ortografía y redacción
    nota: str = ""                                       # justificación de la regla (documentación viva)


CATALOGO: list[Casilla] = [
    Casilla(1, "Número de factura comercial", "identificador", campo_factura="numero"),
    Casilla(2, "Fecha de factura", "fecha", campo_factura="fecha"),
    Casilla(3, "Exportador", "empresa", campo_factura="exportador"),
    Casilla(4, "Importador", "empresa_nit", campo_factura="importador"),
    Casilla(5, "País de origen", "pais",
            soportes=("certificado_origen", "factura_comercial"), campo_factura="pais_origen",
            nota="El soporte formal del origen es el certificado de origen; la factura sólo sirve como referencia provisional."),
    Casilla(6, "Incoterm", "incoterm", campo_factura="incoterm"),
    Casilla(7, "Moneda de negociación", "moneda", campo_factura="moneda"),
    Casilla(8, "Descripción de la mercancía", "descripcion", campo_factura="descripcion", texto_libre=True,
            nota="Se compara de forma semántica (términos), no literal: la declaración suele resumir la factura."),
    Casilla(9, "Cantidad (unidades)", "cantidad", campo_factura="cantidad"),
    Casilla(10, "Número de bultos", "entero",
            soportes=("lista_empaque", "factura_comercial"), campo_factura="bultos",
            nota="La lista de empaque es el soporte formal; la factura puede mencionar los bultos como referencia."),
    Casilla(11, "Peso bruto (kg)", "peso",
            soportes=("lista_empaque", "documento_transporte", "factura_comercial"), campo_factura="peso_bruto",
            nota="El peso bruto lo respaldan la lista de empaque o el documento de transporte. La factura normalmente "
                 "no lo trae; si lo menciona, sirve sólo como referencia provisional."),
    Casilla(12, "Valor FOB (USD)", "monto", campo_factura="fob"),
    Casilla(13, "Flete (USD)", "monto",
            soportes=("factura_comercial", "documento_transporte"), campo_factura="flete",
            nota="Con Incoterm CIF el flete viene desglosado en la factura; el documento de transporte lo confirmaría."),
    Casilla(14, "Seguro (USD)", "monto", campo_factura="seguro",
            nota="Con Incoterm CIF el seguro viene desglosado en la factura."),
    Casilla(15, "Valor CIF total (USD)", "monto", campo_factura="cif"),
]

CASILLAS_POR_NUMERO = {c.numero: c for c in CATALOGO}


@dataclass(frozen=True)
class ReglaCruzada:
    """Regla aritmética entre casillas de la misma declaración (regla de diligenciamiento)."""
    id: str
    nombre: str
    casilla_resultado: int
    casillas_sumandos: tuple[int, ...]
    detalle: str


REGLAS_CRUZADAS: list[ReglaCruzada] = [
    ReglaCruzada(
        id="cif_igual_fob_mas_flete_mas_seguro",
        nombre="Valor CIF total = FOB + flete + seguro",
        casilla_resultado=15,
        casillas_sumandos=(12, 13, 14),
        detalle="El valor CIF declarado debe ser la suma de los valores FOB, flete y seguro declarados.",
    ),
]


# Tipos de alerta. Los cuatro primeros son los pedidos en la prueba; el quinto se agrega
# porque una declaración puede ser internamente incoherente aunque cada casilla, por
# separado, coincida con algún documento (ver README, "Decisiones de diseño").
TIPOS_ALERTA = {
    "discrepancia_valor": "El valor de la casilla no coincide con el de su documento soporte.",
    "campo_faltante": "Un campo obligatorio quedó sin diligenciar.",
    "sin_documento_soporte": "No se pudo validar (o sólo se validó de forma provisional) porque el documento que respalda la casilla no está disponible.",
    "ortografia": "Hallazgo de ortografía o redacción en un campo de texto.",
    "inconsistencia_interna": "Los valores declarados no cumplen una regla aritmética entre casillas.",
}

SEVERIDAD_POR_TIPO = {
    "discrepancia_valor": "alta",
    "campo_faltante": "alta",
    "inconsistencia_interna": "alta",
    "sin_documento_soporte": "media",
    "ortografia": "baja",
}
