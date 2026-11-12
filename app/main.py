from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from typing import Any, Literal

import fitz
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

from app import jobs
from app.providers import extract_with_provider

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("api_pdf")

app = FastAPI(title="api-pdf", version="0.2.0")
MAX_BYTES = int(os.getenv("MAX_UPLOAD_MB", "20")) * 1024 * 1024
MAX_PAGES = int(os.getenv("MAX_PDF_PAGES", "50"))
OCR_LANG = os.getenv("OCR_LANG", "spa+eng")
OCR_MIN_CHARS_PER_PAGE = int(os.getenv("OCR_MIN_CHARS_PER_PAGE", "25"))
OCR_TIMEOUT_SECONDS = int(os.getenv("OCR_TIMEOUT_SECONDS", "45"))
OCR_TOTAL_TIMEOUT_SECONDS = int(os.getenv("OCR_TOTAL_TIMEOUT_SECONDS", "180"))
PROVIDER_TIMEOUT_SECONDS = int(os.getenv("PROVIDER_TIMEOUT_SECONDS", "60"))
EXTRACTION_QUEUE_TIMEOUT_SECONDS = max(0.1, float(os.getenv("EXTRACTION_QUEUE_TIMEOUT_SECONDS", "30")))
API_METRICS_FILE = os.getenv("API_METRICS_FILE", "/tmp/api-pdf-metrics.jsonl")
EXTRACTION_SEMAPHORE = asyncio.Semaphore(max(1, int(os.getenv("MAX_CONCURRENT_EXTRACTIONS", "2"))))
METRICS_LOCK = threading.Lock()
QSTASH_TOKEN = os.getenv("QSTASH_TOKEN", "")
QSTASH_QUEUE_NAME = os.getenv("QSTASH_QUEUE_NAME", "api-pdf-extractions")
QSTASH_WORKER_URL = os.getenv("QSTASH_WORKER_URL", "")
QSTASH_FAILURE_CALLBACK_URL = os.getenv(
    "QSTASH_FAILURE_CALLBACK_URL",
    QSTASH_WORKER_URL.rsplit("/", 1)[0] + "/failures" if QSTASH_WORKER_URL else "",
)
JOB_WAIT_SECONDS = max(0, float(os.getenv("JOB_WAIT_SECONDS", "25")))


@asynccontextmanager
async def extraction_slot():
    """Wait briefly for capacity; report an explicit retryable busy response if full."""
    try:
        await asyncio.wait_for(
            EXTRACTION_SEMAPHORE.acquire(), timeout=EXTRACTION_QUEUE_TIMEOUT_SECONDS
        )
    except TimeoutError as exc:
        raise HTTPException(
            status_code=503,
            detail="API ocupada procesando otros documentos; reintente en unos segundos.",
            headers={"Retry-After": "10"},
        ) from exc
    try:
        yield
    finally:
        EXTRACTION_SEMAPHORE.release()


class ExtractionResponse(BaseModel):
    request_id: str
    document_type: str
    document_subtype: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    raw_text: str
    confidence: float | None = None
    warnings: list[str] = Field(default_factory=list)
    source: dict[str, Any]
    schema_version: str = "1.0"
    extraction: dict[str, Any] = Field(default_factory=dict)


class ExtractionJobResponse(BaseModel):
    job_id: str
    status: Literal["queued", "processing", "completed", "failed"]
    status_url: str
    result: ExtractionResponse | None = None
    error: str | None = None


