# api-pdf

API liviana y reutilizable para clasificar documentos y extraer datos. No escribe en las bases de datos de los proyectos consumidores ni administra sus archivos en R2: n8n conserva la orquestación, sube el original y persiste la respuesta.

## Inicio local

```sh
./server.sh
```

El script prepara `.venv`, instala las dependencias si faltan y arranca Uvicorn en `127.0.0.1:8000`. Si no existe `.env`, lo crea con un bearer aleatorio. Se requiere Tesseract instalado en el sistema; en Debian/Ubuntu: `sudo apt-get install tesseract-ocr tesseract-ocr-spa tesseract-ocr-eng`. Configura al menos una clave de proveedor (`GEMINI_API_KEY` y/o `OPENAI_API_KEY`) para clasificar y estructurar; sin ellas, queda disponible extracción local de texto/OCR. Usa `HOST`, `PORT` o `RELOAD=1` para cambiar el inicio.

El endpoint primero extrae texto embebido y aplica OCR local a páginas con imágenes o sin texto suficiente. Gemini es el proveedor principal. Si falla, devuelve error, excede su timeout o responde sin clasificar el documento, la solicitud actual pasa a OpenAI y se abre un circuito de 30 minutos: durante ese periodo las solicitudes siguientes van directo a OpenAI. Al vencer el periodo, se vuelve a probar Gemini; si funciona, queda como proveedor principal. El estado se guarda en `/var/lib/api-pdf/gemini-circuit.json` en la VM para sobrevivir reinicios y actualizaciones del contenedor. Si falla el fallback de OpenAI o no está configurado, la API responde `503` con `Retry-After: 10` y un mensaje `No se pudo procesar`; nunca devuelve `document_type: unknown`. Configura `OPENAI_API_KEY` para habilitar el fallback. El texto extraído completo se devuelve en `raw_text` cuando un proveedor logra clasificar el documento.

Endpoint: `POST /v1/extractions`, multipart con `file`, `document_type` (`auto`, `invoice`, `quote`, `transport_document`), `locale` (`es-CL`) y `wait` (`true` o `false`). `wait` por defecto es `true`. Para facturas se buscan número, RUT de cliente/proveedor, fecha y monto; si no se leen, se devuelven como `null` cuando el proveedor estructure el documento y se agregan advertencias. Esos campos no se exigen para otros documentos.

### Jobs en cola con QStash

Con `QSTASH_TOKEN`, claves de firma y `QSTASH_WORKER_URL` configurados, todas las solicitudes se encolan en QStash. `wait=true` (por defecto) conserva la experiencia de esperar la extracción: devuelve el resultado si termina dentro de `JOB_WAIT_SECONDS`; si sigue procesando, responde `202` con el ID y la URL de consulta. `wait=false` devuelve ese `202` inmediatamente. Sin QStash configurado, `wait=true` mantiene el procesamiento síncrono anterior y `wait=false` responde `503`.

QStash debe poder acceder públicamente a `QSTASH_WORKER_URL`, que debe ser la URL HTTPS completa de `POST /internal/qstash/extractions`. La API verifica `Upstash-Signature` con ambas claves de firma rotables. El callback de fallos usa `QSTASH_FAILURE_CALLBACK_URL` (si se omite, se deriva como `/internal/qstash/failures`). Crea una cola llamada igual a `QSTASH_QUEUE_NAME` en QStash y configura su paralelismo igual a `QSTASH_PARALLELISM`. El estado y el PDF pendiente quedan en SQLite bajo `/var/lib/api-pdf/jobs.sqlite3`, dentro del volumen persistente; el PDF se elimina de la fila al completar el job. Los resultados se conservan por `JOB_RETENTION_DAYS` (7 días por defecto). Protege el volumen.

Configura `QSTASH_URL`, `QSTASH_TOKEN`, `QSTASH_CURRENT_SIGNING_KEY`, `QSTASH_NEXT_SIGNING_KEY` y `QSTASH_WORKER_URL` en `.env`. `QSTASH_URL` es la URL regional de Upstash que muestra la consola (por ejemplo, `https://qstash-us-east-1.upstash.io`); la URL del worker debe apuntar a tu API pública. Para encolar y consultar:

```sh
curl --request POST 'https://pdf.solidtec.cl/v1/extractions' \
  --header "Authorization: Bearer $API_BEARER_TOKEN" \
  --form 'file=@./documento.pdf;type=application/pdf' \
  --form 'document_type=auto' \
  --form 'locale=es-CL' \
  --form 'wait=false'

curl 'https://pdf.solidtec.cl/v1/extractions/JOB_ID' \
  --header "Authorization: Bearer $API_BEARER_TOKEN"
```

