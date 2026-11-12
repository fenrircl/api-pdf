from __future__ import annotations

import asyncio
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
Para facturas, data debe incluir invoice_number, customer_rut, supplier_rut, date (YYYY-MM-DD), amount (total final), currency, net_amount, tax_amount, issuer, recipient y line_items. Si el total final no es legible pero el monto neto y el IVA sí lo son, calcula amount = net_amount + tax_amount solo cuando no haya montos exentos, descuentos no nulos, impuestos adicionales ni otros cargos que alteren la suma; agrega una advertencia indicando que el total fue calculado y requiere revisión. Si la suma no es segura, usa null. Usa null para cualquier otro campo que no aparezca o no sea legible. En otros documentos extrae los campos relevantes encontrados en data, sin imponer campos de factura. Conserva números y moneda con fidelidad. Idioma/localidad solicitada: {locale}. Tipo solicitado: {document_type}.
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
GEMINI_ATTEMPT_TIMEOUT_SECONDS = max(0.1, float(os.getenv("GEMINI_ATTEMPT_TIMEOUT_SECONDS", "30")))
GEMINI_FAILURE_COOLDOWN_SECONDS = max(1, int(os.getenv("GEMINI_FAILURE_COOLDOWN_SECONDS", "1800")))
GEMINI_CIRCUIT_FILE = os.getenv("GEMINI_CIRCUIT_FILE", "/tmp/api-pdf-gemini-circuit.json")
SUPPORTED_DOCUMENT_TYPES = {"invoice", "receipt", "quote", "transport_document", "other"}
_COOLDOWN_LOCK = threading.RLock()
_COOLDOWN_CACHE: dict[str, float] = {}
_GEMINI_CIRCUIT_LOCK = threading.RLock()
_GEMINI_CIRCUIT_UNTIL = 0.0


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


def _gemini_circuit_key() -> str:
    key_hash = hashlib.sha256(os.getenv("GEMINI_API_KEY", "").encode()).hexdigest()[:12]
    return f"gemini:{key_hash}"


def _read_gemini_circuit_state() -> dict[str, float]:
    try:
        with open(GEMINI_CIRCUIT_FILE, encoding="utf-8") as state_file:
            state = json.load(state_file)
        if isinstance(state, dict):
            return {key: float(value) for key, value in state.items() if isinstance(value, (int, float))}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning("Could not read Gemini circuit state: %s", type(exc).__name__)
    return {}


def _write_gemini_circuit_state(state: dict[str, float]) -> None:
    temp_name: str | None = None
    try:
        directory = os.path.dirname(GEMINI_CIRCUIT_FILE) or "."
        os.makedirs(directory, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, delete=False) as state_file:
            json.dump(state, state_file)
            temp_name = state_file.name
        os.replace(temp_name, GEMINI_CIRCUIT_FILE)
    except OSError as exc:
        logger.warning("Could not persist Gemini circuit state: %s", type(exc).__name__)
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass


def _gemini_circuit_remaining() -> int:
    now = time.time()
    with _GEMINI_CIRCUIT_LOCK:
        expiry = max(_GEMINI_CIRCUIT_UNTIL, _read_gemini_circuit_state().get(_gemini_circuit_key(), 0))
    return max(0, int(expiry - now))


def _open_gemini_circuit(reason: str) -> None:
    global _GEMINI_CIRCUIT_UNTIL
    expiry = time.time() + GEMINI_FAILURE_COOLDOWN_SECONDS
    with _GEMINI_CIRCUIT_LOCK:
        _GEMINI_CIRCUIT_UNTIL = expiry
        state = _read_gemini_circuit_state()
        state[_gemini_circuit_key()] = expiry
        _write_gemini_circuit_state(state)
    logger.warning(
        "Gemini circuit opened cooldown_seconds=%d reason=%s",
        GEMINI_FAILURE_COOLDOWN_SECONDS, reason,
    )


def _close_gemini_circuit() -> None:
    global _GEMINI_CIRCUIT_UNTIL
    with _GEMINI_CIRCUIT_LOCK:
        _GEMINI_CIRCUIT_UNTIL = 0.0
        state = _read_gemini_circuit_state()
        state.pop(_gemini_circuit_key(), None)
        _write_gemini_circuit_state(state)


def _has_supported_document_type(result: dict[str, Any]) -> bool:
    return result.get("document_type") in SUPPORTED_DOCUMENT_TYPES


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
    """Prefer Gemini; open a persistent 30-minute circuit after any Gemini failure."""
    if not text.strip():
        raise RuntimeError("No text was extracted from the PDF")
    prompt = EXTRACTION_PROMPT.format(
        locale=locale[:32], document_type=document_type,
        text=text[: int(os.getenv("MAX_PROVIDER_TEXT_CHARS", "100000"))],
    )
    errors: list[str] = []
    first_result: dict[str, Any] | None = None
    first_model: str | None = None
    loop = asyncio.get_running_loop()
    provider_started = loop.time()
    provider_timeout = max(1.0, float(os.getenv("PROVIDER_TIMEOUT_SECONDS", "60")))

    if os.getenv("GEMINI_API_KEY"):
        circuit_remaining = _gemini_circuit_remaining()
        if circuit_remaining:
            logger.info("Gemini circuit open; using OpenAI fallback retry_in_seconds=%d", circuit_remaining)
            if not os.getenv("OPENAI_API_KEY"):
                errors.append("gemini_circuit_open_without_openai")
        else:
            models = _models_not_in_cooldown(_gemini_models())
            if not models:
                errors.append("gemini:no_available_models")
                _open_gemini_circuit("no_available_models")
            else:
                model = models[0]
                try:
                    result = await asyncio.wait_for(
                        _gemini_extract(prompt, model),
                        timeout=min(GEMINI_ATTEMPT_TIMEOUT_SECONDS, max(0.1, provider_timeout - 1)),
                    )
                    if not _has_supported_document_type(result):
                        raise RuntimeError("Gemini returned an unclassified document")
                    if _invoice_incomplete(result, document_type):
                        first_result, first_model = result, model
                        raise RuntimeError("Gemini returned incomplete invoice fields")
                    _close_gemini_circuit()
                    return result, "gemini", model
                except Exception as exc:
                    errors.append(f"gemini:{type(exc).__name__}")
                    if _is_daily_quota_error(exc):
                        _cool_down_model_for_daily_quota(model)
                    _open_gemini_circuit(type(exc).__name__)

    if os.getenv("OPENAI_API_KEY"):
        remaining = provider_timeout - (loop.time() - provider_started) - 0.5
        if remaining > 0:
            try:
                result, model = await asyncio.wait_for(_openai_extract(prompt), timeout=remaining)
                if not _has_supported_document_type(result):
                    raise RuntimeError("OpenAI returned an unclassified document")
                return result, "openai", model
            except Exception as exc:
                errors.append(f"openai:{type(exc).__name__}")
        else:
            errors.append("openai:no_time_remaining")

    if first_result is not None:
        return first_result, "gemini", first_model
    if not os.getenv("GEMINI_API_KEY") and not os.getenv("OPENAI_API_KEY"):
        errors.append("no_provider_configured")
    raise RuntimeError("No provider could classify the document: " + ",".join(errors))
