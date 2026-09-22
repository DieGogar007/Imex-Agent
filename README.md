# Agente revisor de declaraciones de importación

Agente que lee una declaración de importación (15 casillas) y su factura comercial en PDF, las
contrasta casilla por casilla y entrega un informe de alertas: discrepancias, campos sin
diligenciar, casillas que no se pudieron verificar por falta de documento soporte, errores de
ortografía o redacción e inconsistencias aritméticas internas de la declaración.

Se puede usar de tres formas: una **interfaz web local** pensada para cualquier persona, una
**línea de comandos** que produce JSON, y como **librería** (`agente/`). El núcleo es
determinista y corre sin internet; la interpretación de la descripción de la mercancía y la
revisión de su ortografía pueden delegarse a un **modelo de lenguaje** (Claude, Gemini u otro
servicio compatible con la API de OpenAI), con un método básico incorporado como respaldo.

Licencia: MIT (ver `LICENSE`).

---

## Contenido de la entrega

- **Extracción** de las 15 casillas de la declaración y de todos los datos de la factura: número, fecha, exportador, importador con NIT, país de origen, Incoterm, moneda, ítems con cantidad y precios, FOB, flete, seguro, CIF, bultos, peso bruto y la lista de documentos de la carpeta de soporte.
- **Normalización** para comparar significados y no cadenas: montos en formato colombiano y anglosajón, fechas en varios formatos, países por código ISO, Incoterm sin la versión, monedas por código, unidades sinónimas, razón social y NIT.
- **Motor de contraste** guiado por un catálogo declarativo de casillas, con política explícita de incertidumbre (validación provisional o "no validable" cuando falta el documento soporte) y reglas cruzadas entre casillas.
- **Cinco tipos de alerta**: los cuatro pedidos en la prueba más `inconsistencia_interna`, con severidad, fuente y explicación en lenguaje claro.
- **Cuatro pares de documentos de prueba** (el de la prueba y tres generados con el mismo formato) con sus resultados, y un **generador de casos** reproducible.
- **Interfaz web local** para todo público: arrastrar los PDF, ver el avance, leer el informe con iconos y explicaciones, descargar el JSON y probar con los ejemplos.
- **Validación de errores** con mensajes de "qué pasó y qué hacer" para archivos que no son PDF, escaneados sin texto, protegidos, intercambiados, del mismo tipo, de otro formato o con datos faltantes.
- **Revisión de texto con modelo de lenguaje**, opcional, con dos proveedores (Claude mediante el SDK oficial con salida estructurada; cualquier servicio compatible con la API de OpenAI, como Gemini), salvaguardas contra invenciones, reintentos y respaldo automático.
- **Script de evaluación** del revisor de texto con ocho casos de respuesta conocida y **47 pruebas automáticas** (`unittest`).

### Cumplimiento de lo pedido en la prueba

| Requisito de la prueba | Dónde se cumple |
|---|---|
| Extraer el valor de cada una de las 15 casillas de la declaración | `agente/extraccion.py` (`extraer_declaracion`); resultado en `reporte_completo.json` → `extraccion.declaracion` |
| Extraer los datos correspondientes de la factura comercial | `agente/extraccion.py` (`extraer_factura`); resultado en `reporte_completo.json` → `extraccion.factura` |
| Contrastar cada casilla con su dato equivalente y alertar cuando no coincidan | `agente/validacion.py` con los comparadores por tipo de dato; alertas `discrepancia_valor` |
| Detectar campos obligatorios sin diligenciar | Catálogo `agente/reglas.py` (obligatoriedad) + alertas `campo_faltante` |
| Señalar casillas no validables porque su documento soporte no está disponible | Catálogo (documentos soporte por casilla) + alertas `sin_documento_soporte` |
| Reportar hallazgos de ortografía o redacción en los campos de texto | `agente/ortografia.py` y `agente/lenguaje.py`; alertas `ortografia` |
| Salida JSON con una entrada por alerta, con los campos del ejemplo | `salida/alertas.json` (formato de la sección 2) |
| Tipos de alerta esperados, con ajustes justificados | Los cuatro tipos originales más `inconsistencia_interna` (sección 8) |
| Manejo de la incertidumbre sin inventar respuestas | Política de soportes por casilla (sección 7, paso 2 y 4) |
| Claridad y estructura del código y de la salida | Módulos por etapa (sección 7), reporte completo con estado por casilla |
| Cómo escalar a las ~50 casillas y a las reglas de diligenciamiento de la DIAN | Sección 9 |

