"""
Pruebas del agente. Se ejecutan con:  python -m unittest discover -s tests -v

Tres niveles:
  1. Normalización: que los formatos distintos de cada documento se lean igual.
  2. Motor de validación con declaraciones sintéticas (sin PDF): que las reglas
     produzcan exactamente las alertas esperadas y ninguna más.
  3. Extremo a extremo con los PDF de ejemplo de la prueba.
"""
from __future__ import annotations

import json
import sys
import unittest
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import shutil  # noqa: E402
import tempfile  # noqa: E402

from pypdf import PdfWriter  # noqa: E402

import interfaz  # noqa: E402
from agente import ortografia  # noqa: E402
from agente.lenguaje import (  # noqa: E402
    ConfiguracionLLM, ErrorLLM, RevisorHeuristico, RevisorLLM, cargar_configuracion, crear_revisor,
)
from agente.carga import ErrorDocumento, cargar_documentos, detectar_tipo, validar_archivo_pdf  # noqa: E402
from agente.extraccion import extraer_declaracion, extraer_factura, leer_pdf  # noqa: E402
from agente.normalizacion import (  # noqa: E402
    es_valor_vacio, extraer_unidad, normalizar_fecha, normalizar_incoterm, normalizar_moneda, normalizar_numero,
    normalizar_pais, separar_empresa_nit,
)
from agente.reglas import CATALOGO  # noqa: E402
from agente.validacion import revisar  # noqa: E402

DECLARACION_PDF = BASE / "ejemplos" / "declaracion_importacion_ejemplo.pdf"
FACTURA_PDF = BASE / "ejemplos" / "factura_comercial_ejemplo.pdf"
DECLARACION_DISCREPANCIAS_PDF = BASE / "ejemplos" / "declaracion_importacion_discrepancias.pdf"
FACTURA_DISCREPANCIAS_PDF = BASE / "ejemplos" / "factura_comercial_discrepancias.pdf"
DECLARACION_FORMATOS_PDF = BASE / "ejemplos" / "declaracion_importacion_formatos_distintos.pdf"
FACTURA_FORMATOS_PDF = BASE / "ejemplos" / "factura_comercial_formatos_distintos.pdf"
DECLARACION_FALTANTES_PDF = BASE / "ejemplos" / "declaracion_importacion_faltantes.pdf"
FACTURA_FALTANTES_PDF = BASE / "ejemplos" / "factura_comercial_faltantes.pdf"


class TestNormalizacion(unittest.TestCase):
    def test_numeros_en_ambos_formatos(self):
        self.assertEqual(normalizar_numero("18.540,00"), Decimal("18540.00"))   # formato colombiano
        self.assertEqual(normalizar_numero("18,450.00"), Decimal("18450.00"))   # formato anglosajón
        self.assertEqual(normalizar_numero("1.250,00 kg"), Decimal("1250.00"))
        self.assertEqual(normalizar_numero("USD 620.00"), Decimal("620.00"))
        self.assertEqual(normalizar_numero("500 unidades"), Decimal("500"))
        self.assertEqual(normalizar_numero("1.250"), Decimal("1250"))           # separador de miles
        self.assertEqual(normalizar_numero("36.90"), Decimal("36.90"))
        self.assertIsNone(normalizar_numero("sin valor"))

    def test_fechas(self):
        self.assertEqual(normalizar_fecha("15 de junio de 2026"), date(2026, 6, 15))
        self.assertEqual(normalizar_fecha("2026-06-15"), date(2026, 6, 15))
        self.assertEqual(normalizar_fecha("15/06/2026"), date(2026, 6, 15))
        self.assertEqual(normalizar_fecha("June 15, 2026"), date(2026, 6, 15))

    def test_moneda_e_incoterm(self):
        self.assertEqual(normalizar_moneda("Dólares de los Estados Unidos(USD)"), "USD")
        self.assertEqual(normalizar_moneda("USD"), "USD")
        con_version = normalizar_incoterm("CIF Cartagena, Colombia (Incoterms 2020)")
        sin_version = normalizar_incoterm("CIF Cartagena, Colombia")
        self.assertEqual((con_version["termino"], con_version["lugar"]), (sin_version["termino"], sin_version["lugar"]))
        self.assertEqual(con_version["version"], "2020")

    def test_paises_y_empresas(self):
        self.assertEqual(normalizar_pais("República Popular China"), "CN")
        self.assertEqual(normalizar_pais("China"), "CN")
        self.assertEqual(normalizar_pais("Corea del Sur"), "KR")
        self.assertEqual(separar_empresa_nit("Distribuidora Andina S.A.S. — NIT 900.123.456-7"),
                         ("Distribuidora Andina S.A.S.", "9001234567"))

    def test_unidades(self):
        self.assertEqual(extraer_unidad("500 unidades"), "unidades")
        self.assertEqual(extraer_unidad("500 und."), "unidades")
        self.assertEqual(extraer_unidad("500 pcs"), "unidades")
        self.assertEqual(extraer_unidad("1.250,00 kg"), "kg")
        self.assertEqual(extraer_unidad("1250 kgs"), "kg")

    def test_valores_vacios(self):
        self.assertTrue(es_valor_vacio(None))
        self.assertTrue(es_valor_vacio("(sin diligenciar)"))
        self.assertTrue(es_valor_vacio("—"))
        self.assertFalse(es_valor_vacio("500"))


