"""
Análisis de texto libre: interpretación de la descripción de la mercancía y
revisión de ortografía/redacción.

Hay dos implementaciones intercambiables del mismo contrato (`analizar`):

  - RevisorHeuristico: el método básico, sin dependencias ni red (comparación por
    términos y similitud de palabras, ver ortografia.py).
  - RevisorLLM: usa un modelo de lenguaje a través de cualquier servicio compatible
    con la API de OpenAI (Gemini, Ollama en local, LM Studio, OpenAI, etc.).
    El modelo SOLO interpreta lenguaje: decide si dos descripciones hablan de la
    misma mercancía, qué omite la declaración y qué errores de escritura tiene.
    Nunca decide sobre montos, fechas ni códigos; eso sigue siendo determinista.

Salvaguardas del RevisorLLM:
  - salida estructurada (JSON con un esquema fijo) y temperatura 0;
  - se valida cada campo y se descartan hallazgos sobre palabras que no están en el texto;
  - "reportar, nunca corregir": el texto de la declaración no se modifica;
  - si el servicio no responde o devuelve algo inválido, se usa el método básico y se avisa.

Hay dos proveedores:
  - "anthropic": Claude, con el SDK oficial y salida estructurada (lenguaje_claude.py). Recomendado.
  - "openai_compatible": cualquier servicio con API estilo OpenAI (Gemini, Ollama, LM Studio, OpenAI).

Configuración (variables de entorno o archivo .env en la raíz del proyecto):
  LLM_PROVEEDOR      anthropic | openai_compatible (si no se indica, se deduce de la clave: "sk-ant-" = anthropic)
  LLM_API_KEY        clave del servicio (sin ella se usa el método básico). Para Claude también vale ANTHROPIC_API_KEY.
  LLM_MODELO         por defecto claude-haiku-4-5 (anthropic) o gemini-3-flash-preview (openai_compatible)
  LLM_BASE_URL       sólo openai_compatible; por defecto el endpoint compatible con OpenAI de Gemini
  LLM_TIEMPO_MAXIMO  segundos de espera por llamada (por defecto 30)
  LLM_DESACTIVAR=1   fuerza el método básico aunque haya clave
"""
from __future__ import annotations

import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import urlparse

from . import ortografia
from .normalizacion import quitar_acentos

BASE_URL_POR_DEFECTO = "https://generativelanguage.googleapis.com/v1beta/openai/"   # Gemini (capa gratuita)
MODELO_POR_DEFECTO = "gemini-3-flash-preview"
TIEMPO_MAXIMO_POR_DEFECTO = 60   # los modelos que razonan pueden tardar más de 30 s bajo carga
RUTA_ENV = Path(__file__).resolve().parent.parent / ".env"

EQUIVALENCIAS = ("equivalente", "parcial", "distinta")
TIPOS_HALLAZGO = ("acentuacion", "error_tipografico", "gramatica", "redaccion")


# ---------------------------------------------------------------------------
# Contrato
# ---------------------------------------------------------------------------

@dataclass
class HallazgoTexto:
    palabra: str
    sugerencia: str | None
    tipo: str                      # acentuacion | error_tipografico | gramatica | redaccion
    explicacion: str = ""


@dataclass
class AnalisisTexto:
    equivalencia: str              # equivalente | parcial | distinta | sin_referencia
    confianza: float
    explicacion: str
    terminos_omitidos: list[str] = field(default_factory=list)
    hallazgos: list[HallazgoTexto] = field(default_factory=list)
    origen: str = "heuristico"     # "heuristico" o "llm:<modelo>"
    aviso: str | None = None       # p. ej. "el modelo no respondió; se usó el método básico"

    @property
    def compatible(self) -> bool:
        return self.equivalencia in ("equivalente", "parcial", "sin_referencia")


class RevisorTexto(Protocol):
    nombre: str

    def analizar(self, texto: str, referencia: str | None, vocabulario: set[str]) -> AnalisisTexto: ...


# ---------------------------------------------------------------------------
# Método básico (sin modelo)
# ---------------------------------------------------------------------------