---

## 1. Inicio rápido

Requisitos: Python 3.10 o superior. Windows, Linux o macOS.

```bash
pip install -r requirements.txt
```

### Interfaz gráfica

Doble clic en `Iniciar_revisor.bat` (Windows) o:

```bash
python interfaz.py
```

Se abre el navegador en una página local. Sin configuración adicional usa el método básico de
revisión de texto; con un archivo `.env` (sección 6) usa el modelo de lenguaje.

### Línea de comandos

```bash
python revisar_declaracion.py
```

Sin argumentos revisa los PDF de la prueba (`ejemplos/`). Para otros documentos:

```bash
python revisar_declaracion.py --declaracion ruta/declaracion.pdf --factura ruta/factura.pdf --salida salida/
```

Opciones: `--sin-llm` fuerza el método básico aunque haya modelo configurado; `--silencioso`
omite el resumen en consola. Códigos de salida: `0` revisión hecha (haya o no alertas), `2` los
archivos no se pudieron usar (el mensaje dice por qué).

Genera dos archivos en la carpeta de salida:

| Archivo | Contenido |
|---|---|
| `alertas.json` | Sólo la lista de alertas, en el formato pedido por la prueba. |
| `reporte_completo.json` | Extracción de ambos documentos, estado de cada casilla, verificaciones de la factura, análisis de texto y las alertas. |

### Pruebas

```bash
python -m unittest discover -s tests -v
```

---

## 2. Qué revisa y cómo lo reporta

Cada alerta es un objeto JSON con la casilla, el campo, el tipo, la severidad, el valor de la
declaración, el valor del documento fuente, la fuente y una explicación:

```json
{
  "casilla": 12,
  "campo": "Valor FOB (USD)",
  "tipo": "discrepancia_valor",
  "severidad": "alta",
  "valor_declaracion": "18.540,00",
  "valor_fuente": "18,450.00",
  "fuente": "factura_comercial",
  "detalle": "El valor declarado (18.540,00) no coincide con el de la factura comercial (18,450.00). Diferencia: 90,00. Los dígitos son los mismos en distinto orden: posible transposición al digitar."
}
```

| Tipo | Significado | Severidad |
|---|---|---|
| `discrepancia_valor` | El valor de la casilla no coincide con el de su documento soporte. | alta |
| `campo_faltante` | Un campo obligatorio quedó sin diligenciar (o no se encontró en el PDF). | alta |
| `inconsistencia_interna` | Los valores declarados no cumplen una regla aritmética entre casillas (CIF = FOB + flete + seguro). Tipo agregado; ver sección 8. | alta |
| `sin_documento_soporte` | No se pudo validar, o sólo provisionalmente, porque el documento que respalda la casilla no está disponible. | media |
| `ortografia` | Hallazgo de ortografía (tilde, digitación, gramática) o de redacción en un campo de texto. | baja |

Respecto al formato pedido en la prueba se agregaron `severidad`, `fuente` y, cuando aplica,
`sugerencia` (corrección propuesta), `casillas_relacionadas` (reglas entre casillas) y `analisis`
(quién analizó el texto: `heuristico` o `llm:<modelo>`).

Además de las alertas, el reporte completo trae el estado de cada casilla (`ok`,
`ok_provisional`, `discrepancia`, `faltante`, `no_encontrada`, `no_validable`), la lista de
documentos de la carpeta de soporte y la verificación de la propia factura (cantidad × precio =
subtotal, suma de subtotales = FOB, FOB + flete + seguro = CIF).

---

## 3. Resultado sobre los documentos de la prueba

### Contraste casilla por casilla

