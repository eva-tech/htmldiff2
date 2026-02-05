# -*- coding: utf-8 -*-
"""
Block event processing logic.

This module contains functions for processing block-level events during diffing.
It handles START, END, and TEXT events within block contexts.
"""
from __future__ import with_statement

from genshi.core import QName, Attrs, START, END, TEXT

from .config import BLOCK_WRAPPER_TAGS
from .utils import qname_localname
from .text_differ import mark_text


def handle_start_event(differ, tag, attrs, pos):
    """Handle START events within block_process."""
    lname = qname_localname(tag)
    # Visualize <br> changes explicitly
    if lname == 'br' and differ._context in ('ins', 'del'):
        marker = getattr(differ.config, 'linebreak_marker', u'\u00b6')
        if differ._context == 'ins':
            # <ins>¶</ins><br>
            mark_text(differ, pos, marker, 'ins')
            differ.enter(pos, tag, attrs)
        else:
            # <del>¶<br></del> (put <br> inside <del> so it gets deleted properly)
            # Create <del> tag manually so we can put <br> inside before closing
            change_tag = QName('del')
            change_attrs = differ._change_attrs(diff_id=differ._active_diff_id())
            differ.append(START, (change_tag, change_attrs), pos)
            # Put the marker text inside
            differ.append(TEXT, marker, pos)
            # Put the <br> INSIDE the <del> tag
            differ.append(START, (tag, attrs), pos)
            differ.append(END, tag, pos)
            # Now close the <del>
            differ.append(END, change_tag, pos)
            differ._skip_end_for.append(lname)
        return True

    # For structural tags that shouldn't be wrapped in <ins>/<del> (to keep HTML valid
    # and avoid "ghost" cells/bullets), we inject a special class into the tag itself.
    structural_tags = ('table', 'thead', 'tbody', 'tfoot', 'tr', 'td', 'th', 'ul', 'ol', 'li')
    if lname in structural_tags and differ._context in ('ins', 'del'):
        suffix = 'added' if differ._context == 'ins' else 'deleted'
        attrs = differ.inject_class(attrs, 'tagdiff_' + suffix)
        if getattr(differ.config, 'add_diff_ids', False):
            diff_id = differ._active_diff_id() or differ._new_diff_id()
            attrs = differ._set_attr(attrs, getattr(differ.config, 'diff_id_attr', 'data-diff-id'), diff_id)
        
        # Use enter() which preserves the tag but with our new class.
        # We don't return True here because we WANT the children to be processed
        # normally (wrapped in <ins>/<del> for text) to maintain visual consistency.
        differ.enter(pos, tag, attrs)
        return True
        
    # Wrap block wrappers (p, h1, etc.) so the whole element is deleted/inserted.
    # This prevents "empty tags" remaining after accept/reject (e.g. <p><del>...</del></p> -> <p></p>).
    if lname in BLOCK_WRAPPER_TAGS and differ._context in ('ins', 'del'):
        change_tag = QName(differ._context)
        
        # Special Check: Are we inserting/deleting a block element directly inside a List?
        # e.g. <ul><del><p>...</p></del></ul> is invalid. It should be <ul><li><del><p>...</p></del></li></ul>.
        # Convert context-less blocks into proper list items if needed.
        if differ._stack and qname_localname(differ._stack[-1]) in ('ul', 'ol') and lname != 'li':
            # Inject Synthetic LI wrapper
            li_tag = QName('li')
            li_attrs = Attrs([
                (QName('class'), 'tagdiff_added' if differ._context == 'ins' else 'tagdiff_deleted')
            ])
            if getattr(differ.config, 'add_diff_ids', False):
                 diff_id = differ._active_diff_id() or differ._new_diff_id()
                 li_attrs = differ._set_attr(li_attrs, getattr(differ.config, 'diff_id_attr', 'data-diff-id'), diff_id)

            differ.append(START, (li_tag, li_attrs), pos)
            # Ensure we close this LI after the block ends
            differ._wrap_change_end_for.append((lname, li_tag, None))

        differ.append(START, (change_tag, differ._change_attrs(diff_id=differ._active_diff_id())), pos)
        differ.enter(pos, tag, attrs)
        # Store context to restore effectively (we clear it so nested text isn't double wrapped)
        differ._wrap_change_end_for.append((lname, change_tag, differ._context))
        differ._context = None 
        return True

    # Wrap void/non-textual elements (e.g. <img>) with <ins>/<del> so the
    # change is visible even though there is no TEXT to mark.
    wrap_void = set(getattr(differ.config, 'wrap_void_tag_changes_with_ins_del', ()))
    if lname in wrap_void and differ._context in ('ins', 'del'):
        change_tag = QName(differ._context)
        differ.append(START, (change_tag, differ._change_attrs(diff_id=differ._active_diff_id())), pos)
        differ.enter(pos, tag, attrs)
        differ._wrap_change_end_for.append((lname, change_tag, None))
        return True

    differ.enter(pos, tag, attrs)
    return False


def handle_end_event(differ, data, pos):
    """Handle END events within block_process."""
    lname = qname_localname(data)
    if differ._skip_end_for and differ._skip_end_for[-1] == lname:
        differ._skip_end_for.pop()
        return True

    # Close wrapper <ins>/<del> (and potentially synthetic parents like <li>) 
    # for wrapped void or block elements after their END.
    # We use a loop because we might have multiple wrappers (e.g. <li><del>... for an invalid child).
    handled_leave = False
    while differ._wrap_change_end_for and differ._wrap_change_end_for[-1][0] == lname:
        _lname, change_tag, restore_ctx = differ._wrap_change_end_for.pop()
        
        if not handled_leave:
            differ.leave(pos, data)
            handled_leave = True
            
        if change_tag:
            differ.append(END, change_tag, pos)
        if restore_ctx is not None:
            differ._context = restore_ctx
    
    if handled_leave:
        return True

    differ.leave(pos, data)
    return False


def handle_text_event(differ, data, pos):
    """Handle TEXT events within block_process."""
    if differ._context is not None:
        # Wrap visible text AND inline whitespace (e.g. the space between words)
        # so inserts like "en negrita" highlight the whole phrase including space.
        # Avoid wrapping newline indentation, which would create noisy diffs.
        if data.strip() or ('\n' not in data and '\r' not in data):
            mark_text(differ, pos, data, differ._context)
            return
    differ.append(TEXT, data, pos)


def block_process(differ, events):
    """Process block-level events."""
    for event in events:
        event_type, data, pos = event
        if event_type == START:
            tag, attrs = data
            if handle_start_event(differ, tag, attrs, pos):
                continue
        elif event_type == END:
            if handle_end_event(differ, data, pos):
                continue
        elif event_type == TEXT:
            handle_text_event(differ, data, pos)
        else:
            differ.append(event_type, data, pos)
