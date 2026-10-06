"""Profile the fixture tables through the fake cursor -> tests/fixture/profile.json (no SQL Server needed)."""
import json
import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).parent.parent), str(Path(__file__).parent)]

from dbexplorer.config import load_config  # noqa: E402
from dbexplorer.model import Catalog  # noqa: E402
from dbexplorer.prefix import detect_prefixes, logical_name  # noqa: E402
from dbexplorer.profile import _val, profile_table  # noqa: E402
from fake_db import ROWS, FakeCursor  # noqa: E402

if __name__ == "__main__":
    root = Path(__file__).parent.parent
    cfg = load_config(root / "config.example.ini")
    cat = Catalog.load(root / "tests/fixture/catalog.json")
    prefixes = detect_prefixes(cat, cfg)
    out = {}
    for t in ROWS:
        key = f"dbo.{t}"
        logical = {c["name"].lower(): logical_name(c["name"], prefixes[key]["prefix"], cfg) for c in cat.columns[key]}
        out[key] = profile_table(FakeCursor(), key, len(ROWS[t]), cat.columns[key], logical, cfg)
        out[key]["profiled_at"] = "2026-10-07T01:00:00"
    path = root / "tests/fixture/profile.json"
    path.write_text(json.dumps(out, indent=1, default=_val), encoding="utf-8")
    print(f"wrote {path}")