La respuesta `202` incluye `job_id`, `status_url` y `status` (`queued`, `processing`, `completed` o `failed`). Consulta `GET /v1/extractions/{job_id}` con el mismo bearer hasta que el estado sea `completed` o `failed`; al terminar incluye `result` con el mismo esquema de la respuesta síncrona. Con `wait=true`, la API hace esta espera por hasta 25 segundos por defecto y puede devolver `202` si el job sigue en curso. Con `wait=false`, devuelve `202` inmediatamente. La cola QStash es durable y reintenta entregas; SQLite mantiene estado/resultados y requiere que el volumen persistente esté montado. Para varias réplicas o tolerancia a pérdida de VM, mueve el almacenamiento de jobs a una base administrada compartida.

## Integración con n8n

Usa el nodo **HTTP Request** para enviar cada PDF y recibir una respuesta JSON. El archivo se procesa localmente: se lee el texto embebido, se aplica OCR a páginas con imágenes o poco texto, y luego se clasifica y estructura con Gemini, usando OpenAI como fallback.

### Configurar el nodo HTTP Request

1. Asegúrate de que el nodo anterior entregue el PDF como dato binario. En n8n suele llamarse `data`; usa el nombre real de la propiedad binaria de tu flujo.
2. Configura el nodo **HTTP Request**:
   - **Method:** `POST`
   - **URL:** `https://pdf.solidtec.cl/v1/extractions` (o la URL accesible desde tu instancia n8n)
   - **Authentication:** usa una credencial **Header Auth** con nombre `Authorization` y valor `Bearer <API_BEARER_TOKEN>`. Mantén el token en credenciales de n8n, no escrito en el workflow.
   - **Send Body:** activado
   - **Body Content Type:** `Form-Data`
   - Agrega `file` como tipo **n8n Binary File**; en **Input Data Field Name** indica la propiedad binaria, por ejemplo `data`.
   - Agrega `document_type` como **Form Data**, valor `auto` para clasificar cualquier PDF automáticamente. También puedes indicar `invoice`, `quote` o `transport_document` cuando ya conozcas el tipo.
   - Agrega `locale` como **Form Data**, valor `es-CL`.
   - Agrega `wait` como **Form Data**. Usa `true` para esperar hasta 25 segundos; usa `false` para iniciar el job y pasar de inmediato al polling.
   - Deja que n8n genere el encabezado `Content-Type` de multipart; no lo fijes manualmente, porque debe incluir el límite (boundary) del formulario.
   - Configura el formato de respuesta como JSON/Autodetect. Trata tanto HTTP `200` como `202` como respuestas válidas.

Para el flujo asíncrono, envía `wait=false`. Después del POST, agrega un nodo **HTTP Request** GET a `https://pdf.solidtec.cl{{$json.status_url}}` usando la misma credencial Bearer. Revisa `status`: si es `queued` o `processing`, espera unos segundos y vuelve a consultar; si es `completed`, usa `result`; si es `failed`, deriva el job a manejo de errores. Si activas **Include Response Headers and Status**, n8n puede anidar el JSON bajo `body`; ajusta las expresiones (`$json.status_url`, `$json.status`, `$json.result`) a esa salida.

El nodo requiere que el campo del archivo se llame exactamente `file`. No envíes el PDF como JSON ni como texto base64. Consulta la documentación del [nodo HTTP Request de n8n](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.httprequest/) para los nombres de opciones de tu versión.

### Prueba con curl

```sh
export API_BEARER_TOKEN='tu-token-configurado-en-el-servidor'
curl --request POST 'https://pdf.solidtec.cl/v1/extractions' \
  --header "Authorization: Bearer $API_BEARER_TOKEN" \
  --form 'file=@./documento.pdf;type=application/pdf' \
  --form 'document_type=auto' \
  --form 'locale=es-CL' \
  --form 'wait=true'
```

Con QStash habilitado, `wait=true` puede responder `202` si el job aún no termina después del tiempo de espera; en ese caso consulta el `status_url` como en el ejemplo anterior.

### Respuesta y uso de los datos

La respuesta incluye siempre `data` como objeto JSON y `raw_text` con el texto completo reconocido. En facturas, `data` puede contener `invoice_number`, `customer_rut`, `supplier_rut`, `date` (`YYYY-MM-DD`), `amount`, `currency`, `net_amount`, `tax_amount`, `issuer`, `recipient` y `line_items`. Los cinco campos de factura principales pueden venir como `null` si no se pudieron leer; para otros documentos no se exigen esos campos y `data` contiene los valores relevantes encontrados.

Ejemplo abreviado de respuesta:

