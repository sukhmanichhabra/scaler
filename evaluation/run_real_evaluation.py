"""
Scores the detector against hand-labelled ground truth drawn from the REAL
source document (see sample_real_document.py for how the sample was taken
and real_ground_truth.json for the labelling policy).

This complements run_evaluation.py, which scores the synthetic corpus. The
synthetic corpus is useful because it can contain every PII type on demand,
but it is written by the same person who wrote the detectors -- so it
measures the detector against its author's assumptions. This harness
measures it against a filing nobody wrote for it.

Matching rules are identical to the synthetic harness so the two numbers
are comparable: a prediction is a true positive when it shares a gold
span's type and overlaps it by >= 50% of the gold span's length.

Run: python evaluation/run_real_evaluation.py
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.defined_terms import extract_defined_terms
from app.core.detectors.base import DetectionConfig
from app.core.detectors.registry import apply_known_names, build_known_names
from app.core.redactor import Redactor

HERE = os.path.dirname(os.path.abspath(__file__))

# Matches how the delivered document was produced.
ISSUER_NAMES = {
    "ksh international limited", "ksh international",
    "bhandary metal extrusion private limited", "bhandary metal extrusion",
}


def overlap_ratio(a_start, a_end, b_start, b_end):
    inter = max(0, min(a_end, b_end) - max(a_start, b_start))
    b_len = b_end - b_start
    return inter / b_len if b_len else 0.0


def resolve_label_offsets(block_text: str, label: dict, used: set):
    """
    Turns a labelled substring into concrete offsets. Repeated values within
    one block (the same email twice) are handled by taking the next unused
    occurrence, so duplicates don't silently collapse onto one span.
    """
    start = -1
    search_from = 0
    while True:
        found = block_text.find(label["value"], search_from)
        if found == -1:
            break
        if (found, found + len(label["value"])) not in used:
            start = found
            break
        search_from = found + 1
    if start == -1:
        raise ValueError(
            f"Label {label['value']!r} not found in block {label['id']} -- "
            "ground truth is out of sync with real_sample.json."
        )
    end = start + len(label["value"])
    used.add((start, end))
    return start, end


def evaluate(source_docx: str = "Red Herring Prospectus.docx", overlap_threshold: float = 0.5):
    with open(os.path.join(HERE, "real_sample.json"), encoding="utf-8") as f:
        blocks = json.load(f)
    with open(os.path.join(HERE, "real_ground_truth.json"), encoding="utf-8") as f:
        gt = json.load(f)["labels"]

    # The glossary stoplist is part of the pipeline being measured, so the
    # evaluation must load it exactly as redact_docx would.
    defined_terms = set()
    doc_path = os.path.join(os.path.dirname(HERE), source_docx)
    if os.path.exists(doc_path):
        from docx import Document
        defined_terms = extract_defined_terms(Document(doc_path))

    labels_by_block = defaultdict(list)
    for label in gt:
        labels_by_block[label["id"]].append(label)

    redactor = Redactor(config=DetectionConfig(), issuer_names=ISSUER_NAMES,
                        defined_terms=defined_terms)

    per_type = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    per_stratum = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    false_positives, false_negatives = [], []

    for block in blocks:
        text = block["text"]
        stratum = block["stratum"]

        matches = redactor.detect(text, context=block.get("context", ""))
        matches = apply_known_names(text, matches, build_known_names(matches))
        predictions = [{"start": m.start, "end": m.end, "type": m.entity_type, "text": m.text}
                       for m in matches]

        used = set()
        gold = []
        for label in labels_by_block.get(block["id"], []):
            start, end = resolve_label_offsets(text, label, used)
            gold.append({"start": start, "end": end, "type": label["type"], "text": label["value"]})

        matched_pred, matched_gold = set(), set()
        for gi, g in enumerate(gold):
            for pi, p in enumerate(predictions):
                if pi in matched_pred or p["type"] != g["type"]:
                    continue
                if overlap_ratio(p["start"], p["end"], g["start"], g["end"]) >= overlap_threshold:
                    matched_pred.add(pi)
                    matched_gold.add(gi)
                    per_type[g["type"]]["tp"] += 1
                    per_stratum[stratum]["tp"] += 1
                    break

        for gi, g in enumerate(gold):
            if gi not in matched_gold:
                per_type[g["type"]]["fn"] += 1
                per_stratum[stratum]["fn"] += 1
                false_negatives.append({"block": block["id"], "stratum": stratum, **g})
        for pi, p in enumerate(predictions):
            if pi not in matched_pred:
                per_type[p["type"]]["fp"] += 1
                per_stratum[stratum]["fp"] += 1
                false_positives.append({"block": block["id"], "stratum": stratum, **p})

    def prf(counts):
        tp, fp, fn = counts["tp"], counts["fp"], counts["fn"]
        precision = tp / (tp + fp) if (tp + fp) else (1.0 if fn == 0 else 0.0)
        recall = tp / (tp + fn) if (tp + fn) else (1.0 if fp == 0 else 0.0)
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        return {**counts, "precision": precision, "recall": recall, "f1": f1}

    totals = {"tp": sum(c["tp"] for c in per_type.values()),
              "fp": sum(c["fp"] for c in per_type.values()),
              "fn": sum(c["fn"] for c in per_type.values())}
    totals = prf(totals)
    totals["accuracy"] = (totals["tp"] / (totals["tp"] + totals["fp"] + totals["fn"])
                          if (totals["tp"] + totals["fp"] + totals["fn"]) else 0.0)

    return {
        "blocks": len(blocks),
        "gold_spans": len(gt),
        "overlap_threshold": overlap_threshold,
        "totals": totals,
        "per_type": {t: prf(c) for t, c in sorted(per_type.items())},
        "per_stratum": {s: prf(c) for s, c in sorted(per_stratum.items())},
        "false_positives": false_positives,
        "false_negatives": false_negatives,
    }


if __name__ == "__main__":
    report = evaluate()
    with open(os.path.join(HERE, "real_evaluation_results.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    t = report["totals"]
    print(f"REAL DOCUMENT — {report['blocks']} sampled blocks, {report['gold_spans']} labelled spans\n")
    print(f"OVERALL  precision={t['precision']:.3f}  recall={t['recall']:.3f}  "
          f"f1={t['f1']:.3f}  accuracy={t['accuracy']:.3f}  "
          f"(TP={t['tp']} FP={t['fp']} FN={t['fn']})\n")

    print(f"{'TYPE':16s} {'TP':>4s} {'FP':>4s} {'FN':>4s} {'PREC':>7s} {'REC':>7s} {'F1':>7s}")
    for name, m in report["per_type"].items():
        print(f"{name:16s} {m['tp']:4d} {m['fp']:4d} {m['fn']:4d} "
              f"{m['precision']:7.3f} {m['recall']:7.3f} {m['f1']:7.3f}")

    print(f"\n{'STRATUM':16s} {'TP':>4s} {'FP':>4s} {'FN':>4s} {'PREC':>7s} {'REC':>7s}")
    for name, m in report["per_stratum"].items():
        print(f"{name:16s} {m['tp']:4d} {m['fp']:4d} {m['fn']:4d} "
              f"{m['precision']:7.3f} {m['recall']:7.3f}")

    print("\nFalse negatives (missed PII):")
    for fn in report["false_negatives"]:
        print(f"  [{fn['type']}] block {fn['block']}: {fn['text']!r}")
    print("\nFalse positives (over-redacted):")
    for fp in report["false_positives"]:
        print(f"  [{fp['type']}] block {fp['block']}: {fp['text']!r}")
