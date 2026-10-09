"""Tests for the Slack report formatter (Markdown -> Block Kit).

The daily usage report is produced as Markdown by the skill; the Slack adapter
converts it to Block Kit before posting so tables/notes render nicely. This
module is dependency-light (only :mod:`re`), so it is exercised directly.
"""

from __future__ import annotations

from turnstone.channels.slack.format import blocks_fallback, markdown_to_blocks, apply

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
    blocks = _blocks_of(SAMPLE_REPORT)
    header = next(b for b in blocks if "Tokens" in b["text"]["text"])
    assert "*Model*" in header["text"]["text"]
    assert "*Cost (Langfuse est.)*" in header["text"]["text"]


def test_model_name_bold_but_values_plain() -> None:
    blocks = _blocks_of(SAMPLE_REPORT)
    row = next(b for b in blocks if "Claude Sonnet 5.5" in b["text"]["text"])
    text = row["text"]["text"]
    assert text == "*Claude Sonnet 5.5* | 462,217 | 7 | $0.80"


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
