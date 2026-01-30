# -*- coding: utf-8 -*-
"""
Configuración y constantes para htmldiff2.
"""
import re

# Python 3
text_type = str
string_types = (str,)

# Expresiones regulares (exportadas para uso en otros módulos)
_leading_space_re = re.compile(r'^(\s+)', re.U)
_diff_split_re = re.compile(r'(\s+)', re.U)
_token_split_re = re.compile(r'(\s+|[^\w\s]+)', re.U)

# Constantes para tipos de tags HTML
INLINE_FORMATTING_TAGS = frozenset(['span', 'strong', 'b', 'em', 'i', 'u'])
BLOCK_WRAPPER_TAGS = frozenset(['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'])
STRUCTURAL_TAGS = frozenset(['p', 'br', 'table', 'ul', 'ol', 'li', 'tr', 'td', 'th',
                              'h1', 'h2', 'h3', 'h4', 'h5', 'h6'])


class DiffConfig(object):
    """
    Runtime configuration for diff rendering.

    Kept as a plain object for Python 2 compatibility.
    """

    # Display requirements
    delete_first = True
    linebreak_marker = u'\u00b6'  # ¶

    # Visual/attribute diff
    track_attrs = ('style', 'class', 'src', 'href', 'ref', 'data-ref')
    # Tags where "visual-only" changes (attrs/tag changes with same text) should be
    # rendered as a visible diff (del then ins). Excludes structural containers.
    visual_container_tags = (
        'span', 'div', 'p',
        'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
        'strong', 'b', 'em', 'i', 'u',
        'td', 'th',
    )
    # Tags to atomize as blocks for alignment. Intentionally excludes generic
    # container <div> to avoid swallowing large sections like report-content.
    visual_atomize_tags = (
        'span', 'p', 'div',
        'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
        'strong', 'b', 'em', 'i', 'u',
    )

    # Granularity improvements
    tokenize_text = True
    tokenize_regex = _token_split_re
    # Make whitespace-only / whitespace-leading/trailing diffs visible in HTML rendering
    preserve_whitespace_in_diff = True
    # Merge adjacent <ins>...</ins><ins>...</ins> (and same for <del>) into a single tag
    merge_adjacent_change_tags = True
    # When a change is purely visual (same text; different tag/attrs), render it
    # as an inline del+ins (keeps reading order and avoids block duplication).
    visual_replace_inline = True

    # Heuristics for complex structures
    enable_list_atomization = True
    enable_table_atomization = True
    enable_inline_wrapper_atomization = True

    # Void / non-textual elements (e.g. <img>) should still be shown as insert/delete
    # when they are added/removed. We keep this list intentionally small to avoid
    # noisy diffs that can break layout.
    force_event_diff_on_equal_for_tags = ('img',)
    wrap_void_tag_changes_with_ins_del = ('img',)

    # --- Optional: Add stable IDs to diff markers for per-change Apply/Reject in the frontend ---
    #
    # IMPORTANT: we intentionally use a data-* attribute by default because HTML `id`
    # MUST be unique in the document, and a "paired" ins/del would otherwise create
    # invalid HTML if both share the same `id`.
    add_diff_ids = True
    diff_id_attr = 'data-diff-id'

    # Threshold for InsensitiveSequenceMatcher: matches with fewer tokens than this
    # are ignored, preventing "shredded" diffs on unrelated texts.
    sequence_match_threshold = 2

    # Global similarity threshold: if SequenceMatcher.ratio() of the full texts is
    # below this value, skip structural matching and render as bulk del + ins.
    # Set to 0 to disable this feature.
    bulk_replace_similarity_threshold = 0.3



