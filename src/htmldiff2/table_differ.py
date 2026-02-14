# -*- coding: utf-8 -*-
"""
Table diffing logic.

This module contains functions for diffing tables, rows, and cells.
It handles column insertion/deletion, row alignment, and cell-level diffs.
"""
from __future__ import with_statement

from difflib import SequenceMatcher
from genshi.core import START, END, Attrs, QName

from .utils import (
    qname_localname, collapse_ws, extract_text_from_events,
    attrs_signature, structure_signature, normalize_style_value
)


def _attrs_equal_normalized(attrs1, attrs2):
    """Compare two Attrs objects with normalized style values (order-independent)."""
    def _normalize(attrs):
        result = {}
        try:
            for k, v in attrs:
                k_str = str(k)
                if k_str == 'style' and v:
                    result[k_str] = normalize_style_value(v)
                else:
                    result[k_str] = v
        except Exception:
            return attrs
        return result
    return _normalize(attrs1) == _normalize(attrs2)


def extract_direct_tr_cells(tr_events):
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


def extract_tr_blocks(table_events):
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


def row_key(tr_events):
    """Key to align rows across table changes (based on first 2 cells' text)."""
    cells = extract_direct_tr_cells(tr_events)
    if not cells:
        return ("", "")
    def _cell_txt(c):
        return collapse_ws(extract_text_from_events(c["events"]))
    c0 = _cell_txt(cells[0]) if len(cells) > 0 else ""
    c1 = _cell_txt(cells[1]) if len(cells) > 1 else ""
    return (c0, c1)


def cell_key(cell, config):
    """Key used to align table cells inside a row."""
    lname = cell['tag']
    block_events = cell['events']
    attrs = cell.get('attrs')
    block_text = collapse_ws(extract_text_from_events(block_events))
    # Match mostly by visible text + structure; attrs included to allow visual-only diffs.
    return (lname, block_text, attrs_signature(attrs, config), structure_signature(block_events, config))


def best_single_delete_index(oldk, newk):
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


def best_single_insert_index(oldk, newk):
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


# CSS properties that are inherited by child elements and thus relevant
# when comparing old table style vs new table style for del rendering.
_INHERITABLE_PROPS = ('font-family', 'font-size', 'font-style', 'font-weight', 'color')


def _merge_inherited_style(del_style_val, table_old_style):
    """Merge inheritable CSS props from old table style into the del style.

    When the old cell didn't explicitly set font-family (it inherited from the
    table), and the table's font changed, the <del> must carry the old
    table font so it renders in the font the text originally had.
    """
    if not table_old_style:
        return del_style_val
    # Parse table old style into dict
    table_props = {}
    for part in table_old_style.split(';'):
        part = part.strip()
        if ':' in part:
            k, v = part.split(':', 1)
            table_props[k.strip().lower()] = v.strip()
    # Parse del style into dict (preserving order)
    del_props = {}
    if del_style_val:
        for part in del_style_val.split(';'):
            part = part.strip()
            if ':' in part:
                k, v = part.split(':', 1)
                del_props[k.strip().lower()] = v.strip()
    # Merge inheritable props that are missing from del
    merged = False
    for prop in _INHERITABLE_PROPS:
        if prop in table_props and prop not in del_props:
            del_props[prop] = table_props[prop]
            merged = True
    if not merged:
        return del_style_val
    return '; '.join(f'{k}: {v}' for k, v in del_props.items())


