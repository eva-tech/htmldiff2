# -*- coding: utf-8 -*-
"""
Clases principales para realizar diffs de streams de Genshi.
"""
from __future__ import with_statement

import re
from difflib import SequenceMatcher
from itertools import chain
from contextlib import contextmanager
from genshi.core import Stream, QName, Attrs, START, END, TEXT

from .config import DiffConfig, text_type, _leading_space_re, _diff_split_re, _token_split_re, INLINE_FORMATTING_TAGS, BLOCK_WRAPPER_TAGS
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


class InsensitiveSequenceMatcher(SequenceMatcher):
    """
    SequenceMatcher that ignores very small matching blocks.
    
    This prevents "shredded" diffs where unrelated texts get word-by-word
    interleaving due to incidental small matches (e.g., "del" matching "DE").
    """
    
    def __init__(self, isjunk=None, a='', b='', threshold=2):
        super().__init__(isjunk, a, b)
        self.threshold = threshold
    
    def get_matching_blocks(self):
        # Dynamically adjust threshold based on sequence size to avoid
        # over-filtering on very short sequences.
        size = min(len(self.a), len(self.b))
        effective_threshold = min(self.threshold, size // 4)
        
        blocks = super().get_matching_blocks()
        # Keep blocks larger than threshold, or the sentinel (size=0) at the end.
        return [block for block in blocks 
                if block[2] > effective_threshold or block[2] == 0]


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

    def text_split(self, text):
        """
        Tokenize text for diffing.

        We prefer a tokenizer that separates punctuation (e.g. "CAD" vs "CAD.")
        so pure punctuation edits become insert/delete instead of replace that
        accidentally includes adjacent whitespace.
        """
        if getattr(self.config, 'tokenize_text', True):
            rx = getattr(self.config, 'tokenize_regex', _token_split_re)
            parts = [p for p in rx.split(text) if p != u'']
            return parts
        # Legacy behavior: keep words glued to following whitespace
        worditer = chain([u''], _diff_split_re.split(text))
        return [x + next(worditer) for x in worditer]

    def cut_leading_space(self, s):
        match = _leading_space_re.match(s)
        if match is None:
            return u'', s
        return match.group(), s[match.end():]

    def mark_text(self, pos, text, tag, diff_id=None):
        def _make_ws_visible(s):
            # Convert whitespace that would otherwise be collapsed by HTML into NBSPs,
            # but keep single mid-string spaces intact for readability.
            if not s:
                return s
            # Leading / trailing spaces: always NBSP
            s = re.sub(r'^\s+', lambda m: u'\u00a0' * len(m.group(0)), s, flags=re.U)
            s = re.sub(r'\s+$', lambda m: u'\u00a0' * len(m.group(0)), s, flags=re.U)
            # Runs of 2+ spaces inside: NBSP for the run
            s = re.sub(r' {2,}', lambda m: u'\u00a0' * len(m.group(0)), s)
            return s

        tag = QName(tag)
        preserve_ws = getattr(self.config, 'preserve_whitespace_in_diff', True) and qname_localname(tag) in ('del', 'ins')
        if preserve_ws:
            text = _make_ws_visible(text)
            attrs = self._change_attrs(diff_id=diff_id)
            self.append(START, (tag, attrs), pos)
            self.append(TEXT, text, pos)
            self.append(END, tag, pos)
            return

        ws, text = self.cut_leading_space(text)
        if ws:
            self.append(TEXT, ws, pos)
        attrs = self._change_attrs(diff_id=diff_id)
        self.append(START, (tag, attrs), pos)
        self.append(TEXT, text, pos)
        self.append(END, tag, pos)

    def diff_text(self, pos, old_text, new_text):
        old = self.text_split(old_text)
        new = self.text_split(new_text)
        threshold = getattr(self.config, 'sequence_match_threshold', 2)
        matcher = InsensitiveSequenceMatcher(None, old, new, threshold=threshold)

        def wrap(tag, words, diff_id=None):
            return self.mark_text(pos, u''.join(words), tag, diff_id=diff_id)

        # Enforce deterministic delete->insert ordering within each changed region.
        # SequenceMatcher can produce patterns like delete/insert/delete (e.g. when
        # a middle token changes), which renders as insertion "inside" deletion.
        pending_del = []
        pending_ins = []

        def flush_pending():
            if pending_del and pending_ins:
                # Pair del+ins under the same diff-id for per-change frontend actions.
                diff_id = self._new_diff_id() if getattr(self.config, 'add_diff_ids', False) else None
                wrap('del', pending_del, diff_id=diff_id)
                del pending_del[:]
                wrap('ins', pending_ins, diff_id=diff_id)
                del pending_ins[:]
                return
            if pending_del:
                wrap('del', pending_del, diff_id=(self._new_diff_id() if getattr(self.config, 'add_diff_ids', False) else None))
                del pending_del[:]
            if pending_ins:
                wrap('ins', pending_ins, diff_id=(self._new_diff_id() if getattr(self.config, 'add_diff_ids', False) else None))
                del pending_ins[:]

        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == 'equal':
                flush_pending()
                self.append(TEXT, u''.join(old[i1:i2]), pos)
                continue
            if tag == 'replace':
                # Special-case: whitespace-only "replace" where the only change is the
                # multiplicity of spaces (e.g. "   " -> " "). SequenceMatcher tends
                # to emit this as replace, which renders as del+ins. For EdenAI we
                # prefer a minimal representation: keep the common whitespace
                # unchanged, and only mark the extra spaces as deleted/inserted.
                old_part = u''.join(old[i1:i2])
                new_part = u''.join(new[j1:j2])
                if (
                    old_part
                    and new_part
                    and old_part.strip() == u''
                    and new_part.strip() == u''
                    and ('\n' not in old_part and '\r' not in old_part)
                    and ('\n' not in new_part and '\r' not in new_part)
                ):
                    flush_pending()
                    common_len = 0
                    max_common = min(len(old_part), len(new_part))
                    while common_len < max_common and old_part[common_len] == new_part[common_len]:
                        common_len += 1
                    if common_len:
                        self.append(TEXT, new_part[:common_len], pos)
                    old_rem = old_part[common_len:]
                    new_rem = new_part[common_len:]
                    if old_rem:
                        pending_del.append(old_rem)
                    if new_rem:
                        pending_ins.append(new_rem)
                    continue
                pending_del.extend(old[i1:i2])
                pending_ins.extend(new[j1:j2])
            elif tag == 'delete':
                pending_del.extend(old[i1:i2])
            elif tag == 'insert':
                pending_ins.extend(new[j1:j2])
            else:
                pass
        flush_pending()

    def _handle_replace_special_cases(self, old, new, old_start, old_end, new_start, new_end):
        """Maneja casos especiales de reemplazo antes del procesamiento general."""
        # Special-case: one inline wrapper removed/changed while keeping a shared
        # prefix/suffix. This prevents over-highlighting unchanged prefix text
        # (e.g. "Texto " in underline_removal) and keeps del->ins ordering.
        if self._try_inline_wrapper_to_plain(old, new):
            return True

        # Special-case: visual-only wrapper added/removed around identical text,
        # where the wrapper carries visual styling (style/class/id). In tables this
        # happens a lot (<td>10.8</td> -> <td><strong style=...>10.8</strong></td>).
        # Rendering this as del+ins duplicates the same value and looks terrible.
        # Instead, render a single copy and mark the wrapper as "replaced".
        if self._try_visual_wrapper_toggle_without_dup(old, new):
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
        if self._can_unwrap_wrapper(old, new):
            with self.diff_group():
                self.delete(old_start, old_end)
                self.insert(new_start, new_end)
            return True

        # Special-case: visual-only changes (same text, different attrs/tag).
        # Required to mark font-size/font-weight/class/style/id changes as diffs even
        # when text is identical.
        if self._can_visual_container_replace(old, new):
            if getattr(self.config, 'visual_replace_inline', True):
                # Render inline del->ins while keeping styles, so changes like
                # font-size/font-weight don't turn into separate block lines.
                with self.diff_group():
                    self._render_visual_replace_inline(old, new)
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
            self.diff_text(pos, old_event[1], new_text)
        else:
            self.append(*new_event)

    def _handle_mismatched_event_types(self, old_event, new_event, old_start, old_end, 
                                       new_start, new_end, idx):
        """Maneja eventos de tipos diferentes."""
        # If the old event was text and the new one is the start or end of a tag
        if old_event[0] == TEXT and new_event[0] in (START, END):
            _, text, pos = old_event
            self.mark_text(pos, text, 'del')
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

    def _try_visual_wrapper_toggle_without_dup(self, old_events, new_events):
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
            if not has_visual_attrs(n_attrs, self.config):
                return False
            if collapse_ws(o_text_ev[1]) != collapse_ws(extract_text_from_events(n_inner)):
                return False
            for ev in n_lws:
                self.append(*ev)
            # Genshi Attrs is list-like, not dict-like
            attrs2 = Attrs(list(n_attrs))
            attrs2 = self.inject_class(attrs2, 'tagdiff_replaced')
            attrs2 |= [(QName('data-old-tag'), 'none')]
            if getattr(self.config, 'add_diff_ids', False):
                diff_id = self._active_diff_id() or self._new_diff_id()
                attrs2 = self._set_attr(attrs2, getattr(self.config, 'diff_id_attr', 'data-diff-id'), diff_id)
            pos = (n_inner[0][2] if n_inner else (new_events[0][2] if new_events else old_events[0][2]))
            self.append(START, (n_tag, attrs2), pos)
            for ev in n_inner:
                self.append(*ev)
            self.append(END, n_tag, pos)
            for ev in n_tws:
                self.append(*ev)
            return True

        # Removal: styled wrapper -> plain
        if o[0] == 'wrap' and n[0] == 'plain':
            _o_kind, _o_lws, o_inner, _o_tws, (_o_tag, o_attrs), o_lname = o
            _n_kind, n_lws, n_text_ev, n_tws, _n_tagattrs, _n_lname = n
            if not has_visual_attrs(o_attrs, self.config):
                return False
            if collapse_ws(extract_text_from_events(o_inner)) != collapse_ws(n_text_ev[1]):
                return False
            for ev in n_lws:
                self.append(*ev)
            span_tag = QName('span')
            span_attrs = Attrs()
            span_attrs |= [(QName('data-old-tag'), o_lname)]
            span_attrs = self.inject_refattr(span_attrs, o_attrs)
            span_attrs = self.inject_class(span_attrs, 'tagdiff_replaced')
            if getattr(self.config, 'add_diff_ids', False):
                diff_id = self._active_diff_id() or self._new_diff_id()
                span_attrs = self._set_attr(span_attrs, getattr(self.config, 'diff_id_attr', 'data-diff-id'), diff_id)
            self.append(START, (span_tag, span_attrs), n_text_ev[2])
            self.append(*n_text_ev)
            self.append(END, span_tag, n_text_ev[2])
            for ev in n_tws:
                self.append(*ev)
            return True

        return False

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
        for event in events:
            event_type, data, pos = event
            if event_type == START:
                tag, attrs = data
                if self._handle_start_event_in_block(tag, attrs, pos):
                    continue
            elif event_type == END:
                if self._handle_end_event_in_block(data, pos):
                    continue
            elif event_type == TEXT:
                self._handle_text_event_in_block(data, pos)
            else:
                self.append(event_type, data, pos)

    def _handle_start_event_in_block(self, tag, attrs, pos):
        """Maneja eventos START dentro de block_process."""
        lname = qname_localname(tag)
        # Visualize <br> changes explicitly
        if lname == 'br' and self._context in ('ins', 'del'):
            marker = getattr(self.config, 'linebreak_marker', u'\u00b6')
            if self._context == 'ins':
                # <ins>¶</ins><br>
                self.mark_text(pos, marker, 'ins')
                self.enter(pos, tag, attrs)
            else:
                # <del>¶<br></del> (put <br> inside <del> so it gets deleted properly)
                # Create <del> tag manually so we can put <br> inside before closing
                change_tag = QName('del')
                change_attrs = self._change_attrs(diff_id=self._active_diff_id())
                self.append(START, (change_tag, change_attrs), pos)
                # Put the marker text inside
                self.append(TEXT, marker, pos)
                # Put the <br> INSIDE the <del> tag
                self.append(START, (tag, attrs), pos)
                self.append(END, tag, pos)
                # Now close the <del>
                self.append(END, change_tag, pos)
                self._skip_end_for.append(lname)
            return True

        # For structural tags that shouldn't be wrapped in <ins>/<del> (to keep HTML valid
        # and avoid "ghost" cells/bullets), we inject a special class into the tag itself.
        structural_tags = ('table', 'thead', 'tbody', 'tfoot', 'tr', 'td', 'th', 'ul', 'ol', 'li')
        if lname in structural_tags and self._context in ('ins', 'del'):
            suffix = 'added' if self._context == 'ins' else 'deleted'
            attrs = self.inject_class(attrs, 'tagdiff_' + suffix)
            if getattr(self.config, 'add_diff_ids', False):
                diff_id = self._active_diff_id() or self._new_diff_id()
                attrs = self._set_attr(attrs, getattr(self.config, 'diff_id_attr', 'data-diff-id'), diff_id)
            
            # Use enter() which preserves the tag but with our new class.
            # We don't return True here because we WANT the children to be processed
            # normally (wrapped in <ins>/<del> for text) to maintain visual consistency.
            self.enter(pos, tag, attrs)
            return True
            
        # Wrap block wrappers (p, h1, etc.) so the whole element is deleted/inserted.
        # This prevents "empty tags" remaining after accept/reject (e.g. <p><del>...</del></p> -> <p></p>).
        if lname in BLOCK_WRAPPER_TAGS and self._context in ('ins', 'del'):
            change_tag = QName(self._context)
            self.append(START, (change_tag, self._change_attrs(diff_id=self._active_diff_id())), pos)
            self.enter(pos, tag, attrs)
            # Store context to restore effectively (we clear it so nested text isn't double wrapped)
            self._wrap_change_end_for.append((lname, change_tag, self._context))
            self._context = None 
            return True

        # Wrap void/non-textual elements (e.g. <img>) with <ins>/<del> so the
        # change is visible even though there is no TEXT to mark.
        wrap_void = set(getattr(self.config, 'wrap_void_tag_changes_with_ins_del', ()))
        if lname in wrap_void and self._context in ('ins', 'del'):
            change_tag = QName(self._context)
            self.append(START, (change_tag, self._change_attrs(diff_id=self._active_diff_id())), pos)
            self.enter(pos, tag, attrs)
            self._wrap_change_end_for.append((lname, change_tag, None))
            return True

        self.enter(pos, tag, attrs)
        return False

    def _handle_end_event_in_block(self, data, pos):
        """Maneja eventos END dentro de block_process."""
        lname = qname_localname(data)
        if self._skip_end_for and self._skip_end_for[-1] == lname:
            self._skip_end_for.pop()
            return True

        # Close wrapper <ins>/<del> for wrapped void or block elements after their END.
        if self._wrap_change_end_for and self._wrap_change_end_for[-1][0] == lname:
            _lname, change_tag, restore_ctx = self._wrap_change_end_for.pop()
            self.leave(pos, data)
            if change_tag:
                self.append(END, change_tag, pos)
            if restore_ctx is not None:
                self._context = restore_ctx
            return True

        self.leave(pos, data)
        return False

    def _handle_text_event_in_block(self, data, pos):
        """Maneja eventos TEXT dentro de block_process."""
        if self._context is not None:
            # Wrap visible text AND inline whitespace (e.g. the space between words)
            # so inserts like "en negrita" highlight the whole phrase including space.
            # Avoid wrapping newline indentation, which would create noisy diffs.
            if data.strip() or ('\n' not in data and '\r' not in data):
                self.mark_text(pos, data, self._context)
                return
        self.append(TEXT, data, pos)

    def _extract_direct_tr_cells(self, tr_events):
        """
        Extract direct child <td>/<th> blocks from a <tr> event slice.

        Returns a list of dicts: { 'tag': 'td'|'th', 'events': [...], 'attrs': Attrs }.
        """
        cells = []
        i = 0
        n = len(tr_events)
        while i < n:
            etype, data, _pos = tr_events[i]
            if etype == START:
                tag, attrs = data
                lname = qname_localname(tag)
                if lname in ('td', 'th'):
                    # Find matching END for this cell
                    depth = 1
                    j = i + 1
                    while j < n and depth:
                        t2, d2, _p2 = tr_events[j]
                        if t2 == START and qname_localname(d2[0]) == lname:
                            depth += 1
                        elif t2 == END and qname_localname(d2) == lname:
                            depth -= 1
                        j += 1
                    block = tr_events[i:j]
                    cells.append({'tag': lname, 'events': block, 'attrs': attrs})
                    i = j
                    continue
            i += 1
        return cells

    def _extract_tr_blocks(self, table_events):
        """Extract all <tr> blocks found within a <table> event slice (thead/tbody included)."""
        blocks = []
        i = 0
        n = len(table_events)
        while i < n:
            etype, data, _pos = table_events[i]
            if etype == START:
                tag, _attrs = data
                lname = qname_localname(tag)
                if lname == "tr":
                    depth = 1
                    j = i + 1
                    while j < n and depth:
                        t2, d2, _p2 = table_events[j]
                        if t2 == START and qname_localname(d2[0]) == "tr":
                            depth += 1
                        elif t2 == END and qname_localname(d2) == "tr":
                            depth -= 1
                        j += 1
                    blocks.append(table_events[i:j])
                    i = j
                    continue
            i += 1
        return blocks

    def _row_key(self, tr_events):
        """Key to align rows across table changes (based on first 2 cells' text)."""
        cells = self._extract_direct_tr_cells(tr_events)
        if not cells:
            return ("", "")
        def _cell_txt(c):
            return collapse_ws(extract_text_from_events(c["events"]))
        c0 = _cell_txt(cells[0]) if len(cells) > 0 else ""
        c1 = _cell_txt(cells[1]) if len(cells) > 1 else ""
        return (c0, c1)

    def _diff_table_by_rows(self, old_table_events, new_table_events):
        """
        Diff a table by aligning rows (<tr>) and diffing each row by cells.

        This keeps the output HTML valid even when the LLM restyles the table/tag
        attributes, and ensures column removals are handled by our row-aware
        `_diff_tr_by_cells` logic.
        """
        if not old_table_events or not new_table_events:
            inner = _EventDiffer(old_table_events, new_table_events, self.config, diff_id_state=self._diff_id_state)
            for ev in inner.get_diff_events():
                self.append(*ev)
            return

        # Emit the table wrapper from the OLD side (keeps structure valid; inner diffs
        # will mark style/attr changes at cell level).
        self.append(*old_table_events[0])

        old_rows = self._extract_tr_blocks(old_table_events)
        new_rows = self._extract_tr_blocks(new_table_events)
        old_keys = [self._row_key(r) for r in old_rows]
        new_keys = [self._row_key(r) for r in new_rows]

        matcher = SequenceMatcher(None, old_keys, new_keys)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                for oi, nj in zip(range(i1, i2), range(j1, j2)):
                    self._diff_tr_by_cells(old_rows[oi], new_rows[nj])
            elif tag == "delete":
                with self.diff_group():
                    with self.context("del"):
                        for oi in range(i1, i2):
                            self.block_process(old_rows[oi])
            elif tag == "insert":
                with self.diff_group():
                    with self.context("ins"):
                        for nj in range(j1, j2):
                            self.block_process(new_rows[nj])
            else:  # replace
                # Pair rows positionally where possible
                n = min(i2 - i1, j2 - j1)
                for k in range(n):
                    self._diff_tr_by_cells(old_rows[i1 + k], new_rows[j1 + k])
                if (i2 - i1) > n:
                    with self.diff_group():
                        with self.context("del"):
                            for oi in range(i1 + n, i2):
                                self.block_process(old_rows[oi])
                if (j2 - j1) > n:
                    with self.diff_group():
                        with self.context("ins"):
                            for nj in range(j1 + n, j2):
                                self.block_process(new_rows[nj])

        # Emit closing </table> from OLD wrapper.
        self.append(*old_table_events[-1])

    def _cell_key(self, cell):
        """Key used to align table cells inside a row."""
        lname = cell['tag']
        block_events = cell['events']
        attrs = cell.get('attrs')
        block_text = collapse_ws(extract_text_from_events(block_events))
        # Match mostly by visible text + structure; attrs included to allow visual-only diffs.
        return (lname, block_text, attrs_signature(attrs, self.config), structure_signature(block_events, self.config))

    def _diff_tr_by_cells(self, old_tr_events, new_tr_events):
        """
        Diff a table row by aligning direct child cells (<td>/<th>) with a row-aware
        algorithm that prefers preserving left-to-right structure.

        This avoids SequenceMatcher's tendency to misalign duplicate values (e.g. "8", "8")
        when a column is removed/inserted, which otherwise causes the wrong cell/column
        to be marked as deleted and can break the table when changes are applied.
        """
        # Defensive: if the slice doesn't look like a <tr> block, fall back.
        if not old_tr_events or not new_tr_events:
            inner = _EventDiffer(old_tr_events, new_tr_events, self.config, diff_id_state=self._diff_id_state)
            for ev in inner.get_diff_events():
                self.append(*ev)
            return

        # Emit the <tr> wrapper (keep old wrapper; attributes rarely matter here).
        self.append(*old_tr_events[0])

        old_cells = self._extract_direct_tr_cells(old_tr_events)
        new_cells = self._extract_direct_tr_cells(new_tr_events)
        # Two key types:
        # - align key (text-only): used to keep column alignment stable even if the LLM
        #   adds styling/attributes (border/padding) everywhere.
        # - full key (includes attrs/structure): used only when deciding whether we
        #   need to render a replace vs. let EventDiffer mark attribute diffs.
        def _align_key(cell):
            return (
                cell["tag"],
                collapse_ws(extract_text_from_events(cell["events"])),
            )

        old_align = [_align_key(c) for c in old_cells]
        new_align = [_align_key(c) for c in new_cells]

        def _diff_cell_pair(old_cell, new_cell):
            """Diff one old/new cell (td/th), preserving structure."""
            # If the visible text is the same, prefer an inner diff so style/attrs
            # changes do NOT shift column alignment.
            if _align_key(old_cell) == _align_key(new_cell):
                inner = _EventDiffer(old_cell['events'], new_cell['events'], self.config, diff_id_state=self._diff_id_state)
                for ev in inner.get_diff_events():
                    self.append(*ev)
                return
            with self.diff_group():
                with self.context('del'):
                    self.block_process(old_cell['events'])
                with self.context('ins'):
                    self.block_process(new_cell['events'])

        def _best_single_delete_index(oldk, newk):
            """
            Choose which index to delete from old (len(old)=len(new)+1) to best
            preserve left-to-right alignment. Important when many cells are empty
            (empty keys are identical and would otherwise drift).
            """
            best_k = 0
            best_score = -1
            new_len = len(newk)
            for k in range(len(oldk)):
                score = 0
                # prefix
                for i0 in range(min(k, new_len)):
                    if oldk[i0] == newk[i0]:
                        score += 1
                # suffix: old[k+1:] aligns with new[k:]
                for i0 in range(k, new_len):
                    if oldk[i0 + 1] == newk[i0]:
                        score += 1
                if score > best_score:
                    best_score = score
                    best_k = k
            return best_k

        def _best_single_insert_index(oldk, newk):
            """
            Choose which index to insert into old (len(new)=len(old)+1) by selecting
            the index in new that best preserves alignment (i.e. delete that index
            from new yields best match).
            """
            best_k = 0
            best_score = -1
            old_len = len(oldk)
            for k in range(len(newk)):
                score = 0
                # prefix
                for i0 in range(min(k, old_len)):
                    if oldk[i0] == newk[i0]:
                        score += 1
                # suffix: old[k:] aligns with new[k+1:]
                for i0 in range(k, old_len):
                    if oldk[i0] == newk[i0 + 1]:
                        score += 1
                if score > best_score:
                    best_score = score
                    best_k = k
            return best_k

        # Special-case: single-column removal/addition. Do a positional alignment
        # with a stable chosen index, instead of key-based matching that can drift
        # across identical empty cells.
        if len(old_cells) == len(new_cells) + 1:
            k = _best_single_delete_index(old_align, new_align)
            # diff cells before k
            for idx in range(k):
                if idx < len(new_cells):
                    _diff_cell_pair(old_cells[idx], new_cells[idx])
                else:
                    with self.diff_group():
                        with self.context('del'):
                            self.block_process(old_cells[idx]['events'])
            # delete the removed column cell
            with self.diff_group():
                with self.context('del'):
                    self.block_process(old_cells[k]['events'])
            # diff remaining cells after k (shifted left by one)
            for idx in range(k, len(new_cells)):
                _diff_cell_pair(old_cells[idx + 1], new_cells[idx])
            self.append(*old_tr_events[-1])
            return

        if len(new_cells) == len(old_cells) + 1:
            k = _best_single_insert_index(old_align, new_align)
            # diff cells before k
            for idx in range(k):
                if idx < len(old_cells):
                    _diff_cell_pair(old_cells[idx], new_cells[idx])
                else:
                    with self.diff_group():
                        with self.context('ins'):
                            self.block_process(new_cells[idx]['events'])
            # insert the added column cell
            with self.diff_group():
                with self.context('ins'):
                    self.block_process(new_cells[k]['events'])
            # diff remaining cells after k (shifted right by one in new)
            for idx in range(k, len(old_cells)):
                _diff_cell_pair(old_cells[idx], new_cells[idx + 1])
            self.append(*old_tr_events[-1])
            return

        i = 0
        j = 0
        while i < len(old_cells) or j < len(new_cells):
            if i < len(old_cells) and j < len(new_cells) and old_align[i] == new_align[j]:
                # Same cell -> inner diff to catch formatting/text changes.
                inner = _EventDiffer(old_cells[i]['events'], new_cells[j]['events'], self.config, diff_id_state=self._diff_id_state)
                for ev in inner.get_diff_events():
                    self.append(*ev)
                i += 1
                j += 1
                continue

            old_remaining = len(old_cells) - i
            new_remaining = len(new_cells) - j

            if i < len(old_cells) and old_remaining > new_remaining:
                # Prefer deleting from old when old has extra cells (common: column removal).
                with self.diff_group():
                    with self.context('del'):
                        self.block_process(old_cells[i]['events'])
                i += 1
                continue

            if j < len(new_cells) and new_remaining > old_remaining:
                # Prefer inserting when new has extra cells (column insertion).
                with self.diff_group():
                    with self.context('ins'):
                        self.block_process(new_cells[j]['events'])
                j += 1
                continue

            # Same remaining length but different keys => treat as replace (paired).
            if i < len(old_cells) and j < len(new_cells):
                with self.diff_group():
                    with self.context('del'):
                        self.block_process(old_cells[i]['events'])
                    with self.context('ins'):
                        self.block_process(new_cells[j]['events'])
                i += 1
                j += 1
                continue

            # Only one side has cells left
            if i < len(old_cells):
                with self.diff_group():
                    with self.context('del'):
                        self.block_process(old_cells[i]['events'])
                i += 1
            elif j < len(new_cells):
                with self.diff_group():
                    with self.context('ins'):
                        self.block_process(new_cells[j]['events'])
                j += 1

        # Emit closing </tr> from old wrapper.
        self.append(*old_tr_events[-1])

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
            self._diff_tr_by_cells(old_atoms_slice[0]['events'], new_atoms_slice[0]['events'])
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
            self._diff_table_by_rows(old_atoms_slice[0]["events"], new_atoms_slice[0]["events"])
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
                    self._diff_tr_by_cells(a_old['events'], a_new['events'])
                elif a_new.get('tag') == 'table':
                    self._diff_table_by_rows(a_old['events'], a_new['events'])
                else:
                    inner = _EventDiffer(a_old['events'], a_new['events'], self.config, diff_id_state=self._diff_id_state)
                    for ev in inner.get_diff_events():
                        self.append(*ev)
            else:
                old_events = a_old.get('events') or []
                new_events = a_new.get('events') or []

                # Visual-only attribute changes (same text, different style/class/attrs)
                # should still produce a visible diff even when atom keys match.
                if old_events != new_events and self._can_visual_container_replace(old_events, new_events):
                    with self.diff_group():
                        self._render_visual_replace_inline(old_events, new_events)
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

    def _can_unwrap_wrapper(self, old_events, new_events):
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

    def _can_visual_container_replace(self, old_events, new_events):
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

        allowed = set(getattr(self.config, 'visual_container_tags', ()))
        if old_lname not in allowed and new_lname not in allowed:
            return False

        old_txt = extract_text_from_events(old_events)
        new_txt = extract_text_from_events(new_events)
        if not old_txt or not new_txt:
            return False
        if collapse_ws(old_txt) != collapse_ws(new_txt):
            return False

        # Same visible text but different inline formatting structure (e.g. strong removed)
        if structure_signature(old_events, self.config) != structure_signature(new_events, self.config):
            return True

        # If tag differs OR any tracked attribute differs, treat as visual change
        if old_lname != new_lname:
            return True
        for attr in getattr(self.config, 'track_attrs', ('style', 'class', 'src', 'href')):
            if old_attrs.get(attr) != new_attrs.get(attr):
                return True
        # Also consider id as a common visual/selection attribute in product HTML
        if old_attrs.get('id') != new_attrs.get('id'):
            return True

        return False

    def _wrap_inline_visual_replace(self, kind, wrapper_tag, attrs, inner_events, pos):
        """Envuelve eventos internos en un wrapper inline para reemplazo visual."""
        kind_tag = QName(kind)
        wrapper_q = wrapper_tag
        self.append(START, (kind_tag, self._change_attrs(diff_id=self._active_diff_id())), pos)
        self.append(START, (wrapper_q, attrs), pos)
        # Render inner events verbatim (including <br>, <strong>, etc.)
        with self.context(None):
            self.block_process(inner_events)
        self.append(END, wrapper_q, pos)
        self.append(END, kind_tag, pos)

    def _wrap_block_visual_replace(self, kind, wrapper_tag, attrs, inner_events, pos):
        """
        Keep HTML valid by not nesting block tags inside <ins>/<del>.
        Instead:
          <p style=old><del>TEXT</del></p>
          <p style=new><ins>TEXT</ins></p>
        """
        kind_tag = QName(kind)
        wrapper_q = wrapper_tag
        self.append(START, (wrapper_q, attrs), pos)
        self.append(START, (kind_tag, self._change_attrs(diff_id=self._active_diff_id())), pos)
        # Emit inner events without wrapping again (we are already inside <ins>/<del>),
        # but convert <br> into a visible marker so double line breaks show an empty
        # line with ¶ even when the change is "visual-only".
        marker = getattr(self.config, 'linebreak_marker', u'\u00b6')
        skip_br_end = 0
        for et, d, p2 in inner_events:
            if skip_br_end and et == END and qname_localname(d) == 'br':
                skip_br_end -= 1
                continue
            if et == START:
                ttag, tattrs = d
                if qname_localname(ttag) == 'br':
                    # inside <ins>/<del>, so plain TEXT marker is enough
                    self.append(TEXT, marker, p2)
                    self.append(START, (ttag, tattrs), p2)
                    self.append(END, ttag, p2)
                    skip_br_end += 1
                    continue
            self.append(et, d, p2)
        self.append(END, kind_tag, pos)
        self.append(END, wrapper_q, pos)

    def _render_visual_replace_inline(self, old_events, new_events):
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
            self.append(*ev)

        old_events = old_core
        new_events = new_core
        if not old_events or not new_events:
            # fallback
            self.delete(0, 0)
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
            self.append(START, (new_tag, new_attrs), pos)
            # Then emit del/ins of content
            self._wrap_inline_visual_replace('del', QName('span'), old_attrs, old_inner, pos)
            self._wrap_inline_visual_replace('ins', QName('span'), new_attrs, new_inner, pos)
            self.append(END, new_tag, pos)
        else:
            if old_l in BLOCK_WRAPPER_TAGS:
                self._wrap_block_visual_replace('del', old_wrap, old_attrs, old_inner, pos)
            else:
                self._wrap_inline_visual_replace('del', old_wrap, old_attrs, old_inner, pos)

            if new_l in BLOCK_WRAPPER_TAGS:
                self._wrap_block_visual_replace('ins', new_wrap, new_attrs, new_inner, pos)
            else:
                self._wrap_inline_visual_replace('ins', new_wrap, new_attrs, new_inner, pos)

        for ev in tws_new:
            self.append(*ev)

    def _find_inline_wrapper_bounds(self, events):
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

    def _validate_prefix_suffix_alignment(self, prefix_text, suffix_text, old_text, new_text):
        """Valida que el prefijo y sufijo común estén alineados correctamente."""
        pre_len = longest_common_prefix_len(old_text, new_text)
        suf_len = longest_common_suffix_len(old_text, new_text, max_prefix=pre_len)
        return pre_len == len(prefix_text) and suf_len == len(suffix_text)

    def _try_inline_wrapper_to_plain(self, old_events, new_events):
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
        start_idx, end_idx = self._find_inline_wrapper_bounds(old_events)
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
        if not self._validate_prefix_suffix_alignment(prefix_text, suffix_text, old_text, new_text):
            return False

        # Compute common prefix/suffix on raw strings
        pre_len = longest_common_prefix_len(old_text, new_text)
        suf_len = longest_common_suffix_len(old_text, new_text, max_prefix=pre_len)

        # Remaining new text that replaces the wrapper subtree
        mid_new = new_text[pre_len:len(new_text) - suf_len if suf_len else len(new_text)]

        # Emit prefix unchanged
        if prefix_text:
            pos = (prefix_events[-1][2] if prefix_events else new_events[0][2])
            self.append(TEXT, prefix_text, pos)

        # Emit deletion preserving wrapper formatting, then insertion of the replacement text
        with self.context('del'):
            self.block_process(wrapper_events)
        if mid_new:
            self.mark_text(new_events[0][2], mid_new, 'ins')

        # Emit suffix unchanged
        if suffix_text:
            self.append(TEXT, suffix_text, new_events[0][2])

        return True


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


