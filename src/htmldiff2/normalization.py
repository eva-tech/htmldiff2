# -*- coding: utf-8 -*-
"""
Funciones de normalización de opcodes para mejorar la calidad del diff.
"""
from genshi.core import START, END, TEXT
from .utils import qname_localname, extract_text_from_events, collapse_ws, structure_signature
from .config import INLINE_FORMATTING_TAGS, BLOCK_WRAPPER_TAGS


def normalize_opcodes_for_delete_first(opcodes):
    """
    Convert adjacent insert/delete pairs into a single replace, so output order
    becomes deterministic (del then ins).
    """
    out = []
    i = 0
    while i < len(opcodes):
        tag, i1, i2, j1, j2 = opcodes[i]
        if i + 1 < len(opcodes):
            tag2, i1b, i2b, j1b, j2b = opcodes[i + 1]
            # insert then delete at the same anchor -> replace
            if tag == 'insert' and tag2 == 'delete' and i1 == i2 == i1b == i2b and j1b == j2b == j1:
                out.append(('replace', i1b, i2b, j1, j2))
                i += 2
                continue
            # delete then insert at the same anchor -> replace (already ok, but unify)
            if tag == 'delete' and tag2 == 'insert' and j1 == j2 == j1b == j2b and i1b == i2b == i1:
                out.append(('replace', i1, i2, j1b, j2b))
                i += 2
                continue
        out.append(opcodes[i])
        i += 1
    return out


def normalize_inline_wrapper_opcodes(opcodes, old_events, new_events):
    """
    Detect patterns where an inline wrapper is removed/added around unchanged
    text, which SequenceMatcher often represents as:
      delete(START wrapper), equal(TEXT), delete(END wrapper)
    or the inverse with insert.

    We rewrite these into a single replace so downstream logic can emit a
    stable Delete -> Insert representation.
    """
    def is_inline_wrapper_tag(tag):
        return qname_localname(tag) in INLINE_FORMATTING_TAGS

    out = []
    i = 0
    while i < len(opcodes):
        if i + 2 < len(opcodes):
            t1, a1, a2, b1, b2 = opcodes[i]
            t2, c1, c2, d1, d2 = opcodes[i + 1]
            t3, e1, e2, f1, f2 = opcodes[i + 2]

            # Wrapper removed: delete START, equal TEXT, delete END
            if t1 == 'delete' and t2 == 'equal' and t3 == 'delete' and (a2 - a1) == 1 and (c2 - c1) == 1 and (e2 - e1) == 1:
                ev_start = old_events[a1]
                ev_mid_old = old_events[c1]
                ev_mid_new = new_events[d1]
                ev_end = old_events[e1]
                if ev_start[0] == START and ev_end[0] == END and ev_mid_old[0] == TEXT and ev_mid_new[0] == TEXT:
                    start_tag = ev_start[1][0]
                    end_tag = ev_end[1]
                    if is_inline_wrapper_tag(start_tag) and qname_localname(start_tag) == qname_localname(end_tag):
                        out.append(('replace', a1, e2, d1, d2))
                        i += 3
                        continue

            # Wrapper added: insert START, equal TEXT, insert END
            if t1 == 'insert' and t2 == 'equal' and t3 == 'insert' and (b2 - b1) == 1 and (c2 - c1) == 1 and (f2 - f1) == 1:
                ev_start = new_events[b1]
                ev_mid_old = old_events[c1]
                ev_mid_new = new_events[d1]
                ev_end = new_events[f1]
                if ev_start[0] == START and ev_end[0] == END and ev_mid_old[0] == TEXT and ev_mid_new[0] == TEXT:
                    start_tag = ev_start[1][0]
                    end_tag = ev_end[1]
                    if is_inline_wrapper_tag(start_tag) and qname_localname(start_tag) == qname_localname(end_tag):
                        out.append(('replace', c1, c2, b1, f2))
                        i += 3
                        continue

        out.append(opcodes[i])
        i += 1
    return out


