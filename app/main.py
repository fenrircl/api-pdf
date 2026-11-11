from __future__ import annotations

import hmac
import os
from typing import Literal

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel

app = FastAPI(title="api-pdf", version="0.1.0")
MAX_BYTES = int(os.getenv("MAX_UPLOAD_MB", "20")) * 1024 * 1024


class ExtractionResponse(BaseModel):
    request_id: str
    document_type: str
    document_subtype: str | None = None
    data: dict
    confidence: float | None = None
    warnings: list[str] = []
    schema_version: str = "1.0"
    source: dict


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


@app.post("/v1/extractions", response_model=ExtractionResponse)
async def extract_document(
    _: None = Depends(require_bearer),
    file: UploadFile = File(...),
    document_type: Literal["auto", "invoice", "quote", "transport_document"] = Form("auto"),
    locale: str = Form("es-CL"),
) -> ExtractionResponse:
    content = await file.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise HTTPException(413, "File exceeds upload limit")
    # Fail closed until a provider adapter and typed document schemas are configured.
    if not content.startswith(b"%PDF-"):
        raise HTTPException(415, "Only PDF files are supported in this initial skeleton")
    raise HTTPException(501, "Extraction provider is not implemented yet")
