# Architecture

How the tool is put together, and **why** each significant decision went the
way it did. [`README.md`](README.md) covers what it does and how to run it;
[`EVALUATION.md`](EVALUATION.md) covers how well it works.

---

## 1. The shape of the problem

Redaction is two jobs that pull in opposite directions:

- **Find every piece of PII.** Missing one is a privacy failure.
- **Touch nothing else.** Over-redaction destroys the document. A prospectus
  where "Equity Shares" became "Taylor Inc" 36 times is unusable, and the
  brief grades precision explicitly.

Nothing solves both perfectly, so the design question is *where to put the
error budget*. This tool puts it where a human would also hesitate — names,
company names, addresses — and drives it to zero everywhere a machine can be
certain, by refusing to trust a pattern match that hasn't been verified.

A third job appears only once you handle real files: **preserve the
document**. A `.docx` is not a string. Formatting, tables, merged cells,
headers, text boxes and metadata all carry content, and a naive
text-in/text-out approach silently damages or leaks through all of them.

---

## 2. Data flow

```
                    ┌──────────────────────────────────┐
  .docx ───────────►│ document_io.redact_docx          │
  .pdf  ──┐         │  · walk every reachable paragraph│
  .txt  ──┴────────►│  · parse the document's glossary │
                    └───────────────┬──────────────────┘
                                    │  text + structural context
                                    ▼
                    ┌──────────────────────────────────┐
                    │ registry.run_all_detectors       │
                    │                                  │
                    │  regex_detectors ──┐             │
                    │   (verified)       ├─► resolve_  │
                    │  ner_detector   ───┘   overlaps  │
                    └───────────────┬──────────────────┘
                                    │  List[PIIMatch]
                                    ▼
                    ┌──────────────────────────────────┐
                    │ document-wide refinement         │
                    │  · build_known_names             │
                    │  · derive_name_aliases           │
                    │  · sweep_known_names  (no model) │
                    └───────────────┬──────────────────┘
                                    │
                                    ▼
                    ┌──────────────────────────────────┐
                    │ faker_mapper.ConsistentFaker     │
                    │  same value → same fake value    │
                    └───────────────┬──────────────────┘
                                    │  (span → fake) edits
                                    ▼
                    ┌──────────────────────────────────┐
                    │ splice into runs, scrub metadata │
                    └───────────────┬──────────────────┘
                                    ▼
                            redacted .docx + audit log
```

The ordering matters in one non-obvious way: **detection completes over the
whole document before any replacement happens.** That is what makes the
document-wide refinement step possible — you cannot know that `KUSHAL
SUBBAYYA HEGDE` on the cover page is a person until you have seen `Kushal
Subbayya Hegde` in a sentence 400 paragraphs later.

---

## 3. Detection: three layers, deliberately unequal

### Layer 1 — Structured PII: pattern **plus proof**

`app/core/detectors/regex_detectors.py`

Email, phone, SSN, credit card, IP, PAN, Aadhaar, date of birth. Every one of
these follows the same two-step shape:

```python
for candidate in PATTERN.finditer(text):
    if not really_valid(candidate):
        continue
    yield PIIMatch(...)
```

The verification step is the whole point:

| Type | Pattern finds | Proof required |
|---|---|---|
| Credit card | 13–19 digits | **Luhn checksum** |
| Phone | digit groups | **libphonenumber** parse, IN/US/international |
| SSN | `NNN-NN-NNNN` | area number actually issued (not 000/666/900+) |
| IP | dotted quad | every octet ≤ 255 |
| Date of birth | a date | a birth keyword nearby *or* in the table header |

A long number in a financial document is far more likely to be a share count
than a card number. Luhn is what tells them apart. **This is why every
structured type scores 1.000 precision and recall** — these detectors don't
guess, and when they're wrong it's a bug rather than a judgement call.

Two of them are context-gated rather than purely structural, because their
shape is ambiguous on its own:

