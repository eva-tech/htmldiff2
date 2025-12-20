# htmldiff2

[![Tests](https://github.com/edsu/htmldiff2/actions/workflows/test.yml/badge.svg)](https://github.com/edsu/htmldiff2/actions/workflows/test.yml)

htmldiff2 is a library that uses [difflib], [genshi] and [html5lib] to diff
arbitrary fragments of HTML inline. htmldiff2 is a friendly fork of Armin
Ronacher's [htmldiff](https://github.com/mitsuhiko/htmldiff) which needed to be
upgraded for the [diffengine](https://github.com/docnow/diffengine) project. See
[this issue](https://github.com/mitsuhiko/htmldiff/issues/7) for context.

```python
>>> from htmldiff2 import render_html_diff
>>> render_html_diff('Foo <b>bar</b> baz', 'Foo <i>bar</i> baz')
u'<div class="diff">Foo <i class="tagdiff_replaced">bar</i> baz</div>'
>>> render_html_diff('Foo bar baz', 'Foo baz')
u'<div class="diff">Foo <del>bar</del> baz</div>'
>>> render_html_diff('Foo baz', 'Foo blah baz')
u'<div class="diff">Foo <ins>blah</ins> baz</div>'
```

## EdenAI adaptations (this repo)

This repository contains a refactor plus a set of behavioral improvements made to better support **EdenAI** use-cases (medical report-like HTML, lots of inline styling, tables, and whitespace-sensitive rendering).

Key goals are: **visual correctness**, **stable `<del>` then `<ins>` ordering**, and **avoiding noisy diffs** when only presentation changes.

- **Visible whitespace in diffs**: preserve and surface meaningful spaces (e.g., leading/trailing/multiple spaces) so deletions/insertions are actually visible.
- **Style / formatting changes are first-class diffs**: changes like bold/italic/underline/font-size/color are represented as deletions + insertions (keeping the original styling in the deletion when possible).
- **Consistent ordering inside text blocks**: ensure deletes render before inserts (prevents “insert inside delete” visual bugs).
- **Line break markers**: when `<br>` changes, the diff can render a visible marker (`¶`) so empty lines / double breaks are not invisible.
- **Table-aware visual-only changes**: inside `<td>/<th>`, when the text is identical but wrappers/attributes change, we avoid duplicating the cell text and instead mark the node as `class="tagdiff_replaced"` with `data-old-*` attributes for visual highlighting.
- **Void tags tracked when configured**: additions/removals of void tags (e.g. `<img>`) can be explicitly wrapped as `<ins>/<del>` to make non-textual changes visible.
- **Refactor into a package**: the original single-file entrypoint `htmldiff2.py` re-exports the public API, while implementation lives under `htmldiff2/` (e.g. `htmldiff2/differ.py`, `htmldiff2/atomization.py`, `htmldiff2/normalization.py`, `htmldiff2/config.py`).

Validation:

- **Python regression tests**: `python test.py`

## Develop

```
python -mvenv .venv
source .venv/bin/activate
python -m pip install -e .
python test.py
```

## Publish

```
python -m pip install setuptools build twine
python -m build
python -m twine upload dist/*
```

[genshi]: https://genshi.edgewall.org/
[html5lib]: https://github.com/html5lib/html5lib-python
[difflib]: https://docs.python.org/3/library/difflib.html
