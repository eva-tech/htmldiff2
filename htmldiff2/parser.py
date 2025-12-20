# -*- coding: utf-8 -*-
"""
Funciones de parsing HTML para htmldiff2.
"""
from genshi.input import ET
import html5lib


def parse_html(html, wrapper_element='div', wrapper_class='diff'):
    """Parse an HTML fragment into a Genshi stream."""
    builder = html5lib.getTreeBuilder('etree')
    parser = html5lib.HTMLParser(tree=builder)
    tree = parser.parseFragment(html)
    tree.tag = wrapper_element
    if wrapper_class is not None:
        tree.set('class', wrapper_class)
    return ET(tree)


def longzip(a, b):
    """Like `izip` but yields `None` for missing items."""
    aiter = iter(a)
    biter = iter(b)
    try:
        for item1 in aiter:
            yield item1, next(biter)
    except StopIteration:
        for item1 in aiter:
            yield item1, None
    else:
        for item2 in biter:
            yield None, item2


