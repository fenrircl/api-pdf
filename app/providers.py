from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import tempfile
import threading
import time
from typing import Any


EXTRACTION_PROMPT = """Eres un extractor de documentos. Usa solamente la evidencia del texto; no inventes valores. Devuelve SOLO JSON válido con esta forma:
{{"document_type":"invoice|quote|transport_document|other|unknown","document_subtype":null,"data":{{}},"confidence":0.0,"warnings":[]}}
Para facturas, data debe incluir invoice_number, customer_rut, supplier_rut, date (YYYY-MM-DD), amount (número), currency, net_amount, tax_amount, issuer, recipient y line_items. Usa null si un campo no aparece o no es legible. En otros documentos extrae los campos relevantes encontrados en data, sin imponer campos de factura. Conserva números y moneda con fidelidad. Idioma/localidad solicitada: {locale}. Tipo solicitado: {document_type}.
Texto del PDF:
{text}
"""

DEFAULT_GEMINI_MODELS = (
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3-flash-preview",
    # Known models that were temporarily overloaded or unavailable to this key
    # go last so a transient failure does not delay the common path.
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
)

logger = logging.getLogger(__name__)
_COOLDOWN_LOCK = threading.RLock()
_COOLDOWN_CACHE: dict[str, float] = {}


def _parse_json(value: str) -> dict[str, Any]:
    value = value.strip()
    value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value, flags=re.IGNORECASE)
    result = json.loads(value)
    if not isinstance(result, dict):
        raise ValueError("Provider returned a non-object JSON value")
    return result


async def _gemini_extract(prompt: str, model: str) -> dict[str, Any]:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    config_options: dict[str, Any] = {"response_mime_type": "application/json"}
    model_major = re.match(r"^gemini-(\d+)", model)
    if not model_major or int(model_major.group(1)) < 3:
        config_options["temperature"] = 0
    response = await client.aio.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(**config_options),
    )
    return _parse_json(response.text or "")


async def _openai_extract(prompt: str) -> tuple[dict[str, Any], str]:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"], timeout=float(os.getenv("PROVIDER_TIMEOUT_SECONDS", "60")))
    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    request_options: dict[str, Any] = {}
    if model.startswith("gpt-6-luna"):
        # GPT-6 reasoning models reject temperature when reasoning_effort is set.
        request_options["reasoning_effort"] = os.getenv("OPENAI_REASONING_EFFORT", "medium")
    else:
        request_options["temperature"] = 0
    response = await client.chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": "Extrae datos de documentos sin inventar información. Responde únicamente JSON."},
            {"role": "user", "content": prompt},
        ],
        **request_options,
    )
    return _parse_json(response.choices[0].message.content or ""), model


def _invoice_incomplete(result: dict[str, Any], requested_type: str) -> bool:
    if requested_type != "invoice" and result.get("document_type") != "invoice":
        return False
    data = result.get("data")
    if not isinstance(data, dict):
        return True
    required = ("invoice_number", "customer_rut", "supplier_rut", "date", "amount")
    return any(data.get(key) in (None, "") for key in required)


def _gemini_models() -> list[str]:
    configured = os.getenv("GEMINI_MODELS", "")
    if configured.strip():
        candidates = [model.strip() for model in configured.split(",") if model.strip()]
    else:
        candidates = list(DEFAULT_GEMINI_MODELS)
        # Keep a legacy custom model available after the recommended model chain.
        legacy_model = os.getenv("GEMINI_MODEL", "").strip()
        if legacy_model:
            candidates.append(legacy_model)
    return list(dict.fromkeys(candidates))


def _cooldown_path() -> str:
    return os.getenv("GEMINI_COOLDOWN_FILE", "/tmp/api-pdf-model-cooldowns.json")


def _cooldown_key(model: str) -> str:
    key_hash = hashlib.sha256(os.getenv("GEMINI_API_KEY", "").encode()).hexdigest()[:12]
    return f"{key_hash}:{model}"


def _load_model_cooldowns() -> dict[str, float]:
    global _COOLDOWN_CACHE
    now = time.time()
    with _COOLDOWN_LOCK:
        active = {key: expiry for key, expiry in _COOLDOWN_CACHE.items() if expiry > now}
        try:
            with open(_cooldown_path(), encoding="utf-8") as cooldown_file:
                stored = json.load(cooldown_file)
            if isinstance(stored, dict):
                for key, expiry in stored.items():
                    if isinstance(expiry, (int, float)) and expiry > now:
                        active[key] = float(expiry)
        except FileNotFoundError:
            pass
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read Gemini cooldown state: %s", type(exc).__name__)
        _COOLDOWN_CACHE = active
        return dict(active)


