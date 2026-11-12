# Implementación de api-pdf

## Propósito

Servicio independiente para que n8n u otros sistemas envíen un documento y reciban clasificación y datos estructurados. Casos iniciales: cotizaciones, facturas de proveedor/cliente y documentación de transporte. El consumidor controla carpetas, reglas de negocio, almacenamiento R2 y base de datos.

## Contrato

`POST /v1/extractions` requiere `Authorization: Bearer <token>` y `multipart/form-data`:

| Campo | Uso |
|---|---|
| `file` | PDF o imagen permitida; validar extensión, MIME, firma y tamaño máximo |
| `document_type` | `auto`, `invoice`, `quote`, `transport_document` |
| `locale` | Idioma/país, por defecto `es-CL` |
| `wait` | `true` (por defecto) espera hasta `JOB_WAIT_SECONDS` y devuelve el resultado o `202`; `false` devuelve `202` inmediatamente con `job_id` |

`GET /v1/extractions/{job_id}` requiere el mismo bearer y devuelve el estado (`queued`, `processing`, `completed`, `failed`) y el resultado cuando está disponible. El consumidor debe consultar hasta `completed` o `failed`; al completar, el objeto de extracción está en `result`. Los jobs se guardan en SQLite en el volumen persistente; QStash hace entrega durable, reintentos y callback final de fallos. Si no hay credenciales QStash, se mantiene el modo síncrono y `wait=false` responde `503`.

La respuesta debe incluir `document_type`, `document_subtype`, `data`, `confidence`, `warnings`, `source` (nombre, MIME, SHA-256, páginas) y `schema_version`. No afirmar que una extracción está confirmada: los valores no legibles van como `null`, y los warnings explican ambigüedades.

Esquema común de factura: emisor/receptor (nombre, RUT), tipo DTE, folio, fecha emisión/vencimiento, moneda, neto, exento, IVA, total, orden de compra y líneas (descripción, cantidad, unidad, precio, subtotal). Cotización agrega vigencia y condiciones; transporte agrega origen/destino, fechas, patente, conductor y números de guía/orden. Mantener estos campos opcionales para reutilizar el contrato entre proyectos.

Errores HTTP: `400` formato/tipo no soportado, `401` token ausente/inválido, `413` archivo demasiado grande, `422` documento ilegible o petición inválida, `429` límite, `502/503` proveedor de extracción no disponible. Usar `request_id` para correlación.

## Diseño de ejecución

1. FastAPI valida bearer, tamaño, MIME/firma y cantidad de páginas antes del procesamiento.
2. El adaptador crea un extractor por tipo de documento. La lógica de proveedores queda detrás de una interfaz `extract(file_bytes, schema, locale)`.
3. Procesar en modo síncrono o, con QStash configurado, encolar cada documento; persistir el PDF pendiente y el resultado/status en SQLite montado en volumen. Consultar con polling usando `job_id`.
4. Validar la salida con Pydantic; normalizar fechas ISO `YYYY-MM-DD`, moneda/códigos y RUT sin inventar valores. Incluir proveedor/modelo y duración en metadata operativa, sin registrar texto sensible.
5. No conectar con R2 ni con BD de Acerosera/Transportes. n8n sube el original a R2, busca/crea o actualiza la factura según reglas del proyecto y guarda la URL/clave del objeto junto a los datos extraídos.

## Integración n8n

### Acerosera

Leer carpeta (cotizaciones, facturas proveedor y cliente) → enviar archivo a `/v1/extractions` con `document_type=auto` y `wait=false` → consultar `GET /v1/extractions/{job_id}` hasta `completed` (o gestionar `failed`) → revisar tipo y campos clave → subir binario a R2 → localizar factura por tipo DTE, RUT emisor, folio y fecha → insertar o actualizar la factura existente sin datos, agregando clave/URL R2 → mover el archivo o registrar resultado. Aplicar idempotencia con hash SHA-256 y no sobrescribir datos confirmados con valores vacíos.

### Transportes / Google Drive

Listar archivos nuevos → bajar contenido → extraer → clasificar usando `document_type` y reglas de carpeta/metadatos → cargar los campos al sistema → archivar/mover el original según resultado. Registrar fallidos para revisión/reintento y evitar duplicados con hash + ID de Drive.