| # | Casilla | Declaración | Factura comercial | Resultado |
|---|---|---|---|---|
| 1 | Número de factura | FC-2026-00**147** | FC-2026-00**417** | **Discrepancia** (dígitos transpuestos; el archivo de la carpeta se llama FC-2026-00417.pdf) |
| 2 | Fecha de factura | 15 de junio de 2026 | 15 de junio de 2026 | OK |
| 3 | Exportador | Shenzhen PowerTech Motors Co., Ltd. | Shenzhen PowerTech Motors Co., Ltd. | OK |
| 4 | Importador | Distribuidora Andina S.A.S. — NIT 900.123.456-7 | Distribuidora Andina S.A.S. / NIT 900.123.456-7 | OK (razón social y NIT) |
| 5 | País de origen | Corea del Sur | República Popular China | **Discrepancia**, y validación *provisional*: falta el certificado de origen |
| 6 | Incoterm | CIF Cartagena, Colombia | CIF Cartagena, Colombia (Incoterms 2020) | OK (la versión de las reglas no cuenta) |
| 7 | Moneda | USD | Dólares de los Estados Unidos (USD) | OK |
| 8 | Descripción | Motores electricos trifasicos de baja poternica, uso industrial | Motores eléctricos trifásicos de baja potencia (< 0.75 kW), uso industrial, modelo PTM-075T | Compatible, pero **3 errores de ortografía** y una observación de redacción |
| 9 | Cantidad | 500 unidades | 500 unidades | OK |
| 10 | Número de bultos | *(sin diligenciar)* | 25 cajas (bultos) | **Campo faltante** (la factura da 25 como referencia; el soporte formal es la lista de empaque, ausente) |
| 11 | Peso bruto | 1.250,00 kg | *(no aparece; remite a la lista de empaque)* | **No validable**: faltan lista de empaque y BL |
| 12 | Valor FOB | 18.**540**,00 | 18,**450**.00 | **Discrepancia** (diferencia 90,00; dígitos transpuestos) |
| 13 | Flete | 620,00 | 620.00 | OK |
| 14 | Seguro | 95,00 | 95.00 | OK |
| 15 | Valor CIF total | 19.165,00 | 19,165.00 | OK frente a la factura, pero **inconsistencia interna**: 18.540 + 620 + 95 = 19.255 ≠ 19.165 |

### Las 11 alertas generadas

| Casilla | Tipo | Severidad | Resumen |
|---|---|---|---|
| 1 | `discrepancia_valor` | alta | Número de factura FC-2026-00147 vs FC-2026-00417. |
| 5 | `discrepancia_valor` | alta | País de origen Corea del Sur (KR) vs República Popular China (CN). |
| 5 | `sin_documento_soporte` | media | Se validó contra la factura porque no hay certificado de origen; resultado provisional. |
| 8 | `ortografia` | baja | «electricos» → «eléctricos» (tilde). |
| 8 | `ortografia` | baja | «trifasicos» → «trifásicos» (tilde). |
| 8 | `ortografia` | baja | «poternica» → «potencia» (error de digitación). |
| 8 | `ortografia` | baja | Redacción: omite la potencia (0.75 kW) y el modelo (PTM-075T) que sí trae la factura. |
| 10 | `campo_faltante` | alta | Número de bultos sin diligenciar; referencia en factura: 25 (20 unidades por caja). |
| 11 | `sin_documento_soporte` | media | Peso bruto no validable: sin lista de empaque ni BL; la factura no lo trae. |
| 12 | `discrepancia_valor` | alta | FOB 18.540,00 vs 18,450.00. |
| 15 | `inconsistencia_interna` | alta | FOB + flete + seguro declarados = 19.255,00 ≠ CIF declarado 19.165,00. Apunta a la casilla 12. |

Casillas sin ninguna alerta: 2, 3, 4, 6, 7, 9, 13 y 14. La factura se verificó a sí misma y es
coherente. Los archivos generados están en `salida/`.

---

## 4. Casos de prueba adicionales

`ejemplos/` incluye, además del par de la prueba, tres pares más con el mismo formato. Cada uno
ejercita un escenario distinto y tiene su salida en `salida/<caso>/`:

| Caso | Archivos | Escenario | Resultado |
|---|---|---|---|
| Todas las casillas con discrepancia | `*_discrepancias.pdf` | Las 15 casillas difieren de la factura (transposiciones, NIT distinto, país, Incoterm, moneda, mercancía distinta con 4 errores de ortografía, cantidades y montos). | 22 alertas: 15 discrepancias, 3 provisionales, 4 de ortografía. Ninguna casilla limpia y ningún falso positivo. |
| Formatos distintos | `*_formatos_distintos.pdf` | Declaración correcta escrita de otra manera: fecha ISO, exportador en mayúsculas, NIT sin puntos, país por nombre corto, Incoterm con versión, moneda por su nombre, `500 und.`, `1250 kg`, `USD 18,450.00`, descripción en dos renglones. | 3 alertas provisionales y **ninguna discrepancia**: el agente compara significados. |
| Faltantes y dos ítems | `*_faltantes.pdf` | Factura con dos ítems; casillas 2, 6 y 7 vacías; CIF con dígitos transpuestos; lista de empaque presente en la carpeta. | 10 alertas: 3 faltantes con valor de referencia, discrepancia e inconsistencia interna en el CIF, 3 provisionales (el mensaje distingue "documento ausente" de "presente pero no procesado"), 2 de ortografía/redacción. |

Para correr cualquiera:

```bash
python revisar_declaracion.py --declaracion ejemplos/declaracion_importacion_faltantes.pdf --factura ejemplos/factura_comercial_faltantes.pdf --salida salida/faltantes
```

### Generar más casos

`ejemplos/generar_ejemplos.py` arma el HTML de una declaración y una factura con los valores de
un caso y lo imprime a PDF con Chrome o Edge sin cabeza, con el mismo diseño de los documentos
de la prueba. Agregar un caso es agregar una entrada al diccionario `CASOS`. El caso `original`
reproduce los PDF de la prueba con extracción idéntica campo por campo (hay una prueba
automática para eso).

```bash
python ejemplos/generar_ejemplos.py --listar
```

```bash
python ejemplos/generar_ejemplos.py --todos
```

---

## 5. Interfaz para cualquier usuario

La página explica en tres pasos qué hace la herramienta, con un glosario (FOB, CIF, Incoterm,
bultos, documento soporte). Deja arrastrar o elegir los dos PDF, valida extensión y tamaño antes
de enviar, muestra el avance paso a paso y entrega el informe en lenguaje claro: un veredicto en
color, cuatro cifras (por corregir, por confirmar, recomendaciones, casillas limpias), la tabla
casilla por casilla con iconos y notas, las alertas ordenadas por importancia con "en la
declaración" frente a "en la factura", la lista de documentos de la carpeta, y botones para
descargar el informe en JSON y para probar con los cuatro casos de ejemplo. Indica qué método
está revisando el texto y, si hay modelo de lenguaje, qué información se le envía.

Es un servidor web de la librería estándar de Python que escucha sólo en `127.0.0.1`. Los PDF se
guardan en una carpeta temporal mientras se revisan y se borran al terminar; el servidor limita
el tamaño de la solicitud, acepta únicamente los dos campos esperados y nunca sirve archivos del
disco.

### Validación de errores

`agente/carga.py` comprueba los archivos antes de revisar y convierte cada problema en un mensaje
con tres partes: qué pasó, por qué y qué hacer.

| Situación | Qué hace la herramienta |
|---|---|
| El archivo no existe, está vacío o pesa más de 20 MB | Lo dice y pide volver a seleccionarlo. |
| El archivo no es un PDF (aunque tenga la extensión) | "No tiene formato PDF": pide exportarlo como PDF. |
| El PDF está dañado o protegido con contraseña | Explica cuál de los dos casos es y cómo resolverlo. |
| El PDF es una imagen escaneada sin texto | Explica que esta versión no hace OCR y pide el PDF original. |
| Los dos archivos vienen intercambiados | Lo detecta por el contenido, los corrige solo y lo avisa. |
| Los dos archivos son del mismo tipo | Dice cuál falta y en qué campo va. |
| El PDF no es ni declaración ni factura | "No se reconoce": pide el formulario o la factura originales. |
| El formulario tiene otro formato (menos de 8 casillas legibles) | Lo dice; si sólo faltan algunas, las reporta como "no encontradas" y avisa. |
| A la factura le faltan datos (número, fecha, totales, ítems) | Avisa cuáles faltan; las casillas que dependen de ellos quedan sin verificar. |