class TestOrtografia(unittest.TestCase):
    def setUp(self):
        self.lexico = ortografia.construir_lexico(["modelo", "eléctricos", "trifásicos", "potencia"])

    def test_detecta_tildes_y_errores_de_digitacion(self):
        hallazgos = ortografia.revisar_texto("Motores electricos trifasicos de baja poternica, uso industrial", self.lexico)
        self.assertEqual({(h.palabra, h.sugerencia, h.subtipo) for h in hallazgos}, {
            ("electricos", "eléctricos", "acentuacion"),
            ("trifasicos", "trifásicos", "acentuacion"),
            ("poternica", "potencia", "error_tipografico"),
        })

    def test_no_marca_texto_correcto_ni_nombres_propios(self):
        self.assertEqual(ortografia.revisar_texto("Motores eléctricos trifásicos de baja potencia, uso industrial", self.lexico), [])
        self.assertEqual(ortografia.revisar_texto("Motores marca Shenzhen PowerTech", self.lexico), [])

    def test_comparacion_por_terminos(self):
        decl = "Motores electricos trifasicos de baja poternica, uso industrial"
        fact = "Motores eléctricos trifásicos de baja potencia (< 0.75kW), uso industrial, modelo PTM-075T"
        self.assertEqual(ortografia.cobertura_terminos(decl, fact), 1.0)
        self.assertEqual(ortografia.terminos_omitidos(decl, fact), ["0.75kW", "modelo", "PTM-075T"])


# ---------------------------------------------------------------------------
# Motor de validación con datos sintéticos
# ---------------------------------------------------------------------------

FACTURA_OK = {
    "archivo": "factura.pdf", "numero": "FC-2026-00417", "fecha": "15 de junio de 2026",
    "exportador": {"nombre": "Shenzhen PowerTech Motors Co., Ltd.", "nit": None},
    "importador": {"nombre": "Distribuidora Andina S.A.S.", "nit": "9001234567"},
    "pais_origen": "República Popular China", "incoterm": "CIF Cartagena, Colombia (Incoterms 2020)",
    "moneda": "Dólares de los Estados Unidos (USD)",
    "items": [{"item": 1, "descripcion": "Motores eléctricos trifásicos de baja potencia, uso industrial",
               "cantidad": "500", "unidad": "unidades", "precio_unitario": "36.90", "subtotal": "18,450.00"}],
    "descripcion": "Motores eléctricos trifásicos de baja potencia, uso industrial", "cantidad": "500 unidades",
    "fob": "18,450.00", "flete": "620.00", "seguro": "95.00", "cif": "19,165.00",
    "bultos": "25", "unidades_por_bulto": "20", "peso_bruto": None, "remite_lista_empaque": True,
}

VALORES_OK = {
    1: "FC-2026-00417", 2: "15 de junio de 2026", 3: "Shenzhen PowerTech Motors Co., Ltd.",
    4: "Distribuidora Andina S.A.S. — NIT 900.123.456-7", 5: "China", 6: "CIF Cartagena, Colombia", 7: "USD",
    8: "Motores eléctricos trifásicos de baja potencia, uso industrial", 9: "500 unidades", 10: "25",
    11: "1.250,00 kg", 12: "18.450,00", 13: "620,00", 14: "95,00", 15: "19.165,00",
}


def declaracion_sintetica(valores: dict, documentos_disponibles: set[str]) -> dict:
    todos = ("factura_comercial", "lista_empaque", "certificado_origen", "documento_transporte")
    return {
        "archivo": "declaracion.pdf", "encabezado": {"referencia_interna": "TEST"},
        "casillas": {c.numero: {"numero": c.numero, "etiqueta": c.nombre, "valor": valores.get(c.numero)} for c in CATALOGO},
        "documentos_soporte": {d: {"etiqueta": d, "disponible": d in documentos_disponibles, "detalle": ""} for d in todos},
    }


def tipos_por_casilla(reporte: dict) -> Counter:
    return Counter((a["casilla"], a["tipo"]) for a in reporte["alertas"])


