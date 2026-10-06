"""Build a synthetic catalog.json (stock broker schema) so the analyzer can be tested without SQL Server."""
import json
import sys
from pathlib import Path

V = lambda n: ("varchar", n)
INT, DEC, DT, BIT = ("int", 4), ("decimal", 9), ("datetime", 8), ("bit", 1)

TABLES = {
    # name: ([(column, type)], pk columns)
    "clients": ([("clclient", V(10)), ("clname", V(100)), ("clbranch", V(5)), ("clstatus", V(1)), ("clpan", V(10)), ("clmobile", V(15))], ["clclient"]),
    "stocks": ([("smstock", V(12)), ("smname", V(100)), ("smisin", V(12)), ("smexchange", V(5))], ["smstock"]),
    "branches": ([("brbranch", V(5)), ("brname", V(50)), ("brcity", V(50))], ["brbranch"]),
    "trade": ([("trdid", INT), ("trdclient", V(10)), ("trdstock", V(12)), ("trdquantity", INT), ("trdcosts", DEC),
               ("trddate", DT), ("trdtype", V(1)), ("trdexchange", V(5))], ["trdid"]),
    "vaults": ([("vlclient", V(10)), ("vlstock", V(12)), ("vlquantity", INT), ("vlupdatedon", DT)], []),
    "ledger": ([("ldgid", INT), ("ldgclient", V(10)), ("ldgamount", DEC), ("ldgtrdid", INT), ("ldgdate", DT)], ["ldgid"]),
    "clientbank": ([("cbclient", V(10)), ("cbbankacc", V(20)), ("cbifsc", V(11)), ("cbprimary", BIT)], []),
    "holdings_audit": ([("haclient", V(10)), ("hastock", V(12)), ("haqty", INT), ("haaction", V(1)), ("hadate", DT)], []),
    "settlement": ([("stlid", INT), ("stltrdid", INT), ("stlclient", V(10)), ("stlamount", DEC), ("stlstatus", V(1))], ["stlid"]),
    "tmp_import": ([("line_no", INT), ("raw_text", V(4000))], []),
}
FKS = [("FK_clientbank_clients", "clientbank", "clients", [("cbclient", "clclient")])]
CONSTRAINTS = [  # (table, kind, name, column, definition)
    ("trade", "CHECK", "CK_trade_type", "trdtype", "([trdtype]='S' OR [trdtype]='B' OR [trdtype]='X')"),
    ("trade", "CHECK", "CK_trade_qty", "trdquantity", "([trdquantity]>(0))"),
    ("trade", "DEFAULT", "DF_trade_date", "trddate", "(getdate())"),
    ("clients", "DEFAULT", "DF_clients_status", "clstatus", "('A')"),
]
MODIFIED = {"usp_Settle": "2026-09-30T18:20:00", "trade": "2026-09-15T10:00:00"}

