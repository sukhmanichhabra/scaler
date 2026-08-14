"""
Draws a reproducible sample of text blocks from the REAL source document,
to be hand-labelled as ground truth for the real-document evaluation.

Why sample at all: the prospectus runs to ~700 paragraphs and 76 tables.
Labelling every PII span in it by hand is not realistic, and labelling
*none* of it leaves the headline metrics resting entirely on a synthetic
corpus the same author wrote -- which measures the detector against its own
assumptions rather than against a real filing.

Sampling strategy (stratified, and reported as such):

  Stratum A - "PII-dense": blocks from the parts of the filing where
    personal data actually lives (cover page, management, promoters,
    registrar/banker contact blocks). Selected by looking for structural
    CONTACT MARKERS -- "Telephone:", "Email:", "Contact Person:",
    "Registered Office:" -- rather than by looking for the things the
    detector detects, so the sample is not drawn using the detector's own
    opinion. This stratum drives the recall estimate.

  Stratum B - "prose": blocks drawn uniformly at random from everything
    else: risk factors, regulatory boilerplate, industry commentary. Mostly
    contains no PII at all, which is exactly what makes it a fair test of
    precision (it is full of the capitalised defined terms and legal
    phrasing that a naive detector over-redacts).

Both strata are reported separately in EVALUATION.md as well as pooled, so
the pooled figure is never mistaken for a uniform-random estimate of the
whole document.

Run:  python evaluation/sample_real_document.py --input "Red Herring Prospectus.docx"
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docx import Document

from app.core.document_io import _iter_docx_paragraphs

HERE = os.path.dirname(os.path.abspath(__file__))

# Structural markers of a contact block. Deliberately NOT the patterns the
# detectors use -- these are document-layout cues, so the sample isn't
# selected by the thing being measured.
_CONTACT_MARKERS = re.compile(
    r"(Telephone|Tel\.?|Email|E-mail|Contact Person|Registered Office|"
    r"Corporate Office|Website|Compliance Officer|Investor Grievance)\s*[:.]",
    re.IGNORECASE,
)

_MIN_CHARS = 60
_MAX_CHARS = 700


def collect_blocks(path: str):
    doc = Document(path)
    seen = set()
    blocks = []
    for paragraph, context in _iter_docx_paragraphs(doc):
        text = "".join(run.text for run in paragraph.runs).strip()
        if not (_MIN_CHARS <= len(text) <= _MAX_CHARS):
            continue
        if text in seen:  # running headers repeat verbatim across sections
            continue
        seen.add(text)
        blocks.append({"text": text, "context": context})
    return blocks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Path to the source .docx")
    parser.add_argument("--dense", type=int, default=18, help="Stratum A sample size")
    parser.add_argument("--prose", type=int, default=18, help="Stratum B sample size")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    blocks = collect_blocks(args.input)
    dense = [b for b in blocks if _CONTACT_MARKERS.search(b["text"])]
    prose = [b for b in blocks if not _CONTACT_MARKERS.search(b["text"])]

    rng = random.Random(args.seed)
    sample = []
    for stratum, pool, size in (("dense", dense, args.dense), ("prose", prose, args.prose)):
        chosen = rng.sample(pool, min(size, len(pool)))
        for b in chosen:
            sample.append({
                "id": len(sample),
                "stratum": stratum,
                "context": b["context"],
                "text": b["text"],
            })

    out_path = os.path.join(HERE, "real_sample.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(sample, f, indent=2, ensure_ascii=False)

    print(f"Eligible blocks: {len(blocks)}  (dense={len(dense)}, prose={len(prose)})")
    print(f"Sampled {len(sample)} blocks -> {out_path}")


if __name__ == "__main__":
    main()