class TestMotorValidacion(unittest.TestCase):
    def test_declaracion_correcta_no_genera_discrepancias(self):
        decl = declaracion_sintetica(VALORES_OK, {"factura_comercial"})
        reporte = revisar(decl, FACTURA_OK)
        tipos = tipos_por_casilla(reporte)
        self.assertEqual(sum(1 for (_, t) in tipos.elements() if t == "discrepancia_valor"), 0)
        self.assertEqual(sum(1 for (_, t) in tipos.elements() if t in ("campo_faltante", "ortografia", "inconsistencia_interna")), 0)
        # Lo único que queda es la falta de documentos: origen (provisional), bultos (provisional) y peso (no validable)
        self.assertEqual(set(tipos), {(5, "sin_documento_soporte"), (10, "sin_documento_soporte"), (11, "sin_documento_soporte")})

    def test_campo_obligatorio_vacio(self):
        valores = {**VALORES_OK, 9: None}
        reporte = revisar(declaracion_sintetica(valores, {"factura_comercial"}), FACTURA_OK)
        self.assertIn((9, "campo_faltante"), tipos_por_casilla(reporte))

    def test_regla_cruzada_cif(self):
        valores = {**VALORES_OK, 13: "700,00"}    # flete alterado: CIF ya no cuadra
        reporte = revisar(declaracion_sintetica(valores, {"factura_comercial"}), FACTURA_OK)
        tipos = tipos_por_casilla(reporte)
        self.assertIn((13, "discrepancia_valor"), tipos)
        self.assertIn((15, "inconsistencia_interna"), tipos)

    def test_sin_factura_nada_se_valida(self):
        reporte = revisar(declaracion_sintetica(VALORES_OK, set()), None)
        tipos = tipos_por_casilla(reporte)
        self.assertEqual(sum(1 for (_, t) in tipos.elements() if t == "sin_documento_soporte"), 15)
        self.assertEqual(sum(1 for (_, t) in tipos.elements() if t == "discrepancia_valor"), 0)

    def test_formatos_distintos_no_generan_falsos_positivos(self):
        valores = {**VALORES_OK, 5: "República Popular China", 6: "CIF Cartagena, Colombia (Incoterms 2020)",
                   7: "Dólares de los Estados Unidos", 12: "18,450.00"}
        reporte = revisar(declaracion_sintetica(valores, {"factura_comercial"}), FACTURA_OK)
        self.assertEqual(sum(1 for (_, t) in tipos_por_casilla(reporte).elements() if t == "discrepancia_valor"), 0)


# ---------------------------------------------------------------------------
# Extremo a extremo con los PDF de la prueba
# ---------------------------------------------------------------------------

@unittest.skipUnless(DECLARACION_PDF.exists() and FACTURA_PDF.exists(), "faltan los PDF de ejemplo")
class TestExtremoAExtremo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.declaracion = extraer_declaracion(DECLARACION_PDF)
        cls.factura = extraer_factura(FACTURA_PDF)
        cls.reporte = revisar(cls.declaracion, cls.factura)

    def test_extrae_las_15_casillas(self):
        self.assertEqual(sorted(self.declaracion["casillas"]), list(range(1, 16)))
        self.assertEqual(self.declaracion["casillas"][12]["valor"], "18.540,00")
        self.assertEqual(self.declaracion["casillas"][10]["valor"], "(sin diligenciar)")
        self.assertTrue(self.declaracion["documentos_soporte"]["factura_comercial"]["disponible"])
        self.assertFalse(self.declaracion["documentos_soporte"]["lista_empaque"]["disponible"])

    def test_extrae_la_factura(self):
        f = self.factura
        self.assertEqual(f["numero"], "FC-2026-00417")
        self.assertEqual(f["pais_origen"], "República Popular China")
        self.assertEqual(f["importador"]["nit"], "9001234567")
        self.assertEqual((f["fob"], f["flete"], f["seguro"], f["cif"]), ("18,450.00", "620.00", "95.00", "19,165.00"))
        self.assertEqual(f["bultos"], "25")
        self.assertEqual(len(f["items"]), 1)
        self.assertEqual(f["items"][0]["cantidad"], "500")
        self.assertIsNone(f["peso_bruto"])

    def test_alertas_exactas(self):
        esperadas = Counter({
            (1, "discrepancia_valor"): 1,        # FC-2026-00147 vs FC-2026-00417
            (5, "discrepancia_valor"): 1,        # Corea del Sur vs China
            (5, "sin_documento_soporte"): 1,     # sin certificado de origen (validación provisional)
            (8, "ortografia"): 4,                # electricos, trifasicos, poternica + redacción
            (10, "campo_faltante"): 1,           # bultos sin diligenciar
            (11, "sin_documento_soporte"): 1,    # peso bruto sin lista de empaque ni BL
            (12, "discrepancia_valor"): 1,       # 18.540,00 vs 18,450.00
            (15, "inconsistencia_interna"): 1,   # FOB + flete + seguro != CIF declarado
        })
        self.assertEqual(tipos_por_casilla(self.reporte), esperadas)

    def test_casillas_correctas_sin_alertas(self):
        self.assertEqual(self.reporte["resumen"]["casillas_sin_alerta"], [2, 3, 4, 6, 7, 9, 13, 14])

    def test_la_factura_es_internamente_coherente(self):
        self.assertTrue(all(v["cumple"] for v in self.reporte["verificaciones_factura"]))


@unittest.skipUnless(DECLARACION_DISCREPANCIAS_PDF.exists() and FACTURA_DISCREPANCIAS_PDF.exists(),
                     "faltan los PDF del caso de discrepancias (ver ejemplos/generar_ejemplos.py)")
