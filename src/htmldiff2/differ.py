# -*- coding: utf-8 -*-
"""
Clases principales para realizar diffs de streams de Genshi.
"""
from __future__ import with_statement

from difflib import SequenceMatcher
from contextlib import contextmanager
from genshi.core import Stream, QName, Attrs, START, END, TEXT

from .config import DiffConfig, text_type, INLINE_FORMATTING_TAGS, BLOCK_WRAPPER_TAGS
from .utils import (
    qname_localname, collapse_ws, strip_edge_whitespace_events,
    extract_text_from_events, raw_text_from_events, concat_events,
    longest_common_prefix_len, longest_common_suffix_len,
    has_visual_attrs, attrs_signature, structure_signature,
    merge_adjacent_change_tags
)
from .atomization import atomize_events
from .normalization import (
    normalize_opcodes_for_delete_first,
    normalize_inline_wrapper_opcodes,
    normalize_inline_wrapper_tag_change_opcodes,
    should_force_visual_replace
)
from .parser import longzip
from .table_differ import (
    extract_direct_tr_cells, extract_tr_blocks, row_key, cell_key,
    diff_table_by_rows, diff_tr_by_cells
)
from .block_processor import block_process as block_process_events
from .visual_replace import (
    try_visual_wrapper_toggle_without_dup, can_unwrap_wrapper,
    can_visual_container_replace, wrap_inline_visual_replace,
    wrap_block_visual_replace, render_visual_replace_inline,
    find_inline_wrapper_bounds, validate_prefix_suffix_alignment,
    try_inline_wrapper_to_plain
)
from .text_differ import mark_text, diff_text


def diff_genshi_stream(old_stream, new_stream):
    """Renders a creole diff for two texts."""
    differ = StreamDiffer(old_stream, new_stream)
    return differ.get_diff_stream()


def render_html_diff(old, new, wrapper_element='div', wrapper_class='diff', config=None):
    """Renders the diff between two HTML fragments."""
    from .parser import parse_html
    old_stream = parse_html(old, wrapper_element, wrapper_class)
    new_stream = parse_html(new, wrapper_element, wrapper_class)
    differ = StreamDiffer(old_stream, new_stream, config=config)
    rv = differ.get_diff_stream()
    return rv.render('html', encoding=None)


