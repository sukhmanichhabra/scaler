"""
Residual scan: re-checks the FINISHED output file for PII, rather than
trusting that whatever the redaction pass did was complete.

Why a tool should check its own work
-------------------------------------
Every fix made to this pipeline over its development was found the same
way: by re-reading the actual output and comparing it against the source,
not by reasoning about the code in the abstract. That process -- "does
this original value still appear in the delivered file?" -- is exactly
mechanical enough to run automatically on every redaction, not just when
someone happens to go looking. This module is that check, formalized.

Two independent passes, because they catch different failure classes:

1. **Leak check** (`find_leaked_originals`): for every value the pipeline
   believed it redacted, does that exact original text still appear
   ANYWHERE in the final file? This is cheap, precise, and has zero false
   positives -- if it fires, something that was supposed to be gone is
   still there. It cannot, however, catch PII the detectors never found
   in the first place.

2. **Unexpected-PII re-scan** (`find_unexpected_pii`): re-runs detection
   over the ENTIRE finished document as one pass, independent of the
   paragraph-by-paragraph structure the main pipeline used. A span split
   exactly across a paragraph boundary, or a context-gated match (a date
   next to a birth keyword in a DIFFERENT paragraph than the one it was
   scored in) can behave differently at document scope than at paragraph
   scope -- so this is a genuinely independent second opinion, not a
   re-run of the same check. It is a heuristic safety net, not a
   guarantee: the fake replacement values are themselves PII-shaped by
   design (a fake email is still shaped like an email), so every KNOWN
   fake value is excluded before anything is reported, and what's left
   after that exclusion is inherently best-effort, not exhaustive.

Both scans read the FINISHED FILE ON DISK, not the in-memory objects the
main pipeline built it from -- this checks what a recipient would actually
open, including the custom-properties rewrite that happens after
`Document.save()`.
"""
from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from typing import List, Optional

from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import qn

from .detectors.base import PIIMatch
from .detectors.registry import is_confidently_identifying
from .redactor import Redactor


@dataclass
class ResidualFinding:
    entity_type: str
    text: str
    location: str  # "body/table/header/footer", "footnote/endnote", "comment", "custom_property"


@dataclass
class ResidualScanReport:
    # `clean` tracks `leaked_originals` ONLY -- a confirmed original value
    # still present in the finished file, which is always a genuine
    # problem. `unexpected_matches` is a second, independent NER pass and
    # inherits that pass's own precision limits (documented throughout this
    # project); on a large real document it will usually surface some
    # noise. Folding it into `clean` would make "not clean" the ordinary
    # outcome for any substantial filing, which trains users to ignore the
    # flag -- exactly the failure mode a verification feature exists to
    # prevent. `unexpected_matches` is still fully reported, just as an
    # advisory worth a skim rather than a blocking finding.
    clean: bool
    leaked_originals: List[ResidualFinding] = field(default_factory=list)
    unexpected_matches: List[ResidualFinding] = field(default_factory=list)
    scanned_characters: int = 0

    def summary(self) -> dict:
        """A non-sensitive summary safe to send over a network response --
        counts and types only, never the actual leaked/matched text. The
        full `ResidualFinding` objects (with text) are for local use: the
        CLI's stdout/audit log, or a server-side log a human can inspect
        directly, never a JSON API response or the browser."""
        return {
            "clean": self.clean,
            "leaked_original_count": len(self.leaked_originals),
            "leaked_original_types": sorted({f.entity_type for f in self.leaked_originals}),
            "unexpected_match_count": len(self.unexpected_matches),
            "unexpected_match_types": sorted({f.entity_type for f in self.unexpected_matches}),
        }


def _gather_all_text(docx_path: str) -> "list[tuple[str, str]]":
    """
    Every piece of text this tool knows how to reach in a saved .docx,
    tagged with where it came from. Reopens the file fresh rather than
    reusing in-memory objects from the redaction pass, so this checks the
    artifact that will actually be handed to someone, including the
    custom-properties rewrite that happens after `Document.save()`.
    """
    # Imported here (not at module load) to avoid a circular import:
    # document_io imports Redactor/RedactionResult from this package's
    # sibling modules, and this function is only needed at call time.
    from .document_io import _footnote_and_endnote_paragraphs, _iter_docx_paragraphs

    doc = Document(docx_path)
    blocks: List[tuple] = []

    for paragraph, _context in _iter_docx_paragraphs(doc):
        text = paragraph.text
        if text.strip():
            blocks.append(("body/table/header/footer", text))

    note_paragraphs, _writeback = _footnote_and_endnote_paragraphs(doc)
    for paragraph in note_paragraphs:
        if paragraph.text.strip():
            blocks.append(("footnote/endnote", paragraph.text))

    for comment in doc.part.comments:
        for paragraph in comment.paragraphs:
            if paragraph.text.strip():
                blocks.append(("comment", paragraph.text))
        if comment.author:
            blocks.append(("comment_author", comment.author))

    try:
        with zipfile.ZipFile(docx_path) as z:
            if "docProps/custom.xml" in z.namelist():
                root = parse_xml(z.read("docProps/custom.xml"))
                vt_ns = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"
                for tag in ("lpwstr", "lpstr", "bstr"):
                    for el in root.iter(f"{{{vt_ns}}}{tag}"):
                        if el.text and el.text.strip():
                            blocks.append(("custom_property", el.text))
    except (KeyError, zipfile.BadZipFile):  # pragma: no cover - defensive
        pass

    return blocks


