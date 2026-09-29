"""
Regressions for edits that cross block boundaries (AI-303).

When a change range starts inside one <p> and ends inside the next (e.g. the
suggestion merges two paragraphs), the block-wrapper handling used to open a
<del>/<ins> before the second <p> that nothing in the slice could close. The
wrapper then swallowed unchanged text and later diff markers (ins nested inside
del), so accepting the diff dropped the whole tail of the document.
"""
from __future__ import annotations

import re

from htmldiff2 import render_html_diff


def _assert_no_nested_change_markers(html: str) -> None:
    # Walk ins/del tags with a stack; a change marker must never contain
    # another change marker (block tags inside del/ins are a designed shape
    # for whole-block deletions and are allowed).
    stack = []
    for m in re.finditer(r"<(/?)(ins|del)\b[^>]*>", html):
        closing, tag = m.group(1), m.group(2)
        if closing:
            assert stack and stack[-1] == tag, f"unbalanced </{tag}> in: {html}"
            stack.pop()
        else:
            assert not stack, (
                f"<{tag}> nested inside <{stack[-1] if stack else ''}> in: {html}"
            )
            stack.append(tag)
    assert not stack, f"unclosed change marker in: {html}"


def _apply(html: str, mode: str) -> str:
    # Safe only when change markers are not nested (asserted separately).
    drop, keep = ("del", "ins") if mode == "accept" else ("ins", "del")
    html = re.sub(r"<%s\b[^>]*>.*?</%s>" % (drop, drop), "", html, flags=re.S)
    html = re.sub(r"</?%s\b[^>]*>" % keep, "", html)
    return html


def _norm_text(html: str) -> str:
    # Tag-boundary-aware text: pad tags with a space so block/br boundaries
    # never glue words, then strip tags, markers and collapse whitespace.
    txt = re.sub(r"<[^>]+>", " ", html)
    txt = txt.replace("¶", " ").replace(" ", " ").replace("&nbsp;", " ")
    return " ".join(txt.split())


ORIGINAL_TWO_PARAGRAPHS = (
    "<p>Common intro sentence stays here.<br/><br/>"
    "Old trailing sentence to be replaced.</p>\n"
    "<p>Shared opening sentence of second block.<br/><br/>"
    "Shared middle sentence one.<br/>Shared middle sentence two.</p>"
)

# The suggestion merges both paragraphs and rewrites the boundary sentences.
SUGGESTED_MERGED_PARAGRAPH = (
    "<p>Common intro sentence stays here.<br/><br/>"
    "Brand new replacement sentence appears now. "
    "Shared opening sentence of second block.<br/><br/>"
    "Shared middle sentence one.<br/>Shared middle sentence two.</p>"
)


def test_cross_paragraph_edit_has_no_nested_markers():
    out = render_html_diff(ORIGINAL_TWO_PARAGRAPHS, SUGGESTED_MERGED_PARAGRAPH)
    _assert_no_nested_change_markers(out)


def test_cross_paragraph_edit_accept_reject_fidelity():
    out = render_html_diff(ORIGINAL_TWO_PARAGRAPHS, SUGGESTED_MERGED_PARAGRAPH)
    _assert_no_nested_change_markers(out)
    assert _norm_text(_apply(out, "accept")) == _norm_text(SUGGESTED_MERGED_PARAGRAPH)
    assert _norm_text(_apply(out, "reject")) == _norm_text(ORIGINAL_TWO_PARAGRAPHS)


def test_cross_paragraph_edit_with_dropped_empty_paragraph():
    # Second shape from the same production trace: an empty <p> </p> between
    # blocks disappears in the suggestion while the next paragraph is edited.
    original = (
        "<p>Stable paragraph before the gap.</p>\n"
        "<p> </p>\n"
        "<p>Target sentence to rewrite here.<br/>"
        "Unchanged closing sentence stays.</p>"
    )
    suggested = (
        "<p>Stable paragraph before the gap.</p>\n"
        "<p>Target sentence fully rewritten now, with details.<br/>"
        "Unchanged closing sentence stays.</p>"
    )
    out = render_html_diff(original, suggested)
    _assert_no_nested_change_markers(out)
    assert _norm_text(_apply(out, "accept")) == _norm_text(suggested)
    assert _norm_text(_apply(out, "reject")) == _norm_text(original)