```json
{
  "request_id": "uuid-de-la-solicitud",
  "document_type": "invoice",
  "document_subtype": null,
  "data": {
    "invoice_number": "F-123",
    "customer_rut": "76.123.456-7",
    "supplier_rut": "77.123.456-8",
    "date": "2026-11-12",
    "amount": 11900,
    "currency": "CLP",
    "net_amount": 10000,
    "tax_amount": 1900,
    "issuer": "Proveedor Ejemplo",
    "recipient": "Cliente Ejemplo",
    "line_items": []
  },
  "raw_text": "Texto completo reconocido del PDF...",
  "confidence": 0.95,
  "warnings": [],
  "source": {
    "filename": "documento.pdf",
    "mime_type": "application/pdf",
    "sha256": "hash-del-archivo",
    "pages": 1
  },
  "schema_version": "1.0",
  "extraction": {
    "ocr_pages": 1,
    "ocr_failures": 0,
    "provider": "gemini",
    "model": "gemini-3.5-flash",
    "duration_ms": 1200
  }
}
```

Después del nodo HTTP Request, puedes acceder directamente a `{{$json.document_type}}`, `{{$json.data.invoice_number}}`, `{{$json.data.amount}}`, `{{$json.raw_text}}` y `{{$json.extraction.provider}}`. Si la opción **Include Response Headers and Status** está activada, la respuesta puede quedar anidada bajo `body`; ajusta entonces las expresiones a la estructura mostrada por la salida del nodo.

Antes de guardar o automatizar el registro, revisa `warnings`, `confidence` y `extraction.provider`. Un HTTP `200` indica que el PDF fue procesado, pero si los proveedores fallan puede venir `data` vacío, `provider: null` y una advertencia mientras se conserva `raw_text`. En facturas, revisa además que `invoice_number`, `customer_rut`, `supplier_rut`, `date` y `amount` no sean `null`. El `raw_text` puede contener datos personales o comerciales; guárdalo solo si tu flujo lo necesita.

### Casos de prueba: cotización, factura de proveedor y factura de cliente

La carpeta `test_pdf/` contiene tres ejemplos reales. Los PDFs están excluidos de Git; los resultados estructurados pueden variar según proveedor/modelo y disponibilidad. En la prueba de la factura de proveedor sí se obtuvo JSON estructurado; las pruebas de cotización y del PDF de cliente conservaron el texto OCR, pero el proveedor no devolvió una estructura (`provider: null`). Por eso los siguientes puntos describen el contrato actual y las limitaciones observadas, no garantizan que cada campo aparezca en cada documento.

| Archivo de prueba | Qué contiene / cómo enviarlo | Consideraciones para n8n |
|---|---|---|
| `cotizacion_283 (1).pdf` | Cotización de una página. Enviar `document_type=quote` o `auto`. | Puede extraer número de cotización, emisor/proveedor, cliente, fecha, totales y detalle. Para el documento de prueba el OCR leyó una línea con descripción, cantidad, precio y total; la IA no llegó a estructurarla en esa ejecución. |
| `otero 1 item.pdf` | Factura de proveedor de una página. Enviar `document_type=invoice` o `auto`. | La respuesta real clasificó `invoice` con `gemini-3.5-flash` y confianza `0.95`. Extrajo los campos de factura y una línea de producto. |
| `Receipt_006222.pdf` | PDF de 13 páginas que contiene varias facturas electrónicas de cliente, no una sola factura. Enviar el archivo completo devuelve como máximo un objeto de extracción para todo el PDF. | Separar el PDF en una factura por archivo antes de llamar a este endpoint. La API no separa lotes ni devuelve `documents[]`; el OCR detecta texto de varias facturas, pero no permite mapear cada una con seguridad a un único objeto. |

En una factura individual, `supplier_rut` y `issuer` identifican al emisor/proveedor; `customer_rut` y `recipient` identifican al receptor/cliente. La API no decide por sí sola si es una factura de compra o venta desde la perspectiva de tu empresa. En n8n compara los RUT normalizados con el RUT de tu empresa:

- Si `customer_rut` coincide con el RUT de tu empresa, normalmente es una factura recibida de proveedor.
- Si `supplier_rut` coincide con el RUT de tu empresa, normalmente es una factura emitida a cliente.
- Si ninguno coincide, ambos son nulos o la confianza es baja, deriva el documento a revisión.

El `line_items` de la factura de proveedor real tuvo esta forma (valores anonimizados):

```json
{
  "code": "ITEM-01",
  "description": "Descripción del producto o servicio",
  "quantity": 1.0,
  "unit_price": 10000,
  "total_amount": 10000
}
```

`quantity`, `unit_price` y `total_amount` fueron números JSON en esa respuesta. **No apareció una unidad (`unit`) en el resultado real** y el extractor actual no garantiza ese campo ni normaliza todas las líneas de cotizaciones a un esquema fijo. En n8n trata `unit` como opcional y no la infieras; si tu sistema la requiere, deja el registro pendiente cuando falte. Para cotizaciones envía `document_type=quote` y revisa `raw_text`, `warnings` y `provider` antes de insertar las líneas.

