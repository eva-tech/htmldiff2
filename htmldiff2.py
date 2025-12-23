# -*- coding: utf-8 -*-
"""
    htmldiff2
    ~~~~~~~~

    Diffs HTML fragments.  Nice to show what changed between two revisions
    of a document for an arbitrary user.  Examples:

    >>> from htmldiff2 import render_html_diff

    >>> print(render_html_diff('Foo <b>bar</b> baz', 'Foo <i>bar</i> baz'))
    <div class="diff">Foo <del><b>bar</b></del><ins><i>bar</i></ins> baz</div>

    >>> print(render_html_diff('Foo bar baz', 'Foo baz'))
    <div class="diff">Foo <del>bar\xa0</del>baz</div>

    >>> print(render_html_diff('Foo baz', 'Foo blah baz'))
    <div class="diff">Foo <ins>blah\xa0</ins>baz</div>

    >>> print(render_html_diff('<img src="pic0.jpg"/>', '<img src="pic1.jpg"/>'))
    <div class="diff"><img src="pic1.jpg" class="tagdiff_replaced" data-old-src="pic0.jpg"></div>

    :copyright: (c) 2011 by Armin Ronacher, see AUTHORS for more details.
    :license: BSD, see LICENSE for more details.
"""
from __future__ import with_statement

# Layout shim: tras mover el paquete a `src/htmldiff2/`, este archivo (módulo)
# chocaría con el paquete del mismo nombre. Para mantener compatibilidad, lo
# convertimos en "paquete" declarando __path__ apuntando al directorio real.
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_PKG_DIR = _ROOT / "src" / "htmldiff2"
if not _PKG_DIR.exists():
    # Fallback (layout antiguo): htmldiff2/ en la raíz
    _PKG_DIR = _ROOT / "htmldiff2"

# Si existe, hacemos que este módulo sea tratado como paquete para habilitar
# `from htmldiff2.differ import ...`
if _PKG_DIR.exists():
    __path__ = [str(_PKG_DIR)]  # type: ignore[name-defined]

# Re-exportar desde el módulo refactorizado para mantener compatibilidad hacia atrás
from htmldiff2.differ import (
    StreamDiffer,
    diff_genshi_stream,
    render_html_diff,
)
from htmldiff2.parser import parse_html, longzip
from htmldiff2.config import DiffConfig

# Mantener compatibilidad: exportar todo lo que se exportaba antes
__all__ = [
    'render_html_diff',
    'parse_html',
    'diff_genshi_stream',
    'DiffConfig',
    'StreamDiffer',
    'longzip',
]
