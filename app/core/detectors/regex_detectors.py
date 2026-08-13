"""
Regex + validation based detectors for *structured* PII.

Structured PII (emails, phone numbers, credit cards, SSNs, IPs...) has a
predictable shape, so deterministic pattern matching -- backed by a real
validation check (Luhn, octet ranges, checksum, etc.) -- gives much higher
precision than asking a statistical NER model to find them. NER is reserved
for genuinely unstructured PII (names, orgs, addresses) in `ner_detector.py`.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import List

import phonenumbers

from .base import PIIDetector, PIIMatch


class EmailDetector(PIIDetector):
    name = "email"
    _PATTERN = re.compile(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
    )

    def detect(self, text: str) -> List[PIIMatch]:
        return [
            PIIMatch("EMAIL", m.group(), m.start(), m.end(), 0.99, "regex")
            for m in self._PATTERN.finditer(text)
        ]


class PhoneDetector(PIIDetector):
    """
    Finds phone-number-shaped substrings, then validates them with Google's
    libphonenumber (via the `phonenumbers` package) so that things like
    "Ticket #98765432" or a 10-digit account number aren't misflagged.
    Defaults to India ("IN") as the fallback region since that's the primary
    use case here, but also matches explicit international (+CC) numbers.
    """

    name = "phone"
    # Candidate spans: optional +CC, then groups of digits/spaces/dashes/parens,
    # at least 9 digits total so we don't catch short numeric codes. The
    # lookbehind also excludes a directly-preceding letter (no `\b` here since
    # letter->digit isn't a word-boundary transition) so that reference codes
    # like a SEBI registration number "INM000013004" don't get their trailing
    # digit run misread as a phone number.
    _CANDIDATE = re.compile(
        r"(?<![A-Za-z\d])(\+?\d{1,3}[\s-]?)?(\(?\d{2,5}\)?[\s-]?){2,5}\d{3,4}(?!\d)"
    )

    def detect(self, text: str) -> List[PIIMatch]:
        matches: List[PIIMatch] = []
        for m in self._CANDIDATE.finditer(text):
            candidate = m.group().strip()
            digits = re.sub(r"\D", "", candidate)
            if not (9 <= len(digits) <= 13):
                continue
            parsed_ok = False
            for region in ("IN", "US", None):
                try:
                    num = phonenumbers.parse(candidate, region)
                    if phonenumbers.is_valid_number(num) or phonenumbers.is_possible_number(num):
                        parsed_ok = True
                        break
                except phonenumbers.NumberParseException:
                    continue
            if parsed_ok:
                matches.append(
                    PIIMatch("PHONE", m.group(), m.start(), m.end(), 0.95, "regex")
                )
        return matches


class IPAddressDetector(PIIDetector):
    name = "ip_address"
    _PATTERN = re.compile(
        r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"
    )
    # crude IPv6 pattern (full form + common compressed form)
    _V6_PATTERN = re.compile(
        r"\b(?:[A-Fa-f0-9]{1,4}:){2,7}[A-Fa-f0-9]{1,4}\b"
    )

    def detect(self, text: str) -> List[PIIMatch]:
        matches = [
            PIIMatch("IP_ADDRESS", m.group(), m.start(), m.end(), 0.98, "regex")
            for m in self._PATTERN.finditer(text)
        ]
        for m in self._V6_PATTERN.finditer(text):
            if m.group().count(":") >= 2:
                matches.append(
                    PIIMatch("IP_ADDRESS", m.group(), m.start(), m.end(), 0.85, "regex")
                )
        return matches


class SSNDetector(PIIDetector):
    """US Social Security Numbers: NNN-NN-NNNN, with basic validity rules
    (area number 000/666/900-999 is never issued)."""

    name = "ssn"
    _PATTERN = re.compile(r"\b(\d{3})-(\d{2})-(\d{4})\b")

    def detect(self, text: str) -> List[PIIMatch]:
        matches = []
        for m in self._PATTERN.finditer(text):
            area = int(m.group(1))
            group = int(m.group(2))
            serial = int(m.group(3))
            if area == 0 or area == 666 or area >= 900:
                continue
            if group == 0 or serial == 0:
                continue
            matches.append(PIIMatch("SSN", m.group(), m.start(), m.end(), 0.97, "regex"))
        return matches


class CreditCardDetector(PIIDetector):
    """Matches 13-19 digit card-shaped numbers (with optional spaces/dashes)
    and keeps only those that pass the Luhn checksum -- this is what
    separates a real card number from an arbitrary long ID/reference number."""

    name = "credit_card"
    _CANDIDATE = re.compile(r"\b(?:\d[ -]?){13,19}\b")

    @staticmethod
    def _luhn_ok(digits: str) -> bool:
        total = 0
        reverse = digits[::-1]
        for i, ch in enumerate(reverse):
            d = int(ch)
            if i % 2 == 1:
                d *= 2
                if d > 9:
                    d -= 9
            total += d
        return total % 10 == 0

    def detect(self, text: str) -> List[PIIMatch]:
        matches = []
        for m in self._CANDIDATE.finditer(text):
            raw = m.group()
            # The candidate pattern's optional trailing separator can greedily
            # swallow a trailing space/dash that isn't really part of the
            # number (e.g. "...366 on 12 January" -> "...366 " gets matched).
            # Trim it back off and adjust the end offset to match.
            trimmed = raw.rstrip(" -")
            end = m.start() + len(trimmed)
            digits = re.sub(r"[ -]", "", trimmed)
            if not (13 <= len(digits) <= 19):
                continue
            if self._luhn_ok(digits):
                matches.append(
                    PIIMatch("CREDIT_CARD", trimmed, m.start(), end, 0.96, "regex")
                )
        return matches


class DateOfBirthDetector(PIIDetector):
    """
    Dates on their own are ambiguous in a document like a prospectus
    (filing dates, incorporation dates, board-meeting dates all appear).
    We only classify a date as DATE_OF_BIRTH when it appears near an
    explicit birth-related keyword, which keeps precision high.

    That keyword is not always in the same text block as the date. In a
    directors' table the label "Date of Birth" is a column header or row
    label living in a DIFFERENT cell from the value, so an in-text-only
    search can never fire. `context` carries that structural text (row
    label + column header) so the table layout is handled too.
    """

    name = "date_of_birth"
    accepts_context = True
    _DATE = re.compile(
        r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|"
        r"\d{4}-\d{2}-\d{2}|"
        r"(?:January|February|March|April|May|June|July|August|September|"
        r"October|November|December)\s+\d{1,2},?\s+\d{4}|"
        r"\d{1,2}\s+(?:January|February|March|April|May|June|July|August|"
        r"September|October|November|December)\s+\d{4})\b"
    )
    _CONTEXT = re.compile(
        r"(date of birth|d\.?o\.?b\.?|born on|born)", re.IGNORECASE
    )
    _WINDOW = 40  # chars of context to look at before the date

    def detect(self, text: str, context: str = "") -> List[PIIMatch]:
        # A birth label in the surrounding structure (a table's row label or
        # column header) applies to every date in this cell.
        labelled_by_structure = bool(context and self._CONTEXT.search(context))
        matches = []
        for m in self._DATE.finditer(text):
            window_start = max(0, m.start() - self._WINDOW)
            preceding = text[window_start:m.start()]
            if labelled_by_structure or self._CONTEXT.search(preceding):
                matches.append(
                    PIIMatch("DATE_OF_BIRTH", m.group(), m.start(), m.end(), 0.9, "regex")
                )
        return matches


class PANNumberDetector(PIIDetector):
    """Indian Income-Tax PAN: 5 letters, 4 digits, 1 letter (e.g. ABCDE1234F).
    Bonus detector beyond the assignment's minimum list -- relevant because
    the source document is an Indian regulatory filing."""

    name = "pan_number"
    _PATTERN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")

    def detect(self, text: str) -> List[PIIMatch]:
        return [
            PIIMatch("PAN_NUMBER", m.group(), m.start(), m.end(), 0.9, "regex")
            for m in self._PATTERN.finditer(text)
        ]


class AadhaarNumberDetector(PIIDetector):
    """
    Indian Aadhaar ID: 12 digits, commonly shown as 'XXXX XXXX XXXX'. That
    shape is easy to confuse with a 12-digit phone number, so this only
    fires at high confidence when the word "Aadhaar" appears nearby;
    otherwise it still flags the pattern but at a confidence low enough that
    a validated PHONE match on the same digits wins the overlap.
    """

    name = "aadhaar_number"
    _PATTERN = re.compile(r"\b\d{4}\s\d{4}\s\d{4}\b")
    _CONTEXT = re.compile(r"aadhaar|uidai", re.IGNORECASE)
    _WINDOW = 25

    def detect(self, text: str) -> List[PIIMatch]:
        matches = []
        for m in self._PATTERN.finditer(text):
            window_start = max(0, m.start() - self._WINDOW)
            has_context = bool(self._CONTEXT.search(text[window_start:m.start()]))
            confidence = 0.98 if has_context else 0.6
            matches.append(
                PIIMatch("AADHAAR_NUMBER", m.group(), m.start(), m.end(), confidence, "regex")
            )
        return matches


class IFSCDetector(PIIDetector):
    """
    Indian bank branch code: 4-letter bank code, a literal '0' (reserved
    for future use by the RBI), then 6 alphanumeric branch characters --
    e.g. HDFC0001234. The literal zero in the 5th position is effectively a
    built-in checksum: a random 11-character alphanumeric string only has a
    1-in-36 chance of landing a '0' there, so requiring it does most of the
    validation work a dedicated checksum would, the same spirit as the Luhn
    check on a credit card. Pinpoints a specific bank branch, which combined
    with an account number (see BankAccountNumberDetector) identifies where
    someone banks -- redacted on its own too, since it's already specific
    enough to narrow down a person's bank and branch.
    """

    name = "ifsc_code"
    _PATTERN = re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b")

    def detect(self, text: str) -> List[PIIMatch]:
        return [
            PIIMatch("IFSC_CODE", m.group(), m.start(), m.end(), 0.93, "regex")
            for m in self._PATTERN.finditer(text)
        ]


class GSTINDetector(PIIDetector):
    """
    Indian GST registration number: 2-digit state code + a 10-character PAN
    + 1-digit entity number + a literal 'Z' (reserved) + 1 checksum
    character = 15 characters total. The embedded PAN must itself be
    correctly shaped (5 letters, 4 digits, 1 letter), which is real
    structural validation, not just a length check -- the same approach
    already used for CIN numbers elsewhere in this codebase.

    A GSTIN identifies a business registration, not a person directly --
    for a company it's closer to the CIN numbers this tool already treats
    as non-sensitive. It's included anyway (unlike CIN) because a very
    common real-world case is a GSTIN issued to an individual's sole
    proprietorship, where it functions as a personal tax identifier. If
    that distinction matters for a given document, disable it per-run with
    `--disable GSTIN` / the equivalent API and UI toggle.
    """

    name = "gstin"
    _PATTERN = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b")

    def detect(self, text: str) -> List[PIIMatch]:
        return [
            PIIMatch("GSTIN", m.group(), m.start(), m.end(), 0.92, "regex")
            for m in self._PATTERN.finditer(text)
        ]


class UPIIdDetector(PIIDetector):
    """
    UPI payment address: `<identifier>@<psp-handle>`, e.g.
    "rohan.dey@okhdfcbank" or "9876543210@ybl". Structurally identical to
    an email's local-part-at-domain shape, which is exactly why this can't
    just be "anything@anything": that would either duplicate EmailDetector
    or, worse, flag ordinary text like "meet@noon" as PII. `EmailDetector`
    already requires a dotted TLD (`.com`, `.org`, ...), and a UPI handle
    never has one -- "@ybl" has no dot -- so the two patterns don't
    actually collide; what UPIIdDetector adds is recognising the
    non-email, no-TLD "@handle" shape at all, gated on the handle being one
    of the small, well-known set Indian banks and payment apps actually
    issue. A handle outside this list is not flagged, trading recall for
    precision -- the alternative (matching any "word@word" pattern) would
    flag far too much ordinary text that merely contains an @ sign.
    """

    name = "upi_id"
    # Not exhaustive -- NPCI adds handles occasionally -- but covers the
    # large majority of real UPI IDs: the major banks issuing their own
    # handle, and the major third-party apps (PhonePe, Paytm, Google Pay,
    # Amazon Pay, ...).
    _KNOWN_HANDLES = {
        "okhdfcbank", "okicici", "oksbi", "okaxis", "okbizaxis",
        "ybl", "ibl", "axl", "apl", "jio",
        "paytm", "ptsbi", "ptaxis", "ptyes",
        "hdfcbank", "icici", "axisbank", "sbi", "yesbank",
        "kotak", "federal", "indus", "idfcbank", "rbl",
        "freecharge", "airtel", "fbl", "cnrb", "barodampay",
        "upi", "waaxis", "wahdfcbank", "waicici", "wasbi",
    }
    _PATTERN = re.compile(r"\b([\w.\-]{2,49})@([a-zA-Z][a-zA-Z0-9]{1,64})\b")

    def detect(self, text: str) -> List[PIIMatch]:
        matches = []
        for m in self._PATTERN.finditer(text):
            handle = m.group(2).lower()
            if handle in self._KNOWN_HANDLES:
                matches.append(PIIMatch("UPI_ID", m.group(), m.start(), m.end(), 0.9, "regex"))
        return matches


class PassportNumberDetector(PIIDetector):
    """
    Indian passport number: 1 letter + 7 digits (e.g. M1234567). This shape
    alone is far too generic to flag on its own -- it collides with all
    kinds of reference codes and IDs a formal document uses freely -- so,
    like DateOfBirthDetector, this only fires when the word "passport"
    appears nearby. Without that anchor, precision would collapse.
    """

    name = "passport_number"
    _PATTERN = re.compile(r"\b[A-Z]\d{7}\b")
    _CONTEXT = re.compile(r"passport", re.IGNORECASE)
    _WINDOW = 40

    def detect(self, text: str) -> List[PIIMatch]:
        matches = []
        for m in self._PATTERN.finditer(text):
            window_start = max(0, m.start() - self._WINDOW)
            if self._CONTEXT.search(text[window_start:m.start()]):
                matches.append(
                    PIIMatch("PASSPORT_NUMBER", m.group(), m.start(), m.end(), 0.88, "regex")
                )
        return matches


class VoterIdDetector(PIIDetector):
    """
    Indian Elector's Photo ID Card (EPIC) number: 3 letters + 7 digits
    (e.g. ABC1234567). Same reasoning as PassportNumberDetector: the bare
    pattern is too generic to trust alone, so this requires "voter",
    "EPIC", or "elector" nearby.
    """

    name = "voter_id"
    _PATTERN = re.compile(r"\b[A-Z]{3}\d{7}\b")
    _CONTEXT = re.compile(r"voter|epic|elector", re.IGNORECASE)
    _WINDOW = 40

    def detect(self, text: str) -> List[PIIMatch]:
        matches = []
        for m in self._PATTERN.finditer(text):
            window_start = max(0, m.start() - self._WINDOW)
            if self._CONTEXT.search(text[window_start:m.start()]):
                matches.append(
                    PIIMatch("VOTER_ID", m.group(), m.start(), m.end(), 0.88, "regex")
                )
        return matches


class DrivingLicenseDetector(PIIDetector):
    """
    Indian driving licence number. State-issued and only loosely
    standardised in practice, but the common shape is 2-letter state code +
    2-digit RTO code + 4-digit year + 7-digit serial (e.g. "MH12 20110012345",
    sometimes with hyphens or extra spaces between groups). Even with that
    much structure, a 15-digit-ish code is generic enough that this still
    requires a "driving licence/license" or "DL No" label nearby, the same
    context-gating used for passport and voter ID above.
    """

    name = "driving_license"
    _PATTERN = re.compile(r"\b[A-Z]{2}[-\s]?\d{2}[-\s]?(?:19|20)\d{2}[-\s]?\d{7}\b")
    _CONTEXT = re.compile(r"driving licen[cs]e|\bdl\s*(?:no\.?|number)?\b", re.IGNORECASE)
    _WINDOW = 40
    # Above PhoneDetector's 0.95, for the same reason as
    # BankAccountNumberDetector: the 13-digit tail of a licence number is
    # phone-shaped enough that PHONE won every overlap, and the number was
    # redacted but recorded in the audit log under the wrong type. An
    # explicit "Driving Licence No" label is far stronger evidence than a
    # generic digit run, so it carries the higher confidence.
    _LABELLED_CONFIDENCE = 0.96

    def detect(self, text: str) -> List[PIIMatch]:
        matches = []
        for m in self._PATTERN.finditer(text):
            window_start = max(0, m.start() - self._WINDOW)
            if self._CONTEXT.search(text[window_start:m.start()]):
                matches.append(
                    PIIMatch("DRIVING_LICENSE", m.group(), m.start(), m.end(),
                             self._LABELLED_CONFIDENCE, "regex")
                )
        return matches


class BankAccountNumberDetector(PIIDetector):
    """
    Indian bank account number: 9 to 18 digits, with no fixed structure or
    checksum to validate against (unlike a credit card's Luhn check) --
    which is exactly why this is the most tightly context-gated detector in
    the codebase. A bare 9-18 digit run is essentially indistinguishable
    from an order number, a reference code, or any other long numeric ID a
    formal document is full of, so this ONLY fires next to an explicit
    "account no" / "a/c no" label -- there is no confidence level at which
    the bare pattern alone would be safe to flag.
    """

    name = "bank_account_number"
    _PATTERN = re.compile(r"(?<![A-Za-z0-9])\d{9,18}(?![A-Za-z0-9])")
    _CONTEXT = re.compile(r"\ba\s*/\s*c\b|account\s*(?:no\.?|number)?\b|bank\s*account", re.IGNORECASE)
    _WINDOW = 40
    # Deliberately above PhoneDetector's 0.95. A 9-18 digit run genuinely IS
    # phone-shaped, and libphonenumber will happily accept many account
    # numbers as "possible" numbers -- so on confidence alone PHONE won every
    # overlap and account numbers were never redacted as such. But this
    # detector only fires at all when an explicit "Account No" / "A/c"
    # label sits within `_WINDOW` characters, and that label is stronger,
    # more specific evidence than a generic digit-shape match. Expressing
    # that as a higher confidence is how the architecture intends conflicts
    # to be settled -- detectors stay independent and `resolve_overlaps`
    # arbitrates -- rather than teaching PhoneDetector about account labels.
    _LABELLED_CONFIDENCE = 0.96

    def detect(self, text: str) -> List[PIIMatch]:
        matches = []
        for m in self._PATTERN.finditer(text):
            window_start = max(0, m.start() - self._WINDOW)
            if self._CONTEXT.search(text[window_start:m.start()]):
                matches.append(
                    PIIMatch("BANK_ACCOUNT_NUMBER", m.group(), m.start(), m.end(),
                             self._LABELLED_CONFIDENCE, "regex")
                )
        return matches
