import doctest
import htmldiff2

doctest.testmod(htmldiff2, verbose=True)

# Additional regression checks (basic asserts)
def _assert_contains(haystack, needle):
    assert needle in haystack, "Expected %r to contain %r" % (haystack, needle)


def run_regressions():
    # Delete before insert ordering
    out = htmldiff2.render_html_diff('Foo baz', 'Foo blah baz')
    _assert_contains(out, '<ins>')

    # Bold -> normal should not invert order; should show a deletion (old formatting) before insertion.
    out = htmldiff2.render_html_diff('Foo <strong>bar</strong> baz', 'Foo bar baz')
    _assert_contains(out, '<del>')
    _assert_contains(out, '<ins>')

    # Style-only change should be marked as a diff even if text is identical
    out = htmldiff2.render_html_diff(
        'Foo <span style="font-size:14px">bar</span>',
        'Foo <span style="font-size:20px">bar</span>',
    )
    _assert_contains(out, '<del>')
    _assert_contains(out, '<ins>')
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
    assert '<ins>' in out and 'cambiado' in out
    assert '<del>Uno</del>' not in out
    assert '<del>Tres</del>' not in out

    # Tables: change inside one <td> should be localized
    old = '<table><tr><td>A</td><td>B</td></tr></table>'
    new = '<table><tr><td>A</td><td>C</td></tr></table>'
    out = htmldiff2.render_html_diff(old, new)
    _assert_contains(out, '<del>B</del>')
    _assert_contains(out, '<ins>C</ins>')
    assert '<del>A</del>' not in out

    # Whitespace-only change inside a single TEXT node should be visible
    out = htmldiff2.render_html_diff("<p>Texto con   espacios</p>", "<p>Texto con espacios</p>")
    _assert_contains(out, "<del>")
    # Only 2 spaces were removed (3 -> 1), so this should NOT create an insertion.
    assert "<ins>" not in out

    # Void elements: adding/removing <img> should be visible as <ins>/<del>
    out = htmldiff2.render_html_diff("<p>Hola</p>", "<p>Hola <img src='a.jpg'/></p>")
    _assert_contains(out, '<ins>')
    _assert_contains(out, '<img')
    out = htmldiff2.render_html_diff("<p>Hola <img src='a.jpg'/></p>", "<p>Hola</p>")
    _assert_contains(out, '<del>')
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


if __name__ == '__main__':
    run_regressions()