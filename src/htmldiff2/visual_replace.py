# -*- coding: utf-8 -*-
"""
Visual replacement handling logic.

This module contains functions for detecting and rendering visual-only changes
(attribute changes, wrapper toggles, etc.) that don't change the actual text content.
"""
from __future__ import with_statement

from genshi.core import QName, Attrs, START, END, TEXT

from .config import INLINE_FORMATTING_TAGS, BLOCK_WRAPPER_TAGS
from .utils import (
    qname_localname, collapse_ws, extract_text_from_events, raw_text_from_events,
    strip_edge_whitespace_events, has_visual_attrs, structure_signature,
    longest_common_prefix_len, longest_common_suffix_len
)
from .text_differ import mark_text


def try_visual_wrapper_toggle_without_dup(differ, old_events, new_events):
    """
    If one side is plain text and the other wraps the *same* text in a single
    inline wrapper with visual attrs (style/class/id), render only one copy
    and mark it as tagdiff_replaced.

    This reduces noisy table diffs where many cells get styled (highlighting),
    and avoids duplicating values inside <td>/<th>.
    """

    def parse(events):
        lws, core, tws = strip_edge_whitespace_events(events)
        if len(core) == 1 and core[0][0] == TEXT:
            return ('plain', lws, core[0], tws, None, None)
        if len(core) >= 3 and core[0][0] == START and core[-1][0] == END:
            tag, attrs = core[0][1]
            lname = qname_localname(tag)
            if lname in INLINE_FORMATTING_TAGS and qname_localname(core[-1][1]) == lname:
                inner = core[1:-1]
                if inner and all(t == TEXT for (t, _d, _p) in inner):
                    return ('wrap', lws, inner, tws, (tag, attrs), lname)
        return None

    o = parse(old_events)
    n = parse(new_events)
    if not o or not n:
        return False

    # Addition: plain -> styled wrapper
    if o[0] == 'plain' and n[0] == 'wrap':
        _o_kind, _o_lws, o_text_ev, _o_tws, _o_tagattrs, _o_lname = o
        _n_kind, n_lws, n_inner, n_tws, (n_tag, n_attrs), _n_lname = n
        if not has_visual_attrs(n_attrs, differ.config):
            return False
        if collapse_ws(o_text_ev[1]) != collapse_ws(extract_text_from_events(n_inner)):
            return False
        for ev in n_lws:
            differ.append(*ev)
        # Genshi Attrs is list-like, not dict-like
        attrs2 = Attrs(list(n_attrs))
        attrs2 = differ.inject_class(attrs2, 'tagdiff_replaced')
        attrs2 |= [(QName('data-old-tag'), 'none')]
        if getattr(differ.config, 'add_diff_ids', False):
            diff_id = differ._active_diff_id() or differ._new_diff_id()
            attrs2 = differ._set_attr(attrs2, getattr(differ.config, 'diff_id_attr', 'data-diff-id'), diff_id)
        pos = (n_inner[0][2] if n_inner else (new_events[0][2] if new_events else old_events[0][2]))
        differ.append(START, (n_tag, attrs2), pos)
        for ev in n_inner:
            differ.append(*ev)
        differ.append(END, n_tag, pos)
        for ev in n_tws:
            differ.append(*ev)
        return True

    # Removal: styled wrapper -> plain
    if o[0] == 'wrap' and n[0] == 'plain':
        _o_kind, _o_lws, o_inner, _o_tws, (_o_tag, o_attrs), o_lname = o
        _n_kind, n_lws, n_text_ev, n_tws, _n_tagattrs, _n_lname = n
        if not has_visual_attrs(o_attrs, differ.config):
            return False
        if collapse_ws(extract_text_from_events(o_inner)) != collapse_ws(n_text_ev[1]):
            return False
        for ev in n_lws:
            differ.append(*ev)
        span_tag = QName('span')
        span_attrs = Attrs()
        span_attrs |= [(QName('data-old-tag'), o_lname)]
        span_attrs = differ.inject_refattr(span_attrs, o_attrs)
        span_attrs = differ.inject_class(span_attrs, 'tagdiff_replaced')
        if getattr(differ.config, 'add_diff_ids', False):
            diff_id = differ._active_diff_id() or differ._new_diff_id()
            span_attrs = differ._set_attr(span_attrs, getattr(differ.config, 'diff_id_attr', 'data-diff-id'), diff_id)
        differ.append(START, (span_tag, span_attrs), n_text_ev[2])
        differ.append(*n_text_ev)
        differ.append(END, span_tag, n_text_ev[2])
        for ev in n_tws:
            differ.append(*ev)
        return True

    return False