---

## 6. Revisión de texto con modelo de lenguaje

El modelo se usa **sólo** para la casilla de texto libre, la descripción de la mercancía: decide
si describe la misma mercancía que la factura aunque esté resumida, con sinónimos o en otro
idioma; lista lo que omite; y detecta errores de ortografía, digitación, gramática y redacción.
Las otras 14 casillas nunca pasan por el modelo. Sin configuración, o si el modelo no responde,
el método básico hace el trabajo y el informe lo dice.

### Configuración

Copia `.env.ejemplo` como `.env` en la raíz del proyecto y completa una de las opciones. El
archivo `.env` está en `.gitignore`: nunca se sube al repositorio.

**Claude (Anthropic).** Clave en https://console.anthropic.com/. Usa el SDK oficial
(`anthropic`, incluido en `requirements.txt`) y la salida estructurada de la API, que garantiza
que la respuesta cumple el esquema. Requiere créditos prepagados en la consola.

```
LLM_PROVEEDOR=anthropic
LLM_API_KEY=sk-ant-...
LLM_MODELO=claude-haiku-4-5      # el más económico; claude-sonnet-5 o claude-opus-5 para mayor calidad
```

**Gemini (Google), capa gratuita sin tarjeta.** Clave en https://aistudio.google.com/apikey.
Usa el endpoint compatible con OpenAI de Google.

```
LLM_PROVEEDOR=openai_compatible
LLM_API_KEY=AIza...
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
LLM_MODELO=gemini-3-flash-preview
```

**Otro servicio compatible con OpenAI** (Ollama en local, LM Studio, OpenAI): la misma opción
cambiando `LLM_BASE_URL`, `LLM_MODELO` y la clave. Variables comunes: `LLM_TIEMPO_MAXIMO`
(segundos por llamada, 60 por defecto) y `LLM_DESACTIVAR=1` para forzar el método básico.

### Salvaguardas

- El modelo responde en JSON con esquema fijo, a temperatura cero, con un ejemplo de referencia en la conversación. Con Claude el esquema lo garantiza la API; con los demás, el cliente rescata el JSON del texto.
- Cada campo se valida; se descarta cualquier hallazgo sobre una palabra que no esté en el texto; el texto de la declaración nunca se modifica ("reportar, no corregir").
- Los errores transitorios (servicio saturado, límite de uso) se reintentan hasta tres veces con espera. Si el servicio no responde, devuelve algo inválido o no hay clave o saldo, se usa el método básico y el informe lo avisa con la causa real.
- Cada alerta de texto indica quién la produjo (`analisis`), y la interfaz muestra qué se envía al servicio.

### Evaluación

`ejemplos/evaluar_texto.py` corre ocho casos de respuesta conocida (resúmenes con errores, otra
mercancía, descripción completa, dos modelos con una contradicción de potencia, inglés,
sinónimos y abreviaturas, mercancía distinta, descripción vaga) e imprime los aciertos de
equivalencia y de ortografía del revisor configurado.

```bash
python ejemplos/evaluar_texto.py
```

```bash
python ejemplos/evaluar_texto.py --sin-llm
```

El método básico, reproducible sin conexión, acierta 6 de 8 en equivalencia y 7 de 8 en
ortografía: falla en la descripción en inglés, en las abreviaturas y en la contradicción de
potencia, que son justamente los casos que un modelo de lenguaje resuelve. Con un modelo
configurado, esos casos quedan cubiertos; el resto del informe es idéntico.

---

## 7. Cómo funciona

```
 PDF declaración ─┐                                   ┌─ reglas.py      (catálogo de casillas + reglas cruzadas)
                  ├─► carga.py ─► extraccion.py ─► validacion.py ──┼─ lenguaje.py    (revisor de texto: básico / LLM)
 PDF factura ─────┘   (valida)        │                │           └─ ortografia.py  (léxico + similitud)
                                      │                │
                               normalizacion.py ◄──────┘        ─►  alertas.json / reporte_completo.json
```