class TestCasoTodasDiscrepancias(unittest.TestCase):
    """Caso de estrés generado con ejemplos/generar_ejemplos.py: las 15 casillas difieren de la factura."""

    @classmethod
    def setUpClass(cls):
        cls.reporte = revisar(extraer_declaracion(DECLARACION_DISCREPANCIAS_PDF), extraer_factura(FACTURA_DISCREPANCIAS_PDF))

    def test_las_15_casillas_tienen_discrepancia(self):
        tipos = tipos_por_casilla(self.reporte)
        self.assertEqual(sorted(n for (n, t) in tipos if t == "discrepancia_valor"), list(range(1, 16)))
        self.assertEqual(self.reporte["resumen"]["casillas_sin_alerta"], [])

    def test_no_hay_alertas_de_otros_tipos_inesperadas(self):
        tipos = tipos_por_casilla(self.reporte)
        otras = Counter({(n, t): k for (n, t), k in tipos.items() if t != "discrepancia_valor"})
        self.assertEqual(otras, Counter({
            (5, "sin_documento_soporte"): 1,     # país de origen: sin certificado de origen (provisional)
            (10, "sin_documento_soporte"): 1,    # bultos: sin lista de empaque (provisional)
            (11, "sin_documento_soporte"): 1,    # peso bruto: la factura lo menciona, pero el soporte formal falta
            (8, "ortografia"): 4,                # electricos, monofasicos, potencai, domestico
        }))
        # El CIF declarado es coherente con sus componentes declarados: no debe haber inconsistencia interna
        self.assertNotIn("inconsistencia_interna", self.reporte["resumen"]["por_tipo"])


@unittest.skipUnless(DECLARACION_FORMATOS_PDF.exists() and FACTURA_FORMATOS_PDF.exists(),
                     "faltan los PDF del caso de formatos distintos (ver ejemplos/generar_ejemplos.py)")
class TestCasoFormatosDistintos(unittest.TestCase):
    """Declaración correcta escrita con otros formatos: la normalización debe evitar todo falso positivo."""

    @classmethod
    def setUpClass(cls):
        cls.declaracion = extraer_declaracion(DECLARACION_FORMATOS_PDF)
        cls.reporte = revisar(cls.declaracion, extraer_factura(FACTURA_FORMATOS_PDF))

    def test_solo_quedan_las_validaciones_provisionales(self):
        self.assertEqual(tipos_por_casilla(self.reporte), Counter({
            (5, "sin_documento_soporte"): 1, (10, "sin_documento_soporte"): 1, (11, "sin_documento_soporte"): 1,
        }))
        self.assertEqual(self.reporte["resumen"]["casillas_sin_alerta"], [1, 2, 3, 4, 6, 7, 8, 9, 12, 13, 14, 15])

    def test_valor_en_dos_renglones_se_reconstruye(self):
        self.assertEqual(self.declaracion["casillas"][8]["valor"],
                         "Motores eléctricos trifásicos de baja potencia (< 0.75 kW), uso industrial, modelo PTM-075T")


@unittest.skipUnless(DECLARACION_FALTANTES_PDF.exists() and FACTURA_FALTANTES_PDF.exists(),
                     "faltan los PDF del caso de faltantes (ver ejemplos/generar_ejemplos.py)")
class TestCasoFaltantesYDosItems(unittest.TestCase):
    """Factura con dos ítems, tres casillas vacías, CIF transpuesto y lista de empaque presente pero no procesada."""

    @classmethod
    def setUpClass(cls):
        cls.factura = extraer_factura(FACTURA_FALTANTES_PDF)
        cls.reporte = revisar(extraer_declaracion(DECLARACION_FALTANTES_PDF), cls.factura)

    def test_alertas_exactas(self):
        self.assertEqual(tipos_por_casilla(self.reporte), Counter({
            (2, "campo_faltante"): 1, (6, "campo_faltante"): 1, (7, "campo_faltante"): 1,
            (5, "sin_documento_soporte"): 1, (10, "sin_documento_soporte"): 1, (11, "sin_documento_soporte"): 1,
            (8, "ortografia"): 2,                 # «electricos» + redacción (omite las potencias)
            (15, "discrepancia_valor"): 1,        # 22.630,00 vs 22,360.00
            (15, "inconsistencia_interna"): 1,    # 12 + 13 + 14 = 22.360,00 ≠ 22.630,00
        }))

    def test_factura_con_dos_items(self):
        self.assertEqual(len(self.factura["items"]), 2)
        self.assertEqual(self.factura["cantidad"], "500 unidades")
        self.assertEqual((self.factura["fob"], self.factura["cif"]), ("21,550.00", "22,360.00"))
        self.assertTrue(all(v["cumple"] for v in self.reporte["verificaciones_factura"]))

    def test_lista_de_empaque_presente_pero_no_procesada(self):
        detalle = next(a["detalle"] for a in self.reporte["alertas"] if a["casilla"] == 10 and a["tipo"] == "sin_documento_soporte")
        self.assertIn("está en la carpeta", detalle)