class StreamDiffer(object):
    """A class that can diff a stream of Genshi events. It will inject
``<ins>`` and ``<del>`` tags into the stream. It probably breaks
in very ugly ways if you pass a random Genshi stream to it. I'm
not exactly sure if it's correct what creoleparser is doing here,
but it appears that it's not using a namespace. That's fine with me
so the tags the `StreamDiffer` adds are also unnamespaced.
"""

    def __init__(self, old_stream, new_stream, config=None, diff_id_state=None):
        self.config = config or DiffConfig()
        self._old_events = list(old_stream)
        self._new_events = list(new_stream)
        # Atomize for better list/table alignment + text granularity.
        self._old_atoms = atomize_events(self._old_events, self.config)
        self._new_atoms = atomize_events(self._new_events, self.config)
        self._result = None
        self._stack = []
        self._context = None
        self._skip_end_for = []  # used for suppressing void tags on delete (e.g. <br>)
        self._wrap_change_end_for = []  # stack of (localname, change_tag) for void elements (e.g. img)
        self._diff_id_state = diff_id_state if diff_id_state is not None else [0]
        self._diff_id_stack = []

    @contextmanager
    def diff_group(self, diff_id=None):
        """
        Group multiple emitted diff markers under the same diff id.

        When config.add_diff_ids is False, this is a no-op.
        """
        if not getattr(self.config, 'add_diff_ids', False):
            yield None
            return
        if diff_id is None:
            diff_id = self._new_diff_id()
        self._diff_id_stack.append(text_type(diff_id))
        try:
            yield text_type(diff_id)
        finally:
            self._diff_id_stack.pop()

    def _new_diff_id(self):
        self._diff_id_state[0] += 1
        return text_type(self._diff_id_state[0])

    def _active_diff_id(self):
        if self._diff_id_stack:
            return self._diff_id_stack[-1]
        return None

    def _set_attr(self, attrs, name, value):
        q = QName(name)
        try:
            items = list(attrs) if attrs is not None else []
        except Exception:
            items = []
        items = [(k, v) for (k, v) in items if k != q]
        items.append((q, text_type(value)))
        return Attrs(items)

    def _change_attrs(self, base_attrs=None, diff_id=None):
        """
        Build Attrs for an <ins>/<del> wrapper, injecting diff id if enabled.
        """
        attrs = Attrs(list(base_attrs)) if base_attrs is not None else Attrs()
        if getattr(self.config, 'add_diff_ids', False):
            if diff_id is None:
                diff_id = self._active_diff_id() or self._new_diff_id()
            attr_name = getattr(self.config, 'diff_id_attr', 'data-diff-id')
            attrs = self._set_attr(attrs, attr_name, diff_id)
        return attrs

    @contextmanager
    def context(self, kind):
        old_context = self._context
        self._context = kind
        try:
            yield
        finally:
            self._context = old_context

    def inject_class(self, attrs, classname):
        cls = attrs.get('class')
        attrs |= [(QName('class'), cls and cls + ' ' + classname or classname)]
        return attrs

    def inject_refattr(self, attrs, old_attrs):
        # Track attribute changes (visual + refs). Only inject data-old-* when changed.
        for attr in getattr(self.config, 'track_attrs', ('style', 'class', 'src', 'href')):
            old_attr = old_attrs.get(attr)
            new_attr = attrs.get(attr)
            if old_attr != new_attr:
                if new_attr is not None:
                    attrs |= [(QName(attr), new_attr)]
                if old_attr is not None:
                    attrs |= [(QName('data-old-%s' % attr), old_attr)]
        return attrs

    def append(self, type, data, pos):
        self._result.append((type, data, pos))

    def _handle_replace_special_cases(self, old, new, old_start, old_end, new_start, new_end):
        """Maneja casos especiales de reemplazo antes del procesamiento general."""
        # Special-case: one inline wrapper removed/changed while keeping a shared
        # prefix/suffix. This prevents over-highlighting unchanged prefix text
        # (e.g. "Texto " in underline_removal) and keeps del->ins ordering.
        if try_inline_wrapper_to_plain(self, old, new):
            return True

        # Special-case: visual-only wrapper added/removed around identical text,
        # where the wrapper carries visual styling (style/class/id). In tables this
        # happens a lot (<td>10.8</td> -> <td><strong style=...>10.8</strong></td>).
        # Rendering this as del+ins duplicates the same value and looks terrible.
        # Instead, render a single copy and mark the wrapper as "replaced".
        if try_visual_wrapper_toggle_without_dup(self, old, new):
            return True

        # Fallback: If the new side collapses to a single TEXT node but the old side contains
        # formatting tags, SequenceMatcher can emit ins->del ordering. Prefer a stable
        # del->ins ordering and preserve old formatting in the deletion.
        if len(new) == 1 and new[0][0] == TEXT and any(e[0] in (START, END) for e in old):
            with self.diff_group():
                self.delete(old_start, old_end)
                self.insert(new_start, new_end)
            return True

        # Special-case: unwrap/wrap inline wrapper (e.g. <b>/<strong>) with same text.
        # Fixes Bold -> Normal and maintains consistent Delete -> Insert.
        if can_unwrap_wrapper(self, old, new):
            with self.diff_group():
                self.delete(old_start, old_end)
                self.insert(new_start, new_end)
            return True

        # Special-case: visual-only changes (same text, different attrs/tag).
        # Required to mark font-size/font-weight/class/style/id changes as diffs even
        # when text is identical.
        if can_visual_container_replace(self, old, new):
            if getattr(self.config, 'visual_replace_inline', True):
                # Render inline del->ins while keeping styles, so changes like
                # font-size/font-weight don't turn into separate block lines.
                with self.diff_group():
                    render_visual_replace_inline(self, old, new)
            else:
                with self.diff_group():
                    self.delete(old_start, old_end)
                    self.insert(new_start, new_end)
            return True
        
        return False

    def _handle_matching_event_types(self, old_event, new_event):
        """Maneja eventos del mismo tipo."""
        event_type = old_event[0]
        if event_type == START:
            old_, (old_tag, old_attrs), old_pos = old_event
            _, (tag, attrs), pos = new_event
            self.enter_mark_replaced(pos, tag, attrs, old_attrs)
        elif event_type == END:
            _, tag, pos = new_event
            if not self.leave(pos, tag):
                self.leave(pos, old_event[1])
        elif event_type == TEXT:
            _, new_text, pos = new_event
            diff_text(self, pos, old_event[1], new_text)
        else:
            self.append(*new_event)

    def _handle_mismatched_event_types(self, old_event, new_event, old_start, old_end, 
                                       new_start, new_end, idx):
        """Maneja eventos de tipos diferentes."""
        # If the old event was text and the new one is the start or end of a tag
        if old_event[0] == TEXT and new_event[0] in (START, END):
            _, text, pos = old_event
            mark_text(self, pos, text, 'del')
            type, data, pos = new_event
            if type == START:
                self.enter(pos, *data)
            else:
                self.leave(pos, data)
            return False

        # Old stream opened or closed a tag that went away in the new one
        if old_event[0] in (START, END) and new_event[0] == TEXT:
            # Prefer a stable delete->insert representation that preserves
            # the old formatting wrappers (e.g. <strong>...) instead of
            # dropping them and emitting only an <ins>.
            #
            # This is especially important for changes like:
            #   "Texto <strong>en negrita</strong>" -> "Texto normal"
            # where the deleted content must remain bold (inside <del>).
            with self.diff_group():
                self.delete(old_start + idx, old_end)
                self.insert(new_start + idx, new_end)
            return True  # Signal to break
        
        return False

    def replace(self, old_start, old_end, new_start, new_end):
        old = self._old_events[old_start:old_end]
        new = self._new_events[new_start:new_end]

        # Handle special cases first
        if self._handle_replace_special_cases(old, new, old_start, old_end, new_start, new_end):
            return

        # Process events pairwise
        for idx, (old_event, new_event) in enumerate(longzip(old, new)):
            if old_event is None:
                self.insert(new_start + idx, new_end + idx)
                break
            elif new_event is None:
                self.delete(old_start + idx, old_end + idx)
                break
 
            # Handle matching event types
            if old_event[0] == new_event[0]:
                self._handle_matching_event_types(old_event, new_event)
            else:
                # Handle mismatched event types
                if self._handle_mismatched_event_types(old_event, new_event, 
                                                       old_start, old_end, 
                                                       new_start, new_end, idx):
                    break


    def delete(self, start, end):
        with self.context('del'):
            self.block_process(self._old_events[start:end])

    def insert(self, start, end):
        with self.context('ins'):
            self.block_process(self._new_events[start:end])

    def unchanged(self, start, end):
        with self.context(None):
            self.block_process(self._old_events[start:end])

    def enter(self, pos, tag, attrs):
        self._stack.append(tag)
        self.append(START, (tag, attrs), pos)

    def enter_mark_replaced(self, pos, tag, attrs, old_attrs):
        attrs = self.inject_class(attrs, 'tagdiff_replaced')
        attrs = self.inject_refattr(attrs, old_attrs)
        if getattr(self.config, 'add_diff_ids', False):
            diff_id = self._active_diff_id() or self._new_diff_id()
            attrs = self._set_attr(attrs, getattr(self.config, 'diff_id_attr', 'data-diff-id'), diff_id)
        self._stack.append(tag)
        self.append(START, (tag, attrs), pos)

    def leave(self, pos, tag):
        if not self._stack:
            return False
        if tag == self._stack[-1]:
            self.append(END, tag, pos)
            self._stack.pop()
            return True
        return False

    def leave_all(self):
        if self._stack:
            last_pos = (self._new_events or self._old_events)[-1][2]
            for tag in reversed(self._stack):
                self.append(END, tag, last_pos)
        del self._stack[:]

    def block_process(self, events):
        """Process block-level events."""
        block_process_events(self, events)





    def _process_replace_opcode(self, old_atoms_slice, new_atoms_slice):
        """Procesa un opcode de tipo 'replace'."""
        old_events = concat_events(old_atoms_slice)
        new_events = concat_events(new_atoms_slice)
        
        # SIEMPRE agrupar si hay un cambio estructural de lista (bullets)
        # o si hay una mezcla de tags estructurales que el matcher
        # no pudo alinear perfectamente.
        def _has_structural_tags(events):
            for et, d, _p in events:
                if et == START:
                    t, _a = d
                    ln = qname_localname(t)
                    if ln in ("ul", "ol", "li", "table", "tr", "td", "th"):
                        return True
            return False

        # Si el cambio involucra tags estructurales, forzamos un bloque atómico.
        # EXCEPCIÓN: Si los átomos de ambos lados son exactamente iguales en cantidad
        # y tipo de tag, dejamos que el inner differ lo maneje (para cambios de estilo).
        is_pure_style_structural = (
            len(old_atoms_slice) == len(new_atoms_slice) and
            all(a1.get('kind') == 'block' and a2.get('kind') == 'block' and a1.get('tag') == a2.get('tag')
                for a1, a2 in zip(old_atoms_slice, new_atoms_slice))
        )

        # Special-case: <tr> blocks should be diffed by cells, not by raw events.
        if (
            is_pure_style_structural
            and len(old_atoms_slice) == 1
            and len(new_atoms_slice) == 1
            and old_atoms_slice[0].get('kind') == 'block'
            and new_atoms_slice[0].get('kind') == 'block'
            and old_atoms_slice[0].get('tag') == 'tr'
            and new_atoms_slice[0].get('tag') == 'tr'
        ):
            diff_tr_by_cells(self, old_atoms_slice[0]['events'], new_atoms_slice[0]['events'])
            return

        # Special-case: whole table blocks should be diffed by rows/cells (keeps HTML valid
        # even if table attributes change and avoids splitting table start/end across opcodes).
        if (
            len(old_atoms_slice) == 1
            and len(new_atoms_slice) == 1
            and old_atoms_slice[0].get("kind") == "block"
            and new_atoms_slice[0].get("kind") == "block"
            and old_atoms_slice[0].get("tag") == "table"
            and new_atoms_slice[0].get("tag") == "table"
        ):
            diff_table_by_rows(self, old_atoms_slice[0]["events"], new_atoms_slice[0]["events"])
            return

        if (_has_structural_tags(old_events) or _has_structural_tags(new_events)) and not is_pure_style_structural:
            with self.diff_group():
                with self.context("del"):
                    self.block_process(old_events)
                with self.context("ins"):
                    self.block_process(new_events)
            return

        # Default: Diff the expanded events with an inner EventDiffer (no atomization)
        # Pass current diff_id_state to maintain consistent IDs
        inner = _EventDiffer(old_events, new_events, self.config, diff_id_state=self._diff_id_state)
        for ev in inner.get_diff_events():
            self.append(*ev)

    def _process_equal_opcode(self, old_atoms_slice, new_atoms_slice):
        """Procesa un opcode de tipo 'equal' con manejo especial para tablas."""
        # For table/list-related blocks, run an inner event diff even when the
        # outer atom keys are equal. This catches visual-only formatting
        # changes (e.g. <th> style, wrapping <strong style=...>) without
        # breaking structure.
        for a_old, a_new in longzip(old_atoms_slice, new_atoms_slice):
            if a_old is None:
                with self.context(None):
                    self.block_process(concat_events([a_new]))
                continue
            if a_new is None:
                with self.context(None):
                    self.block_process(concat_events([a_old]))
                continue
            
            # Special case: Paragraph <-> List Item transition with matching text
            # (bullet stripping in atomization makes keys equal).
            # Force inner diff to show granular "-" deletion / bullet insertion.
            old_t = a_old.get('tag')
            new_t = a_new.get('tag')
            if (old_t == 'p' and new_t == 'li') or (old_t == 'li' and new_t == 'p'):
                 inner = _EventDiffer(a_old['events'], a_new['events'], self.config, diff_id_state=self._diff_id_state)
                 for ev in inner.get_diff_events():
                     self.append(*ev)
                 continue

            # Si el texto es igual pero los tags son distintos (ej: <p> -> <li>), 
            # forzamos un bloque diff atómico con un solo ID.
            if a_new.get('kind') == 'block' and a_old.get('kind') == 'block' and a_old['events'][0][1][0] != a_new['events'][0][1][0]:
                with self.diff_group():
                    with self.context('del'):
                        self.block_process(a_old['events'])
                    with self.context('ins'):
                        self.block_process(a_new['events'])
                continue

            is_structural = a_new.get('kind') == 'block' and a_new.get('tag') in ('table', 'tr', 'td', 'th', 'ul', 'ol', 'li')
            
            if is_structural:
                if a_new.get('tag') == 'tr':
                    diff_tr_by_cells(self, a_old['events'], a_new['events'])
                elif a_new.get('tag') == 'table':
                    diff_table_by_rows(self, a_old['events'], a_new['events'])
                else:
                    inner = _EventDiffer(a_old['events'], a_new['events'], self.config, diff_id_state=self._diff_id_state)
                    for ev in inner.get_diff_events():
                        self.append(*ev)
            else:
                old_events = a_old.get('events') or []
                new_events = a_new.get('events') or []

                # Visual-only attribute changes (same text, different style/class/attrs)
                # should still produce a visible diff even when atom keys match.
                if old_events != new_events and can_visual_container_replace(self, old_events, new_events):
                    # Check if the structure differs (e.g. <strong> tags added/removed).
                    # If so, run a granular inner diff on children instead of full block replace.
                    old_sig = structure_signature(old_events, self.config)
                    new_sig = structure_signature(new_events, self.config)
                    
                    if old_sig != new_sig:
                        # Structure differs - use specialized inline formatting diff
                        # that preserves unchanged text and only marks formatting changes.
                        from .diff_inline_formatting import diff_inline_formatting
                        if diff_inline_formatting(self, old_events, new_events):
                            continue
                        
                        # Fallback: if text doesn't match, use standard inner diff
                        if (old_events and new_events and
                            old_events[0][0] == START and new_events[0][0] == START and
                            old_events[-1][0] == END and new_events[-1][0] == END):
                            
                            (cont_tag, cont_attrs) = new_events[0][1]
                            cont_pos = new_events[0][2]
                            self.enter(cont_pos, cont_tag, cont_attrs)
                            
                            old_children = old_events[1:-1]
                            new_children = new_events[1:-1]
                            
                            inner = _EventDiffer(old_children, new_children, self.config, diff_id_state=self._diff_id_state)
                            for ev in inner.get_diff_events():
                                self.append(*ev)
                            
                            self.leave(new_events[-1][2], new_events[-1][1])
                            continue
                    
                    # Attribute-only change (same structure) - use visual replace
                    with self.diff_group():
                        render_visual_replace_inline(self, old_events, new_events)
                    continue

                # Whitespace-only text changes can be hidden by atomization keys
                # (we intentionally collapse whitespace for alignment). If this atom
                # is a simple container with a single TEXT child, and the only
                # difference is whitespace multiplicity, run an inner event diff so
                # deleted/inserted spaces become visible.
                if old_events != new_events:
                    try:
                        if (
                            len(old_events) == 3
                            and len(new_events) == 3
                            and old_events[0][0] == START and new_events[0][0] == START
                            and old_events[1][0] == TEXT and new_events[1][0] == TEXT
                            and old_events[2][0] == END and new_events[2][0] == END
                            and old_events[0][1][0] == new_events[0][1][0]
                            and old_events[2][1] == new_events[2][1]
                        ):
                            old_txt = old_events[1][1] or u''
                            new_txt = new_events[1][1] or u''
                            if old_txt != new_txt and collapse_ws(old_txt) == collapse_ws(new_txt):
                                inner = _EventDiffer(old_events, new_events, self.config, diff_id_state=self._diff_id_state)
                                for ev in inner.get_diff_events():
                                    self.append(*ev)
                                continue
                    except Exception:
                        # If anything goes wrong, fall back to unchanged rendering.
                        pass

                # If atoms compare equal by key but differ in event streams due to
                # non-textual "void" elements (e.g. <img>), run an inner event diff
                # so additions/removals become visible as <ins>/<del>.
                force_tags = set(getattr(self.config, 'force_event_diff_on_equal_for_tags', ()))
                if force_tags and old_events != new_events:
                    def _has_force_tag(events):
                        for et, d, _p in events:
                            if et == START:
                                t, _a = d
                                if qname_localname(t) in force_tags:
                                    return True
                        return False

                    if _has_force_tag(old_events) or _has_force_tag(new_events):
                        # Prefer diffing only the *children* when both sides are a
                        # simple container (START...END). This keeps unchanged prefix
                        # text outside of <del>/<ins> when the only real change is a
                        # void element like <img> being added/removed.
                        if (
                            old_events
                            and new_events
                            and old_events[0][0] == START and old_events[-1][0] == END
                            and new_events[0][0] == START and new_events[-1][0] == END
                            and old_events[0][1][0] == new_events[0][1][0]
                            and old_events[-1][1] == new_events[-1][1]
                        ):
                            # Emit container start, diff children, then container end
                            (cont_tag, cont_attrs) = new_events[0][1]
                            cont_pos = new_events[0][2]
                            self.enter(cont_pos, cont_tag, cont_attrs)

                            old_children = old_events[1:-1]
                            new_children = new_events[1:-1]

                            def _visible_ws(s):
                                if not s:
                                    return s
                                if not getattr(self.config, 'preserve_whitespace_in_diff', True):
                                    return s
                                # Keep newlines (indentation) as-is, but make inline
                                # whitespace visible inside <ins>/<del>.
                                return u''.join((u'\u00a0' if (ch.isspace() and ch not in u'\n\r') else ch) for ch in s)

                            def _split_text_then_force_tail(children):
                                # Keep leading/trailing whitespace-only TEXT as-is, but require
                                # at most one TEXT with non-whitespace content.
                                if children is None:
                                    return None
                                leading_ws = []
                                i = 0
                                while i < len(children) and children[i][0] == TEXT and (children[i][1] or u'').strip() == u'':
                                    leading_ws.append(children[i])
                                    i += 1
                                if i >= len(children):
                                    return leading_ws, None, []
                                if children[i][0] != TEXT:
                                    return None
                                text_ev = children[i]
                                tail = children[i + 1:]
                                # Tail must be whitespace-only TEXT or START/END of force tags.
                                for et, d, _p in tail:
                                    if et == TEXT:
                                        if (d or u'').strip() != u'':
                                            return None
                                    elif et in (START, END):
                                        t = d[0] if et == START else d
                                        if qname_localname(t) not in force_tags:
                                            return None
                                    else:
                                        return None
                                return leading_ws, text_ev, tail

                            # Ultra-specific: keep common text unchanged and only mark the
                            # trailing void tail (plus any trailing whitespace) as ins/del.
                            parsed_old = _split_text_then_force_tail(old_children)
                            parsed_new = _split_text_then_force_tail(new_children)
                            if parsed_old and parsed_new and parsed_old[1] and parsed_new[1]:
                                old_lead, old_text_ev, old_tail = parsed_old
                                new_lead, new_text_ev, new_tail = parsed_new
                                old_text = old_text_ev[1] or u''
                                new_text = new_text_ev[1] or u''
                                if collapse_ws(old_text) == collapse_ws(new_text):
                                    pre_len = longest_common_prefix_len(old_text, new_text)
                                    suf_len = longest_common_suffix_len(old_text, new_text, max_prefix=pre_len)
                                    old_mid = old_text[pre_len:len(old_text) - suf_len if suf_len else len(old_text)]
                                    new_mid = new_text[pre_len:len(new_text) - suf_len if suf_len else len(new_text)]
                                    common_prefix = old_text[:pre_len]
                                    common_suffix = old_text[len(old_text) - suf_len:] if suf_len else u''

                                    # Emit leading whitespace from "new"
                                    for ev in new_lead:
                                        self.append(*ev)
                                    # Emit common text unchanged (prefix + suffix)
                                    self.append(TEXT, common_prefix + common_suffix, new_text_ev[2])

                                    # Delete tail (mid + old_tail)
                                    if (old_mid or old_tail) and not (new_mid or new_tail):
                                        self.append(START, (QName('del'), self._change_attrs(diff_id=self._active_diff_id())), old_text_ev[2])
                                        if old_mid:
                                            self.append(TEXT, _visible_ws(old_mid), old_text_ev[2])
                                        for ev in old_tail:
                                            self.append(*ev)
                                        self.append(END, QName('del'), old_text_ev[2])
                                    # Insert tail (mid + new_tail)
                                    elif (new_mid or new_tail) and not (old_mid or old_tail):
                                        self.append(START, (QName('ins'), self._change_attrs(diff_id=self._active_diff_id())), new_text_ev[2])
                                        if new_mid:
                                            self.append(TEXT, _visible_ws(new_mid), new_text_ev[2])
                                        for ev in new_tail:
                                            self.append(*ev)
                                        self.append(END, QName('ins'), new_text_ev[2])
                                    else:
                                        # Fallback to inner differ for anything more complex
                                        inner = _EventDiffer(old_children, new_children, self.config, diff_id_state=self._diff_id_state)
                                        for ev in inner.get_diff_events():
                                            self.append(*ev)

                                    # Emit trailing whitespace from "new" (events after tail are none by design)
                                    self.leave(new_events[-1][2], new_events[-1][1])
                                    continue

                            inner = _EventDiffer(old_children, new_children, self.config, diff_id_state=self._diff_id_state)
                            for ev in inner.get_diff_events():
                                self.append(*ev)
                            self.leave(new_events[-1][2], new_events[-1][1])
                            continue

                        inner = _EventDiffer(old_events, new_events, self.config, diff_id_state=self._diff_id_state)
                        for ev in inner.get_diff_events():
                            self.append(*ev)
                        continue

                with self.context(None):
                    self.block_process(new_events)

    def process(self):
        self._result = []
        
        # Global similarity check: if texts are too different, do bulk del + ins
        # instead of granular structural matching (avoids interleaved shredding).
        bulk_threshold = getattr(self.config, 'bulk_replace_similarity_threshold', 0.3)
        if bulk_threshold > 0:
            old_text = extract_text_from_events(self._old_events)
            new_text = extract_text_from_events(self._new_events)
            if old_text.strip() and new_text.strip():
                ratio = SequenceMatcher(None, old_text, new_text).ratio()
                if ratio < bulk_threshold:
                    # Texts are too different - render as bulk delete then bulk insert
                    with self.diff_group():
                        with self.context('del'):
                            self.block_process(self._old_events)
                        with self.context('ins'):
                            self.block_process(self._new_events)
                    return
        
        # Run SequenceMatcher on atom keys (better alignment for lists/tables/text).
        old_keys = [a['key'] for a in self._old_atoms]
        new_keys = [a['key'] for a in self._new_atoms]
        matcher = SequenceMatcher(None, old_keys, new_keys)
        opcodes = matcher.get_opcodes()
        if getattr(self.config, 'delete_first', True):
            opcodes = normalize_opcodes_for_delete_first(opcodes)

        def _has_list_tags(events):
            for et, d, _p in events:
                if et == START:
                    t, _a = d
                    if qname_localname(t) in ("ul", "ol", "li"):
                        return True
            return False

        def _count_block_wrappers(events):
            count = 0
            for et, d, _p in events:
                if et == START:
                    t, _a = d
                    if qname_localname(t) in ("p", "h1", "h2", "h3", "h4", "h5", "h6"):
                        count += 1
            return count

        k = 0
        while k < len(opcodes):
            tag, i1, i2, j1, j2 = opcodes[k]

            # Pair structural list conversions even when SequenceMatcher emits delete+insert
            # as separate opcodes (not the same anchor => not normalized into replace).
            if tag in ("delete", "insert") and k + 1 < len(opcodes):
                tag2, i1b, i2b, j1b, j2b = opcodes[k + 1]
                if tag == "delete" and tag2 == "insert":
                    old_events = concat_events(self._old_atoms[i1:i2])
                    new_events = concat_events(self._new_atoms[j1b:j2b])
                    if _has_list_tags(old_events) != _has_list_tags(new_events):
                        if _count_block_wrappers(old_events) <= 1 and _count_block_wrappers(new_events) <= 2:
                            with self.diff_group():
                                with self.context("del"):
                                    self.block_process(old_events)
                                with self.context("ins"):
                                    self.block_process(new_events)
                            k += 2
                            continue
                if tag == "insert" and tag2 == "delete":
                    old_events = concat_events(self._old_atoms[i1b:i2b])
                    new_events = concat_events(self._new_atoms[j1:j2])
                    if _has_list_tags(old_events) != _has_list_tags(new_events):
                        if _count_block_wrappers(old_events) <= 1 and _count_block_wrappers(new_events) <= 2:
                            with self.diff_group():
                                with self.context("del"):
                                    self.block_process(old_events)
                                with self.context("ins"):
                                    self.block_process(new_events)
                            k += 2
                            continue

            if tag == 'replace':
                # Special Check: Attribute-only change on a structural start tag?
                # Pattern: replace(START) where tag names match and is structural.
                # This avoids nesting (e.g. <ul deleted><ul added>...</ul></ul>).
                if (i2 - i1) == 1 and (j2 - j1) == 1:
                    old_atom = self._old_atoms[i1]
                    new_atom = self._new_atoms[j1]
                    old_evs = old_atom.get('events', [])
                    new_evs = new_atom.get('events', [])
                    
                    if len(old_evs) == 1 and len(new_evs) == 1:
                        old_ev = old_evs[0]
                        new_ev = new_evs[0]
                        if old_ev[0] == START and new_ev[0] == START:
                            (old_t, old_attrs) = old_ev[1]
                            (new_t, new_attrs) = new_ev[1]
                            structural_tags = ('table', 'thead', 'tbody', 'tfoot', 'tr', 'td', 'th', 'ul', 'ol', 'li')
                            lname = qname_localname(old_t)
                            if lname == qname_localname(new_t) and lname in structural_tags:
                                self.enter_mark_replaced(new_ev[2], new_t, new_attrs, old_attrs)
                                k += 1
                                continue

                self._process_replace_opcode(self._old_atoms[i1:i2], self._new_atoms[j1:j2])
            elif tag == 'delete':
                with self.diff_group():
                    with self.context('del'):
                        self.block_process(concat_events(self._old_atoms[i1:i2]))
            elif tag == 'insert':
                with self.diff_group():
                    with self.context('ins'):
                        self.block_process(concat_events(self._new_atoms[j1:j2]))
            else:  # equal
                self._process_equal_opcode(self._old_atoms[i1:i2], self._new_atoms[j1:j2])
            k += 1
        self.leave_all()

    def get_diff_stream(self):
        if self._result is None:
            self.process()
        if getattr(self.config, 'merge_adjacent_change_tags', True):
            self._result = merge_adjacent_change_tags(self._result, config=self.config)
        return Stream(self._result)



# Import _EventDiffer factory function (will be created after StreamDiffer is defined)
from .event_differ import create_event_differ_class

# Create _EventDiffer class now that StreamDiffer is fully defined
_EventDiffer = create_event_differ_class(StreamDiffer)