def can_unwrap_wrapper(differ, old_events, new_events):
    """
    Detect cases like:
      old: <strong>TEXT</strong>
      new: TEXT
    or the inverse. If so, we force a delete then insert at that range to
    avoid inverted output and broken rendering in inline wrappers.
    """
    def is_inline_wrapper(tag):
        return qname_localname(tag) in INLINE_FORMATTING_TAGS

    def unwrap(events):
        if len(events) >= 3 and events[0][0] == START and events[-1][0] == END:
            tag0 = events[0][1][0]
            tag1 = events[-1][1]
            if tag0 == tag1 and is_inline_wrapper(tag0):
                inner = events[1:-1]
                txt = extract_text_from_events(inner)
                return qname_localname(tag0), txt
        return None, None

    old_tag, old_txt = unwrap(old_events)
    new_tag, new_txt = unwrap(new_events)
    old_plain = extract_text_from_events(old_events)
    new_plain = extract_text_from_events(new_events)

    # old wrapped -> new plain with same text
    if old_tag and (not new_tag) and old_txt and old_txt == collapse_ws(new_plain):
        return True
    # old plain -> new wrapped with same text
    if new_tag and (not old_tag) and new_txt and new_txt == collapse_ws(old_plain):
        return True
    return False


def can_visual_container_replace(differ, old_events, new_events):
    """
    Detect container/tag/attribute-only changes that should still produce a
    visible diff:
      <p style="...">Texto</p>  -> <p style="...">Texto</p>
      <div id="a">X</div>       -> <div id="b">X</div>
      <div>Texto</div>          -> <span>Texto</span>

    We only apply this to a safe allowlist of tags to avoid blowing up
    structural HTML (tables/lists).
    """
    if not old_events or not new_events:
        return False
    _lws, old_events, _tws = strip_edge_whitespace_events(old_events)
    _lws2, new_events, _tws2 = strip_edge_whitespace_events(new_events)
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

    allowed = set(getattr(differ.config, 'visual_container_tags', ()))
    if old_lname not in allowed and new_lname not in allowed:
        return False

    old_txt = extract_text_from_events(old_events)
    new_txt = extract_text_from_events(new_events)
    if not old_txt or not new_txt:
        return False
    if collapse_ws(old_txt) != collapse_ws(new_txt):
        return False

    # Same visible text but different inline formatting structure (e.g. strong removed)
    if structure_signature(old_events, differ.config) != structure_signature(new_events, differ.config):
        return True

    # If tag differs OR any tracked attribute differs, treat as visual change
    if old_lname != new_lname:
        return True
    for attr in getattr(differ.config, 'track_attrs', ('style', 'class', 'src', 'href')):
        if old_attrs.get(attr) != new_attrs.get(attr):
            return True
    # Also consider id as a common visual/selection attribute in product HTML
    if old_attrs.get('id') != new_attrs.get('id'):
        return True

    return False


def wrap_inline_visual_replace(differ, kind, wrapper_tag, attrs, inner_events, pos):
    """Envuelve eventos internos en un wrapper inline para reemplazo visual."""
    kind_tag = QName(kind)
    wrapper_q = wrapper_tag
    differ.append(START, (kind_tag, differ._change_attrs(diff_id=differ._active_diff_id())), pos)
    differ.append(START, (wrapper_q, attrs), pos)
    # Render inner events verbatim (including <br>, <strong>, etc.)
    with differ.context(None):
        differ.block_process(inner_events)
    differ.append(END, wrapper_q, pos)
    differ.append(END, kind_tag, pos)


