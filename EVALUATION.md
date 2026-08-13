# Evaluation Report — PII Redaction Tool

Two independent evaluations are reported:

1. **The real document** (§1–§4) — a hand-labelled sample drawn from the
   actual Red Herring Prospectus that was redacted. This is the headline
   result, because it measures the tool on the filing it was asked to
   process.
2. **A synthetic corpus** (§5) — a purpose-built document containing every
   PII type, including the ones the real filing happens not to contain
   (SSN, credit card, IP address, Aadhaar, PAN, date of birth). Necessary
   for coverage, but weaker evidence, since the same author wrote both the
   corpus and the detectors.

Reporting only the synthetic figure would measure the detector against its
own author's assumptions. Reporting only the real one would leave six of the
nine required PII types unmeasured. Hence both.

---

## 1. Methodology — real document

**Sampling.** `evaluation/sample_real_document.py` draws a reproducible
(seeded) sample of text blocks from the source `.docx`. Blocks are
stratified, and the strata are reported separately:

| Stratum | n | What it is | What it measures |
|---|---|---|---|
| `dense` | 18 | Contact blocks — cover page, registrar, bankers, compliance officer | **Recall**: does it find PII that is definitely there? |
| `prose` | 18 | Risk factors, glossary rows, regulatory boilerplate | **Precision**: does it leave alone the capitalised legal language that fills a prospectus? |

The `dense` stratum is selected by **document-layout markers**
(`Telephone:`, `Contact Person:`, `Registered Office:`) rather than by
anything the detectors look for, so the sample is not chosen using the
opinion of the system under test.

**Labelling.** `evaluation/real_ground_truth.json` holds 37 spans labelled
by hand against the sampled text. Labels are stored as **exact substrings,
not character offsets** — the scorer locates each one, so offsets are
correct by construction and there is nothing to mis-count. The file also
carries the full labelling policy; the decisions that matter:

- Website URLs are **not** PII (not one of the required types; a public
  corporate URL identifies nobody).
- Statutes, regulations and circulars, and the **regulators named inside
  them** (SEBI, RBI, the stock exchanges), are **not** PII. Redacting
  "registered with SEBI under the SEBI ICDR Regulations" protects no one and
  destroys the sentence.
- The document's **own glossary terms** ("Net Proceeds", "Promoter Group",
  "Statutory Auditors") are **not** PII.
- A bare place name ("Supa Ahilyanagar in Maharashtra") is **not** an
  address — it identifies a facility, not a person.
- The **issuer's own name** is excluded by configuration, matching how the
  delivered file was produced (`--issuer "KSH International Limited" …`).

**Scoring.** Identical rules to the synthetic harness, so the two are
comparable: a prediction is a true positive when it shares a gold span's
type and overlaps it by **≥ 50%** of the gold span's length. Unmatched gold
spans are false negatives; unmatched predictions are false positives.
"Accuracy" is `TP / (TP + FP + FN)` — stated explicitly because span
detection over free text has no meaningful true-negative count, so classic
accuracy does not apply and quoting it unqualified would mislead.

Run: `python evaluation/run_real_evaluation.py`

## 2. Results — real document

| Metric | Value |
|---|---|
| Precision | **0.947** |
| Recall | **0.973** |
| F1 | **0.960** |
| Accuracy (TP/(TP+FP+FN)) | **0.923** |
| True positives | 36 |
| False positives | 2 |
| False negatives | 1 |

### By PII type

| Type | TP | FP | FN | Precision | Recall | F1 |
|---|---|---|---|---|---|---|
| EMAIL | 13 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| PHONE | 8 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| ADDRESS | 4 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| PERSON | 11 | 0 | 1 | 1.000 | 0.917 | 0.957 |
| ORG | 0 | 2 | 0 | — | — | — |

### By stratum

| Stratum | TP | FP | FN | Precision | Recall |
|---|---|---|---|---|---|
| `dense` (contact blocks) | 36 | 0 | 1 | 1.000 | 0.973 |
| `prose` (boilerplate) | 0 | 2 | 0 | — | — |

**Reading the ORG row honestly.** The sample contains **no ORG gold spans**:
the counterparty names (bankers, auditors, registrar) live in the cover-page
tables, which exceed the sampler's 700-character block limit and were
therefore not drawn. So this sample measures **ORG precision only** — via
the three false positives — and says **nothing about ORG recall**. That is a
known limitation of this sample, not a result. ORG recall is only evidenced
by the synthetic corpus (§5) and by manual inspection of the output, where
the auditors, bankers and registrar are correctly redacted.

