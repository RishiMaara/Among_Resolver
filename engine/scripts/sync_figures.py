#!/usr/bin/env python3
"""
Every published figure, written from one registry.

The same measured figures sat in nine documents, typed by hand into each, and
every re-measurement meant nine edits and a search for the one that was
missed. They now live in docs/figures.json, each named and pointed at the
result file and field it comes from. In the documents a figure is wrapped in
an invisible marker:

    <!--fig:bench_cleared-->92%<!--/fig-->
    <!--fig:rr_anchored_exact:pct1-->94.6%<!--/fig-->   (a format of its own)
    <!--fig:lines=engine/src/orchestrator.py-->507<!--/fig-->   (a file's line count)

This script rewrites every marker from the registry; --check changes nothing
and lists what is stale. tests/test_published_figures.py runs the check, so a
document that disagrees with its source file fails the build.

From the repository root or engine/:
    python engine/scripts/sync_figures.py           # rewrite
    python engine/scripts/sync_figures.py --check   # list stale, exit 1 if any
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "engine" / "docs" / "benchmarks" / "figures.json"
MARKER = re.compile(r"<!--fig:([a-z0-9_]+|lines=[A-Za-z0-9_./-]+)(?::([a-z0-9]+))?-->(.*?)<!--/fig-->",
                    re.S)
# INDUSTRY_COMPARISON.md is maintained by hand, outside this project's edits.
SKIP = {"INDUSTRY_COMPARISON.md"}


def documents() -> list[Path]:
    docs = [ROOT / "README.md", ROOT / "FAILURE_LOG.md"]
    docs += sorted(p for p in (ROOT / "docs").glob("*.md") if p.name not in SKIP)
    return [p for p in docs if p.exists()]


def _read(file: str, path: str):
    node = json.loads((ROOT / file).read_text(encoding="utf-8"))
    for part in path.split("."):
        node = node[int(part)] if isinstance(node, list) else node[part]
    return node


def _fmt(value: float, fmt: str) -> str:
    if fmt == "int":
        return f"{int(value):,}"
    if fmt == "pct0":
        return f"{value:.0f}%"
    if fmt == "pct1":
        return f"{value:.1f}%"
    if fmt == "pct2":
        return f"{value:.2f}%"
    if fmt == "dec3":
        return f"{value:.3f}"
    if fmt == "dec4":
        return f"{value:.4f}"
    raise ValueError(f"unknown format {fmt!r}")


def value(spec: dict, fmt: str | None = None) -> str:
    """The figure as it is printed, in its own format or the one given."""
    spec = {**spec, "format": fmt or spec["format"]}
    if "paths" in spec:
        # A range over several results, e.g. held-out seeds: "92–93%".
        vals = sorted(_read(spec["file"], p) for p in spec["paths"])
        unit = spec["format"]
        lo, hi = _fmt(vals[0], unit), _fmt(vals[-1], unit)
        if lo == hi:
            return lo
        suffix = "%" if unit.startswith("pct") else ""
        return f"{lo.rstrip('%')}–{hi.rstrip('%')}{suffix}"
    return _fmt(_read(spec["file"], spec["path"]), spec["format"])


def registry() -> dict[str, dict]:
    return json.loads(REGISTRY.read_text(encoding="utf-8"))["figures"]


def figures() -> dict[str, str]:
    return {name: value(spec) for name, spec in registry().items()}


def problems(write: bool = False) -> list[str]:
    """Stale or unknown markers; with write, stale ones are rewritten."""
    known = registry()
    found: list[str] = []
    for doc in documents():
        # Bytes, so a document keeps its own line endings when rewritten.
        raw = doc.read_bytes().decode("utf-8")
        crlf = "\r\n" in raw
        text = raw.replace("\r\n", "\n")

        def fix(m: re.Match) -> str:
            name, fmt, shown = m.group(1), m.group(2), m.group(3)
            if name.startswith("lines="):
                source = ROOT / name[len("lines="):]
                if not source.exists():
                    found.append(f"{doc.name}: no such file {source.name!r}")
                    return m.group(0)
                want = str(len(source.read_bytes().decode("utf-8").splitlines()))
                if shown != want:
                    found.append(f"{doc.name}: {source.name} has {want} lines, it says {shown!r}")
                return f"<!--fig:{name}-->{want}<!--/fig-->"
            if name not in known:
                found.append(f"{doc.name}: unknown figure {name!r}")
                return m.group(0)
            try:
                want = value(known[name], fmt)
            except (OSError, KeyError, IndexError, ValueError) as exc:
                found.append(f"{doc.name}: {name} cannot be read from its source ({exc})")
                return m.group(0)
            if shown != want:
                found.append(f"{doc.name}: {name} says {shown!r}, its source says {want!r}")
            tag = f"{name}:{fmt}" if fmt else name
            return f"<!--fig:{tag}-->{want}<!--/fig-->"

        new = MARKER.sub(fix, text)
        if write and new != text:
            doc.write_bytes((new.replace("\n", "\r\n") if crlf else new).encode("utf-8"))
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="change nothing; exit 1 if stale")
    args = ap.parse_args()
    found = problems(write=not args.check)
    for line in found:
        print(line)
    if args.check:
        return 1 if found else 0
    print(f"{len(found)} figure(s) rewritten" if found else "every figure is current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
