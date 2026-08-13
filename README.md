# PII Redaction Tool

Redacts personally identifiable information from documents (`.docx`, `.pdf`,
`.txt` in → `.docx` out), replacing every piece of PII with a **realistic fake
value** — consistently, so the same person, company or number reads as the
same fake value everywhere it appears. Ships as a CLI, a REST API, and a
password-protected web UI.

Built for and evaluated against a real 1.8 MB Indian IPO filing (a Red
Herring Prospectus: 692 paragraphs, 76 tables, 1,006 body elements).

## Summary

**Approach:** a hybrid engine — regex **plus real validation** (Luhn
checksum, libphonenumber parsing, SSN area rules, IFSC/GSTIN structural
checks) for structured PII, and **spaCy NER** (`en_core_web_sm`) for
unstructured PII (names, organisations, addresses), with a stoplist parsed
from the document's own "Definitions" glossary so legal terms like "Equity
Shares" aren't mistaken for a company. Fake replacements are generated with
**Faker**, cached per `(type, original value)` so the same person, company or
number reads as the same fake everywhere; `python-docx` preserves formatting
run-by-run, and `pdfplumber` handles PDF input.

**Tradeoffs and known false positives/negatives** (measured, not guessed —
see `EVALUATION.md` §3 for every individual error): the largest **false
negative** is a single-word brand name with no corporate suffix (e.g.
"CareEdge") — nothing structurally separates it from an ordinary capitalised
noun, and a broader sweep was tried and produced far more false positives
than it fixed; an address with neither a PIN code nor a lead-in phrase is
also missed. The main **false positive** source is spaCy occasionally tagging
institutional/legal phrasing as an organisation ("ICDR Master Circular") —
ordinary NER imprecision on formal prose, never personal data. Reference
numbers (order/ticket/invoice, ISIN, CIN, SEBI registration numbers) are
**deliberately not redacted** — they identify a transaction or filing, not a
person — and the filing's own subject company name is kept by default, since
a prospectus is inherently about that company.

Full methodology and per-type numbers are in [`EVALUATION.md`](EVALUATION.md);
the architecture and every design decision are in [`ARCHITECTURE.md`](ARCHITECTURE.md).

```bash
python cli.py --input "Red Herring Prospectus.docx" \
              --output "output/Red Herring Prospectus_redacted.docx" \
              --issuer "KSH International Limited" "KSH International" \
              --audit-log output/redaction_audit_log.json
```

**Deliverables in this repo:** `output/Red Herring Prospectus_redacted.docx`
(the redacted filing) and `output/redaction_audit_log.json` (every
original → fake replacement, with confidence tiers and the residual-scan
result).

---

## Contents