| Módulo | Responsabilidad | Idea clave |
|---|---|---|
| `agente/carga.py` | Valida los PDF (existencia, formato, contraseña, texto, tipo de documento, extracción utilizable). | Cada error trae título, explicación y qué hacer; los archivos intercambiados se corrigen solos. |
| `agente/extraccion.py` | Convierte cada PDF en un diccionario con los valores **tal como están escritos**. | La declaración se lee en modo *layout* (conserva columnas) y cada casilla se captura por regex, incluidos valores de dos renglones. La factura se lee por "etiquetas ancla", tabla de ítems y totales. |
| `agente/normalizacion.py` | Lleva ambos lados a una forma canónica antes de comparar. | `18.540,00` y `18,450.00` → `Decimal`; fechas → `date`; monedas y países → códigos; Incoterm → término + lugar; unidades sinónimas; razón social y NIT. |
| `agente/reglas.py` | **Catálogo declarativo** de las 15 casillas y las reglas entre casillas. Sin lógica. | Por casilla: tipo de dato, obligatoriedad, documentos que la respaldan en orden de prioridad y campo de la factura con el que se compara. |
| `agente/validacion.py` | Motor: recorre el catálogo, decide con qué documento validar, compara y arma las alertas. | Aquí vive la política de incertidumbre (sección 8). |
| `agente/ortografia.py` | Método básico de ortografía y redacción; comparación de descripciones por términos. | Léxico = vocabulario base + palabras de la factura; palabra desconocida con una muy parecida → hallazgo con sugerencia. |
| `agente/lenguaje.py` | Contrato "revisor de texto": método básico, cliente para servicios compatibles con OpenAI, configuración y fábrica que elige proveedor. | El motor llama a `analizar(texto, referencia, vocabulario)` sin saber quién responde. Saneamiento, reintentos y respaldo automático. |
| `agente/lenguaje_claude.py` | Revisor de texto con Claude (SDK oficial, salida estructurada, errores tipados). | Mismo contrato; el esquema lo garantiza la API. |
| `revisar_declaracion.py` | Línea de comandos. | |
| `interfaz.py` + `interfaz/index.html` | Interfaz web local. | Servidor de la librería estándar en `127.0.0.1`; archivos temporales borrados al terminar. |
| `ejemplos/generar_ejemplos.py` | Generador de pares de prueba en PDF con el formato de la prueba. | HTML impreso a PDF con Chrome/Edge sin cabeza. |
| `ejemplos/evaluar_texto.py` | Evaluación del revisor de texto con casos conocidos. | Línea base (método básico) contra el modelo. |
| `tests/test_agente.py` | 47 pruebas (`unittest`). | Normalización, ortografía, motor con declaraciones sintéticas, cuatro casos extremo a extremo, carga, interfaz, revisor de texto con modelo simulado, reintentos, regresión del generador. |

Para cada casilla del catálogo el motor sigue estos pasos:

1. **¿Está vacía o no se encontró?** Si es obligatoria → `campo_faltante`. Si la factura trae un valor de referencia (por ejemplo, 25 bultos), se incluye como ayuda, indicando que debe confirmarse con el soporte formal.
2. **¿Con qué documento se puede validar?** Se toma el primer documento de la lista de soportes que esté disponible y que el agente sepa leer. Si ninguno → `sin_documento_soporte`, con el nombre de los documentos que faltan. Nunca se inventa un valor ni se marca "OK".
3. **Comparar** con el comparador del tipo de dato (identificador, fecha, empresa, empresa + NIT, país, Incoterm, moneda, cantidad, entero, peso, monto) o, para la descripción, con el revisor de texto. Si difieren → `discrepancia_valor` con ambos valores y un detalle (diferencia, posible transposición de dígitos, evidencia adicional como el nombre del archivo de la factura).
4. **¿Se validó contra un documento secundario?** Si el soporte formal falta pero otro documento trae el dato, se compara igualmente **y** se emite `sin_documento_soporte` para dejar constancia de que el resultado es provisional.

Después de las casillas se evalúan las reglas cruzadas, la ortografía y redacción de los campos
de texto libre, y la coherencia interna de la factura.

---

