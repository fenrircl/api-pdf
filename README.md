# api-pdf

API liviana y reutilizable para clasificar documentos y extraer datos. No escribe en las bases de datos de los proyectos consumidores ni administra sus archivos en R2: n8n conserva la orquestación, sube el original y persiste la respuesta.

## Inicio local

```sh
./server.sh
```

El script prepara `.venv`, instala las dependencias si faltan y arranca Uvicorn en `127.0.0.1:8000`. Si no existe `.env`, lo crea con un bearer aleatorio. Se requiere Tesseract instalado en el sistema; en Debian/Ubuntu: `sudo apt-get install tesseract-ocr tesseract-ocr-spa tesseract-ocr-eng`. Configura al menos una clave de proveedor (`GEMINI_API_KEY` y/o `OPENAI_API_KEY`) para clasificar y estructurar; sin ellas, queda disponible extracción local de texto/OCR. Usa `HOST`, `PORT` o `RELOAD=1` para cambiar el inicio.

El endpoint primero extrae texto embebido y aplica OCR local a páginas con imágenes o sin texto suficiente. Después prueba la lista `GEMINI_MODELS` en orden. Si un modelo alcanza un límite diario, se omite por 24 horas (configurable con `GEMINI_DAILY_QUOTA_COOLDOWN_SECONDS`) y se intenta el siguiente; los errores transitorios, como un 503, solo activan el fallback para esa solicitud. Los enfriamientos se guardan en `/var/lib/api-pdf/model-cooldowns.json` en la VM para sobrevivir reinicios y actualizaciones del contenedor. Cuando no queda un Gemini disponible, usa OpenAI si está configurado. Los errores de autenticación o saldo Prepay no se marcan como límite diario, porque suelen afectar a toda la cuenta. El texto extraído completo se devuelve en `raw_text`.

Endpoint: `POST /v1/extractions`, multipart con `file` y campos `document_type` (`auto`, `invoice`, `quote`, `transport_document`) y `locale` (`es-CL`). Para facturas se buscan número, RUT de cliente/proveedor, fecha y monto; si no se leen, se devuelven como `null` cuando el proveedor estructure el documento y se agregan advertencias. Esos campos no se exigen para otros documentos.

Límites configurables: `MAX_UPLOAD_MB`, `MAX_PDF_PAGES`, `MAX_CONCURRENT_EXTRACTIONS`, `OCR_LANG`, `OCR_TIMEOUT_SECONDS`, `OCR_TOTAL_TIMEOUT_SECONDS`, `PROVIDER_TIMEOUT_SECONDS` y `MAX_PROVIDER_TEXT_CHARS`. Para OCR en español e inglés, la imagen base instala los paquetes Tesseract correspondientes.

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

El PAT solicitado por `docker login` necesita `read:packages`. En `.env`, configura `API_BEARER_TOKEN` y `GEMINI_API_KEY` y/o `OPENAI_API_KEY`; no subas `.env` a GitHub. Después de esta configuración, cada push a `master` publica la imagen y la VM la instala automáticamente en un máximo aproximado de cinco minutos. El actualizador crea `/var/lib/api-pdf` para guardar los cooldowns de modelos. Si cambias `deploy/api-pdf-update.sh`, actualiza también la copia instalada en la VM con `sudo install -m 755 deploy/api-pdf-update.sh /usr/local/sbin/api-pdf-update`; esa copia está fuera del contenedor y no se actualiza al descargar una nueva imagen. El workflow construye `linux/amd64`, adecuado para una VM x86_64. Para ver el resultado, usa `sudo journalctl -u api-pdf-update.service`.

Comprueba el servicio con `curl http://127.0.0.1:8000/health`. El contenedor publica el puerto 8000 en las interfaces de la VM; permite el acceso solo desde los consumidores o el proxy que deban usar la API.