- [Headline results](#headline-results)
- [What it detects](#what-it-detects)
- [How it works](#how-it-works)
- [Where it looks — hidden content](#where-it-looks--hidden-content)
- [It checks its own work](#it-checks-its-own-work)
- [Confidence tiers](#confidence-tiers)
- [Explicit design choices](#explicit-design-choices)
- [Known limitations](#known-limitations)
- [Running it](#running-it)
- [Web app and security](#web-app-and-security)
- [Project layout](#project-layout)
- [Extending it](#extending-it)
- [Testing](#testing)
- [Deployment](#deployment)

### Other documents

| Document | What's in it |
|---|---|
| [**EVALUATION.md**](EVALUATION.md) | Methodology, precision/recall/F1, and every individual error |
| [**ARCHITECTURE.md**](ARCHITECTURE.md) | How it's built and *why* — data flow, each design decision, the bugs that shaped them |
| [**API.md**](API.md) | REST endpoint reference, auth flow, and using the engine as a library |

---

## Headline results

Measured two ways — against the real filing, and against a synthetic corpus
covering the PII types the real filing happens not to contain. Full
methodology and every individual error are in [`EVALUATION.md`](EVALUATION.md).

| | Precision | Recall | F1 |
|---|---|---|---|
| **Real prospectus** (36 sampled blocks, 37 hand-labelled spans) | **0.947** | **0.973** | **0.960** |
| Synthetic corpus (33 labelled spans, all core types) | 0.912 | 0.939 | 0.925 |

Every **structured** type — where a validated pattern applies (Luhn checksum,
libphonenumber parsing, SSN area rules, octet ranges) — scores **1.000
precision and recall**. All remaining imprecision sits in PERSON / ORG /
ADDRESS, where the ambiguity is genuine and a human would also hesitate.

On the supplied prospectus: **~1,035 replacements in ~30 s**, plus a
verification pass over the finished file.

---

## What it detects

**18 types.** The nine required by the brief, plus nine India-specific
identifiers relevant to this document class.

### Required types

| Type | How it's found | Validation |
|---|---|---|
| `PERSON` | spaCy NER + contact-block label rule | — |
| `EMAIL` | regex | dotted TLD required |
| `PHONE` | regex candidates | **libphonenumber** parse (IN/US/international) |
| `ORG` | spaCy NER + legal-form rule | corporate suffix / glossary stoplist |
| `ADDRESS` | 4 structural rules (see below) | PIN code or street keyword |
| `SSN` | regex | area number actually issued (not 000/666/900+) |
| `CREDIT_CARD` | regex | **Luhn checksum** |
| `DATE_OF_BIRTH` | regex | birth keyword nearby *or in the table header* |
| `IP_ADDRESS` | regex | every octet ≤ 255, v4 and v6 |

### India-specific types

| Type | Shape | What keeps precision up |
|---|---|---|
| `PAN_NUMBER` | `ABCDE1234F` | fixed 5-letter/4-digit/1-letter structure |
| `AADHAAR_NUMBER` | `XXXX XXXX XXXX` | high confidence only with "Aadhaar"/"UIDAI" nearby |
| `IFSC_CODE` | `HDFC0001234` | **reserved `0` in position 5** — a 1-in-36 structural check |
| `GSTIN` | `27AAAPL1234C1Z5` | **embedded PAN must be well-formed** + reserved `Z` |
| `UPI_ID` | `name@okhdfcbank` | handle must be a **known PSP** (allowlist) |
| `PASSPORT_NUMBER` | `M1234567` | requires "passport" nearby |
| `VOTER_ID` | `ABC1234567` | requires "voter"/"EPIC"/"elector" nearby |
| `DRIVING_LICENSE` | `MH12 20110012345` | requires "driving licence"/"DL No" nearby |
| `BANK_ACCOUNT_NUMBER` | 9–18 digits | requires "Account No"/"A/c" nearby |

The last four are **context-gated on purpose**. A bare `M1234567` or a
12-digit run is indistinguishable from the reference codes a formal document
is full of — there is no confidence level at which the pattern alone is safe
to flag, so they only fire next to an explicit label.

> **A collision worth knowing about:** a bank account number and a driving
> licence are both *phone-shaped* enough that libphonenumber accepts them,
> and `PhoneDetector` won every overlap on confidence — the number was
> redacted but recorded under the wrong type. Both now carry higher
> confidence *because* they require an explicit label, which is stronger
> evidence than a generic digit run. Detectors stay independent; the central
> resolver arbitrates.

---

## How it works

**A hybrid engine: validated regex for structured PII, NER for unstructured
PII, and the document's own glossary as a precision filter.**

I evaluated Microsoft Presidio first — it's the purpose-built tool for this —
but its default recognisers misfired badly on Indian phone numbers
(`+91 9876543210` classified as a UK NHS number, phone confidence 0.4) and it
pulls a network-dependent locale lookup at runtime. Rather than fight its
recogniser set I built a smaller, controllable engine on the same idea.

### 1. Structured PII — pattern *plus proof*

A pattern match alone is never enough. A long number in a financial filing is
far more likely to be a share count than a card number; **Luhn is what tells
them apart.** These detectors don't guess — they pattern-match, then verify.

### 2. Unstructured PII — NER, then structure

spaCy (`en_core_web_sm`) does the base work, but a statistical model needs
sentence context and large parts of a filing have none. Two structural rules
cover what NER structurally cannot:

**Contact-block names.** A filing's contact details are a label/value layout:

```
Telephone: +91 20 6769 4648  Contact Person: Manisha Shukla  Website: …
```

There's no grammar here for a model to read. Measured against the real
document, this single layout accounted for **half of all missed names**. A
rule that reads the *label* instead — the same cue a human uses — took
**PERSON recall from 0.500 to 0.917.** It is the highest-value thing in the
codebase and it is not clever; it only became visible by evaluating against a
real filing.

**Addresses** are found by four rules, never by NER location entities
(redacting "India" costs precision and protects nobody):

1. **Lead-in phrase** — "situated at", "Registered Office:" — capture the
   clause that follows.
2. **Field label** — a table cell whose *column header* says "Registered
   Office" *is* an address; the label lives in a different cell.
3. **PIN code + street keyword** together.
4. **Split across lines** — a cover-page address typeset one line per
   paragraph, where no single line holds enough evidence alone.

> Getting a **whole** address matters more than it first appears. When no rule
> fires, the address doesn't survive intact — the ORG and PERSON detectors
> pick off its recognisable fragments, producing a mangled line where fake
> names sit between the real house number, PIN code and state. That's worse
> than either redacting or ignoring it cleanly.

### 3. The document's own glossary as a stoplist

A prospectus is written against a definitions section: "Equity Shares", "the
Offer Price", "Anchor Investors" are *defined terms*, capitalised because
they carry legal meaning — not because they name anything. NER sees Title
Case and reasonably guesses ORG.

Rather than hardcode a lexicon tuned to one document, the tool **parses the
document's own `Term | Description` tables at runtime** — 579 terms on this
filing — and stoplists them. This removed **~585 false-positive company
redactions**.

The guard rail matters as much as the feature: a glossary row may define an
**alias for a real company** (`Nuvama` → `Nuvama Wealth Management Limited`).
Stoplisting that would leave a real bank unredacted — a privacy failure
caused by a precision feature — so rows whose term appears inside a
corporate-suffixed description are skipped.

### 4. Consistent fake replacement

Keyed by `(entity_type, normalized_value)`, so the same original always maps
to the same fake. Four refinements make the output read like a real document:

- **Indian locale** (`Faker('en_IN')`) — a Pune address becoming "998 Chelsea
  Shoals, Sandrastad, AL" makes the output obviously synthetic.
- **Phone shape preserved** digit-for-digit: `+91 22 4009 4400` (landline)
  and `9876543210` (mobile) keep their separators, grouping and character.
  Structural digits — a leading `0` marking an area code, a bare `91` — are
  kept, so a fake number reads as the same *kind* of number.
- **Casing follows context** — an ALL-CAPS cover-page mention stays ALL-CAPS
  while resolving to the *same* fake identity as its Title Case mentions.
- **Name and company variants linked** — "Pushpa Hegde"/"Pushpa Kushal
  Hegde" and "ICICI Securities"/"ICICI Securities Limited" each map to one
  fake identity, but only when exactly one full name yields that short form,
  so two people are never merged by guesswork.

---

## Where it looks — hidden content

A `.docx` is not a string. Text lives in places a naive text pass never
reaches, and **every one of these was verified to leak before it was fixed**.

| Surface | Why it's missed | Status |
|---|---|---|
| Body, tables, headers, footers | — | ✅ redacted |
| **Merged table cells** | `python-docx` yields the same cell once per spanned column — 3,168 duplicate references in this document. Redacting each occurrence corrupted the text (the second pass found "PII" in the first pass's fake output) | ✅ deduplicated |
| **First-page / even-page headers** | `.header` is only the *default* variant of three | ✅ all six variants |
| **Text boxes and shapes** | `<w:txbxContent>` isn't a child of the body, so `paragraph.runs` never returns it | ✅ walked explicitly |
| **Tracked-change insertions** | Runs nested in `<w:ins>` — verified: `len(paragraph.runs) == 0`. The text was invisible to *every* detector | ✅ flattened first |
| **Tracked-change deletions** | Stored in `<w:delText>`, still in the file, visible the moment someone enables "Show Markup" | ✅ removed entirely |
| **Hyperlink display text** | Runs nested in `<w:hyperlink>` — often exactly the email someone pasted as a link | ✅ flattened first |
| **Footnotes / endnotes** | No `python-docx` API exists at all (no `docx.parts.footnotes` module) | ✅ raw XML parse + write-back |
| **Comments** | API exists but was never wired in | ✅ redacted |
| **Comment authors** | A real name in the metadata, independent of the comment text | ✅ scrubbed + audited |
| **Core properties** | Author / last-modified-by survive any text-only redaction | ✅ scrubbed |
| **Custom properties** | No API; a "Prepared For" property is as much a leak as body text | ✅ raw zip rewrite |
| **Embedded images** | Would require OCR | ⚠️ **warned, not redacted** |
| **Embedded objects** | Opaque binary (e.g. an embedded spreadsheet) | ⚠️ **warned, not redacted** |

Tracked changes are always redacted **as if every change had been accepted** —
insertions promoted, deletions dropped. That's a deliberate, disclosed choice,
not an accident of what `python-docx` exposes.

The two ⚠️ rows produce an explicit warning in the CLI output, the API
response and the web UI. The tool says what it *couldn't* check rather than
reporting a clean result that quietly isn't.

---

## It checks its own work

After writing the output, the tool **re-opens the finished file and re-scans
it**. Every fix in this project was found by reading the actual output and
comparing it to the source; that process is mechanical enough to automate, so
it runs on every redaction.

Two independent passes, reported separately because they carry different weight:

**1. Leak check** — for every value the pipeline believed it redacted, does
that exact text still appear anywhere in the finished file? Zero false
positives by construction: if it fires, something that should be gone is
still there. **This is what `clean` tracks.**

**2. Unexpected-PII re-scan** — re-runs detection over the whole finished
document, independent of the paragraph-by-paragraph structure of the main
pass, and reports anything that isn't a known fake value. A genuinely
independent second opinion — but it inherits the same statistical imprecision
as the primary pass, so it's an advisory to skim, not a blocking finding.

```
Residual scan (re-checked the finished file for leftover PII):
  LEAKS: none -- every original value the pipeline redacted is confirmed gone.
```

> Two false-positive classes had to be designed out of this before it was
> useful. Fake values are *themselves PII-shaped* — with 250+ fake people and
> 600+ fake companies in one document, fragments recombine and get re-tagged,
> so a match built entirely from known fake words is suppressed. And a
> single NER misfire on a section heading ("Capital Structure") doesn't make
> every *other* occurrence of that heading a leak — the leak check is gated
> to matches confident enough to propagate document-wide. Skipping that
> second guard reported **45 "leaks"** on the real prospectus, all of them
> headings correctly left alone.

Verification roughly doubles wall-clock time. Skip it with `--no-verify`
(not recommended for a document you'll actually share).

---

## Confidence tiers

Every replacement carries a tier, so a reviewer knows where to spend attention:

| Tier | Threshold | Means |
|---|---|---|
| **High** | ≥ 0.90 | Validated structured PII, or a deterministic rule (corporate legal form). Safe to trust. |
| **Medium** | 0.80 – 0.89 | A statistical model's normal-confidence guess. Right far more often than not. |
| **Needs review** | < 0.80 | Found by inference — the consistency sweep, or Aadhaar without a nearby keyword. Worth a human glance. |

Surfaced in the CLI, the `X-Redaction-Summary` API header, the audit log, and
as a proportional bar in the web UI.

---

## Explicit design choices

The brief asks to be explicit about what's treated as sensitive.

1. **Order / ticket / invoice numbers, ISINs, CINs and SEBI registration
   numbers are not redacted.** They identify a transaction or a filing, not a
   person.
2. **The issuing company's own name is kept** (`--issuer`). A prospectus is
   inherently *about* one named company; redacting it throughout adds no
   privacy (the filing is public) and makes the document unreadable. All
   *other* organisations — auditors, bankers, counsel, registrars — are
   redacted. Override with `--redact-issuer-company`.
3. **Public regulators are not redacted** (SEBI, RBI, the stock exchanges,
   Registrar of Companies). "Registered with `<fake company>` under the
   `<fake company>` Regulations" protects nobody and destroys meaning.
4. **A bare city or country is not an address.** "Bangalore" alone doesn't
   identify anyone.
5. **Website URLs are not redacted** — not one of the required types, and a
   public corporate URL identifies no one.
6. **Tracked changes are redacted as if accepted** (see above).
7. **GSTIN is redacted by default** even though for a company it's closer to
   a CIN, because it's very commonly issued to an individual's sole
   proprietorship. Disable per-run if that doesn't apply.

---

## Known limitations

Measured, not guessed — each is an observed error in [`EVALUATION.md`](EVALUATION.md) §3.

- **Image and embedded-object text is never scanned.** No OCR. The tool warns
  when they're present.
- **Single-word brands with no legal form** ("CareEdge") are missed. Nothing
  structural separates them from an ordinary capitalised noun; sweeping
  single words was tested and produced far too many false positives to keep.
  Multi-word and legal-form names are caught.
- **Long shared contact labels truncate** — the fifth name in
  `Contact Person: A/ B/ C/ D/ E` can fall outside the 160-character scan
  window.
- **Institutional over-redaction remains** — "India Scheme", "ICDR Master
  Circular". Every remaining false positive is of this kind: legal or
  institutional language, never personal data.
- **US-style street addresses** carry neither a PIN code nor an Indian
  address keyword.
- **ORG recall is unmeasured** on the real document — the sample contains no
  ORG gold spans. Stating that is better than quoting a number the sample
  can't support.
- **A scanned/image-only PDF extracts to empty text** and produces an empty
  output with 0 redactions. The result is honest but the input was never
  readable — check the redaction count.

---

## Running it

```bash
pip install -r requirements.txt      # includes the spaCy model wheel
```

### CLI

```bash
python cli.py --input FILE --output OUT.docx [options]
```

| Flag | Purpose |
|---|---|
| `--input` | `.docx`, `.pdf` or `.txt` |
| `--output` | always written as `.docx` |
| `--issuer NAME [NAME ...]` | company names to **keep** unredacted |
| `--redact-issuer-company` | redact the subject company too |
| `--disable TYPE [TYPE ...]` | switch off PII types |
| `--seed N` | reproducible fake values (default 42) |
| `--audit-log PATH` | JSON audit trail + confidence summary + residual scan |
| `--no-verify` | skip the residual scan (~halves runtime) |

### Web app

```bash
export PII_APP_PASSWORD="choose-a-password"   # omit to disable the gate
uvicorn app.main:app --reload --port 8000     # → http://localhost:8000
```

On Windows PowerShell use `$env:PII_APP_PASSWORD = "…"` instead of `export`.

### As a library

```python
from app.core.detectors.base import DetectionConfig
from app.core.document_io import redact_file
from app.core.redactor import Redactor

result = redact_file("filing.docx", "redacted.docx",
                     Redactor(config=DetectionConfig(),
                              issuer_names={"KSH International Limited"}))

print(len(result.replacements), result.residual.clean, result.warnings)
```

---

## Web app and security

`PII_APP_PASSWORD` gates both the page and the API:

- Compared with `secrets.compare_digest` — no timing leak
- Exchanged once for an opaque random session token (12 h), never stored in source
- Token kept in `sessionStorage`, so it dies with the browser tab
- Failed logins rate-limited per IP (8 per 5 minutes)

Other hardening:

- **Streaming upload cap** — the body is read in 1 MB chunks and aborted the
  moment it exceeds 10 MB, rather than buffering the whole upload and *then*
  checking its size. A `Content-Length` pre-check rejects the obvious cases
  without reading a byte.
- **Concurrency limit** — redaction is CPU-bound; `PII_MAX_CONCURRENT_JOBS`
  (default 3) caps simultaneous jobs, with a clear `503` instead of an
  indefinite hang.
- **Threadpool** — a long job never blocks the event loop for other requests.
- **No persistence** — uploads go to a temp directory deleted immediately
  after the response.
- **The API never echoes matched text.** The residual-scan summary sent over
  the wire is counts and types only; a PII-redaction service returning
  potentially-sensitive strings in its own response would defeat the point.
- **`no-cache` on static assets** — a stale cached `script.js` is
  indistinguishable from a bug, and cost real debugging time during
  development.

With no password set the gate is disabled entirely — convenient locally,
**so set it before deploying anywhere public.**

---

## Project layout

```
app/
  core/
    defined_terms.py        # parses the document's own glossary → stoplist
    docx_revisions.py       # flattens tracked changes + hyperlinks
    docx_hidden_content.py  # footnotes, endnotes, comments, custom properties
    verification.py         # residual scan of the finished file
    detectors/
      base.py               # PIIMatch, PIIDetector, DetectionConfig, tiers
      regex_detectors.py    # 15 validated / context-gated structured types
      ner_detector.py       # PERSON, ORG, ADDRESS (spaCy + structural rules)
      registry.py           # assembles detectors, resolves overlaps, name sweep
    faker_mapper.py         # consistent, locale-aware fake values
    redactor.py             # detect + replace orchestration
    document_io.py          # .docx/.pdf/.txt in → formatted .docx out
  api/
    auth.py                 # shared-password gate
    routes.py               # /api/redact, /api/auth/*, /api/health
  main.py                   # FastAPI app, serves the frontend
frontend/                   # login + upload UI (no build step)
evaluation/
  build_test_corpus.py      # synthetic corpus + ground truth, together
  run_evaluation.py         # scores the synthetic corpus
  sample_real_document.py   # stratified sample of the REAL document
  real_ground_truth.json    # hand labels + labelling policy
  run_real_evaluation.py    # scores the real document
tests/
  conftest.py               # .docx fixture builders (revisions, hidden content)
  test_detectors.py         # detector + pipeline tests
  test_hidden_content.py    # hidden surfaces + residual scan
output/                     # redacted .docx + audit log (the deliverables)
cli.py
README.md · EVALUATION.md · ARCHITECTURE.md · API.md
```

---

## Extending it

The plugin contract is one method:

```python
class PassportDetector(PIIDetector):
    name = "passport"
    _PATTERN = re.compile(r"\b[A-Z]\d{7}\b")

    def detect(self, text: str) -> List[PIIMatch]:
        return [PIIMatch("PASSPORT", m.group(), m.start(), m.end(), 0.9, "regex")
                for m in self._PATTERN.finditer(text)]
```

1. Write the class.
2. Add one line to `_TYPE_TO_DETECTOR` in `registry.py`, and the type name to
   `ALL_PII_TYPES` in `base.py`.
3. Optionally add `_fake_passport()` to `faker_mapper.py` — without it, a
   deterministic placeholder is still produced.

The CLI's `--disable`, the API's `disable_types`, the web UI's checkboxes and
both evaluation harnesses pick it up with no further changes, because they all
read `ALL_PII_TYPES` rather than keeping their own copy. (They used to keep
three separate copies; that drifts.)

**No detector knows about any other.** Overlap resolution, consistency and
replacement are central, so a new detector can't break an existing one — the
worst it can do is lose an overlap. If it needs document structure (a table's
column header), set `accepts_context = True`; that flag exists so the common
case stays a one-method contract.

---

## Testing

```bash
python -m pytest tests/ -v                    # 64 tests
python evaluation/run_evaluation.py           # synthetic corpus
python evaluation/run_real_evaluation.py      # real document
```

Weighted toward **regressions of real bugs** rather than restating the happy
path. Each of these encodes a failure that actually occurred:

| Test | The bug it locks down |
|---|---|
| `test_address_survives_a_phone_number_inside_it` | street addresses leaking in cleartext |
| `test_two_addresses_in_one_sentence` | a capture bound that failed when one sentence held two addresses |
| `test_abbreviation_does_not_end_the_address` | "Plot No." truncating the span to "Plot No" |
| `test_insertion_is_invisible_before_flattening` | guards the premise: tracked insertions really do expose 0 runs |
| `test_tracked_deletion_is_removed_entirely` | deleted text still present under "Show Markup" |
| `test_formatting_metadata_and_merged_cells` | merged-cell double redaction, flattened formatting, author metadata |
| `test_comment_author_metadata_is_scrubbed` | a reviewer's real name in comment metadata |
| `test_generic_shapes_require_a_nearby_keyword` | passport/voter/account patterns firing on reference codes |
| `test_summary_never_exposes_the_matched_text` | the API echoing sensitive strings back |
| `test_injected_leak_is_caught` | the verifier actually detecting a planted leak |

The two evaluation harnesses are the other half of the safety net: any change
to detection is scored against both before it's kept. That's how the
overlap-splitting regression (0.912 → 0.829) was caught immediately rather
than shipped.

---

## Deployment

`Procfile` and `render.yaml` are included.

**Render:** New → Web Service → connect the repo → `render.yaml` is read
automatically. Set `PII_APP_PASSWORD` in the dashboard. Free tier works; cold
starts take a few extra seconds to load the spaCy model.

**Railway:** New Project → Deploy from repo → the `Procfile` is auto-detected.
Set `PII_APP_PASSWORD` in the service variables.

**Vercel/Netlify** are serverless-first and a poor fit for a
several-hundred-MB Python NLP stack. If required, host `frontend/` there and
point it at a Render/Railway backend — CORS already allows it, and
`X-Redaction-Summary` is explicitly exposed so the breakdown survives a
cross-origin request.

> **Memory note:** spaCy plus the model needs roughly 400–600 MB resident.
> Render's 512 MB free tier is marginal — if the service restarts under load,
> that's the first thing to check.

---

## Tech stack

Python 3.12 · FastAPI + Uvicorn · spaCy (`en_core_web_sm`) ·
`phonenumbers` (libphonenumber) · `Faker` · `python-docx` · `pdfplumber` ·
`lxml` · vanilla HTML/CSS/JS (no build step).