def find_leaked_originals(blocks: "list[tuple[str, str]]",
                           replacements: "list[dict]") -> List[ResidualFinding]:
    """
    Checks every ORIGINAL value the pipeline redacted against the finished
    file. A hit here means the pipeline believed it replaced something
    that is, in fact, still present.

    Restricted to `is_confidently_identifying` matches -- i.e. exactly the
    ones the consistency sweep would also trust enough to propagate. The
    unrestricted version of this check was tried first and reported 45
    "leaks" on the real prospectus that were entirely section headings and
    defined terms ("Capital Structure", "RISKS") that a single NER
    misfire, in ONE spot, doesn't turn into a document-wide redaction
    obligation -- every other occurrence being left alone is correct
    behaviour, not a leak. See `registry.is_confidently_identifying` for
    the full reasoning.
    """
    seen: set = set()
    findings: List[ResidualFinding] = []
    for rep in replacements:
        key = (rep["type"], rep["original"])
        if key in seen or len(rep["original"]) < 4:
            continue
        if not is_confidently_identifying(rep["type"], rep["original"]):
            continue
        seen.add(key)
        for location, text in blocks:
            if rep["original"] in text:
                findings.append(ResidualFinding(rep["type"], rep["original"], location))
                break
    return findings


#: Below this length a word is too generic ("A", "of", "II") to count as
#: evidence that a match is built entirely from fake content -- an
#: unindicative short word appearing in a known fake value is a coincidence,
#: not proof that some OTHER text made of the same word is also fake.
_MIN_FRAGMENT_WORD_LEN = 3


def _looks_like_fake_content(match_text: str, known_fake_words: set) -> bool:
    """
    True when a match is plausibly a recombination of the tool's OWN fake
    output rather than genuine leftover PII.

    `Faker` draws from a large pool of names/companies/addresses, and this
    document alone contains 250+ fake people and 600+ fake organisations --
    at that volume, a surname from one fake name ("Kaul") and a company
    fragment from another ("Chaudhry and Sons") coincidentally recombine
    often enough that re-running NER over the finished document repeatedly
    re-tags pieces of the tool's OWN output as a "new" entity. None of that
    is a leak: the underlying identity was already fake before NER touched
    it. Requiring every significant word in the match to come from a known
    fake value (rather than requiring an exact full-string match) is what
    catches this recombination, since the re-detected span rarely lines up
    with any single original replacement's exact boundaries.
    """
    words = [w for w in re.split(r"\W+", match_text) if len(w) >= _MIN_FRAGMENT_WORD_LEN]
    if not words:
        return False
    return all(w.lower() in known_fake_words for w in words)


def find_unexpected_pii(blocks: "list[tuple[str, str]]", redactor: Redactor,
                         known_fake_values: set) -> List[ResidualFinding]:
    """Re-runs detection over the whole finished document as independent
    blocks (not reusing the per-paragraph matches from the main pass), and
    reports anything that isn't a known fake value the pipeline itself
    produced -- or a recombination of one, see `_looks_like_fake_content`.
    See module docstring for why this is a heuristic second opinion rather
    than an exhaustive guarantee."""
    known_fake_words = {w.lower() for value in known_fake_values for w in re.split(r"\W+", value)
                        if len(w) >= _MIN_FRAGMENT_WORD_LEN}
    findings: List[ResidualFinding] = []
    seen: set = set()
    for location, text in blocks:
        matches: List[PIIMatch] = redactor.detect(text)
        for m in matches:
            if m.text in known_fake_values:
                continue
            if _looks_like_fake_content(m.text, known_fake_words):
                continue
            key = (m.entity_type, m.text, location)
            if key in seen:
                continue
            seen.add(key)
            findings.append(ResidualFinding(m.entity_type, m.text, location))
    return findings


def verify_docx(docx_path: str, replacements: "list[dict]", redactor: Redactor) -> ResidualScanReport:
    """Runs both residual checks against a finished .docx file on disk."""
    blocks = _gather_all_text(docx_path)
    known_fake_values = {rep["fake"] for rep in replacements}
    leaked = find_leaked_originals(blocks, replacements)
    unexpected = find_unexpected_pii(blocks, redactor, known_fake_values)
    scanned_chars = sum(len(text) for _loc, text in blocks)
    return ResidualScanReport(
        clean=not leaked,
        leaked_originals=leaked,
        unexpected_matches=unexpected,
        scanned_characters=scanned_chars,
    )
