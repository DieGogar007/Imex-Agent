#!/usr/bin/env python
"""
Agente revisor de declaración de importación — línea de comandos.

Uso:
    python revisar_declaracion.py                      # usa los PDF de ejemplos/
    python revisar_declaracion.py --declaracion mi_declaracion.pdf --factura mi_factura.pdf --salida salida/

Si prefieres no escribir rutas, usa la interfaz gráfica:  python interfaz.py  (o doble clic en Iniciar_revisor.bat)

Genera:
    salida/alertas.json           -> sólo la lista de alertas (formato pedido en la prueba)
    salida/reporte_completo.json  -> extracción de ambos documentos, estado por casilla y alertas

Códigos de salida: 0 = revisión hecha (haya o no alertas); 2 = los archivos no se pudieron usar (ver mensaje).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agente.carga import ErrorDocumento, cargar_documentos
from agente.lenguaje import crear_revisor
from agente.validacion import revisar, serializar

BASE = Path(__file__).resolve().parent
ICONO_ESTADO = {
    "ok": "OK", "ok_provisional": "OK (provisional)", "discrepancia": "DISCREPANCIA",
    "faltante": "SIN DILIGENCIAR", "no_encontrada": "NO ENCONTRADA EN EL PDF", "no_validable": "NO VALIDABLE",
    "vacia_opcional": "vacía (opcional)",
}


def imprimir_resumen(reporte: dict) -> None:
    meta = reporte["metadata"]
    print("=" * 96)
    print(f"REVISIÓN DE DECLARACIÓN {meta.get('referencia_interna') or ''}  —  {meta.get('declarante') or ''}")
    print("=" * 96)
    print("\nDocumentos soporte en la carpeta:")
    for doc in reporte["documentos_soporte"].values():
        marca = "[x]" if doc["disponible"] else "[ ]"
        print(f"  {marca} {doc['nombre']}: {doc['detalle'] or ''}")

    print("\nEstado por casilla:")
    print(f"  {'#':>2}  {'Campo':<28} {'Valor declarado':<48} {'Estado'}")
    for fila in reporte["resumen_casillas"]:
        valor = fila["valor_declaracion"] or "(vacío)"
        print(f"  {fila['casilla']:>2}  {fila['campo']:<28} {valor[:47]:<48} {ICONO_ESTADO.get(fila['estado'], fila['estado'])}")

    res = reporte["resumen"]
    print(f"\nAlertas: {res['total_alertas']}  |  por tipo: {res['por_tipo']}  |  por severidad: {res['por_severidad']}")
    print("-" * 96)
    for a in reporte["alertas"]:
        casilla = f"casilla {a['casilla']}" if a["casilla"] is not None else "factura"
        print(f"[{a['severidad'].upper():5}] {a['tipo']:<22} {casilla:<11} {a['campo']}")
        if a["valor_declaracion"] is not None or a["valor_fuente"] is not None:
            print(f"        declaración: {a['valor_declaracion']!s:<45} fuente: {a['valor_fuente']}")
        print(f"        {a['detalle']}")
    print("-" * 96)
    for v in reporte["verificaciones_factura"]:
        print(f"  factura · {v['regla']}: {'cumple' if v['cumple'] else 'NO CUMPLE'} ({v['detalle']})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Revisa una declaración de importación contra su factura comercial.")
    parser.add_argument("--declaracion", default=str(BASE / "ejemplos" / "declaracion_importacion_ejemplo.pdf"))
    parser.add_argument("--factura", default=str(BASE / "ejemplos" / "factura_comercial_ejemplo.pdf"))
    parser.add_argument("--salida", default=str(BASE / "salida"), help="carpeta donde se escriben los JSON")
    parser.add_argument("--silencioso", action="store_true", help="no imprimir el resumen en consola")
    parser.add_argument("--sin-llm", action="store_true", help="revisar el texto con el método básico aunque haya modelo configurado")
    args = parser.parse_args(argv)

    try:
        carga = cargar_documentos(args.declaracion, args.factura)
    except ErrorDocumento as error:
        print(f"\nNo se pudo hacer la revisión — {error.titulo}\n  {error.detalle}", file=sys.stderr)
        if error.sugerencia:
            print(f"  Qué hacer: {error.sugerencia}", file=sys.stderr)
        return 2

    revisor, info_llm = crear_revisor(forzar_heuristico=args.sin_llm)
    if info_llm["modo"] == "llm":
        print(f"Revisión de texto: modelo de lenguaje «{info_llm['modelo']}» en {info_llm['servicio']}")
    else:
        print(f"Revisión de texto: método básico. {info_llm['aviso']}")
    for aviso in carga.avisos:
        print(f"AVISO: {aviso}")
    reporte = revisar(carga.declaracion, carga.factura, revisor=revisor)
    reporte["avisos"] = carga.avisos + reporte["analisis_texto"]["avisos"]
    for aviso in reporte["analisis_texto"]["avisos"]:
        print(f"AVISO: {aviso}")

    salida = Path(args.salida)
    salida.mkdir(parents=True, exist_ok=True)
    (salida / "alertas.json").write_text(
        json.dumps(reporte["alertas"], ensure_ascii=False, indent=2, default=serializar), encoding="utf-8")
    (salida / "reporte_completo.json").write_text(
        json.dumps(reporte, ensure_ascii=False, indent=2, default=serializar), encoding="utf-8")

    if not args.silencioso:
        imprimir_resumen(reporte)
        print(f"\nArchivos generados en {salida}: alertas.json, reporte_completo.json")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
