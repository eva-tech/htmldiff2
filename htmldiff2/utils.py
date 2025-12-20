# -*- coding: utf-8 -*-
"""
Funciones utilitarias para htmldiff2.
"""
from .config import text_type, INLINE_FORMATTING_TAGS, STRUCTURAL_TAGS
from genshi.core import TEXT, START, END


def qname_localname(qname):
    """
    QName in genshi renders like 'tag' or '{ns}tag'. Normalize to localname.
    Always coerce to text; QName can behave like a string in some environments,
    but we want a real text value (not a QName instance) to keep comparisons stable.
    """
    s = text_type(qname)
    if '}' in s:
        left, right = s.split('}', 1)
        # Handles both '{ns}tag' and 'ns}tag' (observed from html5lib+etree builder)
        if left.startswith('{') or '://' in left or left.startswith('http'):
            return right
    return s


def collapse_ws(s):
    """Colapsa espacios en blanco múltiples en un solo espacio."""
    import re
    return re.sub(r'\s+', ' ', s, flags=re.U).strip()


def strip_edge_whitespace_events(events):
    """
    Remove leading/trailing TEXT events that are whitespace-only.
    Returns (leading_ws_events, core_events, trailing_ws_events).
    """
    if not events:
        return [], [], []
    i = 0
    j = len(events)
    while i < j and events[i][0] == TEXT and (events[i][1] or u'').strip() == u'':
        i += 1
    while j > i and events[j - 1][0] == TEXT and (events[j - 1][1] or u'').strip() == u'':
        j -= 1
    return events[:i], events[i:j], events[j:]


def attrs_is_empty(attrs):
    """Verifica si los atributos están vacíos."""
    try:
        return not attrs or len(attrs) == 0
    except Exception:
        try:
            return not attrs or len(list(attrs)) == 0
        except Exception:
            return True


def extract_text_from_events(events):
    """Extrae texto de eventos y lo colapsa."""
    parts = []
    for etype, data, _pos in events:
        if etype == TEXT and data:
            parts.append(data)
    return collapse_ws(u''.join(parts)).lower()


def raw_text_from_events(events):
    """Extrae texto crudo de eventos sin procesar."""
    parts = []
    for etype, data, _pos in events:
        if etype == TEXT and data:
            parts.append(data)
    return u''.join(parts)


def concat_events(atoms):
    """Concatena eventos de múltiples átomos."""
    rv = []
    for a in atoms:
        rv.extend(a['events'])
    return rv


def longest_common_prefix_len(a, b):
    """Calcula la longitud del prefijo común más largo."""
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def longest_common_suffix_len(a, b, max_prefix=0):
    """Calcula la longitud del sufijo común más largo evitando solapamiento con el prefijo."""
    max_len = min(len(a) - max_prefix, len(b) - max_prefix)
    i = 0
    while i < max_len and a[-1 - i] == b[-1 - i]:
        i += 1
    return i


def has_visual_attrs(attrs, config):
    """Verifica si los atributos contienen propiedades visuales relevantes."""
    keys = list(getattr(config, 'track_attrs', ('style', 'class', 'src', 'href')))
    if 'id' not in keys:
        keys.append('id')
    for k in keys:
        v = attrs.get(k)
        if v:
            return True
    return False


def is_diff_wrapper(tag, attrs):
    """Verifica si un tag es el wrapper artificial de diff."""
    lname = qname_localname(tag)
    if lname == 'div':
        cls = attrs.get('class')
        return cls and 'diff' in text_type(cls).split()
    return False


def attrs_signature(attrs, config):
    """
    Produce a stable signature for attributes we consider meaningful for matching.
    """
    keys = list(getattr(config, 'track_attrs', ('style', 'class', 'src', 'href')))
    if 'id' not in keys:
        keys.append('id')
    sig = []
    for k in keys:
        v = attrs.get(k)
        if v is not None:
            sig.append((k, text_type(v)))
    return tuple(sig)


def structure_signature(events, config):
    """
    Fingerprint of inline formatting structure within a block.
    This lets us treat 'same text but different formatting' as a replace
    (so we can render it as a diff).
    """
    # IMPORTANT: do not include <br> here. Line breaks should diff as their own atoms
    # (see atomization), otherwise small layout changes can force a visual "replace"
    # of entire blocks and incorrectly mark unchanged text as deleted/inserted.
    sig = []
    for etype, data, _pos in events:
        if etype == START:
            tag, _attrs = data
            lname = qname_localname(tag)
            if lname in INLINE_FORMATTING_TAGS:
                sig.append(lname)
    return tuple(sig)


def merge_adjacent_change_tags(events, merge_tags=('ins', 'del')):
    """
    Merge adjacent change tags in a flat Genshi event stream:
      ... END ins, START ins ...  -> ... (merge into one ins) ...

    This turns:
      <ins>en</ins><ins> </ins><ins>negrita</ins>
    into:
      <ins>en negrita</ins>

    Only merges when the <ins>/<del> tags have no attributes (pre-ID stage),
    to avoid losing metadata.
    """
    from genshi.core import START, END
    out = []
    for etype, data, pos in events:
        if etype == START:
            tag, attrs = data
            lname = qname_localname(tag)
            if lname in merge_tags and attrs_is_empty(attrs):
                if out and out[-1][0] == END and qname_localname(out[-1][1]) == lname:
                    # Remove previous END and skip this START, keeping one continuous tag.
                    out.pop()
                    continue
        out.append((etype, data, pos))
    return out


