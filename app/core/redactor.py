"""
Orchestrates detection (registry.py) + consistent replacement (faker_mapper.py)
over a block of text, and produces a structured report of everything it did
(useful for the evaluation harness and for an audit trail).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .detectors.base import DetectionConfig, PIIMatch, confidence_tier
from .detectors.registry import run_all_detectors
from .faker_mapper import ConsistentFaker


@dataclass
class RedactionResult:
    redacted_text: str
    matches: List[PIIMatch] = field(default_factory=list)
    replacements: List[dict] = field(default_factory=list)  # audit log: original -> fake
    # Disclosed coverage gaps for THIS document -- e.g. "3 embedded image(s)
    # detected; image text is not scanned for PII" -- surfaced to the
    # caller (CLI output, API response, web UI) rather than silently
    # producing a result that looks complete but isn't. Empty for .txt/.pdf
    # input, which has no such hidden-content surface to warn about.
    warnings: List[str] = field(default_factory=list)
    # Populated by `verification.verify_docx` after the output file is
    # written -- a re-check of the FINISHED file, not a claim made about it
    # in advance. None until that check has actually run.
    residual: Optional["ResidualScanReport"] = None


class Redactor:
    def __init__(self, config: Optional[DetectionConfig] = None, seed: int = 42,
                 issuer_names: Optional[set] = None, defined_terms: Optional[set] = None):
        self.config = config or DetectionConfig()
        self.faker = ConsistentFaker(seed=seed)
        self.issuer_names = issuer_names or set()
        # Per-document glossary of non-PII defined terms, if the document
        # ships one -- see `app/core/defined_terms.py`.
        self.defined_terms = defined_terms or set()

    def detect(self, text: str, skip_ner: bool = False,
               known_names: Optional[Dict[str, str]] = None,
               context: str = "") -> List[PIIMatch]:
        """Run detection only (no replacement) -- used directly by the
        evaluation harness to score precision/recall against ground truth.

        `context` carries surrounding structural text (e.g. a table cell's
        row label and column header) for detectors that opt into it."""
        return run_all_detectors(text, self.config, issuer_names=self.issuer_names,
                                  skip_ner=skip_ner, known_names=known_names,
                                  defined_terms=self.defined_terms, context=context)

    def redact_matches(self, text: str, matches: List[PIIMatch]) -> RedactionResult:
        """
        Replacement step only, over matches that were detected earlier.
        Kept separate from `redact` so the document pipeline can detect once
        and replace once, instead of re-running the (expensive) NER model.
        """
        # replace back-to-front so earlier offsets stay valid as we edit
        redacted = text
        replacements = []
        for m in sorted(matches, key=lambda x: -x.start):
            fake_value = self.faker.fake_for(m.entity_type, m.text)
            redacted = redacted[:m.start] + fake_value + redacted[m.end:]
            replacements.append({
                "type": m.entity_type,
                "original": m.text,
                "fake": fake_value,
                "confidence": m.confidence,
                "source": m.source,
                "confidence_tier": confidence_tier(m.confidence),
            })
        replacements.reverse()  # restore original document order for the audit log
        return RedactionResult(redacted_text=redacted, matches=matches, replacements=replacements)

    def redact(self, text: str, skip_ner: bool = False,
               known_names: Optional[Dict[str, str]] = None) -> RedactionResult:
        matches = self.detect(text, skip_ner=skip_ner, known_names=known_names)
        return self.redact_matches(text, matches)