The `prose` stratum producing zero true positives is the intended outcome,
not a failure: those 18 blocks contain no PII, so the only thing measurable
there is whether the tool over-redacts. It produced 2 false positives across
18 blocks of dense legal language.

## 3. Every error, and why

**False negative (1):**

| Type | Text | Why |
|---|---|---|
| PERSON | `Siddharth Jadhav` | Fourth of five names sharing one `Contact Person:` label. The contact-block rule scans a 160-character window after the label and this name falls past it. Raising the window trades precision for recall; it was left as-is. |

**False positives (2):**

| Type | Text | Why |
|---|---|---|
| ORG | `India Scheme` | From "Merchandise Exports from India Scheme (MEIS)" — a government scheme name, glossed in the document with quotes the glossary parser doesn't pick up. |
| ORG | `ICDR Master Circular` | A substring of the glossary term "SEBI ICDR Master Circular"; the stoplist matches whole spans, so the partial span slips past. |

Both are ORG false positives, and neither is personal data — the failure mode
is over-redaction of institutional/legal language, not a privacy leak.

## 4. What changed, measured

The real-document evaluation was built first and then used to drive fixes.
Both numbers below are on the same sample with the same labels:

| | Precision | Recall | F1 |
|---|---|---|---|
| Before | 0.833 | 0.811 | 0.822 |
| After | **0.947** | **0.973** | **0.960** |

Two changes drove almost all of it, and neither was visible from the
synthetic corpus:

**PERSON recall: 0.500 → 0.917.** Half of all names were being missed
because they sit in contact blocks ("`Contact Person: Manisha Shukla
Website: …`") — a label/value layout with no sentence grammar for a
statistical model to work with. Reading the *label* instead of the grammar
is what a human does, and it turned the worst category into one of the
strongest.

**ADDRESS recall: 0.750 → 1.000.** Addresses were not merely being missed,
they were being *shredded*: with no whole-span match, the ORG and PERSON
detectors picked off the recognisable fragments and left the house number,
PIN code and state in cleartext between fake names. Three separate causes
had to be fixed — a capture bound that failed outright when a sentence
carried two addresses, an abbreviation ("Plot No.") read as a sentence end,
and addresses typeset one line per paragraph where no single line held
enough evidence on its own.

Neither failure appears in the synthetic corpus, which is written as prose
with one address per sentence. Both were found by reading the actual
redacted output.

---

## 5. Synthetic corpus — full type coverage

`evaluation/build_test_corpus.py` generates a prospectus-style document and
its ground truth **together**, recording each span at the exact offset it is
inserted at. 33 labelled spans across the core types, plus deliberate non-PII
look-alikes that must **not** be redacted (`Order #45678`,
`Ticket ID TCK-99881`, `Invoice No. INV-2024-1123`, an ISIN, a CIN, "the
Company", a filing date with no birth context) and three intentionally hard
cases that are expected misses.

Run: `python evaluation/run_evaluation.py`

| Metric | Value |
|---|---|
| Precision | **0.912** |
| Recall | **0.939** |
| F1 | **0.925** |
| Accuracy | **0.861** |

| Type | TP | FP | FN | Precision | Recall | F1 |
|---|---|---|---|---|---|---|
| AADHAAR_NUMBER | 1 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| ADDRESS | 3 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| CREDIT_CARD | 2 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| DATE_OF_BIRTH | 2 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| EMAIL | 5 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| IP_ADDRESS | 2 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| ORG | 3 | 1 | 1 | 0.750 | 0.750 | 0.750 |
| PAN_NUMBER | 1 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| PERSON | 6 | 2 | 1 | 0.750 | 0.857 | 0.800 |
| PHONE | 4 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| SSN | 2 | 0 | 0 | 1.000 | 1.000 | 1.000 |

Every **structured** type — where a validated regex applies (Luhn checksum,
libphonenumber parsing, SSN area rules, octet ranges) — scores 1.000. Those
detectors do not guess: they pattern-match and then verify. All imprecision
sits in PERSON/ORG/ADDRESS, where the ambiguity is genuine. That is the
right place for a redaction tool's error budget: visible, explainable, and
confined to categories where a human would also hesitate — rather than
silently missing a card number.

Known misses in this corpus are deliberate and documented: a single-word
brand with no corporate suffix ("Zomato"), an address with neither PIN code
nor lead-in phrase, and an informal username (`rahul_v88`).

## 6. Verified non-redactions

Explicitly checked as correctly left alone — these are what the brief warns
against over-redacting:

`Order #45678` · `Ticket ID TCK-99881` · `Invoice No. INV-2024-1123` ·
ISIN `INE0XYZ01011` · CIN `U28129PN1979PLC141032` · SEBI registration
numbers (`INM000013004`) · "the Company" · "Fiscal Year 2024" · filing dates
with no birth context · the issuer's own name · ~579 glossary terms parsed
from the document itself ("Equity Shares", "the Offer Price", "Anchor
Investors", "Promoter Group").

## 6a. Types not exercised by either corpus

Seven India-specific identifiers were added after the two corpora above were
built: IFSC, GSTIN, UPI ID, passport, voter ID, driving licence and bank
account number. **Neither corpus contains any of them**, so they carry *no*
precision/recall figure here, and it would be misleading to fold them into
the headline numbers.

They are instead covered by unit tests that assert both directions — the
detector fires on a valid instance, and does **not** fire on the near-miss
that would be a false positive:

| Type | Positive | Must NOT match | What does the work |
|---|---|---|---|
| IFSC | `HDFC0001234` | `ABCD1234567` | reserved `0` in position 5 |
| GSTIN | `27AAAPL1234C1Z5` | `271234AAAAC1Z5X` | embedded PAN must be well-formed |
| UPI ID | `rohan.dey@okhdfcbank` | `meet@noon`, `x@gmail.com` | PSP-handle allowlist; emails left to `EmailDetector` |
| Passport | `Passport No: M1234567` | `Reference M1234567` | requires nearby keyword |
| Voter ID | `Voter ID: ABC1234567` | `Batch ABC1234567` | requires nearby keyword |
| Driving licence | `Driving Licence No: MH12 20110012345` | `Batch MH12 20110012345` | requires nearby keyword |
| Bank account | `Account No: 123456789012` | `Order number 123456789012` | requires nearby keyword |

The last four are context-gated because the bare shape is indistinguishable
from the reference codes a formal document is full of. There is no confidence
level at which `M1234567` alone is safe to redact, so the label *is* the
evidence.

**One collision surfaced by these tests is worth recording.** A bank account
number and a driving licence are both phone-shaped enough that
libphonenumber accepts them, so `PhoneDetector` (0.95) won every overlap:
the value was redacted, but logged under the wrong type. Both detectors now
carry 0.96 — higher *because* they require an explicit label, which is
stronger evidence than a generic digit run. This is resolved through the
central confidence arbitration rather than by teaching `PhoneDetector` about
account labels, keeping detectors independent of one another.

## 6b. Verification pass on the delivered document

The residual scan (see ARCHITECTURE §6a) re-opens the finished file and
re-checks it. On the delivered prospectus:

```
LEAKS: none -- every original value the pipeline redacted is confirmed gone.
Also found 55 PII-shaped item(s) on a second pass, worth a skim.
```

The **0 leaks** is the load-bearing number: no value the pipeline claimed to
redact survives anywhere in the output — body, tables, headers, footers,
footnotes, endnotes, comments, or custom properties.

The 55 second-pass items are advisory and are *not* counted as failures.
They are the same NER imprecision the primary pass exhibits ("Chartered
Accountants", "Financial Data" — institutional language, not personal data),
re-surfacing when detection is run again over the output. An earlier version
of this check reported 45 additional "leaks" that were entirely section
headings; see ARCHITECTURE §6a for why that gating was added.

Confidence distribution across the 1,035 replacements:

| Tier | Count | Share |
|---|---|---|
| High (validated / deterministic) | 191 | 18% |
| Medium (statistical, ordinarily correct) | 811 | 78% |
| Needs review (inferred) | 33 | 3% |

## 7. What would move the numbers further

- **`en_core_web_lg` or a transformer pipeline.** The small model is a
  ~13 MB trade for fast cold starts on free-tier hosting. It misses entities
  outright in short, context-free strings — "ICICI Securities Limited" in a
  table cell returns no entity at all — which is why deterministic rules had
  to be added around it. A larger model would close part of that gap. One-line
  change in `get_nlp()`.
- **Single-word brand names with no legal form** ("CareEdge") remain the
  clearest known miss: there is no structural signal separating them from an
  ordinary capitalised noun, and a sweep on single words was tested and
  produced far too many false positives to keep.
- **US-style street addresses**, which carry neither a PIN code nor an Indian
  address keyword.
- **Substring-aware glossary matching**, so "ICDR Master Circular" is
  recognised as part of "SEBI ICDR Master Circular".
- **A larger labelled sample**, including the long cover-page tables, to put
  a real number on ORG recall — the one category this evaluation cannot
  currently measure.
