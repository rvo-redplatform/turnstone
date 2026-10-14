"""Tests for the Slack report formatter (Markdown -> Block Kit).

The daily usage report is produced as Markdown by the skill; the Slack adapter
converts it to Block Kit before posting so tables/notes render nicely. This
module is dependency-light (only :mod:`re`), so it is exercised directly.
"""

from __future__ import annotations

import re

from turnstone.channels.slack.format import apply, blocks_fallback, markdown_to_blocks

# A compact, representative usage report: h1 title, summary bullets, two tables
# (with markdown separator rows), and a Notes section.
SAMPLE_REPORT = """\
# AI Data Platform Usage Report - 2026-10-08

## Summary

- **Models with traffic:** 6  (2 bedrock, 4 open-weight)
- **Total tokens:** 190,728,986 — **Total requests:** 3,258

## Bedrock models

| Model | Tokens | Requests | Cost (Langfuse est.) |
| --- | ---: | ---: | ---: |
| Claude Sonnet 5.5 | 462,217 | 7 | $0.80 |
| z.ai GLM-5 | 456,065 | 15 | $0.44 |

## Notes

- **Requests** = Langfuse observation count (one per LLM call).
- **GPU-hours** is deferred because kubectl/RBAC are unavailable.
"""


def _blocks_of(report: str) -> list[dict]:
    msgs = markdown_to_blocks(report)
    assert msgs  # never returns [] for non-empty input
    return msgs[0]


def _section_texts(md: str) -> list[str]:
    return [b["text"]["text"] for b in _blocks_of(md) if b["type"] == "section"]


def test_apply_escapes_and_strips_backticks() -> None:
    assert apply("<b>&x</b>") == "&lt;b&gt;&amp;x&lt;/b&gt;"
    assert apply("**bold**") == "*bold*"
    assert apply("`pip install` here") == "pip install here"


def test_apply_breaks_domain_linkify() -> None:
    # A bare dot between letters must not become a Slack hyperlink.
    text = apply("see z.ai and nvidia.com models")
    assert "<http://" not in text
    assert "z．ai" in text  # fullwidth dot U+FF0E


def test_title_is_a_bold_section() -> None:
    blocks = _blocks_of(SAMPLE_REPORT)
    assert blocks[0] == {
        "type": "section",
        "text": {"type": "mrkdwn", "text": "*AI Data Platform Usage Report - 2026-10-08*"},
    }


def test_table_separator_row_is_not_emitted() -> None:
    blocks = _blocks_of(SAMPLE_REPORT)
    blob = " ".join(str(b) for b in blocks)
    # The markdown `| --- | ---: |` separator must never leak as a data row.
    assert "---" not in blob


def test_column_headers_render_bold() -> None:
    header = next(t for t in _section_texts(SAMPLE_REPORT) if "Tokens" in t)
    assert "*Model*" in header
    assert "*Cost (Langfuse est.)*" in header


def test_model_name_bold_but_values_plain() -> None:
    row = next(t for t in _section_texts(SAMPLE_REPORT) if "Claude Sonnet 5.5" in t)
    assert row == "*Claude Sonnet 5.5* | 462,217 | 7 | $0.80"


def test_notes_grouped_into_one_context_block() -> None:
    blocks = _blocks_of(SAMPLE_REPORT)
    contexts = [b for b in blocks if b["type"] == "context"]
    assert len(contexts) == 1, "Notes should collapse into a single muted block"
    ctx_texts = [el["text"] for el in contexts[0]["elements"]]
    assert len(ctx_texts) == 2
    assert any("Requests" in t for t in ctx_texts)
    assert any("GPU-hours" in t for t in ctx_texts)


def test_block_count_chunks_at_50() -> None:
    big = "# Title\n\n" + "\n".join(f"- note {i}" for i in range(80))
    msgs = markdown_to_blocks(big)
    assert len(msgs) == 2
    assert all(len(m) <= 50 for m in msgs)


def test_empty_body_is_marked_for_plain_text() -> None:
    # An empty body yields no blocks; bot.py's ``or [None]`` guard then posts
    # plain text ("(no output)") so nothing renders as an empty message.
    assert markdown_to_blocks("") == []


def test_blocks_fallback_uses_first_section_text() -> None:
    blocks = _blocks_of(SAMPLE_REPORT)
    assert blocks_fallback(blocks).startswith("*AI Data Platform Usage Report - 2026-10-08*")


