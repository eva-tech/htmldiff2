"""
Regression for style-changed elements whose content also changed (AI-303).

A same-tag style change buffers the element's content and re-emits it as
<del>+<ins> copies. When the content changed too, the buffer carried the inner
ins/del markers into both copies: markers nested inside markers (invalid for
accept/reject) and both text versions rendered in both copies. Observed in
production trace 3681d19c.
"""
from __future__ import annotations

import re

from htmldiff2 import render_html_diff


def _assert_no_nested_change_markers(html: str) -> None:
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
    txt = re.sub(r"<[^>]+>", " ", html)
    txt = txt.replace("¶", " ").replace(" ", " ").replace("&nbsp;", " ")
    return " ".join(txt.split())


ORIGINAL = (
    '<p style="text-align: left;">'
    '<span style="font-size: 11pt;">Old finding text here.</span></p>'
)
SUGGESTED = (
    '<p style="text-align: justify;">'
    '<span style="font-size: 11pt;">New finding text appears instead.</span></p>'
)


def test_style_and_content_change_does_not_nest_markers():
    out = render_html_diff(ORIGINAL, SUGGESTED)
    _assert_no_nested_change_markers(out)


def test_style_and_content_change_each_copy_holds_one_side():
    out = render_html_diff(ORIGINAL, SUGGESTED)
    _assert_no_nested_change_markers(out)
    assert _norm_text(_apply(out, "accept")) == _norm_text(SUGGESTED)
    assert _norm_text(_apply(out, "reject")) == _norm_text(ORIGINAL)
