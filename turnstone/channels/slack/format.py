"""Markdown report -> Slack Block Kit.

The daily usage report is produced as Markdown by the skill. Slack does not
render Markdown tables/pipes, so the gateway converts that Markdown to Block
Kit before posting -- pretty tables, headings and notes instead of raw pipe
text. The converter is deliberately generic (not report-specific): it renders
*any* Markdown notification nicely.

Slack mrkdwn hazards handled by :func:`apply` (text bound for a mrkdwn field):

* ``<>&`` escaped so user/model text can't inject links/mentions/``<!x>``.
* ``**bold**`` -> ``*bold*`` (Slack mrkdwn bold is a single asterisk).
* inline `` `code` `` backticks dropped -- Slack auto-links inside a code
  span anyway, so the backticks add no value here.
* ``.`` in domain-like sequences (``word.tld``) swapped for a full-width dot
  (．, U+FF0E).  It reads as a dot but doesn't linkify, so a model id like
  ``z.ai`` stays plain text instead of being turned into ``<http://z.ai|z.ai>``.
* ``_em_`` and ``*bold*`` preserved.
"""

from __future__ import annotations

import re
from typing import Any

# A Slack Block Kit block, as sent to chat.postMessage.
Block = dict[str, Any]

# Slack caps a message at 50 blocks; each section/context text at 3000 chars,
# and a context block at 10 elements.
BLOCKS_PER_MSG = 50
MAX_TEXT = 2800  # leave headroom below the 3000-char section cap
CONTEXT_ELEMENTS = 10
FULLWIDTH_DOT = "\uff0e"  # reads as a dot, doesn't linkify

_HEADER_RE = re.compile(r"^#{1,3}\s+(.*)$")
_TABLE_ROW_RE = re.compile(r"^\|.*\|$")
# A "separator" row like  | --- | ---: | --- |  -- skipped, not a data row.
_TABLE_DIVIDER_RE = re.compile(r"^\|?\s*:?\s*-{1,}\s*:?\s*(\|\s*:?\s*-{1,}\s*:?\s*)+\|?\s*$")
# word.tld -> the dot between two letters is what Slack treats as a domain.
_LINKIFY_RE = re.compile(r"([A-Za-z])\.([A-Za-z])")


def apply(text: str) -> str:
    """Sanitise + normalise a string bound for a Slack mrkdwn field."""
    t = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    t = t.replace("**", "*")  # Slack bold is a single asterisk
    t = re.sub(r"`([^`]*)`", r"\1", t)  # drop inline-code backticks
    t = _LINKIFY_RE.sub(lambda m: f"{m.group(1)}{FULLWIDTH_DOT}{m.group(2)}", t)
    return t


# An escape entity produced by apply(); a split must never cut one in half.
_ENTITY_RE = re.compile(r"&(?:amp|lt|gt);")


def split_mrkdwn(text: str, limit: int = MAX_TEXT) -> list[str]:
    """Split already-escaped mrkdwn into pieces of at most ``limit`` chars.

    Nothing is dropped: each piece ends at the last newline before the limit,
    else the last space, else a hard cut moved back so it never lands inside
    an ``&amp;``/``&lt;``/``&gt;`` entity. The newline/space a piece ends on
    is consumed (it is the boundary between two blocks).
    """
    pieces: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[: limit + 1]  # a separator right at the limit is fine
        cut = window.rfind("\n")
        if cut <= 0:
            cut = window.rfind(" ")
        if cut > 0:
            pieces.append(rest[:cut])
            rest = rest[cut + 1 :]
            continue
        cut = limit
        for m in _ENTITY_RE.finditer(rest, max(0, limit - 4), limit + 4):
            if m.start() < cut < m.end():
                cut = m.start()
        pieces.append(rest[:cut])
        rest = rest[cut:]
    pieces.append(rest)
    return [p for p in pieces if p] or [text]


def _raw_section(mrkdwn: str) -> Block:
    """A section from text that has *already* been through :func:`apply`."""
    return {"type": "section", "text": {"type": "mrkdwn", "text": mrkdwn}}


