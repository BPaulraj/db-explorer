import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from dbexplorer.analysis import impact, run_analysis  # noqa: E402
from dbexplorer.config import load_config  # noqa: E402
from dbexplorer.model import Catalog, split_name  # noqa: E402
from dbexplorer.prefix import detect_prefixes  # noqa: E402
from dbexplorer.tsql import Analyzer  # noqa: E402
from make_fixture import build  # noqa: E402


@pytest.fixture(scope="module")
def cat():
    return Catalog(build())


@pytest.fixture(scope="module")
def cfg():
    return load_config(Path(__file__).parent.parent / "config.example.ini")


def ops(cat, sql):
    an = Analyzer(cat).analyze(sql)
    return {(r.key.split(".")[1], r.op) for r in an.refs}, an


def test_split_name():
    assert split_name("[db]..[my table]") == ["db", "", "my table"]
    assert split_name("dbo.trade") == ["dbo", "trade"]


def test_prefixes(cat, cfg):
    p = detect_prefixes(cat, cfg)
    assert p["dbo.trade"]["prefix"] == "trd"
    assert p["dbo.vaults"]["prefix"] == "vl"
    assert p["dbo.ledger"]["prefix"] == "ldg"
    assert p["dbo.tmp_import"]["prefix"] == ""


def test_statements_without_semicolons(cat):
    found, _ = ops(cat, """
        INSERT INTO trade (trdclient) SELECT clclient FROM clients c, branches b WHERE c.clbranch = b.brbranch
        SELECT * FROM stocks
        IF EXISTS (SELECT 1 FROM vaults WHERE vlclient = 'X') UPDATE vaults SET vlquantity = 0
        ELSE DELETE FROM ledger WHERE ldgid = 1
    """)
    assert found == {("trade", "INSERT"), ("clients", "SELECT"), ("branches", "SELECT"), ("stocks", "SELECT"),
                     ("vaults", "SELECT"), ("vaults", "UPDATE"), ("ledger", "DELETE")}


def test_comma_join_mined(cat):
    _, an = ops(cat, "SELECT 1 FROM clients c, branches b WHERE c.clbranch = b.brbranch")
    assert (("dbo.clients", "clbranch"), ("dbo.branches", "brbranch")) in [(a, b) for a, b, _, _ in an.joins]


def test_update_alias_and_written_columns(cat):
    _, an = ops(cat, "UPDATE t SET t.trdquantity = 1, trdcosts = 2 FROM trade t JOIN clients c ON c.clclient = t.trdclient")
    upd = [r for r in an.refs if r.op == "UPDATE"][0]
    assert upd.key == "dbo.trade" and upd.cols_written == {"trdquantity", "trdcosts"}
    assert not any(r.key == "dbo.trade" and r.op == "SELECT" for r in an.refs)


def test_output_into_cursor_hints_temp(cat):
    found, an = ops(cat, """
        DECLARE cur CURSOR FOR SELECT trdid FROM trade (NOLOCK)
        OPEN cur
        FETCH NEXT FROM cur INTO @id
        UPDATE vaults SET vlquantity = 0 OUTPUT deleted.vlclient INTO holdings_audit (haclient) WHERE vlclient = @c
        SELECT * INTO #snap FROM settlement WITH (NOLOCK)
        INSERT INTO @tv SELECT ldgid FROM ledger
    """)
    assert ("trade", "SELECT") in found
    assert ("holdings_audit", "INSERT") in found and ("vaults", "UPDATE") in found
    assert ("settlement", "SELECT") in found and ("ledger", "SELECT") in found
    assert "#snap" in an.temp_tables
    assert not an.unresolved


def test_cte_and_case_end(cat):
    found, an = ops(cat, """
        WITH x AS (SELECT trdclient FROM trade), y (c) AS (SELECT vlclient FROM vaults)
        SELECT CASE WHEN 1 = 1 THEN 'a' ELSE 'b' END FROM x JOIN y ON y.c = x.trdclient
        DELETE FROM stocks
    """)
    assert found == {("trade", "SELECT"), ("vaults", "SELECT"), ("stocks", "DELETE")}
    assert not an.unresolved


def test_dynamic_sql_flagged(cat):
    found, an = ops(cat, "DECLARE @s nvarchar(max) = 'UPDATE dbo.clients SET clstatus = ''I''' EXEC (@s)")
    assert an.dynamic_sql and ("clients", "UPDATE") in found


def test_relationships_and_impact(cat, cfg):
    doc = run_analysis(cat, cfg)
    pairs = {(e["a"]["table"], e["a"]["column"], e["b"]["table"], e["b"]["column"]): e for e in doc["edges"]}
    assert pairs[("dbo.clients", "clclient", "dbo.trade", "trdclient")]["parent"] == "a"
    assert pairs[("dbo.ledger", "ldgtrdid", "dbo.trade", "trdid")]["parent"] == "b"
    assert "fk" in pairs[("dbo.clientbank", "cbclient", "dbo.clients", "clclient")]["sources"]
    # Surrogate keys (trdid, ldgid -> 'id') must not match each other by name.
    assert ("dbo.ledger", "ldgid", "dbo.trade", "trdid") not in pairs

    r = impact(doc, "dbo.vaults", "vlquantity")
    mods = {(u["module"], u["op"]) for u in r["direct"]}
    assert ("dbo.usp_booktrade", "UPDATE") in mods and ("dbo.usp_booktrade", "INSERT") in mods
    assert any(u["module"] == "dbo.usp_dailyreport" for u in r["via_view"])
    assert [t["key"] for t in r["triggers"]] == ["dbo.trg_vaults_audit"]
    callers = {c["caller"] for c in impact(doc, "dbo.settlement")["callers"]}
    assert callers == {"dbo.usp_nightly"}
