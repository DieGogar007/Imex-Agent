#!/usr/bin/env python
"""
Generador de casos de prueba: produce pares declaración + factura en PDF con el
mismo formato que los documentos de ejemplo de la prueba técnica.

Los PDF originales se crearon desde HTML con Chrome (productor "Skia/PDF"), así
que aquí se hace lo mismo: se arma el HTML con los valores de cada caso y se
imprime a PDF con Chrome o Edge en modo sin cabeza (headless).

Uso:
    python ejemplos/generar_ejemplos.py --caso todas_discrepancias
    python ejemplos/generar_ejemplos.py --caso original --salida /tmp/verificacion
    python ejemplos/generar_ejemplos.py --listar

Para agregar un caso nuevo basta con añadir una entrada al diccionario CASOS.
"""
from __future__ import annotations

import argparse
import html
import os
import shutil
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Casos de prueba
# ---------------------------------------------------------------------------

FACTURA_BASE = {
    "numero": "FC-2026-00417",
    "fecha": "15 de junio de 2026",
    "exportador": {"nombre": "Shenzhen PowerTech Motors Co., Ltd.",
                   "direccion": ["No. 88 Baoan Industrial Avenue", "Shenzhen, Guangdong, China 518101"]},
    "importador": {"nombre": "Distribuidora Andina S.A.S.",
                   "direccion": ["NIT 900.123.456-7", "Calle 24 No. 68-45, Bodega 12", "Bogotá D.C., Colombia"]},
    "pais_origen": "República Popular China",
    "pais_embarque": "China",
    "incoterm": "CIF Cartagena, Colombia (Incoterms 2020)",
    "puerto_destino": "Cartagena, Colombia",
    "moneda": "Dólares de los Estados Unidos (USD)",
    "forma_pago": "Transferencia bancaria — 30 días",
    "items": [{"item": 1, "descripcion": "Motores eléctricos trifásicos de baja potencia (< 0.75 kW), uso industrial, modelo PTM-075T",
               "cantidad": "500 unidades", "precio_unitario": "36.90", "subtotal": "18,450.00"}],
    "fob": "18,450.00", "flete": "620.00", "seguro": "95.00", "cif": "19,165.00",
    "bultos": "25 cajas de cartón (bultos)", "unidades_por_bulto": "20 unidades por caja",
    "peso_bruto": None,   # la factura original no indica el peso; si se indica, aparece en la nota de embalaje
}

DECLARACION_BASE = {
    "declarante": "Distribuidora Andina S.A.S. — NIT 900.123.456-7",
    "referencia": "IMP-2026-0638",
    "casillas": {
        1: "FC-2026-00147", 2: "15 de junio de 2026", 3: "Shenzhen PowerTech Motors Co., Ltd.",
        4: "Distribuidora Andina S.A.S. — NIT 900.123.456-7", 5: "Corea del Sur", 6: "CIF Cartagena, Colombia",
        7: "USD", 8: "Motores electricos trifasicos de baja poternica, uso industrial", 9: "500 unidades",
        10: None, 11: "1.250,00 kg", 12: "18.540,00", 13: "620,00", 14: "95,00", 15: "19.165,00",
    },
    "carpeta": [
        ("Factura comercial", True, "FC-2026-00417.pdf"),
        ("Lista de empaque", False, "no se encontró en la carpeta"),
        ("Certificado de origen", False, "no se encontró en la carpeta"),
        ("BL / documento de transporte", False, "no se encontró en la carpeta"),
    ],
}

