#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [[ ! -f .env ]]; then
  cp .env.example .env
  TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  sed -i "s/^API_BEARER_TOKEN=.*/API_BEARER_TOKEN=${TOKEN}/" .env
  echo "Creé .env con un bearer aleatorio. Puedes consultarlo localmente con: grep '^API_BEARER_TOKEN=' .env"
fi

# .env debe ser un archivo local confiable con formato KEY=value.
set -a
# shellcheck disable=SC1091
source .env
set +a

if [[ -z "${API_BEARER_TOKEN:-}" || "$API_BEARER_TOKEN" == "replace-with-long-random-secret" ]]; then
  echo "Configura un API_BEARER_TOKEN propio en .env antes de iniciar." >&2
  exit 1
fi

if ! command -v tesseract >/dev/null 2>&1; then
  echo "No se encontró Tesseract, necesario para procesar páginas escaneadas." >&2
  echo "En Debian/Ubuntu instala: sudo apt-get install tesseract-ocr tesseract-ocr-spa tesseract-ocr-eng" >&2
  exit 1
fi

if [[ -z "${GEMINI_API_KEY:-}" && -z "${OPENAI_API_KEY:-}" ]]; then
  echo "Aviso: no hay claves Gemini/OpenAI; el servicio devolverá texto OCR sin clasificación estructurada."
fi

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi

PYTHON=".venv/bin/python"
if ! "$PYTHON" -c 'import fastapi, fitz, PIL, pytesseract, google.genai, openai' >/dev/null 2>&1; then
  echo "Instalando dependencias del servicio en .venv..."
  "$PYTHON" -m pip install --upgrade pip
  "$PYTHON" -m pip install -e .
fi

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
echo "Iniciando api-pdf en http://${HOST}:${PORT} (Ctrl+C para detener)"
if [[ "${RELOAD:-0}" == "1" ]]; then
  exec .venv/bin/uvicorn app.main:app --host "$HOST" --port "$PORT" --reload
fi
exec .venv/bin/uvicorn app.main:app --host "$HOST" --port "$PORT"
