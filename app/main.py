"""
FastAPI application: single-file-upload PII redaction service.

Run locally:
    uvicorn app.main:app --reload --port 8000

Then open http://localhost:8000
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.api.routes import router as api_router

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


class NoCacheStaticFiles(StaticFiles):
    """
    Serves the frontend with revalidation forced on every request.

    Browsers will otherwise heuristically cache `/static/script.js` and
    `/static/style.css` and keep serving a stale copy long after the file on
    disk has changed -- which looks exactly like "my fix did nothing", and
    is very hard to tell apart from a real bug. For a handful of small local
    assets the revalidation round-trip costs nothing, and always shipping
    the current file is worth far more than the saved bytes.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response

app = FastAPI(
    title="PII Redaction Tool",
    description="Upload a document, get back a PII-redacted .docx",
    version="1.0.0",
)

# The frontend may be hosted on a different origin than the API (e.g. a
# static frontend on Netlify calling a Render backend), so cross-origin
# requests are allowed. Access is gated by the shared-password token in the
# Authorization header rather than by origin -- and because auth travels in
# a header rather than a cookie, permitting any origin does not expose an
# authenticated session to a third-party page.
#
# `expose_headers` is required: without it the browser hides
# X-Redaction-Summary from cross-origin JavaScript, and the UI silently
# loses the redaction breakdown even though the request succeeded.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Redaction-Summary", "Content-Disposition"],
)

app.include_router(api_router, prefix="/api")


@app.get("/")
def serve_index():
    return FileResponse(
        FRONTEND_DIR / "index.html",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


app.mount("/static", NoCacheStaticFiles(directory=str(FRONTEND_DIR)), name="static")
