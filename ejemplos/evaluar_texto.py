#!/usr/bin/env python
"""
Evaluación del revisor de texto (modelo de lenguaje o método básico) sobre casos con
respuesta conocida. Un modelo no se prueba con asserts: se mide cuántas veces acierta.

    python ejemplos/evaluar_texto.py            # usa el modelo si hay clave (.env o variables)
    python ejemplos/evaluar_texto.py --sin-llm  # mide el método básico, para comparar

Para cada caso imprime la equivalencia esperada y la obtenida, los errores de ortografía
esperados y los detectados, y un veredicto. Al final, el total de aciertos.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from agente import ortografia  # noqa: E402
from agente.lenguaje import crear_revisor  # noqa: E402
from agente.normalizacion import quitar_acentos  # noqa: E402

FACTURA = "Motores eléctricos trifásicos de baja potencia (< 0.75 kW), uso industrial, modelo PTM-075T"
FACTURA_DOS = FACTURA + " | Motores eléctricos trifásicos de media potencia (1.5 kW), uso industrial, modelo PTM-150T"
VOCABULARIO = ortografia.vocabulario_de([FACTURA_DOS])   # palabras de la factura, con sus tildes, como en el motor

# (nombre, descripción en la declaración, descripción en la factura, equivalencias aceptadas, errores esperados)
CASOS = [
    ("original: resumen con 3 errores", "Motores electricos trifasicos de baja poternica, uso industrial", FACTURA,
     {"equivalente", "parcial"}, {"electricos", "trifasicos", "poternica"}),
    ("otra mercancía con 4 errores", "Motores electricos monofasicos de alta potencai, uso domestico", FACTURA,
     {"distinta"}, {"electricos", "monofasicos", "potencai", "domestico"}),
    ("descripción completa y correcta", FACTURA, FACTURA, {"equivalente"}, set()),
    # La declaración llama "baja potencia" a los dos modelos, pero en la factura el PTM-150T es de media potencia (1.5 kW):
    # un revisor atento debe verla como parcial o distinta; el método básico la da por equivalente porque sólo cuenta palabras.
    ("dos modelos, una tilde", "Motores electricos trifásicos de baja potencia, uso industrial, modelos PTM-075T y PTM-150T", FACTURA_DOS,
     {"parcial", "distinta"}, {"electricos"}),
    ("misma mercancía en inglés", "Three-phase low-power electric motors, industrial use", FACTURA, {"equivalente", "parcial"}, set()),
    ("sinónimos y abreviaturas", "Motor eléctrico 3F 0,75 kW industrial ref. PTM-075T", FACTURA, {"equivalente", "parcial"}, set()),
    ("mercancía totalmente distinta", "Bombas hidráulicas centrífugas para riego agrícola", FACTURA, {"distinta"}, set()),
    ("descripción demasiado vaga", "Mercancía varia", FACTURA, {"parcial", "distinta"}, set()),
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evalúa el revisor de texto sobre casos conocidos.")
    parser.add_argument("--sin-llm", action="store_true")
    args = parser.parse_args(argv)
    revisor, info = crear_revisor(forzar_heuristico=args.sin_llm)
    print(f"Revisor: {revisor.nombre}" + (f"  ({info['aviso']})" if info["aviso"] else ""))
    print("=" * 100)

    aciertos_equivalencia = aciertos_ortografia = 0
    for nombre, texto, referencia, esperadas, errores_esperados in CASOS:
        analisis = revisor.analizar(texto, referencia, VOCABULARIO)
        detectados = {quitar_acentos(h.palabra).lower() for h in analisis.hallazgos}
        ok_eq = analisis.equivalencia in esperadas
        ok_ort = detectados == {quitar_acentos(e).lower() for e in errores_esperados}
        aciertos_equivalencia += ok_eq
        aciertos_ortografia += ok_ort
        print(f"{nombre}")
        print(f"   declaración: {texto}")
        print(f"   equivalencia: esperada {sorted(esperadas)} → obtenida «{analisis.equivalencia}» (confianza {analisis.confianza:.0%}) {'✔' if ok_eq else '✘'}")
        print(f"   ortografía:   esperados {sorted(errores_esperados)} → detectados {sorted(detectados)} {'✔' if ok_ort else '✘'}")
        if analisis.terminos_omitidos:
            print(f"   omite: {', '.join(analisis.terminos_omitidos)}")
        if analisis.explicacion:
            print(f"   explicación: {analisis.explicacion}")
        if analisis.aviso:
            print(f"   AVISO: {analisis.aviso}")
        print("-" * 100)
    total = len(CASOS)
    print(f"Equivalencia correcta: {aciertos_equivalencia}/{total}   Ortografía exacta: {aciertos_ortografia}/{total}")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
