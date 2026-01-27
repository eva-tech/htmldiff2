import doctest
import re
import xml.etree.ElementTree as ET

import html5lib
import htmldiff2
from htmldiff2 import DiffConfig


def _local_name(tag: str) -> str:
    # html5lib's etree builder may include the XHTML namespace.
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _norm_text(s: str) -> str:
    # Normalize whitespace + NBSP for robust comparisons.
    if not s:
        return ""
    return " ".join(s.replace("\u00a0", " ").split())


def _text_content(el: ET.Element) -> str:
    return _norm_text("".join(el.itertext()))


def _has_class(el: ET.Element, class_name: str) -> bool:
    cls = el.get("class") or ""
    parts = [p for p in cls.split() if p]
    return class_name in parts


def _parse_fragment(html: str) -> ET.Element:
    # Wrap fragment into a stable root for querying.
    root = ET.Element("root")
    for child in html5lib.parseFragment(html, treebuilder="etree"):
        root.append(child)
    return root


def _find_by_tag_and_text(root: ET.Element, tag: str, expected_text: str) -> list[ET.Element]:
    expected = _norm_text(expected_text)
    matches = []
    for el in root.iter():
        if _local_name(el.tag) != tag:
            continue
        if _text_content(el) == expected:
            matches.append(el)
    return matches


def test_doctests_htmldiff2_module():
    # Keep parity with legacy `python test.py` runner.
    res = doctest.testmod(htmldiff2, verbose=False)
    assert res.failed == 0


def test_delete_before_insert_ordering():
    out = htmldiff2.render_html_diff("Foo baz", "Foo blah baz")
    assert "<ins" in out


def test_inline_wrapper_change_does_not_delete_whole_sentence():
    before = """<div class="report-content">
            <p>
                <span>CLINICAL HISTORY:</span> The patient reports chest pain and fatigue.
            </p>
        </div>"""
    after = """<div class="report-content">
            <p>
                <strong>CLINICAL HISTORY:</strong> The patient reports chest pain and fatigue.
            </p>
        </div>"""
    out = htmldiff2.render_html_diff(before, after)
    assert "The patient reports chest pain and fatigue." in out
    assert "<del>The patient reports chest pain and fatigue." not in out
    assert "<ins>The patient reports chest pain and fatigue." not in out


def test_style_only_change_is_marked():
    out = htmldiff2.render_html_diff(
        'Foo <span style="font-size:14px">bar</span>',
        'Foo <span style="font-size:20px">bar</span>',
    )
    assert "<del" in out
    assert "<ins" in out
    assert "font-size:14px" in out
    assert "font-size:20px" in out


def test_linebreak_marker_visible_on_insert_and_delete():
    out = htmldiff2.render_html_diff("Foo", "Foo<br>Bar")
    assert "\u00b6" in out

    out = htmldiff2.render_html_diff("FooBar", "Foo<br><br>Bar")
    assert out.count("\u00b6") >= 2

    out = htmldiff2.render_html_diff("Foo<br><br>Bar", "FooBar")
    assert out.count("\u00b6") >= 2


def test_moving_linebreaks_does_not_touch_unchanged_paragraphs():
    before = """<div class="report-content">
    <h3>REPORT STATUS: FINAL</h3>
    <p><strong>INDICATION:</strong> Severe headache.</p>
    <p><strong>COMPARISON:</strong> None available.</p>
    <br><br><br>
    <p><strong>Electronically Signed by:</strong> Dr. Strange</p>
</div>"""
    after = """<div class="report-content">
    <h3>REPORT STATUS: FINAL</h3>
    <br><br>
    <p><strong>INDICATION:</strong> Severe headache.</p>
    <p><strong>COMPARISON:</strong> None available.</p>
    <p><strong>Electronically Signed by:</strong> Dr. Strange</p>
</div>"""
    out = htmldiff2.render_html_diff(before, after)
    assert out.count("Severe headache.") == 1
    assert "<del>Severe headache." not in out and "<ins>Severe headache." not in out
    assert out.count("None available.") == 1
    assert "<del>None available." not in out and "<ins>None available." not in out
    assert out.count("Dr. Strange") == 1
    assert "<del>Dr. Strange" not in out and "<ins>Dr. Strange" not in out
    assert out.count("\u00b6") >= 5


def test_list_change_localized_to_modified_item():
    old = "<ul><li>Uno</li><li>Dos</li><li>Tres</li></ul>"
    new = "<ul><li>Uno</li><li>Dos cambiado</li><li>Tres</li></ul>"
    out = htmldiff2.render_html_diff(old, new)
    assert "<ins" in out and "cambiado" in out
    assert not re.search(r"<del[^>]*>Uno</del>", out)
    assert not re.search(r"<del[^>]*>Tres</del>", out)


def test_table_cell_change_localized():
    old = "<table><tr><td>A</td><td>B</td></tr></table>"
    new = "<table><tr><td>A</td><td>C</td></tr></table>"
    out = htmldiff2.render_html_diff(old, new)
    assert re.search(r"<del[^>]*>B</del>", out), out
    assert re.search(r"<ins[^>]*>C</ins>", out), out
    assert not re.search(r"<del[^>]*>A</del>", out), out