def wrap_block_visual_replace(differ, kind, wrapper_tag, attrs, inner_events, pos):
    """
    Keep HTML valid by not nesting block tags inside <ins>/<del>.
    Instead:
      <p style=old><del>TEXT</del></p>
      <p style=new><ins>TEXT</ins></p>
    """
    kind_tag = QName(kind)
    wrapper_q = wrapper_tag
    differ.append(START, (wrapper_q, attrs), pos)
    differ.append(START, (kind_tag, differ._change_attrs(diff_id=differ._active_diff_id())), pos)
    # Emit inner events without wrapping again (we are already inside <ins>/<del>),
    # but convert <br> into a visible marker so double line breaks show an empty
    # line with ¶ even when the change is "visual-only".
    marker = getattr(differ.config, 'linebreak_marker', u'\u00b6')
    skip_br_end = 0
    for et, d, p2 in inner_events:
        if skip_br_end and et == END and qname_localname(d) == 'br':
            skip_br_end -= 1
            continue
        if et == START:
            ttag, tattrs = d
            if qname_localname(ttag) == 'br':
                # inside <ins>/<del>, so plain TEXT marker is enough
                differ.append(TEXT, marker, p2)
                differ.append(START, (ttag, tattrs), p2)
                differ.append(END, ttag, p2)
                skip_br_end += 1
                continue
        differ.append(et, d, p2)
    differ.append(END, kind_tag, pos)
    differ.append(END, wrapper_q, pos)


def render_visual_replace_inline(differ, old_events, new_events):
    """
    Inline visual replace:
      <p style="old">TEXT</p> -> <p style="new">TEXT</p>
    becomes:
      <del><span style="old">TEXT</span></del><ins><span style="new">TEXT</span></ins>

    This preserves reading order (del then ins) and keeps the diff inline.
    """
    lws_old, old_core, tws_old = strip_edge_whitespace_events(old_events)
    lws_new, new_core, tws_new = strip_edge_whitespace_events(new_events)

    # Preserve leading/trailing whitespace events (mostly new-side to keep DOM stable)
    for ev in lws_new:
        differ.append(*ev)

    old_events = old_core
    new_events = new_core
    if not old_events or not new_events:
        # fallback
        differ.delete(0, 0)
        return

    # Pick a stable position for injected events
    pos = (new_events or old_events)[0][2]

    old_tag, old_attrs = old_events[0][1]
    new_tag, new_attrs = new_events[0][1]

    old_inner = old_events[1:-1]
    new_inner = new_events[1:-1]

    old_l = qname_localname(old_tag)
    new_l = qname_localname(new_tag)
    # Structural tags (td, th) must remain the outermost tag to keep HTML valid.
    is_structural = (old_l in ('td', 'th') and new_l in ('td', 'th'))

    # Preserve actual wrapper tags when possible:
    # - inline wrappers: span/strong/em...
    # - block wrappers: p/h1..h6 (titles/paragraphs)
    # - structural: td/th
    old_wrap = old_tag if (old_l in INLINE_FORMATTING_TAGS or old_l in BLOCK_WRAPPER_TAGS or old_l in ('td', 'th')) else QName('span')
    new_wrap = new_tag if (new_l in INLINE_FORMATTING_TAGS or new_l in BLOCK_WRAPPER_TAGS or new_l in ('td', 'th')) else QName('span')

    if is_structural:
        # Emit the new structural tag once
        differ.append(START, (new_tag, new_attrs), pos)
        # Then emit del/ins of content
        wrap_inline_visual_replace(differ, 'del', QName('span'), old_attrs, old_inner, pos)
        wrap_inline_visual_replace(differ, 'ins', QName('span'), new_attrs, new_inner, pos)
        differ.append(END, new_tag, pos)
    else:
        if old_l in BLOCK_WRAPPER_TAGS:
            wrap_block_visual_replace(differ, 'del', old_wrap, old_attrs, old_inner, pos)
        else:
            wrap_inline_visual_replace(differ, 'del', old_wrap, old_attrs, old_inner, pos)

        if new_l in BLOCK_WRAPPER_TAGS:
            wrap_block_visual_replace(differ, 'ins', new_wrap, new_attrs, new_inner, pos)
        else:
            wrap_inline_visual_replace(differ, 'ins', new_wrap, new_attrs, new_inner, pos)

    for ev in tws_new:
        differ.append(*ev)


