# api-pdf

API liviana y reutilizable para clasificar documentos y extraer datos. No escribe en las bases de datos de los proyectos consumidores ni administra sus archivos en R2: n8n conserva la orquestación, sube el original y persiste la respuesta.

## Inicio local

```sh
cp .env.example .env
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
uvicorn app.main:app --reload
```

Configura `API_BEARER_TOKEN` y al menos una clave de proveedor (`GEMINI_API_KEY` y/o `OPENAI_API_KEY`). El contenedor instala Tesseract con idiomas español e inglés; el endpoint primero extrae texto embebido y aplica OCR local a páginas sin texto suficiente, luego usa Gemini y OpenAI como respaldo en ese orden para clasificar y estructurar. El texto extraído completo se devuelve en `raw_text`.

Endpoint: `POST /v1/extractions`, multipart con `file` y campos `document_type` (`auto`, `invoice`, `quote`, `transport_document`) y `locale` (`es-CL`). Para facturas se buscan número, RUT de cliente/proveedor, fecha y monto; si no se leen, se devuelven como `null` cuando el proveedor estructure el documento y se agregan advertencias. Esos campos no se exigen para otros documentos.

Límites configurables: `MAX_UPLOAD_MB`, `MAX_PDF_PAGES`, `MAX_CONCURRENT_EXTRACTIONS`, `OCR_LANG`, `OCR_TIMEOUT_SECONDS`, `OCR_TOTAL_TIMEOUT_SECONDS`, `PROVIDER_TIMEOUT_SECONDS` y `MAX_PROVIDER_TEXT_CHARS`. Para OCR en español e inglés, la imagen base instala los paquetes Tesseract correspondientes.