class RevisorHeuristico:
    nombre = "heuristico"

    def analizar(self, texto: str, referencia: str | None, vocabulario: set[str]) -> AnalisisTexto:
        lexico = ortografia.construir_lexico(vocabulario)
        hallazgos = [HallazgoTexto(h.palabra, h.sugerencia, h.subtipo) for h in ortografia.revisar_texto(texto, lexico)]
        if not referencia:
            return AnalisisTexto("sin_referencia", 0.0, "No hay descripción de referencia con la cual comparar.", [], hallazgos)
        cobertura = ortografia.cobertura_terminos(texto, referencia)
        omitidos = ortografia.terminos_omitidos(texto, referencia)
        equivalencia = "equivalente" if cobertura >= 0.6 else "distinta"
        explicacion = f"{cobertura:.0%} de los términos de la declaración aparecen en la descripción de la factura."
        return AnalisisTexto(equivalencia, round(cobertura, 2), explicacion, omitidos, hallazgos)


# ---------------------------------------------------------------------------
# Modelo de lenguaje (API compatible con OpenAI)
# ---------------------------------------------------------------------------

INSTRUCCIONES = """Eres un revisor experto de declaraciones de importación en Colombia. Recibes dos textos:
(1) la descripción de la mercancía escrita en la declaración de importación y
(2) la descripción de la misma mercancía en la factura comercial, que es la fuente de verdad.

Tareas:
A. Decide si describen la misma mercancía:
   - "equivalente": misma mercancía, aunque la declaración tenga menos detalle o esté en otro idioma;
   - "parcial": compatible, pero la declaración es ambigua o le falta información necesaria para identificar la mercancía;
   - "distinta": hablan de mercancías diferentes o contradicen un dato esencial (tipo de producto, características técnicas).
B. Lista los términos relevantes que la factura menciona y la declaración omite (modelo, potencia, material, uso...). Cita el término tal como aparece en la factura.
C. Revisa la ortografía y la redacción de la descripción de la DECLARACIÓN, en el idioma en que esté escrita: tildes faltantes, errores de digitación, concordancia, frases mal construidas. Cada hallazgo debe citar la palabra exactamente como aparece en la declaración.

Reglas:
- No corrijas ni reescribas los textos; sólo reporta.
- No inventes hallazgos: si la descripción está bien escrita, devuelve una lista vacía.
- Si los textos están en idiomas distintos, compara el significado.
- Responde ÚNICAMENTE con un objeto JSON, sin texto adicional, con exactamente esta estructura:
{"equivalencia": "equivalente" | "parcial" | "distinta",
 "confianza": número entre 0 y 1,
 "explicacion": "una o dos frases en español",
 "terminos_omitidos": ["término", ...],
 "hallazgos_ortografia": [{"palabra": "tal como aparece", "sugerencia": "forma correcta", "tipo": "acentuacion" | "error_tipografico" | "gramatica" | "redaccion", "explicacion": "breve"}]}"""

EJEMPLO_USUARIO = ("Descripción en la declaración: «Motores electricos trifasicos de baja poternica, uso industrial»\n"
                   "Descripción en la factura: «Motores eléctricos trifásicos de baja potencia (< 0.75 kW), uso industrial, modelo PTM-075T»")
EJEMPLO_RESPUESTA = json.dumps({
    "equivalencia": "equivalente", "confianza": 0.95,
    "explicacion": "Ambas describen motores eléctricos trifásicos de baja potencia para uso industrial; la declaración omite la potencia y el modelo.",
    "terminos_omitidos": ["< 0.75 kW", "modelo PTM-075T"],
    "hallazgos_ortografia": [
        {"palabra": "electricos", "sugerencia": "eléctricos", "tipo": "acentuacion", "explicacion": "Falta la tilde."},
        {"palabra": "trifasicos", "sugerencia": "trifásicos", "tipo": "acentuacion", "explicacion": "Falta la tilde."},
        {"palabra": "poternica", "sugerencia": "potencia", "tipo": "error_tipografico", "explicacion": "Letras intercambiadas."},
    ],
}, ensure_ascii=False)


PROVEEDORES = ("anthropic", "openai_compatible")
BASE_URL_ANTHROPIC = "https://api.anthropic.com"
MODELO_ANTHROPIC_POR_DEFECTO = "claude-haiku-4-5"


