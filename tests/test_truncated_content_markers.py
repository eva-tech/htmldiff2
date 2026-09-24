"""
Regression for content dropped without a change marker (AI-659).

A radiologist reviews the suggested report before signing it, so anything the
model removes has to be visible as a deletion. When typed numbering ("1. 2. 3.")
is upgraded to a real list, the old paragraph and the new <li> elements are
matched sentence by sentence to produce inline markers. Two things defeated that
matching and emitted the items with no <del> at all, so dropped measurements,
prior-study comparisons and hedged suspicion disappeared silently:

* old sentences were split only at <br/>, so a paragraph numbering its items as
  literal text stayed a single sentence and lined up with no <li>;
* a guard skipped any old sentence more than 1.5x the new item's length, which
  is precisely what a truncation looks like.

Observed regenerating the impression of the report in AI-657.
"""
from __future__ import annotations

import re

from htmldiff2 import render_html_diff

# The impression of AI-657's reproduction: heading and hand-typed numbering
# sharing one paragraph.
OLD = (
    "<p><strong>HALLAZGOS:</strong><br/>Nodulo de 17,9 mm, previo 3,7 mm.</p>"
    "<p><strong>IMPRESION:</strong><br/>"
    "1. Crecimiento significativo del nodulo en el lobulo medio, segmento medio, "
    "de 17,9 mm (previo 3,7 mm). Hallazgo de alta sospecha, probable metastasis "
    "versus nuevo primario pulmonar. "
    "2. Nodulo perifisural de 4,5 mm estable. "
    "3. Fibrosis pulmonar leve estable.</p>"
)


def _visible_deletions(out: str) -> str:
    """Text inside <del> markers that the reviewer can actually see.

    A structural revert payload rides in a hidden
    ``<del class="structural-revert-data" style="display:none">``. It is state
    for the reject path, not a change marker, so it must not count as content
    shown as removed.
    """
    visible = re.sub(
        r'<del[^>]*style="display:none"[^>]*>.*?</del>', "", out, flags=re.S
    )
    return " ".join(
        re.sub(r"<[^>]+>", "", match)
        for match in re.findall(r"<del[^>]*>(.*?)</del>", visible, flags=re.S)
    )


def test_truncated_item_shows_dropped_wording_as_removed():
    """Wording dropped while shortening an item must be struck through."""
    new = (
        "<p><strong>HALLAZGOS:</strong><br/>Nodulo de 17,9 mm, previo 3,7 mm.</p>"
        "<p><strong>IMPRESION:</strong></p><ol>"
        "<li>Crecimiento significativo del nodulo en el lobulo medio.</li>"
        "<li>Nodulo perifisural de 4,5 mm estable.</li>"
        "<li>Fibrosis pulmonar leve estable.</li></ol>"
    )

    struck = _visible_deletions(render_html_diff(OLD, new))

    assert "17,9 mm" in struck, "dropped measurement must be shown as removed"
    assert "previo 3,7 mm" in struck, "dropped prior comparison must be shown as removed"
    assert "metastasis" in struck, "dropped suspicion must be shown as removed"


def test_items_carried_over_verbatim_are_not_marked():
    """Converting numbering to a list is not itself a deletion."""
    old = (
        "<p><strong>IMPRESION:</strong><br/>"
        "1. Nodulo perifisural de 4,5 mm estable. "
        "2. Fibrosis pulmonar leve estable.</p>"
    )
    new = (
        "<p><strong>IMPRESION:</strong></p><ol>"
        "<li>Nodulo perifisural de 4,5 mm estable.</li>"
        "<li>Fibrosis pulmonar leve estable.</li></ol>"
    )

    struck = _visible_deletions(render_html_diff(old, new))

    assert struck.strip() == "", f"nothing was removed, but got: {struck!r}"


def test_decimal_measurement_is_not_an_item_boundary():
    """A number inside a sentence must not split it into two items.

    "de 17,9 mm" and "3. Fibrosis" both contain a digit followed by punctuation;
    only the latter starts an item.
    """
    new = (
        "<p><strong>HALLAZGOS:</strong><br/>Nodulo de 17,9 mm, previo 3,7 mm.</p>"
        "<p><strong>IMPRESION:</strong></p><ol>"
        "<li>Crecimiento significativo del nodulo en el lobulo medio, segmento medio, "
        "de 17,9 mm (previo 3,7 mm). Hallazgo de alta sospecha, probable metastasis "
        "versus nuevo primario pulmonar.</li>"
        "<li>Nodulo perifisural de 4,5 mm estable.</li>"
        "<li>Fibrosis pulmonar leve estable.</li></ol>"
    )

    struck = _visible_deletions(render_html_diff(OLD, new))

    assert struck.strip() == "", (
        f"every item was carried over verbatim, but got struck: {struck!r}"
    )


def test_unrelated_shorter_item_is_not_matched_as_a_truncation():
    """A genuinely different, shorter item must not be paired with an old one."""
    old = (
        "<p><strong>IMPRESION:</strong><br/>"
        "1. Extensas calcificaciones vasculares en la aorta toracica y sus ramas. "
        "2. Fibrosis pulmonar leve estable.</p>"
    )
    new = (
        "<p><strong>IMPRESION:</strong></p><ol>"
        "<li>Derrame pleural izquierdo.</li>"
        "<li>Fibrosis pulmonar leve estable.</li></ol>"
    )

    out = render_html_diff(old, new)

    assert "Derrame pleural izquierdo" in out, "the new item must appear"


def test_truncation_survives_paraphrase_and_accents():
    """A rewrite that also inserts a word or shifts punctuation still marks the cut.

    Taken from a live run: the model shortened the item, inserted "sólido", and
    ended it with a full stop where the original had a comma. Comparing raw
    tokens counted each of those as a changed word and dropped containment just
    below the threshold, so the removed measurement and suspicion went unmarked.
    """
    old = (
        "<p><strong>IMPRESIÓN:</strong><br/>"
        "1. Crecimiento significativo del nódulo en el lóbulo medio, segmento medio, "
        "de 17,9 mm (previo 3,7 mm). Hallazgo de alta sospecha, probable metástasis "
        "versus nuevo primario pulmonar. "
        "2. Nódulo perifisural de 4,5 mm estable.</p>"
    )
    new = (
        "<p><strong>IMPRESIÓN:</strong></p><ol>"
        "<li>Crecimiento significativo del nódulo sólido en el lóbulo medio.</li>"
        "<li>Nódulo perifisural estable.</li></ol>"
    )

    struck = _visible_deletions(render_html_diff(old, new))

    assert "17,9 mm" in struck, "dropped measurement must be shown as removed"
    assert "metástasis" in struck, "dropped suspicion must be shown as removed"
    assert "de 4,5 mm" in struck, "dropped measurement in item 2 must be shown"


def test_accent_only_difference_is_not_a_deletion():
    """Correcting an accent is a spelling change, not removed content."""
    old = (
        "<p><strong>IMPRESIÓN:</strong><br/>"
        "1. Nodulo perifisural estable. 2. Fibrosis pulmonar leve.</p>"
    )
    new = (
        "<p><strong>IMPRESIÓN:</strong></p><ol>"
        "<li>Nódulo perifisural estable.</li>"
        "<li>Fibrosis pulmonar leve.</li></ol>"
    )

    struck = _visible_deletions(render_html_diff(old, new))

    assert "perifisural" not in struck, (
        f"an accent fix must not read as removed content, got: {struck!r}"
    )
