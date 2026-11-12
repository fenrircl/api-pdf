from __future__ import annotations

import json
import os
import re
from typing import Any


EXTRACTION_PROMPT = """Eres un extractor de documentos. Usa solamente la evidencia del texto; no inventes valores. Devuelve SOLO JSON válido con esta forma:
{{"document_type":"invoice|quote|transport_document|other|unknown","document_subtype":null,"data":{{}},"confidence":0.0,"warnings":[]}}
Para facturas, data debe incluir invoice_number, customer_rut, supplier_rut, date (YYYY-MM-DD), amount (número), currency, net_amount, tax_amount, issuer, recipient y line_items. Usa null si un campo no aparece o no es legible. En otros documentos extrae los campos relevantes encontrados en data, sin imponer campos de factura. Conserva números y moneda con fidelidad. Idioma/localidad solicitada: {locale}. Tipo solicitado: {document_type}.
Texto del PDF:
{text}
"""

DEFAULT_GEMINI_MODELS = (
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-3-flash-preview",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-pro",
)


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
    response = await client.aio.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0),
    )
    return _parse_json(response.text or "")


async def _openai_extract(prompt: str) -> tuple[dict[str, Any], str]:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"], timeout=float(os.getenv("PROVIDER_TIMEOUT_SECONDS", "60")))
    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    response = await client.chat.completions.create(
        model=model,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": "Extrae datos de documentos sin inventar información. Responde únicamente JSON."},
            {"role": "user", "content": prompt},
        ],
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
    if isinstance(code, int) and code in {400, 401, 402, 403}:
        return False
    message = str(exc).lower()
    if any(marker in message for marker in (
        "prepayment credits are depleted", "payment required", "unauthorized", "permission denied",
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
        for model in _gemini_models():
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
