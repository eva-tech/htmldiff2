# -*- coding: utf-8 -*-
"""
Funciones de atomización de eventos para mejor alineación en diffs.
"""
from genshi.core import START, END, TEXT
import re
from .config import STRUCTURAL_TAGS
from .utils import (
    qname_localname, extract_text_from_events, collapse_ws,
    attrs_signature, structure_signature, is_diff_wrapper
)


_list_marker_re = re.compile(r'^[\-\*\•\+]+\s+')


def _first_n_cell_texts_from_tr_events(tr_events, n=2):
    """
    Extract visible text from the first N direct child <td>/<th> cells of a <tr>.

    This helps row alignment when columns are added/removed: the "row identity"
    usually lives in the first columns (e.g. Hallazgo / Descripción), while later
    columns may be the ones being removed (e.g. Localización).
    """
    texts = []
    cell_idx = -1
    in_cell = False
    cell_depth = 0
    buf = []

    for et, d, _p in tr_events:
        if et == START:
            tag, _attrs = d
            lname = qname_localname(tag)
            if lname in ("td", "th"):
                if not in_cell:
                    # entering a direct cell
                    cell_idx += 1
                    in_cell = True
                    cell_depth = 1
                    buf = []
                else:
                    # nested cell tag (unlikely) - track depth
                    cell_depth += 1
        elif et == END:
            lname = qname_localname(d)
            if in_cell and lname in ("td", "th"):
                cell_depth -= 1
                if cell_depth == 0:
                    # leaving the direct cell
                    if cell_idx < n:
                        texts.append(collapse_ws("".join(buf)))
                        if len(texts) >= n:
                            break
                    in_cell = False
        elif et == TEXT:
            if in_cell and cell_idx < n:
                buf.append(d or "")

    # Ensure length n (pad with empty) for stable keys
    while len(texts) < n:
        texts.append("")
    return tuple(texts[:n])


def build_block_tags_set(config):
    """Construye el conjunto de tags que deben ser atomizados como bloques."""
    block_tags = set()
    if getattr(config, 'enable_list_atomization', True):
        block_tags |= set(['li'])
    if getattr(config, 'enable_table_atomization', True):
        # Atomize rows and cells so the outer matcher doesn't drift across rows.
        # NOTE: we still need row-aware logic when diffing <tr> blocks (see differ.py),
        # because event-level diff inside a row can misalign cells when a column is
        # removed/inserted and there are duplicate values.
        # Also atomize <table> so table start/end cannot be split across opcodes
        # when the LLM restyles the table tag (border/padding/etc).
        block_tags |= set(['td', 'th', 'tr', 'table'])
    if getattr(config, 'enable_inline_wrapper_atomization', True):
        # Helps treat formatting wrapper removal/addition as a cohesive unit.
        block_tags |= set(['b', 'strong', 'i', 'em'])
    # Visual tags to atomize for alignment (avoid large container divs).
    visual_tags = set(getattr(config, 'visual_atomize_tags', ()))
    block_tags |= visual_tags
    return block_tags, visual_tags


def find_block_end(events, start_idx, tag_name):
    """Encuentra el índice del evento END que cierra el bloque que comienza en start_idx."""
    depth = 1
    n = len(events)
    j = start_idx + 1
    while j < n and depth:
        t2, d2, _p2 = events[j]
        if t2 == START and qname_localname(d2[0]) == tag_name:
            depth += 1
        elif t2 == END and qname_localname(d2) == tag_name:
            depth -= 1
        j += 1
    return j


def has_structural_children(block_events):
    """Verifica si un bloque div contiene hijos estructurales."""
    for t2, d2, _p2 in block_events[1:-1]:
        if t2 == START and qname_localname(d2[0]) in STRUCTURAL_TAGS:
            return True
    return False


