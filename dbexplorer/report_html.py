import json
from pathlib import Path

VENDOR = Path(__file__).parent / "vendor" / "cytoscape.min.js"
TEMPLATE = Path(__file__).parent / "explorer_template.html"


def write_html(doc, path):
    data = json.dumps(doc, default=list, separators=(",", ":")).replace("</", "<\\/")
    html = TEMPLATE.read_text(encoding="utf-8")
    html = html.replace("/*__CYTOSCAPE__*/", VENDOR.read_text(encoding="utf-8"))
    html = html.replace("__TITLE__", f"{doc['meta'].get('database', 'Database')} Explorer")
    html = html.replace("/*__DATA__*/", data)
    Path(path).write_text(html, encoding="utf-8")
