import re
import sys
from pathlib import Path

# Ensure we import the repo-local htmldiff2 (not a pip-installed one).
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import htmldiff2  # noqa: E402


def _extract_table(html: str) -> str:
    m = re.search(r"(<table[\\s\\S]*?</table>)", html)
    return m.group(1) if m else ""


def main():
    old_html = """<div>
<h2><strong>COMPARACIÓN DE NÓDULOS PULMONARES</strong></h2>
<br/>
<p><strong>INFORMACIÓN CLÍNICA:</strong></p>
<p>Paciente pediátrico, edad 0 años.</p>
<br/>
<p><strong>TÉCNICA:</strong></p>
<p>Se realizó comparación de nódulos pulmonares con estudio previo.</p>
<br/>
<p><strong>COMPARACIÓN:</strong></p>
<p>Estudio previo de enero de 2024.</p>
<br/>
<p><strong>HALLAZGOS:</strong></p>
<p>Se presenta tabla comparativa de nódulos pulmonares:</p>
<br/>
<table>
<thead>
<tr>
<th>Localización</th>
<th>Diámetro Actual (mm)</th>
<th>Diámetro Previo (mm)</th>
<th>Cambio (%)</th>
<th>Fecha Previa</th>
</tr>
</thead>
<tbody>
<tr>
<td>Lóbulo Superior Derecho</td>
<td>11</td>
<td>10</td>
<td>+10%</td>
<td>Enero 2024</td>
</tr>
<tr>
<td>Lóbulo Inferior Izquierdo</td>
<td>8</td>
<td>8</td>
<td>0%</td>
<td>Enero 2024</td>
</tr>
</tbody>
</table>
<br/>
<p><strong>IMPRESIÓN:</strong></p>
<p>1. Nódulo en lóbulo superior derecho ha aumentado de 10 mm a 11 mm (incremento del 10%) desde enero de 2024.</p>
<p>2. Nódulo en lóbulo inferior izquierdo se mantiene estable en 8 mm desde enero de 2024.</p>
</div>"""

    # "Eliminar columna de diámetro previo (mm)" => remove header and each row's 3rd cell.
    new_html = """<div>
<h2><strong>COMPARACIÓN DE NÓDULOS PULMONARES</strong></h2>
<br/>
<p><strong>INFORMACIÓN CLÍNICA:</strong></p>
<p>Paciente pediátrico, edad 0 años.</p>
<br/>
<p><strong>TÉCNICA:</strong></p>
<p>Se realizó comparación de nódulos pulmonares con estudio previo.</p>
<br/>
<p><strong>COMPARACIÓN:</strong></p>
<p>Estudio previo de enero de 2024.</p>
<br/>
<p><strong>HALLAZGOS:</strong></p>
<p>Se presenta tabla comparativa de nódulos pulmonares:</p>
<br/>
<table>
<thead>
<tr>
<th>Localización</th>
<th>Diámetro Actual (mm)</th>
<th>Cambio (%)</th>
<th>Fecha Previa</th>
</tr>
</thead>
<tbody>
<tr>
<td>Lóbulo Superior Derecho</td>
<td>11</td>
<td>+10%</td>
<td>Enero 2024</td>
</tr>
<tr>
<td>Lóbulo Inferior Izquierdo</td>
<td>8</td>
<td>0%</td>
<td>Enero 2024</td>
</tr>
</tbody>
</table>
<br/>
<p><strong>IMPRESIÓN:</strong></p>
<p>1. Nódulo en lóbulo superior derecho ha aumentado de 10 mm a 11 mm (incremento del 10%) desde enero de 2024.</p>
<p>2. Nódulo en lóbulo inferior izquierdo se mantiene estable en 8 mm desde enero de 2024.</p>
</div>"""

    out = htmldiff2.render_html_diff(old_html, new_html)
    table = _extract_table(out)
    print("----TABLE DIFF----")
    print(table.encode("utf-8", errors="replace").decode("utf-8"))
    print("----FULL DIFF (truncated)----")
    # Print only around the table for readability
    start = out.find("<table")
    end = out.find("</table>") + len("</table>")
    snippet = out[start:end] if start != -1 and end != -1 else out
    print(snippet.encode("utf-8", errors="replace").decode("utf-8"))


if __name__ == "__main__":
    main()