CASOS = {
    # Réplica del par de documentos entregado con la prueba (sirve para verificar el generador).
    "original": {
        "sufijo": "ejemplo_regenerado",
        "declaracion": DECLARACION_BASE,
        "factura": FACTURA_BASE,
    },
    # Caso de estrés: las 15 casillas difieren de la factura. La factura es la misma de la
    # prueba, salvo que su nota de embalaje indica el peso bruto (para poder contrastar la casilla 11).
    "todas_discrepancias": {
        "sufijo": "discrepancias",
        "declaracion": {
            **DECLARACION_BASE,
            "referencia": "IMP-2026-0639",
            "casillas": {
                1: "FC-2026-00174",                                    # dígitos transpuestos (00417)
                2: "16 de junio de 2026",                              # un día después
                3: "Guangzhou PowerTech Motors Co., Ltd.",             # otra ciudad / otra empresa
                4: "Distribuidora Andina S.A.S. — NIT 900.123.465-7",  # NIT con dígitos transpuestos
                5: "Vietnam",                                          # la factura dice China
                6: "FOB Shenzhen, China",                              # la factura dice CIF Cartagena
                7: "EUR",                                              # la factura dice USD
                8: "Motores electricos monofasicos de alta potencai, uso domestico",  # otra mercancía + 4 errores
                9: "450 unidades",                                     # la factura dice 500
                10: "30",                                              # la factura dice 25 cajas
                11: "1.250,00 kg",                                     # la factura dirá 1.310,00 kg
                12: "17.450,00",                                       # FOB 18,450.00
                13: "650,00",                                          # flete 620.00
                14: "59,00",                                           # seguro 95.00 (transpuesto)
                15: "18.159,00",                                       # = 17.450 + 650 + 59 (coherente, pero ≠ 19,165.00)
            },
        },
        "factura": {**FACTURA_BASE, "peso_bruto": "1.310,00 kg"},
    },
    # Declaración correcta pero escrita con formatos distintos a los de la factura (fecha ISO, montos en
    # formato anglosajón, país por nombre corto, unidad abreviada...). No debe generar ninguna discrepancia.
    "formatos_distintos": {
        "sufijo": "formatos_distintos",
        "declaracion": {
            **DECLARACION_BASE,
            "referencia": "IMP-2026-0640",
            "casillas": {
                1: "FC-2026-00417",
                2: "2026-06-15",                                        # fecha en formato ISO
                3: "SHENZHEN POWERTECH MOTORS CO., LTD.",              # mayúsculas
                4: "Distribuidora Andina SAS — NIT 9001234567",         # sin puntos ni guion
                5: "China",                                            # nombre corto del país
                6: "CIF Cartagena, Colombia (Incoterms 2020)",          # con versión de las reglas
                7: "Dólares de los Estados Unidos (USD)",              # nombre de la moneda
                8: "Motores eléctricos trifásicos de baja potencia (< 0.75 kW), uso industrial, modelo PTM-075T",  # completa (ocupa dos renglones)
                9: "500 und.",                                         # unidad abreviada
                10: "25 bultos",
                11: "1250 kg",                                         # sin separadores de miles ni decimales
                12: "USD 18,450.00",                                   # formato anglosajón con código de moneda
                13: "620",
                14: "95.00",
                15: "19,165.00",
            },
        },
        "factura": {**FACTURA_BASE, "peso_bruto": "1.250,00 kg"},
    },
    # Factura con dos ítems, tres casillas obligatorias sin diligenciar, CIF con dígitos transpuestos
    # (discrepancia + inconsistencia interna) y lista de empaque presente en la carpeta.
    "faltantes_y_dos_items": {
        "sufijo": "faltantes",
        "declaracion": {
            **DECLARACION_BASE,
            "referencia": "IMP-2026-0641",
            "casillas": {
                1: "FC-2026-00520",
                2: None,                                               # obligatoria, vacía
                3: "Shenzhen PowerTech Motors Co., Ltd.",
                4: "Distribuidora Andina S.A.S. — NIT 900.123.456-7",
                5: "República Popular China",
                6: None,                                               # obligatoria, vacía
                7: None,                                               # obligatoria, vacía
                8: "Motores electricos trifásicos de baja potencia, uso industrial, modelos PTM-075T y PTM-150T",  # una tilde; omite las potencias
                9: "500 unidades",                                     # 300 + 200 de la factura
                10: "25",
                11: "1.290,00 kg",
                12: "21.550,00",
                13: "700,00",
                14: "110,00",
                15: "22.630,00",                                       # transpuesto: la factura dice 22,360.00 = 12 + 13 + 14
            },
            "carpeta": [
                ("Factura comercial", True, "FC-2026-00520.pdf"),
                ("Lista de empaque", True, "PL-2026-00520.pdf"),
                ("Certificado de origen", False, "no se encontró en la carpeta"),
                ("BL / documento de transporte", False, "no se encontró en la carpeta"),
            ],
        },
        "factura": {
            **FACTURA_BASE,
            "numero": "FC-2026-00520",
            "fecha": "3 de julio de 2026",
            "items": [
                {"item": 1, "descripcion": "Motores eléctricos trifásicos de baja potencia (< 0.75 kW), uso industrial, modelo PTM-075T",
                 "cantidad": "300 unidades", "precio_unitario": "36.90", "subtotal": "11,070.00"},
                {"item": 2, "descripcion": "Motores eléctricos trifásicos de media potencia (1.5 kW), uso industrial, modelo PTM-150T",
                 "cantidad": "200 unidades", "precio_unitario": "52.40", "subtotal": "10,480.00"},
            ],
            "fob": "21,550.00", "flete": "700.00", "seguro": "110.00", "cif": "22,360.00",
            "peso_bruto": "1.290,00 kg",
        },
    },
}

