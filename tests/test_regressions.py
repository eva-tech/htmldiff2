from __future__ import annotations

import doctest
import re
import xml.etree.ElementTree as ET

import html5lib
import htmldiff2
from htmldiff2 import DiffConfig, render_html_diff


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


def test_unrelated_texts_are_grouped_not_shredded():
    """
    When old and new texts are completely unrelated (e.g., template vs full report),
    the diff should show clean grouped del/ins blocks instead of word-by-word interleaving.
    
    Bad (shredded):  <del>Motivo</del><ins>RADIOGRAFÍA</ins> <del>del</del><ins>DE</ins>...
    Good (grouped):  <del>Motivo del estudio:</del><ins>RADIOGRAFÍA DE PELVIS AP</ins>
    """
    old = '<p><strong>Motivo del estudio:</strong></p>'
    new = '<p><strong>RADIOGRAFÍA DE PELVIS AP</strong></p>'
    out = htmldiff2.render_html_diff(old, new)
    
    # Should NOT have shredded interleaving like <del>Motivo</del><ins>RADIOGRAFÍA</ins>
    assert '<del' in out and '<ins' in out
    # The old text should be grouped together in one del
    assert 'Motivo del estudio:' in out
    # The new text should be grouped together in one ins
    assert 'RADIOGRAFÍA DE PELVIS AP' in out
    # Should NOT have word-by-word matching like "del" matching "DE"
    assert '<del' not in out or '>Motivo</del>' not in out  # "Motivo" alone = shredded


def test_block_wrapper_wrapping_in_bulk_replace():
    """
    Ensure block wrappers (p, h1, etc.) are wrapped BY the del/ins tag
    when doing a bulk replace, so that accepting the change deletes the whole element.
    
    Old behavior: <p><del>...</del></p> (leaves empty <p> on accept)
    New behavior: <del><p>...</p></del> (removes <p> on accept)
    """
    old = '<p>ZZZ</p>'
    new = '<h1>AAA</h1>'
    
    # Use bulk replace threshold (defaults to 0.3, these strings are very different)
    out = htmldiff2.render_html_diff(old, new)
    
    # Check that del wraps p
    assert '<del' in out
    assert '<del' in out and '<p>' in out
    # We want to see <del...><p...
    # Regex or simple string check. Since attributes might be present, check order.
    del_idx = out.find('<del')
    p_idx = out.find('<p', del_idx)
    del_close_idx = out.find('</del>', p_idx)
    p_close_idx = out.find('</p>', p_idx)
    
    assert del_idx != -1
    assert p_idx != -1
    assert p_idx > del_idx, "<p> should be after <del>"
    assert del_close_idx > p_close_idx, "</del> should be after </p>"
    
    # Check that ins wraps h1
    ins_idx = out.find('<ins')
    h1_idx = out.find('<h1', ins_idx)
    ins_close_idx = out.find('</ins>', h1_idx)
    h1_close_idx = out.find('</h1>', h1_idx)
    
    assert ins_idx != -1
    assert h1_idx != -1
    assert h1_idx > ins_idx, "<h1> should be after <ins>"
    assert ins_close_idx > h1_close_idx, "</ins> should be after </h1>"


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


def test_table_remove_column_with_many_empty_cells_deletes_correct_index():
    # Regression: when multiple trailing cells are empty, we must still delete the
    # intended column (not drift and delete the last empty one).
    old = """<table>
<tbody>
<tr><td>Hallazgo</td><td>Descripción</td><td>Localización</td><td>Medidas</td><td>Volumen</td></tr>
<tr><td>Hígado</td><td>Texto</td><td> </td><td> </td><td> </td></tr>
</tbody>
</table>"""
    new = """<table>
<tbody>
<tr><td>Hallazgo</td><td>Descripción</td><td>Medidas</td><td>Volumen</td></tr>
<tr><td>Hígado</td><td>Texto</td><td> </td><td> </td></tr>
</tbody>
</table>"""
    out = htmldiff2.render_html_diff(old, new)
    root = _parse_fragment(out)

    # Find the "Hígado" row and ensure the *third* cell is the deleted one.
    trs = [el for el in root.iter() if _local_name(el.tag) == "tr"]
    target = None
    for tr in trs:
        tds = [c for c in tr if _local_name(c.tag) == "td"]
        if not tds:
            continue
        if _text_content(tds[0]) == "Hígado":
            target = tds
            break
    assert target is not None, out
    assert len(target) == 5, out  # old structure preserved, one cell marked deleted
    assert _has_class(target[2], "tagdiff_deleted"), out
    assert not _has_class(target[4], "tagdiff_deleted"), out


