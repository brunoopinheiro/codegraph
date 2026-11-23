"""Self-contained HTML viewer for the file-level import graph (`codegraph view`).

The page is one HTML file: the export JSON sits in a `<script type="application/json">`
tag, `app.js` and `app.css` are inlined, and the rendering libraries (sigma.js, graphology,
graphology-library) are inlined from `vendor/` when those files ship with the package, or
loaded from a CDN at the pinned versions below when they don't.
"""
from __future__ import annotations

import html
import json
import re
import webbrowser
from importlib import resources
from pathlib import Path

ASSETS = resources.files(__name__)
VENDOR = ASSETS / "vendor"  # tests point this somewhere else

# The one place that pins the browser libraries: (file name in vendor/, CDN URL of that same file).
# Listed in load order. To work offline, download each URL into vendor/ under its file name.
LIBRARIES = (
    ("graphology.umd.min.js", "https://cdn.jsdelivr.net/npm/graphology@0.25.4/dist/graphology.umd.min.js"),
    ("graphology-library.min.js",
     "https://cdn.jsdelivr.net/npm/graphology-library@0.8.0/dist/graphology-library.min.js"),
    ("sigma.min.js", "https://cdn.jsdelivr.net/npm/sigma@3.0.0/dist/sigma.min.js"),
)

_PLACEHOLDER = re.compile(r"<!--CG:([A-Z]+)-->")


def script_safe(js: str) -> str:
    """Make text safe inside an inline <script>: it must not close the tag or open an HTML comment."""
    return js.replace("</script", "<\\/script").replace("</SCRIPT", "<\\/SCRIPT").replace("<!--", "<\\!--")


def embed_json(data) -> str:
    """JSON for a <script type="application/json"> tag. `<`, `>` and `&` become \\u escapes, which
    JSON.parse turns back into the same characters, so no `</script>` can end the tag early."""
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def library_tags(vendor=None) -> tuple[str, str]:
    """(<script> tags, mode): mode 'inline' when every vendored file is present, else 'cdn'."""
    vendor = VENDOR if vendor is None else vendor
    if all((vendor / name).is_file() for name, _ in LIBRARIES):
        tags = [f"<script>/* {name} (vendored) */\n{script_safe((vendor / name).read_text(encoding='utf-8'))}\n</script>"
                for name, _ in LIBRARIES]
        return "\n".join(tags), "inline"
    return "\n".join(f'<script src="{url}" referrerpolicy="no-referrer"></script>' for _, url in LIBRARIES), "cdn"


def render_html(data: dict, vendor=None) -> tuple[str, str]:
    """The complete page for `data` ({"title", "files": export, "groups": collapsed export}) and the library mode."""
    template = (ASSETS / "template.html").read_text(encoding="utf-8")
    libraries, mode = library_tags(vendor)
    parts = {
        "TITLE": html.escape(f"codegraph: {data.get('title') or 'import graph'}"),
        "STYLE": (ASSETS / "app.css").read_text(encoding="utf-8").replace("</style", "<\\/style"),
        "LIBRARIES": libraries,
        "DATA": embed_json(data),
        "APP": script_safe((ASSETS / "app.js").read_text(encoding="utf-8")),
    }
    # One pass, so placeholder-looking text inside the data or the libraries is never substituted.
    return _PLACEHOLDER.sub(lambda m: parts[m.group(1)], template), mode


def open_in_browser(path: Path) -> bool:
    return webbrowser.open(Path(path).resolve().as_uri())
