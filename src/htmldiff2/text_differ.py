# -*- coding: utf-8 -*-
"""
Text diffing logic and helpers.

This module contains functions for tokenizing, splitting, and diffing text content,
as well as marking text with ins/del tags.
"""
from __future__ import with_statement

import re
from difflib import SequenceMatcher
from itertools import chain
from genshi.core import QName, START, END, TEXT

from .config import _leading_space_re, _diff_split_re, _token_split_re
from .utils import qname_localname


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


def text_split(differ, text):
    """
    Tokenize text for diffing.

    We prefer a tokenizer that separates punctuation (e.g. "CAD" vs "CAD.")
    so pure punctuation edits become insert/delete instead of replace that
    accidentally includes adjacent whitespace.
    """
    if getattr(differ.config, 'tokenize_text', True):
        rx = getattr(differ.config, 'tokenize_regex', _token_split_re)
        parts = [p for p in rx.split(text) if p != u'']
        return parts
    # Legacy behavior: keep words glued to following whitespace
    worditer = chain([u''], _diff_split_re.split(text))
    return [x + next(worditer) for x in worditer]


def cut_leading_space(s):
    """Cut leading whitespace from a string, returning (whitespace, rest)."""
    match = _leading_space_re.match(s)
    if match is None:
        return u'', s
    return match.group(), s[match.end():]


def mark_text(differ, pos, text, tag, diff_id=None):
    """
    Mark text with an ins/del tag, handling whitespace visibility.
    
    Args:
        differ: StreamDiffer instance
        pos: Position tuple for the event
        text: Text content to mark
        tag: Tag name ('ins' or 'del')
        diff_id: Optional diff ID for grouping
    """
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

    tag_qname = QName(tag)
    preserve_ws = getattr(differ.config, 'preserve_whitespace_in_diff', True) and qname_localname(tag_qname) in ('del', 'ins')
    if preserve_ws:
        text = _make_ws_visible(text)
        attrs = differ._change_attrs(diff_id=diff_id)
        differ.append(START, (tag_qname, attrs), pos)
        differ.append(TEXT, text, pos)
        differ.append(END, tag_qname, pos)
        return

    ws, text = cut_leading_space(text)
    if ws:
        differ.append(TEXT, ws, pos)
    attrs = differ._change_attrs(diff_id=diff_id)
    differ.append(START, (tag_qname, attrs), pos)
    differ.append(TEXT, text, pos)
    differ.append(END, tag_qname, pos)


def diff_text(differ, pos, old_text, new_text):
    """
    Diff two text strings, emitting ins/del markers for changes.
    
    Args:
        differ: StreamDiffer instance
        pos: Position tuple for events
        old_text: Original text
        new_text: New text
    """
    old = text_split(differ, old_text)
    new = text_split(differ, new_text)
    threshold = getattr(differ.config, 'sequence_match_threshold', 2)
    matcher = InsensitiveSequenceMatcher(None, old, new, threshold=threshold)

    def wrap(tag, words, diff_id=None):
        return mark_text(differ, pos, u''.join(words), tag, diff_id=diff_id)

    # Enforce deterministic delete->insert ordering within each changed region.
    # SequenceMatcher can produce patterns like delete/insert/delete (e.g. when
    # a middle token changes), which renders as insertion "inside" deletion.
    pending_del = []
    pending_ins = []

    def flush_pending():
        if pending_del and pending_ins:
            # Pair del+ins under the same diff-id for per-change frontend actions.
            diff_id = differ._new_diff_id() if getattr(differ.config, 'add_diff_ids', False) else None
            wrap('del', pending_del, diff_id=diff_id)
            del pending_del[:]
            wrap('ins', pending_ins, diff_id=diff_id)
            del pending_ins[:]
            return
        if pending_del:
            wrap('del', pending_del, diff_id=(differ._new_diff_id() if getattr(differ.config, 'add_diff_ids', False) else None))
            del pending_del[:]
        if pending_ins:
            wrap('ins', pending_ins, diff_id=(differ._new_diff_id() if getattr(differ.config, 'add_diff_ids', False) else None))
            del pending_ins[:]

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            flush_pending()
            differ.append(TEXT, u''.join(old[i1:i2]), pos)
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
                    differ.append(TEXT, new_part[:common_len], pos)
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