# ---------------------------------------------------------------------------
# Plantillas HTML (mismo diseño que los PDF de la prueba)
# ---------------------------------------------------------------------------

ETIQUETAS = {
    1: "Número de factura comercial", 2: "Fecha de factura", 3: "Exportador", 4: "Importador",
    5: "País de origen", 6: "Incoterm", 7: "Moneda de negociación", 8: "Descripción de la mercancía",
    9: "Cantidad (unidades)", 10: "Número de bultos", 11: "Peso bruto (kg)", 12: "Valor FOB (USD)",
    13: "Flete (USD)", 14: "Seguro (USD)", 15: "Valor CIF total (USD)",
}
SECCIONES = [("Datos del documento soporte", (1, 2, 3, 4)), ("Origen y condiciones de negociación", (5, 6, 7)),
             ("Mercancía", (8, 9, 10, 11)), ("Valores declarados", (12, 13, 14, 15))]

FUENTE = '"Liberation Sans", Arial, Helvetica, sans-serif'


def e(texto) -> str:
    return html.escape(str(texto), quote=False)


def html_declaracion(d: dict) -> str:
    filas = []
    for titulo, numeros in SECCIONES:
        filas.append(f'<tr><th class="seccion" colspan="3">{e(titulo)}</th></tr>')
        for n in numeros:
            valor = d["casillas"].get(n)
            if valor is None:
                celda = '<td class="val vacio">(sin diligenciar)</td>'
            else:
                celda = f'<td class="val">{e(valor)}</td>'
            filas.append(f'<tr><td class="num">{n}</td><td class="etq">{e(ETIQUETAS[n])}</td>{celda}</tr>')
    carpeta = "".join(
        f'<li><span class="{"ok" if disp else "no"}">{"✔" if disp else "✘"}</span> {e(nombre)} — {e(detalle)}</li>'
        for nombre, disp, detalle in d["carpeta"]
    )
    return f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8"><title>declaracion.html</title>