## 8. Decisiones de diseño

**Híbrido: código para los datos, modelo de lenguaje para el lenguaje.** Montos, fechas, códigos
y NIT se comparan con reglas explícitas: exactas, gratis, instantáneas y auditables; un modelo
puede equivocarse justo al transponer dígitos. La descripción de la mercancía y su ortografía son
comprensión de lenguaje (sinónimos, abreviaturas, otro idioma, redacción), y ahí un modelo es
claramente mejor que cualquier heurística. El modelo propone en un formato fijo; el motor decide,
marca el origen de cada alerta y vuelve al método básico si el modelo falla.

**Normalizar antes de comparar.** Es lo que evita falsos positivos: los documentos escriben los
montos con separadores distintos, los países con nombres distintos y el Incoterm con o sin
versión. Sólo se alerta cuando el *significado* difiere.

**Catálogo declarativo.** Las casillas no están "cableadas" en el código: son filas de una tabla
con tipo de dato, obligatoriedad, documentos soporte y campo fuente. Agregar una casilla es
agregar una fila; agregar un tipo de dato es agregar un comparador.

**La ausencia de un documento es incertidumbre, no error ni éxito.** Cada casilla declara qué
documentos la respaldan y en qué orden. Así el agente distingue: validada contra su soporte
formal (OK), validada contra un soporte alterno (OK provisional + alerta) o no validable (alerta,
sin veredicto). El peso bruto es el caso límite: la factura es su último recurso; si lo menciona,
se contrasta y queda provisional; si no, la casilla queda no validable.

**Un tipo de alerta adicional: `inconsistencia_interna`.** La casilla 15 coincide con la factura
y aun así la declaración está mal, porque FOB + flete + seguro declarados no suman el CIF
declarado. Ninguno de los cuatro tipos originales describe eso: no es discrepancia contra un
documento, es una regla de diligenciamiento incumplida, la clase de regla que la DIAN valida.

**La fuente de verdad también se verifica.** Antes de usar la factura como referencia, el agente
comprueba que sus propios totales cuadren; si no, alerta en lugar de propagar el error.

**Severidad.** `alta` para lo que impide presentar la declaración, `media` para lo que requiere
conseguir un documento, `baja` para ortografía y redacción.

**Los mensajes son plantillas, no texto generado.** Todo lo que lee el usuario (detalles de
alertas, errores de carga, textos de la interfaz) está escrito en el código y se rellena con los
datos extraídos. Es predecible, auditable y traducible. La única excepción, marcada como tal en
cada alerta, es la explicación que da el modelo de lenguaje sobre la descripción.

**Un método básico que siempre está.** Sin modelo, el vocabulario de la factura es la mejor
referencia posible para la descripción: si la declaración dice "poternica" y la factura
"potencia", la similitud lo delata. Es el respaldo y la línea base contra la que se mide el modelo.

---

## 9. Cómo escalar a las ~50 casillas reales y a las reglas de la DIAN

La arquitectura separa lo que cambia (el conocimiento: catálogo, reglas, extractores) de lo que
no (el motor). Escalar es crecer el conocimiento, no reescribir el motor.

1. **El catálogo pasa a ser configuración versionada** (YAML o tabla) con una fila por casilla: tipo de dato, obligatoriedad fija o condicional ("obligatoria si modalidad = C100"), documentos soporte en orden, campo fuente y comparador. Las ~50 casillas del Formulario 500 son filas; el motor no cambia.
2. **Nuevos tipos de dato = nuevos comparadores**, cada uno con su lista maestra: subpartida arancelaria, códigos DIAN de modalidad, aduana, país y tipo de documento, tasa de cambio contra la TRM del día, tributos (arancel × base gravable, IVA).
3. **Las reglas de diligenciamiento se modelan como reglas cruzadas**, igual que CIF = FOB + flete + seguro: aritméticas (base gravable = CIF × TRM), condicionales (si el Incoterm es FOB, el flete debe venir del documento de transporte; si hay acuerdo comercial, el certificado de origen es obligatorio), de dominio y de coherencia (subpartida compatible con la descripción).
4. **Un extractor por tipo de documento** con el mismo contrato de salida: factura, lista de empaque, documento de transporte, certificado de origen, póliza. Con formatos heterogéneos o escaneados, la extracción pasa a OCR + modelo de lenguaje con salida estructurada y cita del fragmento de origen; el motor sigue siendo determinista.
5. **El modelo de lenguaje** ya está en la comparación semántica de descripciones y la redacción; el siguiente paso es la extracción de documentos variables y las etiquetas en varios idiomas. Siempre bajo "reportar, no corregir ni completar".
6. **Confianza y trazabilidad por alerta**: a la fuente y la severidad se suma un puntaje de confianza y el fragmento exacto del documento que la respalda.
7. **Calidad medible**: una prueba por regla, un corpus de declaraciones reales anonimizadas como regresión, y precisión y cobertura de las alertas a partir de lo que los analistas confirman, como ya hace `evaluar_texto.py` para el texto.

