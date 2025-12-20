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

# Importar API pública para mantener compatibilidad
from .parser import parse_html, longzip
from .differ import StreamDiffer, diff_genshi_stream, render_html_diff
from .config import DiffConfig

# Exportar SOLO la API pública (mantener compatibilidad con código existente)
# No exportar módulos internos para evitar contaminar el namespace
__all__ = [
    'render_html_diff',
    'parse_html',
    'diff_genshi_stream',
    'DiffConfig',
    'StreamDiffer',
    'longzip',
]

# Asegurar que los módulos internos no sean accesibles directamente
# (aunque Python los expone por defecto, esto documenta la intención)


