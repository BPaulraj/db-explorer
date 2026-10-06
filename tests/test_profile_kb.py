import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from dbexplorer.analysis import run_analysis  # noqa: E402
from dbexplorer.config import load_config  # noqa: E402
from dbexplorer.kb import NOTES_END, NOTES_START, Exporter, collect_notes  # noqa: E402
from dbexplorer.model import Catalog  # noqa: E402
from dbexplorer.prefix import detect_prefixes, logical_name  # noqa: E402
from dbexplorer.profile import profile_table  # noqa: E402
from dbexplorer.sensitive import by_values, shape  # noqa: E402
from dbexplorer.tsql import header_parameters, metrics  # noqa: E402
from fake_db import ROWS, FakeCursor  # noqa: E402
from make_fixture import build  # noqa: E402


@pytest.fixture(scope="module")
def cfg():
    return load_config(Path(__file__).parent.parent / "config.example.ini")


@pytest.fixture(scope="module")
def cat():
    return Catalog(build())


@pytest.fixture(scope="module")
def profile(cat, cfg):
    prefixes = detect_prefixes(cat, cfg)
    out = {}
    for t in ROWS:
        key = f"dbo.{t}"
        logical = {c["name"].lower(): logical_name(c["name"], prefixes[key]["prefix"], cfg) for c in cat.columns[key]}
        out[key] = profile_table(FakeCursor(), f"dbo.{t}", len(ROWS[t]), cat.columns[key], logical, cfg)
    return out


def test_shape_and_value_rules():
    assert shape("ABCPX1234X") == "AAAAA9999A"
    assert by_values(["ABCPX1234X", "BBCPY2234Z", "QWERT1111K"]) == "PAN"
    assert by_values(["9876543210", "+91 9123456789", "7000000001"]) == "Mobile/phone"
    assert by_values(["B", "S", "X"]) is None


def test_profile_masks_sensitive_columns(profile):
    cl = profile["dbo.clients"]["columns"]
    assert cl["clpan"]["sensitive"] == "PAN"
    assert "values" not in cl["clpan"] and "examples" not in cl["clpan"]
    assert cl["clpan"]["formats"][0][0] == "AAAAA9999A"
    assert cl["clmobile"]["sensitive"] == "Mobile/phone"
    assert cl["clname"]["sensitive"] == "Name"
    assert "examples" not in cl["clname"]
    assert dict(map(tuple, cl["clstatus"]["values"])) == {"A": 36, "I": 4}
    tr = profile["dbo.trade"]["columns"]
    assert tr["trdquantity"]["negatives"] == 1 and tr["trddate"]["null_pct"] == 11.0
    assert {v for v, _ in tr["trdtype"]["values"]} == {"B", "S", "X"}


def test_metrics_and_parameters(cat):
    d = cat.objects["dbo.usp_booktrade"]["definition"]
    p = header_parameters(d)
    assert p["@cost"]["default"] == "0" and p["@client"]["default"] is None
    m = metrics(d)
    assert m["writes"] == 4 and m["branches"] == 1 and not m["try_catch"]
    assert header_parameters(cat.objects["dbo.fn_clientbalance"]["definition"]) == {"@c": {"default": None, "output": False}}


def test_test_notes_and_risk(cat, cfg, profile):
    doc = run_analysis(cat, cfg, None, profile)
    trade = next(t for t in doc["tables"] if t["key"] == "dbo.trade")
    text = "\n".join(trade["test_notes"])
    assert "CHECK ([trdtype]='S'" in text and "trdtype (type) takes values" in text
    assert "negative values" in text and "trddate = (getdate())" in text
    clients = next(t for t in doc["tables"] if t["key"] == "dbo.clients")
    assert any("Sensitive data" in n and "clpan (PAN)" in n for n in clients["test_notes"])
    settle = next(m for m in doc["modules"] if m["key"] == "dbo.usp_settle")
    assert settle["risk"]["recently_modified"] and "writes 2 tables" in settle["risk"]["reasons"]
    book = next(m for m in doc["modules"] if m["key"] == "dbo.usp_booktrade")
    assert book["fires_triggers"] == ["dbo.trg_vaults_audit"]


def test_kb_export_preserves_notes_and_masks(cat, cfg, profile, tmp_path):
    doc = run_analysis(cat, cfg, None, profile)
    Exporter(doc, cfg, cat).write(tmp_path)
    f = tmp_path / "tables" / "dbo.trade.md"
    text = f.read_text(encoding="utf-8")
    assert "## Test considerations" in text and "dbo.usp_BookTrade" in text
    # A tester writes notes; regeneration must keep them.
    note = "Holds every executed trade. trdtype: B=buy, S=sell, X=cancelled."
    start = text.index(NOTES_START) + len(NOTES_START)
    f.write_text(text[:start] + "\n" + note + "\n" + text[text.index(NOTES_END):], encoding="utf-8")
    Exporter(doc, cfg, cat).write(tmp_path)
    assert note in (tmp_path / "tables" / "dbo.trade.md").read_text(encoding="utf-8")
    assert collect_notes(tmp_path)["dbo.trade"] == note
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "Holds every executed trade" in readme and "dbo.usp_Settle" in readme
    # No real PAN / mobile values anywhere in the knowledge base.
    everything = "".join(p.read_text(encoding="utf-8") for p in tmp_path.rglob("*") if p.is_file())
    for r in ROWS["clients"]:
        assert r["clpan"] not in everything and r["clmobile"] not in everything
    bundle = json.loads((tmp_path / "context.json").read_text(encoding="utf-8"))
    assert next(t for t in bundle["tables"] if t["name"] == "dbo.trade")["notes"] == note
    assert (tmp_path / "prompts" / "01-test-cases-for-procedure.md").exists()