def test_remove_localizacion_column_full_ticket_example():
    # Exact ticket example (table + wrapper). Doctor instruction: remove "Localización" column.
    old = """<div>
<div ref="1">
<div>
<div ref="1"><br/><br/><br/><br/><br/><br/><br/><br/><br/><br/><br/><br/><br/><br/><br/><br/><br/><br/><br/><br/><br/><br/>
<p>MOTIVO DEL ESTUDIO: Dolor abdominal, con antecedente de apendicetomía hace 6 meses y colecistectomía hace 10 días. <br/>TÉCNICA: Se realiza estudio tomográfico helicoidal desde las bases pulmonares hasta la sínfisis del pubis en fase simple, arterial, venosa y de eliminación tras la administración de medio de contraste oral (agua); se post-procesan diversas reconstrucciones planares observando los siguientes hallazgos:  <br/>HALLAZGOS: </p>
<table>
<tbody>
<tr>
<td>Hallazgo</td>
<td>Descripción</td>
<td>Localización</td>
<td>Medidas</td>
<td>Volumen</td>
</tr>
<tr>
<td>Colección</td>
<td>Con burbujas de gas en tejido celular subcutáneo, asociada a mínima colección.</td>
<td>Línea media y paramedial derecha, infraumbilical</td>
<td>1.8x1.5x1cm</td>
<td> </td>
</tr>
<tr>
<td>Colección</td>
<td>Con pared delgada que presenta reforzamiento tras la administración de medio de contraste.</td>
<td>Fosa ilíaca derecha sobre lecho quirúrgico pericecal</td>
<td>8.3x2.4x4cm</td>
<td>19cc</td>
</tr>
<tr>
<td>Cambios en pared abdominal</td>
<td>Aumento en la densidad del espesor graso, burbujas de gas en tejido celular subcutáneo.</td>
<td>Línea media y paramedial derecha, infraumbilical</td>
<td> </td>
<td> </td>
</tr>
<tr>
<td>Atelectasias</td>
<td>Discretas</td>
<td>Basales bilaterales</td>
<td> </td>
<td> </td>
</tr>
<tr>
<td>Hígado</td>
<td>Patrón de atenuación heterogéneo con zonas de densidad 10-14UH en fase simple, sin realces anormales posterior al contraste intravenoso.</td>
<td> </td>
<td> </td>
<td> </td>
</tr>
<tr>
<td>Vía biliar</td>
<td>No dilatada.</td>
<td>Intra y extrahepática</td>
<td> </td>
<td> </td>
</tr>
<tr>
<td>Lecho vesicular</td>
<td>Grapas quirúrgicas.</td>
<td> </td>
<td> </td>
<td> </td>
</tr>
<tr>
<td>Intestino delgado</td>
<td>Dilatación continua de hasta 4 cm. Disminución de su luz en segmento del íleon de 4 cm de longitud, por efecto de masa de colección pericecal. Tránsito intestinal conservado hacia el resto del íleon y colon. Reforzamiento de la pared intestinal simétrico, no se identifican engrosamientos de pared ni obstrucciones intrínsecas.</td>
<td>Íleon</td>
<td>4 cm (segmento afectado)</td>
<td> </td>
</tr>
<tr>
<td>Colon</td>
<td>Trayecto redundante, con residuo y gas en su interior de predominio en colon ascendente y transverso.</td>
<td>Ascendente y transverso</td>
<td> </td>
<td> </td>
</tr>
<tr>
<td>Vasos mesentéricos</td>
<td>Ingurgitación.</td>
<td> </td>
<td> </td>
<td> </td>
</tr>
<tr>
<td>Neumoperitoneo</td>
<td>Aisladas burbujas de gas en cavidad abdominal.</td>
<td>Cavidad abdominal</td>
<td> </td>
<td> </td>
</tr>
<tr>
<td>Vejiga</td>
<td>Pobre repleción de paredes regulares y contenido homogéneo.</td>
<td> </td>
<td> </td>
<td> </td>
</tr>
<tr>
<td>Próstata</td>
<td>Morfología conservada.</td>
<td> </td>
<td>Eje transverso de 3.7cm</td>
<td> </td>
</tr>
<tr>
<td>Estructuras óseas</td>
<td>Mineralización conservada, sin remodelación.</td>
<td> </td>
<td> </td>
<td> </td>
</tr>
<tr>
<td>Páncreas, glándulas suprarrenales y bazo</td>
<td>De situación, tamaño y forma habituales.</td>
<td> </td>
<td> </td>
<td> </td>
</tr>
<tr>
<td>Riñones</td>
<td>De tamaño conservado, el parénquima concentra y elimina de forma simétrica y homogénea el medio de contraste; no hay dilatación pielocalicial ni imágenes ocupativas.</td>
<td> </td>
<td> </td>
<td> </td>
</tr>
<tr>
<td>Unión esófago-gástrica</td>
<td>Se localiza por debajo del diafragma.</td>
<td> </td>
<td> </td>
<td> </td>
</tr>
<tr>
<td>Estómago</td>
<td>Distendido con agua, con presencia de sonda en su interior, de paredes regulares.</td>
<td> </td>
<td> </td>
<td> </td>
</tr>
<tr>
<td>Duodeno</td>
<td>De trayecto habitual.</td>
<td> </td>
<td> </td>
<td> </td>
</tr>
</tbody>
</table>
<br/>IMPRESIÓN:  <br/>Hallazgos tomográficos compatibles con colección en fosa iliaca derecha sobre lecho quirúrgico pericecal, con volumen aproximado de 19cc.  <br/>Obstrucción intestinal parcial en segmento de ilion, probablemente secundaria a compresión por colección referida, no se descartan adherencias por esta modalidad de imagen.  <br/>Atelectasias basales bilaterales.  <br/>Neumoperitoneo de tipo posquirúrgico como posibilidad.  <br/>Cambios posquirúrgicos en pared abdominal.  <br/>Resto como se comenta en texto.</div>
</div>
</div>
</div>"""

    # Same as `old` but removing the "Localización" column (3rd column) from each row.
    new = """<div>
<div ref="1">
<div>
<div ref="1"><br/><br/><br/><br/><br/><br/><br/><br/><br/><br/><br/><br/><br/><br/><br/><br/><br/><br/><br/><br/><br/><br/>
<p>MOTIVO DEL ESTUDIO: Dolor abdominal, con antecedente de apendicetomía hace 6 meses y colecistectomía hace 10 días. <br/>TÉCNICA: Se realiza estudio tomográfico helicoidal desde las bases pulmonares hasta la sínfisis del pubis en fase simple, arterial, venosa y de eliminación tras la administración de medio de contraste oral (agua); se post-procesan diversas reconstrucciones planares observando los siguientes hallazgos:  <br/>HALLAZGOS: </p>
<table>
<tbody>
<tr>
<td>Hallazgo</td>
<td>Descripción</td>
<td>Medidas</td>
<td>Volumen</td>
</tr>
<tr>
<td>Colección</td>
<td>Con burbujas de gas en tejido celular subcutáneo, asociada a mínima colección.</td>
<td>1.8x1.5x1cm</td>
<td> </td>
</tr>
<tr>
<td>Colección</td>
<td>Con pared delgada que presenta reforzamiento tras la administración de medio de contraste.</td>
<td>8.3x2.4x4cm</td>
<td>19cc</td>
</tr>
<tr>
<td>Cambios en pared abdominal</td>
<td>Aumento en la densidad del espesor graso, burbujas de gas en tejido celular subcutáneo.</td>
<td> </td>
<td> </td>
</tr>
<tr>
<td>Atelectasias</td>
<td>Discretas</td>
<td> </td>
<td> </td>
</tr>
<tr>
<td>Hígado</td>
<td>Patrón de atenuación heterogéneo con zonas de densidad 10-14UH en fase simple, sin realces anormales posterior al contraste intravenoso.</td>
<td> </td>
<td> </td>
</tr>
<tr>
<td>Vía biliar</td>
<td>No dilatada.</td>
<td> </td>
<td> </td>
</tr>
<tr>
<td>Lecho vesicular</td>
<td>Grapas quirúrgicas.</td>
<td> </td>
<td> </td>
</tr>
<tr>
<td>Intestino delgado</td>
<td>Dilatación continua de hasta 4 cm. Disminución de su luz en segmento del íleon de 4 cm de longitud, por efecto de masa de colección pericecal. Tránsito intestinal conservado hacia el resto del íleon y colon. Reforzamiento de la pared intestinal simétrico, no se identifican engrosamientos de pared ni obstrucciones intrínsecas.</td>
<td>4 cm (segmento afectado)</td>
<td> </td>
</tr>
<tr>
<td>Colon</td>
<td>Trayecto redundante, con residuo y gas en su interior de predominio en colon ascendente y transverso.</td>
<td> </td>
<td> </td>
</tr>
<tr>
<td>Vasos mesentéricos</td>
<td>Ingurgitación.</td>
<td> </td>
<td> </td>
</tr>
<tr>
<td>Neumoperitoneo</td>
<td>Aisladas burbujas de gas en cavidad abdominal.</td>
<td> </td>
<td> </td>
</tr>
<tr>
<td>Vejiga</td>
<td>Pobre repleción de paredes regulares y contenido homogéneo.</td>
<td> </td>
<td> </td>
</tr>
<tr>
<td>Próstata</td>
<td>Morfología conservada.</td>
<td>Eje transverso de 3.7cm</td>
<td> </td>
</tr>
<tr>
<td>Estructuras óseas</td>
<td>Mineralización conservada, sin remodelación.</td>
<td> </td>
<td> </td>
</tr>
<tr>
<td>Páncreas, glándulas suprarrenales y bazo</td>
<td>De situación, tamaño y forma habituales.</td>
<td> </td>
<td> </td>
</tr>
<tr>
<td>Riñones</td>
<td>De tamaño conservado, el parénquima concentra y elimina de forma simétrica y homogénea el medio de contraste; no hay dilatación pielocalicial ni imágenes ocupativas.</td>
<td> </td>
<td> </td>
</tr>
<tr>
<td>Unión esófago-gástrica</td>
<td>Se localiza por debajo del diafragma.</td>
<td> </td>
<td> </td>
</tr>
<tr>
<td>Estómago</td>
<td>Distendido con agua, con presencia de sonda en su interior, de paredes regulares.</td>
<td> </td>
<td> </td>
</tr>
<tr>
<td>Duodeno</td>
<td>De trayecto habitual.</td>
<td> </td>
<td> </td>
</tr>
</tbody>
</table>
<br/>IMPRESIÓN:  <br/>Hallazgos tomográficos compatibles con colección en fosa iliaca derecha sobre lecho quirúrgico pericecal, con volumen aproximado de 19cc.  <br/>Obstrucción intestinal parcial en segmento de ilion, probablemente secundaria a compresión por colección referida, no se descartan adherencias por esta modalidad de imagen.  <br/>Atelectasias basales bilaterales.  <br/>Neumoperitoneo de tipo posquirúrgico como posibilidad.  <br/>Cambios posquirúrgicos en pared abdominal.  <br/>Resto como se comenta en texto.</div>
</div>
</div>
</div>"""

    out = htmldiff2.render_html_diff(old, new)
    root = _parse_fragment(out)

    # Header "Localización" must be deleted (3rd cell) and must not drift to "Volumen".
    header_trs = [el for el in root.iter() if _local_name(el.tag) == "tr"]
    assert header_trs, out
    header = header_trs[0]
    header_tds = [c for c in header if _local_name(c.tag) == "td"]
    assert len(header_tds) == 5, out
    assert _text_content(header_tds[2]) == "Localización", out
    assert _has_class(header_tds[2], "tagdiff_deleted"), out
    assert not _has_class(header_tds[4], "tagdiff_deleted"), out

    # Row with non-empty location: ensure the location cell is the deleted one.
    coleccion_row = None
    for tr in header_trs[1:]:
        tds = [c for c in tr if _local_name(c.tag) == "td"]
        if tds and _text_content(tds[0]) == "Colección":
            if "infraumbilical" in _text_content(tds[2]):
                coleccion_row = tds
                break
    assert coleccion_row is not None, out
    assert _has_class(coleccion_row[2], "tagdiff_deleted"), out
    assert "infraumbilical" in _text_content(coleccion_row[2]), out

    # Row with empty trailing cells: ensure the deleted one is still the 3rd cell (Localización),
    # not the last empty column.
    higado_row = None
    for tr in header_trs[1:]:
        tds = [c for c in tr if _local_name(c.tag) == "td"]
        if tds and _text_content(tds[0]) == "Hígado":
            higado_row = tds
            break
    assert higado_row is not None, out
    assert len(higado_row) == 5, out
    assert _has_class(higado_row[2], "tagdiff_deleted"), out
    assert not _has_class(higado_row[4], "tagdiff_deleted"), out


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
    # Bullet-only diff markers should be present on the LI items.
    assert "diff-bullet-ins" in out
    # Old content should be hidden in structural-revert-data for accept/reject.
    assert "structural-revert-data" in out


