import os
import sys

# Add src to path so we test the local fork
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from htmldiff2 import DiffConfig, render_html_diff


def main():
    old_html = """<div>
<div ref="1">
<div>
<h2><strong>TOMOGRAFÍA COMPUTARIZADA</strong></h2>
<br/>
<p><strong>INFORMACIÓN CLÍNICA:</strong></p>
<p>Paciente masculino de 80 años.</p>
<br/>
<p><strong>TÉCNICA:</strong></p>
<p>Se realizó tomografía computarizada de tórax.</p>
<br/>
<p><strong>COMPARACIÓN:</strong></p>
<p>No disponible.</p>
<br/>
<p><strong>HALLAZGOS:</strong></p>
<br/>
<p><strong>IMPRESIÓN:</strong></p>
<p>Masa pulmonar en lóbulo superior derecho, sospechosa de malignidad.</p>
<p>Adenopatías mediastínicas, que requieren evaluación adicional.</p>
<table ref="2">
<thead>
<tr>
<th ref="3">Hallazgo</th>
<th ref="3">Descripción</th>
<th ref="3">Localización</th>
<th ref="3">Tamaño</th>
</tr>
</thead>
<tbody>
<tr>
<td ref="4">Masa pulmonar</td>
<td ref="4">Con bordes espiculados</td>
<td ref="4">Lóbulo superior derecho</td>
<td ref="4">Aproximadamente 3 cm de diámetro</td>
</tr>
<tr>
<td ref="4">Adenopatías mediastínicas</td>
<td ref="4"> </td>
<td ref="4">Mediastino</td>
<td ref="4">La mayor de ellas de 1.5 cm (ventana mediastinal)</td>
</tr>
<tr>
<td ref="4">Parénquima pulmonar</td>
<td ref="4">Sin otros hallazgos significativos</td>
<td ref="4"> </td>
<td ref="4"> </td>
</tr>
<tr>
<td ref="4">Derrame pleural</td>
<td ref="4">No observado</td>
<td ref="4"> </td>
<td ref="4"> </td>
</tr>
<tr>
<td ref="4">Estructuras mediastínicas y vasculares</td>
<td ref="4">Sin alteraciones aparentes</td>
<td ref="4"> </td>
<td ref="4"> </td>
</tr>
</tbody>
</table>
</div>
</div>
</div>"""

    new_html = """<div>
<div ref="1">
<div>
<h2><strong>TOMOGRAFÍA COMPUTARIZADA</strong></h2>
<br/>
<p><strong>INFORMACIÓN CLÍNICA:</strong></p>
<p>Paciente masculino de 80 años.</p>
<br/>
<p><strong>TÉCNICA:</strong></p>
<p>Se realizó tomografía computarizada de tórax.</p>
<br/>
<p><strong>COMPARACIÓN:</strong></p>
<p>No disponible.</p>
<br/>
<p><strong>HALLAZGOS:</strong></p>
<br/>
<p><strong>IMPRESIÓN:</strong></p>
<p>Masa pulmonar en lóbulo superior derecho, sospechosa de malignidad.</p>
<p>Adenopatías mediastínicas, que requieren evaluación adicional.</p>
<table ref="2">
<thead>
<tr>
<th ref="3">Hallazgo</th>
<th ref="3">Localización</th>
<th ref="3">Tamaño</th>
</tr>
</thead>
<tbody>
<tr>
<td ref="4">Masa pulmonar</td>
<td ref="4">Lóbulo superior derecho</td>
<td ref="4">Aproximadamente 3 cm de diámetro</td>
</tr>
<tr>
<td ref="4">Adenopatías mediastínicas</td>
<td ref="4">Mediastino</td>
<td ref="4">La mayor de ellas de 1.5 cm (ventana mediastinal)</td>
</tr>
<tr>
<td ref="4">Parénquima pulmonar</td>
<td ref="4"> </td>
<td ref="4"> </td>
</tr>
<tr>
<td ref="4">Derrame pleural</td>
<td ref="4"> </td>
<td ref="4"> </td>
</tr>
<tr>
<td ref="4">Estructuras mediastínicas y vasculares</td>
<td ref="4"> </td>
<td ref="4"> </td>
</tr>
</tbody>
</table>
</div>
</div>
</div>"""

    config = DiffConfig()
    config.add_diff_ids = True

    diff_html = render_html_diff(old_html, new_html, config=config)
    # Ensure Windows console doesn't blow up on unicode; show as UTF-8 anyway
    print(diff_html.encode("utf-8", errors="replace").decode("utf-8"))


if __name__ == "__main__":
    main()