def _section(text: str) -> Block:
    """A section from raw text; escapes it exactly once via :func:`apply`."""
    return _raw_section(apply(text))


def blocks_fallback(blocks: list[Block]) -> str:
    """Plain-text fallback: Slack requires ``text`` whenever ``blocks`` are sent.

    Take the first section's text (usually the title/first line) and cap it.
    """
    for block in blocks:
        if block.get("type") == "section":
            text: str = block["text"]["text"]
            return text[:200]
    return "Notification"


def markdown_to_blocks(md: str) -> list[list[Block]]:
    """Convert Markdown report text into up to 50-block Slack messages."""
    out: list[Block] = []
    notes: list[str] = []  # Notes bullets -> one muted context block
    last_header: str | None = None  # for Notes routing

    def flush_notes() -> None:
        nonlocal notes
        if notes:
            out.append(
                {"type": "context", "elements": [{"type": "mrkdwn", "text": n} for n in notes]}
            )
            notes = []

    def emit_table(header_rows: list[list[str]]) -> None:
        # header_rows[0] is the column-header row; the rest are data rows.
        if not header_rows:
            return
        rows = header_rows[1:]
        head = [c.strip() for c in header_rows[0] if c.strip()]
        if head:
            out.append(_raw_section(" | ".join(f"*{apply(h)}*" for h in head)))
        for row in rows:
            if not row or not row[0].strip():
                continue
            cells = [apply(f"*{row[0].strip()}*")]  # model-name column stays bold
            cells += [apply(c.strip()) for c in row[1:] if c.strip()]
            if cells:
                out.append(_raw_section(" | ".join(cells)))

    lines = md.split("\n")
    i = 0
    while i < len(lines):
        s = lines[i].strip()

        # Table: a leading pipe row starts a table; gather rows until a
        # non-pipe line (skipping the "---" separator row).
        if _TABLE_ROW_RE.match(s):
            header_rows = [s.strip("|").split("|")]
            i += 1
            while i < len(lines) and _TABLE_ROW_RE.match(lines[i].strip()):
                if _TABLE_DIVIDER_RE.match(lines[i].strip()):  # skip | --- | --- |
                    i += 1
                    continue
                header_rows.append(lines[i].strip("|").split("|"))
                i += 1
            flush_notes()
            emit_table(header_rows)
            continue

        if not s:  # blank flushes buffered Notes bullets
            flush_notes()
            i += 1
            continue

        hm = _HEADER_RE.match(s)
        if hm:  # heading: h1 title, h2 section subtitle
            last_header = hm.group(1).strip()
            flush_notes()
            if s.startswith("## "):  # divider before h2
                out.append({"type": "divider"})
            out.append(_raw_section(f"*{apply(hm.group(1).strip())}*"))
            i += 1
            continue

        # list item -- Notes bullets go into one muted context block
        if s.startswith("- ") or s.startswith("* "):
            body = apply(s[2:].strip())
            if last_header == "Notes":
                notes.append(body)
            else:
                out.append(_raw_section(body))
            i += 1
            continue

        # any other paragraph / stray line
        flush_notes()
        out.append(_section(s))
        i += 1

    flush_notes()

    # Enforce per-block text caps by splitting (never truncating), then chunk
    # into Slack-compatible messages.
    capped: list[Block] = []
    for blk in out:
        if blk.get("type") == "section":
            capped.extend(_raw_section(p) for p in split_mrkdwn(blk["text"]["text"]))
        elif blk.get("type") == "context":
            els = [
                {"type": "mrkdwn", "text": p}
                for el in blk["elements"]
                for p in split_mrkdwn(el["text"])
            ]
            capped.extend(
                {"type": "context", "elements": els[k : k + CONTEXT_ELEMENTS]}
                for k in range(0, len(els), CONTEXT_ELEMENTS)
            )
        else:
            capped.append(blk)

    return [capped[k : k + BLOCKS_PER_MSG] for k in range(0, len(capped), BLOCKS_PER_MSG)]
