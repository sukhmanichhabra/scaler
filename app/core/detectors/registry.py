"""
Central registry: instantiates every detector plugin and runs them all over
a document, then resolves overlapping spans (e.g. a NER 'ADDRESS' match and
a regex 'PHONE' match that happen to overlap) by preferring higher
confidence / more specific detectors.

Adding a new PII type = write a PIIDetector subclass + add one line here.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Dict, List, Optional

from .base import DetectionConfig, PIIDetector, PIIMatch
from .ner_detector import (AddressDetector, OrganizationDetector, PersonNameDetector,
                           is_org_sweep_eligible, is_sweep_eligible)
from .regex_detectors import (
    AadhaarNumberDetector,
    BankAccountNumberDetector,
    CreditCardDetector,
    DateOfBirthDetector,
    DrivingLicenseDetector,
    EmailDetector,
    GSTINDetector,
    IFSCDetector,
    IPAddressDetector,
    PANNumberDetector,
    PassportNumberDetector,
    PhoneDetector,
    SSNDetector,
    UPIIdDetector,
    VoterIdDetector,
)

# Order matters only as a tie-breaker hint; actual conflict resolution is by
# confidence score in `resolve_overlaps`.
_TYPE_TO_DETECTOR = {
    "EMAIL": EmailDetector,
    "PHONE": PhoneDetector,
    "IP_ADDRESS": IPAddressDetector,
    "SSN": SSNDetector,
    "CREDIT_CARD": CreditCardDetector,
    "DATE_OF_BIRTH": DateOfBirthDetector,
    "PAN_NUMBER": PANNumberDetector,
    "AADHAAR_NUMBER": AadhaarNumberDetector,
    "IFSC_CODE": IFSCDetector,
    "GSTIN": GSTINDetector,
    "UPI_ID": UPIIdDetector,
    "PASSPORT_NUMBER": PassportNumberDetector,
    "VOTER_ID": VoterIdDetector,
    "DRIVING_LICENSE": DrivingLicenseDetector,
    "BANK_ACCOUNT_NUMBER": BankAccountNumberDetector,
    "PERSON": PersonNameDetector,
    "ORG": OrganizationDetector,
    "ADDRESS": AddressDetector,
}


def build_detectors(config: DetectionConfig, issuer_names: Optional[set] = None,
                     skip_ner: bool = False, defined_terms: Optional[set] = None) -> List[PIIDetector]:
    detectors = []
    for entity_type in config.enabled_types:
        if skip_ner and entity_type == "PERSON":
            # Headings/titles are short, context-free phrases where spaCy's
            # statistical NER is at its noisiest ("Reference Numbers" ->
            # false-positive PERSON). Structured regex PII (email, phone,
            # SSN, etc.) and the pattern-based ADDRESS detector are
            # unaffected and stay on, since they don't need sentence context.
            continue
        cls = _TYPE_TO_DETECTOR.get(entity_type)
        if cls is None:
            continue
        if cls is OrganizationDetector:
            # Organisations run even in headings, but in `strict` mode: a
            # heading is exactly where a bank or registrar gets named
            # ("State Bank of India, Industrial Finance Branch"), and
            # skipping headings outright meant that name was never detected
            # anywhere, so nothing could recover it later.
            detectors.append(cls(
                issuer_names=issuer_names if not config.redact_issuer_company else None,
                defined_terms=defined_terms,
                strict=skip_ner,
            ))
        elif cls is PersonNameDetector:
            detectors.append(cls(defined_terms=defined_terms))
        else:
            detectors.append(cls())
    return detectors


# Lower number = wins conflicts first. Structured, validated regex types are
# the most trustworthy (email/phone/SSN/etc. pass a real format check), so
# they always take priority over statistical NER guesses. A full ADDRESS
# line is more useful than a fragment of it being misread as an ORG, so it
# outranks PERSON/ORG too. This ordering is what stopped a bug where a
# building name inside a full mailing address was redacted as a fake company
# while the rest of the same address line leaked in cleartext.
_TYPE_PRIORITY = {
    "EMAIL": 0, "PHONE": 0, "SSN": 0, "CREDIT_CARD": 0, "IP_ADDRESS": 0,
    "PAN_NUMBER": 0, "AADHAAR_NUMBER": 0, "DATE_OF_BIRTH": 0,
    "IFSC_CODE": 0, "GSTIN": 0, "UPI_ID": 0,
    "PASSPORT_NUMBER": 0, "VOTER_ID": 0, "DRIVING_LICENSE": 0, "BANK_ACCOUNT_NUMBER": 0,
    "ADDRESS": 1,
    "PERSON": 2,
    "ORG": 3,
}


# Types where a PARTIAL span is still worth redacting on its own. A mailing
# address routinely encloses a phone number or email ("...Mumbai 400083
# Telephone: +91 81081 14949"); the enclosed match wins the overlap, and
# without this the entire surrounding address would simply be dropped --
# leaking the street address in cleartext while redacting only the phone.
# For PERSON/ORG the opposite is true: half a name is noise, not privacy,
# so those are dropped whole as before.
_SPLITTABLE_TYPES = {"ADDRESS"}

# Below this many meaningful characters a leftover fragment ("， India",
# "Email:") carries no address information and is discarded rather than
# replaced with a full fake address.
_MIN_FRAGMENT_CHARS = 10


def _uncovered_spans(start: int, end: int, blockers: List[tuple]) -> List[tuple]:
    """The sub-ranges of [start, end) not covered by any blocker range."""
    gaps = []
    cursor = start
    for b_start, b_end in sorted(blockers):
        if b_start > cursor:
            gaps.append((cursor, min(b_start, end)))
        cursor = max(cursor, b_end)
        if cursor >= end:
            break
    if cursor < end:
        gaps.append((cursor, end))
    return [(s, e) for s, e in gaps if e > s]


def resolve_overlaps(matches: List[PIIMatch]) -> List[PIIMatch]:
    """
    When two matches overlap (e.g. an ADDRESS heuristic line that also
    contains a PHONE number, or two detectors both claiming similar text),
    keep the higher-priority / higher-confidence / longer match and drop the
    rest so the same characters aren't redacted twice or replaced
    inconsistently.

    For splittable types the loser isn't discarded outright: whatever part
    of it the winner didn't claim is kept as a shorter match of the same
    type, so an address wrapped around a phone number still gets redacted.
    """
    if not matches:
        return []
    ordered = sorted(
        matches,
        key=lambda m: (_TYPE_PRIORITY.get(m.entity_type, 9), -m.confidence, -(len(m)), m.start),
    )
    kept: List[PIIMatch] = []
    occupied: List[tuple] = []  # (start, end, entity_type)
    for m in ordered:
        conflicts = [(s, e, t) for s, e, t in occupied if not (m.end <= s or m.start >= e)]
        if not conflicts:
            kept.append(m)
            occupied.append((m.start, m.end, m.entity_type))
            continue
        if m.entity_type not in _SPLITTABLE_TYPES:
            continue
        # Two detectors claiming the SAME type over the same region are
        # rival readings of one span (the address detector's lead-in rule
        # and its PIN-code rule routinely both fire). Splitting there would
        # leave the loser's non-overlapping prose -- "The registered office
        # is situated at " -- behind as a bogus address fragment, which then
        # outranks and suppresses real names inside it. Only a different
        # type genuinely embedded in this one warrants a split.
        if any(t == m.entity_type for _, _, t in conflicts):
            continue
        for frag_start, frag_end in _uncovered_spans(m.start, m.end, [(s, e) for s, e, _ in conflicts]):
            fragment = m.text[frag_start - m.start:frag_end - m.start]
            if len(fragment.strip(" ,.;:-()")) < _MIN_FRAGMENT_CHARS:
                continue
            if not any(c.isalpha() for c in fragment):
                continue
            kept.append(PIIMatch(m.entity_type, fragment, frag_start, frag_end, m.confidence, m.source))
            occupied.append((frag_start, frag_end, m.entity_type))
    return sorted(kept, key=lambda m: m.start)


# PERSON vs ORG is the one place NER can flip its mind on the *identical*
# string between two mentions in the same document (e.g. "Rohan Dey" tagged
# PERSON the first time and, more rarely, ORG the second) -- both real
# examples seen during testing. Left alone, that breaks the "same value ->
# same fake value" consistency guarantee, because the fake-value cache is
# keyed by (entity_type, text). This reconciles every mention of the same
# exact string to whichever type was the majority vote across the document,
# so consistency holds even when the model itself isn't fully consistent.
# Deliberately scoped to PERSON/ORG only -- the structured regex types are
# already validated and never need this.
_RECONCILE_TYPES = {"PERSON", "ORG"}


def reconcile_entity_types(matches: List[PIIMatch]) -> List[PIIMatch]:
    votes: dict = {}
    for m in matches:
        if m.entity_type in _RECONCILE_TYPES:
            key = m.text.strip().lower()
            votes.setdefault(key, Counter())[m.entity_type] += 1

    dominant = {key: counter.most_common(1)[0][0] for key, counter in votes.items() if len(counter) > 1}
    if not dominant:
        return matches

    reconciled = []
    for m in matches:
        key = m.text.strip().lower()
        target_type = dominant.get(key)
        if m.entity_type in _RECONCILE_TYPES and target_type and target_type != m.entity_type:
            reconciled.append(PIIMatch(target_type, m.text, m.start, m.end, m.confidence, m.source))
        else:
            reconciled.append(m)
    return reconciled


# A company name carrying a legal-form or industry suffix is unambiguously
# an organisation. That distinction is what makes an ORG sweep safe: the
# false positives that made an earlier, ungated ORG sweep catastrophic were
# document headings ("Red Herring Prospectus", "Capital Structure"), and a
# heading never carries a suffix like this. Without the sweep, a company
# spaCy tags in one sentence and misses in the next ("HDFC Bank",
# "ICICI Securities") is redacted inconsistently and leaks.
# Kept to unambiguous legal/industry forms. Words like "Capital", "Finance"
# or "Services" are deliberately absent: they appear in ordinary headings
# ("Capital Structure", "Financial Services Industry") and would reopen
# exactly the heading-propagation failure this gate exists to prevent.
#: The legal form at the end of a company name, which everyday usage drops
#: ("ICICI Securities Limited" -> "ICICI Securities").
_LEGAL_FORM_TAIL = re.compile(
    r"\s+(?:(?:private|pvt\.?)\s+)?"
    r"(?:limited|ltd\.?|llp|llc|plc|inc\.?|incorporated|corporation|corp\.?)$",
    re.IGNORECASE,
)

_SWEEPABLE_ORG_SUFFIX = re.compile(
    r"\b(Ltd|Limited|LLP|Pvt|Private|Inc|Corp|Corporation|PLC|LLC|"
    r"Bank|Securities|Insurance|Partners|Associates|Advisors|Advisers|"
    r"Consultants|Industries|Technologies|Enterprises|Ventures)\b",
    re.IGNORECASE,
)


def _org_is_sweepable(text: str) -> bool:
    """
    Whether an ORG-tagged span is safe to propagate document-wide.

    Two accepted shapes:
      - a corporate suffix ("HDFC Bank", "State Bank of India") -- the
        legal/industry form makes it unambiguously an organisation;
      - three or more words passing the strict person-style gate. spaCy
        routinely tags a PERSON as ORG, and a director's name carries no
        corporate suffix, so a suffix-only rule left real names leaking.
        Requiring three words keeps two-word section headings ("Capital
        Structure", "Offer Details") out, while "Dinesh Hirachand Munot"
        gets through.
    """
    if is_org_sweep_eligible(text) and _SWEEPABLE_ORG_SUFFIX.search(text):
        return True
    return is_sweep_eligible(text) and len(text.split()) >= 3


def is_confidently_identifying(entity_type: str, text: str) -> bool:
    """
    Whether a (type, text) match is trustworthy enough to assume EVERY
    occurrence of the same string in the document should be treated the
    same way -- propagated by the consistency sweep, and (in
    `verification.py`) checked for whether it still leaks elsewhere.

    Structured, regex-validated types (EMAIL, PHONE, SSN, ...) and ADDRESS
    always qualify -- a validated email address is never a false positive.
    PERSON/ORG do NOT automatically qualify, and this is the reason: NER
    tags plenty of things that are not names at all ("Capital Structure",
    "RISKS", generic section headings), and unlike a real name, the SAME
    false-positive string recurring elsewhere in a 700-paragraph filing is
    not a privacy problem -- it's just the same heading appearing again.
    Assuming otherwise was tried (an early version of the residual scan in
    verification.py) and produced 45 "leaks" on the real prospectus that
    were entirely section headings and defined terms correctly left alone
    everywhere except the one place NER misfired on them.

    So PERSON/ORG only qualify when they ALSO pass the same strict gate
    that decides whether a name is safe to sweep across the document in
    the first place (`is_sweep_eligible` / `_org_is_sweepable`) --
    reusing that gate here rather than inventing a second one keeps both
    checks answering the same underlying question: "is this confidently a
    real name, not a NER guess on ordinary prose."
    """
    if entity_type == "PERSON":
        return is_sweep_eligible(text)
    if entity_type == "ORG":
        return _org_is_sweepable(text)
    return True


def build_known_names(matches: List[PIIMatch]) -> Dict[str, str]:
    """
    Registry of names confidently detected SOMEWHERE in the document, keyed
    by normalized-lowercase text -- feeds the cross-document consistency
    sweep below. Only multi-word, fully capitalised, non-generic names
    qualify (`is_sweep_eligible`); organisations must additionally carry a
    corporate suffix (`_SWEEPABLE_ORG_SUFFIX`).
    """
    votes: Dict[str, Counter] = {}
    for m in matches:
        if m.entity_type not in ("PERSON", "ORG"):
            continue
        text = m.text.strip()
        if not is_confidently_identifying(m.entity_type, text):
            continue
        votes.setdefault(text.lower(), Counter())[m.entity_type] += 1
    # A single confident detection is enough. This threshold was originally
    # 2, as a guard against propagating a one-off NER fluke -- but that guard
    # predates the structural gates now applied above (multi-word, fully
    # capitalised, no offer/process nouns, not a glossary term, and for
    # organisations a corporate suffix). Those reject every false positive
    # the threshold was catching ("air conditioning", "Wilful defaulter",
    # "Supa Facility"), while the threshold itself was rejecting real names
    # that NER tags in one place and misses in another -- which is exactly
    # the leak the sweep exists to close.
    return {name: counter.most_common(1)[0][0] for name, counter in votes.items()}


def derive_name_aliases(known_names: Dict[str, str]) -> Dict[str, str]:
    """
    Maps a shortened form of a known person's name to its full form:
    "Pushpa Kushal Hegde" also appears as "Pushpa Hegde".

    Without this, the two spellings are different cache keys and therefore
    get two different fake identities -- and any mention NER happens to miss
    in the short form leaks entirely. Indian naming conventions make this
    common (given name + father's name + surname, often abbreviated to given
    name + surname), and this document has four members of one family.

    An alias is only registered when exactly ONE full name produces it. If
    two people would collapse onto the same short form, the mapping is
    ambiguous and is skipped rather than guessed at.
    """
    candidates: Dict[str, set] = {}
    for full_name, entity_type in known_names.items():
        if entity_type != "PERSON":
            continue
        parts = full_name.split()
        if len(parts) < 3:
            continue
        short = f"{parts[0]} {parts[-1]}"
        if short == full_name:
            continue
        candidates.setdefault(short, set()).add(full_name)
    return {short: next(iter(fulls)) for short, fulls in candidates.items()
            if len(fulls) == 1 and short not in known_names}


def derive_org_short_forms(known_names: Dict[str, str]) -> Dict[str, str]:
    """
    Maps a company's everyday short form to its full legal name:
    "ICICI Securities Limited" is written "ICICI Securities" just as often,
    and "Link Intime India Private Limited" becomes "Link Intime".

    Exactly the problem `derive_name_aliases` solves for people, and it
    matters for the same reason: spaCy tags the full name reliably and the
    short form erratically, so without this the short form leaks wherever
    the model happens to miss it.

    Only the leading two words are taken, and only when they are unique
    across the document's known companies -- an ambiguous prefix shared by
    two firms is skipped rather than guessed at.
    """
    short_forms: Dict[str, str] = {}
    for full_name, entity_type in known_names.items():
        if entity_type != "ORG":
            continue
        # ONLY the legal-form tail is dropped. Taking an arbitrary leading
        # slice instead looks tempting but is destructive: these keys are
        # lowercased, so the capitalisation guards no longer apply, and a
        # two-word prefix rule invented "red herring" from "Red Herring
        # Prospectus" and swept it 139 times across the document. Requiring
        # a legal form to strip means the remainder is, by construction,
        # the company's name without its legal form.
        short = _LEGAL_FORM_TAIL.sub("", full_name).strip()
        if short == full_name or len(short.split()) < 2:
            continue
        if short in known_names:
            continue
        short_forms[short] = "ORG"
    return short_forms


def sweep_known_names(text: str, existing: List[PIIMatch], known_names: Dict[str, str]) -> List[PIIMatch]:
    """
    Catches a casing variant of an already-known name that NER missed on
    THIS specific occurrence -- e.g. spaCy correctly tags "Kushal Subbayya
    Hegde" as PERSON in one paragraph but fails to tag the same name
    ALL-CAPS in a cover-page declaration elsewhere. Since the fake-value
    cache in `faker_mapper.py` is already keyed by lowercased text, sweeping
    in an extra match here for the same name automatically gets the same
    fake replacement -- no separate consistency bookkeeping needed.
    """
    if not known_names:
        return []
    covered = [(m.start, m.end) for m in existing]
    extra: List[PIIMatch] = []
    # Longest names first so e.g. "Kushal Subbayya Hegde" is preferred over
    # a shorter name that happens to be a substring of it.
    for name, entity_type in sorted(known_names.items(), key=lambda kv: -len(kv[0])):
        pattern = re.compile(r"\b" + re.escape(name) + r"\b", re.IGNORECASE)
        for m in pattern.finditer(text):
            if any(not (m.end() <= s or m.start() >= e) for s, e in covered):
                continue
            extra.append(PIIMatch(entity_type, m.group(), m.start(), m.end(), 0.75, "consistency_sweep"))
            covered.append((m.start(), m.end()))
    return extra


def apply_known_names(text: str, matches: List[PIIMatch],
                       known_names: Optional[Dict[str, str]]) -> List[PIIMatch]:
    """
    Second-stage refinement over ALREADY-detected matches: sweep in casing
    variants of known names, then reconcile PERSON/ORG disagreements. Split
    out from `run_all_detectors` so the document pipeline can run detection
    once and apply this cheap, model-free step separately.
    """
    resolved = matches
    if known_names:
        extra = sweep_known_names(text, resolved, known_names)
        if extra:
            resolved = resolve_overlaps(resolved + extra)
    return reconcile_entity_types(resolved)


def run_all_detectors(
    text: str,
    config: Optional[DetectionConfig] = None,
    issuer_names: Optional[set] = None,
    skip_ner: bool = False,
    known_names: Optional[Dict[str, str]] = None,
    defined_terms: Optional[set] = None,
    context: str = "",
) -> List[PIIMatch]:
    config = config or DetectionConfig()
    detectors = build_detectors(config, issuer_names=issuer_names, skip_ner=skip_ner,
                                 defined_terms=defined_terms)
    raw_matches: List[PIIMatch] = []
    for detector in detectors:
        # Structural context is opt-in per detector -- see
        # `PIIDetector.accepts_context`.
        if getattr(detector, "accepts_context", False):
            raw_matches.extend(detector.detect(text, context=context))
        else:
            raw_matches.extend(detector.detect(text))
    raw_matches = [m for m in raw_matches if m.confidence >= config.min_confidence]
    return apply_known_names(text, resolve_overlaps(raw_matches), known_names)
