#!/usr/bin/env python3
"""
Standalone CLI for the PII Redaction Tool.

Usage:
    python cli.py --input "Red Herring Prospectus.pdf" --output redacted.docx
    python cli.py --input notes.txt --output redacted.docx --redact-issuer-company
    python cli.py --input filing.docx --output redacted.docx --issuer "Acme Corp" "Acme"
    python cli.py --input filing.docx --output redacted.docx --disable ADDRESS DATE_OF_BIRTH
"""
from __future__ import annotations

import argparse
import json
import sys

from app.core.detectors.base import ALL_PII_TYPES, DetectionConfig
from app.core.document_io import redact_file
from app.core.redactor import Redactor

ALL_TYPES = ALL_PII_TYPES


def main():
    parser = argparse.ArgumentParser(description="Redact PII from a document, output a .docx")
    parser.add_argument("--input", required=True, help="Path to input file (.docx, .pdf, or .txt)")
    parser.add_argument("--output", required=True, help="Path to write the redacted .docx")
    parser.add_argument("--issuer", nargs="*", default=[],
                         help="Name(s)/alias(es) of the document's own subject company to KEEP "
                              "unredacted (e.g. --issuer 'Novaweave Technologies Limited' Novaweave)")
    parser.add_argument("--redact-issuer-company", action="store_true",
                         help="Redact the issuer/subject company's name too (default: keep it)")
    parser.add_argument("--disable", nargs="*", default=[], metavar="TYPE",
                         help=f"PII types to turn off. Choices: {sorted(ALL_TYPES)}")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for fake-value generation")
    parser.add_argument("--audit-log", help="Optional path to write a JSON audit log of every replacement")
    parser.add_argument("--no-verify", action="store_true",
                         help="Skip the residual scan (re-checking the finished file for leftover PII). "
                              "Roughly halves runtime; not recommended for a document you'll actually share.")
    args = parser.parse_args()

    disabled = set(t.upper() for t in args.disable)
    unknown = disabled - ALL_TYPES
    if unknown:
        parser.error(f"Unknown PII type(s) in --disable: {sorted(unknown)}. Choices: {sorted(ALL_TYPES)}")

    config = DetectionConfig(
        enabled_types=ALL_TYPES - disabled,
        redact_issuer_company=args.redact_issuer_company,
    )
    redactor = Redactor(config=config, seed=args.seed, issuer_names=set(args.issuer))

    try:
        result = redact_file(args.input, args.output, redactor, verify=not args.no_verify)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Done. {len(result.replacements)} PII instance(s) redacted.")
    print(f"Redacted document written to: {args.output}")

    if result.warnings:
        print("\nWarnings:")
        for w in result.warnings:
            print(f"  - {w}")

    by_type = {}
    for rep in result.replacements:
        by_type[rep["type"]] = by_type.get(rep["type"], 0) + 1
    if by_type:
        print("\nBreakdown by type:")
        for t, count in sorted(by_type.items()):
            print(f"  {t:16s} {count}")

    tiers = {"high": 0, "medium": 0, "needs_review": 0}
    for rep in result.replacements:
        tiers[rep.get("confidence_tier", "needs_review")] += 1
    print("\nConfidence:")
    print(f"  {'High':16s} {tiers['high']}   (validated / deterministic -- safe to trust)")
    print(f"  {'Medium':16s} {tiers['medium']}   (statistical model, ordinarily correct)")
    print(f"  {'Needs review':16s} {tiers['needs_review']}   (inferred -- worth a human glance)")

    if result.residual is not None:
        r = result.residual
        print("\nResidual scan (re-checked the finished file for leftover PII):")
        # The two checks carry different weight and are reported separately
        # rather than pooled into one number: a LEAK is a confirmed original
        # value still present -- always a real problem. An UNEXPECTED match
        # is a second NER pass finding something PII-shaped that isn't a
        # known fake value -- useful to skim, but inherits the same
        # statistical imprecision as the primary detection pass (it can,
        # and on a large document usually will, include a few of its own
        # false positives). See app/core/verification.py.
        if not r.leaked_originals:
            print("  LEAKS: none -- every original value the pipeline redacted is confirmed gone.")
        else:
            print(f"  LEAKS: {len(r.leaked_originals)} original value(s) still present -- review before sharing:")
            for f in r.leaked_originals:
                print(f"    [LEAK] {f.entity_type:14s} in {f.location}: {f.text!r}")
        if r.unexpected_matches:
            print(f"  Also found {len(r.unexpected_matches)} PII-shaped item(s) on a second pass, "
                  f"worth a skim (some may be false positives, same as the primary detectors):")
            for f in r.unexpected_matches:
                print(f"    [REVIEW] {f.entity_type:14s} in {f.location}: {f.text!r}")

    if args.audit_log:
        audit = {
            "replacements": result.replacements,
            "warnings": result.warnings,
            "confidence_summary": tiers,
            "residual_scan": result.residual.summary() if result.residual else None,
        }
        with open(args.audit_log, "w") as f:
            json.dump(audit, f, indent=2)
        print(f"\nAudit log written to: {args.audit_log}")


if __name__ == "__main__":
    main()
