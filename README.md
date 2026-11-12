# api-pdf

API liviana y reutilizable para clasificar documentos y extraer datos. No escribe en las bases de datos de los proyectos consumidores ni administra sus archivos en R2: n8n conserva la orquestación, sube el original y persiste la respuesta.

## Inicio local

```sh
./server.sh
```

El script prepara `.venv`, instala las dependencias si faltan y arranca Uvicorn en `127.0.0.1:8000`. Si no existe `.env`, lo crea con un bearer aleatorio. Se requiere Tesseract instalado en el sistema; en Debian/Ubuntu: `sudo apt-get install tesseract-ocr tesseract-ocr-spa tesseract-ocr-eng`. Configura al menos una clave de proveedor (`GEMINI_API_KEY` y/o `OPENAI_API_KEY`) para clasificar y estructurar; sin ellas, queda disponible extracción local de texto/OCR. Usa `HOST`, `PORT` o `RELOAD=1` para cambiar el inicio.

El endpoint primero extrae texto embebido y aplica OCR local a páginas con imágenes o sin texto suficiente. Después prueba la lista `GEMINI_MODELS` en orden y cambia de modelo ante cuota agotada, modelo no disponible o error transitorio; al agotarse Gemini, usa OpenAI si está configurado. La lista predeterminada va de Gemini 3.8 Flash a Gemini 2.5 Pro y puede editarse desde `.env`. Un error de facturación de Gemini no se resuelve rotando modelos. El texto extraído completo se devuelve en `raw_text`.

Endpoint: `POST /v1/extractions`, multipart con `file` y campos `document_type` (`auto`, `invoice`, `quote`, `transport_document`) y `locale` (`es-CL`). Para facturas se buscan número, RUT de cliente/proveedor, fecha y monto; si no se leen, se devuelven como `null` cuando el proveedor estructure el documento y se agregan advertencias. Esos campos no se exigen para otros documentos.

Límites configurables: `MAX_UPLOAD_MB`, `MAX_PDF_PAGES`, `MAX_CONCURRENT_EXTRACTIONS`, `OCR_LANG`, `OCR_TIMEOUT_SECONDS`, `OCR_TOTAL_TIMEOUT_SECONDS`, `PROVIDER_TIMEOUT_SECONDS` y `MAX_PROVIDER_TEXT_CHARS`. Para OCR en español e inglés, la imagen base instala los paquetes Tesseract correspondientes.

## Despliegue en VM con Docker Compose

Instala Docker Engine con el plugin Compose en la VM. Si el paquete GHCR es privado, autentícate con un PAT que tenga `read:packages`:

```sh
docker login ghcr.io -u fenrircl
cp .env.example .env
nano .env
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

En `.env`, configura `API_BEARER_TOKEN` y `GEMINI_API_KEY` y/o `OPENAI_API_KEY`. La imagen de despliegue se publica en GHCR. Para actualizar la aplicación sin reemplazar la VM:

```sh
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

Comprueba el servicio con `curl http://127.0.0.1:8000/health`. El archivo de despliegue publica el puerto 8000 en la interfaz de red de la VM; permite el acceso solo desde los consumidores o el proxy que deban usar la API.
