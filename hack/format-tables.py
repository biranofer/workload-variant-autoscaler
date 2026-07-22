#!/usr/bin/env python3
"""Reformat all markdown tables in a file with consistent column widths.

Table formatting rules:
  1. Every cell has one space of padding inside the pipes (`| cell |`).
  2. Within a table, every row uses the SAME column widths (widest cell wins).
  3. Text columns are left-aligned; columns whose every data cell parses as a
     number are right-aligned. Bold-wrapped numbers (`**123**`) still count as
     numeric for alignment purposes.
  4. Separator row uses dashes only, length = column width.
  5. All rows in a table (header, separator, data) share identical widths.

Usage:
    hack/format-tables.py FILE [FILE ...]

Reads each file, rewrites in place. Non-table lines are left untouched.
Exit non-zero if any file can't be read/written.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


_SEP_RE = re.compile(r"\s*:?-{3,}:?\s*")


def _is_sep(cells: list[str]) -> bool:
    return bool(cells) and all(_SEP_RE.fullmatch(c) for c in cells)


def _is_number(s: str) -> bool:
    s = s.strip().replace("**", "").replace(",", "").rstrip("%")
    if not s:
        return True
    try:
        float(s)
        return True
    except ValueError:
        return False


def _split_row(line: str) -> list[str]:
    cells = line.split("|")
    if cells and cells[0].strip() == "":
        cells = cells[1:]
    if cells and cells[-1].strip() == "":
        cells = cells[:-1]
    return cells


def _fmt_table(rows: list[list[str]]) -> str:
    ncols = len(rows[0])
    widths = [
        max(len(rows[r][c].strip()) for r in range(len(rows)) if not _is_sep(rows[r]))
        for c in range(ncols)
    ]
    right_align: list[bool] = []
    for c in range(ncols):
        data = [
            rows[r][c].strip()
            for r in range(len(rows))
            if r != 0 and not _is_sep(rows[r])
        ]
        right_align.append(bool(data) and all(_is_number(v) for v in data))

    out_lines: list[str] = []
    for row in rows:
        cells: list[str] = []
        for c, v in enumerate(row):
            v = v.strip()
            if _is_sep(row):
                cells.append("-" * (widths[c] + 2))
            else:
                pad = widths[c]
                aligned = v.rjust(pad) if right_align[c] else v.ljust(pad)
                cells.append(" " + aligned + " ")
        out_lines.append("|" + "|".join(cells) + "|")
    return "\n".join(out_lines)


def _table_span(lines: list[str], i: int) -> tuple[int, int]:
    j = i
    while j < len(lines) and lines[j].lstrip().startswith("|"):
        j += 1
    return i, j


def reformat(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith("|"):
            i0, j = _table_span(lines, i)
            block = lines[i0:j]
            rows = [_split_row(ln) for ln in block]
            if len(rows) >= 2 and _is_sep(rows[1]):
                out.append(_fmt_table(rows))
            else:
                out.extend(block)
            i = j
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("files", nargs="+")
    args = p.parse_args()
    rc = 0
    for f in args.files:
        try:
            path = Path(f)
            new = reformat(path.read_text())
            path.write_text(new)
        except Exception as e:
            print(f"ERROR: {f}: {e}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
