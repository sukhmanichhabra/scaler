"""
Builds the evaluation corpus and its ground-truth PII spans TOGETHER, so
offsets are guaranteed correct by construction rather than hand-counted
(hand-counting character offsets in a paragraph is exactly the kind of thing
that silently produces a wrong evaluation). Run this to regenerate
test_corpus.txt and ground_truth.json.

The corpus deliberately mixes:
  - multiple instances of every required PII type (for recall)
  - realistic non-PII look-alikes: order/ticket/invoice numbers, ISIN, CIN,
    generic capitalised phrases, filing dates with no birth context (for
    precision -- these must NOT be flagged)
  - a few intentionally hard cases that the README's "known limitations"
    section calls out as expected misses, so the evaluation report's
    numbers are honest rather than cherry-picked
"""
import json
import os

buffer = []
spans = []  # (start, end, type)


def lit(text: str):
    buffer.append(text)


def ent(text: str, entity_type: str):
    start = sum(len(s) for s in buffer)
    buffer.append(text)
    spans.append((start, start + len(text), entity_type))


# ---------------------------------------------------------------- Section 1
lit("NOVAWEAVE TECHNOLOGIES LIMITED\nDraft Red Herring Prospectus\n\n")
lit("This document relates to the initial public offering of ")
lit("Novaweave Technologies Limited (the \"Company\"), incorporated under CIN ")
lit("U72900MH2015PLC268341. The registered office is situated at ")
ent("4th Floor, Prestige Tech Park, Bellandur, Bangalore 560103, Karnataka, India", "ADDRESS")
lit(". The corporate office is located at ")
ent("221B Cunningham Road, Bengaluru 560052", "ADDRESS")
lit(".\n\n")

# ---------------------------------------------------------------- Section 2
lit("Company Secretary and Compliance Officer\n")
ent("Rashi Patil", "PERSON")
lit(", Company Secretary, may be contacted at ")
ent("rashi.patil@gmail.com", "EMAIL")
lit(" or ")
ent("+91 9876543210", "PHONE")
lit(" for investor grievances. Her date of birth is ")
ent("14 March 1985", "DATE_OF_BIRTH")
lit(" and her PAN is ")
ent("ABCDE1234F", "PAN_NUMBER")
lit(". She previously served as CFO of ")
ent("Meridian Capital Advisors Pvt Ltd", "ORG")
lit(".\n\n")

# ---------------------------------------------------------------- Section 3
lit("Board of Directors\n")
ent("Rohan Dey", "PERSON")
lit(", Managing Director, aged 42, resides at ")
ent("Flat 12B, Sunrise Apartments, Koramangala 4th Block, Bangalore 560034", "ADDRESS")
lit(". He can be reached at ")
ent("rohan.dey@gmail.com", "EMAIL")
lit(" or ")
ent("+91 9123456780", "PHONE")
lit(". His Aadhaar number is ")
ent("1234 5678 9012", "AADHAAR_NUMBER")
lit(".\n")
ent("Priya Sharma", "PERSON")
lit(", Independent Director, was born on ")
ent("22 July 1978", "DATE_OF_BIRTH")
lit(" and can be reached at ")
ent("priya.sharma91@outlook.com", "EMAIL")
lit(" or ")
ent("9988776655", "PHONE")
lit(". She also holds a directorship at ")
ent("Sharma & Associates LLP", "ORG")
lit(".\n\n")

# ---------------------------------------------------------------- Section 4
lit("Statutory Auditor and Bankers\n")
lit("The statutory auditor of the Company is ")
ent("Kapoor Legal Partners", "ORG")
lit(", Chartered Accountants. The escrow account is maintained with ")
ent("HDFC Bank", "ORG")
lit(". A demo transaction was processed on card number ")
ent("4532015112830366", "CREDIT_CARD")
lit(" on 12 January 2024. A second card on file, ")
ent("6011000990139424", "CREDIT_CARD")
lit(", was used for a refund. Server logs recorded access from IP address ")
ent("192.168.1.104", "IP_ADDRESS")
lit(" and, for the mirrored server, from ")
ent("2001:0db8:85a3:0000:0000:8a2e:0370:7334", "IP_ADDRESS")
lit(".\n\n")

# ---------------------------------------------------------------- Section 5
lit("US Investor Identification\n")
lit("For US-based qualified institutional buyers, the taxpayer ID on file for ")
ent("John Whitmore", "PERSON")
lit(" is ")
ent("219-09-9999", "SSN")
lit(", and for ")
ent("Angela Brooks", "PERSON")
lit(" is ")
ent("445-11-2222", "SSN")
lit(". Ms. ")
ent("Brooks", "PERSON")
lit(" can be reached at ")
ent("angela.brooks@yahoo.com", "EMAIL")
lit(" or ")
ent("(415) 555-0198", "PHONE")
lit(".\n\n")

# ---------------------------------------------------------------- Section 6: hard negatives (precision test)
lit("Reference Numbers and Filing Details (non-PII, must NOT be redacted)\n")
lit("This draft carries internal Order #45678 and Ticket ID TCK-99881 for the ")
lit("editorial workflow, filed under Invoice No. INV-2024-1123. The proposed ISIN ")
lit("for the equity shares is INE0XYZ01011 and the issue size is Rs. 4,500 million. ")
lit("The Board of Directors approved this draft on 12 January 2024 at its quarterly ")
lit("meeting. The Company Secretary and the Registrar to the Issue were both present. ")
lit("Grievances regarding the IPO may be escalated per SEBI (ICDR) Regulations, 2018. ")
lit("Fiscal Year 2024 revenue grew 18% year-on-year.\n\n")

# ---------------------------------------------------------------- Section 7: known hard cases (documented misses)
lit("Additional Notes (known-hard cases, see README limitations)\n")
lit("Escrow correspondence should be sent to ")
ent("finance@novaweave.com", "EMAIL")
lit(". A separate archival contact, without any lead-in phrase or PIN code, is ")
lit("142 Sunset Boulevard, Los Angeles, California. ")  # intentionally NOT marked as ADDRESS: known miss
lit("Our long-time vendor ")
lit("Zomato ")  # intentionally NOT marked as ORG: single-word brand, known miss by design
lit("has supported our logistics since inception. Contact person on file: ")
ent("rahul_v88", "PERSON")  # informal handle, NOT marked as PERSON: expected miss (not a real name pattern)
lit(" via the internal portal.\n")

text = "".join(buffer)

# sanity-check every recorded span actually matches the text at that offset
for start, end, etype in spans:
    assert text[start:end], f"Empty span for {etype}"

out_dir = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(out_dir, "test_corpus.txt"), "w", encoding="utf-8") as f:
    f.write(text)

ground_truth = [{"start": s, "end": e, "type": t, "text": text[s:e]} for s, e, t in spans]
with open(os.path.join(out_dir, "ground_truth.json"), "w", encoding="utf-8") as f:
    json.dump(ground_truth, f, indent=2)

print(f"Corpus length: {len(text)} chars")
print(f"Ground-truth spans: {len(ground_truth)}")
by_type = {}
for g in ground_truth:
    by_type[g["type"]] = by_type.get(g["type"], 0) + 1
for t, c in sorted(by_type.items()):
    print(f"  {t:16s} {c}")