Para automatizar el flujo: clasifica con `auto` cuando el tipo no se conoce; para la rama de cotizaciones puedes enviar `quote`; para facturas, normaliza y compara los RUT. Recorre `data.line_items` como una lista de objetos y mapea por nombre de campo (`description`, `quantity`, `unit_price`, `total_amount`), no por posición. Si recibes varios documentos dentro de un PDF, sepáralos antes o solicita revisión en lugar de guardar líneas mezcladas.

### Métrica interna de procesamiento

En la VM cada solicitud completada agrega una línea JSON a `/var/lib/api-pdf/metrics.jsonl`. El registro permite contar documentos y caracteres de texto procesados por archivo; guarda fecha, `request_id`, nombre de archivo, tipo detectado, páginas, páginas OCR, caracteres y bytes del texto, proveedor/modelo, duración y cantidad de advertencias. **No guarda el contenido de `raw_text`**. Para revisar las entradas recientes en la VM:

```sh
sudo tail -n 20 /var/lib/api-pdf/metrics.jsonl
```

Si se alcanzan todas las plazas definidas por `MAX_CONCURRENT_EXTRACTIONS`, la API espera hasta `EXTRACTION_QUEUE_TIMEOUT_SECONDS` (30 segundos por defecto). Si no queda una plaza libre, responde `503 Service Unavailable`, con el mensaje `API ocupada procesando otros documentos; reintente en unos segundos.` y la cabecera `Retry-After: 10`. Configura el nodo HTTP Request de n8n para reintentar errores 503 después de una espera; el flujo de importación incluido procesa un PDF a la vez.

Límites configurables: `MAX_UPLOAD_MB`, `MAX_PDF_PAGES`, `MAX_CONCURRENT_EXTRACTIONS`, `EXTRACTION_QUEUE_TIMEOUT_SECONDS`, `GEMINI_ATTEMPT_TIMEOUT_SECONDS`, `GEMINI_FAILURE_COOLDOWN_SECONDS`, `OCR_LANG`, `OCR_TIMEOUT_SECONDS`, `OCR_TOTAL_TIMEOUT_SECONDS`, `PROVIDER_TIMEOUT_SECONDS` y `MAX_PROVIDER_TEXT_CHARS`. Para OCR en español e inglés, la imagen base instala los paquetes Tesseract correspondientes.

## Despliegue automático en VM

El workflow de GitHub Actions construye y publica la imagen `latest` en cada push a `master`. En la VM, un timer de systemd comprueba GHCR cada cinco minutos, actualiza el contenedor solo cuando cambia la imagen y lo revierte a la imagen anterior si falla el healthcheck. No requiere Docker Compose. La configuración de la VM se hace una sola vez; luego basta hacer `git push` para desplegar.

Primero, sube estos archivos al repositorio GitHub para activar el workflow. En una VM Debian/Ubuntu con Docker instalado, instala Git y clona el repo:

```sh
sudo apt-get update
sudo apt-get install -y git
git clone https://github.com/fenrircl/api-pdf.git
cd api-pdf
```

Luego configura el entorno e instala el timer:

```sh
cp .env.example .env
nano .env
sudo mkdir -p /etc/api-pdf
sudo install -m 600 .env /etc/api-pdf/api-pdf.env
sudo docker login ghcr.io -u fenrircl
sudo install -m 755 deploy/api-pdf-update.sh /usr/local/sbin/api-pdf-update
sudo install -m 644 deploy/api-pdf-update.service deploy/api-pdf-update.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now api-pdf-update.timer
sudo systemctl start api-pdf-update.service
```

El PAT solicitado por `docker login` necesita `read:packages`. En `.env`, configura `API_BEARER_TOKEN` y `GEMINI_API_KEY` y/o `OPENAI_API_KEY`; no subas `.env` a GitHub. Después de esta configuración, cada push a `master` publica la imagen y la VM la instala automáticamente en un máximo aproximado de cinco minutos. El actualizador crea `/var/lib/api-pdf` para guardar los cooldowns de modelos, el circuito de Gemini y las métricas. Si cambias `deploy/api-pdf-update.sh`, actualiza también la copia instalada en la VM con `sudo install -m 755 deploy/api-pdf-update.sh /usr/local/sbin/api-pdf-update`; esa copia está fuera del contenedor y no se actualiza al descargar una nueva imagen. El workflow construye `linux/amd64`, adecuado para una VM x86_64. Para ver el resultado, usa `sudo journalctl -u api-pdf-update.service`.

Comprueba el servicio con `curl http://127.0.0.1:8000/health`. El contenedor publica el puerto 8000 en las interfaces de la VM; permite el acceso solo desde los consumidores o el proxy que deban usar la API.