def find_inline_wrapper_bounds(events):
    """Encuentra los límites de un wrapper inline único en los eventos."""
    # Find first START of inline wrapper
    start_idx = None
    for i, (t, d, _p) in enumerate(events):
        if t == START and qname_localname(d[0]) in INLINE_FORMATTING_TAGS:
            start_idx = i
            break
    if start_idx is None:
        return None, None

    # Find matching END for that wrapper (non-nested heuristic)
    wname = qname_localname(events[start_idx][1][0])
    depth = 0
    end_idx = None
    for j in range(start_idx, len(events)):
        t, d, _p = events[j]
        if t == START and qname_localname(d[0]) == wname:
            depth += 1
        elif t == END and qname_localname(d) == wname:
            depth -= 1
            if depth == 0:
                end_idx = j
                break
    if end_idx is None:
        return None, None

    # Ensure there are no other inline wrapper starts outside this subtree
    for i, (t, d, _p) in enumerate(events):
        if i < start_idx or i > end_idx:
            if t == START and qname_localname(d[0]) in INLINE_FORMATTING_TAGS:
                return None, None

    return start_idx, end_idx


def validate_prefix_suffix_alignment(prefix_text, suffix_text, old_text, new_text):
    """Valida que el prefijo y sufijo común estén alineados correctamente."""
    pre_len = longest_common_prefix_len(old_text, new_text)
    suf_len = longest_common_suffix_len(old_text, new_text, max_prefix=pre_len)
    return pre_len == len(prefix_text) and suf_len == len(suffix_text)


def try_inline_wrapper_to_plain(differ, old_events, new_events):
    """
    Handle patterns like:
      <p>Texto <u>subrayado</u></p> -> <p>Texto normal</p>
    without marking the unchanged prefix ("Texto ") as del/ins.

    This only triggers when:
    - new is a single TEXT event (within the compared range)
    - old has exactly one inline wrapper segment (span/strong/b/em/i/u)
    - common prefix/suffix align cleanly with old's leading/trailing TEXT events
    """
    if len(new_events) != 1 or new_events[0][0] != TEXT:
        return False
    if not old_events:
        return False

    # Identify a single inline wrapper subtree inside old_events
    start_idx, end_idx = find_inline_wrapper_bounds(old_events)
    if start_idx is None or end_idx is None:
        return False

    # Split old events into prefix TEXT (before wrapper), wrapper subtree, suffix TEXT (after wrapper)
    prefix_events = old_events[:start_idx]
    wrapper_events = old_events[start_idx:end_idx + 1]
    suffix_events = old_events[end_idx + 1:]

    old_text = raw_text_from_events(old_events)
    new_text = new_events[0][1] or u''
    prefix_text = raw_text_from_events(prefix_events)
    suffix_text = raw_text_from_events(suffix_events)

    # Validate prefix/suffix alignment
    if not validate_prefix_suffix_alignment(prefix_text, suffix_text, old_text, new_text):
        return False

    # Compute common prefix/suffix on raw strings
    pre_len = longest_common_prefix_len(old_text, new_text)
    suf_len = longest_common_suffix_len(old_text, new_text, max_prefix=pre_len)

    # Remaining new text that replaces the wrapper subtree
    mid_new = new_text[pre_len:len(new_text) - suf_len if suf_len else len(new_text)]

    # Emit prefix unchanged
    if prefix_text:
        pos = (prefix_events[-1][2] if prefix_events else new_events[0][2])
        differ.append(TEXT, prefix_text, pos)

    # Emit deletion preserving wrapper formatting, then insertion of the replacement text
    with differ.context('del'):
        differ.block_process(wrapper_events)
    if mid_new:
        mark_text(differ, new_events[0][2], mid_new, 'ins')

    # Emit suffix unchanged
    if suffix_text:
        differ.append(TEXT, suffix_text, new_events[0][2])

    return True
