"""
Redacts .docx content that lives outside the main body/table/header/footer
paragraph model: footnotes, endnotes, comments, and custom document
properties. Also detects (but does not attempt to redact) embedded objects
and images, since neither carries machine-readable text this pipeline can
scan.

Why these needed separate handling
-----------------------------------
`python-docx` gives first-class read/write access to body paragraphs, table
cells, and headers/footers -- that is what `document_io.py` uses directly.
Everything in this module falls outside that model for a different reason
each time:

  - Comments have a real API (`document.part.comments`) but it was never
    wired into the redaction walk, and a comment's `.author` is a real name
    written directly into the file regardless of what the comment text
    says.
  - Footnotes and endnotes have NO `python-docx` API at all in this version
    (confirmed: no `docx.parts.footnotes` module exists). They are still
    ordinary WordprocessingML -- `<w:footnote>`/`<w:endnote>` elements each
    containing normal `<w:p>` paragraphs -- so they can be redacted with the
    exact same paragraph/run machinery used everywhere else, once the part's
    raw XML is parsed and, after editing, written back. `Part.blob` is
    normally read-only in effect (subclasses override it to serialize a live
    element tree; the generic `Part` just returns whatever bytes it was
    loaded with) but setting the private `_blob` attribute directly and
    confirming the change survives `Document.save()` was verified to work
    before this was built on top of it.
  - Custom document properties (`docProps/custom.xml`) have no `python-docx`
    API either, and are free-form user-defined key/value pairs -- a
    "Client Name" or "Prepared For" property is exactly as much a leak as
    the same text in the body. Handled as a raw post-save zip rewrite, since
    there is nothing in the `python-docx` object graph to hang it off.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional

from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph
from lxml import etree

from .docx_revisions import flatten_revisions

_CUSTOM_PROPS_PARTNAME = "/docProps/custom.xml"
# `python-docx`'s `qn()` only resolves prefixes it has pre-registered, and
# "vt" (docProps custom-property variant types) isn't one of them -- so the
# namespace URI is spelled out directly here instead.
_VT_NS = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"
_VT_VALUE_TAGS = (f"{{{_VT_NS}}}lpwstr", f"{{{_VT_NS}}}lpstr", f"{{{_VT_NS}}}bstr")


@dataclass
class HiddenContentReport:
    """What was found and touched outside the main paragraph model."""

    footnote_paragraphs_redacted: int = 0
    endnote_paragraphs_redacted: int = 0
    comment_paragraphs_redacted: int = 0
    comment_authors_scrubbed: int = 0
    custom_properties_redacted: int = 0
    embedded_images: int = 0
    embedded_objects: int = 0
    warnings: List[str] = field(default_factory=list)


def _note_or_footnote_part(document: Document, suffix: str):
    for rel in document.part.rels.values():
        if rel.reltype.endswith(suffix) and not rel.is_external:
            return rel.target_part
    return None


def _redact_note_part(part, redact_paragraph: Callable[[Paragraph], bool], container_tag: str) -> int:
    """
    Shared logic for footnotes.xml / endnotes.xml: parse the part's raw XML,
    flatten any tracked changes inside it, run every real (non-separator)
    note's paragraphs through the same per-paragraph redaction callback the
    rest of the document uses, then write the mutated tree back.

    Separator/continuation-separator notes (`w:type="separator"` etc.) carry
    no user text -- just the visual divider Word draws above footnotes -- so
    they are skipped rather than fed to detectors for nothing.
    """
    if part is None:
        return 0
    root = parse_xml(part.blob)
    flatten_revisions(root)
    touched = 0
    for note in root.iter(qn(container_tag)):
        note_type = note.get(qn("w:type"))
        if note_type in ("separator", "continuationSeparator"):
            continue
        for p_el in note.findall(qn("w:p")):
            paragraph = Paragraph(p_el, part)
            if redact_paragraph(paragraph):
                touched += 1
    part._blob = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    return touched


def redact_footnotes(document: Document, redact_paragraph: Callable[[Paragraph], bool]) -> int:
    part = _note_or_footnote_part(document, "/footnotes")
    return _redact_note_part(part, redact_paragraph, "w:footnote")


def redact_endnotes(document: Document, redact_paragraph: Callable[[Paragraph], bool]) -> int:
    part = _note_or_footnote_part(document, "/endnotes")
    return _redact_note_part(part, redact_paragraph, "w:endnote")


def redact_comments(document: Document, redact_paragraph: Callable[[Paragraph], bool],
                     fake_author: Callable[[str], str]) -> "tuple[int, int]":
    """
    Redacts every comment's paragraph text via the normal per-paragraph path,
    and separately scrubs `.author`/`.initials` -- a reviewer's real name
    embedded in the comment metadata itself, independent of whatever the
    comment text says. `fake_author` maps an original author name to a
    consistent fake one (the same `ConsistentFaker` instance used for
    PERSON, so "Jane Reviewer" the commenter and "Jane Reviewer" if she is
    also named in the body resolve to the same fake identity).
    """
    comments = document.part.comments
    paragraphs_touched = 0
    authors_touched = 0
    for comment in comments:
        for paragraph in comment.paragraphs:
            if redact_paragraph(paragraph):
                paragraphs_touched += 1
        if comment.author:
            comment.author = fake_author(comment.author)
            authors_touched += 1
        if comment.initials:
            comment.initials = "".join(w[0] for w in comment.author.split()[:2]).upper() or "XX"
    return paragraphs_touched, authors_touched


def redact_custom_properties(docx_path: str, redact_text: Callable[[str], str]) -> int:
    """
    Rewrites `docProps/custom.xml` in place (a raw zip edit -- see module
    docstring for why `python-docx` can't be used here). Every string-typed
    property value is passed through the same redaction function used for
    body text; non-string property types (dates, numbers, booleans) carry no
    free text and are left alone. No-ops silently if the part doesn't exist,
    which is the common case (custom properties are opt-in in Word).
    """
    import zipfile

    with zipfile.ZipFile(docx_path) as zin:
        names = zin.namelist()
        if "docProps/custom.xml" not in names:
            return 0
        entries = {n: zin.read(n) for n in names}

    root = parse_xml(entries["docProps/custom.xml"])
    touched = 0
    for value_el in root.iter():
        if value_el.tag in _VT_VALUE_TAGS and value_el.text:
            redacted = redact_text(value_el.text)
            if redacted != value_el.text:
                value_el.text = redacted
                touched += 1
    if touched:
        entries["docProps/custom.xml"] = etree.tostring(
            root, xml_declaration=True, encoding="UTF-8", standalone=True
        )
        with zipfile.ZipFile(docx_path, "w", zipfile.ZIP_DEFLATED) as zout:
            for name, data in entries.items():
                zout.writestr(name, data)
    return touched


def detect_unredactable_content(document: Document) -> "tuple[int, int]":
    """
    Counts embedded images and embedded objects (OLE spreadsheets, other
    embedded files, etc.) so the caller can warn that this content was NOT
    scanned. Neither is addressed by this tool: image text would require
    OCR, and embedded objects are opaque binary blobs -- both are explicitly
    out of scope rather than silently unhandled, per the disclosed
    limitation in the README.
    """
    # Matched by local tag name rather than a specific namespace prefix/URI:
    # Word documents use inconsistent namespace aliasing across producers,
    # and the local name ("pic", "OLEObject") is what actually identifies
    # the element regardless of which prefix maps to which URI in a given
    # file.
    body = document.element.body
    images = sum(1 for el in body.iter() if isinstance(el.tag, str) and etree.QName(el).localname == "pic")
    objects = sum(1 for el in body.iter() if isinstance(el.tag, str) and etree.QName(el).localname == "OLEObject")
    return images, objects
