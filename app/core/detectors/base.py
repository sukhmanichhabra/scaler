"""
Base abstractions for the PII detection plugin system.

To add a new PII type:
  1. Create a new class that inherits from `PIIDetector`.
  2. Implement `detect(text) -> list[PIIMatch]`.
  3. Register it in `app/core/detectors/registry.py`.
That's it -- the rest of the pipeline (faking, replacement, evaluation)
works with any detector that follows this interface.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

# Single source of truth for every PII type the pipeline knows about. The
# CLI, the API, `DetectionConfig`'s default, and both evaluation harnesses
# all import this rather than each keeping their own copy -- previously
# three separate hardcoded copies had to be kept in sync by hand every time
# a type was added, which is exactly the kind of thing that quietly drifts.
#
# The nine ahead of the line are the assignment's required minimum. Everything
# from PAN_NUMBER on is a bonus type relevant to Indian filings specifically;
# see each detector's docstring in regex_detectors.py for why it's enabled by
# default rather than opt-in, and for the context-gated ones (PASSPORT_NUMBER,
# VOTER_ID, DRIVING_LICENSE, BANK_ACCOUNT_NUMBER), why a bare pattern match
# alone is deliberately not enough to fire.
ALL_PII_TYPES = frozenset({
    "PERSON", "EMAIL", "PHONE", "ORG", "ADDRESS", "SSN",
    "CREDIT_CARD", "DATE_OF_BIRTH", "IP_ADDRESS",
    "PAN_NUMBER", "AADHAAR_NUMBER",
    "IFSC_CODE", "GSTIN", "UPI_ID",
    "PASSPORT_NUMBER", "VOTER_ID", "DRIVING_LICENSE", "BANK_ACCOUNT_NUMBER",
})


@dataclass(frozen=True)
class PIIMatch:
    """A single detected PII span."""

    entity_type: str          # e.g. "EMAIL", "PERSON", "PHONE"
    text: str                 # the exact substring that was matched
    start: int                # character offset (inclusive) in the source text
    end: int                  # character offset (exclusive) in the source text
    confidence: float = 1.0   # 0.0 - 1.0, used to break ties on overlapping spans
    source: str = "regex"     # which detector produced this ("regex" | "ner" | "heuristic")

    def __len__(self) -> int:
        return self.end - self.start


#: Thresholds calibrated against every confidence value actually assigned
#: across the detectors (see `regex_detectors.py`, `ner_detector.py`,
#: `registry.py`): the validated structured types (email, phone, SSN,
#: credit card, IP, DOB, PAN) and the two most reliable heuristic rules sit
#: at 0.9+; ordinary NER and most address heuristics land at 0.8-0.89; the
#: consistency sweep (0.75 -- a name propagated by inference, not directly
#: detected at this exact spot) and Aadhaar-without-context (0.6 -- a
#: 12-digit run that could equally be a phone number) fall below 0.8.
_HIGH_CONFIDENCE_THRESHOLD = 0.9
_MEDIUM_CONFIDENCE_THRESHOLD = 0.8

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_NEEDS_REVIEW = "needs_review"


def confidence_tier(confidence: float) -> str:
    """
    Buckets a match's confidence into a tier meant to be read by a human
    deciding how much to trust the output, not just a number in an audit
    log: HIGH is safe to trust without a second look (validated structured
    PII, or a deterministic rule like a corporate legal-form match);
    MEDIUM is a statistical model's normal-confidence guess, right far more
    often than not; NEEDS_REVIEW is exactly what it says -- a match found
    by inference rather than direct, strong evidence, worth a human glance
    before the document goes out.
    """
    if confidence >= _HIGH_CONFIDENCE_THRESHOLD:
        return CONFIDENCE_HIGH
    if confidence >= _MEDIUM_CONFIDENCE_THRESHOLD:
        return CONFIDENCE_MEDIUM
    return CONFIDENCE_NEEDS_REVIEW


class PIIDetector:
    """Abstract base class every detector plugin must implement."""

    #: short, unique identifier used in reports / registry lookups
    name: str = "base"

    #: Opt-in flag for detectors that need surrounding document structure
    #: rather than just the text block they're scanning. When True the
    #: registry calls `detect(text, context=...)` with nearby structural
    #: text -- for a table cell, its row label and column header.
    #: Defaults to False so a new detector only has to implement
    #: `detect(text)`, keeping the plugin contract as simple as before.
    accepts_context: bool = False

    def detect(self, text: str) -> List[PIIMatch]:  # pragma: no cover - interface
        raise NotImplementedError


@dataclass
class DetectionConfig:
    """Toggle individual entity types on/off without touching detector code."""

    enabled_types: set = field(default_factory=lambda: set(ALL_PII_TYPES))
    # DESIGN CHOICE (see README "Tradeoffs"): defaults to False, meaning the
    # issuing/subject company's own name is left untouched, while every
    # OTHER organisation mentioned (auditors, banks, legal counsel, etc.)
    # is still redacted. A prospectus is inherently about one named company;
    # blanket-redacting that name throughout would make the output useless
    # while adding little privacy benefit, since the document's very
    # existence and public filing already identify the company. Pass
    # `redact_issuer_company=True` (and no issuer_names) to redact it too.
    redact_issuer_company: bool = False
    min_confidence: float = 0.5
