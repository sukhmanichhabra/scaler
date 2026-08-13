"""
Flattens tracked changes and hyperlinks in a .docx XML tree before redaction.

Why this exists
----------------
`python-docx`'s `Paragraph.runs` only returns `<w:r>` elements that are
DIRECT children of `<w:p>`. Word wraps a tracked-change insertion in
`<w:ins>`, a deletion in `<w:del>` (using `<w:delText>` instead of `<w:t>`),
and display text in `<w:hyperlink>` -- in every case the run ends up one
level deeper, and `paragraph.runs` silently returns nothing for it.

Verified empirically: a paragraph containing only a `<w:ins>`-wrapped run
reports `len(paragraph.runs) == 0` and `paragraph.text == ""`. Since the
redaction pipeline builds its input from `"".join(r.text for r in
paragraph.runs)`, that text is invisible to every detector -- a tracked
insertion is not merely under-redacted, it is never seen at all. A
tracked deletion has the same blind spot in the other direction: it is
still physically present in the file (visible the instant someone turns
on "Show Markup"), so leaving it untouched is also a leak.

The fix used here is a normalization pass, run on the raw XML immediately
after the document is opened and before any paragraph is read for
detection:

  - `<w:ins>` (and `<w:moveTo>`) is UNWRAPPED: its child runs are promoted
    to the parent, exactly as if the edit had been accepted. This is what
    makes the inserted text visible to `paragraph.runs`.
  - `<w:del>` (and `<w:moveFrom>`) is REMOVED entirely: exactly as if the
    edit had been rejected -- I mean accepted, since a deletion being
    "accepted" means the text is gone. This closes the leak at the source
    rather than trying to detect and redact text that a `<w:delText>`
    element stores under a different tag than `<w:t>`.
  - `<w:hyperlink>` is UNWRAPPED: its display-text run is promoted to the
    parent, so hyperlink text (very often exactly the free-text email
    address someone pasted as a link) is reachable by `paragraph.runs`
    like any other run.

This means the tool always redacts a document as if every tracked change
had been accepted -- a deliberate, disclosed choice (see README), not an
accident of what `python-docx` happens to expose.
"""
from __future__ import annotations

from lxml import etree

from docx.oxml.ns import qn

# Wrappers whose content should be PROMOTED (the edit is treated as accepted).
_UNWRAP_TAGS = (qn("w:ins"), qn("w:moveTo"), qn("w:hyperlink"))
# Wrappers whose content should be DROPPED (the edit is treated as accepted,
# which for a deletion means the text no longer exists).
_REMOVE_TAGS = (qn("w:del"), qn("w:moveFrom"))


def _unwrap(element) -> None:
    parent = element.getparent()
    if parent is None:
        return
    index = list(parent).index(element)
    for i, child in enumerate(list(element)):
        parent.insert(index + i, child)
    parent.remove(element)


def flatten_revisions(root) -> None:
    """
    Mutates `root` in place: every tracked-change insertion and hyperlink is
    unwrapped, every tracked-change deletion is removed. Idempotent and safe
    to call on a tree with no revisions (a no-op in that case).

    `root` may be a document body, a header/footer element, or the root of a
    parsed footnotes/endnotes/comments part -- anywhere `<w:p>` elements can
    appear.
    """
    # Removal first: a `<w:del>` nested inside a `<w:ins>` (rare but legal --
    # text inserted and then deleted within the same tracked-changes
    # session) must not have its content resurrected by the unwrap pass.
    for tag in _REMOVE_TAGS:
        for element in root.findall(f".//{tag}"):
            parent = element.getparent()
            if parent is not None:
                parent.remove(element)

    # Unwrap repeatedly: `<w:hyperlink>` can itself sit inside a still-nested
    # `<w:ins>` in the other order (an inserted hyperlink), so one pass over
    # `_UNWRAP_TAGS` at a fixed set of matches can leave newly-exposed
    # wrappers unprocessed. Looping until a pass finds nothing left is
    # simpler and cheaper than reasoning about nesting order up front.
    while True:
        found = False
        for tag in _UNWRAP_TAGS:
            for element in root.findall(f".//{tag}"):
                _unwrap(element)
                found = True
        if not found:
            break
