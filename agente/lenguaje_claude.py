"""
Revisor de texto con Claude (API de Anthropic, SDK oficial `anthropic`).

Hace lo mismo que RevisorLLM de lenguaje.py (interpretar la descripción de la
mercancía y revisar su ortografía) pero contra la API de Anthropic, con dos
ventajas propias de esa API:

  - salida estructurada: la API garantiza que la respuesta cumple el esquema
    (RespuestaEsquema); no hay que "rescatar" JSON de un texto libre;
  - manejo de errores tipado (clave inválida, límite de uso, sin conexión...),
    cada uno convertido en un mensaje claro y en respaldo automático al método básico.

Configuración (en .env o variables de entorno):
  LLM_PROVEEDOR=anthropic
  LLM_API_KEY=sk-ant-...        (también se acepta ANTHROPIC_API_KEY)
  LLM_MODELO=claude-haiku-4-5   (por defecto; claude-sonnet-5 o claude-opus-5 para mayor calidad)
"""
from __future__ import annotations

from typing import Literal

import anthropic
from pydantic import BaseModel

from .lenguaje import (
    EJEMPLO_RESPUESTA, EJEMPLO_USUARIO, INSTRUCCIONES, MODELO_ANTHROPIC_POR_DEFECTO, AnalisisTexto, ConfiguracionLLM,
    ErrorLLM, RevisorHeuristico, RevisorTexto, _sanear,
)

MODELO_CLAUDE_POR_DEFECTO = MODELO_ANTHROPIC_POR_DEFECTO   # un solo lugar para cambiarlo: lenguaje.py


class HallazgoEsquema(BaseModel):
    palabra: str
    sugerencia: str
    tipo: Literal["acentuacion", "error_tipografico", "gramatica", "redaccion"]
    explicacion: str


class RespuestaEsquema(BaseModel):
    """Esquema que la API obliga a cumplir; es el mismo JSON que pide INSTRUCCIONES."""
    equivalencia: Literal["equivalente", "parcial", "distinta"]
    confianza: float
    explicacion: str
    terminos_omitidos: list[str]
    hallazgos_ortografia: list[HallazgoEsquema]


class RevisorClaude:
    def __init__(self, config: ConfiguracionLLM, cliente: anthropic.Anthropic | None = None,
                 respaldo: RevisorTexto | None = None):
        self.config = config
        self.modelo = config.modelo or MODELO_CLAUDE_POR_DEFECTO
        self.cliente = cliente or anthropic.Anthropic(api_key=config.api_key, timeout=float(config.tiempo_maximo), max_retries=2)
        self.respaldo = respaldo or RevisorHeuristico()
        self.nombre = f"llm:{self.modelo}"

    def _consultar(self, texto: str, referencia: str | None) -> AnalisisTexto:
        pregunta = f"Descripción en la declaración: «{texto}»\n"
        pregunta += (f"Descripción en la factura: «{referencia}»" if referencia
                     else "Descripción en la factura: (no disponible; sólo revisa la ortografía y la redacción)")
        try:
            respuesta = self.cliente.messages.parse(
                model=self.modelo,
                max_tokens=2048,
                system=INSTRUCCIONES,
                messages=[
                    {"role": "user", "content": EJEMPLO_USUARIO},        # ejemplo de referencia (few-shot)
                    {"role": "assistant", "content": EJEMPLO_RESPUESTA},
                    {"role": "user", "content": pregunta},
                ],
                output_format=RespuestaEsquema,
            )
        except anthropic.AuthenticationError as error:
            raise ErrorLLM("la clave de Anthropic no es válida (revisa LLM_API_KEY en el archivo .env)") from error
        except anthropic.PermissionDeniedError as error:
            raise ErrorLLM("la clave de Anthropic no tiene permiso para usar este modelo") from error
        except anthropic.NotFoundError as error:
            raise ErrorLLM(f"el modelo «{self.modelo}» no existe o no está disponible para esta clave") from error
        except anthropic.RateLimitError as error:
            raise ErrorLLM("se alcanzó el límite de uso de la API de Anthropic; inténtalo de nuevo en unos minutos") from error
        except anthropic.APIStatusError as error:
            detalle = str(getattr(error, "message", "") or "").strip()
            if "credit balance" in detalle.lower():
                raise ErrorLLM("la cuenta de Anthropic no tiene saldo: agrega créditos en https://console.anthropic.com/ "
                               "(Plans & Billing); la clave es válida") from error
            raise ErrorLLM(f"la API de Anthropic respondió HTTP {error.status_code}" + (f" ({detalle[:200]})" if detalle else "")) from error
        except anthropic.APIConnectionError as error:
            raise ErrorLLM("no se pudo conectar con la API de Anthropic (revisa la conexión a internet)") from error
        except Exception as error:  # noqa: BLE001 - cualquier otro fallo también debe caer al respaldo
            raise ErrorLLM(f"fallo inesperado al consultar a Claude ({type(error).__name__}: {error})") from error

        if respuesta.stop_reason == "refusal":
            raise ErrorLLM("el modelo declinó analizar este texto")
        if respuesta.stop_reason == "max_tokens" or respuesta.parsed_output is None:
            raise ErrorLLM("la respuesta del modelo llegó incompleta")

        analisis = _sanear(respuesta.parsed_output.model_dump(), texto, self.nombre)
        if not referencia:
            analisis.equivalencia, analisis.terminos_omitidos = "sin_referencia", []
        return analisis

    def analizar(self, texto: str, referencia: str | None, vocabulario: set[str]) -> AnalisisTexto:
        try:
            return self._consultar(texto, referencia)
        except ErrorLLM as error:
            respaldo = self.respaldo.analizar(texto, referencia, vocabulario)
            respaldo.aviso = (f"Claude ({self.modelo}) no pudo usarse: {error}. "
                              f"La revisión de la descripción y la ortografía se hizo con el método básico.")
            return respaldo