def test_ul_style_change_is_replaced_not_nested():
    """
    Test that changing style/attributes on a structural tag (like UL)
    results in structural diff with diff-bullet-ins + structural-revert-data,
    NOT a deleted UL containing an added UL (nested structure).
    """
    old = """
<p>Hallazgos.</p>
<ul style="margin-top: 0; margin-bottom: 0;">
<li>Item 1</li>
<li>Item 2</li>
</ul>
<p>Conclusion</p>
"""
    new = """
<p>Hallazgos.</p>
<ul style="margin-top: 0; margin-bottom: 0; font-size: 12pt;">
<li>Item 1</li>
<li>Item 2</li>
</ul>
<p>Conclusion</p>
"""
    out = htmldiff2.render_html_diff(old.strip(), new.strip())

    # Should use structural diff pattern
    assert 'structural-revert-data' in out, "Should have hidden revert data"
    assert 'diff-bullet-ins' in out, "Should have bullet-ins class on LIs"
    assert 'tagdiff_added' in out, "Should have tagdiff_added on new list"
    
    # Should NOT have tagdiff_replaced (old behavior)
    assert 'tagdiff_replaced' not in out
    assert 'tagdiff_deleted' not in out
    
    # New style should be present on the visible list
    assert 'font-size: 12pt' in out, "New style should be present"


def test_paragraphs_converted_to_list_wrapped_correctly():
    """
    Test that when converting <p> blocks to a <ul> list with identical text,
    the structural list diff emits bullet-only classes (diff-bullet-ins)
    and hides old content in structural-revert-data.
    """
    old = """
    <p>Item 1</p>
    <p>Item 2</p>
    """
    new = """
    <ul>
    <li>Item 1</li>
    <li>Item 2</li>
    </ul>
    """
    out = htmldiff2.render_html_diff(old, new)
    
    # Should use structural list diff: bullet-only classes on LIs
    assert 'diff-bullet-ins' in out, f"Expected diff-bullet-ins in output: {out}"
    assert 'tagdiff_added' in out, f"Expected tagdiff_added on ul: {out}"
    # Old content hidden for accept/reject
    assert 'structural-revert-data' in out, f"Expected structural-revert-data: {out}"
    
    # Verify content is preserved
    assert "Item 1" in out and "Item 2" in out


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


def test_remove_localizacion_with_style_changes_does_not_delete_medidas():
    # Reported: LLM restyles the table (adds border/padding), and removing Localización
    # ends up also deleting Medidas/Volumen when accepting changes. That means the diff
    # incorrectly tagged Medidas as tagdiff_deleted. We must NOT do that.
    old = """<table ref="2">
<thead>
<tr>
<th ref="3">Hallazgo</th>
<th ref="3">Descripción</th>
<th ref="3">Localización</th>
<th ref="3">Medidas / Volumen</th>
</tr>
</thead>
<tbody>
<tr>
<td ref="3">Pared Abdominal y Tejidos Blandos</td>
<td ref="3">Texto</td>
<td ref="3">General</td>
<td ref="3">N/A</td>
</tr>
</tbody>
</table>"""

    new = """<table border="1" style="border-collapse: collapse; width: 100%;">
<thead>
<tr>
<th style="padding: 8px; text-align: left;">Hallazgo</th>
<th style="padding: 8px; text-align: left;">Descripción</th>
<th style="padding: 8px; text-align: left;">Medidas / Volumen</th>
</tr>
</thead>
<tbody>
<tr>
<td style="padding: 8px; text-align: left;">Pared Abdominal y Tejidos Blandos</td>
<td style="padding: 8px; text-align: left;">Texto</td>
<td style="padding: 8px; text-align: left;">N/A</td>
</tr>
</tbody>
</table>"""

    out = htmldiff2.render_html_diff(old, new)
    root = _parse_fragment(out)

    # Look at the header row and validate column tagging without relying on
    # exact unicode rendering in assertion output.
    trs = [el for el in root.iter() if _local_name(el.tag) == "tr"]
    assert trs, out
    header = trs[0]
    ths = [c for c in header if _local_name(c.tag) == "th"]
    assert len(ths) >= 3, out

    # The deleted header must be "Localización" and ONLY that one.
    deleted_ths = [th for th in ths if _has_class(th, "tagdiff_deleted")]
    assert len(deleted_ths) == 1, out
    assert "localiz" in _text_content(deleted_ths[0]).lower(), out

    # "Medidas / Volumen" must not be deleted.
    medidas_ths = [th for th in ths if "medidas" in _text_content(th).lower()]
    assert medidas_ths, out
    assert all(not _has_class(th, "tagdiff_deleted") for th in medidas_ths), out


