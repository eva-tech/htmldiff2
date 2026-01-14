import sys
import os
sys.path.insert(0, os.path.join(os.getcwd(), "src"))

from htmldiff2 import render_html_diff
from htmldiff2.config import DiffConfig

before = """<table><tr><td style="color:red">Test</td></tr></table>"""
after = """<table><tr><td style="color:blue">Test</td></tr></table>"""

# Force IDs and visual replace for testing
config = DiffConfig()
config.add_diff_ids = True
config.visual_replace_inline = True

print(render_html_diff(before, after, config=config))