- **Date of birth** — a prospectus is full of dates (filing, incorporation,
  board meetings). Only a date near a birth label is a DOB.
- **Aadhaar** — `NNNN NNNN NNNN` is also a phone shape. Without the word
  "Aadhaar" nearby it emits low confidence, so a validated PHONE match on the
  same digits wins the overlap.

### Layer 2 — Unstructured PII: NER, then structure

`app/core/detectors/ner_detector.py`

Names, organisations and addresses have no fixed shape, so spaCy
(`en_core_web_sm`) does the base work. But a statistical model needs
*sentence context*, and large parts of a filing have none. Two structural
rules cover what NER structurally cannot:

**Addresses are not NER-derived at all.** Two precise rules instead:

1. **Lead-in phrase** — "situated at", "resides at", "Registered Office:" —
   capture the clause that follows. Capture starts *after* the phrase, so a
   preceding "Rohan Dey, aged 42," is naturally excluded from the span.
2. **PIN code + street keyword together**, sentence-scoped, for addresses in
   table cells with no lead-in.

Why not NER: treating "any sentence containing a PIN code" as one address
swallows neighbouring names and phone numbers into a single span, and letting
bare location entities count as addresses means redacting "India" — which
costs precision and protects nobody.

**Contact-block names.** A filing's contact details are a label/value layout:

```
Telephone: +91 20 6769 4648  Contact Person: Manisha Shukla  Website: …
```

There is no grammar here for a model to read. Measured against the real
document, this single layout accounted for **half of all missed names**. The
rule reads the *label* — the same cue a human uses — scans a bounded window
after it, stops at the next field label, and accepts 2–4 capitalised words.
**PERSON recall: 0.500 → 0.917.**

This is the most valuable thing in the codebase and it is not clever. It only
became visible by evaluating against a real filing; the synthetic corpus is
written in prose and never exhibited the failure.

### Layer 3 — The document's own glossary as a stoplist

`app/core/defined_terms.py`

A prospectus is written against a definitions section. "Equity Shares", "the
Offer Price", "Anchor Investors" are *defined terms* — capitalised because
they carry legal meaning, not because they name anything. NER sees Title Case
and reasonably guesses ORG.

Rather than hardcode a lexicon tuned to one example document, the tool parses
the document's own `Term | Description` tables at runtime. On this filing that
yields **579 terms** and removes **~585 false-positive ORG redactions**.

The guard rail matters as much as the feature. A glossary row may define an
**alias for a real company**:

```
Nuvama  →  Nuvama Wealth Management Limited
```

Stoplisting "Nuvama" would leave a real bank's name unredacted — a privacy
failure caused by a precision feature. So a row is skipped when the term
carries a corporate suffix, **or** when its description is a short company
name containing the term. "Corporate Promoter" → "Waterloo Industrial Park VI
Private Limited" is *not* skipped, because the term isn't part of the company
name: the label is generic, the company stays redactable.

---

## 4. Overlap resolution, and the bug that shaped it

`registry.resolve_overlaps`

Detectors overlap constantly. Priority is by trustworthiness — verified regex
types outrank ADDRESS, which outranks PERSON, which outranks ORG:

```python
_TYPE_PRIORITY = {EMAIL/PHONE/SSN/…: 0, ADDRESS: 1, PERSON: 2, ORG: 3}
```

The naive greedy version of this **leaked street addresses in cleartext**:

```
RAW:    ADDRESS '4th Floor, Prestige Tech Park, Bangalore 560103, Tel 020-45053237 India'
        PHONE   '020-45053237'
GREEDY: PHONE only  →  the address is silently discarded
```

The phone sits *inside* the address and outranks it, so the containing span
lost and vanished. Three real addresses leaked from the delivered file this
way.

The fix is to **split rather than discard**: the loser keeps whatever the
winner didn't claim.