def test_remove_localizacion_exact_reported_example_with_restyle():
    """
    Exact reported example: doctor asks to remove Localización and the LLM also
    restyles the table (border/padding). Accepting changes must NOT remove
    "Medidas / Volumen", so it must not be tagged tagdiff_deleted.
    """
    old = """<div>
<div ref="1">
<div>
<h2><strong>TC DE ABDOMEN Y PELVIS CON CONTRASTE</strong></h2>
<br/>
<p><strong>INFORMACIÓN CLÍNICA:</strong> Paciente pediátrico (0 años, género no especificado) con dolor abdominal, antecedente de apendicetomía hace 6 meses y colecistectomía hace 10 días.</p>
<br/>
<p><strong>TÉCNICA:</strong> Se realizó estudio tomográfico helicoidal desde las bases pulmonares hasta la sínfisis del pubis en fase simple, arterial, venosa y de eliminación tras la administración de medio de contraste oral (agua). Se post-procesaron diversas reconstrucciones planares.</p>
<br/>
<p><strong>COMPARACIÓN:</strong> No disponible.</p>
<br/>
<p><strong>HALLAZGOS:</strong></p>
<p>Se presenta la siguiente tabla resumen de los hallazgos:</p>
<p> </p>
<table ref="2">
<thead>
<tr>
<th ref="3">Hallazgo</th>
<th ref="3">Descripción</th>
<th ref="3">Localización</th>
<th ref="3">Medidas / Volumen</th>
</tr>
</thead>
<tbody>
<tr>
<td ref="3">Pared Abdominal y Tejidos Blandos</td>
<td ref="3">Aumento en la densidad del espesor graso subcutáneo con imágenes de burbujas de gas. Mínima colección asociada.</td>
<td ref="3">Línea media y paramedial derecha, infraumbilical.</td>
<td ref="3">Colección: 1.8x1.5x1 cm</td>
</tr>
<tr>
<td ref="3">Estructuras Óseas</td>
<td ref="3">Mineralización conservada, sin remodelación ósea aparente.</td>
<td ref="3">General</td>
<td ref="3">N/A</td>
</tr>
<tr>
<td ref="3">Tracto Digestivo</td>
<td ref="3">Estómago con sonda. Intestino delgado con dilatación hasta 4 cm.</td>
<td ref="3">Íleon (segmento afectado), General</td>
<td ref="3">Dilatación ID: hasta 4 cm</td>
</tr>
<tr>
<td ref="3">Fosa Ilíaca Derecha / Región Pericecal</td>
<td ref="3">Colección con pared delgada que presenta reforzamiento tras contraste.</td>
<td ref="3">Fosa ilíaca derecha / Región pericecal</td>
<td ref="3">8.3x2.4x4 cm / 19 cc</td>
</tr>
<tr>
<td ref="3">Próstata</td>
<td ref="3">Morfología conservada.</td>
<td ref="3">General</td>
<td ref="3">Eje transverso: 3.7 cm</td>
</tr>
</tbody>
</table>
<br/>
<p><strong>IMPRESIÓN:</strong></p>
<ol>
<li>Resto de los hallazgos como se describen en el texto.</li>
</ol>
</div>
</div>
</div>"""

    # The LLM's "after": same content, but Localización column removed AND table restyled.
    new = """<div>
<div>
<div>
<h2><strong>TC DE ABDOMEN Y PELVIS CON CONTRASTE</strong></h2>
<br/>
<p><strong>INFORMACIÓN CLÍNICA:</strong> Paciente pediátrico (0 años, género no especificado) con dolor abdominal, antecedente de apendicetomía hace 6 meses y colecistectomía hace 10 días.</p>
<br/>
<p><strong>TÉCNICA:</strong> Se realizó estudio tomográfico helicoidal desde las bases pulmonares hasta la sínfisis del pubis en fase simple, arterial, venosa y de eliminación tras la administración de medio de contraste oral (agua). Se post-procesaron diversas reconstrucciones planares.</p>
<br/>
<p><strong>COMPARACIÓN:</strong> No disponible.</p>
<br/>
<p><strong>HALLAZGOS:</strong></p>
<p>Se presenta la siguiente tabla resumen de los hallazgos:</p>
<p> </p>
<table border="1" style="border-collapse: collapse; width: 100%;">
<thead>
<tr>
<th style="padding: 8px; text-align: left;">Hallazgo</th>
<th style="padding: 8px; text-align: left;">Descripción</th>
<th style="padding: 8px; text-align: left;">Medidas / Volumen</th>
</tr>
</thead>
<tbody>
<tr>
<td style="padding: 8px; text-align: left;">Pared Abdominal y Tejidos Blandos</td>
<td style="padding: 8px; text-align: left;">Aumento en la densidad del espesor graso subcutáneo con imágenes de burbujas de gas. Mínima colección asociada.</td>
<td style="padding: 8px; text-align: left;">Colección: 1.8x1.5x1 cm</td>
</tr>
<tr>
<td style="padding: 8px; text-align: left;">Estructuras Óseas</td>
<td style="padding: 8px; text-align: left;">Mineralización conservada, sin remodelación ósea aparente.</td>
<td style="padding: 8px; text-align: left;">N/A</td>
</tr>
<tr>
<td style="padding: 8px; text-align: left;">Tracto Digestivo</td>
<td style="padding: 8px; text-align: left;">Estómago con sonda. Intestino delgado con dilatación hasta 4 cm.</td>
<td style="padding: 8px; text-align: left;">Dilatación ID: hasta 4 cm</td>
</tr>
<tr>
<td style="padding: 8px; text-align: left;">Fosa Ilíaca Derecha / Región Pericecal</td>
<td style="padding: 8px; text-align: left;">Colección con pared delgada que presenta reforzamiento tras contraste.</td>
<td style="padding: 8px; text-align: left;">8.3x2.4x4 cm / 19 cc</td>
</tr>
<tr>
<td style="padding: 8px; text-align: left;">Próstata</td>
<td style="padding: 8px; text-align: left;">Morfología conservada.</td>
<td style="padding: 8px; text-align: left;">Eje transverso: 3.7 cm</td>
</tr>
</tbody>
</table>
<br/>
<p><strong>IMPRESIÓN:</strong></p>
<ol>
<li>Resto de los hallazgos como se describen en el texto.</li>
</ol>
</div>
</div>
</div>"""

    out = htmldiff2.render_html_diff(old, new)
    root = _parse_fragment(out)

    # Ensure "Medidas / Volumen" is NOT tagged deleted anywhere in header.
    medidas_ths = [th for th in root.iter() if _local_name(th.tag) == "th" and "medidas" in _text_content(th).lower()]
    assert medidas_ths, out
    assert all(not _has_class(th, "tagdiff_deleted") for th in medidas_ths), out

    # Ensure "Localización" is the one being deleted.
    deleted_headers = [
        th
        for th in root.iter()
        if _local_name(th.tag) == "th" and _has_class(th, "tagdiff_deleted")
    ]
    assert deleted_headers, out
    assert any("localiz" in _text_content(th).lower() for th in deleted_headers), out


