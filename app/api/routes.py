"""
API routes.

POST /api/redact
    multipart/form-data:
      file                    - the document to redact (.docx, .pdf, or .txt), max 10 MB
      issuer_names (optional) - comma-separated list of the subject company's own
                                 name/aliases to KEEP unredacted
      redact_issuer (optional)- "true" to redact the issuer/subject company too
      disable_types (optional)- comma-separated PII types to turn off
      verify (optional)       - "false" to skip the residual scan (roughly halves
                                 processing time; recommended on CPU-constrained
                                 hosting). Defaults to "true".
    -> returns the redacted .docx as a file download, with a JSON summary
       of what was redacted in the `X-Redaction-Summary` response header.

GET /api/health
    -> liveness check for the hosting platform.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from collections import Counter

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from app.api import auth
from app.core.detectors.base import ALL_PII_TYPES, DetectionConfig
from app.core.document_io import redact_file
from app.core.redactor import Redactor

router = APIRouter()


class LoginRequest(BaseModel):
    password: str

MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB, per assignment requirement
# Read the upload in chunks and abort as soon as the running total exceeds
# the limit, rather than buffering the whole body first and only then
# checking its length -- a client sending far more than 10 MB (by mistake or
# on purpose) would otherwise sit fully in memory before being rejected.
_UPLOAD_CHUNK_BYTES = 1024 * 1024  # 1 MB
SUPPORTED_EXTENSIONS = {".docx", ".pdf", ".txt"}
ALL_TYPES = ALL_PII_TYPES

# Redaction is CPU-bound (spaCy NER dominates) and this process has a fixed
# number of cores; letting an unbounded number of large documents run at
# once doesn't parallelize the work for free, it just makes every one of
# them slower and risks the box falling over under a burst of uploads. This
# caps how many redaction jobs run at once -- additional requests wait
# briefly for a slot and get a clear 503 rather than an indefinite hang if
# one doesn't free up.
MAX_CONCURRENT_JOBS = int(os.environ.get("PII_MAX_CONCURRENT_JOBS", "3"))
_JOB_WAIT_TIMEOUT_SECONDS = 5
_job_semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)


@router.get("/health")
def health():
    """Liveness probe — deliberately unauthenticated so the hosting
    platform can health-check without the password."""
    return {"status": "ok"}


@router.get("/auth/status")
def auth_status():
    """Lets the frontend know whether to show the login screen at all."""
    return {"auth_required": auth.auth_configured()}


@router.post("/auth/login")
def login(payload: LoginRequest, request: Request):
    token = auth.login(payload.password, auth.client_ip(request))
    return {"token": token, "expires_in": auth.SESSION_TTL_SECONDS}


@router.post("/redact", dependencies=[Depends(auth.require_auth)])
async def redact_endpoint(
    request: Request,
    file: UploadFile = File(...),
    issuer_names: str = Form(""),
    redact_issuer: str = Form("false"),
    disable_types: str = Form(""),
    verify: str = Form("true"),
):
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext}'. Supported: {sorted(SUPPORTED_EXTENSIONS)}",
        )

    # Fast-fail on the declared size before reading a single byte. This is
    # a heuristic, not the authoritative check -- Content-Length covers the
    # whole multipart body (other form fields, boundary overhead), and a
    # client can lie about it entirely -- but it rejects the obvious case
    # (someone attaching a 500 MB file) without touching the body at all.
    declared_length = request.headers.get("content-length")
    if declared_length is not None:
        try:
            if int(declared_length) > MAX_FILE_SIZE_BYTES * 2:
                raise HTTPException(
                    status_code=413,
                    detail=f"Request too large. Max file size is "
                           f"{MAX_FILE_SIZE_BYTES / 1_000_000:.0f} MB.",
                )
        except ValueError:
            pass  # malformed header; fall through to the authoritative check below

    # The authoritative check: read in bounded chunks and abort as soon as
    # the running total exceeds the limit, rather than buffering the entire
    # body first (`await file.read()`) and only checking its length
    # afterward -- which would hold an arbitrarily large upload fully in
    # memory before rejecting it.
    chunks = []
    total = 0
    while True:
        chunk = await file.read(_UPLOAD_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_FILE_SIZE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"File too large (over {MAX_FILE_SIZE_BYTES / 1_000_000:.0f} MB). "
                       f"Max allowed is {MAX_FILE_SIZE_BYTES / 1_000_000:.0f} MB.",
            )
        chunks.append(chunk)
    contents = b"".join(chunks)
    if len(contents) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    disabled = {t.strip().upper() for t in disable_types.split(",") if t.strip()}
    unknown = disabled - ALL_TYPES
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown PII type(s): {sorted(unknown)}")

    issuers = {n.strip() for n in issuer_names.split(",") if n.strip()}
    config = DetectionConfig(
        enabled_types=ALL_TYPES - disabled,
        redact_issuer_company=(redact_issuer.lower() == "true"),
    )
    redactor = Redactor(config=config, issuer_names=issuers)

    work_dir = tempfile.mkdtemp(prefix="pii_redact_")
    input_path = os.path.join(work_dir, f"input{ext}")
    output_name = f"redacted_{uuid.uuid4().hex[:8]}.docx"
    output_path = os.path.join(work_dir, output_name)

    with open(input_path, "wb") as f:
        f.write(contents)

    # Bound how many redaction jobs run at once (see MAX_CONCURRENT_JOBS
    # above). A short wait for a slot is normal under load; past that,
    # telling the client to retry shortly is more honest than either
    # queueing indefinitely or letting the box run N unbounded CPU-bound
    # jobs at once.
    try:
        await asyncio.wait_for(_job_semaphore.acquire(), timeout=_JOB_WAIT_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        _cleanup(work_dir)
        raise HTTPException(
            status_code=503,
            detail="Server is busy processing other redaction jobs. Please try again shortly.",
        )

    verify_bool = verify.strip().lower() not in ("false", "0", "no")

    try:
        # Redaction is CPU-bound and runs for tens of seconds on a large
        # document. Calling it directly inside an async endpoint would block
        # the event loop and freeze the server for every other request for
        # the whole job, so hand it to a worker thread.
        #
        # Verification (see document_io.redact_file / verification.py)
        # re-runs detection over the whole finished document as a second
        # pass, which roughly doubles wall-clock time -- a fine trade on a
        # full-power dev machine, a much rougher one on constrained hosting
        # (e.g. Render's free tier, where a heavily-throttled shared CPU can
        # turn an 80s job into several minutes). Defaults to on; callers
        # that need a faster turnaround on a large document can pass
        # `verify=false` and rely on the primary detection pass alone.
        result = await run_in_threadpool(
            redact_file, input_path, output_path, redactor, verify_bool
        )
    except Exception as e:
        _cleanup(work_dir)
        raise HTTPException(status_code=422, detail=f"Could not process file: {e}")
    finally:
        _job_semaphore.release()

    counts = Counter(r["type"] for r in result.replacements)
    tier_counts = Counter(r.get("confidence_tier", "needs_review") for r in result.replacements)
    summary = {
        "total_redacted": len(result.replacements),
        "by_type": dict(counts),
        "warnings": result.warnings,
        "confidence": {
            "high": tier_counts.get("high", 0),
            "medium": tier_counts.get("medium", 0),
            "needs_review": tier_counts.get("needs_review", 0),
        },
        # `.summary()` is deliberately the ONLY thing sent here: counts and
        # types, never the actual leaked/matched text. A PII-redaction
        # service echoing potentially-sensitive strings back in an API
        # response would undermine the entire point of the tool -- see
        # `ResidualScanReport.summary()` in verification.py.
        "residual_scan": result.residual.summary() if result.residual else None,
    }

    return FileResponse(
        path=output_path,
        filename=output_name,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"X-Redaction-Summary": json.dumps(summary)},
        background=BackgroundTask(_cleanup, work_dir),
    )


def _cleanup(work_dir: str):
    import shutil
    shutil.rmtree(work_dir, ignore_errors=True)
