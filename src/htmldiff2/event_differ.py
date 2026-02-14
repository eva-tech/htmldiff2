# -*- coding: utf-8 -*-
"""
Event differ for handling replace blocks.

This module contains the _EventDiffer class, which is used internally
for replace blocks, operating directly on event lists. It bypasses
atomization to avoid recursive re-grouping side-effects.

NOTE: This module must be imported AFTER StreamDiffer is defined in differ.py
to avoid circular imports. The class will be imported at the end of differ.py.
"""
from __future__ import with_statement

from difflib import SequenceMatcher
from genshi.core import QName, Attrs, START, END, TEXT

from .config import DiffConfig, INLINE_FORMATTING_TAGS
from .utils import (
    qname_localname, collapse_ws, has_visual_attrs
)
from .normalization import (
    normalize_opcodes_for_delete_first,
    normalize_inline_wrapper_opcodes,
    normalize_inline_wrapper_tag_change_opcodes,
    should_force_visual_replace
)


def create_event_differ_class(StreamDiffer):
    """
    Create the _EventDiffer class with StreamDiffer as base class.
    
    This function is called from differ.py after StreamDiffer is defined
    to avoid circular imports.
    """
    class _EventDiffer(StreamDiffer):
        """
        Internal differ used for replace blocks, operating directly on event lists.
        It bypasses atomization to avoid recursive re-grouping side-effects.
        """

        def __init__(self, old_events, new_events, config, diff_id_state=None):
            self.config = config or DiffConfig()
            # IMPORTANT: Keep original TEXT events intact and let diff_text() handle
            # word-level granularity. Splitting TEXT here can cause insertions to
            # appear "inside" deletions for phrase replacements.
            self._old_events = list(old_events)
            self._new_events = list(new_events)
            self._old_atoms = None
            self._new_atoms = None
            self._result = []
            self._stack = []
            self._context = None
            self._skip_end_for = []
            self._wrap_change_end_for = []  # stack of (localname, change_tag) for void elements (e.g. img)
            self._diff_id_state = diff_id_state if diff_id_state is not None else [0]
            self._diff_id_stack = []
            self._style_del_buffer = []

        def get_diff_events(self):
            self.process_events()
            return self._result

        def _handle_table_cell_wrapper_pattern(self, opcodes, k):
            """
            Maneja el patrón especial de tabla donde se agrega un wrapper inline estilizado
            alrededor del texto existente de una celda/encabezado.
            """
            if k + 2 >= len(opcodes):
                return False
            
            t1, a1, a2, b1, b2 = opcodes[k]
            t2, c1, c2, d1, d2 = opcodes[k + 1]
            t3, _e1, _e2, f1, f2 = opcodes[k + 2]
            
            # Pattern: replace(START) + equal(TEXT) + insert(wrapper END)
            if not (t1 == 'replace' and t2 == 'equal' and t3 == 'insert' and 
                    (a2 - a1) == 1 and (c2 - c1) == 1 and (d2 - d1) == 1 and (b2 - b1) >= 2):
                return False
            
            old_start_ev = self._old_events[a1]
            old_text_ev = self._old_events[c1]
            new_text_ev = self._new_events[d1]
            
            if not (old_start_ev[0] == START and old_text_ev[0] == TEXT and new_text_ev[0] == TEXT):
                return False
            
            new_start_ev = self._new_events[b1]
            if new_start_ev[0] != START:
                return False
            
            cont_tag, cont_attrs_new = new_start_ev[1]
            cont_l = qname_localname(cont_tag)
            if cont_l not in ('th', 'td'):
                return False
            
            # Find wrapper with visual attrs
            wrapper_idx = None
            wrapper_tag = None
            wrapper_attrs = None
            for j in range(b1 + 1, b2):
                ev = self._new_events[j]
                if ev[0] == START:
                    w_tag, w_attrs = ev[1]
                    w_l = qname_localname(w_tag)
                    if w_l in INLINE_FORMATTING_TAGS and has_visual_attrs(w_attrs, self.config):
                        wrapper_idx = j
                        wrapper_tag, wrapper_attrs = w_tag, w_attrs
            
            if wrapper_idx is None or (f2 - f1) < 1:
                return False
            
            end_ev = self._new_events[f1]
            if not (end_ev[0] == END and qname_localname(end_ev[1]) == qname_localname(wrapper_tag)):
                return False
            
            if collapse_ws(old_text_ev[1]) != collapse_ws(new_text_ev[1]):
                return False
            
            # Render the pattern
            old_cont_attrs = old_start_ev[1][1]
            self.enter_mark_replaced(new_start_ev[2], cont_tag, cont_attrs_new, old_cont_attrs)
            # whitespace between container and wrapper
            for j in range(b1 + 1, wrapper_idx):
                self.append(*self._new_events[j])
            # wrapper START marked replaced
            w_attrs2 = Attrs(list(wrapper_attrs))
            w_attrs2 = self.inject_class(w_attrs2, 'tagdiff_replaced')
            w_attrs2 |= [(QName('data-old-tag'), 'none')]
            if getattr(self.config, 'add_diff_ids', False):
                diff_id = self._active_diff_id() or self._new_diff_id()
                w_attrs2 = self._set_attr(w_attrs2, getattr(self.config, 'diff_id_attr', 'data-diff-id'), diff_id)
            self.enter(self._new_events[wrapper_idx][2], wrapper_tag, w_attrs2)
            # shared TEXT once
            self.append(TEXT, old_text_ev[1], new_text_ev[2])
            # close wrapper and emit remaining insert tail (indentation)
            self.leave(end_ev[2], end_ev[1])
            for j in range(f1 + 1, f2):
                self.append(*self._new_events[j])
            
            return True

        def process_events(self):
            """Process events and generate diff output."""
            # Fast path: treat visual-only container changes as a single replace so we can
            # render del->ins even when SequenceMatcher only flags the START tag.
            if should_force_visual_replace(self._old_events, self._new_events, self.config):
                self.replace(0, len(self._old_events), 0, len(self._new_events))
                self.leave_all()
                return

            matcher = SequenceMatcher(None, self._old_events, self._new_events)
            opcodes = matcher.get_opcodes()
            if getattr(self.config, 'delete_first', True):
                opcodes = normalize_opcodes_for_delete_first(opcodes)
            opcodes = normalize_inline_wrapper_opcodes(opcodes, self._old_events, self._new_events)
            opcodes = normalize_inline_wrapper_tag_change_opcodes(opcodes, self._old_events, self._new_events, self.config)

            # Table-aware special-case: styled inline wrapper added around an existing
            # cell/header text should be marked as tagdiff_replaced without duplicating
            # the text (no del+ins copy).

            k = 0
            while k < len(opcodes):
                # Try to handle table cell wrapper pattern
                if self._handle_table_cell_wrapper_pattern(opcodes, k):
                    k += 3
                    continue

                tag, i1, i2, j1, j2 = opcodes[k]
                if tag == 'replace':
                    self.replace(i1, i2, j1, j2)
                elif tag == 'delete':
                    with self.diff_group():
                        self.delete(i1, i2)
                elif tag == 'insert':
                    with self.diff_group():
                        self.insert(j1, j2)
                else:
                    self.unchanged(i1, i2)
                k += 1
            self.leave_all()
    
    return _EventDiffer
