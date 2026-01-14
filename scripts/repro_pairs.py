import re
import sys
from pathlib import Path

# Ensure we import the repo-local htmldiff2 (not a pip-installed one).
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import htmldiff2  # noqa: E402


def ids_in(html: str) -> list[str]:
    return re.findall(r'data-diff-id="([^"]+)"', html)


def main():
    before = (
        '<p><b>INFORMACIÓN CLÍNICA:</b> Paciente de 0 años, género no especificado.</p>'
        '<p><b>HALLAZGOS:</b> Los campos pulmonares... Las estructuras óseas...</p>'
    )
    after = (
        '<p><b>INFORMACIÓN CLÍNICA:</b> Paciente de 50 años, masculino.</p>'
        '<p><b>HALLAZGOS:</b></p>'
        '<ul><li>Los campos pulmonares...</li><li>Las estructuras óseas...</li></ul>'
    )

    out = htmldiff2.render_html_diff(before, after)
    ids = ids_in(out)
    print("unique_ids:", sorted(set(ids), key=lambda x: int(x) if x.isdigit() else x))
    print("counts:", {i: ids.count(i) for i in sorted(set(ids), key=lambda x: int(x) if x.isdigit() else x)})
    print(out)


if __name__ == "__main__":
    main()