@dataclass
class ConfiguracionLLM:
    api_key: str
    base_url: str = BASE_URL_POR_DEFECTO
    modelo: str = MODELO_POR_DEFECTO
    tiempo_maximo: int = TIEMPO_MAXIMO_POR_DEFECTO
    proveedor: str = "openai_compatible"

    @property
    def servicio(self) -> str:
        return urlparse(self.base_url).netloc or self.base_url


class ErrorLLM(Exception):
    """El servicio no respondió o respondió algo inutilizable."""


def leer_env(ruta: Path = RUTA_ENV) -> dict[str, str]:
    """Lee un archivo .env sencillo (CLAVE=valor por línea). No sobreescribe variables ya definidas."""
    valores: dict[str, str] = {}
    if not ruta.is_file():
        return valores
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, valor = linea.split("=", 1)
        valores[clave.strip()] = valor.strip().strip("'\"")
    return valores


def cargar_configuracion(entorno: dict[str, str] | None = None, ruta_env: Path = RUTA_ENV) -> ConfiguracionLLM | None:
    """Combina variables de entorno y .env. Devuelve None si no hay clave o si el LLM está desactivado."""
    fuente = dict(leer_env(ruta_env))
    fuente.update(os.environ if entorno is None else entorno)
    if fuente.get("LLM_DESACTIVAR", "").strip() in ("1", "true", "si", "sí"):
        return None
    clave = fuente.get("LLM_API_KEY", "").strip() or fuente.get("ANTHROPIC_API_KEY", "").strip()
    if not clave:
        return None
    try:
        tiempo = int(fuente.get("LLM_TIEMPO_MAXIMO") or TIEMPO_MAXIMO_POR_DEFECTO)
    except ValueError:
        tiempo = TIEMPO_MAXIMO_POR_DEFECTO
    proveedor = fuente.get("LLM_PROVEEDOR", "").strip().lower()
    if proveedor not in PROVEEDORES:
        proveedor = "anthropic" if clave.startswith("sk-ant-") else "openai_compatible"
    modelo = fuente.get("LLM_MODELO", "").strip()
    if proveedor == "anthropic":
        if not modelo.startswith("claude"):   # un modelo de otro proveedor heredado de otra configuración
            modelo = MODELO_ANTHROPIC_POR_DEFECTO
        return ConfiguracionLLM(clave, BASE_URL_ANTHROPIC, modelo, tiempo, "anthropic")
    return ConfiguracionLLM(clave, fuente.get("LLM_BASE_URL", "").strip() or BASE_URL_POR_DEFECTO,
                            modelo or MODELO_POR_DEFECTO, tiempo, "openai_compatible")


def servicio_alcanzable(base_url: str, segundos: float = 5.0) -> bool:
    """Comprueba rápido si el servidor acepta conexiones (evita esperas largas en cada revisión)."""
    url = urlparse(base_url)
    puerto = url.port or (443 if url.scheme == "https" else 80)
    try:
        with socket.create_connection((url.hostname, puerto), timeout=segundos):
            return True
    except OSError:
        return False


def _extraer_json(texto: str) -> dict:
    """El modelo debe responder sólo JSON, pero por si añade texto o ```json```, se rescata el primer objeto."""
    texto = texto.strip()
    texto = re.sub(r"^```(?:json)?\s*|\s*```$", "", texto)
    inicio, fin = texto.find("{"), texto.rfind("}")
    if inicio == -1 or fin == -1:
        raise ErrorLLM("la respuesta del modelo no contiene JSON")
    try:
        datos = json.loads(texto[inicio:fin + 1])
    except json.JSONDecodeError as error:
        raise ErrorLLM(f"la respuesta del modelo no es JSON válido ({error})") from error
    if not isinstance(datos, dict):
        raise ErrorLLM("la respuesta del modelo no es un objeto JSON")
    return datos