@unittest.skipUnless(DECLARACION_PDF.exists() and FACTURA_PDF.exists(), "faltan los PDF de ejemplo")
class TestCargaYValidacion(unittest.TestCase):
    """Todo archivo problemático debe convertirse en un ErrorDocumento con título, detalle y sugerencia."""

    def setUp(self):
        self.carpeta = Path(tempfile.mkdtemp(prefix="prueba_carga_"))

    def tearDown(self):
        shutil.rmtree(self.carpeta, ignore_errors=True)

    def _archivo(self, nombre: str, datos: bytes) -> Path:
        ruta = self.carpeta / nombre
        ruta.write_bytes(datos)
        return ruta

    def _error(self, funcion, *args) -> ErrorDocumento:
        with self.assertRaises(ErrorDocumento) as contexto:
            funcion(*args)
        error = contexto.exception
        self.assertTrue(error.titulo and error.detalle and error.sugerencia)
        return error

    def test_archivo_inexistente_vacio_o_falso(self):
        self.assertEqual(self._error(validar_archivo_pdf, self.carpeta / "no_existe.pdf").titulo, "Archivo no encontrado")
        self.assertEqual(self._error(validar_archivo_pdf, self._archivo("vacio.pdf", b"")).titulo, "Archivo vacío")
        self.assertEqual(self._error(validar_archivo_pdf, self._archivo("falso.pdf", b"esto no es un pdf")).titulo, "El archivo no es un PDF")

    def test_pdf_sin_texto_y_pdf_protegido(self):
        escritor = PdfWriter()
        escritor.add_blank_page(width=612, height=792)
        with open(self.carpeta / "escaneado.pdf", "wb") as archivo:
            escritor.write(archivo)
        self.assertEqual(self._error(validar_archivo_pdf, self.carpeta / "escaneado.pdf").titulo, "El PDF no contiene texto")
        protegido = PdfWriter(clone_from=str(DECLARACION_PDF))
        protegido.encrypt("clave")
        with open(self.carpeta / "protegido.pdf", "wb") as archivo:
            protegido.write(archivo)
        self.assertEqual(self._error(validar_archivo_pdf, self.carpeta / "protegido.pdf").titulo, "PDF protegido con contraseña")

    def test_detecta_el_tipo_de_documento(self):
        self.assertEqual(detectar_tipo(leer_pdf(DECLARACION_PDF)), "declaracion")
        self.assertEqual(detectar_tipo(leer_pdf(FACTURA_PDF)), "factura")
        self.assertEqual(detectar_tipo("Informe de hardening de servidores Linux. Capítulo 1: introducción."), "desconocido")

    def test_archivos_intercambiados_se_corrigen_con_aviso(self):
        carga = cargar_documentos(FACTURA_PDF, DECLARACION_PDF)
        self.assertEqual(Path(carga.ruta_declaracion), DECLARACION_PDF)
        self.assertTrue(any("intercambiados" in aviso for aviso in carga.avisos))
        self.assertEqual(sorted(carga.declaracion["casillas"]), list(range(1, 16)))

    def test_dos_documentos_del_mismo_tipo(self):
        self.assertEqual(self._error(cargar_documentos, FACTURA_PDF, FACTURA_PDF).titulo, "Los dos archivos son facturas")
        self.assertEqual(self._error(cargar_documentos, DECLARACION_PDF, DECLARACION_PDF).titulo, "Los dos archivos son declaraciones")

    def test_casilla_no_encontrada_se_distingue_de_vacia(self):
        decl = declaracion_sintetica(VALORES_OK, {"factura_comercial"})
        del decl["casillas"][9]
        reporte = revisar(decl, FACTURA_OK)
        fila = next(f for f in reporte["resumen_casillas"] if f["casilla"] == 9)
        self.assertEqual(fila["estado"], "no_encontrada")
        self.assertIn((9, "campo_faltante"), tipos_por_casilla(reporte))


@unittest.skipUnless(DECLARACION_PDF.exists() and FACTURA_PDF.exists(), "faltan los PDF de ejemplo")
class TestInterfazWeb(unittest.TestCase):
    """La lógica del servidor web (sin levantar el servidor)."""

    def _cuerpo_multipart(self, campos, limite="----LimitePrueba123"):
        partes = [
            f'--{limite}\r\nContent-Disposition: form-data; name="{nombre}"; filename="{archivo}"\r\n'
            f"Content-Type: application/pdf\r\n\r\n".encode("utf-8") + datos + b"\r\n"
            for nombre, archivo, datos in campos
        ]
        return f"multipart/form-data; boundary={limite}", b"".join(partes) + f"--{limite}--\r\n".encode()

    def test_parsear_multipart_conserva_los_bytes(self):
        datos = DECLARACION_PDF.read_bytes()
        tipo, cuerpo = self._cuerpo_multipart([("declaracion", "declaración ñ.pdf", datos), ("factura", "f.pdf", b"%PDF-1.4 x")])
        archivos = interfaz.parsear_multipart(tipo, cuerpo)
        self.assertEqual(archivos["declaracion"], ("declaración ñ.pdf", datos))
        self.assertEqual(archivos["factura"], ("f.pdf", b"%PDF-1.4 x"))

    def test_revision_de_archivos_subidos(self):
        tipo, cuerpo = self._cuerpo_multipart([("declaracion", "d.pdf", DECLARACION_PDF.read_bytes()),
                                               ("factura", "f.pdf", FACTURA_PDF.read_bytes())])
        resultado = interfaz.revisar_archivos_subidos(interfaz.parsear_multipart(tipo, cuerpo))
        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["reporte"]["resumen"]["total_alertas"], 11)

    def test_errores_amables_en_la_subida(self):
        tipo, cuerpo = self._cuerpo_multipart([("declaracion", "d.pdf", DECLARACION_PDF.read_bytes())])
        sin_factura = interfaz.revisar_archivos_subidos(interfaz.parsear_multipart(tipo, cuerpo))
        self.assertFalse(sin_factura["ok"])
        self.assertIn("Falta el archivo", sin_factura["error"]["titulo"])
        tipo, cuerpo = self._cuerpo_multipart([("declaracion", "mi archivo.pdf", b"no soy pdf"), ("factura", "f.pdf", FACTURA_PDF.read_bytes())])
        falso = interfaz.revisar_archivos_subidos(interfaz.parsear_multipart(tipo, cuerpo))
        self.assertFalse(falso["ok"])
        self.assertIn("mi archivo.pdf", falso["error"]["detalle"])   # se muestra el nombre original, no el temporal

    def test_ejemplos_disponibles(self):
        ids = [e["id"] for e in interfaz.ejemplos_disponibles()]
        self.assertIn("original", ids)