MODULES = {
    ("vw_clientholdings", "V"): """
CREATE VIEW dbo.vw_clientholdings AS
SELECT c.clclient, c.clname, v.vlstock, v.vlquantity
FROM dbo.clients c
JOIN dbo.vaults v ON v.vlclient = c.clclient
""",
    ("usp_BookTrade", "P"): """
CREATE PROCEDURE [dbo].[usp_BookTrade]
    @client varchar(10), @stock varchar(12), @qty int, @cost decimal(18,2) = 0
AS
BEGIN
    SET NOCOUNT ON
    -- record the trade
    INSERT INTO dbo.trade (trdclient, trdstock, trdquantity, trdcosts, trddate)
    VALUES (@client, @stock, @qty, @cost, GETDATE())

    UPDATE vaults SET vlquantity = vlquantity + @qty, vlupdatedon = GETDATE()
    WHERE vlclient = @client AND vlstock = @stock
    IF @@ROWCOUNT = 0
        INSERT INTO vaults VALUES (@client, @stock, @qty, GETDATE())

    INSERT ledger (ldgclient, ldgamount, ldgtrdid, ldgdate)
    SELECT trdclient, trdcosts, trdid, trddate FROM trade WHERE trdid = SCOPE_IDENTITY()

    EXEC dbo.usp_LogAudit @client
END
""",
    ("usp_AmendTrade", "P"): """
CREATE PROC usp_AmendTrade @id int, @q int, @c decimal(18,2) AS
/* amend quantity and costs
   /* nested comment */ still a comment */
UPDATE t
   SET t.trdquantity = @q,
       trdcosts = @c
FROM trade t
INNER JOIN clients c ON c.clclient = t.trdclient
WHERE t.trdid = @id AND c.clstatus = 'A'
""",
    ("usp_DailyReport", "P"): """
CREATE PROCEDURE dbo.usp_DailyReport @d date AS
BEGIN
  ;WITH day_trades AS (
      SELECT t.trdclient, t.trdstock, SUM(t.trdquantity) AS qty
      FROM dbo.trade t WITH (NOLOCK)
      INNER JOIN stocks s ON s.smstock = t.trdstock
      WHERE CAST(t.trddate AS date) = @d
      GROUP BY t.trdclient, t.trdstock
  )
  SELECT dt.*, c.clname, CASE WHEN dt.qty > 100 THEN 'BIG' ELSE 'SMALL' END AS size
  FROM day_trades dt
  JOIN clients c ON c.clclient = dt.trdclient

  SELECT * FROM vw_clientholdings WHERE vlquantity > 0
  SELECT * FROM ArchiveDB.dbo.trade_hist WHERE th_date = @d
END
""",
    ("usp_Settle", "P"): """
CREATE PROCEDURE dbo.usp_Settle @d datetime AS
BEGIN
  MERGE settlement AS tgt
  USING (SELECT trdid, trdclient, trdcosts FROM trade WHERE trddate = @d) AS src
     ON tgt.stltrdid = src.trdid
  WHEN MATCHED THEN UPDATE SET stlamount = src.trdcosts
  WHEN NOT MATCHED THEN INSERT (stltrdid, stlclient, stlamount) VALUES (src.trdid, src.trdclient, src.trdcosts);

  DELETE l FROM ledger l JOIN trade t ON l.ldgtrdid = t.trdid WHERE t.trdtype = 'X'

  UPDATE s SET s.stlstatus = 'S'
  FROM settlement s
  WHERE s.stlclient IN (SELECT clclient FROM clients WHERE clstatus = 'A')
END
""",
    ("usp_PurgeOld", "P"): """
CREATE PROCEDURE dbo.usp_PurgeOld @days int AS
BEGIN
  DECLARE @sql nvarchar(max)
  SET @sql = N'DELETE FROM dbo.holdings_audit WHERE hadate < DATEADD(day, -' + CAST(@days AS nvarchar(10)) + ', GETDATE())'
  EXEC sp_executesql @sql
  TRUNCATE TABLE tmp_import
END
""",
    ("usp_LogAudit", "P"): """
CREATE PROCEDURE dbo.usp_LogAudit @client varchar(10) AS
INSERT INTO holdings_audit (haclient, hastock, haqty, haaction, hadate)
SELECT vlclient, vlstock, vlquantity, 'S', GETDATE() FROM vaults WHERE vlclient = @client
""",
    ("usp_ClientSummary", "P"): """
CREATE PROCEDURE dbo.usp_ClientSummary @branch varchar(5) AS
SELECT c.clclient, c.clname, dbo.fn_ClientBalance(c.clclient) AS balance, cb.cbbankacc
FROM clients c
LEFT JOIN clientbank cb ON cb.cbclient = c.clclient AND cb.cbprimary = 1
WHERE c.clbranch IN (SELECT brbranch FROM branches WHERE brcity = 'Chennai')
""",
    ("usp_Nightly", "P"): """
CREATE PROCEDURE dbo.usp_Nightly AS
BEGIN
  DECLARE @d datetime = GETDATE()
  EXEC dbo.usp_Settle @d
  EXECUTE usp_PurgeOld 90
END
""",
    ("usp_PlaceOrder", "P"): """
CREATE PROCEDURE dbo.usp_PlaceOrder
    @client varchar(10), @stock varchar(12), @qty int, @type char(1) = 'B', @orderid int = NULL OUTPUT
AS
BEGIN
  SET NOCOUNT ON
  DECLARE @msg nvarchar(400) = N'Insufficient holdings for sell order'
  IF @qty <= 0
     RAISERROR('Quantity must be positive', 16, 1)
  IF NOT EXISTS (SELECT 1 FROM clients WHERE clclient = @client AND clstatus = 'A')
  BEGIN
     RAISERROR(50001, 16, 1, @client)
     RETURN -1
  END
  IF @type NOT IN ('B', 'S')
     THROW 50010, 'Invalid order type', 1;
  BEGIN TRY
     BEGIN TRAN
     IF @type = 'S' AND (SELECT ISNULL(SUM(vlquantity), 0) FROM vaults WHERE vlclient = @client AND vlstock = @stock) < @qty
     BEGIN
        RAISERROR(@msg, 16, 1)
     END
     ELSE
        RAISERROR('Order accepted for %s', 10, 1, @client) WITH NOWAIT
     INSERT INTO trade (trdclient, trdstock, trdquantity, trdtype) VALUES (@client, @stock, @qty, @type)
     SET @orderid = SCOPE_IDENTITY()
     COMMIT
  END TRY
  BEGIN CATCH
     IF @@TRANCOUNT > 0 ROLLBACK
     THROW;
  END CATCH
  RETURN 0
END
""",
    ("usp_Legacy", "P"): None,   # encrypted
    ("trg_vaults_audit", "TR"): """
CREATE TRIGGER dbo.trg_vaults_audit ON dbo.vaults AFTER UPDATE, DELETE AS
BEGIN
  IF UPDATE(vlquantity)
    INSERT INTO holdings_audit (haclient, hastock, haqty, haaction)
    SELECT i.vlclient, i.vlstock, i.vlquantity, 'U' FROM inserted i
END
""",
    ("fn_ClientBalance", "FN"): """
CREATE FUNCTION dbo.fn_ClientBalance (@c varchar(10)) RETURNS decimal(18,2) AS
BEGIN
  RETURN (SELECT SUM(ldgamount) FROM ledger WHERE ldgclient = @c)
END
""",
}
def _job(jid, name, enabled, steps, schedules, history):
    return {"job_id": jid, "name": name, "enabled": enabled, "description": "", "category": "Batch", "owner": "sa",
            "created": "2020-01-01T00:00:00", "modified": "2025-05-01T00:00:00",
            "steps": [{"job_id": jid, "step_id": i, "step_name": n, "subsystem": sub, "command": cmd,
                       "database_name": db, "on_success_action": ok, "on_success_step_id": 0,
                       "on_fail_action": fail, "on_fail_step_id": 0, "retry_attempts": 0, "retry_interval": 0}
                      for i, (n, sub, cmd, db, ok, fail) in enumerate(steps, 1)],
            "schedules": [{"job_id": jid, "enabled": True, "freq_relative_interval": 0, "freq_recurrence_factor": 1,
                           "active_start_date": 20200101, "active_end_date": 99991231, "next_run_date": 20261007,
                           "next_run_time": sc.get("active_start_time", 0), **sc} for sc in schedules],
            "history": history}


