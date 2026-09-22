#!/usr/bin/env python
"""
Interfaz gráfica del revisor: una página web local, sin instalar nada más.

    python interfaz.py            # abre el navegador en http://127.0.0.1:8765
    python interfaz.py --puerto 9000 --sin-navegador

Cómo funciona:
  - Este archivo levanta un pequeño servidor web SÓLO en tu computador (127.0.0.1):
    nada sale a internet.
  - La página (interfaz/index.html) deja arrastrar o elegir los dos PDF, explica cada paso
    y muestra el informe en lenguaje claro.
  - Al recibir los PDF, se guardan en una carpeta temporal, se validan (agente/carga.py),
    se revisan (agente/validacion.py) y la carpeta temporal se borra.

Rutas del servidor:
  GET  /                      -> la página
  GET  /ejemplos              -> lista de casos de ejemplo disponibles
  GET  /revisar-ejemplo?caso= -> revisa un caso de ejemplo (sin subir archivos)
  POST /revisar               -> revisa los dos PDF enviados por el formulario
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import socket
import sys
import tempfile
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from agente.carga import LIMITE_BYTES, ErrorDocumento, cargar_documentos
from agente.lenguaje import crear_revisor
from agente.validacion import revisar, serializar

# Revisor de texto (modelo de lenguaje si hay clave y el servicio responde; si no, método básico).
# Se decide una vez al arrancar; `configurar_revisor` permite cambiarlo (p. ej. en pruebas).
REVISOR, INFO_LLM = crear_revisor(comprobar_conexion=False)


def configurar_revisor(forzar_heuristico: bool = False, comprobar_conexion: bool = True) -> dict:
    global REVISOR, INFO_LLM
    REVISOR, INFO_LLM = crear_revisor(forzar_heuristico=forzar_heuristico, comprobar_conexion=comprobar_conexion)
    return INFO_LLM

BASE = Path(__file__).resolve().parent
PAGINA = BASE / "interfaz" / "index.html"
EJEMPLOS_DIR = BASE / "ejemplos"
LIMITE_CUERPO = 2 * LIMITE_BYTES + 1024 * 1024   # dos PDF más el envoltorio del formulario

# Casos de ejemplo que la página ofrece con un clic (sólo se muestran los que existan en disco)
EJEMPLOS = {
    "original": ("Documentos de la prueba (caso mixto)", "declaracion_importacion_ejemplo.pdf", "factura_comercial_ejemplo.pdf"),
    "todas_discrepancias": ("Todas las casillas con discrepancia", "declaracion_importacion_discrepancias.pdf", "factura_comercial_discrepancias.pdf"),
    "formatos_distintos": ("Declaración correcta con formatos distintos", "declaracion_importacion_formatos_distintos.pdf", "factura_comercial_formatos_distintos.pdf"),
    "faltantes": ("Casillas vacías y factura de dos ítems", "declaracion_importacion_faltantes.pdf", "factura_comercial_faltantes.pdf"),
}


# ---------------------------------------------------------------------------
# Lógica (independiente del servidor, para poder probarla)
# ---------------------------------------------------------------------------

def ejecutar_revision(ruta_declaracion: str | Path, ruta_factura: str | Path) -> dict:
    """Devuelve {'ok': True, 'reporte', 'avisos'} o {'ok': False, 'error': {...}}. Nunca lanza excepciones."""
    try:
        carga = cargar_documentos(ruta_declaracion, ruta_factura)
        reporte = revisar(carga.declaracion, carga.factura, revisor=REVISOR)
        reporte.pop("extraccion", None)  # la página no lo necesita y puede ser grande
        return {"ok": True, "reporte": reporte, "avisos": carga.avisos + reporte["analisis_texto"]["avisos"],
                "analisis_texto": INFO_LLM}
    except ErrorDocumento as error:
        return {"ok": False, "error": error.a_dict()}
    except Exception as error:  # noqa: BLE001 - cualquier fallo se muestra de forma amable
        traceback.print_exc()
        return {"ok": False, "error": {
            "titulo": "Ocurrió un error inesperado al revisar los documentos",
            "detalle": f"{type(error).__name__}: {error}",
            "sugerencia": "Verifica que los PDF sean la declaración y la factura originales. Si el problema continúa, "
                          "comparte los archivos con el equipo técnico junto con este mensaje.",
        }}


def parsear_multipart(content_type: str, cuerpo: bytes) -> dict[str, tuple[str, bytes]]:
    """Separa un formulario multipart/form-data en {campo: (nombre_de_archivo, bytes)}."""
    coincidencia = re.search(r'boundary="?([^";]+)"?', content_type or "")
    if not (content_type or "").startswith("multipart/form-data") or not coincidencia:
        raise ErrorDocumento("El formulario no se envió correctamente", "El navegador no envió los archivos en el formato esperado.",
                             "Recarga la página e inténtalo de nuevo.")
    limite = b"--" + coincidencia.group(1).encode("latin-1")
    archivos: dict[str, tuple[str, bytes]] = {}
    for seccion in cuerpo.split(limite)[1:]:
        if seccion.startswith(b"--"):          # límite de cierre
            break
        if seccion.startswith(b"\r\n"):
            seccion = seccion[2:]
        cabeceras, separador, datos = seccion.partition(b"\r\n\r\n")
        if not separador:
            continue
        if datos.endswith(b"\r\n"):
            datos = datos[:-2]
        campo = nombre_archivo = None
        for linea in cabeceras.decode("utf-8", "replace").split("\r\n"):
            if linea.lower().startswith("content-disposition:"):
                m_campo = re.search(r';\s*name="([^"]*)"', linea)
                m_archivo = re.search(r';\s*filename="([^"]*)"', linea)
                campo = m_campo.group(1) if m_campo else None
                nombre_archivo = Path(m_archivo.group(1)).name if m_archivo else ""
        if campo:
            archivos[campo] = (nombre_archivo or "", datos)
    return archivos


def revisar_archivos_subidos(archivos: dict[str, tuple[str, bytes]]) -> dict:
    """Guarda los PDF recibidos en una carpeta temporal, los revisa y borra la carpeta."""
    for campo, etiqueta in (("declaracion", "la declaración de importación"), ("factura", "la factura comercial")):
        nombre, datos = archivos.get(campo, ("", b""))
        if not datos:
            return {"ok": False, "error": {
                "titulo": f"Falta el archivo de {etiqueta}",
                "detalle": "No se recibió ningún archivo en ese campo (o llegó vacío).",
                "sugerencia": "Selecciona el PDF correspondiente y vuelve a pulsar «Revisar documentos».",
            }}
    carpeta = Path(tempfile.mkdtemp(prefix="revisor_"))
    try:
        rutas = {}
        for campo in ("declaracion", "factura"):
            nombre, datos = archivos[campo]
            extension = ".pdf" if nombre.lower().endswith(".pdf") else Path(nombre).suffix or ".bin"
            ruta = carpeta / f"{campo}{extension}"
            ruta.write_bytes(datos)
            rutas[campo] = ruta
        resultado = ejecutar_revision(rutas["declaracion"], rutas["factura"])
        # En los mensajes, mostrar el nombre original del archivo en vez del temporal
        if not resultado["ok"]:
            for campo in ("declaracion", "factura"):
                original = archivos[campo][0]
                if original:
                    for clave in ("detalle", "sugerencia"):
                        resultado["error"][clave] = resultado["error"][clave].replace(rutas[campo].name, original)
        return resultado
    finally:
        shutil.rmtree(carpeta, ignore_errors=True)


def ejemplos_disponibles() -> list[dict]:
    return [{"id": clave, "nombre": nombre}
            for clave, (nombre, decl, fact) in EJEMPLOS.items()
            if (EJEMPLOS_DIR / decl).exists() and (EJEMPLOS_DIR / fact).exists()]


# ---------------------------------------------------------------------------
# Servidor
# ---------------------------------------------------------------------------

class Manejador(BaseHTTPRequestHandler):
    server_version = "RevisorDeclaraciones/1.0"

    def _responder(self, codigo: int, cuerpo: bytes, tipo: str) -> None:
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                         "img-src 'self' data:; connect-src 'self'")
        self.end_headers()
        self.wfile.write(cuerpo)

    def _json(self, codigo: int, datos: dict) -> None:
        self._responder(codigo, json.dumps(datos, ensure_ascii=False, default=serializar).encode("utf-8"),
                        "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802 - nombre exigido por BaseHTTPRequestHandler
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            self._responder(200, PAGINA.read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/ejemplos":
            self._json(200, {"ejemplos": ejemplos_disponibles(), "analisis_texto": INFO_LLM})
        elif url.path == "/revisar-ejemplo":
            caso = parse_qs(url.query).get("caso", [""])[0]
            if caso not in EJEMPLOS:
                self._json(404, {"ok": False, "error": {"titulo": "Ejemplo no encontrado", "detalle": f"No existe el caso «{caso}».", "sugerencia": ""}})
                return
            _, decl, fact = EJEMPLOS[caso]
            self._json(200, ejecutar_revision(EJEMPLOS_DIR / decl, EJEMPLOS_DIR / fact))
        else:
            self._responder(404, b"No encontrado", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/revisar":
            self._responder(404, b"No encontrado", "text/plain; charset=utf-8")
            return
        longitud = int(self.headers.get("Content-Length") or 0)
        if longitud <= 0:
            self._json(400, {"ok": False, "error": {"titulo": "No se recibieron archivos", "detalle": "La solicitud llegó vacía.",
                                                    "sugerencia": "Selecciona los dos PDF y vuelve a intentarlo."}})
            return
        if longitud > LIMITE_CUERPO:
            self._json(413, {"ok": False, "error": {"titulo": "Archivos demasiado grandes",
                                                    "detalle": f"Los archivos enviados superan el límite de {LIMITE_BYTES // (1024 * 1024)} MB cada uno.",
                                                    "sugerencia": "Una declaración o factura normal pesa menos de 1 MB. Verifica que sean los documentos correctos."}})
            return
        cuerpo = self.rfile.read(longitud)
        try:
            archivos = parsear_multipart(self.headers.get("Content-Type", ""), cuerpo)
        except ErrorDocumento as error:
            self._json(400, {"ok": False, "error": error.a_dict()})
            return
        self._json(200, revisar_archivos_subidos(archivos))

    def log_message(self, formato: str, *args) -> None:  # silencia el registro por petición
        pass


def puerto_disponible(preferido: int) -> int:
    for puerto in [preferido] + list(range(preferido + 1, preferido + 20)):
        with socket.socket() as sonda:
            try:
                sonda.bind(("127.0.0.1", puerto))
                return puerto
            except OSError:
                continue
    return 0  # que el sistema elija uno libre


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Interfaz web local del revisor de declaraciones.")
    parser.add_argument("--puerto", type=int, default=8765)
    parser.add_argument("--sin-navegador", action="store_true", help="no abrir el navegador automáticamente")
    parser.add_argument("--sin-llm", action="store_true", help="revisar el texto con el método básico aunque haya modelo configurado")
    args = parser.parse_args(argv)

    if not PAGINA.exists():
        print(f"No se encuentra la página de la interfaz: {PAGINA}", file=sys.stderr)
        return 1
    info = configurar_revisor(forzar_heuristico=args.sin_llm)
    servidor = ThreadingHTTPServer(("127.0.0.1", puerto_disponible(args.puerto)), Manejador)
    direccion = f"http://127.0.0.1:{servidor.server_address[1]}/"
    print("=" * 64)
    print("  Revisor de declaraciones de importación")
    print(f"  Abre esta dirección en tu navegador:  {direccion}")
    if info["modo"] == "llm":
        print(f"  Revisión de texto: modelo «{info['modelo']}» en {info['servicio']}.")
        print("  La descripción de la mercancía se envía a ese servicio; el resto se procesa aquí.")
    else:
        print(f"  Revisión de texto: método básico. {info['aviso']}")
        print("  Los documentos se procesan sólo en este computador.")
    print("  Para cerrar la herramienta, cierra esta ventana o pulsa Ctrl+C.")
    print("=" * 64)
    if not args.sin_navegador:
        webbrowser.open(direccion)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nHerramienta cerrada.")
    finally:
        servidor.server_close()
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