def diff_tr_by_cells(differ, old_tr_events, new_tr_events, table_old_style=None):
    """
    Diff a table row by aligning direct child cells (<td>/<th>) with a row-aware
    algorithm that prefers preserving left-to-right structure.

    This avoids SequenceMatcher's tendency to misalign duplicate values (e.g. "8", "8")
    when a column is removed/inserted, which otherwise causes the wrong cell/column
    to be marked as deleted and can break the table when changes are applied.
    """
    # Import here to avoid circular import - _EventDiffer is created in differ.py
    from .differ import _EventDiffer
    
    # Defensive: if the slice doesn't look like a <tr> block, fall back.
    if not old_tr_events or not new_tr_events:
        inner = _EventDiffer(old_tr_events, new_tr_events, differ.config, diff_id_state=differ._diff_id_state)
        for ev in inner.get_diff_events():
            differ.append(*ev)
        return

    # Emit the <tr> wrapper (keep old wrapper; attributes rarely matter here).
    differ.append(*old_tr_events[0])

    old_cells = extract_direct_tr_cells(old_tr_events)
    new_cells = extract_direct_tr_cells(new_tr_events)
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
        """Diff one old/new cell (td/th), preserving structure.
        
        When text differs, emit a SINGLE cell wrapper with inline del/ins
        for the content, instead of two separate cells (which creates an extra column).
        When only style changes, emit inline del(old style)/ins(new style).
        """
        old_events = old_cell['events']
        new_events = new_cell['events']
        
        if not old_events or old_events[0][0] != START or old_events[-1][0] != END:
            # Fallback
            with differ.diff_group():
                with differ.context('del'):
                    differ.block_process(old_events)
                with differ.context('ins'):
                    differ.block_process(new_events)
            return

        old_attrs = old_events[0][1][1]
        new_attrs = new_events[0][1][1] if new_events and new_events[0][0] == START else old_attrs
        same_text = (_align_key(old_cell) == _align_key(new_cell))
        same_attrs = _attrs_equal_normalized(old_attrs, new_attrs)
        
        if same_text and same_attrs:
            if table_old_style:
                # Table style changed (inherited via CSS) but cell attrs identical.
                # Emit del(table old style)/ins so user sees old vs new font.
                cell_tag = new_events[0][1][0]
                cell_attrs = new_events[0][1][1]
                diff_id = differ._new_diff_id() if getattr(differ.config, 'add_diff_ids', False) else None
                if diff_id:
                    cell_attrs = differ._set_attr(cell_attrs, getattr(differ.config, 'diff_id_attr', 'data-diff-id'), diff_id)
                differ.append(START, (cell_tag, cell_attrs), new_events[0][2])

                old_content = old_events[1:-1]
                new_content = new_events[1:-1]

                with differ.diff_group():
                    del_attrs = Attrs([(QName('style'), table_old_style)])
                    inner_id = differ._new_diff_id() if diff_id else None
                    if inner_id:
                        del_attrs = del_attrs | [(QName(getattr(differ.config, 'diff_id_attr', 'data-diff-id')), inner_id)]
                    differ.append(START, (QName('del'), del_attrs), (None, -1, -1))
                    for ev in old_content:
                        differ.append(*ev)
                    differ.append(END, QName('del'), (None, -1, -1))

                    ins_attrs = Attrs()
                    if inner_id:
                        ins_attrs = ins_attrs | [(QName(getattr(differ.config, 'diff_id_attr', 'data-diff-id')), differ._new_diff_id())]
                    differ.append(START, (QName('ins'), ins_attrs), (None, -1, -1))
                    for ev in new_content:
                        differ.append(*ev)
                    differ.append(END, QName('ins'), (None, -1, -1))

                differ.append(*new_events[-1])
                return
            # Cell text and attrs are equal. However inner elements may have changed
            # (e.g. a <span> style change). Check if events are semantically identical
            # (normalize style order to avoid false positives).
            def _normalize_event(ev):
                if ev[0] == START:
                    tag, attrs = ev[1]
                    norm_attrs = tuple(
                        (str(k), normalize_style_value(v) if str(k) == 'style' else v)
                        for k, v in sorted(attrs, key=lambda x: str(x[0]))
                    )
                    return (ev[0], (str(tag), norm_attrs))
                return (ev[0], ev[1])

            old_norm = [_normalize_event(e) for e in old_events]
            new_norm = [_normalize_event(e) for e in new_events]
            if old_norm == new_norm:
                # Truly identical after normalization: emit as-is
                for ev in new_events:
                    differ.append(*ev)
                return
            # Inner events differ (style change on child element): use EventDiffer
            from .differ import _EventDiffer
            inner = _EventDiffer(old_events, new_events, differ.config, diff_id_state=differ._diff_id_state)
            for ev in inner.get_diff_events():
                differ.append(*ev)
            return
        
        if same_text and not same_attrs:
            # Style-only change: emit new cell wrapper (plain, no extra class),
            # then inline del(old style) + ins(new content).
            cell_tag = new_events[0][1][0]
            cell_attrs = new_events[0][1][1]
            diff_id = differ._new_diff_id() if getattr(differ.config, 'add_diff_ids', False) else None
            if diff_id:
                cell_attrs = differ._set_attr(cell_attrs, getattr(differ.config, 'diff_id_attr', 'data-diff-id'), diff_id)
            differ.append(START, (cell_tag, cell_attrs), new_events[0][2])
            
            old_content = old_events[1:-1]
            new_content = new_events[1:-1]
            
            with differ.diff_group():
                # Del with old style
                old_style_val = old_attrs.get('style')
                old_style_val = _merge_inherited_style(old_style_val, table_old_style)
                del_attrs = Attrs()
                if old_style_val:
                    del_attrs = del_attrs | [(QName('style'), old_style_val)]
                inner_diff_id = differ._new_diff_id() if diff_id else None
                if inner_diff_id:
                    del_attrs = del_attrs | [(QName(getattr(differ.config, 'diff_id_attr', 'data-diff-id')), inner_diff_id)]
                differ.append(START, (QName('del'), del_attrs), (None, -1, -1))
                for ev in old_content:
                    differ.append(*ev)
                differ.append(END, QName('del'), (None, -1, -1))
                
                # Ins inherits new style from cell wrapper
                ins_attrs = Attrs()
                if inner_diff_id:
                    ins_attrs = ins_attrs | [(QName(getattr(differ.config, 'diff_id_attr', 'data-diff-id')), differ._new_diff_id())]
                differ.append(START, (QName('ins'), ins_attrs), (None, -1, -1))
                for ev in new_content:
                    differ.append(*ev)
                differ.append(END, QName('ins'), (None, -1, -1))
            
            differ.append(*new_events[-1])
            return
        
        # Text differs: emit SINGLE cell with inline del/ins content.
        # Use new cell's wrapper (preserves new styles).
        cell_start = new_events[0]
        cell_end = new_events[-1]
        old_content = old_events[1:-1]
        new_content = new_events[1:-1] if len(new_events) > 2 else []
        
        # Emit new cell wrapper (plain, no extra class)
        cell_tag = cell_start[1][0]
        cell_attrs = cell_start[1][1]
        differ.append(START, (cell_tag, cell_attrs), cell_start[2])
        
        with differ.diff_group():
            if old_content:
                # Del with old style if style changed
                del_tag_attrs = Attrs()
                if not same_attrs:
                    old_style_val = _merge_inherited_style(old_attrs.get('style'), table_old_style)
                    if old_style_val:
                        del_tag_attrs = del_tag_attrs | [(QName('style'), old_style_val)]
                elif table_old_style:
                    old_style_val = _merge_inherited_style(None, table_old_style)
                    if old_style_val:
                        del_tag_attrs = del_tag_attrs | [(QName('style'), old_style_val)]
                diff_id = differ._new_diff_id() if getattr(differ.config, 'add_diff_ids', False) else None
                if diff_id:
                    del_tag_attrs = del_tag_attrs | [(QName(getattr(differ.config, 'diff_id_attr', 'data-diff-id')), diff_id)]
                differ.append(START, (QName('del'), del_tag_attrs), (None, -1, -1))
                for ev in old_content:
                    differ.append(*ev)
                differ.append(END, QName('del'), (None, -1, -1))
            if new_content:
                ins_attrs = Attrs()
                diff_id = differ._new_diff_id() if getattr(differ.config, 'add_diff_ids', False) else None
                if diff_id:
                    ins_attrs = ins_attrs | [(QName(getattr(differ.config, 'diff_id_attr', 'data-diff-id')), diff_id)]
                differ.append(START, (QName('ins'), ins_attrs), (None, -1, -1))
                for ev in new_content:
                    differ.append(*ev)
                differ.append(END, QName('ins'), (None, -1, -1))
        
        differ.append(*cell_end)

    # Special-case: single-column removal/addition. Do a positional alignment
    # with a stable chosen index, instead of key-based matching that can drift
    # across identical empty cells.
    if len(old_cells) == len(new_cells) + 1:
        k = best_single_delete_index(old_align, new_align)
        # diff cells before k
        for idx in range(k):
            if idx < len(new_cells):
                _diff_cell_pair(old_cells[idx], new_cells[idx])
            else:
                with differ.diff_group():
                    with differ.context('del'):
                        differ.block_process(old_cells[idx]['events'])
        # delete the removed column cell
        with differ.diff_group():
            with differ.context('del'):
                differ.block_process(old_cells[k]['events'])
        # diff remaining cells after k (shifted left by one)
        for idx in range(k, len(new_cells)):
            _diff_cell_pair(old_cells[idx + 1], new_cells[idx])
        differ.append(*old_tr_events[-1])
        return

    if len(new_cells) == len(old_cells) + 1:
        k = best_single_insert_index(old_align, new_align)
        # diff cells before k
        for idx in range(k):
            if idx < len(old_cells):
                _diff_cell_pair(old_cells[idx], new_cells[idx])
            else:
                with differ.diff_group():
                    with differ.context('ins'):
                        differ.block_process(new_cells[idx]['events'])
        # insert the added column cell
        with differ.diff_group():
            with differ.context('ins'):
                differ.block_process(new_cells[k]['events'])
        # diff remaining cells after k (shifted right by one in new)
        for idx in range(k, len(old_cells)):
            _diff_cell_pair(old_cells[idx], new_cells[idx + 1])
        differ.append(*old_tr_events[-1])
        return

    i = 0
    j = 0
    while i < len(old_cells) or j < len(new_cells):
        if i < len(old_cells) and j < len(new_cells) and old_align[i] == new_align[j]:
            # Same cell -> diff through _diff_cell_pair which normalizes styles.
            _diff_cell_pair(old_cells[i], new_cells[j])
            i += 1
            j += 1
            continue

        old_remaining = len(old_cells) - i
        new_remaining = len(new_cells) - j

        if i < len(old_cells) and old_remaining > new_remaining:
            # Prefer deleting from old when old has extra cells (common: column removal).
            with differ.diff_group():
                with differ.context('del'):
                    differ.block_process(old_cells[i]['events'])
            i += 1
            continue

        if j < len(new_cells) and new_remaining > old_remaining:
            # Prefer inserting when new has extra cells (column insertion).
            with differ.diff_group():
                with differ.context('ins'):
                    differ.block_process(new_cells[j]['events'])
            j += 1
            continue

        # Same remaining length but different keys => treat as replace (paired).
        # Use _diff_cell_pair to emit SINGLE cell with inline del/ins.
        if i < len(old_cells) and j < len(new_cells):
            _diff_cell_pair(old_cells[i], new_cells[j])
            i += 1
            j += 1
            continue

        # Fallback: emit unmatched cells
        if i < len(old_cells):
            with differ.diff_group():
                with differ.context('del'):
                    differ.block_process(old_cells[i]['events'])
            i += 1
        if j < len(new_cells):
            with differ.diff_group():
                with differ.context('ins'):
                    differ.block_process(new_cells[j]['events'])
            j += 1

    differ.append(*old_tr_events[-1])


