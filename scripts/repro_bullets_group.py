import sys
import os
sys.path.insert(0, os.path.join(os.getcwd(), "src"))

from htmldiff2 import render_html_diff
from htmldiff2.config import DiffConfig

before = """
<div>
<p><strong>HALLAZGOS:</strong></p>
<p><strong>Hepatobiliar:</strong> El hígado presenta morfología, tamaño y señal habituales.</p>
<p><strong>Vesícula Biliar:</strong> La vesícula biliar es de tamaño y grosor de pared normales.</p>
</div>
"""

after = """
<div>
<p><strong>HALLAZGOS:</strong></p>
<ul>
<li><strong>Hepatobiliar:</strong> El hígado presenta morfología, tamaño y señal habituales.</li>
<li><strong>Vesícula Biliar:</strong> La vesícula biliar es de tamaño y grosor de pared normales.</li>
</ul>
</div>
"""

config = DiffConfig()
config.add_diff_ids = True

print(render_html_diff(before, after, config=config))