```
ADDRESS '4th Floor, Prestige Tech Park, Bangalore 560103, Tel '
PHONE   '020-45053237'
```

Two constraints keep the split from causing new problems:

- **Only across different types.** Two ADDRESS spans overlapping are rival
  readings of one thing, and splitting them leaves the loser's lead-in prose
  ("The registered office is situated at ") behind as a bogus address
  fragment — which then outranks and suppresses the real names inside it.
  This regressed the benchmark from 0.912 to 0.829 before it was caught.
- **Only for splittable types** (`ADDRESS`). Half a person's name is noise,
  not privacy.

---

## 5. Consistency: same value, same fake value

`app/core/faker_mapper.py`

The brief's worked example requires that "Rohan Dey" become the same fake name
everywhere. The cache is keyed `(entity_type, normalized_value)` — the easy
part. Four things make it hold on a real document:

**Casing follows context, identity does not.** The same name appears Title
Case in prose and ALL CAPS on cover pages. Both must be the same fake person,
but writing Title Case into an ALL-CAPS line visibly breaks the typography. So
casing is applied on *retrieval*, after the cache lookup — one identity, two
renderings.

**Name variants are linked.** "Pushpa Hegde" and "Pushpa Kushal Hegde" are
different cache keys and would become two different fake people. Aliases map
the short form onto the full one — but **only when exactly one full name
produces that short form**. If two people would collapse together, the mapping
is ambiguous and is skipped rather than guessed.

**Shape is preserved for phones.** `+91 22 4009 4400` (landline) and
`9876543210` (mobile) keep their separators, grouping and character; only the
digits change. Structural digits — a leading `0` marking an area code, a bare
`91` country code — are kept, so a fake number reads as the same *kind* of
number as the one it replaced.

**Locale matches the document.** `Faker('en_IN')`, because a Pune address
becoming "998 Chelsea Shoals Suite 024, Sandrastad, AL" makes the output
obviously synthetic. SSNs are the exception and use a US generator, since
`en_IN` has no such provider.

`.unique` is best-effort: it raises once its retry budget is exhausted, which
this document genuinely approaches (~560 distinct companies). Uniqueness is a
readability nicety, not a privacy requirement, so exhaustion falls back to a
plain draw rather than failing the run.

---

## 6. Document fidelity

`app/core/document_io.py`

Everything in this module exists because a `.docx` is not a string.

**Formatting — splice, don't flatten.** A run is the unit of formatting, and a
paragraph is usually many (`"Contact "` / `"Rohan Dey"` / `" on +91…"`). The
earlier approach wrote the whole redacted paragraph into the first run and
blanked the rest — correct in content, but it destroyed bold, italics and
fonts in *every* modified paragraph. Now only the runs a match actually
touches are rewritten. **655 multi-run paragraphs and 527 bold runs survive
in the output.**

**Merged cells — deduplicate.** `python-docx` yields the *same* cell object
once per spanned grid column: **3,168 duplicate references** across this
document's 76 tables. Redacting each occurrence independently meant a merged
cell was redacted repeatedly, with the second pass detecting "PII" inside the
first pass's fake output and replacing that too — corrupting the text and
filling the audit log with invented names.

**Reachability.** Text that a text-only walk never sees:

| Location | Why it's missed | Handled by |
|---|---|---|
| Merged cells | duplicated, not missed | dedupe by `<w:tc>` identity |
| First/even-page headers | `.header` is only the default | all six header/footer variants |
| Text boxes / shapes | not children of the body | walk `<w:txbxContent>` |
| Tracked insertions | runs nested in `<w:ins>` | `docx_revisions.flatten_revisions` |
| Tracked deletions | text stored in `<w:delText>` | removed entirely |
| Hyperlink text | runs nested in `<w:hyperlink>` | unwrapped before reading |
| Footnotes / endnotes | no `python-docx` API exists | parse part XML, write back to `_blob` |
| Comments + authors | API existed, never wired in | `docx_hidden_content.redact_comments` |
| Author, last-modified-by | not page text at all | scrub `core.xml` |
| Custom properties | no API | raw zip rewrite after save |