def diff_table_by_rows(differ, old_table_events, new_table_events):
    """
    Diff a table by aligning rows (<tr>) and diffing each row by cells.

    This keeps the output HTML valid even when the LLM restyles the table/tag
    attributes, and ensures column removals are handled by our row-aware
    `diff_tr_by_cells` logic.
    """
    # Import here to avoid circular import - _EventDiffer is created in differ.py
    from .differ import _EventDiffer
    
    if not old_table_events or not new_table_events:
        inner = _EventDiffer(old_table_events, new_table_events, differ.config, diff_id_state=differ._diff_id_state)
        for ev in inner.get_diff_events():
            differ.append(*ev)
        return

    # Check whether the table wrapper attrs changed (style, etc.)
    old_table_start = old_table_events[0]
    new_table_start = new_table_events[0]
    table_attrs_changed = False
    old_table_style = None
    if old_table_start[0] == START and new_table_start[0] == START:
        old_attrs = old_table_start[1][1]
        new_attrs = new_table_start[1][1]
        table_attrs_changed = not _attrs_equal_normalized(old_attrs, new_attrs)
        if table_attrs_changed:
            old_table_style = old_attrs.get('style') or ''

    # If table attrs changed, emit hidden <del> with old table for revert,
    # then emit new table with tagdiff_added (same pattern as lists).
    if table_attrs_changed:
        # Hidden structural revert data
        del_attrs = Attrs([(QName('class'), 'structural-revert-data'),
                           (QName('style'), 'display:none')])
        diff_id = differ._new_diff_id() if getattr(differ.config, 'add_diff_ids', False) else None
        if diff_id:
            del_attrs = del_attrs | [(QName(getattr(differ.config, 'diff_id_attr', 'data-diff-id')), diff_id)]
        differ.append(START, (QName('del'), del_attrs), (None, -1, -1))
        for ev in old_table_events:
            differ.append(*ev)
        differ.append(END, QName('del'), (None, -1, -1))

        # Emit new table wrapper with tagdiff_added + data-old-*
        new_tag = new_table_start[1][0]
        new_attrs_out = new_table_start[1][1]
        new_attrs_out = differ.inject_class(new_attrs_out, 'tagdiff_added')
        new_attrs_out = differ.inject_refattr(new_attrs_out, old_attrs)
        if diff_id:
            new_attrs_out = differ._set_attr(new_attrs_out, getattr(differ.config, 'diff_id_attr', 'data-diff-id'), diff_id)
        differ.append(START, (new_tag, new_attrs_out), new_table_start[2])
    elif old_table_start[0] == START:
        differ.append(*new_table_events[0])
    else:
        differ.append(*new_table_events[0])

    old_rows = extract_tr_blocks(old_table_events)
    new_rows = extract_tr_blocks(new_table_events)
    old_keys = [row_key(r) for r in old_rows]
    new_keys = [row_key(r) for r in new_rows]

    matcher = SequenceMatcher(None, old_keys, new_keys)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for oi, nj in zip(range(i1, i2), range(j1, j2)):
                diff_tr_by_cells(differ, old_rows[oi], new_rows[nj],
                                 table_old_style=old_table_style if table_attrs_changed else None)
        elif tag == "delete":
            with differ.diff_group():
                with differ.context("del"):
                    for oi in range(i1, i2):
                        differ.block_process(old_rows[oi])
        elif tag == "insert":
            with differ.diff_group():
                with differ.context("ins"):
                    for nj in range(j1, j2):
                        differ.block_process(new_rows[nj])
        else:  # replace
            # Pair rows positionally where possible
            n = min(i2 - i1, j2 - j1)
            for k in range(n):
                diff_tr_by_cells(differ, old_rows[i1 + k], new_rows[j1 + k],
                                 table_old_style=old_table_style if table_attrs_changed else None)
            if (i2 - i1) > n:
                with differ.diff_group():
                    with differ.context("del"):
                        for oi in range(i1 + n, i2):
                            differ.block_process(old_rows[oi])
            if (j2 - j1) > n:
                with differ.diff_group():
                    with differ.context("ins"):
                        for nj in range(j1 + n, j2):
                            differ.block_process(new_rows[nj])

    # Emit closing </table> from NEW wrapper.
    differ.append(*new_table_events[-1])