def _sanear(datos: dict, texto_declaracion: str, origen: str) -> AnalisisTexto:
    """Valida y limpia lo que devolvió el modelo; descarta lo que no cumpla el esquema."""
    equivalencia = str(datos.get("equivalencia", "")).strip().lower()
    if equivalencia not in EQUIVALENCIAS:
        raise ErrorLLM(f"equivalencia inválida: {equivalencia!r}")
    try:
        confianza = min(1.0, max(0.0, float(datos.get("confianza", 0.5))))
    except (TypeError, ValueError):
        confianza = 0.5
    explicacion = str(datos.get("explicacion") or "").strip()
    omitidos = [str(t).strip() for t in (datos.get("terminos_omitidos") or []) if str(t).strip()][:20]

    texto_plano = quitar_acentos(texto_declaracion).lower()
    hallazgos: list[HallazgoTexto] = []
    for h in (datos.get("hallazgos_ortografia") or [])[:30]:
        if not isinstance(h, dict):
            continue
        palabra = str(h.get("palabra") or "").strip()
        if not palabra or quitar_acentos(palabra).lower() not in texto_plano:
            continue  # el modelo citó algo que no está en el texto: se descarta
        tipo = str(h.get("tipo") or "redaccion").strip().lower()
        if tipo not in TIPOS_HALLAZGO:
            tipo = "redaccion"
        sugerencia = str(h.get("sugerencia") or "").strip() or None
        if sugerencia and sugerencia == palabra:
            continue  # no hay cambio propuesto: no es un hallazgo
        hallazgos.append(HallazgoTexto(palabra, sugerencia, tipo, str(h.get("explicacion") or "").strip()))
    return AnalisisTexto(equivalencia, round(confianza, 2), explicacion, omitidos, hallazgos, origen)


REINTENTOS_TRANSITORIOS = 3          # intentos totales ante HTTP 429/503 (límite de uso o servicio saturado)
ESPERA_ENTRE_REINTENTOS = (2, 5)     # segundos de espera antes del 2.º y del 3.º intento


def _mensaje_de_error_http(error: urllib.error.HTTPError) -> str:
    """Extrae el mensaje legible que suelen traer los errores JSON de estos servicios."""
    try:
        cuerpo = json.loads(error.read().decode("utf-8", "replace"))
        if isinstance(cuerpo, list) and cuerpo:
            cuerpo = cuerpo[0]
        mensaje = (cuerpo.get("error") or {}).get("message") if isinstance(cuerpo, dict) else None
        return f" ({str(mensaje)[:160]})" if mensaje else ""
    except Exception:  # noqa: BLE001 - el cuerpo puede no ser JSON
        return ""