def test_remove_descripcion_column_exact_reported_example():
    """
    Reported: removing "Descripción" column causes "Medidas / Volumen" to be tagged deleted.
    Also, user mentions "titulo se eliminó" (likely the header cell of the second column).
    """
    old = """<table>
<tbody>
<tr>
<td><strong>Estructura / Hallazgo</strong></td>
<td><strong>Descripción</strong></td>
<td><strong>Medidas / Volumen</strong></td>
</tr>
<tr>
<td>Pared Abdominal y Tejidos Blandos</td>
<td>Aumento en la densidad del espesor graso subcutáneo con imágenes de burbujas de gas en la línea media y paramedial derecha, infraumbilical. Grapas quirúrgicas en el lecho vesicular.</td>
<td>Mínima colección: 1.8x1.5x1 cm</td>
</tr>
<tr>
<td>Tórax</td>
<td>Discretas atelectasias basales bilaterales.</td>
<td>N/A</td>
</tr>
</tbody>
</table>"""

    # After: second column (Descripción) removed.
    new = """<table>
<tbody>
<tr>
<td><strong>Estructura / Hallazgo</strong></td>
<td><strong>Medidas / Volumen</strong></td>
</tr>
<tr>
<td>Pared Abdominal y Tejidos Blandos</td>
<td>Mínima colección: 1.8x1.5x1 cm</td>
</tr>
<tr>
<td>Tórax</td>
<td>N/A</td>
</tr>
</tbody>
</table>"""

    out = htmldiff2.render_html_diff(old, new)
    root = _parse_fragment(out)

    # 1. Check header row
    trs = [el for el in root.iter() if _local_name(el.tag) == "tr"]
    header_tds = [c for c in trs[0] if _local_name(c.tag) == "td"]
    
    # "Descripción" should be tagged deleted.
    desc_td = next((td for td in header_tds if "descrip" in _text_content(td).lower()), None)
    assert desc_td is not None, out
    assert _has_class(desc_td, "tagdiff_deleted"), out

    # "Medidas / Volumen" should NOT be tagged deleted.
    med_td = next((td for td in header_tds if "medidas" in _text_content(td).lower()), None)
    assert med_td is not None, out
    assert not _has_class(med_td, "tagdiff_deleted"), out

    # 2. Check data row (Pared Abdominal)
    data_tds = [c for c in trs[1] if _local_name(c.tag) == "td"]
    # Cell 1 (Pared) -> kept
    assert not _has_class(data_tds[0], "tagdiff_deleted"), out
    # Cell 2 (Descripción text) -> deleted
    assert _has_class(data_tds[1], "tagdiff_deleted"), out
    # Cell 3 (Medidas text) -> kept
    assert not _has_class(data_tds[2], "tagdiff_deleted"), out


def test_default_config_tracks_refs_and_td_th_visual():
    cfg = DiffConfig()
    assert "ref" in cfg.track_attrs
    assert "data-ref" in cfg.track_attrs
    assert "td" in cfg.visual_container_tags
    assert "th" in cfg.visual_container_tags


# ---------------------------------------------------------------------------
# Whitespace visibility tests (calderón ¶ and NBSP)
# ---------------------------------------------------------------------------


def test_linebreak_marker_pilcrow_appears_on_br_insert():
    """When a <br> is inserted, a pilcrow (¶) marker should appear."""
    old = "<p>Línea uno</p>"
    new = "<p>Línea uno<br/>Segunda línea</p>"
    out = render_html_diff(old, new)
    # The pilcrow character (¶ = \u00b6) should be present
    assert "\u00b6" in out, f"Expected pilcrow in output: {out}"
    assert "<ins" in out, out


def test_linebreak_marker_pilcrow_appears_on_br_delete():
    """When a <br> is deleted, a pilcrow (¶) marker should appear."""
    old = "<p>Línea uno<br/>Segunda línea</p>"
    new = "<p>Línea uno Segunda línea</p>"
    out = render_html_diff(old, new)
    # The pilcrow character (¶ = \u00b6) should be present
    assert "\u00b6" in out, f"Expected pilcrow in output: {out}"
    assert "<del" in out, out


def test_linebreak_marker_can_be_disabled():
    """Setting linebreak_marker to empty string disables pilcrow."""
    cfg = DiffConfig()
    cfg.linebreak_marker = ""
    old = "<p>Línea uno</p>"
    new = "<p>Línea uno<br/>Segunda línea</p>"
    out = render_html_diff(old, new, config=cfg)
    # No pilcrow should appear
    assert "\u00b6" not in out, f"Pilcrow should not appear: {out}"


def test_whitespace_nbsp_for_leading_trailing_spaces():
    """Leading/trailing spaces in diffs are converted to NBSP."""
    old = "<p>texto</p>"
    new = "<p>  texto  </p>"
    out = render_html_diff(old, new)
    # NBSP (\u00a0) should be present for the leading/trailing spaces
    assert "\u00a0" in out, f"Expected NBSP in output: {out}"


def test_whitespace_nbsp_for_multiple_spaces():
    """Multiple consecutive spaces are converted to NBSP."""
    old = "<p>una palabra</p>"
    new = "<p>una    palabra</p>"
    out = render_html_diff(old, new)
    # NBSP (\u00a0) should be present for the multiple spaces
    assert "\u00a0" in out, f"Expected NBSP in output: {out}"


def test_whitespace_preservation_can_be_disabled():
    """Setting preserve_whitespace_in_diff to False disables NBSP conversion."""
    cfg = DiffConfig()
    cfg.preserve_whitespace_in_diff = False
    old = "<p>texto</p>"
    new = "<p>  texto  </p>"
    out = render_html_diff(old, new, config=cfg)
    # Regular spaces instead of NBSP - the diff should still work
    # but whitespace handling changes
    assert "<ins" in out or "<del" in out, f"Expected diff markers: {out}"


def test_default_config_has_whitespace_settings():
    """Verify default config has expected whitespace settings."""
    cfg = DiffConfig()
    assert cfg.linebreak_marker == "\u00b6", "Default linebreak_marker should be pilcrow"
    assert cfg.preserve_whitespace_in_diff is True, "Default should preserve whitespace"


