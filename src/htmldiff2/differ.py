# -*- coding: utf-8 -*-

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
    merge_adjacent_change_tags, events_equal_normalized
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
``<ins>`` and ``<del>`` tags into the stream.
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
        # Stack for buffering content inside style-changed elements.
        # Each entry: {'tag': QName, 'old_style': str, 'events': list, 'diff_id': str|None}
        self._style_del_buffer = []

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
        if self._style_del_buffer:
            # Buffering content for a style-changed element
            self._style_del_buffer[-1]['events'].append((type, data, pos))
        else:
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

    def enter_mark_replaced(self, pos, tag, attrs, old_attrs, old_tag=None):
        from .utils import normalize_style_value
        # Check if this is a same-tag, style-only change -> use del/ins buffer
        if (old_tag is None or old_tag == tag):
            # Check if only style differs (normalize for order)
            non_style_match = True
            old_style = old_attrs.get('style') or ''
            new_style = attrs.get('style') or ''
            for k, v in attrs:
                k_str = str(k)
                if k_str == 'style':
                    continue
                old_v = old_attrs.get(k_str)
                if old_v != v:
                    non_style_match = False
                    break
            if non_style_match:
                for k, v in old_attrs:
                    k_str = str(k)
                    if k_str == 'style':
                        continue
                    new_v = attrs.get(k_str)
                    if new_v != v:
                        non_style_match = False
                        break
            if non_style_match and normalize_style_value(old_style) != normalize_style_value(new_style):
                # Style-only change on same tag: enter normally, buffer content for del/ins
                diff_id = None
                if getattr(self.config, 'add_diff_ids', False):
                    diff_id = self._active_diff_id() or self._new_diff_id()
                    attrs = self._set_attr(attrs, getattr(self.config, 'diff_id_attr', 'data-diff-id'), diff_id)
                self._stack.append(tag)
                self.append(START, (tag, attrs), pos)
                self._style_del_buffer.append({
                    'tag': tag,
                    'old_style': old_style,
                    'events': [],
                    'diff_id': diff_id,
                })
                return

        # Fallback: use tagdiff_replaced for tag changes or complex attr changes
        attrs = self.inject_class(attrs, 'tagdiff_replaced')
        attrs = self.inject_refattr(attrs, old_attrs)
        if old_tag and old_tag != tag:
             attrs |= [(QName('data-old-tag'), qname_localname(old_tag))]
        if getattr(self.config, 'add_diff_ids', False):
            diff_id = self._active_diff_id() or self._new_diff_id()
            attrs = self._set_attr(attrs, getattr(self.config, 'diff_id_attr', 'data-diff-id'), diff_id)
        self._stack.append(tag)
        self.append(START, (tag, attrs), pos)

    def leave(self, pos, tag):
        if not self._stack:
            return False
        if tag == self._stack[-1]:
            # Check if we're closing a style-changed element with buffered content
            if self._style_del_buffer and self._style_del_buffer[-1]['tag'] == tag:
                buf = self._style_del_buffer.pop()
                buffered = buf['events']
                old_style = buf['old_style']
                diff_id = buf['diff_id']

                # Emit del with old style
                del_attrs = Attrs()
                if old_style:
                    del_attrs = del_attrs | [(QName('style'), old_style)]
                if diff_id:
                    inner_id = self._new_diff_id()
                    del_attrs = del_attrs | [(QName(getattr(self.config, 'diff_id_attr', 'data-diff-id')), inner_id)]
                self.append(START, (QName('del'), del_attrs), (None, -1, -1))
                for ev in buffered:
                    self.append(*ev)
                self.append(END, QName('del'), (None, -1, -1))

                # Emit ins
                ins_attrs = Attrs()
                if diff_id:
                    ins_id = self._new_diff_id()
                    ins_attrs = ins_attrs | [(QName(getattr(self.config, 'diff_id_attr', 'data-diff-id')), ins_id)]
                self.append(START, (QName('ins'), ins_attrs), (None, -1, -1))
                for ev in buffered:
                    self.append(*ev)
                self.append(END, QName('ins'), (None, -1, -1))

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
                if not events_equal_normalized(old_events, new_events) and can_visual_container_replace(self, old_events, new_events):
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
                    
                    # Same structure - could be attribute-only OR text-only difference.
                    # If raw text differs, prefer inner diff for granular marking (e.g. trailing space)
                    old_raw = raw_text_from_events(old_events)
                    new_raw = raw_text_from_events(new_events)
                    
                    if old_raw != new_raw:
                        # Text differs - use inner diff for granular change marking
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
                    
                    # Attribute-only change (same structure, same text) - use visual replace
                    with self.diff_group():
                        render_visual_replace_inline(self, old_events, new_events)
                    continue

                # Whitespace-only text changes can be hidden by atomization keys
                # (we intentionally collapse whitespace for alignment). If this atom
                # is a simple container with a single TEXT child, and the only
                # difference is whitespace multiplicity, run an inner event diff so
                # deleted/inserted spaces become visible.
                if not events_equal_normalized(old_events, new_events):
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
                
                # Case-only text changes (e.g. "Cad" -> "CAD") are hidden by atomization
                # keys using .lower(). Detect when raw text differs and run inner diff.
                if not events_equal_normalized(old_events, new_events):
                    old_raw = raw_text_from_events(old_events)
                    new_raw = raw_text_from_events(new_events)
                    if old_raw != new_raw:
                        # Text differs - run inner diff for granular marking
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

                # If atoms compare equal by key but differ in event streams due to
                # non-textual "void" elements (e.g. <img>), run an inner event diff
                # so additions/removals become visible as <ins>/<del>.
                force_tags = set(getattr(self.config, 'force_event_diff_on_equal_for_tags', ()))
                if force_tags and not events_equal_normalized(old_events, new_events):
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

            # ── Structural list diff: text ↔ list with identical content ──
            # Detects pattern: insert/replace(ol/ul START...) → equal(p↔li)... → insert/replace(...ol/ul END)
            # or the reverse: delete/replace(ol/ul START...) → equal(li↔p)... → delete/replace(...ol/ul END)
            # When detected, emits bullet-only classes instead of full text del/ins.
            # Also handles 'replace' where e.g. <p> </p> is replaced by ul START.
            if tag in ('insert', 'replace'):
                # Scan insert range for an ol/ul START event atom
                list_start_ev = None
                list_tag = None
                for nj in range(j1, j2):
                    a = self._new_atoms[nj]
                    evs = a.get('events', [])
                    if len(evs) == 1 and evs[0][0] == START:
                        lname = qname_localname(evs[0][1][0])
                        if lname in ('ol', 'ul'):
                            list_start_ev = evs[0]
                            list_tag = lname
                            break

                if list_tag:
                    # Look ahead: find equal(p↔li) blocks followed by insert(...END ol/ul)
                    bullet_equal_ranges = []
                    scan_k = k + 1
                    found_structural = False
                    while scan_k < len(opcodes):
                        s_tag, s_i1, s_i2, s_j1, s_j2 = opcodes[scan_k]
                        if s_tag in ('equal', 'replace'):
                            # Check old=p blocks (or text), new=li blocks (or text)
                            # 'replace' covers when <p> with <br/> splits into multiple <li>.
                            all_p_to_li = True
                            has_block = False
                            text_only = True  # Track if range is whitespace-only
                            end_ev_in_range = None  # END ol/ul found inside this range
                            for ai in range(s_i1, s_i2):
                                old_a = self._old_atoms[ai]
                                if old_a.get('kind') == 'text':
                                    continue
                                text_only = False
                                if old_a.get('kind') == 'block' and old_a.get('tag') == 'p':
                                    has_block = True
                                    continue
                                all_p_to_li = False
                                break
                            if all_p_to_li:
                                for nj in range(s_j1, s_j2):
                                    new_a = self._new_atoms[nj]
                                    if new_a.get('kind') == 'text':
                                        continue
                                    text_only = False
                                    if new_a.get('kind') == 'block' and new_a.get('tag') == 'li':
                                        has_block = True
                                        continue
                                    # Check if this is the END ol/ul event
                                    evs = new_a.get('events', [])
                                    if (new_a.get('kind') == 'event' and len(evs) == 1
                                            and evs[0][0] == END
                                            and qname_localname(evs[0][1]) == list_tag):
                                        end_ev_in_range = evs[0]
                                        continue
                                    all_p_to_li = False
                                    break
                            if all_p_to_li and (has_block or text_only):
                                # Accept: either has p/li blocks, or is whitespace between blocks
                                bullet_equal_ranges.append((scan_k, s_tag, s_i1, s_i2, s_j1, s_j2))
                                if end_ev_in_range and bullet_equal_ranges:
                                    # END event found inside this range — pattern is complete!
                                    found_structural = True
                                    scan_k += 1
                                    break
                                scan_k += 1
                                continue
                            else:
                                break
                        elif s_tag == 'insert':
                            # Scan for END ol/ul in the insert range
                            end_ev = None
                            for nj in range(s_j1, s_j2):
                                a = self._new_atoms[nj]
                                evs = a.get('events', [])
                                if len(evs) == 1 and evs[0][0] == END:
                                    if qname_localname(evs[0][1]) == list_tag:
                                        end_ev = evs[0]
                                        break
                            if end_ev and bullet_equal_ranges:
                                found_structural = True
                                scan_k += 1
                            break
                        elif s_tag == 'delete':
                            # Old-side only deletion — might be trailing old <p> in the middle
                            # Skip it and continue scanning
                            bullet_equal_ranges.append((scan_k, s_tag, s_i1, s_i2, s_j1, s_j2))
                            scan_k += 1
                            continue
                        else:
                            break

                    if found_structural and bullet_equal_ranges:
                        # Found complete pattern! Emit structural list diff.
                        old_p_atoms = []
                        new_li_atoms = []
                        # Collect old atoms from the initial replace (e.g. deleted <p> </p>)
                        if tag == 'replace':
                            for ai in range(i1, i2):
                                if self._old_atoms[ai].get('kind') == 'block':
                                    old_p_atoms.append(self._old_atoms[ai])
                        for _, _, eq_i1, eq_i2, eq_j1, eq_j2 in bullet_equal_ranges:
                            for ai in range(eq_i1, eq_i2):
                                if self._old_atoms[ai].get('kind') == 'block':
                                    old_p_atoms.append(self._old_atoms[ai])
                            for nj in range(eq_j1, eq_j2):
                                if self._new_atoms[nj].get('kind') == 'block':
                                    new_li_atoms.append(self._new_atoms[nj])

                        if old_p_atoms and new_li_atoms:
                            with self.diff_group():
                                diff_id = self._new_diff_id() if getattr(self.config, 'add_diff_ids', False) else None

                                # Emit hidden <del class="structural-revert-data"> with old <p> events
                                revert_events = concat_events(old_p_atoms)
                                del_attrs = Attrs([(QName('class'), 'structural-revert-data'),
                                                   (QName('style'), 'display:none')])
                                if diff_id:
                                    del_attrs = del_attrs | [(QName(getattr(self.config, 'diff_id_attr', 'data-diff-id')), diff_id)]
                                self.append(START, (QName('del'), del_attrs), (None, -1, -1))
                                for ev in revert_events:
                                    self.append(*ev)
                                self.append(END, QName('del'), (None, -1, -1))

                                # Emit <ol/ul class="tagdiff_added">
                                list_qname = list_start_ev[1][0]
                                list_attrs = list_start_ev[1][1]
                                list_attrs = self.inject_class(list_attrs, 'tagdiff_added')
                                if diff_id:
                                    list_attrs = self._set_attr(list_attrs, getattr(self.config, 'diff_id_attr', 'data-diff-id'), diff_id)
                                self.enter(list_start_ev[2], list_qname, list_attrs)

                                # Build old LI lookup by text key for inner diffing
                                from .utils import normalize_style_value
                                old_li_by_text = {}
                                for oatom in old_p_atoms:
                                    oevs = oatom.get('events', [])
                                    if oevs and oevs[0][0] == START and qname_localname(oevs[0][1][0]) == 'li':
                                        otxt = ''.join(e[1] for e in oevs if e[0] == TEXT).strip()
                                        old_li_by_text[otxt] = oevs

                                # Emit each <li class="diff-bullet-ins">
                                for li_atom in new_li_atoms:
                                    li_evs = li_atom.get('events', [])
                                    if li_evs and li_evs[0][0] == START:
                                        li_tag = li_evs[0][1][0]
                                        li_attrs = li_evs[0][1][1]
                                        li_attrs = self.inject_class(li_attrs, 'diff-bullet-ins')

                                        # Check for old LI match by text
                                        new_txt = ''.join(e[1] for e in li_evs if e[0] == TEXT).strip()
                                        old_li_evs = old_li_by_text.get(new_txt)
                                        if old_li_evs:
                                            old_li_attrs = old_li_evs[0][1][1]
                                            li_attrs = self.inject_refattr(li_attrs, old_li_attrs)
                                            li_style_changed = (old_li_attrs != li_evs[0][1][1])
                                        else:
                                            li_style_changed = False

                                        if diff_id:
                                            li_attrs = self._set_attr(li_attrs, getattr(self.config, 'diff_id_attr', 'data-diff-id'), diff_id)
                                        self.enter(li_evs[0][2], li_tag, li_attrs)

                                        if li_style_changed and old_li_evs:
                                            # LI style changed: inline del(old)/ins
                                            old_style_val = old_li_attrs.get('style')
                                            with self.diff_group():
                                                del_tag_attrs = Attrs()
                                                if old_style_val:
                                                    del_tag_attrs = del_tag_attrs | [(QName('style'), old_style_val)]
                                                if diff_id:
                                                    del_tag_attrs = del_tag_attrs | [(QName(getattr(self.config, 'diff_id_attr', 'data-diff-id')), self._new_diff_id())]
                                                self.append(START, (QName('del'), del_tag_attrs), (None, -1, -1))
                                                for ev in old_li_evs[1:-1]:
                                                    self.append(*ev)
                                                self.append(END, QName('del'), (None, -1, -1))
                                                ins_tag_attrs = Attrs()
                                                if diff_id:
                                                    ins_tag_attrs = ins_tag_attrs | [(QName(getattr(self.config, 'diff_id_attr', 'data-diff-id')), self._new_diff_id())]
                                                self.append(START, (QName('ins'), ins_tag_attrs), (None, -1, -1))
                                                for ev in li_evs[1:-1]:
                                                    self.append(*ev)
                                                self.append(END, QName('ins'), (None, -1, -1))
                                        elif old_li_evs and old_li_evs[1:-1] != li_evs[1:-1]:
                                            # Inner content changed (e.g. <i> wrapper added): use EventDiffer
                                            inner = _EventDiffer(old_li_evs[1:-1], li_evs[1:-1], self.config, diff_id_state=self._diff_id_state)
                                            for ev in inner.get_diff_events():
                                                self.append(*ev)
                                        else:
                                            for ev in li_evs[1:-1]:
                                                self.append(*ev)
                                        self.leave(li_evs[-1][2], li_evs[-1][1])

                                # Close ol/ul
                                self.leave((None, -1, -1), list_qname)

                            k = scan_k
                            continue
                    if found_structural:
                        continue

            # Handle reverse: delete/replace(ol/ul START...) → equal(li↔p)... → delete/replace(...ol/ul END)
            # Use elif when tag is 'replace' to avoid double-processing
            if tag == 'delete' or (tag == 'replace' and not list_tag):
                list_start_ev = None
                list_tag = None
                # For delete: scan old atoms; for replace: also scan old atoms
                for ai in range(i1, i2):
                    a = self._old_atoms[ai]
                    evs = a.get('events', [])
                    if len(evs) == 1 and evs[0][0] == START:
                        lname = qname_localname(evs[0][1][0])
                        if lname in ('ol', 'ul'):
                            list_start_ev = evs[0]
                            list_tag = lname
                            break

                if list_tag:
                    bullet_equal_ranges = []
                    scan_k = k + 1
                    found_structural = False
                    while scan_k < len(opcodes):
                        s_tag, s_i1, s_i2, s_j1, s_j2 = opcodes[scan_k]
                        if s_tag == 'equal':
                            all_li_to_p = True
                            has_block = False
                            for ai in range(s_i1, s_i2):
                                old_a = self._old_atoms[ai]
                                if old_a.get('kind') == 'text':
                                    continue
                                if old_a.get('kind') == 'block' and old_a.get('tag') == 'li':
                                    has_block = True
                                    continue
                                all_li_to_p = False
                                break
                            if all_li_to_p:
                                for nj in range(s_j1, s_j2):
                                    new_a = self._new_atoms[nj]
                                    if new_a.get('kind') == 'text':
                                        continue
                                    if new_a.get('kind') == 'block' and new_a.get('tag') == 'p':
                                        has_block = True
                                        continue
                                    all_li_to_p = False
                                    break
                            if all_li_to_p and has_block:
                                bullet_equal_ranges.append((scan_k, s_tag, s_i1, s_i2, s_j1, s_j2))
                                scan_k += 1
                                continue
                            else:
                                break
                        elif s_tag in ('delete', 'replace'):
                            end_ev = None
                            for ai in range(s_i1, s_i2):
                                a = self._old_atoms[ai]
                                evs = a.get('events', [])
                                if len(evs) == 1 and evs[0][0] == END:
                                    if qname_localname(evs[0][1]) == list_tag:
                                        end_ev = evs[0]
                                        break
                            if end_ev and bullet_equal_ranges:
                                old_li_atoms = []
                                new_p_atoms = []
                                # Collect new atoms from the initial replace (e.g. new <p> replacing ul START)
                                if tag == 'replace':
                                    for nj in range(j1, j2):
                                        if self._new_atoms[nj].get('kind') == 'block':
                                            new_p_atoms.append(self._new_atoms[nj])
                                for _, _, eq_i1, eq_i2, eq_j1, eq_j2 in bullet_equal_ranges:
                                    for ai in range(eq_i1, eq_i2):
                                        if self._old_atoms[ai].get('kind') == 'block':
                                            old_li_atoms.append(self._old_atoms[ai])
                                    for nj in range(eq_j1, eq_j2):
                                        if self._new_atoms[nj].get('kind') == 'block':
                                            new_p_atoms.append(self._new_atoms[nj])
                                # Collect new atoms from the end replace too
                                if s_tag == 'replace':
                                    for nj in range(s_j1, s_j2):
                                        if self._new_atoms[nj].get('kind') == 'block':
                                            new_p_atoms.append(self._new_atoms[nj])

                                if old_li_atoms and new_p_atoms:
                                    with self.diff_group():
                                        diff_id = self._new_diff_id() if getattr(self.config, 'add_diff_ids', False) else None

                                        # Emit <ol/ul class="tagdiff_deleted">
                                        list_qname = list_start_ev[1][0]
                                        list_attrs = list_start_ev[1][1]
                                        list_attrs = self.inject_class(list_attrs, 'tagdiff_deleted')
                                        if diff_id:
                                            list_attrs = self._set_attr(list_attrs, getattr(self.config, 'diff_id_attr', 'data-diff-id'), diff_id)
                                        self.enter(list_start_ev[2], list_qname, list_attrs)

                                        # Emit each <li class="diff-bullet-del">
                                        for li_atom in old_li_atoms:
                                            li_evs = li_atom.get('events', [])
                                            if li_evs and li_evs[0][0] == START:
                                                li_tag = li_evs[0][1][0]
                                                li_attrs = li_evs[0][1][1]
                                                li_attrs = self.inject_class(li_attrs, 'diff-bullet-del')
                                                if diff_id:
                                                    li_attrs = self._set_attr(li_attrs, getattr(self.config, 'diff_id_attr', 'data-diff-id'), diff_id)
                                                self.enter(li_evs[0][2], li_tag, li_attrs)
                                                for ev in li_evs[1:-1]:
                                                    self.append(*ev)
                                                self.leave(li_evs[-1][2], li_evs[-1][1])

                                        # Close ol/ul
                                        self.leave(end_ev[2], end_ev[1])

                                        # Emit hidden <ins class="structural-revert-data"> with new <p> events
                                        revert_events = concat_events(new_p_atoms)
                                        ins_attrs = Attrs([(QName('class'), 'structural-revert-data'),
                                                           (QName('style'), 'display:none')])
                                        if diff_id:
                                            ins_attrs = ins_attrs | [(QName(getattr(self.config, 'diff_id_attr', 'data-diff-id')), diff_id)]
                                        self.append(START, (QName('ins'), ins_attrs), (None, -1, -1))
                                        for ev in revert_events:
                                            self.append(*ev)
                                        self.append(END, QName('ins'), (None, -1, -1))

                                    k = scan_k + 1
                                    found_structural = True
                                    break
                            break
                        else:
                            break
                    if found_structural:
                        continue

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
                            new_lname = qname_localname(new_t)
                            
                            # Allow same-tag replacement OR specific structural tag swaps (ul <-> ol)
                            # This allows granular diffs inside the list (since we don't force block atomization)
                            # while maintaining valid HTML structure by just replacing the container tag.
                            is_allowed_swap = (lname == new_lname) or (lname in ('ul', 'ol') and new_lname in ('ul', 'ol'))
                            
                            if is_allowed_swap and lname in structural_tags and new_lname in structural_tags:
                                if lname in ('ul', 'ol') and new_lname in ('ul', 'ol'):
                                    # List type/style change (ol↔ul swap or same-tag attr change)
                                    # Use structural diff: diff-bullet-ins + structural-revert-data
                                    end_idx_old = None
                                    end_idx_new = None
                                    # Find END atom for old list (search for old tag name)
                                    depth = 1
                                    for oi in range(i1 + 1, len(self._old_atoms)):
                                        for ev in self._old_atoms[oi].get('events', []):
                                            if ev[0] == START and qname_localname(ev[1][0]) == lname:
                                                depth += 1
                                            elif ev[0] == END and qname_localname(ev[1]) == lname:
                                                depth -= 1
                                                if depth == 0:
                                                    end_idx_old = oi
                                        if end_idx_old is not None:
                                            break
                                    # Find END atom for new list (use new_lname for ul↔ol case)
                                    depth = 1
                                    for ni in range(j1 + 1, len(self._new_atoms)):
                                        for ev in self._new_atoms[ni].get('events', []):
                                            if ev[0] == START and qname_localname(ev[1][0]) == new_lname:
                                                depth += 1
                                            elif ev[0] == END and qname_localname(ev[1]) == new_lname:
                                                depth -= 1
                                                if depth == 0:
                                                    end_idx_new = ni
                                        if end_idx_new is not None:
                                            break

                                    if end_idx_old is not None and end_idx_new is not None:
                                        # Collect old list atoms (full list) for revert data
                                        old_list_atoms = self._old_atoms[i1:end_idx_old + 1]
                                        # Collect new LI atoms for bullet display
                                        new_li_atoms = [a for a in self._new_atoms[j1 + 1:end_idx_new]
                                                        if a.get('tag') == 'li']
                                        # Collect old LI atoms for attr comparison
                                        old_li_atoms = [a for a in self._old_atoms[i1 + 1:end_idx_old]
                                                        if a.get('tag') == 'li']

                                        if new_li_atoms:
                                            with self.diff_group():
                                                diff_id = self._new_diff_id() if getattr(self.config, 'add_diff_ids', False) else None

                                                # Emit hidden <del class="structural-revert-data"> with old list
                                                revert_events = concat_events(old_list_atoms)
                                                del_attrs = Attrs([(QName('class'), 'structural-revert-data'),
                                                                   (QName('style'), 'display:none')])
                                                if diff_id:
                                                    del_attrs = del_attrs | [(QName(getattr(self.config, 'diff_id_attr', 'data-diff-id')), diff_id)]
                                                self.append(START, (QName('del'), del_attrs), (None, -1, -1))
                                                for ev in revert_events:
                                                    self.append(*ev)
                                                self.append(END, QName('del'), (None, -1, -1))

                                                # Determine if this is a bullet-visual change:
                                                # - tag swap (ul↔ol): bullets change (dots→numbers)
                                                # - list-style-type changed: bullets change (1,2,3→I,II,III)
                                                # Font/color-only changes are NOT bullet changes.
                                                def _get_lst(style_val):
                                                    for p in (style_val or '').split(';'):
                                                        p = p.strip()
                                                        if p.lower().startswith('list-style-type'):
                                                            return p.split(':', 1)[1].strip().lower()
                                                    return None
                                                old_lst = _get_lst(old_attrs.get('style'))
                                                new_lst = _get_lst(new_ev[1][1].get('style'))
                                                is_bullet_change = (old_t != new_t) or (old_lst != new_lst and (old_lst is not None or new_lst is not None))

                                                # Emit new list with appropriate class
                                                list_qname = new_ev[1][0]
                                                list_attrs_new = new_ev[1][1]
                                                if is_bullet_change:
                                                    list_attrs_new = self.inject_class(list_attrs_new, 'tagdiff_added')
                                                    if old_t != new_t:
                                                        list_attrs_new = list_attrs_new | [(QName('data-old-tag'), qname_localname(old_t))]
                                                else:
                                                    list_attrs_new = self.inject_class(list_attrs_new, 'tagdiff_replaced')
                                                # Track container attr changes (e.g. style: Arial→Comic Sans)
                                                list_attrs_new = self.inject_refattr(list_attrs_new, old_attrs)
                                                if diff_id:
                                                    list_attrs_new = self._set_attr(list_attrs_new, getattr(self.config, 'diff_id_attr', 'data-diff-id'), diff_id)
                                                self.enter(new_ev[2], list_qname, list_attrs_new)

                                                # Compute inherited style diff from list container
                                                # (for propagating font changes down to li del/ins)
                                                _INHERITABLE = ('font-family', 'font-size', 'font-style', 'font-weight', 'color')
                                                old_list_style = old_attrs.get('style', '')
                                                new_list_style = new_ev[1][1].get('style', '')
                                                def _parse_css(s):
                                                    d = {}
                                                    for p in s.split(';'):
                                                        p = p.strip()
                                                        if ':' in p:
                                                            k, v = p.split(':', 1)
                                                            d[k.strip().lower()] = v.strip()
                                                    return d
                                                old_css = _parse_css(old_list_style)
                                                new_css = _parse_css(new_list_style)
                                                inherited_changed = {}
                                                for prop in _INHERITABLE:
                                                    if old_css.get(prop) != new_css.get(prop) and (prop in old_css or prop in new_css):
                                                        # Use old value if it existed, otherwise 'initial'
                                                        # to prevent del from inheriting the new value
                                                        inherited_changed[prop] = old_css.get(prop) or 'initial'

                                                # Emit each LI
                                                for li_idx, li_atom in enumerate(new_li_atoms):
                                                    li_evs = li_atom.get('events', [])
                                                    if li_evs and li_evs[0][0] == START:
                                                        li_tag = li_evs[0][1][0]
                                                        li_attrs = li_evs[0][1][1]
                                                        if is_bullet_change:
                                                            li_attrs = self.inject_class(li_attrs, 'diff-bullet-ins')

                                                        # Check if this LI has attr changes vs old
                                                        old_li_evs = None
                                                        li_style_changed = False
                                                        if li_idx < len(old_li_atoms):
                                                            old_li_evs = old_li_atoms[li_idx].get('events', [])
                                                            if old_li_evs and old_li_evs[0][0] == START:
                                                                old_li_attrs = old_li_evs[0][1][1]
                                                                li_attrs = self.inject_refattr(li_attrs, old_li_attrs)
                                                                li_style_changed = (old_li_attrs != li_evs[0][1][1])

                                                        if diff_id:
                                                            li_attrs = self._set_attr(li_attrs, getattr(self.config, 'diff_id_attr', 'data-diff-id'), diff_id)
                                                        self.enter(li_evs[0][2], li_tag, li_attrs)

                                                        if li_style_changed and old_li_evs:
                                                            # Style changed: inline del(old style) + ins(new style)
                                                            # Put old style on <del> so text renders with old font
                                                            old_style_val = old_li_attrs.get('style')
                                                            with self.diff_group():
                                                                del_tag_attrs = Attrs()
                                                                if old_style_val:
                                                                    del_tag_attrs = del_tag_attrs | [(QName('style'), old_style_val)]
                                                                if diff_id:
                                                                    del_tag_attrs = del_tag_attrs | [(QName(getattr(self.config, 'diff_id_attr', 'data-diff-id')), self._new_diff_id())]
                                                                self.append(START, (QName('del'), del_tag_attrs), (None, -1, -1))
                                                                for ev in old_li_evs[1:-1]:
                                                                    self.append(*ev)
                                                                self.append(END, QName('del'), (None, -1, -1))

                                                                ins_tag_attrs = Attrs()
                                                                if diff_id:
                                                                    ins_tag_attrs = ins_tag_attrs | [(QName(getattr(self.config, 'diff_id_attr', 'data-diff-id')), self._new_diff_id())]
                                                                self.append(START, (QName('ins'), ins_tag_attrs), (None, -1, -1))
                                                                for ev in li_evs[1:-1]:
                                                                    self.append(*ev)
                                                                self.append(END, QName('ins'), (None, -1, -1))
                                                        elif old_li_evs and old_li_evs[1:-1] != li_evs[1:-1]:
                                                            # Inner content changed (e.g. <i> wrapper): use EventDiffer
                                                            inner = _EventDiffer(old_li_evs[1:-1], li_evs[1:-1], self.config, diff_id_state=self._diff_id_state)
                                                            for ev in inner.get_diff_events():
                                                                self.append(*ev)
                                                        elif inherited_changed and old_li_evs:
                                                            # List container style changed with inheritable props
                                                            # (e.g. font-family added) but li content is identical.
                                                            # Emit del(old inherited style)/ins.
                                                            old_li_style = old_li_attrs.get('style', '') if li_idx < len(old_li_atoms) else ''
                                                            old_li_css = _parse_css(old_li_style)
                                                            # Add inherited props that the old li didn't explicitly have
                                                            merged = dict(old_li_css)
                                                            for prop, val in inherited_changed.items():
                                                                if prop not in merged:
                                                                    merged[prop] = val
                                                            merged_style = '; '.join(f'{k}: {v}' for k, v in merged.items()) if merged else ''
                                                            with self.diff_group():
                                                                del_tag_attrs = Attrs()
                                                                if merged_style:
                                                                    del_tag_attrs = del_tag_attrs | [(QName('style'), merged_style)]
                                                                if diff_id:
                                                                    del_tag_attrs = del_tag_attrs | [(QName(getattr(self.config, 'diff_id_attr', 'data-diff-id')), self._new_diff_id())]
                                                                self.append(START, (QName('del'), del_tag_attrs), (None, -1, -1))
                                                                for ev in old_li_evs[1:-1]:
                                                                    self.append(*ev)
                                                                self.append(END, QName('del'), (None, -1, -1))
                                                                ins_tag_attrs = Attrs()
                                                                if diff_id:
                                                                    ins_tag_attrs = ins_tag_attrs | [(QName(getattr(self.config, 'diff_id_attr', 'data-diff-id')), self._new_diff_id())]
                                                                self.append(START, (QName('ins'), ins_tag_attrs), (None, -1, -1))
                                                                for ev in li_evs[1:-1]:
                                                                    self.append(*ev)
                                                                self.append(END, QName('ins'), (None, -1, -1))
                                                        else:
                                                            # No change: just emit content directly
                                                            for ev in li_evs[1:-1]:
                                                                self.append(*ev)

                                                        self.leave(li_evs[-1][2], li_evs[-1][1])

                                                # Close list
                                                end_ev_atoms = self._new_atoms[end_idx_new].get('events', [])
                                                if end_ev_atoms:
                                                    self.leave(end_ev_atoms[0][2], end_ev_atoms[0][1])

                                            # Skip all consumed opcodes
                                            while k + 1 < len(opcodes):
                                                next_tag, next_i1, next_i2, next_j1, next_j2 = opcodes[k + 1]
                                                if next_i1 <= end_idx_old or next_j1 <= end_idx_new:
                                                    k += 1
                                                else:
                                                    break
                                            k += 1
                                            continue

                                elif lname == new_lname and old_attrs != new_attrs:
                                    # Other structural tags (table, tr, etc) - use tagdiff_replaced
                                    self.enter_mark_replaced(new_ev[2], new_t, new_attrs, old_attrs, old_tag=old_t)
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