JOBS = [
    _job("J1", "EOD - Settlement", True,
         [("Settle trades", "TSQL", "EXEC dbo.usp_Nightly", "BrokerDB", 3, 3),
          ("Export contract notes", "SSIS", "/FILE contract_notes.dtsx", "master", 1, 2)],
         [{"name": "Weekdays 18:30", "freq_type": 8, "freq_interval": 62, "freq_subday_type": 1,
           "freq_subday_interval": 0, "active_start_time": 183000, "active_end_time": 235959}],
         {"runs_30d": 22, "failures_30d": 2, "avg_secs": 1260, "max_secs": 2400, "last_status": 1,
          "last_run_date": 20261006, "last_run_time": 183000, "last_secs": 1180}),
    _job("J2", "Intraday - Holdings audit", True,
         [("Audit", "TSQL", "EXEC usp_LogAudit 'C0001'", "BrokerDB", 1, 1)],
         [{"name": "Every 15 min", "freq_type": 8, "freq_interval": 62, "freq_subday_type": 4,
           "freq_subday_interval": 15, "active_start_time": 91500, "active_end_time": 153000}],
         {"runs_30d": 500, "failures_30d": 0, "avg_secs": 300, "max_secs": 1100, "last_status": 1,
          "last_run_date": 20261006, "last_run_time": 153000, "last_secs": 280}),
    _job("J3", "Other DB backup", True,
         [("Backup", "TSQL", "BACKUP DATABASE OtherDb TO DISK = 'x.bak'", "master", 1, 2)],
         [{"name": "Nightly", "freq_type": 4, "freq_interval": 1, "freq_subday_type": 1,
           "freq_subday_interval": 0, "active_start_time": 10000, "active_end_time": 235959}], {}),
]