Metadata is the classic redaction failure: invisible on the page, fully intact
in the file, and it names the people who wrote the document.

**The revision blind spot is worth stating precisely**, because it is the one
place where the API silently lies rather than merely omitting something.
`Paragraph.runs` returns only `<w:r>` elements that are *direct children* of
`<w:p>`. Word nests a tracked insertion inside `<w:ins>`, so a paragraph
consisting entirely of inserted text reports **`len(paragraph.runs) == 0` and
`paragraph.text == ""`** — verified directly, and locked down by
`test_insertion_is_invisible_before_flattening`. Since the pipeline builds its
input as `"".join(r.text for r in paragraph.runs)`, that text was never seen by
any detector. A deletion has the mirror-image problem: it is still physically
in the file, visible the instant a reader enables "Show Markup".

`flatten_revisions` normalises the tree *before* anything reads it —
insertions and hyperlinks unwrapped, deletions dropped — which means the tool
always redacts the document **as if every tracked change had been accepted**.
That is a disclosed editorial choice, not an accident of what the library
exposes.

**Two surfaces are detected but deliberately not redacted:** embedded images
(text inside them would need OCR) and embedded objects (opaque binary). Both
produce an explicit warning in the CLI, API response and UI. Saying "10
embedded images were not scanned" is worth more than a clean-looking result
that quietly wasn't complete.

**Table context.** Each cell is redacted independently, so a `Date of Birth`
column header — a *different cell* from its values — would never reach the
DOB detector. Cells therefore carry their column header and row label as
`context`. Detectors opt in with `accepts_context = True`; everything else
keeps the plain `detect(text)` contract.

---

## 6a. Verification: the tool checks its own work

`app/core/verification.py`

Every fix in this project was found the same way — by reading the actual
output and comparing it against the source. That process is mechanical
enough to automate, so it now runs on every redaction rather than only when
someone thinks to look.

Two passes, deliberately **not** pooled into one number:

**Leak check.** For every value the pipeline believed it redacted, does that
exact text still appear anywhere in the finished file? Zero false positives
by construction. This is what `ResidualScanReport.clean` tracks.

**Unexpected-PII re-scan.** Re-runs detection over the whole finished
document, independent of the paragraph-by-paragraph structure of the main
pass. Genuinely independent — but it inherits the primary pass's statistical
imprecision, so it's advisory.

Folding the second into `clean` was tried and rejected: on any large real
document it makes "not clean" the ordinary outcome, which trains a user to
ignore the flag — precisely the failure mode a verification feature exists
to prevent.

**Two false-positive classes had to be designed out before this was useful,
and both are instructive:**

1. *The tool re-detecting its own fakes.* Fake values are PII-shaped by
   design. With 250+ fake people and 600+ fake companies in one document,
   surname fragments recombine ("Kaul" from one fake name, "Chaudhry" from
   another) and NER re-tags the result as a new entity. Exact-string
   suppression isn't enough because the re-detected span rarely aligns with
   any single replacement's boundaries, so a match whose every significant
   word comes from a known fake value is suppressed.

2. *Treating a NER misfire as a redaction obligation.* The first version
   assumed any redacted string should vanish everywhere, and reported **45
   "leaks"** on the real prospectus — every one a section heading or defined
   term ("Capital Structure", "RISKS") that NER had mistakenly tagged in
   exactly one spot. Every *other* occurrence being left alone is correct
   behaviour. The leak check is now gated by
   `registry.is_confidently_identifying`, reusing the same gate that decides
   whether a name is safe to propagate document-wide, so both checks answer
   one question: *is this confidently a real name, or a guess on ordinary
   prose?*

