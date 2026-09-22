"""
Carga y validación de los PDF antes de revisarlos.

Todo lo que puede salir mal con un archivo que entrega una persona se detecta aquí
y se convierte en un mensaje claro (qué pasó, por qué y qué hacer), en lugar de
un error técnico:

  - el archivo no existe, está vacío o es demasiado grande;
  - no es un PDF (aunque tenga la extensión .pdf);
  - el PDF está dañado, protegido con contraseña o tiene demasiadas páginas;
  - el PDF es una imagen escaneada sin texto (esta versión no hace OCR);
  - los dos archivos vienen intercambiados (se corrige solo y se avisa);
  - los dos archivos son el mismo tipo de documento;
  - el documento no tiene el formato esperado (no se reconocen sus casillas o campos).

También produce "avisos": situaciones que no impiden la revisión pero que el
usuario debe conocer (casillas que no se pudieron leer, datos de la factura ausentes).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from .extraccion import extraer_declaracion, extraer_factura
from .normalizacion import normalizar_texto
from .reglas import CATALOGO

LIMITE_BYTES = 20 * 1024 * 1024   # 20 MB: una declaración o factura normal pesa menos de 1 MB
MAX_PAGINAS = 20
MINIMO_CASILLAS = 8               # si se leen menos, el formulario no es el esperado

# Frases que identifican cada tipo de documento (en texto normalizado: minúsculas y sin tildes)
MARCAS_DECLARACION = ("declaracion de importacion", "referencia interna", "documentos soporte", "declaracion previa",
                      "elaborado por", "moneda de negociacion", "numero de bultos", "peso bruto", "sin diligenciar")
MARCAS_FACTURA = ("commercial invoice", "shipper", "consignee", "precio unitario", "subtotal", "fecha de emision",
                  "forma de pago", "puerto de destino", "pais de embarque", "flete internacional")


class ErrorDocumento(Exception):
    """Error que se le puede mostrar a cualquier persona: título, explicación y qué hacer."""

    def __init__(self, titulo: str, detalle: str, sugerencia: str = ""):
        super().__init__(f"{titulo}: {detalle}")
        self.titulo, self.detalle, self.sugerencia = titulo, detalle, sugerencia

    def a_dict(self) -> dict:
        return {"titulo": self.titulo, "detalle": self.detalle, "sugerencia": self.sugerencia}


@dataclass
class Carga:
    declaracion: dict
    factura: dict
    avisos: list[str] = field(default_factory=list)
    ruta_declaracion: str = ""
    ruta_factura: str = ""


# ---------------------------------------------------------------------------
# Validación de un archivo PDF
# ---------------------------------------------------------------------------

def validar_archivo_pdf(ruta: str | Path) -> str:
    """Comprueba que el archivo sea un PDF legible con texto y devuelve su texto plano."""
    ruta = Path(ruta)
    nombre = ruta.name
    if not ruta.is_file():
        raise ErrorDocumento("Archivo no encontrado", f"No existe el archivo «{nombre}».",
                             "Verifica la ruta o vuelve a seleccionar el archivo.")
    tamano = ruta.stat().st_size
    if tamano == 0:
        raise ErrorDocumento("Archivo vacío", f"El archivo «{nombre}» está vacío (0 bytes).",
                             "Vuelve a exportar o descargar el documento en PDF.")
    if tamano > LIMITE_BYTES:
        raise ErrorDocumento("Archivo demasiado grande",
                             f"«{nombre}» pesa {tamano / (1024 * 1024):.1f} MB y el máximo es {LIMITE_BYTES // (1024 * 1024)} MB.",
                             "Una declaración o una factura normal pesa menos de 1 MB. Verifica que sea el documento correcto.")
    with open(ruta, "rb") as archivo:
        cabecera = archivo.read(1024)
    if b"%PDF" not in cabecera:
        raise ErrorDocumento("El archivo no es un PDF", f"«{nombre}» no tiene formato PDF, aunque tenga esa extensión.",
                             "Abre el documento en su programa original y guárdalo o expórtalo como PDF.")
    try:
        lector = PdfReader(str(ruta))
        if lector.is_encrypted:
            try:
                abierto = lector.decrypt("")
            except Exception:  # pypdf lanza distintos errores según el cifrado
                abierto = 0
            if not abierto:
                raise ErrorDocumento("PDF protegido con contraseña", f"«{nombre}» está protegido y no se puede leer.",
                                     "Quita la contraseña del PDF (o pide una copia sin protección) y vuelve a intentarlo.")
        numero_paginas = len(lector.pages)
        texto = "\n".join((pagina.extract_text() or "") for pagina in lector.pages)
    except ErrorDocumento:
        raise
    except (PdfReadError, ValueError, OSError) as error:
        raise ErrorDocumento("No se pudo leer el PDF", f"«{nombre}» parece dañado o incompleto ({error}).",
                             "Vuelve a descargar o exportar el documento y reintenta.")
    if numero_paginas > MAX_PAGINAS:
        raise ErrorDocumento("Demasiadas páginas", f"«{nombre}» tiene {numero_paginas} páginas; una declaración o factura tiene una o pocas.",
                             "Verifica que hayas seleccionado el documento correcto.")
    if len(texto.strip()) < 30:
        raise ErrorDocumento("El PDF no contiene texto", f"«{nombre}» parece un documento escaneado o una imagen: no tiene texto que se pueda leer.",
                             "Esta versión no reconoce texto en imágenes (OCR). Usa el PDF original generado por el sistema, no una foto ni un escaneo.")
    return texto


def detectar_tipo(texto: str) -> str:
    """'declaracion', 'factura' o 'desconocido', según las frases características que contenga el texto."""
    t = normalizar_texto(texto)
    puntos_declaracion = sum(1 for marca in MARCAS_DECLARACION if marca in t)
    puntos_factura = sum(1 for marca in MARCAS_FACTURA if marca in t)
    if puntos_declaracion >= 2 and puntos_declaracion > puntos_factura:
        return "declaracion"
    if puntos_factura >= 2 and puntos_factura > puntos_declaracion:
        return "factura"
    return "desconocido"


# ---------------------------------------------------------------------------
# Carga de los dos documentos
# ---------------------------------------------------------------------------

def cargar_documentos(ruta_declaracion: str | Path, ruta_factura: str | Path) -> Carga:
    """Valida ambos PDF, corrige si vienen intercambiados, los extrae y valida que la extracción sea utilizable."""
    ruta_d, ruta_f = Path(ruta_declaracion), Path(ruta_factura)
    texto_d = validar_archivo_pdf(ruta_d)
    texto_f = validar_archivo_pdf(ruta_f)
    tipo_d, tipo_f = detectar_tipo(texto_d), detectar_tipo(texto_f)
    avisos: list[str] = []

    if tipo_d == "factura" and tipo_f == "declaracion":
        ruta_d, ruta_f = ruta_f, ruta_d
        avisos.append("Los archivos estaban intercambiados (la factura en el lugar de la declaración y viceversa). "
                      "Se corrigió automáticamente.")
    elif tipo_d == "factura" and tipo_f == "factura":
        raise ErrorDocumento("Los dos archivos son facturas", "Ninguno de los dos PDF es una declaración de importación.",
                             "Selecciona la declaración de importación (el formulario con las casillas numeradas) en el primer campo.")
    elif tipo_d == "declaracion" and tipo_f == "declaracion":
        raise ErrorDocumento("Los dos archivos son declaraciones", "Ninguno de los dos PDF es una factura comercial.",
                             "Selecciona la factura comercial del proveedor en el segundo campo.")
    elif tipo_d == "desconocido":
        raise ErrorDocumento("No se reconoce la declaración",
                             f"El archivo «{ruta_d.name}» no parece una declaración de importación: no se encontraron sus casillas ni sus encabezados.",
                             "Verifica que sea el formulario de declaración generado por el sistema, en PDF con texto (no escaneado).")
    elif tipo_f == "desconocido":
        raise ErrorDocumento("No se reconoce la factura",
                             f"El archivo «{ruta_f.name}» no parece una factura comercial: no se encontraron sus encabezados ni sus totales.",
                             "Verifica que sea la factura comercial del proveedor, en PDF con texto (no escaneado).")

    declaracion = extraer_declaracion(ruta_d)
    factura = extraer_factura(ruta_f)

    no_leidas = [c.numero for c in CATALOGO if c.numero not in declaracion["casillas"]]
    if len(CATALOGO) - len(no_leidas) < MINIMO_CASILLAS:
        raise ErrorDocumento("No se pudieron leer las casillas de la declaración",
                             f"Sólo se reconocieron {len(CATALOGO) - len(no_leidas)} de {len(CATALOGO)} casillas en «{ruta_d.name}».",
                             "El formato del formulario es distinto al esperado (casillas numeradas del 1 al 15 con su etiqueta y su valor). "
                             "Usa el formulario estándar o adapta el extractor a este formato.")
    if no_leidas:
        avisos.append("No se pudieron leer las casillas " + ", ".join(map(str, no_leidas)) +
                      " de la declaración; se reportan como no encontradas.")
    if not declaracion["documentos_soporte"]:
        avisos.append("La declaración no incluye la lista de documentos de la carpeta de soporte; "
                      "se asume que sólo está disponible la factura comercial.")

    faltan = [nombre for campo, nombre in (("numero", "número"), ("fecha", "fecha"), ("fob", "valor FOB"), ("cif", "valor CIF"))
              if not factura.get(campo)]
    if not factura.get("items"):
        faltan.append("ítems (descripción, cantidad y precios)")
    if len(faltan) >= 4:
        raise ErrorDocumento("No se pudieron leer los datos de la factura",
                             f"En «{ruta_f.name}» no se encontraron el número, la fecha, los totales ni los ítems.",
                             "El formato de la factura es distinto al esperado. Usa el PDF original de la factura comercial o adapta el extractor.")
    if faltan:
        avisos.append("En la factura no se pudieron leer: " + ", ".join(faltan) +
                      ". Las casillas que dependen de esos datos quedarán sin verificar.")

    return Carga(declaracion, factura, avisos, str(ruta_d), str(ruta_f))