TYPE_DESC = {"U": "USER_TABLE", "V": "VIEW", "P": "SQL_STORED_PROCEDURE", "TR": "SQL_TRIGGER",
             "FN": "SQL_SCALAR_FUNCTION"}


def build():
    objects, columns, indexes, fks, deps = [], [], [], [], []
    ids = {}
    oid = 1000
    for t, (cols, pk) in TABLES.items():
        oid += 1
        ids[t] = oid
        objects.append({"id": oid, "schema": "dbo", "name": t, "type": "U", "type_desc": "USER_TABLE",
                        "parent_id": 0, "rows": 1000, "created": "2019-01-01T00:00:00",
                        "modified": MODIFIED.get(t, "2021-06-01T00:00:00")})
        for i, (c, (ty, ln)) in enumerate(cols, 1):
            columns.append({"id": oid, "column_id": i, "name": c, "type": ty, "max_length": ln,
                            "precision": 18 if ty == "decimal" else 0, "scale": 2 if ty == "decimal" else 0,
                            "nullable": c not in pk, "identity": c in ("trdid", "ldgid", "stlid"), "computed": False})
        if pk:
            indexes.append({"id": oid, "name": f"PK_{t}", "is_pk": True, "is_unique": True, "columns": pk})
    for name, child, parent, pairs in FKS:
        fks.append({"name": name, "child_id": ids[child], "parent_id": ids[parent], "disabled": False,
                    "columns": [list(p) for p in pairs]})
    for (name, ty), definition in MODULES.items():
        oid += 1
        ids[name] = oid
        o = {"id": oid, "schema": "dbo", "name": name, "type": ty, "type_desc": TYPE_DESC[ty], "parent_id": 0,
             "definition": definition, "created": "2019-01-01T00:00:00",
             "modified": MODIFIED.get(name, "2022-03-01T00:00:00")}
        if definition is None:
            o["encrypted"] = True
        if ty == "TR":
            o.update(parent_id=ids["vaults"], instead_of=False, disabled=False, events=["UPDATE", "DELETE"])
        objects.append(o)
    # View columns
    for i, (c, src) in enumerate([("clclient", "clients"), ("clname", "clients"), ("vlstock", "vaults"),
                                  ("vlquantity", "vaults")], 1):
        src_col = next(x for x in columns if x["id"] == ids[src] and x["name"] == c)
        columns.append({**src_col, "id": ids["vw_clientholdings"], "column_id": i})
    # sys.sql_expression_dependencies is all we know about the encrypted procedure.
    deps.append({"referencing_id": ids["usp_Legacy"], "ref_server": None, "ref_db": None, "ref_schema": "dbo",
                 "ref_name": "trade", "referenced_id": ids["trade"], "is_ambiguous": False})
    return {"messages": [{"message_id": 50001, "severity": 16, "text": "Client %s is not active"}],
            "jobs": JOBS, "jobs_error": None,
            "meta": {"server": "FIXTURE", "database": "BrokerDB", "version": "fixture", "driver": "none",
                     "extracted_at": "2026-10-07T00:00:00"},
            "objects": objects, "columns": columns, "indexes": indexes, "foreign_keys": fks, "dependencies": deps,
            "constraints": [{"id": ids[t], "kind": k, "name": n, "column_name": c, "definition": d, "disabled": False}
                            for t, k, n, c, d in CONSTRAINTS]}


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "tests/fixture/catalog.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(build(), indent=1), encoding="utf-8")
    print(f"wrote {out}")