def test_table_remove_intermediate_column_with_duplicates_marks_correct_column():
    # Regression for "remove intermediate column" with duplicate values (e.g. 8 and 8).
    old = """<table>
<thead><tr>
<th>Localización</th>
<th>Diámetro Actual (mm)</th>
<th>Diámetro Previo (mm)</th>
<th>Cambio (%)</th>
<th>Fecha Previa</th>
</tr></thead>
<tbody>
<tr><td>LSD</td><td>11</td><td>10</td><td>+10%</td><td>Enero 2024</td></tr>
<tr><td>LII</td><td>8</td><td>8</td><td>0%</td><td>Enero 2024</td></tr>
</tbody>
</table>"""
    new = """<table>
<thead><tr>
<th>Localización</th>
<th>Diámetro Actual (mm)</th>
<th>Cambio (%)</th>
<th>Fecha Previa</th>
</tr></thead>
<tbody>
<tr><td>LSD</td><td>11</td><td>+10%</td><td>Enero 2024</td></tr>
<tr><td>LII</td><td>8</td><td>0%</td><td>Enero 2024</td></tr>
</tbody>
</table>"""
    out = htmldiff2.render_html_diff(old, new)
    root = _parse_fragment(out)

    # Deleted header must be the "Diámetro Previo (mm)" column (not "Cambio (%)").
    th_prev = _find_by_tag_and_text(root, "th", "Diámetro Previo (mm)")
    assert th_prev, out
    assert any(_has_class(el, "tagdiff_deleted") for el in th_prev), out

    th_cambio = _find_by_tag_and_text(root, "th", "Cambio (%)")
    assert th_cambio, out
    assert all(not _has_class(el, "tagdiff_deleted") for el in th_cambio), out

    # Deleted cells should contain both removed values (10 and 8) in deleted <td>s.
    deleted_tds = [el for el in root.iter() if _local_name(el.tag) == "td" and _has_class(el, "tagdiff_deleted")]
    deleted_texts = {_text_content(el) for el in deleted_tds}
    assert "10" in deleted_texts, out
    assert "8" in deleted_texts, out


def test_bullets_paragraph_to_list_conversion_is_grouped_and_structural():
    # From scripts/repro_bullets_group.py
    before = """
<div>
<p><strong>HALLAZGOS:</strong></p>
<p><strong>Hepatobiliar:</strong> El hígado presenta morfología, tamaño y señal habituales.</p>
<p><strong>Vesícula Biliar:</strong> La vesícula biliar es de tamaño y grosor de pared normales.</p>
</div>
"""
    after = """
<div>
<p><strong>HALLAZGOS:</strong></p>
<ul>
<li><strong>Hepatobiliar:</strong> El hígado presenta morfología, tamaño y señal habituales.</li>
<li><strong>Vesícula Biliar:</strong> La vesícula biliar es de tamaño y grosor de pared normales.</li>
</ul>
</div>
"""
    cfg = DiffConfig()
    cfg.add_diff_ids = True
    out = htmldiff2.render_html_diff(before, after, config=cfg)

    # Structural markers should exist (frontend removes/keeps whole nodes).
    assert "tagdiff_added" in out
    assert "<ul" in out and "<li" in out
    # Insert markers should be present inside the added items.
    assert "<ins" in out


def test_table_remove_description_column_marks_deleted_cells():
    # From scripts/repro_table_remove_description_column_exact.py
    old = """<table>
<thead><tr>
<th>Hallazgo</th><th>Descripción</th><th>Localización</th><th>Tamaño</th>
</tr></thead>
<tbody>
<tr><td>Masa pulmonar</td><td>Con bordes espiculados</td><td>Lóbulo superior derecho</td><td>Aprox 3 cm</td></tr>
<tr><td>Adenopatías mediastínicas</td><td> </td><td>Mediastino</td><td>1.5 cm</td></tr>
</tbody>
</table>"""
    new = """<table>
<thead><tr>
<th>Hallazgo</th><th>Localización</th><th>Tamaño</th>
</tr></thead>
<tbody>
<tr><td>Masa pulmonar</td><td>Lóbulo superior derecho</td><td>Aprox 3 cm</td></tr>
<tr><td>Adenopatías mediastínicas</td><td>Mediastino</td><td>1.5 cm</td></tr>
</tbody>
</table>"""
    cfg = DiffConfig()
    cfg.add_diff_ids = True
    out = htmldiff2.render_html_diff(old, new, config=cfg)
    assert "Descripción" in out
    assert "tagdiff_deleted" in out

    root = _parse_fragment(out)

    th_desc = _find_by_tag_and_text(root, "th", "Descripción")
    assert th_desc, out
    assert any(_has_class(el, "tagdiff_deleted") for el in th_desc), out

    th_loc = _find_by_tag_and_text(root, "th", "Localización")
    assert th_loc, out
    assert all(not _has_class(el, "tagdiff_deleted") for el in th_loc), out


def test_td_style_change_preserves_table_and_has_ids():
    # From scripts/repro_td_style.py (keep this lenient; exact rendering may evolve).
    before = '<table><tr><td style="color:red">Test</td></tr></table>'
    after = '<table><tr><td style="color:blue">Test</td></tr></table>'
    cfg = DiffConfig()
    cfg.add_diff_ids = True
    cfg.visual_replace_inline = True
    out = htmldiff2.render_html_diff(before, after, config=cfg)
    assert "<table" in out and "</table>" in out
    assert "color:red" in out
    assert "color:blue" in out
    assert re.search(r'\bdata-diff-id="[^"]+"', out)


def test_default_config_tracks_refs_and_td_th_visual():
    cfg = DiffConfig()
    assert "ref" in cfg.track_attrs
    assert "data-ref" in cfg.track_attrs
    assert "td" in cfg.visual_container_tags
    assert "th" in cfg.visual_container_tags