def _save_model_cooldowns(cooldowns: dict[str, float]) -> None:
    global _COOLDOWN_CACHE
    with _COOLDOWN_LOCK:
        _COOLDOWN_CACHE = dict(cooldowns)
        target = _cooldown_path()
        temp_name: str | None = None
        try:
            directory = os.path.dirname(target) or "."
            os.makedirs(directory, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=directory, delete=False
            ) as temp_file:
                json.dump(cooldowns, temp_file)
                temp_name = temp_file.name
            os.replace(temp_name, target)
        except OSError as exc:
            logger.warning("Could not persist Gemini cooldown state: %s", type(exc).__name__)
            if temp_name:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass


def _is_daily_quota_error(exc: Exception) -> bool:
    """Only persist cooldowns when the provider identifies a daily quota limit."""
    details = getattr(exc, "response_json", None)
    message = str(exc).lower()
    if details is not None:
        try:
            message += " " + json.dumps(details).lower()
        except (TypeError, ValueError):
            pass
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if isinstance(code, str) and code.isdigit():
        code = int(code)
    if code is not None and code != 429 and "429" not in message:
        return False
    compact = re.sub(r"[\s_-]+", "", message)
    return any(marker in compact for marker in (
        "perday", "dailyquota", "dailylimit", "requests/day", "requestperday",
    ))


def _models_not_in_cooldown(models: list[str]) -> list[str]:
    cooldowns = _load_model_cooldowns()
    now = time.time()
    available: list[str] = []
    for model in models:
        expiry = cooldowns.get(_cooldown_key(model))
        if expiry and expiry > now:
            logger.info(
                "Skipping Gemini model in daily quota cooldown model=%s retry_in_seconds=%d",
                model, max(1, int(expiry - now)),
            )
        else:
            available.append(model)
    return available


def _cool_down_model_for_daily_quota(model: str) -> None:
    cooldown_seconds = max(60, int(os.getenv("GEMINI_DAILY_QUOTA_COOLDOWN_SECONDS", "86400")))
    cooldowns = _load_model_cooldowns()
    expiry = time.time() + cooldown_seconds
    cooldowns[_cooldown_key(model)] = expiry
    _save_model_cooldowns(cooldowns)
    logger.warning(
        "Gemini daily quota reached; cooling down model=%s cooldown_seconds=%d",
        model, cooldown_seconds,
    )


def _should_try_next_model(exc: Exception) -> bool:
    """Fail over for model/quota/transient errors, but not account-wide failures."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if isinstance(code, str) and code.isdigit():
        code = int(code)
    if not isinstance(code, int):
        response_json = getattr(exc, "response_json", None)
        if isinstance(response_json, dict):
            error = response_json.get("error", {})
            if isinstance(error, dict):
                code = error.get("code")
    if isinstance(code, int) and code in {401, 402}:
        return False
    message = str(exc).lower()
    if any(marker in message for marker in (
        "prepayment credits are depleted", "payment required", "unauthorized",
    )):
        return False
    return True


async def extract_with_provider(
    text: str, *, document_type: str, locale: str
) -> tuple[dict[str, Any], str | None, str | None]:
    """Try Gemini first and OpenAI second; OCR has already run locally."""
    if not text.strip():
        return {}, None, None
    prompt = EXTRACTION_PROMPT.format(
        locale=locale[:32], document_type=document_type,
        text=text[: int(os.getenv("MAX_PROVIDER_TEXT_CHARS", "100000"))],
    )
    errors: list[str] = []
    if os.getenv("GEMINI_API_KEY"):
        first_result: dict[str, Any] | None = None
        first_model: str | None = None
        configured_models = _gemini_models()
        available_models = _models_not_in_cooldown(configured_models)
        if configured_models and not available_models:
            errors.append("gemini:all_models_in_daily_quota_cooldown")
        for model in available_models:
            try:
                result = await _gemini_extract(prompt, model)
                if first_result is None:
                    first_result = result
                    first_model = model
                if not _invoice_incomplete(result, document_type):
                    return result, "gemini", model
                errors.append(f"{model}:invoice_fields_incomplete")
            except Exception as exc:
                errors.append(f"{model}:{type(exc).__name__}")
                if _is_daily_quota_error(exc):
                    _cool_down_model_for_daily_quota(model)
                if not _should_try_next_model(exc):
                    break
        if first_result is not None and not os.getenv("OPENAI_API_KEY"):
            return first_result, "gemini", first_model
    if os.getenv("OPENAI_API_KEY"):
        try:
            result, model = await _openai_extract(prompt)
            return result, "openai", model
        except Exception as exc:
            errors.append(f"openai:{type(exc).__name__}")
    if errors:
        raise RuntimeError("All configured extraction providers failed: " + ",".join(errors))
    return {}, None, None
