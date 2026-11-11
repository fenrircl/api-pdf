# api-pdf

API liviana y reutilizable para clasificar documentos y extraer datos. No escribe en las bases de datos de los proyectos consumidores ni administra sus archivos en R2: n8n conserva la orquestación, sube el original y persiste la respuesta.

## Inicio local

```sh
cp .env.example .env
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
uvicorn app.main:app --reload
```

Configura `API_BEARER_TOKEN` y `GEMINI_API_KEY` o `OPENAI_API_KEY`. La documentación de implementación está en `IMPLEMENTACION.md`.

Endpoint inicial: `POST /v1/extractions`, multipart con `file` y campos `document_type` (`auto`, `invoice`, `quote`, `transport_document`) y `locale` (`es-CL`). Respuesta JSON normalizada.