La clasificación automática puede equivocarse: definir umbral de confianza configurable y enviar documentos ambiguos a revisión humana, sin escritura automática en la BD cuando falten claves de identificación.

## Seguridad y despliegue

- Bearer secreto en variable de entorno/secret store; nunca en el repositorio. Comparación en tiempo constante, rotación y tokens separados por consumidor cuando haya más de un proyecto.
- Limitar tamaño, páginas, concurrencia, duración y tasa. Validar contenido real, nombres seguros y rechazar archivos comprimidos/formatos no soportados.
- Persistir temporalmente el PDF pendiente en el volumen local para que la cola no dependa de la memoria del proceso; borrar ese contenido al completar/fallar. Restringir acceso al volumen, proteger secretos QStash y mantener logs sin texto sensible.
- Ejecutar contenedor no-root detrás de Cloudflare Tunnel en un subdominio, sin publicar el puerto de origen. El túnel cifra el acceso de borde al origen; mantener bearer requerido también en el origen.
- Healthcheck sin información sensible. HTTPS público por Cloudflare, límites de carga/timeout coherentes en proxy y API.

## Estructura prevista

```text
app/
  main.py             # FastAPI y endpoints
  auth.py             # Bearer y validación de configuración
  schemas.py          # Contrato común y esquemas por documento
  extraction.py       # Orquestación y validación de resultados
  providers/          # Adaptadores de Gemini/OpenAI u OCR
tests/
Dockerfile
docker-compose.yml
.env.example
IMPLEMENTACION.md
```

## Entregas sugeridas

1. MVP síncrono para PDF y factura chilena, extracción con salida JSON validada, bearer, límites, healthcheck y ejemplos curl/n8n.
2. Clasificación de cotizaciones y documentos de transporte con esquemas versionados y fixtures representativos anonimizados.
3. Métricas de latencia/calidad, reintentos y modo asíncrono cuando volumen o duración lo justifiquen.

## Reutilización del repositorio de origen

El repositorio `document-processing-api` ya tiene FastAPI, autenticación Bearer, validación Pydantic, proveedor Gemini/OpenAI con fallback y extracción de boletas/estados de cuenta en `infra/processing/app/workers/extract_worker.py`, `infra/processing/shared/ai_gateway.py` y `infra/processing/shared/ai_schemas.py`. Reutilizar patrones y adaptadores puntuales; evitar importar el worker completo porque trae dependencias de R2, colas, Mongo y lógica específica de estados bancarios. Separar el nuevo esquema comercial del contrato de transacciones bancarias.

Antes de producción: acordar límites de archivo/páginas, proveedor/modelo, campos obligatorios por tipo y comportamiento ante baja confianza; probar con PDFs reales anonimizados y medir latencia/costo.

## Estado de este repositorio

Implementado en la primera versión:

- Extracción de texto embebido con PyMuPDF y OCR local Tesseract (`spa+eng`) para páginas con imágenes o sin texto seleccionable suficiente.
- Límite de tamaño y páginas, verificación de extensión/MIME/firma PDF, rechazo de PDF cifrado y límite de concurrencia.
- Estructuración/clasificación por la lista `GEMINI_MODELS` en orden; se cambia de modelo ante límite de cuota, modelo no disponible, error transitorio o campos de factura incompletos. Si todos fallan y OpenAI está configurado, se intenta OpenAI.
- Respuesta con texto completo (`raw_text`), hash, páginas, páginas procesadas por OCR, proveedor/modelo usado, duración, confianza y advertencias.
- Imagen Docker con Tesseract y modelos de idioma español e inglés; ejecución como usuario no-root.

Pendiente antes de producción: probar calidad con muestras anonimizadas reales; definir los campos y esquemas específicos de cotizaciones, transporte y otros documentos; decidir umbral de confianza y revisión humana; ajustar límites/timeout y concurrencia al hardware de la VM/CT; instalar/desplegar en Proxmox detrás del túnel/proxy con límites de cuerpo y timeout alineados; y añadir pruebas automatizadas y ejemplos de integración n8n. La API no persiste documentos ni tiene actualmente autenticación por consumidor, rate limiting distribuido, métricas, modo asíncrono ni mecanismo de reintento durable. El proveedor LLM recibe el texto OCR enviado para estructuración, por lo que hay que aprobar esa transferencia de datos y configurar sus credenciales antes de habilitarla.