def normalize_inline_wrapper_tag_change_opcodes(opcodes, old_events, new_events, config):
    """
    Normalize patterns like:
      replace(START <span> -> <strong>), equal(TEXT), replace(END <span> -> <strong>)
    into a single replace over the full wrapper subtree so we can render it as
    a visible visual diff (del->ins).
    """
    allowed = set(getattr(config, 'visual_container_tags', ()))
    # Focus on wrappers where tag-change should be rendered as a visible diff:
    # - inline formatting wrappers
    # - title/paragraph wrappers
    wrappers = set(['span', 'strong', 'b', 'em', 'i', 'u', 'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'])
    wrappers &= allowed | wrappers

    out = []
    i = 0
    while i < len(opcodes):
        if i + 2 < len(opcodes):
            t1, a1, a2, b1, b2 = opcodes[i]
            t2, c1, c2, d1, d2 = opcodes[i + 1]
            t3, e1, e2, f1, f2 = opcodes[i + 2]
            if t1 == 'replace' and t2 == 'equal' and t3 == 'replace' and (a2 - a1) == 1 and (b2 - b1) == 1 and (e2 - e1) == 1 and (f2 - f1) == 1:
                ev_start_old = old_events[a1]
                ev_start_new = new_events[b1]
                ev_end_old = old_events[e1]
                ev_end_new = new_events[f1]
                if ev_start_old[0] == START and ev_start_new[0] == START and ev_end_old[0] == END and ev_end_new[0] == END:
                    old_tag = ev_start_old[1][0]
                    new_tag = ev_start_new[1][0]
                    old_l = qname_localname(old_tag)
                    new_l = qname_localname(new_tag)
                    if old_l in wrappers and new_l in wrappers and qname_localname(ev_end_old[1]) == old_l and qname_localname(ev_end_new[1]) == new_l:
                        # Fold START + middle + END into one replace span
                        out.append(('replace', a1, e2, b1, f2))
                        i += 3
                        continue
        out.append(opcodes[i])
        i += 1
    return out


def should_force_visual_replace(old_events, new_events, config):
    """
    True when old/new represent the same text but differ by tag or "visual" attrs
    (style/class/id/etc.), and we want a visible diff even if the textual content
    is unchanged.

    This is evaluated at the event-differ level where SequenceMatcher often only
    reports START-tag changes; forcing a full replace lets us render del->ins.
    """
    from .utils import collapse_ws
    
    if not old_events or not new_events:
        return False
    if old_events[0][0] != START or old_events[-1][0] != END:
        return False
    if new_events[0][0] != START or new_events[-1][0] != END:
        return False

    old_tag, old_attrs = old_events[0][1]
    new_tag, new_attrs = new_events[0][1]
    old_lname = qname_localname(old_tag)
    new_lname = qname_localname(new_tag)

    allowed = set(getattr(config, 'visual_container_tags', ()))
    if old_lname not in allowed and new_lname not in allowed:
        return False

    old_txt = extract_text_from_events(old_events)
    new_txt = extract_text_from_events(new_events)
    if not old_txt or not new_txt:
        return False
    if collapse_ws(old_txt) != collapse_ws(new_txt):
        return False

    if old_lname != new_lname:
        return True

    # If the visible text is the same but the *inline formatting* structure differs
    # (e.g. <span>... -> <strong>... or <strong> removed), we sometimes need to
    # force a replace so we can render del->ins deterministically.
    #
    # Important: do NOT escalate inner wrapper changes to a full replace of a
    # block container like <p>/<h1-6>, otherwise unchanged trailing text inside
    # the paragraph gets highlighted as deleted/inserted (EdenAI report case).
    if structure_signature(old_events, config) != structure_signature(new_events, config):
        if old_lname in INLINE_FORMATTING_TAGS or new_lname in INLINE_FORMATTING_TAGS:
            return True
        if old_lname in BLOCK_WRAPPER_TAGS or new_lname in BLOCK_WRAPPER_TAGS:
            return False

    keys = list(getattr(config, 'track_attrs', ('style', 'class', 'src', 'href')))
    if 'id' not in keys:
        keys.append('id')
    for k in keys:
        if old_attrs.get(k) != new_attrs.get(k):
            return True
    return False