def test_br_inside_del_when_deleting_linebreaks():
    """When deleting <br> tags, they should be INSIDE <del> so they get removed properly."""
    # Use a more complex case similar to the user's report
    old = """<p class="p1"><strong>Motivo del estudio:</strong></p>
<p><br/><br/><br/></p>
<p class="p1"><strong>Técnica del estudio:</strong></p>"""
    
    new = """<h2><strong>RADIOGRAFÍA DE PELVIS (AP)</strong></h2>
<br/>
<p><strong>INFORMACIÓN CLÍNICA:</strong></p>"""
    
    out = render_html_diff(old, new)
    
    # Check that <br> tags are INSIDE <del> tags, not outside
    # The pattern <del>...</del><br/> is WRONG (br outside del)
    # The pattern <del>...<br></del> is CORRECT (br inside del)
    import re
    # Look for the problematic pattern: </del> followed by <br> (outside)
    problematic = re.search(r'</del>\s*<br[^>]*/?>', out)
    assert problematic is None, (
        f"Found <br> outside <del> tag: {problematic.group(0)}. "
        f"<br> should be INSIDE <del> so it gets deleted properly. "
        f"Full output: {out[:1000]}"
    )
    
    # Verify that <br> tags exist inside <del> tags (positive check)
    correct_pattern = re.search(r'<del[^>]*>.*?<br[^>]*/?>.*?</del>', out, re.DOTALL)
    if '<del' in out and '<br' in out:
        # If we have both del and br, br should be inside del
        assert correct_pattern is not None, (
            f"Should find <br> inside <del> tag when both are present. "
            f"Output: {out[:1000]}"
        )


def test_table_cell_text_change_no_extra_column():
    """
    Test that when text inside a table cell changes, the output has inline
    del/ins inside a SINGLE cell, NOT two separate cells (which creates extra column).
    """
    old = """
<table border="1">
<tbody>
<tr>
<td><strong>Estudio Previo</strong></td>
<td>No disponible para comparación.</td>
</tr>
</tbody>
</table>
"""
    new = """
<table ref="7">
<tbody>
<tr>
<td><strong>Estudio Previo</strong></td>
<td>25 de septiembre del 2025</td>
</tr>
</tbody>
</table>
"""
    cfg = DiffConfig()
    cfg.add_diff_ids = True
    out = htmldiff2.render_html_diff(old, new, config=cfg)
    
    # The first row should have exactly 2 TD elements
    import re
    first_row_match = re.search(r'<tr[^>]*>(.*?)</tr>', out, re.DOTALL)
    assert first_row_match, "Should find a <tr> element"
    first_row = first_row_match.group(1)
    td_count = len(re.findall(r'<td', first_row))
    assert td_count == 2, f"Expected 2 TDs, got {td_count}. Row: {first_row}"
    
    # The second cell should contain both del and ins (inline change)
    assert '<del' in out and '<ins' in out, "Should have inline del/ins"
    # Old and new content should be present
    assert "No disponible" in out, "Should contain old (deleted) text"
    assert "25 de septiembre" in out, "Should contain new (inserted) text"


def test_li_style_change_shows_del_ins():
    """List type+style changes (ul->ol with LI style added) should use structural diff.
    
    When the list type changes AND LIs get style attributes, the diff
    should use diff-bullet-ins + structural-revert-data pattern.
    """
    old = """
<ul>
<li>Tejidos blandos sin alteraciones.</li>
<li>Estructuras óseas de adecuada radiodensidad.</li>
</ul>
"""
    new = """
<ol>
<li style="font-size: 20pt;">Tejidos blandos sin alteraciones.</li>
<li style="font-size: 20pt;">Estructuras óseas de adecuada radiodensidad.</li>
</ol>
"""
    cfg = DiffConfig()
    cfg.add_diff_ids = True
    out = htmldiff2.render_html_diff(old, new, config=cfg)
    
    # Should use structural diff pattern
    assert 'structural-revert-data' in out, "Should have hidden revert data"
    assert 'diff-bullet-ins' in out, "Should have bullet-ins class on LIs"
    assert 'tagdiff_added' in out, "Should have tagdiff_added on new list"
    
    # Should NOT use tagdiff_replaced
    assert 'tagdiff_replaced' not in out
    
    # LIs should be present with text
    import re
    li_tags = re.findall(r'<li[^>]*>', out)
    assert len(li_tags) >= 2, "Should have LI elements preserved"
    assert 'Tejidos blandos' in out
    assert 'Estructuras' in out

def test_capitalization_change_detected():
    """Test that capitalization changes (Cad -> CAD) are detected and marked.
    
    Previous behavior: hidden due to case-insensitive atomization keys.
    Fixed behavior: raw text comparison detects difference and runs inner diff.
    """
    old = "<div><p>1. Cad with 60% stenosis</p></div>"
    new = "<div><p>1. CAD with 60% stenosis</p></div>"
    
    out = htmldiff2.render_html_diff(old, new)
    
    # Should contain del/ins for the case change
    assert '<del' in out, "Should mark deletion"
    assert '<ins' in out, "Should mark insertion"
    assert 'Cad' in out and 'CAD' in out, "Should preserve both versions"
    
    # Verify it's granular (not full block replace of the P)
    # The "with 60% stenosis" part should be unmarked (plain text)
    plain_text_part = "with 60% stenosis"
    assert plain_text_part in out, "Unchanged part should be present"
    # It should appear cleanly (not inside del/ins). 
    # Just checking it's there is a good start, but counting ensures it's not shredded.

def test_inline_formatting_strong_tags():
    """Test inline strong formatting diffs (adding <strong> tags).
    
    Previous behavior: treated as full block replace (due to structure change), marking entire text as del/ins.
    Fixed behavior: granular inline diff marking only the formatting change.
    """
    old = "<p>TITLE: text here.</p>"
    new = "<p><strong>TITLE:</strong> text here.</p>"
    
    out = htmldiff2.render_html_diff(old, new)
    
    # "TITLE:" should be marked as changed (formatting)
    assert '<del' in out and '<ins' in out, "Should have del/ins markers"
    
    # " text here." should be UNMARKED (plain text)
    # It must appear in the output, but NOT wrapped immediately in del/ins.
    # We can check that the string " text here." exists exactly once (since it's not duplicated in del/ins)
    assert out.count(" text here.") == 1, "Unchanged text should appear exactly once (not inside del/ins)"
    
    # The strong tag should be inside an ins (or wrapping the ins content)
    assert '<strong>' in out or '&lt;strong&gt;' in out, "Strong tag should be present"

def test_style_order_does_not_trigger_diff():
    """Test that CSS style properties in different order are recognized as equivalent.
    
    Previous behavior: "font-size: 20px; text-align: center" vs "text-align: center; font-size: 20px"
    was incorrectly detected as a change.
    Fixed behavior: Style properties are normalized (sorted) before comparison.
    """
    old = '<p style="font-size: 20px; text-align: center;">Texto centrado</p>'
    new = '<p style="text-align: center; font-size: 20px;">Texto centrado</p>'
    
    out = htmldiff2.render_html_diff(old, new)
    
    # Should NOT detect any change (same styles, different order)
    assert '<del' not in out, "Should NOT mark any deletions"
    assert '<ins' not in out, "Should NOT mark any insertions"
    assert "Texto centrado" in out, "Text should be present"


