"""
Old content that no new list item accounts for must stay visible as removed.

When a <p> is upgraded to a list, the whole old block is kept in a hidden
``structural-revert-data`` payload and each new <li> is matched back to an old
sentence so edits show inline. An old sentence that matches no <li> was never
emitted anywhere visible: the model dropped an entire item, or replaced a
paragraph with an unrelated list, and the reviewer saw nothing struck through.

Observed in production on a Portuguese ultrasound report, where
"Endometrio: Regular, medindo mm de espessura." was replaced by a list of
fibroid measurements and vanished into the hidden payload.
"""
from __future__ import annotations

import re

from htmldiff2 import render_html_diff


def _visible_deletions(out: str) -> str:
    """Text inside <del> markers the reviewer can see (hidden payload excluded)."""
    visible = re.sub(
        r'<del[^>]*style="display:none"[^>]*>.*?</del>', "", out, flags=re.S
    )
    return " ".join(
        re.sub(r"<[^>]+>", "", match)
        for match in re.findall(r"<del[^>]*>(.*?)</del>", visible, flags=re.S)
    )


def test_dropped_item_is_shown_as_removed():
    """An item the model removed entirely must be struck through."""
    old = (
        "<p><strong>IMPRESION:</strong><br/>"
        "1. Nodulo pulmonar de 17,9 mm en lobulo medio. "
        "2. Adenopatia hiliar derecha de 12 mm, sospechosa. "
        "3. Fibrosis pulmonar leve estable.</p>"
    )
    new = (
        "<p><strong>IMPRESION:</strong></p><ol>"
        "<li>Nodulo pulmonar de 17,9 mm en lobulo medio.</li>"
        "<li>Fibrosis pulmonar leve estable.</li></ol>"
    )

    struck = _visible_deletions(render_html_diff(old, new))

    assert "Adenopatia hiliar derecha de 12 mm" in struck


def test_paragraph_replaced_by_unrelated_list_is_shown_as_removed():
    """The production case: a sentence replaced by a list it does not appear in."""
    old = (
        "<p><strong>Utero:</strong> Aumentado de volume.</p>"
        "<p><strong>Endometrio:</strong> Regular, medindo mm de espessura.</p>"
    )
    new = (
        "<p><strong>Utero:</strong> Aumentado de volume.</p><ul>"
        "<li>42 x 33 mm na parede fundica (intramural).</li>"
        "<li>32 x 27 mm na parede anterior (intramural).</li></ul>"
    )

    struck = _visible_deletions(render_html_diff(old, new))

    assert "medindo mm de espessura" in struck


def test_heading_kept_by_the_model_is_not_struck():
    """A heading the new document still carries is not shown as removed."""
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

    assert "IMPRESION" not in struck


def test_lossless_conversion_adds_no_removal_markers():
    """Upgrading typed numbering with the wording intact strikes nothing."""
    old = (
        "<p>1. Nodulo perifisural de 4,5 mm estable. "
        "2. Fibrosis pulmonar leve estable. "
        "3. Extensas calcificaciones vasculares.</p>"
    )
    new = (
        "<ol><li>Nodulo perifisural de 4,5 mm estable.</li>"
        "<li>Fibrosis pulmonar leve estable.</li>"
        "<li>Extensas calcificaciones vasculares.</li></ol>"
    )

    assert _visible_deletions(render_html_diff(old, new)).strip() == ""


def test_item_split_in_two_is_not_struck():
    """One old sentence rewritten as two items is accounted for, not removed."""
    old = (
        "<p>1. Nodulo perifisural de 4,5 mm estable y fibrosis pulmonar leve. "
        "2. Extensas calcificaciones vasculares.</p>"
    )
    new = (
        "<ol><li>Nodulo perifisural de 4,5 mm estable.</li>"
        "<li>Fibrosis pulmonar leve.</li>"
        "<li>Extensas calcificaciones vasculares.</li></ol>"
    )

    struck = _visible_deletions(render_html_diff(old, new))

    assert "Nodulo perifisural" not in struck


def _visible_text(out: str) -> str:
    """All text the reviewer sees, hidden revert payload excluded."""
    visible = re.sub(
        r'<del[^>]*style="display:none"[^>]*>.*?</del>', "", out, flags=re.S
    )
    return re.sub(r"<[^>]+>", " ", visible)


def test_heading_returned_by_the_model_is_rendered():
    """The new heading block beside the list must be emitted, not swallowed."""
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

    out = render_html_diff(old, new)

    assert _visible_text(out).count("IMPRESION") == 1
    assert "IMPRESION" not in _visible_deletions(out)


def test_rewritten_paragraph_beside_new_list_is_rendered():
    """A paragraph the model rewrote next to the new list must stay visible."""
    old = (
        "<p><strong>Utero:</strong> Aumentado de volume.</p>"
        "<p><strong>Endometrio:</strong> Regular, medindo mm de espessura.</p>"
    )
    new = (
        "<p><strong>Utero:</strong> Aumentado de volume.</p>"
        "<p><strong>Endometrio:</strong> Regular, medindo 4 mm de espessura.</p><ul>"
        "<li>42 x 33 mm na parede fundica (intramural).</li>"
        "<li>32 x 27 mm na parede anterior (intramural).</li></ul>"
    )

    out = render_html_diff(old, new)

    assert "medindo 4 mm de espessura" in _visible_text(out)


def _visible_insertions(out: str) -> str:
    """Text inside <ins> markers."""
    return " ".join(
        re.sub(r"<[^>]+>", "", match)
        for match in re.findall(r"<ins[^>]*>(.*?)</ins>", out, flags=re.S)
    )


def test_rewritten_item_shows_both_sides():
    """An item rewritten beyond a truncation: old wording struck, new wording inserted.

    Recorded on the testing environment (run c92670ba): the model replaced the
    measurements and suspicion of item 1 with "sugestivo de proceso neoplasico",
    and neither the removal nor the new wording was marked.
    """
    old = (
        "<p><strong>CONCLUSION RADIOGRAFICA.</strong><br/>"
        "1. Crecimiento significativo del nodulo en el lobulo medio, segmento medio, "
        "de 17,9 mm (previo 3,7 mm). Hallazgo de alta sospecha, probable metastasis "
        "versus nuevo primario pulmonar. "
        "2. Nodulo perifisural de 4,5 mm estable. "
        "3. Fibrosis pulmonar leve estable.</p>"
    )
    new = (
        "<p><strong>CONCLUSION RADIOGRAFICA.</strong></p><ol>"
        "<li>Crecimiento significativo del nodulo en el lobulo medio, "
        "sugestivo de proceso neoplasico.</li>"
        "<li>Nodulo perifisural estable.</li>"
        "<li>Fibrosis pulmonar leve estable.</li></ol>"
    )

    out = render_html_diff(old, new)

    assert "17,9 mm" in _visible_deletions(out)
    assert "metastasis" in _visible_deletions(out)
    assert "sugestivo de proceso neoplasico" in _visible_insertions(out)
    assert "CONCLUSION" not in _visible_deletions(out)
