"""
Regression for stray void end tags in rendered diffs (AI-303).

Diff bookkeeping can separate a void element's START from its END across
change-slice boundaries. Genshi then serializes the leftover END as a literal
</br>, which browsers and TinyMCE parse as an *extra* <br> (phantom blank
lines); an unmatched void START instead swallows the next real end tag.
Observed at the tail of production trace 41352a6a's diff.
"""
from __future__ import annotations

from htmldiff2 import render_html_diff

CASES = [
    # Paragraph-merge edit with <br> runs around the boundary (trace shape).
    (
        "<p>Common intro sentence stays here.<br/><br/>"
        "Old trailing sentence to be replaced.</p>\n"
        "<p>Shared opening sentence of second block.<br/><br/>"
        "Shared middle sentence one.<br/>Shared middle sentence two.</p>",
        "<p>Common intro sentence stays here.<br/><br/>"
        "Brand new replacement sentence appears now. "
        "Shared opening sentence of second block.<br/><br/>"
        "Shared middle sentence one.<br/>Shared middle sentence two.</p>",
    ),
    # Inserted list-like lines separated by <br> (impression-section shape).
    (
        "<p>Line one.<br/><br/>Line two.<br/><br/>Summary:<br/>- old item.</p>",
        "<p>Line one.<br/><br/>Line two extended.<br/><br/>Summary:<br/>"
        "- first item.<br/>- second item.<br/>- third item.</p>",
    ),
]


def test_no_stray_void_end_tags():
    for old, new in CASES:
        out = render_html_diff(old, new)
        for void in ("br", "img", "hr", "input"):
            assert "</%s>" % void not in out, out


def test_img_replacement_keeps_following_end_tags():
    # An unmatched void START must not swallow the wrapper's real end tag.
    out = render_html_diff('<img src="pic0.jpg"/>', '<img src="pic1.jpg"/>')
    assert out.endswith("</div>"), out
    assert "</img>" not in out, out