async def require_bearer(authorization: str | None = Header(default=None)) -> None:
    expected = os.getenv("API_BEARER_TOKEN", "")
    if not expected:
        raise HTTPException(503, "API bearer token is not configured")
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token, expected):
        raise HTTPException(401, "Invalid bearer token")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def _record_metrics(response: ExtractionResponse) -> None:
    """Append one metadata-only JSONL record; never persist the extracted text."""
    raw_text_bytes = response.raw_text.encode("utf-8")
    record = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "request_id": response.request_id,
        "filename": response.source.get("filename"),
        "document_type": response.document_type,
        "pages": response.source.get("pages"),
        "ocr_pages": response.extraction.get("ocr_pages", 0),
        "ocr_failures": response.extraction.get("ocr_failures", 0),
        "extracted_text_chars": len(response.raw_text),
        "extracted_text_utf8_bytes": len(raw_text_bytes),
        "provider": response.extraction.get("provider"),
        "model": response.extraction.get("model"),
        "duration_ms": response.extraction.get("duration_ms"),
        "warning_count": len(response.warnings),
    }
    try:
        directory = os.path.dirname(API_METRICS_FILE) or "."
        os.makedirs(directory, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with METRICS_LOCK, open(API_METRICS_FILE, "a", encoding="utf-8") as metrics_file:
            metrics_file.write(line)
            metrics_file.flush()
    except OSError as exc:
        logger.warning(
            "metrics_write_failed request_id=%s error=%s",
            response.request_id, type(exc).__name__,
        )


def _infer_invoice_total(data: dict[str, Any], raw_text: str) -> bool:
    """Infer a missing total only when OCR explicitly supports net + VAT arithmetic."""
    if data.get("amount") not in (None, ""):
        return False

    net = data.get("net_amount")
    tax = data.get("tax_amount")
    if isinstance(net, bool) or isinstance(tax, bool):
        return False
    if not isinstance(net, (int, float)) or not isinstance(tax, (int, float)):
        return False
    if not math.isfinite(net) or not math.isfinite(tax) or net < 0 or tax < 0:
        return False

    text = raw_text.upper()
    if not re.search(r"\b(?:MONTO\s+NETO|NETO)\b", text) or not re.search(r"\bIVA\b", text):
        return False
    if re.search(r"\bEXENTO\b|IMPUESTO\s+ADICIONAL", text):
        return False

    for key in ("discount_amount", "exempt_amount", "additional_tax_amount", "other_charges"):
        value = data.get(key)
        if isinstance(value, bool):
            return False
        if isinstance(value, (int, float)) and (not math.isfinite(value) or value != 0):
            return False

    total = net + tax
    data["amount"] = int(total) if float(total).is_integer() else total
    return True


def _extract_pdf_text(content: bytes) -> tuple[str, int, int, int]:
    """Read embedded text and OCR pages without enough selectable text."""
    try:
        pdf = fitz.open(stream=content, filetype="pdf")
    except Exception as exc:
        raise HTTPException(400, "Invalid or unreadable PDF") from exc
    with pdf:
        if pdf.is_encrypted:
            raise HTTPException(422, "Encrypted PDFs are not supported")
        page_count = pdf.page_count
        if page_count < 1:
            raise HTTPException(422, "PDF has no pages")
        if page_count > MAX_PAGES:
            raise HTTPException(413, f"PDF exceeds the {MAX_PAGES}-page limit")

        pages: list[str] = []
        ocr_pages = 0
        ocr_failures = 0
        ocr_deadline = time.monotonic() + OCR_TOTAL_TIMEOUT_SECONDS
        for page_number, page in enumerate(pdf, start=1):
            text = page.get_text("text").strip()
            has_images = bool(page.get_images(full=True))
            if has_images or len(re.sub(r"\s", "", text)) < OCR_MIN_CHARS_PER_PAGE:
                try:
                    import pytesseract
                    from PIL import Image

                    remaining = ocr_deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Total OCR timeout exceeded")

                    pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                    image = Image.frombytes("RGB", [pixmap.width, pixmap.height], pixmap.samples)
                    ocr_text = pytesseract.image_to_string(
                        image, lang=OCR_LANG, timeout=max(1, min(OCR_TIMEOUT_SECONDS, int(remaining)))
                    ).strip()
                    ocr_pages += 1
                    if ocr_text:
                        text = f"{text}\n{ocr_text}".strip()
                except Exception as exc:
                    ocr_failures += 1
                    logger.warning("OCR failed on page %s: %s", page_number, type(exc).__name__)
                    text = text or ""
            pages.append(f"--- Página {page_number} ---\n{text}".rstrip())
    return "\n\n".join(pages), page_count, ocr_pages, ocr_failures


@app.post("/v1/extractions")
async def extract_document(
    _: None = Depends(require_bearer),
    file: UploadFile = File(...),
    document_type: Literal["auto", "invoice", "quote", "transport_document"] = Form("auto"),
    locale: str = Form("es-CL"),
    wait: bool = Form(True),
) -> Any:
    request_id = str(uuid.uuid4())
    content = await file.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise HTTPException(413, "File exceeds upload limit")
    if not content.startswith(b"%PDF-"):
        raise HTTPException(415, "Only PDF files are supported")
    filename = (file.filename or "document.pdf").replace("\\", "/").rsplit("/", 1)[-1][:255]
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(415, "File must have a .pdf extension")
    if file.content_type not in (None, "", "application/pdf", "application/octet-stream"):
        raise HTTPException(415, "Content-Type must be application/pdf")

    qstash_requested = any((QSTASH_TOKEN, QSTASH_WORKER_URL, os.getenv("QSTASH_CURRENT_SIGNING_KEY", ""), os.getenv("QSTASH_NEXT_SIGNING_KEY", "")))
    queue_enabled = bool(
        QSTASH_TOKEN and QSTASH_WORKER_URL and QSTASH_FAILURE_CALLBACK_URL
        and os.getenv("QSTASH_CURRENT_SIGNING_KEY") and os.getenv("QSTASH_NEXT_SIGNING_KEY")
    )
    if qstash_requested and not queue_enabled:
        raise HTTPException(503, "QStash configuration is incomplete")
    if queue_enabled or not wait:
        if not queue_enabled:
            raise HTTPException(503, "Async jobs require QSTASH_TOKEN and QSTASH_WORKER_URL")
        try:
            await asyncio.to_thread(jobs.create_job, request_id, filename, document_type, locale, content)
            await asyncio.to_thread(_enqueue_job, request_id)
        except Exception as exc:
            await asyncio.to_thread(jobs.delete_job, request_id)
            logger.exception("Could not enqueue extraction job job_id=%s", request_id)
            raise HTTPException(503, "No se pudo encolar el documento. Reintenta la solicitud.") from exc
        if wait and JOB_WAIT_SECONDS:
            deadline = time.monotonic() + JOB_WAIT_SECONDS
            while time.monotonic() < deadline:
                job = await asyncio.to_thread(jobs.get_job, request_id)
                if job and job["status"] in {"completed", "failed"}:
                    return _job_response(job)
                await asyncio.sleep(0.4)
        return JSONResponse(status_code=202, content=_job_response(await asyncio.to_thread(jobs.get_job, request_id)).model_dump(mode="json"))

    return await _process_extraction(request_id, filename, content, document_type, locale)


def _enqueue_job(job_id: str) -> None:
    from qstash import QStash

    client = QStash(QSTASH_TOKEN, base_url=os.getenv("QSTASH_URL") or None)
    client.message.enqueue_json(
        queue=QSTASH_QUEUE_NAME,
        url=QSTASH_WORKER_URL,
        body={"job_id": job_id},
        retries=int(os.getenv("QSTASH_RETRIES", "3")),
        timeout=os.getenv("QSTASH_DELIVERY_TIMEOUT", "300s"),
        failure_callback=QSTASH_FAILURE_CALLBACK_URL,
    )


def _job_response(job: dict[str, Any]) -> ExtractionJobResponse:
    return ExtractionJobResponse(
        job_id=job["job_id"],
        status=job["status"],
        status_url=f"/v1/extractions/{job['job_id']}",
        result=ExtractionResponse.model_validate(job["result"]) if job.get("result") else None,
        error=job.get("error"),
    )


@app.get("/v1/extractions/{job_id}", response_model=ExtractionJobResponse)
async def get_extraction_job(job_id: str, _: None = Depends(require_bearer)) -> ExtractionJobResponse:
    job = await asyncio.to_thread(jobs.get_job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return _job_response(job)


async def _verify_qstash(request: Request, body: bytes) -> None:
    current = os.getenv("QSTASH_CURRENT_SIGNING_KEY", "")
    next_key = os.getenv("QSTASH_NEXT_SIGNING_KEY", "")
    signature = request.headers.get("Upstash-Signature", "")
    if not current or not next_key or not signature or not QSTASH_WORKER_URL:
        raise HTTPException(503, "QStash signature verification is not configured")
    try:
        from qstash import Receiver

        receiver = Receiver(current_signing_key=current, next_signing_key=next_key)
        valid = await asyncio.to_thread(
            receiver.verify, body=body.decode("utf-8"), signature=signature,
            url=QSTASH_WORKER_URL,
        )
    except Exception as exc:
        logger.warning("QStash signature verification failed error=%s", type(exc).__name__)
        valid = False
    if not valid:
        raise HTTPException(401, "Invalid QStash signature")


@app.post("/internal/qstash/extractions", include_in_schema=False)
async def process_queued_extraction(request: Request) -> dict[str, str]:
    body = await request.body()
    await _verify_qstash(request, body)
    try:
        message = json.loads(body)
        job_id = str(uuid.UUID(message["job_id"]))
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "Invalid job message") from exc
    job = await asyncio.to_thread(jobs.get_job, job_id, include_content=True)
    if job is None:
        return {"status": "missing"}
    if job["status"] == "completed":
        return {"status": "completed"}
    await asyncio.to_thread(jobs.set_job_status, job_id, "processing")
    try:
        response = await _process_extraction(
            job_id, job["filename"], job["content"], job["document_type"], job["locale"]
        )
        await asyncio.to_thread(
            jobs.set_job_status, job_id, "completed", result=response.model_dump(mode="json")
        )
        await asyncio.to_thread(jobs.discard_job_content, job_id)
    except HTTPException as exc:
        if exc.status_code < 500:
            await asyncio.to_thread(jobs.set_job_status, job_id, "failed", error=str(exc.detail))
            await asyncio.to_thread(jobs.discard_job_content, job_id)
            return {"status": "failed"}
        # A non-2xx response tells QStash to retry transient processing failures.
        await asyncio.to_thread(jobs.set_job_status, job_id, "queued", error=str(exc.detail))
        raise
    except Exception as exc:
        await asyncio.to_thread(jobs.set_job_status, job_id, "queued", error="Processing failed")
        logger.exception("Queued extraction failed job_id=%s", job_id)
        raise HTTPException(503, "Processing failed; QStash will retry") from exc
    return {"status": "completed"}


@app.post("/internal/qstash/failures", include_in_schema=False)
async def qstash_failure_callback(request: Request) -> dict[str, str]:
    body = await request.body()
    current = os.getenv("QSTASH_CURRENT_SIGNING_KEY", "")
    next_key = os.getenv("QSTASH_NEXT_SIGNING_KEY", "")
    signature = request.headers.get("Upstash-Signature", "")
    if not current or not next_key or not signature or not QSTASH_FAILURE_CALLBACK_URL:
        raise HTTPException(503, "QStash signature verification is not configured")
    try:
        from qstash import Receiver

        receiver = Receiver(current_signing_key=current, next_signing_key=next_key)
        valid = await asyncio.to_thread(
            receiver.verify, body=body.decode("utf-8"), signature=signature,
            url=QSTASH_FAILURE_CALLBACK_URL,
        )
        callback = json.loads(body)
        source = json.loads(base64.b64decode(callback["sourceBody"], validate=True))
        job_id = str(uuid.UUID(source["job_id"]))
    except Exception as exc:
        logger.warning("Invalid QStash failure callback error=%s", type(exc).__name__)
        raise HTTPException(401, "Invalid QStash failure callback") from exc
    if not valid:
        raise HTTPException(401, "Invalid QStash failure callback signature")
    job = await asyncio.to_thread(jobs.get_job, job_id)
    if job and job["status"] != "completed":
        await asyncio.to_thread(jobs.set_job_status, job_id, "failed", error="QStash exhausted delivery retries")
        await asyncio.to_thread(jobs.discard_job_content, job_id)
    return {"status": "recorded"}


async def _process_extraction(
    request_id: str, filename: str, content: bytes, document_type: str, locale: str
) -> ExtractionResponse:
    async with extraction_slot():
        started = asyncio.get_running_loop().time()
        raw_text, page_count, ocr_pages, ocr_failures = await asyncio.to_thread(_extract_pdf_text, content)
        warnings: list[str] = []
        if not raw_text.strip():
            raise HTTPException(422, "No se pudo extraer texto del PDF; revisa el archivo y vuelve a intentarlo.")
        if ocr_failures:
            warnings.append(f"OCR falló en {ocr_failures} página(s); revisar la extracción.")
        if len(raw_text) > int(os.getenv("MAX_PROVIDER_TEXT_CHARS", "100000")):
            warnings.append("El proveedor recibió solo una parte del texto por el límite de contexto; raw_text contiene el texto completo extraído.")
        try:
            result, provider_name, provider_model = await asyncio.wait_for(
                extract_with_provider(raw_text, document_type=document_type, locale=locale),
                timeout=PROVIDER_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.warning("Provider extraction failed request_id=%s error=%s", request_id, type(exc).__name__)
            raise HTTPException(
                status_code=503,
                detail="No se pudo procesar el documento con los modelos disponibles. Reintenta la solicitud.",
                headers={"Retry-After": "10"},
            ) from exc

    kind = result.get("document_type")
    if kind not in {"invoice", "receipt", "quote", "transport_document", "other"}:
        logger.error("Provider returned unsupported classification request_id=%s type=%s", request_id, kind)
        raise HTTPException(
            status_code=503,
            detail="No se pudo clasificar el documento con los modelos disponibles. Reintenta la solicitud.",
            headers={"Retry-After": "10"},
        )
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    if kind == "invoice":
        if _infer_invoice_total(data, raw_text):
            warnings.append("El total no se distinguió en el OCR; se calculó como monto neto + IVA y requiere revisión.")
        required = {
            "invoice_number": "número de factura",
            "customer_rut": "RUT del cliente",
            "supplier_rut": "RUT del proveedor",
            "date": "fecha",
            "amount": "monto",
        }
        for key in required:
            data.setdefault(key, None)
        missing = [label for key, label in required.items() if data.get(key) in (None, "")]
        if missing:
            warnings.append("Campos obligatorios de factura no identificados: " + ", ".join(missing) + ".")
    warnings.extend(item for item in result.get("warnings", []) if isinstance(item, str))
    confidence = result.get("confidence")
    try:
        confidence = float(confidence) if confidence is not None else None
        if confidence is not None and not 0 <= confidence <= 1:
            confidence = None
    except (TypeError, ValueError):
        confidence = None
    response = ExtractionResponse(
        request_id=request_id,
        document_type=kind,
        document_subtype=result.get("document_subtype"),
        data=data,
        raw_text=raw_text,
        confidence=confidence,
        warnings=warnings,
        source={
            "filename": filename,
            "mime_type": "application/pdf",
            "sha256": hashlib.sha256(content).hexdigest(),
            "pages": page_count,
        },
        extraction={
            "ocr_pages": ocr_pages,
            "ocr_failures": ocr_failures,
            "provider": provider_name,
            "model": provider_model,
            "duration_ms": round((asyncio.get_running_loop().time() - started) * 1000),
        },
    )
    logger.info(
        "extraction_complete request_id=%s sha256=%s pages=%s ocr_pages=%s provider=%s model=%s",
        request_id, response.source["sha256"], page_count, ocr_pages,
        provider_name or "none", provider_model or "none",
    )
    _record_metrics(response)
    return response