def transporte_http(config: ConfiguracionLLM, dormir: Callable[[float], None] = time.sleep) -> Callable[[dict], str]:
    """Devuelve una función que envía la petición al servicio y retorna el texto de la respuesta."""
    def llamar(cuerpo: dict) -> str:
        datos = None
        for intento in range(REINTENTOS_TRANSITORIOS):
            peticion = urllib.request.Request(
                config.base_url.rstrip("/") + "/chat/completions",
                data=json.dumps(cuerpo).encode("utf-8"), method="POST",
                headers={"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(peticion, timeout=config.tiempo_maximo) as respuesta:
                    datos = json.loads(respuesta.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as error:
                detalle = _mensaje_de_error_http(error)
                if error.code in (429, 503) and intento < REINTENTOS_TRANSITORIOS - 1:
                    dormir(ESPERA_ENTRE_REINTENTOS[min(intento, len(ESPERA_ENTRE_REINTENTOS) - 1)])
                    continue
                if error.code in (429, 503):
                    raise ErrorLLM(f"el servicio está saturado o se alcanzó el límite de uso (HTTP {error.code}){detalle}, "
                                   f"incluso tras {REINTENTOS_TRANSITORIOS} intentos") from error
                raise ErrorLLM(f"el servicio respondió HTTP {error.code}{detalle}") from error
            except (urllib.error.URLError, OSError, ValueError) as error:
                raise ErrorLLM(f"no se pudo contactar el servicio ({error})") from error
        try:
            contenido = datos["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise ErrorLLM("la respuesta del servicio no tiene el formato esperado") from error
        if isinstance(contenido, list):  # algunos servidores devuelven el contenido en partes [{"type": "text", "text": ...}]
            contenido = "".join(str(p.get("text", "")) if isinstance(p, dict) else str(p) for p in contenido)
        if not isinstance(contenido, str) or not contenido.strip():
            raise ErrorLLM("el servicio devolvió una respuesta vacía")
        return contenido
    return llamar


class RevisorLLM:
    def __init__(self, config: ConfiguracionLLM, transporte: Callable[[dict], str] | None = None,
                 respaldo: RevisorTexto | None = None):
        self.config = config
        self.transporte = transporte or transporte_http(config)
        self.respaldo = respaldo or RevisorHeuristico()
        self.nombre = f"llm:{config.modelo}"

    def _consultar(self, texto: str, referencia: str | None) -> AnalisisTexto:
        pregunta = f"Descripción en la declaración: «{texto}»\n"
        pregunta += f"Descripción en la factura: «{referencia}»" if referencia else "Descripción en la factura: (no disponible; sólo revisa la ortografía y la redacción)"
        cuerpo = {
            # max_tokens amplio: los modelos que "razonan" antes de responder (p. ej. Gemini 3) gastan parte
            # de este límite en su razonamiento; si se agota, devuelven el contenido vacío.
            "model": self.config.modelo, "temperature": 0, "max_tokens": 4000,
            "messages": [
                {"role": "system", "content": INSTRUCCIONES},
                {"role": "user", "content": EJEMPLO_USUARIO},
                {"role": "assistant", "content": EJEMPLO_RESPUESTA},
                {"role": "user", "content": pregunta},
            ],
        }
        analisis = _sanear(_extraer_json(self.transporte(cuerpo)), texto, self.nombre)
        if not referencia:
            analisis.equivalencia, analisis.terminos_omitidos = "sin_referencia", []
        return analisis

    def analizar(self, texto: str, referencia: str | None, vocabulario: set[str]) -> AnalisisTexto:
        try:
            return self._consultar(texto, referencia)
        except ErrorLLM as error:
            respaldo = self.respaldo.analizar(texto, referencia, vocabulario)
            respaldo.aviso = (f"El modelo de lenguaje ({self.config.modelo}) no pudo usarse: {error}. "
                              f"La revisión de la descripción y la ortografía se hizo con el método básico.")
            return respaldo


# ---------------------------------------------------------------------------
# Fábrica
# ---------------------------------------------------------------------------

def crear_revisor(config: ConfiguracionLLM | None = None, forzar_heuristico: bool = False,
                  comprobar_conexion: bool = True) -> tuple[RevisorTexto, dict]:
    """
    Decide qué revisor usar y devuelve (revisor, información para mostrar al usuario):
    {"modo": "llm"|"heuristico", "modelo", "servicio", "aviso"}.
    """
    if forzar_heuristico:
        return RevisorHeuristico(), {"modo": "heuristico", "modelo": None, "servicio": None,
                                     "aviso": "Revisión de texto con el método básico (modelo de lenguaje desactivado)."}
    config = config or cargar_configuracion()
    if config is None:
        return RevisorHeuristico(), {"modo": "heuristico", "modelo": None, "servicio": None,
                                     "aviso": "Revisión de texto con el método básico: no hay clave de modelo de lenguaje configurada (LLM_API_KEY)."}
    if comprobar_conexion and not servicio_alcanzable(config.base_url):
        pista = ("Revisa la conexión a internet." if config.proveedor == "anthropic"
                 else "Revisa la conexión a internet y la dirección del servicio (LLM_BASE_URL).")
        return RevisorHeuristico(), {"modo": "heuristico", "modelo": config.modelo, "servicio": config.servicio,
                                     "aviso": f"El servicio del modelo de lenguaje ({config.servicio}) no responde; "
                                              f"la revisión de texto usará el método básico. {pista}"}
    if config.proveedor == "anthropic":
        try:
            from .lenguaje_claude import RevisorClaude  # import tardío: el SDK es opcional
        except ImportError:
            return RevisorHeuristico(), {"modo": "heuristico", "modelo": config.modelo, "servicio": config.servicio,
                                         "aviso": "Para usar Claude falta instalar el SDK: pip install anthropic. "
                                                  "Mientras tanto la revisión de texto usa el método básico."}
        return RevisorClaude(config), {"modo": "llm", "modelo": config.modelo, "servicio": config.servicio, "aviso": None}
    return RevisorLLM(config), {"modo": "llm", "modelo": config.modelo, "servicio": config.servicio, "aviso": None}