DESC_DECLARACION = "Motores electricos trifasicos de baja poternica, uso industrial"
DESC_FACTURA = "Motores eléctricos trifásicos de baja potencia (< 0.75 kW), uso industrial, modelo PTM-075T"
RESPUESTA_MODELO = {
    "equivalencia": "equivalente", "confianza": 1.7,   # > 1: debe recortarse a 1
    "explicacion": "Misma mercancía; la declaración omite potencia y modelo.",
    "terminos_omitidos": ["< 0.75 kW", "modelo PTM-075T"],
    "hallazgos_ortografia": [
        {"palabra": "electricos", "sugerencia": "eléctricos", "tipo": "acentuacion", "explicacion": "Falta la tilde."},
        {"palabra": "poternica", "sugerencia": "potencia", "tipo": "error_tipografico"},
        {"palabra": "inventada", "sugerencia": "x", "tipo": "gramatica"},       # no está en el texto: se descarta
        {"palabra": "industrial", "sugerencia": "industrial", "tipo": "raro"},  # sin cambio: se descarta
    ],
}


class TestRevisorTexto(unittest.TestCase):
    """El contrato `analizar` con las dos implementaciones, y las salvaguardas del modelo."""

    def test_heuristico(self):
        analisis = RevisorHeuristico().analizar(DESC_DECLARACION, DESC_FACTURA, {"modelo", "eléctricos", "trifásicos", "potencia"})
        self.assertEqual(analisis.equivalencia, "equivalente")
        self.assertEqual({h.palabra for h in analisis.hallazgos}, {"electricos", "trifasicos", "poternica"})
        self.assertIn("PTM-075T", analisis.terminos_omitidos)
        self.assertEqual(analisis.origen, "heuristico")

    def test_llm_con_transporte_simulado_y_saneamiento(self):
        peticiones = []

        def transporte(cuerpo):
            peticiones.append(cuerpo)
            return "```json\n" + json.dumps(RESPUESTA_MODELO, ensure_ascii=False) + "\n```"

        revisor = RevisorLLM(ConfiguracionLLM("clave", "http://x/v1", "prueba"), transporte=transporte)
        analisis = revisor.analizar(DESC_DECLARACION, DESC_FACTURA, set())
        self.assertEqual(analisis.origen, "llm:prueba")
        self.assertEqual(analisis.equivalencia, "equivalente")
        self.assertEqual(analisis.confianza, 1.0)
        self.assertEqual([h.palabra for h in analisis.hallazgos], ["electricos", "poternica"])
        self.assertEqual(analisis.hallazgos[1].tipo, "error_tipografico")
        self.assertEqual(analisis.terminos_omitidos, ["< 0.75 kW", "modelo PTM-075T"])
        self.assertIsNone(analisis.aviso)
        cuerpo = peticiones[0]
        self.assertEqual((cuerpo["model"], cuerpo["temperature"]), ("prueba", 0))
        self.assertEqual([m["role"] for m in cuerpo["messages"]], ["system", "user", "assistant", "user"])
        self.assertIn(DESC_DECLARACION, cuerpo["messages"][-1]["content"])

    def test_llm_cae_al_metodo_basico_si_el_servicio_falla(self):
        def transporte_roto(cuerpo):
            raise ErrorLLM("no se pudo contactar el servicio")

        revisor = RevisorLLM(ConfiguracionLLM("clave", "http://x/v1", "prueba"), transporte=transporte_roto)
        analisis = revisor.analizar(DESC_DECLARACION, DESC_FACTURA, {"potencia"})
        self.assertEqual(analisis.origen, "heuristico")
        self.assertIn("método básico", analisis.aviso)
        self.assertEqual({h.palabra for h in analisis.hallazgos}, {"electricos", "trifasicos", "poternica"})

    def test_llm_respuesta_invalida_tambien_cae_al_metodo_basico(self):
        for respuesta in ("no soy json", '{"equivalencia": "quizas"}', "[]"):
            revisor = RevisorLLM(ConfiguracionLLM("clave", "http://x/v1", "prueba"), transporte=lambda c, r=respuesta: r)
            analisis = revisor.analizar(DESC_DECLARACION, DESC_FACTURA, set())
            self.assertEqual(analisis.origen, "heuristico", respuesta)
            self.assertIsNotNone(analisis.aviso)

    def test_transporte_reintenta_ante_servicio_saturado(self):
        import io
        import urllib.error
        from unittest import mock
        from agente import lenguaje

        def error_503(*args, **kwargs):
            cuerpo = io.BytesIO(b'{"error": {"message": "This model is currently experiencing high demand"}}')
            return urllib.error.HTTPError("http://x/v1/chat/completions", 503, "Unavailable", {}, cuerpo)

        class RespuestaOK:
            def read(self):
                return json.dumps({"choices": [{"message": {"content": "{\"ok\": true}"}}]}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        esperas = []
        config = ConfiguracionLLM("clave", "http://x/v1", "m", 5)
        # Dos fallos 503 y luego éxito: debe devolver la respuesta y haber esperado dos veces
        intentos = iter([error_503(), error_503(), RespuestaOK()])

        def urlopen_falso(peticion, timeout=None):
            resultado = next(intentos)
            if isinstance(resultado, Exception):
                raise resultado
            return resultado

        with mock.patch.object(lenguaje.urllib.request, "urlopen", urlopen_falso):
            contenido = lenguaje.transporte_http(config, dormir=esperas.append)({"model": "m", "messages": []})
        self.assertEqual(contenido, '{"ok": true}')
        self.assertEqual(len(esperas), 2)
        # Tres fallos seguidos: error claro que menciona la saturación y los intentos
        intentos = iter([error_503(), error_503(), error_503()])
        with mock.patch.object(lenguaje.urllib.request, "urlopen", urlopen_falso):
            with self.assertRaises(ErrorLLM) as contexto:
                lenguaje.transporte_http(config, dormir=lambda s: None)({"model": "m", "messages": []})
        self.assertIn("saturado", str(contexto.exception))
        self.assertIn("high demand", str(contexto.exception))

    def test_configuracion_desde_variables(self):
        sin_env = Path(tempfile.gettempdir()) / "no_existe.env"
        self.assertIsNone(cargar_configuracion({}, ruta_env=sin_env))
        self.assertIsNone(cargar_configuracion({"LLM_API_KEY": "sk-x", "LLM_DESACTIVAR": "1"}, ruta_env=sin_env))
        config = cargar_configuracion({"LLM_API_KEY": "sk-x", "LLM_MODELO": "otro", "LLM_TIEMPO_MAXIMO": "12"}, ruta_env=sin_env)
        self.assertEqual((config.api_key, config.modelo, config.tiempo_maximo, config.proveedor), ("sk-x", "otro", 12, "openai_compatible"))
        self.assertTrue(config.base_url.startswith("https://"))
        # Claude: por prefijo de la clave, por LLM_PROVEEDOR o por ANTHROPIC_API_KEY; modelo por defecto el de lenguaje.py
        from agente.lenguaje import MODELO_ANTHROPIC_POR_DEFECTO
        claude = cargar_configuracion({"LLM_API_KEY": "sk-ant-abc"}, ruta_env=sin_env)
        self.assertEqual((claude.proveedor, claude.modelo, claude.servicio), ("anthropic", MODELO_ANTHROPIC_POR_DEFECTO, "api.anthropic.com"))
        self.assertTrue(MODELO_ANTHROPIC_POR_DEFECTO.startswith("claude-"))
        claude = cargar_configuracion({"LLM_PROVEEDOR": "anthropic", "LLM_API_KEY": "otra", "LLM_MODELO": "claude-sonnet-5"}, ruta_env=sin_env)
        self.assertEqual((claude.proveedor, claude.modelo), ("anthropic", "claude-sonnet-5"))
        claude = cargar_configuracion({"ANTHROPIC_API_KEY": "sk-ant-xyz"}, ruta_env=sin_env)
        self.assertEqual((claude.proveedor, claude.api_key), ("anthropic", "sk-ant-xyz"))
        # Un modelo que no es de Claude (heredado de otra configuración) se reemplaza por el predeterminado
        claude = cargar_configuracion({"LLM_API_KEY": "sk-ant-xyz", "LLM_MODELO": "otro-modelo"}, ruta_env=sin_env)
        self.assertEqual(claude.modelo, MODELO_ANTHROPIC_POR_DEFECTO)

    def test_revisor_claude_con_cliente_simulado(self):
        from agente.lenguaje_claude import RespuestaEsquema, RevisorClaude

        class RespuestaFalsa:
            stop_reason = "end_turn"
            parsed_output = RespuestaEsquema(**{**RESPUESTA_MODELO, "hallazgos_ortografia": [
                {**h, "explicacion": h.get("explicacion", "")} for h in RESPUESTA_MODELO["hallazgos_ortografia"] if h["tipo"] != "raro"]})

        peticiones = []

        class ClienteFalso:
            class messages:  # noqa: N801 - imita client.messages.parse(...)
                @staticmethod
                def parse(**kwargs):
                    peticiones.append(kwargs)
                    return RespuestaFalsa()

        config = ConfiguracionLLM("sk-ant-prueba", "https://api.anthropic.com", "claude-opus-5", 30, "anthropic")
        revisor = RevisorClaude(config, cliente=ClienteFalso())
        analisis = revisor.analizar(DESC_DECLARACION, DESC_FACTURA, set())
        self.assertEqual(analisis.origen, "llm:claude-opus-5")
        self.assertEqual([h.palabra for h in analisis.hallazgos], ["electricos", "poternica"])   # "inventada" se descarta
        self.assertEqual(analisis.confianza, 1.0)
        self.assertEqual(peticiones[0]["model"], "claude-opus-5")
        self.assertIs(peticiones[0]["output_format"], RespuestaEsquema)
        self.assertEqual(peticiones[0]["messages"][-1]["role"], "user")

        class ClienteRoto:
            class messages:  # noqa: N801
                @staticmethod
                def parse(**kwargs):
                    raise RuntimeError("sin red")

        analisis = RevisorClaude(config, cliente=ClienteRoto()).analizar(DESC_DECLARACION, DESC_FACTURA, {"potencia"})
        self.assertEqual(analisis.origen, "heuristico")
        self.assertIn("método básico", analisis.aviso)

    def test_crear_revisor_para_claude(self):
        from agente.lenguaje_claude import RevisorClaude
        config = ConfiguracionLLM("sk-ant-prueba", "https://api.anthropic.com", "claude-opus-5", 30, "anthropic")
        revisor, info = crear_revisor(config=config, comprobar_conexion=False)
        self.assertIsInstance(revisor, RevisorClaude)
        self.assertEqual((info["modo"], info["modelo"], info["servicio"]), ("llm", "claude-opus-5", "api.anthropic.com"))

    def test_crear_revisor(self):
        revisor, info = crear_revisor(forzar_heuristico=True)
        self.assertIsInstance(revisor, RevisorHeuristico)
        self.assertEqual(info["modo"], "heuristico")
        inalcanzable = ConfiguracionLLM("sk-x", "http://127.0.0.1:9/v1", "m")
        revisor, info = crear_revisor(config=inalcanzable, comprobar_conexion=True)
        self.assertIsInstance(revisor, RevisorHeuristico)
        self.assertIn("no responde", info["aviso"])
        revisor, info = crear_revisor(config=inalcanzable, comprobar_conexion=False)
        self.assertIsInstance(revisor, RevisorLLM)
        self.assertEqual(info["modo"], "llm")

    @unittest.skipUnless(DECLARACION_PDF.exists() and FACTURA_PDF.exists(), "faltan los PDF de ejemplo")
    def test_motor_completo_con_revisor_llm(self):
        revisor = RevisorLLM(ConfiguracionLLM("clave", "http://x/v1", "prueba"),
                             transporte=lambda c: json.dumps(RESPUESTA_MODELO, ensure_ascii=False))
        reporte = revisar(extraer_declaracion(DECLARACION_PDF), extraer_factura(FACTURA_PDF), revisor=revisor)
        de_texto = [a for a in reporte["alertas"] if a["casilla"] == 8]
        self.assertTrue(de_texto and all(a["analisis"] == "llm:prueba" for a in de_texto))
        self.assertEqual(sum(1 for a in de_texto if a["tipo"] == "ortografia"), 3)   # 2 hallazgos + redacción
        self.assertEqual(reporte["analisis_texto"]["revisor"], "llm:prueba")
        self.assertEqual(reporte["analisis_texto"]["casillas"]["8"]["equivalencia"] if "8" in reporte["analisis_texto"]["casillas"]
                         else reporte["analisis_texto"]["casillas"][8]["equivalencia"], "equivalente")


class TestGeneradorDeEjemplos(unittest.TestCase):
    """El generador debe reproducir los PDF de la prueba con extracción idéntica (requiere Chrome o Edge)."""

    def test_caso_original_se_extrae_igual_que_los_pdf_de_la_prueba(self):
        import tempfile
        sys.path.insert(0, str(BASE / "ejemplos"))
        import generar_ejemplos
        try:
            navegador = generar_ejemplos.encontrar_navegador()
        except SystemExit:
            self.skipTest("no hay Chrome ni Edge para imprimir a PDF")
        if not (DECLARACION_PDF.exists() and FACTURA_PDF.exists()):
            self.skipTest("faltan los PDF de ejemplo")
        with tempfile.TemporaryDirectory() as carpeta:
            decl_pdf, fact_pdf = generar_ejemplos.generar("original", Path(carpeta), navegador)
            d0, d1 = extraer_declaracion(DECLARACION_PDF), extraer_declaracion(decl_pdf)
            f0, f1 = extraer_factura(FACTURA_PDF), extraer_factura(fact_pdf)
        self.assertEqual({n: c["valor"] for n, c in d0["casillas"].items()}, {n: c["valor"] for n, c in d1["casillas"].items()})
        self.assertEqual(d0["documentos_soporte"], d1["documentos_soporte"])
        for campo in ("numero", "fecha", "pais_origen", "incoterm", "moneda", "items", "fob", "flete", "seguro", "cif", "bultos"):
            self.assertEqual(f0[campo], f1[campo], campo)


if __name__ == "__main__":
    unittest.main()
