from __future__ import annotations

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


def test_default_config_tracks_refs_and_td_th_visual():
    cfg = DiffConfig()
    assert "ref" in cfg.track_attrs
    assert "data-ref" in cfg.track_attrs
    assert "td" in cfg.visual_container_tags
    assert "th" in cfg.visual_container_tags