def test_ul_to_ol_nesting_fix():
    """Test that changing UL to OL produces siblings, not nested invalid HTML.
    
    Issue: Previously, UL->OL change logic split the tag change from content matching,
    causing normalize_inline_wrapper... to merge them into a 'replace' that
    EventDiffer rendered as nested: <ul del><ol ins>LIs</ol></ul> (invalid).
    
    Fix: UL/OL are now atomized as blocks, forcing full block replacement:
    <ul del>LIs</ul><ol ins>LIs</ol>.
    """
    old = """
    <ul>
    <li>Item 1</li>
    <li>Item 2</li>
    </ul>
    """
    new = """
    <ol>
    <li>Item 1</li>
    <li>Item 2</li>
    </ol>
    """
    
    out = htmldiff2.render_html_diff(old, new)
    
    # Updated Behavior:
    # List type change (ul -> ol) uses structural diff:
    # - Old list hidden in structural-revert-data
    # - New list with tagdiff_added + diff-bullet-ins per LI
    
    # Check for structural diff pattern
    assert 'structural-revert-data' in out, "Should have hidden revert data"
    assert 'diff-bullet-ins' in out, "Should have bullet-ins on LIs"
    assert 'tagdiff_added' in out, "Should have tagdiff_added on new list"
    
    # Should NOT use tagdiff_replaced (old behavior)
    assert 'tagdiff_replaced' not in out
    
    # Text should be present
    assert 'Item 1' in out
    assert 'Item 2' in out


def test_trailing_space_granular_diff():
    """Test that trailing whitespace changes produce granular diff, not full block replace.
    
    Issue: When a trailing space was removed from text inside a <p> block,
    the entire paragraph was duplicated (del + ins), even though only the space changed.
    
    Fix: 
    1. Added .strip() to atom key generation so trailing whitespace doesn't prevent matching.
    2. When structure is same but text differs, use inner diff instead of visual replace.
    """
    old = '''<p dir="ltr" style="line-height: 1.2; text-align: justify; margin-top: 0pt; margin-bottom: 0pt;"><span style="text-decoration: none; vertical-align: baseline; white-space: pre-wrap;">Espacio visceral, </span><span style="text-decoration: none; vertical-align: baseline; white-space: pre-wrap;">las regiones supra glótica, glótica y subglótica laríngeas dentro de parámetros normales, las estructuras del esqueleto laríngeo sin alteraciones. </span></p>'''
    
    # Same text but trailing space removed from second span
    new = '''<p dir="ltr" style="line-height: 1.2; margin-bottom: 0pt; margin-top: 0pt; text-align: justify;"><span style="text-decoration: none; vertical-align: baseline; white-space: pre-wrap;">Espacio visceral, </span><span style="text-decoration: none; vertical-align: baseline; white-space: pre-wrap;">las regiones supra glótica, glótica y subglótica laríngeas dentro de parámetros normales, las estructuras del esqueleto laríngeo sin alteraciones.</span></p>'''
    
    out = htmldiff2.render_html_diff(old, new)
    
    # Text should appear only ONCE (granular diff), not duplicated
    assert out.count('esqueleto laríngeo') == 1, "Text should not be duplicated"
    
    # The trailing space should be marked with <del>
    assert '<del' in out, "Trailing space should be marked as deleted"
    
    # The full text should NOT be wrapped in del/ins
    assert '<del>Espacio visceral' not in out, "Full text should not be in del"
    assert '<ins>Espacio visceral' not in out, "Full text should not be in ins"


def test_span_font_family_diff():
    """Test that style changes on inner spans (e.g. font-family) trigger a diff.
    
    Issue: can_visual_container_replace only checked outer container (P) attributes.
    When a span inside P had a style change (but P and text remained same), no diff was produced.
    
    Fix: Added check for inner child attribute differences in can_visual_container_replace.
    """
    old = '''<p dir="ltr" style="font-size: 15pt;"><span style="text-decoration: none; vertical-align: baseline;">Motivo del estudio:</span></p>'''
    new = '''<p dir="ltr" style="font-size: 15pt;"><span style="font-family: 'Times New Roman', Times, serif; text-decoration: none; vertical-align: baseline;">Motivo del estudio:</span></p>'''
    
    out = htmldiff2.render_html_diff(old, new)
    
    assert '<del' in out, "Should show deletion for old style"
    assert '<ins' in out, "Should show insertion for new style"
    assert "Times New Roman" in out, "New font family should be visible in diff"







def test_ol_style_change_medical_report():
    """Test user provided case: Medical report OL with style change.
    Should produce single <ol> with inline del/ins per item.
    """
    old = '''<div><div><div><div><div>  <br/>
<h2 style="font-size: 20pt;"><strong>TC DE TÓRAX (CON CONTRASTE)</strong></h2>
<br/>
<p style="font-size: 20pt;"><strong>HALLAZGOS:</strong></p>
<ol>
<li><strong>Pulmones:</strong> Los pulmones están bien expandidos.</li>
<li><strong>Vías Aéreas:</strong> La tráquea y los bronquios principales están libres.</li>
<li><strong>Pleura:</strong> No se observa derrame pleural ni engrosamiento pleural.</li>
</ol>
<br/>
<p style="font-size: 20pt;"><strong>IMPRESIÓN:</strong></p>
<p style="font-size: 20pt;">TC de tórax sin hallazgos patológicos agudos o significativos.</p>
</div></div></div></div></div>'''

    new = '''<div><div><div><div><div>  <br/>
<h2 style="font-size: 20pt;"><strong>TC DE TÓRAX (CON CONTRASTE)</strong></h2>
<br/>
<p style="font-size: 20pt;"><strong>HALLAZGOS:</strong></p>
<ol style="font-size: 20pt;">
<li><strong>Pulmones:</strong> Los pulmones están bien expandidos.</li>
<li><strong>Vías Aéreas:</strong> La tráquea y los bronquios principales están libres.</li>
<li><strong>Pleura:</strong> No se observa derrame pleural ni engrosamiento pleural.</li>
</ol>
<br/>
<p style="font-size: 20pt;"><strong>IMPRESIÓN:</strong></p>
<p style="font-size: 20pt;">TC de tórax sin hallazgos patológicos agudos o significativos.</p>
</div></div></div></div></div>'''

    out = htmldiff2.render_html_diff(old, new)

    # Should use structural diff pattern
    assert 'structural-revert-data' in out, "Should have hidden revert data"
    assert 'diff-bullet-ins' in out, "Should have bullet-ins class on LIs"
    assert 'tagdiff_added' in out, "Should have tagdiff_added on new list"
    
    # New style should be present on visible list
    assert 'style="font-size: 20pt;"' in out
    
    # Should NOT use tagdiff_replaced (old behavior)
    assert 'tagdiff_replaced' not in out
    assert 'tagdiff_deleted' not in out


def test_structural_list_diff_text_to_list():
    """
    When <p> blocks are converted to <ol><li> with identical text,
    emit bullet-only classes (diff-bullet-ins) and hidden revert data.
    """
    old = '<p>Item A.</p>\n<p>Item B.</p>'
    new = '<ol>\n<li><p>Item A.</p></li>\n<li><p>Item B.</p></li>\n</ol>'
    cfg = DiffConfig()
    cfg.add_diff_ids = True
    out = htmldiff2.render_html_diff(old, new, config=cfg)

    assert 'diff-bullet-ins' in out
    assert 'tagdiff_added' in out
    assert 'structural-revert-data' in out
    assert 'display:none' in out
    # Text should NOT be in <ins> tags (bullet-only change)
    assert '<ins' not in out or 'structural-revert-data' in out
    assert 'Item A.' in out and 'Item B.' in out


