import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from dbexplorer.analysis import impact, run_analysis  # noqa: E402
from dbexplorer.config import load_config  # noqa: E402
from dbexplorer.jobs import schedule_text  # noqa: E402
from dbexplorer.kb import Exporter  # noqa: E402
from dbexplorer.model import Catalog  # noqa: E402
from dbexplorer.tsql import error_catalog  # noqa: E402
from make_fixture import build  # noqa: E402


@pytest.fixture(scope="module")
def cat():
    return Catalog(build())


@pytest.fixture(scope="module")
def doc(cat):
    return run_analysis(cat, load_config(Path(__file__).parent.parent / "config.example.ini"))


def errors_by_line(cat):
    msgs = {m["message_id"]: m for m in cat.data["messages"]}
    return {e["line"]: e for e in error_catalog(cat.objects["dbo.usp_placeorder"]["definition"], msgs)}


def test_error_catalog_messages_and_conditions(cat):
    e = errors_by_line(cat)
    assert e[9]["message"] == "Quantity must be positive" and e[9]["condition"] == "@qty <= 0"
    assert e[12]["number"] == 50001 and e[12]["message"] == "Client %s is not active"   # from sys.messages
    assert e[13]["kind"] == "RETURN code" and e[13]["number"] == -1
    assert e[13]["condition"].startswith("NOT EXISTS (SELECT 1 FROM clients")           # RETURN inside IF ... BEGIN
    assert e[16]["kind"] == "THROW" and e[16]["number"] == 50010 and e[16]["message"] == "Invalid order type"
    assert e[21]["message"] == "Insufficient holdings for sell order"                   # resolved from @msg
    assert e[24]["severity"] == 10 and "informational" in e[24]["warning"]
    assert e[24]["condition"].startswith("NOT (@type = 'S'")                             # ELSE branch
    assert e[31]["kind"] == "THROW (rethrow)" and e[31]["in_catch"]


def test_error_catalog_edge_cases():
    sql = """CREATE PROC p AS
    DECLARE @m varchar(100)
    SET @m = 'Limit ' + CAST(@x AS varchar(10)) + ' exceeded'
    RAISERROR(@m, 25, 1)
    THROW 51000, @undefined, 2
    RETURN"""
    out = error_catalog(sql)
    assert out[0]["message"] == "Limit {@x} exceeded" and "WITH LOG" in out[0]["warning"]
    assert out[1]["number"] == 51000 and "runtime" in out[1]["message"]
    assert len(out) == 2   # bare RETURN is not an error code


def test_schedule_text():
    assert schedule_text({"freq_type": 4, "freq_interval": 1, "freq_subday_type": 1, "active_start_time": 183000}) \
        == "Daily at 18:30"
    assert schedule_text({"freq_type": 8, "freq_interval": 62, "freq_subday_type": 4, "freq_subday_interval": 15,
                          "active_start_time": 91500, "active_end_time": 153000}) \
        == "Every weekday (Mon-Fri), every 15 minutes between 09:15 and 15:30"
    assert schedule_text({"freq_type": 8, "freq_interval": 64 + 1, "freq_recurrence_factor": 2, "freq_subday_type": 1,
                          "active_start_time": 20000}) == "Every 2 weeks on Sun, Sat at 02:00"
    assert schedule_text({"freq_type": 32, "freq_interval": 6, "freq_relative_interval": 16, "freq_subday_type": 1,
                          "active_start_time": 220000}) == "Monthly on the last Friday at 22:00"
    assert schedule_text({"freq_type": 64}) == "When SQL Server Agent starts"


def test_jobs_trace_nested_calls_and_warnings(doc):
    jobs = {j["name"]: j for j in doc["jobs"]}
    assert "Other DB backup" not in jobs and doc["meta"]["jobs_skipped"] == 1
    eod = jobs["EOD - Settlement"]
    assert set(eod["procedures"]) == {"dbo.usp_nightly", "dbo.usp_settle", "dbo.usp_purgeold"}
    assert "dbo.settlement" in eod["writes"] and eod["writes"]["dbo.settlement"] == ["dbo.usp_settle"]
    w = " ".join(eod["warnings"])
    assert "continues with the next step" in w and "SSIS" in w and "Failed 2 of 22" in w
    audit = " ".join(jobs["Intraday - Holdings audit"]["warnings"])
    assert "quits reporting SUCCESS" in audit and "exceeds the repeat interval" in audit
    settle = next(m for m in doc["modules"] if m["key"] == "dbo.usp_settle")
    assert settle["run_by_jobs"][0]["how"] == "through the call chain"
    assert "runs in a scheduled job" in settle["risk"]["reasons"]


def test_impact_lists_jobs(doc):
    r = impact(doc, "dbo.settlement")
    assert [j["job"] for j in r["jobs"]] == ["EOD - Settlement"]
    assert impact(doc, "dbo.branches")["jobs"] == []


def test_kb_job_pages(cat, doc, tmp_path):
    Exporter(doc, load_config(Path(__file__).parent.parent / "config.example.ini"), cat).write(tmp_path)
    page = (tmp_path / "jobs" / "EOD - Settlement.md").read_text(encoding="utf-8")
    assert "Every weekday (Mon-Fri) at 18:30" in page and "dbo.usp_Settle" in page
    proc = (tmp_path / "procedures" / "dbo.usp_PlaceOrder.md").read_text(encoding="utf-8")
    assert "## Errors raised" in proc and "Insufficient holdings for sell order" in proc
    assert "EOD - Settlement" in (tmp_path / "procedures" / "dbo.usp_Settle.md").read_text(encoding="utf-8")
    assert (tmp_path / "prompts" / "06-batch-job-testing.md").exists()
