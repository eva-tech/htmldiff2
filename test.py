import doctest
import htmldiff2
from htmldiff2 import DiffConfig
import re

doctest.testmod(htmldiff2, verbose=True)

# Additional regression checks (basic asserts)
def _assert_contains(haystack, needle):
    assert needle in haystack, "Expected %r to contain %r" % (haystack, needle)


def run_regressions():
    # Delete before insert ordering
    out = htmldiff2.render_html_diff('Foo baz', 'Foo blah baz')
    _assert_contains(out, '<ins')

    # Bold -> normal should not invert order; should show a deletion (old formatting) before insertion.
    out = htmldiff2.render_html_diff('Foo <strong>bar</strong> baz', 'Foo bar baz')
    _assert_contains(out, '<del')
    _assert_contains(out, '<ins')

    # Style-only change should be marked as a diff even if text is identical
    out = htmldiff2.render_html_diff(
        'Foo <span style="font-size:14px">bar</span>',
        'Foo <span style="font-size:20px">bar</span>',
    )
    _assert_contains(out, '<del')
    _assert_contains(out, '<ins')
    _assert_contains(out, 'font-size:14px')
    _assert_contains(out, 'font-size:20px')

    # Linebreak visibility: inserted <br> should show marker
    out = htmldiff2.render_html_diff('Foo', 'Foo<br>Bar')
    _assert_contains(out, u'\u00b6')

    # Double linebreak visibility: inserted/removed <br><br> should show markers,
    # including the empty line created by the double break.
    out = htmldiff2.render_html_diff('FooBar', 'Foo<br><br>Bar')
    assert out.count(u'\u00b6') >= 2
    out = htmldiff2.render_html_diff('Foo<br><br>Bar', 'FooBar')
    assert out.count(u'\u00b6') >= 2

    # Moving linebreaks around blocks should NOT mark unchanged paragraphs as changed.
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
    assert out.count("Severe headache.") == 1 and "<del>Severe headache." not in out and "<ins>Severe headache." not in out
    assert out.count("None available.") == 1 and "<del>None available." not in out and "<ins>None available." not in out
    assert out.count("Dr. Strange") == 1 and "<del>Dr. Strange" not in out and "<ins>Dr. Strange" not in out
    # 2 inserted breaks, 3 deleted breaks
    assert out.count(u"\u00b6") >= 5

    # Lists: change inside one <li> should not delete/reinsert the whole list
    old = '<ul><li>Uno</li><li>Dos</li><li>Tres</li></ul>'
    new = '<ul><li>Uno</li><li>Dos cambiado</li><li>Tres</li></ul>'
    out = htmldiff2.render_html_diff(old, new)
    # Either full replace (del+ins) or a minimal insert inside the li is acceptable,
    # but it must be localized to the modified item (don't nuke untouched items).
    assert '<ins' in out and 'cambiado' in out
    assert not re.search(r"<del[^>]*>Uno</del>", out), out
    assert not re.search(r"<del[^>]*>Tres</del>", out), out

    # Tables: change inside one <td> should be localized
    old = '<table><tr><td>A</td><td>B</td></tr></table>'
    new = '<table><tr><td>A</td><td>C</td></tr></table>'
    out = htmldiff2.render_html_diff(old, new)
    assert re.search(r"<del[^>]*>B</del>", out), out
    assert re.search(r"<ins[^>]*>C</ins>", out), out
    assert not re.search(r"<del[^>]*>A</del>", out), out

    # Tables: removing an intermediate column must delete the correct cells,
    # even when there are duplicate values that can confuse alignment.
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
    # The deleted column header
    assert re.search(r'<th[^>]*\bclass="[^"]*\btagdiff_deleted\b[^"]*"[^>]*>.*?<del[^>]*>Diámetro Previo \(mm\)</del>.*?</th>', out, flags=re.S), out
    # The deleted column data (both rows)
    assert out.count('class="tagdiff_deleted"') >= 3, out
    assert re.search(r'<td[^>]*\bclass="[^"]*\btagdiff_deleted\b[^"]*"[^>]*>.*?<del[^>]*>10</del>.*?</td>', out, flags=re.S), out
    # Ensure we did NOT accidentally delete the "Cambio (%)" column/header
    assert not re.search(r'<th[^>]*\bclass="[^"]*\btagdiff_deleted\b[^"]*"[^>]*>\s*Cambio \(%\)\s*</th>', out), out
    assert not re.search(r'>\s*\+10%\s*<', out.split('tagdiff_deleted')[0]), out  # sanity: +10% stays outside deleted cells

    # Whitespace-only change inside a single TEXT node should be visible
    out = htmldiff2.render_html_diff("<p>Texto con   espacios</p>", "<p>Texto con espacios</p>")
    _assert_contains(out, "<del")
    # Only 2 spaces were removed (3 -> 1), so this should NOT create an insertion.
    assert "<ins" not in out

    # Void elements: adding/removing <img> should be visible as <ins>/<del>
    out = htmldiff2.render_html_diff("<p>Hola</p>", "<p>Hola <img src='a.jpg'/></p>")
    _assert_contains(out, '<ins')
    _assert_contains(out, '<img')
    out = htmldiff2.render_html_diff("<p>Hola <img src='a.jpg'/></p>", "<p>Hola</p>")
    _assert_contains(out, '<del')
    _assert_contains(out, '<img')

    # EdenAI: inline wrapper tag change inside a paragraph should NOT mark the
    # entire trailing sentence as deleted/inserted.
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

    # Diff IDs (opt-in): paired del/ins must share the same group id.
    cfg = DiffConfig()
    cfg.add_diff_ids = True
    cfg.diff_id_attr = "data-diff-id"
    out = htmldiff2.render_html_diff('Foo <b>bar</b> baz', 'Foo <i>bar</i> baz', config=cfg)
    m_del = re.search(r'<del[^>]*\bdata-diff-id="([^"]+)"', out)
    m_ins = re.search(r'<ins[^>]*\bdata-diff-id="([^"]+)"', out)
    assert m_del and m_ins, out
    assert m_del.group(1) == m_ins.group(1), out

    # Insert-only should still have a diff id
    out = htmldiff2.render_html_diff('Foo', 'Foo bar', config=cfg)
    assert re.search(r'<ins[^>]*\bdata-diff-id="[^"]+"', out), out

    # Delete-only should still have a diff id
    out = htmldiff2.render_html_diff('Foo bar', 'Foo', config=cfg)
    assert re.search(r'<del[^>]*\bdata-diff-id="[^"]+"', out), out


if __name__ == '__main__':
    run_regressions()