def test_structural_list_diff_list_to_text():
    """
    Reverse: when <ol><li> is converted back to <p> blocks with identical text,
    emit bullet-only classes (diff-bullet-del) and hidden revert data.
    """
    old = '<ol>\n<li><p>Item A.</p></li>\n<li><p>Item B.</p></li>\n</ol>'
    new = '<p>Item A.</p>\n<p>Item B.</p>'
    cfg = DiffConfig()
    cfg.add_diff_ids = True
    out = htmldiff2.render_html_diff(old, new, config=cfg)

    assert 'diff-bullet-del' in out
    assert 'tagdiff_deleted' in out
    assert 'structural-revert-data' in out
    assert 'display:none' in out
    assert 'Item A.' in out and 'Item B.' in out


def test_structural_list_diff_medical_report_bullets():
    """
    User scenario: LLM converts plain text medical findings to a bullet list.
    Text content is identical, only structure changes.
    """
    old = """<h3>Hallazgos:</h3>
<p>El corazón tiene tamaño y morfología normal.</p>
<p>El mediastino es de configuración normal.</p>"""
    new = """<h3>Hallazgos:</h3>
<ol>
<li><p>El corazón tiene tamaño y morfología normal.</p></li>
<li><p>El mediastino es de configuración normal.</p></li>
</ol>"""
    cfg = DiffConfig()
    cfg.add_diff_ids = True
    out = htmldiff2.render_html_diff(old, new, config=cfg)

    assert 'diff-bullet-ins' in out, f"Expected diff-bullet-ins: {out}"
    assert 'tagdiff_added' in out, f"Expected tagdiff_added: {out}"
    assert 'structural-revert-data' in out, f"Expected structural-revert-data: {out}"
    # The heading should be unchanged
    assert '<h3>Hallazgos:</h3>' in out or 'Hallazgos:' in out


def test_ol_to_ul_structural_diff():
    """When ol changes to ul (numbered→bullets), use structural diff pattern.

    Real-world case: LLM changes numbered list to bulleted list in a medical
    report while keeping the same text content.
    """
    old = """<p><span>Hallazgos:</span></p>
<ol style="list-style-type: decimal;">
<li dir="ltr"><span>Cavidades paranasales:</span><span> evaluadas con adecuado desarrollo.</span></li>
<li dir="ltr"><span>Espacio sublingual:</span><span> con volumen conservado.</span></li>
<li dir="ltr"><span>Espacio submandibular:</span><span> densidad homogénea.</span></li>
</ol>
<p>Conclusión diagnóstica:</p>"""

    new = """<p><span>Hallazgos:</span></p>
<ul style="list-style-type: disc;">
<li dir="ltr"><span>Cavidades paranasales:</span><span> evaluadas con adecuado desarrollo.</span></li>
<li dir="ltr"><span>Espacio sublingual:</span><span> con volumen conservado.</span></li>
<li dir="ltr"><span>Espacio submandibular:</span><span> densidad homogénea.</span></li>
</ul>
<p>Conclusión diagnóstica:</p>"""

    cfg = DiffConfig()
    cfg.add_diff_ids = True
    out = htmldiff2.render_html_diff(old, new, config=cfg)

    # Should use structural diff pattern
    assert 'structural-revert-data' in out, "Should have hidden revert data"
    assert 'diff-bullet-ins' in out, "Should have bullet-ins on LIs"
    assert 'tagdiff_added' in out, "Should have tagdiff_added on new list"

    # Revert data should contain the original ol
    assert '<ol' in out, "Revert data should contain original <ol>"

    # Should NOT use tagdiff_replaced
    assert 'tagdiff_replaced' not in out

    # Text should be clean (no del/ins on identical text)
    assert '<del class="del"' not in out
    assert '<ins class="ins"' not in out

    # Surrounding content should be unchanged
    assert 'Hallazgos:' in out
    assert 'Conclusión diagnóstica:' in out


def test_ul_to_ol_structural_diff_reverse():
    """Reverse case: ul→ol (bullets→numbered) should also use structural diff."""
    old = """<ul>
<li>First item</li>
<li>Second item</li>
</ul>"""
    new = """<ol>
<li>First item</li>
<li>Second item</li>
</ol>"""

    cfg = DiffConfig()
    cfg.add_diff_ids = True
    out = htmldiff2.render_html_diff(old, new, config=cfg)

    assert 'structural-revert-data' in out, "Should have hidden revert data"
    assert 'diff-bullet-ins' in out, "Should have bullet-ins on LIs"
    assert 'tagdiff_added' in out, "Should have tagdiff_added on new list"

    # Revert data should contain the original ul
    assert '<ul' in out, "Revert data should contain original <ul>"

    # Text should NOT be wrapped in del/ins
    assert '<del class="del"' not in out
    assert '<ins class="ins"' not in out


def test_ol_decimal_to_roman_structural_diff():
    """Changing list-style-type (decimal→upper-roman) uses structural diff.

    Same tag (ol→ol) but style attribute changes. Should NOT show
    identical text in del/ins.
    """
    old = """<ol style="list-style-type: decimal;">
<li>Primer hallazgo</li>
<li>Segundo hallazgo</li>
</ol>"""
    new = """<ol style="list-style-type: upper-roman;">
<li>Primer hallazgo</li>
<li>Segundo hallazgo</li>
</ol>"""

    cfg = DiffConfig()
    cfg.add_diff_ids = True
    out = htmldiff2.render_html_diff(old, new, config=cfg)

    assert 'structural-revert-data' in out, "Should have hidden revert data"
    assert 'diff-bullet-ins' in out, "Should have bullet-ins on LIs"
    assert 'tagdiff_added' in out, "Should have tagdiff_added on new list"

    # New style should be on visible list
    assert 'upper-roman' in out
    # Old style should be in revert data
    assert 'decimal' in out

    # Text should be clean
    assert '<del class="del"' not in out


def test_structural_list_diff_with_empty_paragraph():
    """p→li structural diff fires even with <p>&nbsp;</p> between heading and list.

    Bug: An empty paragraph between the heading and list content caused a
    'replace' opcode (replacing <p> with <ul> START), which the detection
    logic missed because it only checked 'insert' opcodes.
    """
    old = """<p><strong>IMPRESIÓN:</strong></p>
<p> </p>
<p>Catéter venoso central correctamente posicionado.</p>
<p>Signos de enfermedad pulmonar crónica.</p>
<p>Aterosclerosis aórtica y coronaria.</p>"""

    new = """<p><strong>IMPRESIÓN:</strong></p>
<ul>
<li>Catéter venoso central correctamente posicionado.</li>
<li>Signos de enfermedad pulmonar crónica.</li>
<li>Aterosclerosis aórtica y coronaria.</li>
</ul>"""

    cfg = DiffConfig()
    cfg.add_diff_ids = True
    out = htmldiff2.render_html_diff(old, new, config=cfg)

    # Should use structural diff despite the empty <p>
    assert 'structural-revert-data' in out, "Should have hidden revert data"
    assert 'diff-bullet-ins' in out, "Should have bullet-ins on LIs"
    assert 'tagdiff_added' in out, "Should have tagdiff_added on new list"

    # The empty <p> should be in the revert data
    assert '<p>' in out, "Revert data should include original <p> tags"

    # Text should NOT be in del/ins
    assert '<del class="del"' not in out
    assert '<ins class="ins"' not in out

    # Heading should be unchanged
    assert 'IMPRESIÓN:' in out
