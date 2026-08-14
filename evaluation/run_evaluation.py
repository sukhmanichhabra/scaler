"""
Evaluation harness.

Methodology (documented in EVALUATION.md too):
  - Entity-level matching: a detector match is a True Positive if it overlaps
    a ground-truth span of the SAME type by at least 50% of the ground-truth
    span's length (a common, forgiving-but-meaningful threshold used in NER
    evaluation -- it credits a detector that gets the substance of a span
    right even if a boundary character or two differs, e.g. trailing
    punctuation, without crediting a detector that only grazes a span).
  - Every ground-truth span not matched is a False Negative (missed PII --
    hurts recall).
  - Every detector match that doesn't overlap any ground-truth span (of any
    type, to also catch type confusion) is a False Positive (over-redaction
    -- hurts precision).
  - Metrics are computed per PII type, plus a micro-averaged overall
    precision/recall/F1, plus an overall "accuracy" defined as
    TP / (TP + FP + FN) -- i.e. of every span either side flagged as
    interesting, the fraction both sides agreed on. This is NOT the same as
    classic classification accuracy (there's no meaningful "true negative"
    count in span detection over free text), and the report says so
    explicitly rather than presenting a misleading number.

Run: python evaluation/run_evaluation.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.detectors.base import DetectionConfig
from app.core.redactor import Redactor

HERE = os.path.dirname(os.path.abspath(__file__))


def overlap_ratio(a_start, a_end, b_start, b_end):
    inter = max(0, min(a_end, b_end) - max(a_start, b_start))
    b_len = b_end - b_start
    return inter / b_len if b_len else 0.0


def evaluate(overlap_threshold: float = 0.5):
    with open(os.path.join(HERE, "test_corpus.txt"), encoding="utf-8") as f:
        text = f.read()
    with open(os.path.join(HERE, "ground_truth.json"), encoding="utf-8") as f:
        gold = json.load(f)

    redactor = Redactor(config=DetectionConfig(), issuer_names={"novaweave technologies limited", "novaweave"})
    predicted = redactor.detect(text)
    pred = [{"start": m.start, "end": m.end, "type": m.entity_type, "text": m.text} for m in predicted]

    matched_gold_idx = set()
    matched_pred_idx = set()
    tp_pairs = []

    # Greedy matching: for each gold span, find the first unmatched predicted
    # span of the same type with sufficient overlap.
    for gi, g in enumerate(gold):
        for pi, p in enumerate(pred):
            if pi in matched_pred_idx or p["type"] != g["type"]:
                continue
            if overlap_ratio(p["start"], p["end"], g["start"], g["end"]) >= overlap_threshold:
                matched_gold_idx.add(gi)
                matched_pred_idx.add(pi)
                tp_pairs.append((g, p))
                break

    false_negatives = [g for gi, g in enumerate(gold) if gi not in matched_gold_idx]
    false_positives = [p for pi, p in enumerate(pred) if pi not in matched_pred_idx]

    types = sorted(set([g["type"] for g in gold] + [p["type"] for p in pred]))
    per_type = {}
    for t in types:
        tp = sum(1 for g, p in tp_pairs if g["type"] == t)
        fn = sum(1 for g in false_negatives if g["type"] == t)
        fp = sum(1 for p in false_positives if p["type"] == t)
        precision = tp / (tp + fp) if (tp + fp) else (1.0 if fn == 0 else 0.0)
        recall = tp / (tp + fn) if (tp + fn) else (1.0 if fp == 0 else 0.0)
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        per_type[t] = {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall, "f1": f1}

    total_tp = len(tp_pairs)
    total_fp = len(false_positives)
    total_fn = len(false_negatives)
    overall_precision = total_tp / (total_tp + total_fp) if (total_tp + total_fp) else 0.0
    overall_recall = total_tp / (total_tp + total_fn) if (total_tp + total_fn) else 0.0
    overall_f1 = (2 * overall_precision * overall_recall / (overall_precision + overall_recall)
                  if (overall_precision + overall_recall) else 0.0)
    overall_accuracy = total_tp / (total_tp + total_fp + total_fn) if (total_tp + total_fp + total_fn) else 0.0

    report = {
        "overlap_threshold": overlap_threshold,
        "totals": {
            "tp": total_tp, "fp": total_fp, "fn": total_fn,
            "precision": overall_precision, "recall": overall_recall,
            "f1": overall_f1, "accuracy": overall_accuracy,
        },
        "per_type": per_type,
        "false_negatives": false_negatives,
        "false_positives": false_positives,
    }
    return report


if __name__ == "__main__":
    report = evaluate()
    with open(os.path.join(HERE, "evaluation_results.json"), "w") as f:
        json.dump(report, f, indent=2)

    t = report["totals"]
    print(f"OVERALL  precision={t['precision']:.3f}  recall={t['recall']:.3f}  "
          f"f1={t['f1']:.3f}  accuracy={t['accuracy']:.3f}  "
          f"(TP={t['tp']} FP={t['fp']} FN={t['fn']})\n")
    print(f"{'TYPE':16s} {'TP':>4s} {'FP':>4s} {'FN':>4s} {'PREC':>7s} {'REC':>7s} {'F1':>7s}")
    for t_name, m in sorted(report["per_type"].items()):
        print(f"{t_name:16s} {m['tp']:4d} {m['fp']:4d} {m['fn']:4d} "
              f"{m['precision']:7.3f} {m['recall']:7.3f} {m['f1']:7.3f}")

    print("\nFalse negatives (missed):")
    for fn in report["false_negatives"]:
        print(f"  [{fn['type']}] {fn['text']!r}")

    print("\nFalse positives (over-flagged):")
    for fp in report["false_positives"]:
        print(f"  [{fp['type']}] {fp['text']!r}")