The scan reads the **finished file on disk**, not the in-memory objects it
was built from — so it also covers the custom-properties rewrite that happens
after `Document.save()`, and it checks what a recipient would actually open.

`ResidualScanReport.summary()` returns counts and types only, never the
matched text. That summary is what crosses the network; a PII-redaction
service echoing potentially-sensitive strings back in its own API response
would defeat the purpose.

---

## 7. Performance

The pipeline ran ~70 s on this document; it now runs ~30 s, plus ~50 s for
the verification pass (which re-runs full detection over the output — see
§6a; `--no-verify` skips it). Two causes, both structural rather than
micro-optimisation:

- **spaCy ran four times per paragraph.** `PersonNameDetector` and
  `OrganizationDetector` each invoked the model on the same text, and the
  pipeline ran two full detection passes. An `lru_cache` on the analysis
  collapses the first pair; reusing pass-one matches removes the second pass
  entirely. **4 model invocations per paragraph → 1.**
- **Unused pipeline components.** Only NER is needed, so the tagger, parser,
  attribute ruler and lemmatizer are excluded at load. Verified to produce
  identical entities, roughly 2× faster.

The document-wide refinement pass is regex-only by design — no model runs
during replacement.

---

## 8. Extending it

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
   `DetectionConfig.enabled_types`.
3. Optionally add `_fake_passport()` to `faker_mapper.py` — without it, a
   deterministic placeholder is still produced.

The CLI's `--disable`, the API's `disable_types`, the web UI's checkboxes and
both evaluation harnesses pick it up with no further changes, because they all
iterate `enabled_types` rather than hardcoding a list.

**No detector knows about any other.** Overlap resolution, consistency and
replacement are central, so a new detector cannot break an existing one — the
worst it can do is lose an overlap.

If a detector needs document structure, set `accepts_context = True` and
receive the surrounding text. That flag exists so the common case stays a
one-method contract.

---

## 9. Web layer

`app/api/`

- **`auth.py`** — a single shared password, because the requirement is "stop
  anyone who finds the URL", not "support users and roles". Compared with
  `secrets.compare_digest` (no timing leak), exchanged once for an opaque
  random token, rate-limited per IP. Tokens are in-memory, so a restart logs
  everyone out — acceptable for a tool that holds no other state.
- **`routes.py`** — redaction is CPU-bound for tens of seconds, so it runs via
  `run_in_threadpool`. Called directly inside `async def` it would block the
  event loop and freeze the server for every other request.
- **CORS** allows any origin but exposes `X-Redaction-Summary` explicitly;
  without that the browser hides the header from cross-origin JavaScript and
  the UI silently loses its breakdown. Auth travels in a header rather than a
  cookie, so a permissive origin policy doesn't hand a third-party page an
  authenticated session.

Uploads go to a temp directory deleted immediately after the response.

---

## 10. Testing strategy

64 tests, weighted toward **regressions of real bugs** rather than
restating the happy path. Each of these encodes a failure that actually
occurred:

| Test | The bug it locks down |
|---|---|
| `test_address_survives_a_phone_number_inside_it` | street addresses leaking in cleartext |
| `test_formatting_metadata_and_merged_cells` | merged-cell double redaction, flattened formatting, author metadata |
| `test_pii_inside_a_text_box_is_redacted` | text boxes invisible to `python-docx` |
| `test_does_not_flag_letter_prefixed_registration_numbers` | `INM000013004` read as a phone number |
| `test_short_form_maps_to_the_same_fake_person` | one person becoming two fake people |
| `test_ambiguous_short_form_is_not_aliased` | two people merged into one |
| `test_real_company_is_still_redacted` | the glossary stoplist over-reaching |

The two evaluation harnesses are the other half of the safety net: any change
to detection is checked against both the synthetic corpus and the
hand-labelled real-document sample before it is kept. That is how the
overlap-splitting regression (0.912 → 0.829) was caught immediately rather
than shipped.
