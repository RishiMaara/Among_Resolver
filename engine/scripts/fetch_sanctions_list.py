#!/usr/bin/env python3
"""
Fetch the UN Security Council Consolidated List and write it in the
one-identifier-per-line format `compliance_agent` screens against.

WHY A SCRIPT AND NOT A COMMITTED FILE
-------------------------------------
A sanctions list is a point-in-time snapshot of a thing that changes. A copy
committed to a repository is stale the week after it lands, and stale
screening is the failure mode that matters: it passes a party who was
designated after the snapshot, while looking exactly like screening that
works. So the list is fetched deliberately, stamped with the date it was
retrieved, and the loader reports that date so nobody has to guess.

WHY THE UN LIST
---------------
It is the one consolidated list that is public, free, machine-readable and
requires no registration or API key. OFAC's SDN list is equally public but is
a US designation; the UN Consolidated List is the one India implements
domestically through the UAPA Order, which is the jurisdiction this engine
is built for.

WHAT THIS IS NOT
----------------
Real screening also needs fuzzy name matching, transliteration variants and
date-of-birth disambiguation to distinguish two people with the same name.
This produces an exact-match list of primary names and aliases. That catches
an exact hit and misses a misspelling, which is stated to the reviewer
in compliance_rulebook.SANCTIONS_HIT rather than left to be discovered.
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sys
import urllib.request
import xml.etree.ElementTree as ET

UN_CONSOLIDATED_XML = "https://scsanctions.un.org/resources/xml/en/consolidated.xml"

# Name parts appear as separate elements; aliases live in child nodes whose
# tag differs between individuals and entities. Matching by suffix rather than
# exact tag keeps this working if the UN adds a third record type.
NAME_PARTS = ("FIRST_NAME", "SECOND_NAME", "THIRD_NAME", "FOURTH_NAME")
ALIAS_TAG_SUFFIX = "_ALIAS"
ALIAS_NAME_TAG = "ALIAS_NAME"


def normalize(name: str) -> str:
    """
    Fold a name to the form the matcher compares.

    Punctuation and spacing are noise: "AL-QAIDA", "AL QAIDA" and "Al Qaida"
    are the same designation written three ways, and a screening list that
    treats them as three different strings catches whichever one the file
    happens to use. Kept deliberately simple and identical to the function
    in compliance_agent — two normalisers that drift apart would silently
    stop the list matching itself.
    """
    return " ".join("".join(
        ch if ch.isalnum() else " " for ch in (name or "")
    ).upper().split())


def extract(root: ET.Element) -> set[str]:
    names: set[str] = set()

    for record in root.iter():
        tag = record.tag.upper()
        if tag not in ("INDIVIDUAL", "ENTITY"):
            continue

        parts = []
        for part in NAME_PARTS:
            el = record.find(part)
            if el is not None and (el.text or "").strip():
                parts.append(el.text.strip())
        if parts:
            full = normalize(" ".join(parts))
            if full:
                names.add(full)

        for child in record:
            if not child.tag.upper().endswith(ALIAS_TAG_SUFFIX):
                continue
            alias_el = child.find(ALIAS_NAME_TAG)
            if alias_el is None:
                continue
            alias = normalize(alias_el.text or "")
            # Single-token aliases are dropped. The list contains entries like
            # "ABU" and "HAJI"; screening on those would block a large share
            # of legitimate South Asian counterparties. A false block on a
            # sanctions rule is not a minor inconvenience — it freezes funds.
            if alias and len(alias.split()) >= 2:
                names.add(alias)

    return names


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default=UN_CONSOLIDATED_XML)
    ap.add_argument("--out", default="../data/sanctions/un_consolidated.txt")
    ap.add_argument("--from-file", help="Parse an already-downloaded XML instead of fetching.")
    args = ap.parse_args()

    if args.from_file:
        raw = pathlib.Path(args.from_file).read_bytes()
        origin = f"local file {args.from_file}"
    else:
        print(f"Fetching {args.url} ...", file=sys.stderr)
        with urllib.request.urlopen(args.url, timeout=60) as resp:
            raw = resp.read()
        origin = args.url

    root = ET.fromstring(raw)
    names = extract(root)
    if not names:
        print("ERROR: parsed 0 names — the source schema has probably changed. "
              "Refusing to write a file that would screen against nothing.",
              file=sys.stderr)
        return 1

    generated = root.get("dateGenerated", "unknown")
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        f.write("# UN Security Council Consolidated List\n")
        f.write(f"# source: {origin}\n")
        f.write(f"# generated: {generated}\n")
        f.write(f"# retrieved: {dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}\n")
        f.write(f"# entries: {len(names)}\n")
        f.write("# Normalised: uppercase, punctuation folded to spaces. Primary\n")
        f.write("# names and multi-token aliases. Exact match after normalisation\n")
        f.write("# only -- no fuzzy matching, transliteration or DOB disambiguation.\n")
        for n in sorted(names):
            f.write(n + "\n")

    print(f"Wrote {len(names)} identifiers to {out} (list generated {generated}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
