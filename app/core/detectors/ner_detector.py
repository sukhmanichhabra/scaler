"""
NER-based detectors for unstructured PII: person names, organisation names,
and physical addresses. Structured PII (emails, phones, etc.) is deliberately
NOT handled here -- see regex_detectors.py and the module docstring there for
why the split exists.
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import List, Optional

import spacy

from ..defined_terms import normalize_term
from .base import PIIDetector, PIIMatch

_NLP = None  # lazy-loaded singleton, shared across detector instances

# Only NER is used from the spaCy pipeline. The tagger/parser/lemmatizer
# components are pure overhead here -- disabling them cuts model runtime
# substantially on a long document without affecting entity output, since
# the NER component depends only on tok2vec.
_UNUSED_PIPES = ["tagger", "parser", "attribute_ruler", "lemmatizer"]


def get_nlp():
    global _NLP
    if _NLP is None:
        _NLP = spacy.load("en_core_web_sm", exclude=_UNUSED_PIPES)
    return _NLP


@lru_cache(maxsize=8)
def _analyze(text: str):
    """
    Cached spaCy analysis, shared across detectors.

    PersonNameDetector and OrganizationDetector both need entities from the
    SAME text and would otherwise each run the full pipeline over it --
    doubling the most expensive step in the system for no benefit. They run
    back-to-back per paragraph, so even a tiny cache collapses that to one
    model invocation. `PIIMatch` offsets are read straight off this Doc, so
    the cache must stay keyed on the exact text.
    """
    return get_nlp()(text)


# spaCy's small model frequently mistakes short, capitalised regulatory /
# organisational acronyms for a PERSON or ORG entity (e.g. "PAN", "SSN",
# "CFO", "KYC"). Since these acronyms are also exactly the label text that
# precedes the *real* PII our regex detectors already catch (e.g.
# "PAN: ABCDE1234F"), blocking them here costs us nothing on recall and
# meaningfully improves precision. Documented as a known tradeoff in the
# README: a legitimate short organisation name that happens to collide with
# this list (e.g. "IT" as a company name) would be missed.
_ACRONYM_STOPLIST = {
    "SSN", "PAN", "DOB", "CFO", "CEO", "COO", "CTO", "CFO", "HR", "IT", "IP",
    "PIN", "IFSC", "GST", "TAN", "CIN", "ISIN", "KYC", "ID", "AADHAAR",
    "NRI", "RHP", "SEBI", "NSE", "BSE", "IPO", "PAT", "EPS", "P&L",
    # Labels for the India-specific identifier types. Each of these is the
    # word that INTRODUCES a value the regex detectors already catch
    # ("GSTIN: 27AAAPL1234C1Z5"), so blocking the label costs no recall --
    # and without them spaCy reads the all-caps label itself as a name and
    # replaces "GSTIN:" with a fake person.
    "GSTIN", "UPI", "EPIC", "DL", "MICR", "NEFT", "RTGS", "IMPS", "NPCI",
    "UIDAI", "EPFO", "ESIC", "DIN", "LEI",
}

# Job titles / role names that spaCy's small model regularly mistakes for a
# PERSON entity when they appear capitalised and unattached to an actual
# name (e.g. "...contact the Compliance Officer for..."). If every word in
# the candidate span is a role/title word, it's treated as a false positive.
# Job titles / role names that spaCy's small model regularly mistakes for a
# PERSON entity when they appear capitalised and unattached to an actual
# name (e.g. "...contact the Compliance Officer for..."). If every
# *content* word (stopwords like "the"/"of" are ignored) in the candidate
# span is a role/title word, it's treated as a false positive.
_TITLE_TOKENS = {
    "compliance", "officer", "director", "directors", "managing",
    "independent", "company", "secretary", "statutory", "auditor",
    "auditors", "legal", "counsel", "chief", "chairman", "president",
    "registrar", "trustee", "advisor", "advisers", "advisors", "manager",
    "executive", "board", "issue", "lead",
    # Field labels from contact blocks. spaCy tags a bare "Email" or
    # "Website" as a PERSON surprisingly often in label/value layouts,
    # where there is no grammar to tell it otherwise.
    "email", "e-mail", "website", "web", "telephone", "tel", "fax",
    "contact", "person", "address", "grievance", "investor", "name",
}

# Public bodies, regulators and exchanges. These are named constantly in a
# regulatory filing, are emphatically not personal data, and redacting them
# destroys the document's meaning ("registered with <fake company> under the
# <fake company> Regulations"). Treated the same way as the issuer's own
# name: a deliberate, documented non-redaction. Private companies that
# merely sound official are unaffected -- this list is explicit.
_PUBLIC_BODIES = {
    "securities and exchange board of india", "sebi",
    "reserve bank of india", "rbi",
    "national stock exchange of india limited", "national stock exchange",
    "bse limited", "bombay stock exchange", "stock exchanges",
    "registrar of companies", "ministry of corporate affairs",
    "government of india", "income tax department",
    "national payments corporation of india", "npci",
    "insurance regulatory and development authority",
}

# Reference/label phrases ("Invoice No.", "Order Reference") that precede a
# code but aren't an organisation themselves.
_LABEL_TOKENS = _TITLE_TOKENS | {
    "invoice", "order", "reference", "ref", "case", "docket", "ticket",
    "no", "number", "id",
}

_STOPWORDS = {"the", "of", "and", "to", "a", "an", "for", "in", "on"}

# Generic offer/process nouns from securities-filing boilerplate ("the Offer
# Price", "Promoter Selling Shareholders", "Bid cum Application Form") that
# spaCy sometimes mistags as PERSON/ORG on a single occurrence. These aren't
# excluded from ordinary per-occurrence detection (that's too blunt --
# it would also exclude the rare cases where the model gets it right), but
# they DO gate the cross-document name-consistency sweep in `registry.py`:
# that sweep propagates an already-detected name to every other casing
# variant of the same string found anywhere in the document, so a single
# stray false-positive tag on a common word like "Offer" or "Bid" would
# otherwise get replicated across every one of its hundreds of plain-prose
# occurrences. Requiring 2+ words already blocks single-word terms; this
# stoplist additionally blocks multi-word defined terms built from them.
_PROCESS_TERM_TOKENS = {
    "offer", "bid", "bidder", "bidders", "promoter", "promoters", "selling",
    "shareholder", "shareholders", "equity", "share", "shares", "investor",
    "investors", "anchor", "proceeds", "price", "facility", "portion",
    "amount", "account", "period", "date", "report", "statement",
    "statements", "personnel", "managerial", "fund", "funds", "email",
    "bank", "group", "trust", "institutional", "retail", "qualified",
    "net", "key", "cap", "floor", "restated", "financial", "form",
}


def _is_capitalised_multiword(span_text: str) -> bool:
    """2+ words, every alphabetic word capitalised, no stopwords."""
    words = [w.strip(",.'’-") for w in span_text.split()]
    words = [w for w in words if w]
    if len(words) < 2:
        return False
    if not all(w[0].isupper() for w in words if w[0].isalpha()):
        return False
    return not any(w.lower() in _STOPWORDS for w in words)


def is_org_sweep_eligible(span_text: str) -> bool:
    """
    Organisation gate for the consistency sweep.

    Two deliberate differences from the person gate:

    - No offer/process-noun filter. Those very words -- "bank", "capital",
      "trust" -- are exactly what real company names end in, so the filter
      would reject "HDFC Bank" outright. The corporate-suffix requirement in
      `registry._SWEEPABLE_ORG_SUFFIX` does that job instead, and does it
      better: a document heading never carries a legal form.
    - Internal lowercase connectors are allowed, because company names
      routinely contain them ("State Bank of India", "Board of Trustees").
      Only the first and last words must be capitalised. This is safe for
      the same reason: the suffix gate still has to pass.
    """
    words = [w.strip(",.'’-") for w in span_text.split()]
    words = [w for w in words if w]
    if len(words) < 2:
        return False
    edges = (words[0], words[-1])
    if not all(w[0].isupper() for w in edges if w[0].isalpha()):
        return False
    return any(c.isalpha() for c in words[0]) and any(c.isalpha() for c in words[-1])


def is_sweep_eligible(span_text: str) -> bool:
    """
    Gate for the cross-document name-consistency sweep (see
    `registry.sweep_known_names`): only a genuine multi-word proper name
    (e.g. "Kushal Subbayya Hegde") should be propagated to every casing
    variant found elsewhere in the document. Three structural requirements,
    each found necessary by testing against a real filing:
      - 2+ words: blocks single generic words ("Offer", "Bid") from being
        propagated to every one of their hundreds of ordinary-prose uses.
      - every word capitalised: real names are conventionally Title Case or
        ALL-CAPS in full; this excludes phrases spaCy mistagged where only
        SOME words are capitalised ("Wilful defaulter", "widely circulated
        Marathi daily newspaper") -- a pattern that doesn't occur in actual
        personal names.
      - none of the words are a known offer/process noun -- catches multi-
        word DEFINED TERMS from securities-filing boilerplate that ARE
        fully capitalised ("Key Managerial Personnel", "Anchor Investors")
        and so survive the previous check.
    Deliberately does not gate ORG the same way: this document's section/
    document-title headings ("Red Herring Prospectus", "Capital Structure",
    "Registered Office") are indistinguishable from real company names by
    any of the above structural signals, so ORG is excluded from the sweep
    entirely in `registry.build_known_names` rather than risk propagating a
    heading as a fake company name across the whole document.
    """
    if not _is_capitalised_multiword(span_text):
        return False
    words = [w.strip(",.'’-").lower() for w in span_text.split()]
    return not any(w in _PROCESS_TERM_TOKENS for w in words if w)

# Reference/ticket-style codes ("TCK-99881", "ORD45678") are exactly the
# kind of non-PII the assignment calls out by name -- a capitalised phrase
# that merely CONTAINS one should never be classified as an organisation.
_REFERENCE_CODE = re.compile(r"\b[A-Za-z]{1,6}-?\d{3,}\b")


# Filings freely derive new words from defined terms ("pre-Offer",
# "post-Offer", "sub-Bid"), which the glossary lists only in base form.
_DERIVED_PREFIX = re.compile(r"^(pre|post|non|sub|re)[-\s]+", re.IGNORECASE)


def _matches_defined_term(span_text: str, defined_terms: set) -> bool:
    """True when the span is one of the document's own glossary terms."""
    if not defined_terms:
        return False
    normalized = normalize_term(span_text)
    if normalized in defined_terms:
        return True
    stripped = _DERIVED_PREFIX.sub("", normalized).strip()
    return bool(stripped) and stripped in defined_terms


def _is_acronym_false_positive(span_text: str) -> bool:
    token = span_text.strip().rstrip(":").upper()
    return token in _ACRONYM_STOPLIST


def _content_words(span_text: str):
    return [w.lower().strip(",.'’s") for w in span_text.split()
            if w.lower().strip(",.'’s") not in _STOPWORDS]


def _is_title_phrase(span_text: str) -> bool:
    words = _content_words(span_text)
    return bool(words) and all(w in _TITLE_TOKENS for w in words)


def _is_label_phrase(span_text: str) -> bool:
    words = _content_words(span_text)
    return bool(words) and all(w in _LABEL_TOKENS for w in words)


class PersonNameDetector(PIIDetector):
    """
    Person names from spaCy NER, plus a structural rule for contact blocks.

    The NER model needs sentence context to recognise a name, and a filing's
    contact blocks provide none -- "Contact Person: Manisha Shukla Website:
    www..." is a run of labels and values, not prose. Measured against
    hand-labelled ground truth from the real prospectus, that single layout
    accounted for half of all missed names. The lead-in rule below reads the
    label instead of the grammar, which is exactly the cue a human uses.
    """

    name = "person_name"

    # "Contact Person:", "Compliance Officer:" etc. introduce one or more
    # names. Anchored on the colon so it only fires on label/value layouts.
    _CONTACT_LEADIN = re.compile(
        r"(?:Contact\s+Person(?:\(s\))?|Contact|Compliance\s+Officer|"
        r"Company\s+Secretary|Name\s+of\s+(?:the\s+)?Contact\s+Person)\s*[:\-]\s*",
        re.IGNORECASE,
    )
    # The value ends where the next field label begins.
    _FIELD_BOUNDARY = re.compile(
        r"\b(Website|Web|Email|E-?mail|Telephone|Tel|Fax|SEBI|Investor|Address|"
        r"CIN|Registration|Registered|Corporate|Contact)\b",
        re.IGNORECASE,
    )
    # A plausible personal name: 2-4 capitalised words, allowing initials
    # ("R. K. Sharma") and hyphenated surnames.
    _NAME_SHAPE = re.compile(
        r"\b([A-Z][A-Za-z.'’-]*(?:\s+[A-Z][A-Za-z.'’-]*){1,3})"
    )

    def __init__(self, defined_terms: Optional[set] = None):
        # Per-document glossary of defined terms -- see `defined_terms.py`.
        self.defined_terms = defined_terms or set()

    def _contact_block_names(self, text: str) -> List[PIIMatch]:
        matches: List[PIIMatch] = []
        for lead in self._CONTACT_LEADIN.finditer(text):
            region_start = lead.end()
            region = text[region_start:region_start + 160]
            boundary = self._FIELD_BOUNDARY.search(region)
            if boundary:
                region = region[:boundary.start()]
            if not region.strip():
                continue
            # Several people often share one label, separated by "/" or ",".
            for candidate in self._NAME_SHAPE.finditer(region):
                span_text = candidate.group(1).strip()
                words = span_text.split()
                if not (2 <= len(words) <= 4):
                    continue
                if _is_title_phrase(span_text) or _is_acronym_false_positive(span_text):
                    continue
                if _matches_defined_term(span_text, self.defined_terms):
                    continue
                start = region_start + candidate.start(1)
                matches.append(
                    PIIMatch("PERSON", span_text, start, start + len(span_text), 0.8, "heuristic")
                )
        return matches

    def detect(self, text: str) -> List[PIIMatch]:
        doc = _analyze(text)
        matches = self._contact_block_names(text)
        for ent in doc.ents:
            if ent.label_ == "PERSON":
                # spaCy occasionally grabs a trailing possessive "'s" or a
                # leading honorific space; trim whitespace-only edges.
                span_text = ent.text.strip()
                if _is_acronym_false_positive(span_text) or _is_title_phrase(span_text):
                    continue
                if _matches_defined_term(span_text, self.defined_terms):
                    continue
                if normalize_term(span_text) in _PUBLIC_BODIES:
                    continue
                if len(span_text.split()) >= 1 and any(c.isalpha() for c in span_text):
                    start = ent.start_char + (ent.text.find(span_text))
                    matches.append(
                        PIIMatch("PERSON", span_text, start, start + len(span_text), 0.85, "ner")
                    )
        return matches


class OrganizationDetector(PIIDetector):
    """
    Detects company / organisation names via spaCy's ORG label.
    `issuer_names`, if provided, lets the caller EXCLUDE the filing company's
    own name from redaction (a prospectus is inherently *about* that company,
    so blanket-redacting it everywhere often makes the document useless --
    see README for this tradeoff discussion). Pass an empty set to redact
    every organisation name found, including the issuer.
    """

    name = "organization"
    # Real organisation mentions in a formal document overwhelmingly either
    # span 2+ words ("Kapoor Legal Partners"), carry a corporate suffix
    # ("HDFC Bank"), or are a clean all-caps acronym ("SEBI" -- caught
    # separately by the stoplist above when it's a *known* generic one).
    # A single generic capitalised word with none of these ("Grievances",
    # "Registrar") is far more often a spaCy false positive on formal/legal
    # prose than a real company name -- so it's filtered here. Documented
    # tradeoff: a genuine single-word brand with no suffix (e.g. "Google",
    # "Infosys") would be missed by this rule alone.
    _CORP_SUFFIX = re.compile(
        r"\b(Ltd|Limited|LLP|Pvt|Inc|Corp|Corporation|Bank|Group|Partners|"
        r"Advisors|Associates|Company|Co\.|PLC|LLC)\b",
        re.IGNORECASE,
    )

    # Deterministic company-name rule, run alongside NER.
    #
    # spaCy's small model silently misses company names in short,
    # context-free strings -- "ICICI Securities Limited" in a table cell
    # returns no entity at all, and "Link Intime India Private Limited"
    # comes back missing its first word. The consistency sweep can't help,
    # because it can only propagate a name the model tagged *somewhere*.
    #
    # But a run of capitalised words ending in a legal form IS a company
    # name; that is what the legal form means. Matching it directly needs no
    # model and no context, so it catches exactly the cases NER drops. The
    # usual filters (glossary terms, issuer names) still apply to the result.
    # "&" is allowed as a word of its own so partnership names survive
    # intact -- without it "Kirtane & Pandit LLP" matches only "Pandit LLP"
    # and, being the higher-confidence match, suppresses the NER span that
    # had the name right, leaving "Kirtane &" in cleartext.
    _LEGAL_FORM_NAME = re.compile(
        r"\b((?:(?:[A-Z][\w&.\-]*|&)\s+){1,6}?"
        r"(?:Private\s+|Pvt\.?\s+)?"
        r"(?:Limited|Ltd\.?|LLP|LLC|PLC|Incorporated|Inc\.?|Corporation|Corp\.?))"
        r"(?!\w)"
    )
    # Capitalised words that introduce a company name without being part of
    # it: "(Formerly Link Intime ... Limited)", "namely, Acme Limited".
    _LEADING_CONNECTORS = {"formerly", "namely", "erstwhile", "and", "or",
                           "the", "our", "its", "viz", "including"}

    # Company indicators strong enough to trust inside a heading, where
    # statistical NER is switched off for being too noisy.
    _STRONG_ORG_WORD = re.compile(
        r"\b(Ltd|Limited|LLP|Pvt|Private|Inc|Corp|Corporation|PLC|LLC|"
        r"Bank|Securities|Insurance|Partners|Associates|Advisors|Advisers|"
        r"Consultants|Industries|Technologies|Enterprises|Ventures)\b",
        re.IGNORECASE,
    )

    def __init__(self, issuer_names: Optional[set] = None, defined_terms: Optional[set] = None,
                 strict: bool = False):
        # `strict` is used for headings: keep the deterministic rules but
        # require a strong company indicator, so "State Bank of India,
        # Industrial Finance Branch" is still caught while "Capital
        # Structure" and "Board of Directors" are not.
        self.strict = strict
        self.issuer_names = {n.lower() for n in (issuer_names or set())}
        # Per-document glossary of defined terms -- see `defined_terms.py`.
        # This is where it earns its keep: "Equity Shares", "the Offer
        # Price" and friends are Title Case multi-word phrases that spaCy
        # reasonably but wrongly reads as organisations.
        self.defined_terms = defined_terms or set()

    def _is_excluded(self, span_text: str) -> bool:
        """Filters shared by the NER and legal-form paths."""
        if _is_acronym_false_positive(span_text):
            return True
        if _matches_defined_term(span_text, self.defined_terms):
            return True
        if normalize_term(span_text) in _PUBLIC_BODIES:
            return True
        if _REFERENCE_CODE.search(span_text):
            return True
        if _is_label_phrase(span_text):
            return True
        lowered = span_text.lower().strip()
        if lowered in {"company", "the company", "issuer", "the issuer", "the corporation"}:
            return True
        if self.strict and not self._STRONG_ORG_WORD.search(span_text):
            return True
        return any(lowered in issuer or issuer in lowered for issuer in self.issuer_names)

    def _legal_form_matches(self, text: str) -> List[PIIMatch]:
        found = []
        for m in self._LEGAL_FORM_NAME.finditer(text):
            span_text = m.group(1).strip()
            start = m.start(1)
            # Drop introducing words that aren't part of the name itself.
            while True:
                words = span_text.split()
                if len(words) > 2 and words[0].lower().strip(",.") in self._LEADING_CONNECTORS:
                    start += len(words[0]) + 1
                    span_text = span_text[len(words[0]) + 1:].lstrip()
                    continue
                break
            # A bare legal form with nothing in front of it names no one.
            if len(span_text.split()) < 2 or self._is_excluded(span_text):
                continue
            found.append(
                PIIMatch("ORG", span_text, start, start + len(span_text), 0.9, "heuristic")
            )
        return found

    def detect(self, text: str) -> List[PIIMatch]:
        doc = _analyze(text)
        matches = self._legal_form_matches(text)
        for ent in doc.ents:
            if ent.label_ != "ORG":
                continue
            span_text = ent.text.strip()
            if not span_text:
                continue
            if self._is_excluded(span_text):
                continue
            words = span_text.split()
            is_multiword = len(words) >= 2
            has_suffix = bool(self._CORP_SUFFIX.search(span_text))
            is_acronym = span_text.isupper() and 2 <= len(span_text) <= 6
            if not (is_multiword or has_suffix or is_acronym):
                continue
            start = ent.start_char + (ent.text.find(span_text))
            matches.append(
                PIIMatch("ORG", span_text, start, start + len(span_text), 0.8, "ner")
            )
        return matches


class AddressDetector(PIIDetector):
    """
    Physical/mailing addresses are handled with pattern rules rather than
    generic NER, for a specific reason found during testing: naively
    treating "any line/sentence containing a PIN code" as one giant address
    span tends to swallow neighbouring PII (names, emails, phone numbers)
    that happen to share the same sentence or paragraph, and letting bare
    NER location entities ("US", "India", "Bangalore" on their own) count as
    "addresses" hurts precision without protecting anyone's privacy -- a
    lone city or country name doesn't identify a specific individual.

    Instead this uses three rules:
      1. Lead-in phrase rule: after words like "situated at", "located at",
         "Registered Office:", capture the clause that follows and keep it
         only if it contains a PIN code or a street-address keyword.
         Capturing starts *after* the lead-in, so a preceding "Rohan Dey,
         aged 42," is naturally excluded from the address span.
      2. Field-label rule: a table cell whose column header or row label
         names an address field ("Registered Office", "Corporate Office",
         "Address") IS an address, so the whole cell is taken. The label
         lives in a different cell from the value, which is why this needs
         the structural `context` rather than the cell text alone.
      3. PIN-code + keyword rule: a clause carrying both a 6-digit Indian
         PIN code and an address keyword, for addresses with neither a
         lead-in nor a labelled column.

    Getting a WHOLE address matters more than it first appears. When no rule
    fires, the address does not simply survive intact -- the ORG and PERSON
    detectors pick off its recognisable fragments ("Village Birdewadi" reads
    as an organisation), producing a mangled line where fake names sit
    between the real house number, PIN code and state. That is worse than
    either redacting or ignoring it cleanly, and it is why these rules err
    toward claiming the entire span: ADDRESS outranks PERSON/ORG in
    `resolve_overlaps`, so a whole-span match suppresses the fragments.

    Known limitation: an address with no PIN code, no lead-in phrase and no
    recognised keyword (e.g. a bare US-style street address) is still missed.
    """

    name = "address"
    accepts_context = True
    # Indian PIN codes are commonly typeset either contiguous ("410501") or
    # space/hyphen-split in two groups of three ("410 501", "410-501") --
    # both are the same 6-digit code, so both must match or the space-split
    # form (very common in real filings) silently fails to register as a PIN.
    _PIN_CODE = re.compile(r"\b\d{3}[\s-]?\d{3}\b")
    # Widened against the real filing, where the original short list matched
    # none of the actual addresses: Indian addresses lean on administrative
    # units (Village, Taluka, District), premises names (Tower, Centre,
    # Complex, Estate) and relative markers (Off, Near, Opposite) far more
    # than on the "Street/Avenue" vocabulary of a US address.
    _ADDRESS_KEYWORDS = re.compile(
        r"\b("
        # thoroughfares
        r"Road|Rd|Street|St|Lane|Marg|Path|Avenue|Highway|Bypass|Cross|Circle|Gali|"
        # administrative units
        r"Village|Taluka|Tehsil|District|Dist|Post|Mandal|Ward|Sector|Phase|Zone|"
        # premises / built form
        r"Tower|Towers|Building|Bldg|Centre|Center|Complex|Estate|Industrial|Park|"
        r"Plot|Gat|Survey|Khasra|Floor|Wing|Unit|Premises|Compound|House|Bhavan|"
        r"Chambers|Mansion|Heights|Residency|Arcade|Plaza|Apartments?|Block|"
        # locality suffixes common in Indian place names
        r"Nagar|Colony|Layout|Society|Vihar|Puram|Pura|Bagh|Enclave|Garden|Gardens|"
        r"Wadi|Peth|Chowk|Bazaar|Bazar|Market|Farms|Farm|Extension|Annexe|Annex|"
        # relative markers that almost only appear inside an address
        r"Opposite|Opp|Behind|Near|Beside|Adjacent"
        r")\b",
        re.IGNORECASE,
    )
    # Column headers / row labels that declare the cell beneath them to be an
    # address. Anchored to the start so a passing mention inside prose (e.g.
    # "changes to the registered office require...") doesn't qualify.
    _ADDRESS_FIELD_LABEL = re.compile(
        r"^\s*(?:our\s+|the\s+)?"
        r"(registered\s+office|corporate\s+office|head\s+office|branch\s+office|"
        r"registered\s+address|correspondence\s+address|office\s+address|address)"
        r"\b",
        re.IGNORECASE,
    )
    # Deliberately only the precise verb-phrase forms ("situated at",
    # "located at", ...), not coarser prefixes like "registered office" on
    # their own -- an earlier version matched "registered office" as soon as
    # it appeared and then swallowed everything up to the period, including
    # "of the Company is situated at" itself, into the replaced text. Since
    # finditer takes the leftmost match, a coarse alternative earlier in the
    # sentence would win over the precise one that follows it. The
    # colon-anchored "registered/corporate office:" label form is safe to add
    # despite that history because it only fires when the colon immediately
    # follows the label (a structured "Field: value" listing), which never
    # overlaps with the "office ... is situated at" prose form that caused
    # the original bug.
    # The terminator is a lookahead that also accepts end-of-text. Requiring
    # an actual "." / ";" / newline meant an address ending a table cell --
    # "...located at 11/3, Village Birdewadi, Pune - 410 501, Maharashtra,
    # India" with no trailing period -- never matched at all, and the ORG and
    # PERSON detectors then shredded it into fragments.
    # Only the lead-in PHRASE is matched here; where the address ends is
    # decided afterwards by `_trim_address_clause`. An earlier version tried
    # to express the end as a bounded capture with a required terminator,
    # which failed outright whenever no terminator appeared inside the bound
    # -- as in "...Registered Office at <90-char address> and its Corporate
    # Office at <address>.", a single sentence carrying two addresses. The
    # match failing meant no ADDRESS at all, and the ORG/PERSON detectors
    # then shredded the first address into fake names. Matching the phrase
    # alone also lets `finditer` find BOTH lead-ins, since the match no
    # longer spans the text between them.
    _LEADIN = re.compile(
        r"(?:situated at|located at|resides?\s+at|residing at|office at|"
        r"registered address(?:\s+is)?:?|address(?:\s+is)?:?|"
        r"(?:registered|corporate) office\s*:|"
        # contact blocks introduce the address the same way
        r"contact details?[^:\n]{0,40}:)\s+",
        re.IGNORECASE,
    )
    _SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

    #: How far past a lead-in phrase to look for the end of the address.
    _CLAUSE_WINDOW = 240

    # A conjunction introducing a *different* field ends the address:
    # "...Maharashtra, India and its Corporate Office at ..."
    _NEXT_FIELD = re.compile(r"\s+and\s+(?:its|our|the)\s+", re.IGNORECASE)
    # A period ends it only when it is a real sentence end -- Indian
    # addresses are full of abbreviations ("Plot No. J-25", "Survey No. 12")
    # and treating those as the boundary truncated the span to "Plot No".
    # Lookbehinds are fixed-width, as Python's `re` requires.
    _SENTENCE_END = re.compile(
        r"(?<!No)(?<!Nos)(?<!Sr)(?<!Jr)(?<!Dr)(?<!St)(?<!Rd)(?<!Mr)(?<!Ms)\.(?:\s|$)"
    )

    def _trim_address_clause(self, window: str) -> str:
        """Cuts the text following a lead-in phrase down to just the address."""
        for hard_break in ("\n", ";"):
            index = window.find(hard_break)
            if index != -1:
                window = window[:index]
        next_field = self._NEXT_FIELD.search(window)
        if next_field:
            window = window[:next_field.start()]
        sentence_end = self._SENTENCE_END.search(window)
        if sentence_end:
            window = window[:sentence_end.start()]
        return window.rstrip(" ,.;:")

    def _looks_like_address(self, text: str) -> bool:
        return bool(self._PIN_CODE.search(text) or self._ADDRESS_KEYWORDS.search(text))

    def detect(self, text: str, context: str = "") -> List[PIIMatch]:
        matches: List[PIIMatch] = []

        # Rule 1: lead-in phrase -> take the clause that follows it
        for m in self._LEADIN.finditer(text):
            start = m.end()
            clause = self._trim_address_clause(text[start:start + self._CLAUSE_WINDOW])
            if len(clause) >= 5 and self._looks_like_address(clause):
                end = start + len(clause)
                matches.append(PIIMatch("ADDRESS", text[start:end], start, end, 0.9, "heuristic"))

        # Rule 2: the surrounding structure labels this cell as an address.
        # The label ("Registered Office") sits in the header cell, a
        # different cell from the value, so only `context` can see it.
        if not matches and context and self._ADDRESS_FIELD_LABEL.search(context):
            stripped = text.strip()
            if len(stripped) >= 12 and self._looks_like_address(stripped):
                start = text.find(stripped)
                matches.append(
                    PIIMatch("ADDRESS", stripped, start, start + len(stripped), 0.88, "heuristic")
                )

        # Rule 2b: one line of an address split across consecutive lines.
        # Each line alone is too thin to recognise -- "Pune - 410 501" has a
        # PIN but no street keyword; "11/3, Village Birdewadi" the reverse --
        # so the evidence is pooled with the neighbouring lines while the
        # match itself stays within this line.
        if not matches and context:
            stripped = text.strip()
            is_line_fragment = (
                0 < len(stripped) <= 120
                and not stripped.endswith(".")          # a fragment, not prose
                and ("," in stripped or any(c.isdigit() for c in stripped))
            )
            if (is_line_fragment
                    and self._looks_like_address(stripped)
                    and self._PIN_CODE.search(f"{context} {stripped}")):
                start = text.find(stripped)
                matches.append(
                    PIIMatch("ADDRESS", stripped, start, start + len(stripped), 0.82, "heuristic")
                )

        covered = [(a.start, a.end) for a in matches]

        # Rule 2: PIN code + keyword together, sentence-scoped, for addresses
        # with no lead-in phrase (e.g. inside a table cell)
        offset = 0
        for sentence in self._SENTENCE_SPLIT.split(text):
            s_start = text.find(sentence, offset)
            if s_start == -1:
                s_start = offset
            s_end = s_start + len(sentence)
            offset = s_end
            if any(cs <= s_start and s_end <= ce for cs, ce in covered):
                continue
            if self._PIN_CODE.search(sentence) and self._ADDRESS_KEYWORDS.search(sentence):
                stripped = sentence.strip()
                if len(stripped) < 8:
                    continue
                local_start = s_start + sentence.find(stripped)
                matches.append(
                    PIIMatch("ADDRESS", stripped, local_start, local_start + len(stripped), 0.8, "heuristic")
                )

        return matches