def create_block_atom_key(lname, block_events, attrs, config, visual_tags):
    """Crea la clave de atomización para un bloque según su tipo."""
    block_text = collapse_ws(extract_text_from_events(block_events))
    if lname in ('td', 'th'):
        # Cells: include attrs + formatting structure so visual-only changes
        # (e.g. strong style) are detected, but ignore indentation whitespace.
        return (lname, block_text, attrs_signature(attrs, config), 
                structure_signature(block_events, config))
    elif lname == 'tr':
        # Rows: key by the first columns (typically the stable identity of the row),
        # so column deletions/insertions in later columns still match the same row.
        c0, c1 = _first_n_cell_texts_from_tr_events(block_events, n=2)
        if c0 or c1:
            return (lname, c0, c1)
        return (lname, block_text)
    elif lname in ('li', 'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
        # Normalize structural blocks to just their text content for the 
        # initial alignment. This allows a paragraph to match a list item
        # if the text is identical, while the tag change is handled later.
        # Include a 'block' marker to distinguish from raw text atoms.
        # Also strip common list markers (-, *, •) to allow "- Item" (p) to match "Item" (li).
        normalized_text = _list_marker_re.sub('', block_text)
        return ('block', normalized_text)
    elif lname in ('ul', 'ol'):
        # Force these containers to always be 'equal' in the outer matcher
        # so we always run an inner diff on their children.
        return (lname,)
    elif lname in visual_tags:
        return (lname, block_text, attrs_signature(attrs, config), 
                structure_signature(block_events, config))
    else:
        return (lname, block_text)


def atomize_events(events, config):
    """
    Convert a flat list of Genshi events to 'atoms' that SequenceMatcher can
    align better (esp. <li> and table cells), while preserving original events
    for rendering.
    """
    from .config import _token_split_re
    
    atoms = []
    i = 0
    n = len(events)
    block_tags, visual_tags = build_block_tags_set(config)

    while i < n:
        etype, data, pos = events[i]

        # Treat <br> as a single atomic unit (START+END) so moving breaks doesn't
        # disturb alignment of neighboring blocks and doesn't cause giant replaces.
        if etype == START:
            tag, attrs = data
            lname0 = qname_localname(tag)
            if lname0 == 'br' and i + 1 < n and events[i + 1][0] == END and qname_localname(events[i + 1][1]) == 'br':
                atoms.append({'kind': 'br', 'key': ('br',), 'events': [events[i], events[i + 1]], 'pos': pos})
                i += 2
                continue

        # Group structural blocks (<li>, <tr>, <td>/<th>) as atomic units
        if etype == START:
            tag, attrs = data
            lname = qname_localname(tag)
            # Don't treat the artificial wrapper (<div class="diff">) as a block atom,
            # otherwise attribute-only changes inside can be swallowed as "equal".
            wrapper = is_diff_wrapper(tag, attrs)

            if lname in block_tags and not wrapper:
                j = find_block_end(events, i, lname)
                block_events = events[i:j]

                # Heuristic: don't atomize large container divs that contain structural blocks
                # (prevents swallowing report-content-like containers).
                has_structural_child = False
                if lname == 'div':
                    has_structural_child = has_structural_children(block_events)
                    if not has_structural_child:
                        # Atomize this div as a visual block
                        key = (lname, extract_text_from_events(block_events), 
                               attrs_signature(attrs, config), 
                               structure_signature(block_events, config)) if lname in visual_tags else \
                              (lname, extract_text_from_events(block_events))
                        atoms.append({'kind': 'block', 'tag': lname, 'key': key, 
                                    'events': block_events, 'pos': pos})
                        i = j
                        continue

                # For visual containers, include attribute signature so style/class/id
                # changes produce a 'replace' opcode even if text stays the same.
                if not (lname == 'div' and has_structural_child):
                    key = create_block_atom_key(lname, block_events, attrs, config, visual_tags)
                    atoms.append({'kind': 'block', 'tag': lname, 'key': key, 
                                'events': block_events, 'pos': pos})
                    i = j
                    continue

        # Tokenize text events for better alignment granularity
        if etype == TEXT and getattr(config, 'tokenize_text', True) and data:
            parts = [p for p in getattr(config, 'tokenize_regex', _token_split_re).split(data) if p != u'']
            for p in parts:
                atoms.append({'kind': 'text', 'key': ('t', p), 'events': [(TEXT, p, pos)], 'pos': pos})
            i += 1
            continue

        # Default: single-event atom
        atoms.append({'kind': 'event', 'key': ('e', etype, data), 'events': [events[i]], 'pos': pos})
        i += 1

    return atoms