# Each field must be escaped exactly once: a second apply() turns "&amp;" into
# "&amp;amp;", which Slack shows literally.
ESCAPE_MD = "# R&D <x>\n| A&1 | <B> |\n| --- | --- |\n| a&b | <1 |\n- i&j"
DOUBLE_ESCAPED = ("&amp;amp;", "&amp;lt;", "&amp;gt;")


def test_no_field_is_double_escaped() -> None:
    texts = _section_texts(ESCAPE_MD)
    for t in texts:
        for bad in DOUBLE_ESCAPED:
            assert bad not in t, (bad, t)


def test_heading_escaped_once() -> None:
    assert _section_texts("# R&D <report>") == ["*R&amp;D &lt;report&gt;*"]


def test_table_header_escaped_once() -> None:
    assert _section_texts(ESCAPE_MD)[1] == "*A&amp;1* | *&lt;B&gt;*"


def test_table_row_escaped_once() -> None:
    assert _section_texts(ESCAPE_MD)[2] == "*a&amp;b* | &lt;1"


def test_list_item_escaped_once() -> None:
    assert _section_texts(ESCAPE_MD)[3] == "i&amp;j"


def test_h2_gets_a_divider_h1_does_not() -> None:
    blocks = _blocks_of("# Title\n## Section")
    assert [b["type"] for b in blocks] == ["section", "divider", "section"]


def test_mentions_escaped_in_every_position() -> None:
    md = (
        "# <!channel> <@U123>\n"
        "| <!channel> | <@U123> |\n| --- | --- |\n| <!here> | <@U123> |\n"
        "- <!channel> <@U123>\n"
        "<!channel> <@U123>\n"
        "## Notes\n- <!channel> <@U123>"
    )
    blocks = _blocks_of(md)
    texts = [b["text"]["text"] for b in blocks if b["type"] == "section"]
    texts += [el["text"] for b in blocks if b["type"] == "context" for el in b["elements"]]
    assert len(texts) == 7  # heading, table header, row, bullet, paragraph, h2, note
    for t in texts:
        assert "<" not in t and ">" not in t, t
        for bad in DOUBLE_ESCAPED:
            assert bad not in t, (bad, t)
    assert "&lt;!channel&gt;" in texts[0] and "&lt;@U123&gt;" in texts[0]


# Over-long text is split across blocks, never truncated (Slack caps a section
# or context element at 3000 chars).
def _all_texts(md: str) -> list[str]:
    blocks = [b for m in markdown_to_blocks(md) for b in m]
    texts = [b["text"]["text"] for b in blocks if b["type"] == "section"]
    texts += [el["text"] for b in blocks if b["type"] == "context" for el in b["elements"]]
    return texts


def test_long_paragraph_split_not_truncated() -> None:
    texts = _all_texts("word " * 2000)
    assert len(texts) > 1
    assert all(len(t) <= 3000 for t in texts)
    assert not any(t.endswith("...") for t in texts)
    assert " ".join(texts).split() == ["word"] * 2000


def test_single_unbroken_line_delivered_in_full() -> None:
    line = "".join(chr(ord("a") + i % 26) for i in range(6000))  # no spaces/newlines
    texts = _all_texts(line)
    assert len(texts) == 3
    assert all(len(t) <= 3000 for t in texts)
    assert "".join(texts) == line


def test_long_note_split_into_context_elements() -> None:
    texts = _all_texts("## Notes\n- " + "note " * 1500)
    assert all(len(t) <= 3000 for t in texts)
    assert texts[0] == "*Notes*"
    assert " ".join(texts[1:]).split() == ["note"] * 1500


def test_split_never_cuts_an_entity() -> None:
    # Hard cuts (no whitespace) at every offset around the limit; each piece
    # must hold only whole entities and the pieces must rejoin losslessly.
    for pad in range(2790, 2805):
        md = "x" * pad + "&<>" * 10
        texts = _all_texts(md)
        assert "".join(texts) == "x" * pad + "&amp;&lt;&gt;" * 10
        for t in texts:
            assert len(t) <= 3000
            assert "&" not in re.sub(r"&(?:amp|lt|gt);", "", t), t[-8:]
            assert not re.match(r"(?:amp|lt|gt);", t), t[:8]