---

## 10. Limitaciones conocidas

- La extracción por expresiones regulares está ajustada al formato de estos PDF y a etiquetas en español; un formulario o una factura en otro formato o idioma requieren adaptar los patrones (o el paso a OCR + modelo de la sección 9). Los datos en sí ya se entienden en varios idiomas y formatos.
- El método básico de ortografía usa un léxico pequeño y marca como errores palabras de otro idioma; el modelo de lenguaje no tiene ese problema.
- Sólo la factura tiene extractor. Si la lista de empaque, el certificado de origen o el BL están en la carpeta, el agente lo dice pero no los lee.
- La tabla de países cubre los más frecuentes; en producción se usa la lista oficial completa.
- Flete y seguro se aceptan de la factura porque el Incoterm es CIF; con FOB o EXW el catálogo debería exigir el documento de transporte y la póliza.
- Las capas gratuitas de los servicios de modelo tienen cuotas pequeñas y pueden saturarse; para uso intensivo conviene un plan de pago.

---

## 11. Estructura del repositorio

```
imex-agente-revisor/
├── Iniciar_revisor.bat         # doble clic: abre la interfaz gráfica (Windows)
├── interfaz.py                 # servidor web local de la interfaz
├── interfaz/index.html         # la página (explicación, carga de PDF, informe)
├── revisar_declaracion.py      # línea de comandos
├── requirements.txt            # pypdf y anthropic (este último sólo para Claude)
├── .env.ejemplo                # plantilla de configuración del modelo de lenguaje (copiar a .env)
├── .gitignore                  # excluye .env, venv y cachés
├── LICENSE                     # MIT
├── README.md
├── agente/
│   ├── carga.py                # validación de los PDF y mensajes de error claros
│   ├── extraccion.py           # PDF -> diccionarios
│   ├── normalizacion.py        # formas canónicas (montos, fechas, países, incoterm, unidades, empresas)
│   ├── reglas.py               # catálogo de casillas, reglas cruzadas, tipos de alerta
│   ├── ortografia.py           # método básico de ortografía y comparación por términos
│   ├── lenguaje.py             # revisor de texto: básico, cliente OpenAI-compatible, configuración, fábrica
│   ├── lenguaje_claude.py      # revisor de texto con Claude (SDK de Anthropic)
│   └── validacion.py           # motor de contraste y generación de alertas
├── ejemplos/
│   ├── declaracion_importacion_ejemplo.pdf        # el par de la prueba
│   ├── factura_comercial_ejemplo.pdf
│   ├── *_discrepancias.pdf     # caso: 15 casillas con discrepancia
│   ├── *_formatos_distintos.pdf # caso: declaración correcta con otros formatos
│   ├── *_faltantes.pdf         # caso: dos ítems, casillas vacías, CIF transpuesto
│   ├── html/                   # HTML fuente de los PDF generados
│   ├── generar_ejemplos.py     # generador de casos (HTML -> PDF con Chrome/Edge)
│   └── evaluar_texto.py        # evaluación del revisor de texto
├── salida/                     # resultados del caso de la prueba (alertas.json, reporte_completo.json)
│   ├── discrepancias/          # y de cada caso adicional
│   ├── formatos_distintos/
│   └── faltantes/
└── tests/test_agente.py        # 47 pruebas (unittest)
```
