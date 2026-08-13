# API Reference

Base URL is the deployed service root; locally `http://localhost:8000`.

All endpoints live under `/api`. The web UI at `/` is a client of exactly
this API — there are no private endpoints.

---

## Authentication

Authentication is **enabled by the presence of the `PII_APP_PASSWORD`
environment variable**. If it is unset, the gate is disabled entirely and
`/api/redact` is open — convenient for local use and the CLI, unsafe for a
public URL.

```bash
export PII_APP_PASSWORD="choose-a-password"
```

The flow is: post the password once, get an opaque session token, send that
token as a bearer credential on subsequent requests.

```
POST /api/auth/login   {"password": "…"}   ─►   {"token": "…"}
POST /api/redact       Authorization: Bearer <token>
```

| Property | Value |
|---|---|
| Token lifetime | 12 hours |
| Token storage (server) | in memory — a restart invalidates all sessions |
| Token storage (browser) | `sessionStorage`, cleared when the tab closes |
| Password comparison | `secrets.compare_digest` (constant time) |
| Rate limit | 8 failed attempts per IP per 5 minutes |

---

## `GET /api/health`

Liveness probe. **Never authenticated**, so hosting platforms can health-check
without the password.

```json
{ "status": "ok" }
```

---

## `GET /api/auth/status`

Whether this deployment requires a password. The frontend calls this on load
to decide whether to show the login screen.

```json
{ "auth_required": true }
```

---

## `POST /api/auth/login`

Exchanges the shared password for a session token.

**Request** — `application/json`

```json
{ "password": "choose-a-password" }
```

**Response** — `200 OK`

```json
{ "token": "0FZ8…opaque…", "expires_in": 43200 }
```

**Errors**

| Status | Meaning |
|---|---|
| `401` | Incorrect password |
| `429` | Too many failed attempts from this IP; wait and retry |

---

## `POST /api/redact`

Redacts one document and returns the redacted `.docx`.

**Authentication:** required when `PII_APP_PASSWORD` is set.

**Request** — `multipart/form-data`

| Field | Type | Default | Description |
|---|---|---|---|
| `file` | file | *required* | `.docx`, `.pdf` or `.txt`, max 10 MB |
| `issuer_names` | string | `""` | Comma-separated company names to **keep** unredacted |
| `redact_issuer` | string | `"false"` | `"true"` redacts the subject company's own name too |
| `disable_types` | string | `""` | Comma-separated PII types to switch off |

Valid `disable_types` values (18 total): `PERSON`, `EMAIL`, `PHONE`, `ORG`,
`ADDRESS`, `SSN`, `CREDIT_CARD`, `DATE_OF_BIRTH`, `IP_ADDRESS`, `PAN_NUMBER`,
`AADHAAR_NUMBER`, `IFSC_CODE`, `GSTIN`, `UPI_ID`, `PASSPORT_NUMBER`,
`VOTER_ID`, `DRIVING_LICENSE`, `BANK_ACCOUNT_NUMBER`.

The single source of truth is `ALL_PII_TYPES` in
`app/core/detectors/base.py`; the CLI, this API, the web UI and both
evaluation harnesses all read it rather than keeping their own copy.

**Response** — `200 OK`

The body is the redacted `.docx` as a file download
(`application/vnd.openxmlformats-officedocument.wordprocessingml.document`).

A JSON summary rides along in the **`X-Redaction-Summary`** header:

```json
{ "total_redacted": 1035,
  "by_type": { "ORG": 624, "PERSON": 251, "EMAIL": 52, "PHONE": 36, "ADDRESS": 72 } }
```

> This header is listed in the CORS `expose_headers` allowlist. Without that,
> browsers hide it from cross-origin JavaScript and the summary silently
> disappears even though the request succeeded.

**Errors**

| Status | Meaning |
|---|---|
| `400` | Unsupported file extension, or the file is empty |
| `400` | Unknown PII type in `disable_types` |
| `401` | Missing or invalid token (when auth is enabled) |
| `413` | File exceeds 10 MB |
| `422` | The file could not be parsed (corrupt or password-protected) |

---

## Examples

### curl — no authentication

```bash
curl -X POST http://localhost:8000/api/redact \
  -F 'file=@"Red Herring Prospectus.docx"' \
  -F 'issuer_names=KSH International Limited,KSH International' \
  -D headers.txt -o redacted.docx

grep -i x-redaction-summary headers.txt
```

### curl — with authentication

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"password":"choose-a-password"}' | python -c 'import sys,json;print(json.load(sys.stdin)["token"])')

curl -X POST http://localhost:8000/api/redact \
  -H "Authorization: Bearer $TOKEN" \
  -F 'file=@filing.docx' \
  -F 'disable_types=ORG,ADDRESS' \
  -o redacted.docx
```

### Python

```python
import requests

base = "http://localhost:8000"
token = requests.post(f"{base}/api/auth/login",
                      json={"password": "choose-a-password"}).json()["token"]

with open("filing.docx", "rb") as fh:
    r = requests.post(
        f"{base}/api/redact",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": fh},
        data={"issuer_names": "KSH International Limited"},
    )
r.raise_for_status()

print(r.headers["X-Redaction-Summary"])
open("redacted.docx", "wb").write(r.content)
```

---

## Using the engine directly

The API is a thin wrapper. For batch work, skip HTTP entirely:

```python
from app.core.detectors.base import DetectionConfig
from app.core.document_io import redact_file
from app.core.redactor import Redactor

redactor = Redactor(
    config=DetectionConfig(enabled_types={"PERSON", "EMAIL", "PHONE"}),
    issuer_names={"KSH International Limited"},
    seed=42,                      # reproducible fake values
)

result = redact_file("filing.docx", "redacted.docx", redactor)

print(len(result.replacements))
for r in result.replacements[:5]:
    print(r["type"], r["original"], "→", r["fake"])
```

`redact_file` dispatches on the input extension and always writes a `.docx`.
The document's glossary is parsed automatically for `.docx` input; pass
`defined_terms=` to `Redactor` to supply your own stoplist instead.

### Audit log

`result.replacements` is the audit trail, in document order:

```json
{ "type": "PERSON",
  "original": "Kushal Subbayya Hegde",
  "fake": "Aryan Maharaj",
  "confidence": 0.85,
  "source": "ner" }
```

`source` is one of `regex` (pattern + verification), `ner` (spaCy),
`heuristic` (structural rule such as the address lead-in or contact-block
name), or `consistency_sweep` (a casing variant matched to a name confidently
detected elsewhere in the document).

The CLI writes this to a file with `--audit-log`.

---

## Operational notes

- **Processing time** scales with document length — roughly 25–30 s for a
  1.8 MB, 692-paragraph filing. Requests are handled in a worker thread, so a
  long job does not block other requests, but the client must be prepared to
  wait. Set proxy and client timeouts accordingly.
- **Cold start** on free-tier hosting adds a few seconds while the spaCy model
  loads on first use.
- **Nothing is persisted.** Uploads are written to a temp directory and
  deleted immediately after the response is sent.
