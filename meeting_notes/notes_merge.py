"""Co-editing "Your notes": a line-level three-way merge, so the recorder and the Recorders page can both type at once.

The recorder holds the notes. The page sends ``set_notes {text, base}``: its text and the recorder's text it last
saw. The recorder merges what the page changed since ``base`` into whatever is typed there now, so neither side
loses a line. The page merges the same way in the other direction (``recNotesMerge`` in
``server/webpages/recorders.py``, the same policy in JavaScript), when the recorder's notes change while it edits.

This is Life Dash's ``collab.merge3`` (the notes co-editing there), kept to its policy:

* only one side changed a run of lines -> that side's lines;
* both inserted at the same spot -> both insertions, ``theirs`` first, then ``mine``;
* both changed the same lines differently -> ``mine`` wins that run of lines (never the whole text).

Lines only: a merge inside a line would have to guess what a half-typed word meant. Two people typing on the same
line at the same moment is rare; when it happens the page's edit wins that line (it is ``mine`` on both sides).

The line diff is a small longest-common-subsequence diff (``line_ops``) rather than ``difflib``: the page runs the
same algorithm in JavaScript (``recLineOps``), so both sides always merge to the same text.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

# The changed middle of two texts (after their common head and tail) is compared line by line only up to this many
# line pairs; beyond it the whole middle counts as replaced. Same number as the page's REC_DIFF_CELLS.
DIFF_CELLS = 250_000


def _lines(text: str) -> List[str]:
    return text.split("\n") if text else []  # empty notes have no lines (two people typing into them keep both)


def line_ops(a: List[str], b: List[str]) -> List[Tuple[str, int, int, int, int]]:
    """``(tag, i1, i2, j1, j2)`` runs turning ``a`` into ``b``, like ``SequenceMatcher.get_opcodes``."""
    n, m = len(a), len(b)
    pre = 0
    while pre < n and pre < m and a[pre] == b[pre]:
        pre += 1
    suf = 0
    while suf < n - pre and suf < m - pre and a[n - 1 - suf] == b[m - 1 - suf]:
        suf += 1
    ops: List[List] = []

    def push(tag: str, i1: int, i2: int, j1: int, j2: int) -> None:
        if i1 == i2 and j1 == j2:
            return
        if ops and (tag == "equal") == (ops[-1][0] == "equal"):
            last = ops[-1]
            last[2], last[4] = i2, j2
            if tag != "equal":
                last[0] = "insert" if last[1] == last[2] else "delete" if last[3] == last[4] else "replace"
            return
        ops.append([tag, i1, i2, j1, j2])

    def change(i1: int, i2: int, j1: int, j2: int) -> None:
        push("insert" if i1 == i2 else "delete" if j1 == j2 else "replace", i1, i2, j1, j2)

    push("equal", 0, pre, 0, pre)
    an, bm = n - pre - suf, m - pre - suf
    if an and bm and an * bm <= DIFF_CELLS:
        w = bm + 1
        table = [0] * ((an + 1) * w)
        for i in range(an - 1, -1, -1):
            for j in range(bm - 1, -1, -1):
                if a[pre + i] == b[pre + j]:
                    table[i * w + j] = table[(i + 1) * w + j + 1] + 1
                else:
                    table[i * w + j] = max(table[(i + 1) * w + j], table[i * w + j + 1])
        i = j = ci = cj = 0
        while i < an and j < bm:
            if a[pre + i] == b[pre + j]:
                change(pre + ci, pre + i, pre + cj, pre + j)
                push("equal", pre + i, pre + i + 1, pre + j, pre + j + 1)
                i += 1
                j += 1
                ci, cj = i, j
            elif table[(i + 1) * w + j] >= table[i * w + j + 1]:
                i += 1
            else:
                j += 1
        change(pre + ci, pre + an, pre + cj, pre + bm)
    else:
        change(pre, pre + an, pre, pre + bm)
    push("equal", n - suf, n, m - suf, m)
    return [tuple(op) for op in ops]


def merge3(base: str, mine: str, theirs: str) -> str:
    """``base`` -> ``mine`` and ``base`` -> ``theirs`` merged into one text (``mine`` wins a contested run)."""
    if mine == theirs or theirs == base:
        return mine
    if mine == base:
        return theirs
    base_lines = _lines(base)

    def hunks(target: List[str], side: str) -> List[Tuple[int, int, str, List[str]]]:
        return [(i1, i2, side, target[j1:j2]) for tag, i1, i2, j1, j2 in line_ops(base_lines, target) if tag != "equal"]

    all_hunks = sorted(hunks(_lines(mine), "mine") + hunks(_lines(theirs), "theirs"), key=lambda h: (h[0], h[1]))
    # Regions of truly overlapping base lines (abutting edits are independent); an insert at the same gap as a bare
    # insert joins it (both are kept).
    regions: List[Dict] = []
    for h in all_hunks:
        start, end = h[0], h[1]
        if regions:
            r = regions[-1]
            same_gap_insert = start == end == r["end"] and r["start"] == r["end"]
            if start < r["end"] or same_gap_insert:
                r["end"] = max(r["end"], end)
                r["hunks"].append(h)
                continue
        regions.append({"start": start, "end": end, "hunks": [h]})

    def side_take(region: Dict, side: str) -> List[str]:
        """One side's version of the whole region: its replacements, and the base lines it left alone."""
        out: List[str] = []
        pos = region["start"]
        for h_start, h_end, h_side, repl in region["hunks"]:
            if h_side != side:
                continue
            out.extend(base_lines[pos:h_start])
            out.extend(repl)
            pos = h_end
        out.extend(base_lines[pos:region["end"]])
        return out

    out: List[str] = []
    pos = 0
    for region in regions:
        out.extend(base_lines[pos:region["start"]])
        sides = {h[2] for h in region["hunks"]}
        if sides == {"mine", "theirs"}:
            if region["start"] == region["end"]:
                out.extend(side_take(region, "theirs"))
                out.extend(side_take(region, "mine"))
            else:
                out.extend(side_take(region, "mine"))
        else:
            out.extend(side_take(region, "mine" if "mine" in sides else "theirs"))
        pos = region["end"]
    out.extend(base_lines[pos:])
    return "\n".join(out)


def line_map(old: List[str], new: List[str]):
    """A function taking a line (block) index in ``old`` to where it is in ``new``: keeps a caret on its line when
    the text around it is replaced by a merge."""
    opcodes = line_ops(old, new)
    last = max(0, len(new) - 1)

    def where(index: int) -> int:
        for tag, i1, i2, j1, j2 in opcodes:
            if i1 <= index < i2:
                if tag == "equal":
                    return j1 + (index - i1)
                return min(last, j1 + min(index - i1, max(0, j2 - j1 - 1)))
        return last

    return where
