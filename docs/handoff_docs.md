# Documentación de Handoff (htmldiff2)

## 1. Descripción del proyecto

Fork de [htmldiff2](https://github.com/edsu/htmldiff2) adaptado para **EdenAI Report**. Compara un borrador HTML del doctor contra un reporte sugerido por un LLM y produce HTML con marcadores de diff (`<ins>`, `<del>`, clases CSS) que el frontend usa para mostrar cambios y permitir Aceptar/Rechazar por cambio individual.

```python
from htmldiff2 import render_html_diff, DiffConfig
output_html = render_html_diff(old_html, new_html, config=DiffConfig())
```

**Dependencias**: `genshi` (representación de eventos HTML), `html5lib` (parsing HTML), `difflib` (stdlib, algoritmo de alineación).

---

## 2. Conceptos fundamentales

### 2.1 Eventos Genshi (START, END, TEXT)

Todo el procesamiento interno trabaja con **eventos Genshi**, no con strings de HTML. El HTML se parsea a una lista de tuplas `(tipo, datos, posición)`:

```python
# HTML: <p style="color:red">Hola <strong>mundo</strong></p>
# Se convierte en esta lista de eventos:

(START, (QName('p'), Attrs([('style','color:red')])), pos)   # Apertura de <p>
(TEXT,  'Hola ',                                      pos)   # Texto suelto
(START, (QName('strong'), Attrs()),                    pos)   # Apertura de <strong>
(TEXT,  'mundo',                                       pos)   # Texto dentro de <strong>
(END,   QName('strong'),                               pos)   # Cierre de </strong>
(END,   QName('p'),                                    pos)   # Cierre de </p>
```

- **START**: Apertura de tag. `datos` = `(QName_del_tag, Attrs)`.
- **END**: Cierre de tag. `datos` = `QName_del_tag`.
- **TEXT**: Texto. `datos` = string con el contenido.

Toda la librería opera sobre estas tuplas: las compara, las alinea, las transforma y las emite al resultado final, que Genshi vuelve a renderizar como HTML string.

### 2.2 Átomos

Si le pasaras la lista plana de eventos a `SequenceMatcher` de difflib, el resultado sería terrible — intentaría alinear eventos individuales (`START`, `TEXT`, `END`) sin entender que pertenecen juntos.

Los **átomos** resuelven esto agrupando eventos en unidades lógicas con una **key** de comparación. Cada átomo es un dict:

```python
{
    'kind': 'block',                           # Tipo: 'block', 'text', 'event', 'br'
    'tag': 'li',                               # Tag HTML (solo para bloques)
    'key': ('block', 'contenido del item'),    # Key para SequenceMatcher
    'events': [(START,...), (TEXT,...), (END,...)],  # Los eventos Genshi originales
    'pos': ...
}
```

**Ejemplo**: `<li>Item uno</li>` se convierte en UN solo átomo de tipo `block` con key `('block', 'item uno')`, en vez de 3 eventos separados.

La key es lo que `SequenceMatcher` compara. Decisiones clave de keying:

- **`<p>` y `<li>`** usan la misma estructura de key: `('block', texto_normalizado)`. Así un párrafo puede matchear con un item de lista si el texto es igual, lo cual habilita el diff estructural de listas.
- **`<ul>`/`<ol>`** tienen key trivial `('ul',)` / `('ol',)` para forzar que siempre se comparen como “iguales” y se ejecute un diff interno de sus hijos.
- **`<tr>`** usa como key el texto de las primeras 2 celdas (identidad de fila estable ante cambios de columnas).
- **Texto** se tokeniza palabra por palabra: cada token es un átomo `text` con key `('t', palabra)`.

### 2.3 Los dos niveles de diff

La librería diffea en **dos niveles**:

1. **Nivel de átomos** (`StreamDiffer`): `SequenceMatcher` corre sobre las keys de los átomos. Produce opcodes como `('replace', 2, 5, 3, 7)` — “los átomos old[2:5] se reemplazaron por new[3:7]”. Aquí se detectan los cambios estructurales grandes (conversiones de lista, cambios de tabla, etc.).
2. **Nivel de eventos** (`_EventDiffer`): Cuando un opcode del nivel de átomos necesita diffing granular (ej. el contenido dentro de un `<td>` matcheado cambió), se crea un `_EventDiffer` que corre `SequenceMatcher` directo sobre los eventos crudos, **sin atomización**. Esto produce el diff a nivel de palabra/formato.

---

## 3. Flujo de procesamiento

```
          ┌─────────────────────────────────────────────────────────────────┐
          │                                                                 │
 old_html ──▶ parse_html() ──▶ eventos ──▶ atomize_events() ──▶ átomos_old │
 new_html ──▶ parse_html() ──▶ eventos ──▶ atomize_events() ──▶ átomos_new │
          │                                                                 │
          └────────────┬────────────────────────────────────────────────────┘
                       │
                       ▼
          SequenceMatcher(atom_keys_old, atom_keys_new)
                       │
                       ▼
                   opcodes: [('equal', ...), ('replace', ...), ('delete', ...), ...]
                       │
                       ▼
          ┌────────────────────────────────────────┐
          │    StreamDiffer.process() — loop        │
          │    por cada opcode, en orden:           │
          │                                         │
          │    1. ¿Es conversión p↔lista?           │──▶ Diff estructural de listas
          │    2. ¿Es cambio de atributos de tabla? │──▶ Diff de tabla por filas/celdas
          │    3. ¿Es replace genérico?             │──▶ _EventDiffer (diff granular)
          │    4. ¿Es delete/insert?                │──▶ block_process() con contexto del/ins
          │    5. ¿Es equal?                        │──▶ Emitir (con checks de diffs visuales)
          └────────────┬───────────────────────────┘
                       │
                       ▼
          merge_adjacent_change_tags()  (fusiona <ins>a</ins><ins>b</ins> → <ins>ab</ins>)
                       │
                       ▼
                  HTML de salida
```

### ¿Qué hace `block_process()`?

Es la función que emite eventos al resultado final respetando el contexto actual (`ins`, `del`, o `None`). Decide cómo wrappear:

- **Tags estructurales** (`<table>`, `<ul>`, `<li>`, etc.): Inyecta la clase `tagdiff_added/deleted` en el tag mismo (mantiene HTML válido).
- **Tags de bloque** (`<p>`, `<h1>`–`<h6>`): Envuelve el tag completo en `<del>` o `<ins>` (ej. `<del><p>texto</p></del>`).
- **Texto**: Lo envuelve en `<del>`/`<ins>` con whitespace visible.
- **`<br>`**: Agrega el marcador `¶` visible.

---

## 4. Casos principales que maneja

### Cambios de texto

Diff a nivel de palabra. Los textos completamente distintos (ratio < 0.3) se muestran como bloque `<del>todo lo viejo</del><ins>todo lo nuevo</ins>` en vez de interleaving ruidoso.

### Cambios de estilo / atributos

Mismo texto, diferentes attrs (ej. `font-size` cambió) → `<del><span style="viejo">texto</span></del><ins><span style="nuevo">texto</span></ins>`. La comparación de CSS es independiente del orden de propiedades.

### Formato inline (negrita, cursiva, subrayado)

`<span>TÍTULO:</span> oración` → `<strong>TÍTULO:</strong> oración`: solo “TÍTULO:” se marca como cambiado, la oración que sigue queda sin marcar.

### Conversión párrafos ↔︎ listas

`<p>Item 1</p><p>Item 2</p>` → `<ul><li>Item 1</li><li>Item 2</li></ul>`: emite items con `class="diff-bullet-ins"` + contenido viejo oculto en `class="structural-revert-data"` para el botón Revertir del frontend.

### Tablas

Diffing consciente de filas (alineación por primeras 2 celdas) y celdas (alineación posicional para evitar drift con valores duplicados). Eliminación/inserción de columnas marca la celda correcta sin romper estructura.

### Line breaks y void elements

`<br>` agregados/eliminados muestran `¶`. `<img>` agregados/eliminados se envuelven en `<ins>`/`<del>`.

---

## 5. Formato de salida para el frontend

| Marcador | Significado |
| --- | --- |
| `<ins data-diff-id="N">` | Contenido insertado |
| `<del data-diff-id="N">` | Contenido eliminado |
| `class="tagdiff_added"` | Elemento estructural agregado |
| `class="tagdiff_deleted"` | Elemento estructural eliminado |
| `class="tagdiff_replaced"` | Atributos/tag cambiados (in-place) |
| `class="diff-bullet-ins"` | Item de lista en conversión p→lista |
| `class="diff-bullet-del"` | Item de lista en conversión lista→p |
| `class="structural-revert-data" style="display:none"` | Datos ocultos para “Revertir” |
| `data-old-style`, `data-old-tag`, etc. | Valores originales de atributos |

`data-diff-id` agrupa `<del>` y `<ins>` pareados bajo el mismo ID para Aceptar/Rechazar individual.

---

## 6. Mapa de archivos

| Archivo | Responsabilidad |
| --- | --- |
| `config.py` | Clase `DiffConfig` con todos los parámetros configurables |
| `parser.py` | HTML string → stream de eventos Genshi |
| `atomization.py` | Eventos → átomos con keys de alineación |
| `differ.py` | `StreamDiffer` — motor principal, procesamiento de opcodes (~1600 líneas) |
| `event_differ.py` | `_EventDiffer` — differ interno sin atomización |
| `block_processor.py` | Emite eventos dentro de contextos ins/del |
| `text_differ.py` | Diff de texto a nivel de palabra |
| `normalization.py` | Normalización de opcodes (delete-first, merge wrappers) |
| `visual_replace.py` | Manejo de cambios mismo-texto-diferente-estilo |
| `diff_inline_formatting.py` | Diffs de formato inline (ej. negrita agregada a parte del texto) |
| `table_differ.py` | Diffing de tablas por filas y celdas |
| `utils.py` | Utilidades (extracción de texto, normalización de CSS, merge de tags) |

---

## 7. Desarrollo

Para inspeccionar átomos y opcodes durante debugging:

```python
from htmldiff2.parser import parse_html
from htmldiff2.differ import StreamDiffer
from htmldiff2.config import DiffConfig
from difflib import SequenceMatcher

differ = StreamDiffer(parse_html(old), parse_html(new), config=DiffConfig())

for i, a in enumerate(differ._old_atoms):
    print(f"old[{i}]{a['kind']} tag={a.get('tag')} key={a['key']}")

old_keys = [a['key'] for a in differ._old_atoms]
new_keys = [a['key'] for a in differ._new_atoms]
for op in SequenceMatcher(None, old_keys, new_keys).get_opcodes():
    print(op)
```

---

## 8. Gotchas importantes

- **`<p>` y `<li>` comparten key** a propósito — permite detectar conversiones párrafo↔︎lista.
- **`<div>` con hijos estructurales NO se atomiza** — si no, se tragaría secciones enteras del reporte.
- **Import circular**: `_EventDiffer` se crea al final de `differ.py` vía factory; `table_differ.py` lo importa en scope de función.
- **CSS order-independent**: Los estilos se normalizan alfabéticamente antes de comparar.
- **`structural-revert-data`**: Contenedor oculto con datos para que el frontend implemente “Revertir” sin re-llamar a la API.
- **Block wrappers envueltos POR del/ins**: `<del><p>...</p></del>` (no `<p><del>...</del></p>`) para que aceptar un cambio elimine el `<p>` completo.