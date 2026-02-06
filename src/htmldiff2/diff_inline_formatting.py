# -*- coding: utf-8 -*-
"""
diff_inline_formatting.py

Specialized diff function for inline formatting changes where text is preserved
but wrapped in new tags (e.g. adding <strong> around titles).

Approach:
1. Extract text with position/formatting info from both old and new events
2. Build "text spans" with associated formatting metadata
3. Diff the pure text to find aligned regions
4. Emit output with correct formatting for each region
"""
from genshi.core import QName, Attrs, START, END, TEXT
from difflib import SequenceMatcher

from .utils import qname_localname, collapse_ws
from .config import INLINE_FORMATTING_TAGS


def extract_text_spans(events):
    """
    Extract text spans from events, tracking which inline formatting tags wrap each span.
    
    Returns list of dicts with:
    - text: the actual text content
    - formatting: list of (tag, attrs) for inline wrappers currently active
    - start_char: starting character index in concatenated text
    - end_char: ending character index
    """
    spans = []
    formatting_stack = []
    char_pos = 0
    
    for etype, data, pos in events:
        if etype == START:
            tag, attrs = data
            lname = qname_localname(tag)
            if lname in INLINE_FORMATTING_TAGS:
                formatting_stack.append((tag, attrs))
        
        elif etype == END:
            lname = qname_localname(data)
            if lname in INLINE_FORMATTING_TAGS and formatting_stack:
                for j in range(len(formatting_stack) - 1, -1, -1):
                    if qname_localname(formatting_stack[j][0]) == lname:
                        formatting_stack.pop(j)
                        break
        
        elif etype == TEXT:
            text = data or ''
            if text:
                spans.append({
                    'text': text,
                    'formatting': list(formatting_stack),
                    'start_char': char_pos,
                    'end_char': char_pos + len(text)
                })
                char_pos += len(text)
    
    return spans


def find_formatting_at_pos(spans, pos):
    """Find the formatting stack active at character position `pos`."""
    for span in spans:
        if span['start_char'] <= pos < span['end_char']:
            return span['formatting']
    return []


def emit_text_with_formatting(differ, text, formatting, pos, change_tag=None):
    """
    Emit text with given formatting wrappers.
    If change_tag is 'del' or 'ins', wrap the entire output in that marker.
    """
    if not text:
        return
    
    # Open change marker if needed
    if change_tag:
        diff_id = differ._new_diff_id() if getattr(differ.config, 'add_diff_ids', False) else None
        attrs = differ._change_attrs(diff_id=diff_id)
        differ.append(START, (QName(change_tag), attrs), pos)
    
    # Open formatting wrappers
    for tag, attrs in formatting:
        differ.append(START, (tag, attrs), pos)
    
    # Emit text
    differ.append(TEXT, text, pos)
    
    # Close formatting wrappers (reverse order)
    for tag, attrs in reversed(formatting):
        differ.append(END, tag, pos)
    
    # Close change marker
    if change_tag:
        differ.append(END, QName(change_tag), pos)


def diff_inline_formatting(differ, old_events, new_events):
    """
    Diff events where the pure text is the same but inline formatting differs.
    
    Returns True if handled, False if caller should use different approach.
    """
    # Extract children (skip container START/END)
    if len(old_events) > 2 and old_events[0][0] == START and old_events[-1][0] == END:
        old_children = old_events[1:-1]
    else:
        old_children = old_events
    
    if len(new_events) > 2 and new_events[0][0] == START and new_events[-1][0] == END:
        new_children = new_events[1:-1]
    else:
        new_children = new_events
    
    old_spans = extract_text_spans(old_children)
    new_spans = extract_text_spans(new_children)
    
    # Concatenate all text
    old_text = ''.join(s['text'] for s in old_spans)
    new_text = ''.join(s['text'] for s in new_spans)
    
    # If texts don't match, this isn't a pure formatting change
    if collapse_ws(old_text) != collapse_ws(new_text):
        return False
    
    # Texts match! Now emit with correct formatting.
    # Strategy: Walk through new text character by character.
    # For each region, check if formatting matches old. If not, emit del+ins pair.
    
    pos = (None, -1, -1)
    
    # Emit container start from new
    if len(new_events) > 2 and new_events[0][0] == START:
        cont_tag, cont_attrs = new_events[0][1]
        differ.enter(new_events[0][2], cont_tag, cont_attrs)
    
    # Walk through new spans and compare with old formatting at each position
    for span in new_spans:
        text = span['text']
        new_fmt = span['formatting']
        start_pos = span['start_char']
        
        # Find old formatting at this position
        old_fmt = find_formatting_at_pos(old_spans, start_pos)
        
        # Compare formatting
        old_fmt_names = tuple(qname_localname(t) for t, a in old_fmt)
        new_fmt_names = tuple(qname_localname(t) for t, a in new_fmt)
        
        if old_fmt_names == new_fmt_names:
            # Formatting unchanged - emit plain text with new formatting
            emit_text_with_formatting(differ, text, new_fmt, pos, change_tag=None)
        else:
            # Formatting differs - emit del (old formatting) then ins (new formatting)
            # Find the old text at this position
            old_text_at_pos = ''
            for ospan in old_spans:
                if ospan['start_char'] <= start_pos < ospan['end_char']:
                    # Calculate overlap
                    overlap_start = max(ospan['start_char'], start_pos)
                    overlap_end = min(ospan['end_char'], span['end_char'])
                    if overlap_start < overlap_end:
                        rel_start = overlap_start - ospan['start_char']
                        rel_end = overlap_end - ospan['start_char']
                        old_text_at_pos = ospan['text'][rel_start:rel_end]
                        old_fmt = ospan['formatting']
                    break
            
            # Use diff_group to pair del+ins under same ID
            with differ.diff_group():
                emit_text_with_formatting(differ, old_text_at_pos or text, old_fmt, pos, change_tag='del')
                emit_text_with_formatting(differ, text, new_fmt, pos, change_tag='ins')
    
    # Emit container end from new
    if len(new_events) > 2 and new_events[-1][0] == END:
        differ.leave(new_events[-1][2], new_events[-1][1])
    
    return True


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    
    from .parser import parse_html
    
    old_html = "<p>TITLE: text here.</p>"
    new_html = "<p><strong>TITLE:</strong> text here.</p>"
    
    old_stream = list(parse_html(old_html.strip()))
    new_stream = list(parse_html(new_html.strip()))
    
    old_p = old_stream[1:-1]
    new_p = new_stream[1:-1]
    
    print("=== Testing extract_text_spans ===")
    old_spans = extract_text_spans(old_p[1:-1])
    new_spans = extract_text_spans(new_p[1:-1])
    
    print(f"\nOld spans:")
    for s in old_spans:
        print(f"  text={s['text']!r}, formatting={[qname_localname(t) for t,a in s['formatting']]}, chars=[{s['start_char']}:{s['end_char']}]")
    
    print(f"\nNew spans:")
    for s in new_spans:
        print(f"  text={s['text']!r}, formatting={[qname_localname(t) for t,a in s['formatting']]}, chars=[{s['start_char']}:{s['end_char']}]")
