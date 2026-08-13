"""
Generates realistic fake replacement values (via the Faker library) and
guarantees that the SAME original value always maps to the SAME fake value
within a document -- e.g. every occurrence of "rohan.dey@gmail.com" becomes
"peter.parker@example.com", and every occurrence of "Rohan Dey" becomes
"Peter Parker", exactly like the worked example in the assignment.

Fake person names and their fake emails are also cross-linked where
possible (a name mapped to "Peter Parker" will bias new emails for the same
original person toward peter.parker@example.com-style addresses) purely for
output readability -- this is cosmetic, not a privacy requirement.
"""
from __future__ import annotations

import hashlib
import ipaddress
import random
import re
from typing import Dict, Tuple

from faker import Faker
from faker.exceptions import UniquenessException


class ConsistentFaker:
    def __init__(self, seed: int = 42, locale: str = "en_IN"):
        # Locale matters for readability of the output: replacing a Pune
        # address with "998 Chelsea Shoals, Sandrastad, AL" or an Indian
        # director with "Allison Hill" makes a redacted Indian filing read
        # as obviously synthetic. Faker's en_IN provider keeps names,
        # addresses and companies plausible for the source document.
        self.faker = Faker(locale)
        # SSNs are a US identifier and en_IN has no provider for them, so a
        # dedicated US generator is kept for that one type.
        self.faker_us = Faker("en_US")
        # A fixed seed makes runs reproducible (useful for evaluation and for
        # the "same input -> same output" expectation in demos/tests). Each
        # generator instance still produces distinct values across a
        # document because Faker's internal state advances with each call.
        Faker.seed(seed)
        random.seed(seed)
        self._cache: Dict[Tuple[str, str], str] = {}
        self._person_email_hint: Dict[str, str] = {}  # normalized original name -> fake full name
        self._aliases: Dict[str, str] = {}  # short name form -> canonical full name

    @staticmethod
    def _unique(generator):
        """
        Faker's `.unique` proxy raises once its retry budget is exhausted,
        which a long document can genuinely hit (this prospectus alone needs
        ~560 distinct company names). Uniqueness is a readability nicety,
        not a privacy requirement, so fall back to a plain draw rather than
        failing the whole redaction.
        """
        try:
            return generator.unique()
        except (UniquenessException, AttributeError):
            return generator()

    def register_aliases(self, aliases: Dict[str, str]) -> None:
        """
        Declares that different spellings refer to the same person, so they
        resolve to one fake identity ("Pushpa Hegde" and "Pushpa Kushal
        Hegde" must not become two different fake people). Keys and values
        are normalized lowercase names -- see `registry.derive_name_aliases`.
        """
        self._aliases.update(aliases)

    def _key(self, entity_type: str, original: str) -> Tuple[str, str]:
        normalized = original.strip().lower()
        # Short forms of the same person or company resolve to one identity.
        if entity_type in ("PERSON", "ORG"):
            normalized = self._aliases.get(normalized, normalized)
        return (entity_type, normalized)

    #: types whose replacement should follow the casing of the text it
    #: replaces (see `_match_case`)
    _CASE_SENSITIVE_TYPES = {"PERSON", "ORG", "ADDRESS"}

    @staticmethod
    def _match_case(original: str, fake: str) -> str:
        """
        Mirrors an ALL-CAPS original in its replacement.

        Formal filings set the same name in Title Case in body prose and in
        ALL CAPS on cover pages and in headings. Because the cache is keyed
        case-insensitively, both mentions resolve to one fake identity --
        which is what consistency requires -- but writing the Title Case
        form into an ALL-CAPS line visibly breaks the document's
        typography. Casing is applied on retrieval so the identity stays
        stable while the typography follows its context.
        """
        if original.isupper() and any(c.isalpha() for c in original):
            return fake.upper()
        return fake

    def fake_for(self, entity_type: str, original: str) -> str:
        key = self._key(entity_type, original)
        if key in self._cache:
            value = self._cache[key]
        else:
            generator = getattr(self, f"_fake_{entity_type.lower()}", None)
            value = generator(original) if generator else self._fake_generic(original)
            self._cache[key] = value
        if entity_type in self._CASE_SENSITIVE_TYPES:
            return self._match_case(original, value)
        return value

    # ---- per-type fake generators -----------------------------------

    def _fake_person(self, original: str) -> str:
        fake_name = self._unique(self.faker.unique.name)
        self._person_email_hint[original.strip().lower()] = fake_name
        return fake_name

    def _fake_email(self, original: str) -> str:
        # try to reuse a previously faked name for a nicer, linked-looking address
        local_part_seed = original.split("@")[0].lower()
        hinted_name = None
        for orig_name, fname in self._person_email_hint.items():
            if orig_name.replace(" ", ".") in local_part_seed or orig_name.replace(" ", "") in local_part_seed.replace(".", ""):
                hinted_name = fname
                break
        if hinted_name:
            local = hinted_name.lower().replace(" ", ".")
        else:
            local = self.faker.unique.user_name()
        return f"{local}@example.com"

    def _fake_phone(self, original: str) -> str:
        """
        Rebuilds the number digit-for-digit in the original's own shape.

        A prospectus mixes mobiles ("+91 98765 43210"), landlines with area
        codes ("+91 22 4009 4400") and local forms ("020-45053237").
        Replacing all of them with one 10-digit mobile pattern loses that
        distinction and reads wrong next to a "Tel:" label. Preserving the
        separators and group lengths -- and only randomising the digits --
        keeps every variant looking like what it replaced.
        """
        stripped = original.strip()
        cc_match = re.match(r"^\+(\d{1,3})[\s-]*", stripped)
        prefix = cc_match.group(0) if cc_match else ""
        body = stripped[len(prefix):]
        digits = re.sub(r"\D", "", body)
        if not digits:
            return original

        # Some digits are structural rather than identifying: a bare "91"
        # country code, or the "0" that marks a landline area code. Keeping
        # them makes the fake number read as the same KIND of number.
        if not prefix and len(digits) >= 11 and digits.startswith("91"):
            keep = 2
        elif digits.startswith("0"):
            keep = 1
        else:
            keep = 0

        new_digits = list(digits[:keep])
        remaining = len(digits) - keep
        for i in range(remaining):
            if i == 0 and remaining == 10 and not keep:
                new_digits.append(str(random.randint(6, 9)))  # Indian mobile series
            elif i == 0:
                new_digits.append(str(random.randint(1, 9)))  # no second leading zero
            else:
                new_digits.append(str(random.randint(0, 9)))

        # Re-inject into the original's separator template.
        stream = iter(new_digits)
        rebuilt = "".join(next(stream) if ch.isdigit() else ch for ch in body)
        return prefix + rebuilt

    def _fake_org(self, original: str) -> str:
        return self._unique(self.faker.unique.company)

    def _fake_address(self, original: str) -> str:
        return self.faker.address().replace("\n", ", ")

    def _fake_ssn(self, original: str) -> str:
        # US identifier -- en_IN has no SSN provider, so use the US locale.
        return self.faker_us.ssn()

    def _fake_credit_card(self, original: str) -> str:
        return self.faker.credit_card_number()

    def _fake_date_of_birth(self, original: str) -> str:
        dob = self.faker.date_of_birth(minimum_age=18, maximum_age=75)
        return dob.strftime("%d %B %Y")

    def _fake_ip_address(self, original: str) -> str:
        try:
            ipaddress.IPv6Address(original)
            return self.faker.ipv6()
        except ValueError:
            return self.faker.ipv4_public()

    def _fake_pan_number(self, original: str) -> str:
        letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        return "".join(random.choice(letters) for _ in range(5)) + \
            "".join(str(random.randint(0, 9)) for _ in range(4)) + \
            random.choice(letters)

    def _fake_aadhaar_number(self, original: str) -> str:
        digits = "".join(str(random.randint(0, 9)) for _ in range(12))
        return f"{digits[0:4]} {digits[4:8]} {digits[8:12]}"

    def _fake_ifsc_code(self, original: str) -> str:
        letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        bank_code = "".join(random.choice(letters) for _ in range(4))
        branch_code = "".join(random.choice(letters + "0123456789") for _ in range(6))
        return f"{bank_code}0{branch_code}"

    def _fake_gstin(self, original: str) -> str:
        state_code = f"{random.randint(1, 37):02d}"
        pan_like = self._fake_pan_number(original)
        entity_code = random.choice("123456789")
        checksum = random.choice("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        return f"{state_code}{pan_like}{entity_code}Z{checksum}"

    def _fake_upi_id(self, original: str) -> str:
        handles = ("okhdfcbank", "ybl", "oksbi", "paytm", "okicici", "okaxis")
        local = self.faker.unique.user_name().replace(".", "").replace("_", "")
        return f"{local}@{random.choice(handles)}"

    def _fake_passport_number(self, original: str) -> str:
        letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        return random.choice(letters) + "".join(str(random.randint(0, 9)) for _ in range(7))

    def _fake_voter_id(self, original: str) -> str:
        letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        return "".join(random.choice(letters) for _ in range(3)) + \
            "".join(str(random.randint(0, 9)) for _ in range(7))

    def _fake_driving_license(self, original: str) -> str:
        letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        state = "".join(random.choice(letters) for _ in range(2))
        rto = f"{random.randint(1, 99):02d}"
        year = random.randint(1990, 2023)
        serial = f"{random.randint(0, 9_999_999):07d}"
        return f"{state}{rto} {year}{serial}"

    def _fake_bank_account_number(self, original: str) -> str:
        # Preserved length, same reasoning as phone shape preservation: an
        # account number's LENGTH is itself a mild signal (different banks
        # use different lengths), so a fixed-length fake would be a subtler
        # tell that the document has been altered at that exact spot.
        digit_count = sum(1 for c in original if c.isdigit()) or 12
        first = str(random.randint(1, 9))
        rest = "".join(str(random.randint(0, 9)) for _ in range(digit_count - 1))
        return first + rest

    def _fake_generic(self, original: str) -> str:
        # deterministic fallback for any entity type without a dedicated generator
        digest = hashlib.sha256(original.encode()).hexdigest()[:8]
        return f"REDACTED-{digest}"