<style>
  @page {{ size: Letter; margin: 0.385in; }}
  body {{ font-family: {FUENTE}; color: #1a1a1a; font-size: 11.5px; margin: 0; padding: 0 42px; }}
  h1 {{ font-size: 16px; text-align: center; color: #444; margin: 8px 0 4px; }}
  .sub {{ text-align: center; color: #666; font-size: 11px; margin: 0 0 10px; }}
  .regla {{ border-top: 1px solid #444; border-bottom: 1px solid #444; height: 1px; margin-bottom: 18px; }}
  table.casillas {{ width: 100%; border-collapse: collapse; }}
  table.casillas th, table.casillas td {{ border: 1px solid #b8b8b8; padding: 5px 9px; height: 14px; text-align: left; }}
  th.seccion {{ background: #3a3a3a; color: #fff; font-size: 10.5px; font-weight: bold; }}
  td.num {{ width: 34px; background: #f2f2f2; text-align: center; font-weight: bold; color: #444; }}
  td.etq {{ width: 192px; font-weight: bold; }}
  td.vacio {{ color: #b33333; font-style: italic; }}
  .carpeta {{ margin-top: 20px; background: #f7f7f7; border: 1px solid #e0e0e0; padding: 10px 14px; }}
  .carpeta ul {{ list-style: none; margin: 6px 0 0; padding: 0 0 0 18px; }}
  .carpeta li {{ line-height: 16px; }}
  .carpeta li span {{ display: inline-block; width: 16px; }}
  .ok {{ color: #2e7d32; }} .no {{ color: #b33333; }}
  .pie {{ display: flex; justify-content: space-between; color: #555; margin-top: 26px; }}
  .footer {{ text-align: center; color: #888; font-size: 10.5px; margin-top: 40px; }}
</style></head>
<body>
  <h1>FORMULARIO DE DECLARACIÓN DE IMPORTACIÓN (PREVIA)</h1>
  <p class="sub">{e(d["declarante"])} &nbsp;|&nbsp; Referencia interna: {e(d["referencia"])}</p>
  <div class="regla"></div>
  <table class="casillas">
    {"".join(filas)}
  </table>
  <div class="carpeta">Carpeta de documentos soporte (SharePoint) asociada a esta declaración:
    <ul>{carpeta}</ul>
  </div>
  <div class="pie"><span>Elaborado por: Analista de Comercio Exterior</span><span>Estado: Pendiente de revisión</span></div>
  <div class="footer">Referencia interna {e(d["referencia"])} — Declaración previa — Página 1 de 1</div>
</body></html>
"""


def html_factura(f: dict) -> str:
    items = "".join(
        f'<tr><td class="c">{it["item"]}</td><td>{e(it["descripcion"])}</td><td>{e(it["cantidad"])}</td>'
        f'<td class="r">{e(it["precio_unitario"])}</td><td class="r">{e(it["subtotal"])}</td></tr>'
        for it in f["items"]
    )
    peso = f' Peso bruto total: {e(f["peso_bruto"])}.' if f.get("peso_bruto") else ""
    exp, imp = f["exportador"], f["importador"]
    return f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8"><title>factura.html</title>
<style>
  @page {{ size: Letter; margin: 0.385in; }}
  body {{ font-family: {FUENTE}; color: #1a1a1a; font-size: 11.5px; margin: 0; padding: 0 42px; }}
  table {{ border-collapse: collapse; width: 100%; }}
  .cab td {{ vertical-align: top; padding: 0; }}
  .empresa {{ font-size: 19px; font-weight: bold; color: #1f3864; margin-top: 6px; }}
  .dir {{ color: #555; font-size: 11px; line-height: 17px; }}
  .der {{ text-align: right; }}
  .titulo {{ font-size: 20px; font-weight: bold; color: #1f3864; margin-top: 6px; }}
  .der div {{ line-height: 18px; }}
  .barra {{ border-top: 3px solid #1f3864; margin: 14px 0 26px; }}
  .partes td {{ vertical-align: top; width: 50%; padding: 0 0 0 13px; border-left: 3px solid #d0d0d0; }}
  .rotulo {{ color: #888; font-size: 10.5px; font-weight: bold; margin-bottom: 4px; }}
  .nombre {{ font-size: 13px; font-weight: bold; margin-bottom: 4px; }}
  .partes div.l {{ color: #333; line-height: 17px; }}
  .cond {{ margin-top: 26px; }}
  .cond th, .cond td {{ border: 1px solid #d0d0d0; padding: 5px 10px; text-align: left; }}
  .cond th {{ background: #f2f4f7; color: #1f3864; font-weight: bold; width: 124px; }}
  .items {{ margin-top: 18px; table-layout: fixed; }}
  .items th {{ background: #1f3864; color: #fff; font-size: 11px; padding: 6px 8px; text-align: left; border: 1px solid #1f3864; }}
  .items td {{ border: 1px solid #d8d8d8; padding: 6px 8px; vertical-align: middle; }}
  .items td.r, .items th.r {{ text-align: right; }}
  .items td.c {{ text-align: center; }}
  .totales {{ width: 270px; margin: 20px 0 0 auto; font-size: 12px; }}
  .totales td {{ padding: 5px 6px; border-bottom: 1px solid #e5e5e5; }}
  .totales td.r {{ text-align: right; }}
  .totales tr.cif td {{ font-size: 13.5px; font-weight: bold; color: #1f3864; border-bottom: none; padding-top: 10px; }}
  .nota {{ margin-top: 28px; background: #f7f7f7; border: 1px solid #e0e0e0; padding: 10px 14px; line-height: 17px; }}
  .footer {{ display: flex; justify-content: space-between; color: #888; font-size: 10.5px; margin-top: 44px; }}
</style></head>
<body>
  <table class="cab"><tr>
    <td>
      <div class="empresa">{e(exp["nombre"].upper())}</div>
      <div class="dir">No. 88 Baoan Industrial Avenue, Bao'an District</div>
      <div class="dir">Shenzhen, Guangdong, China 518101</div>
      <div class="dir">Tel: +86 755 8891 2200 &nbsp;·&nbsp; export@powertechmotors.cn</div>
    </td>
    <td class="der">
      <div class="titulo">COMMERCIAL INVOICE</div>
      <div>No. {e(f["numero"])}</div>
      <div>Fecha de emisión: {e(f["fecha"])}</div>
    </td>
  </tr></table>
  <div class="barra"></div>
  <table class="partes"><tr>
    <td><div class="rotulo">EXPORTADOR / SHIPPER</div><div class="nombre">{e(exp["nombre"])}</div>
        {"".join(f'<div class="l">{e(l)}</div>' for l in exp["direccion"])}</td>
    <td><div class="rotulo">IMPORTADOR / CONSIGNEE</div><div class="nombre">{e(imp["nombre"])}</div>
        {"".join(f'<div class="l">{e(l)}</div>' for l in imp["direccion"])}</td>
  </tr></table>
  <table class="cond">
    <tr><th>País de origen</th><td>{e(f["pais_origen"])}</td><th>País de embarque</th><td>{e(f["pais_embarque"])}</td></tr>
    <tr><th>Incoterm</th><td>{e(f["incoterm"])}</td><th>Puerto de destino</th><td>{e(f["puerto_destino"])}</td></tr>
    <tr><th>Moneda</th><td>{e(f["moneda"])}</td><th>Forma de pago</th><td>{e(f["forma_pago"])}</td></tr>
  </table>
  <table class="items">
    <colgroup><col style="width:65px"><col style="width:289px"><col style="width:92px"><col style="width:105px"><col style="width:106px"></colgroup>
    <thead><tr><th>Ítem</th><th>Descripción de la mercancía</th><th>Cantidad</th><th class="r">Precio unitario (USD)</th><th class="r">Subtotal (USD)</th></tr></thead>
    <tbody>{items}</tbody>
  </table>
  <table class="totales">
    <tr><td>Valor FOB</td><td class="r">USD {e(f["fob"])}</td></tr>
    <tr><td>Flete internacional</td><td class="r">USD {e(f["flete"])}</td></tr>
    <tr><td>Seguro</td><td class="r">USD {e(f["seguro"])}</td></tr>
    <tr class="cif"><td>Valor CIF total</td><td class="r">USD {e(f["cif"])}</td></tr>
  </table>
  <div class="nota">Mercancía embalada en <b>{e(f["bultos"])}</b>, {e(f["unidades_por_bulto"])}.{peso} Detalle de pesos y dimensiones por bulto: ver <b>lista de empaque (packing list)</b> adjunta por separado.</div>
  <div class="footer"><span>{e(exp["nombre"])} — Commercial Invoice {e(f["numero"])}</span><span>Página 1 de 1</span></div>
</body></html>
"""


# ---------------------------------------------------------------------------
# Impresión a PDF con Chrome / Edge sin cabeza
# ---------------------------------------------------------------------------

RUTAS_NAVEGADOR = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/microsoft-edge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


def encontrar_navegador(explicito: str | None = None) -> str:
    candidatos = [explicito, os.environ.get("NAVEGADOR")] + RUTAS_NAVEGADOR
    for ruta in candidatos:
        if ruta and Path(ruta).exists():
            return ruta
    for nombre in ("chrome", "google-chrome", "chromium", "msedge"):
        encontrado = shutil.which(nombre)
        if encontrado:
            return encontrado
    raise SystemExit("No se encontró Chrome ni Edge. Indique la ruta con --navegador o la variable NAVEGADOR.")


def imprimir_pdf(navegador: str, html_path: Path, pdf_path: Path) -> None:
    comando = [
        navegador, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-pdf-header-footer",
        f"--print-to-pdf={pdf_path}", html_path.resolve().as_uri(),
    ]
    resultado = subprocess.run(comando, capture_output=True, text=True, timeout=120)
    if not pdf_path.exists():
        raise SystemExit(f"Chrome no generó {pdf_path}:\n{resultado.stderr}")


def generar(nombre_caso: str, salida: Path, navegador: str | None = None) -> tuple[Path, Path]:
    caso = CASOS[nombre_caso]
    salida.mkdir(parents=True, exist_ok=True)
    carpeta_html = salida / "html"
    carpeta_html.mkdir(exist_ok=True)
    sufijo = caso["sufijo"]
    pares = [
        (f"declaracion_importacion_{sufijo}", html_declaracion(caso["declaracion"])),
        (f"factura_comercial_{sufijo}", html_factura(caso["factura"])),
    ]
    ruta_navegador = encontrar_navegador(navegador)
    pdfs = []
    for nombre, contenido in pares:
        html_path = carpeta_html / f"{nombre}.html"
        html_path.write_text(contenido, encoding="utf-8")
        pdf_path = salida / f"{nombre}.pdf"
        imprimir_pdf(ruta_navegador, html_path, pdf_path)
        pdfs.append(pdf_path)
    return pdfs[0], pdfs[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Genera pares declaración + factura en PDF para probar el agente.")
    parser.add_argument("--caso", default="todas_discrepancias", choices=sorted(CASOS))
    parser.add_argument("--todos", action="store_true", help="genera todos los casos (excepto 'original')")
    parser.add_argument("--salida", default=str(BASE), help="carpeta de salida (por defecto, ejemplos/)")
    parser.add_argument("--navegador", default=None, help="ruta a chrome.exe o msedge.exe")
    parser.add_argument("--listar", action="store_true", help="muestra los casos disponibles")
    args = parser.parse_args(argv)
    if args.listar:
        for nombre, caso in CASOS.items():
            print(f"{nombre:<22} -> *_{caso['sufijo']}.pdf")
        return 0
    casos = [c for c in CASOS if c != "original"] if args.todos else [args.caso]
    for nombre in casos:
        decl, fact = generar(nombre, Path(args.salida), args.navegador)
        print(f"Caso '{nombre}' generado:\n  {decl}\n  {fact}")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
