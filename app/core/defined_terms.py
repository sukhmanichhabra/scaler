"""
Extracts a document's OWN glossary of defined terms, to be used as a
per-document stoplist for the NER detectors.

Why this exists
---------------
Formal filings (a Red Herring Prospectus especially) are written against a
glossary: "Equity Shares", "the Offer Price", "Anchor Investors",
"Promoter Selling Shareholders" are all *defined terms*, capitalised
throughout the document precisely because they carry a specific legal
meaning. A statistical NER model sees a multi-word Title Case phrase and
reasonably guesses "organisation" -- which is how a redactor ends up
replacing "Equity Shares" with a fake company name 36 times, destroying
both precision and readability.

The fix used here is document-aware rather than hardcoded: an RHP contains
its own answer in the "Definitions and Abbreviations" section, laid out as
`Term | Description` tables. Parsing that section yields an exact,
per-document list of phrases that are by definition NOT PII. This
generalises to any filing that ships a glossary, instead of relying on a
lexicon curated against one example document.

Guard against over-stoplisting
------------------------------
Some glossary rows define an ALIAS FOR A REAL COMPANY -- e.g.
`Nuvama / Nuvama Wealth Management Limited`. Stoplisting those would
suppress redaction of a genuine company name (a recall loss, and a privacy
one). So any row where *any* alternative carries a corporate suffix is
skipped entirely, leaving those names fully redactable.
"""
from __future__ import annotations

import re
from typing import List, Set

# Header labels that mark a two-column glossary table.
_TERM_HEADERS = {"term", "terms"}
_DESC_HEADERS = {"description", "definition", "meaning", "descriptions"}

# A glossary row naming a real company ("... Limited", "... LLP") is an alias
# for an actual organisation, not a generic legal term -- see module docstring.
_CORP_SUFFIX = re.compile(
    r"\b(Ltd|Limited|LLP|Pvt|Private|Inc|Corp|Corporation|Bank|PLC|LLC|"
    r"Partners|Associates|Advisors|Securities)\b",
    re.IGNORECASE,
)

# Glossary terms routinely bundle synonyms in one cell:
#   "AoA/Articles of Association or Articles"
#   "Our Company/ the Company"
_ALTERNATIVE_SPLIT = re.compile(r"\s*/\s*|\s+\bor\b\s+", re.IGNORECASE)

# "Director(s)" should stoplist both "Director" and "Directors".
_OPTIONAL_PLURAL = re.compile(r"\(s\)$", re.IGNORECASE)

_LEADING_ARTICLE = re.compile(r"^(the|our|an|a)\s+", re.IGNORECASE)

_MAX_TERM_LEN = 60  # anything longer is prose, not a defined term

# A glossary row whose DESCRIPTION is just a company name, and whose term
# appears inside that name, is an alias for a real organisation:
#   "Nuvama"  ->  "Nuvama Wealth Management Limited"
# Stoplisting the term there would leave a real bank's name unredacted. By
# contrast "Corporate Promoter" -> "Waterloo Industrial Park VI Private
# Limited" is a genuine role label (the term is NOT part of the company
# name), so it stays stoplisted and the company itself stays redactable.
_MAX_ALIAS_DESC_LEN = 90


def _clean(raw: str) -> str:
    text = raw.replace("“", "").replace("”", "").replace('"', "")
    return re.sub(r"\s+", " ", text).strip().strip(",;:")


def normalize_term(text: str) -> str:
    """Lookup key for comparing a detected span against the glossary."""
    return _LEADING_ARTICLE.sub("", _clean(text).lower()).strip()


def _is_company_alias(alternatives: List[str], description: str) -> bool:
    desc = _clean(description)
    if not desc or len(desc) > _MAX_ALIAS_DESC_LEN:
        return False
    if not _CORP_SUFFIX.search(desc):
        return False
    desc_l = desc.lower()
    return any(a.strip() and a.strip().lower() in desc_l for a in alternatives)


def _variants(term: str) -> List[str]:
    """All surface forms a defined term may appear as in the body text."""
    out: List[str] = []
    base = _clean(term)
    if not base:
        return out
    stems = [base]
    if _OPTIONAL_PLURAL.search(base):
        stem = _OPTIONAL_PLURAL.sub("", base).strip()
        stems = [stem]
    for form in stems:
        form = form.strip()
        if not form or len(form) > _MAX_TERM_LEN:
            continue
        # The glossary defines "Bidder" but the body says "Bidders";
        # it defines "Equity Shares" but the body says "Equity Share".
        # Index both directions so either surface form is recognised.
        surface = {form}
        if len(form) > 3:
            surface.add(form[:-1] if form.lower().endswith("s") else form + "s")
        for f in surface:
            out.append(f)
            stripped = _LEADING_ARTICLE.sub("", f).strip()
            if stripped and stripped != f:
                out.append(stripped)
    return out


def _is_glossary_table(table) -> bool:
    try:
        header = [c.text.strip().lower() for c in table.rows[0].cells]
    except (IndexError, AttributeError):
        return False
    if len(header) < 2:
        return False
    return header[0] in _TERM_HEADERS and any(h in _DESC_HEADERS for h in header[1:])


def extract_defined_terms(doc) -> Set[str]:
    """
    Returns lowercase defined terms found in the document's own glossary
    tables. Empty set if the document has no glossary -- detection then
    behaves exactly as it did before, so this is purely additive.
    """
    terms: Set[str] = set()
    for table in doc.tables:
        if not _is_glossary_table(table):
            continue
        for row in table.rows[1:]:
            cells = row.cells
            if len(cells) < 2:
                continue
            term_cell = _clean(cells[0].text)
            if not term_cell or len(term_cell) > _MAX_TERM_LEN * 3:
                continue
            alternatives = [a for a in _ALTERNATIVE_SPLIT.split(term_cell) if a and a.strip()]
            if not alternatives:
                continue
            # Row defines an alias for a real company -> keep it redactable.
            if any(_CORP_SUFFIX.search(a) for a in alternatives):
                continue
            if _is_company_alias(alternatives, cells[1].text):
                continue
            for alt in alternatives:
                for variant in _variants(alt):
                    terms.add(normalize_term(variant))
    terms.discard("")
    return terms
