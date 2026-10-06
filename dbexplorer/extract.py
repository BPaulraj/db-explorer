"""Read the SQL Server catalog into a local JSON cache. Only system views are queried."""
import json
from datetime import datetime
from pathlib import Path

from .db import connect, rows

OBJECTS_SQL = """
SELECT o.object_id AS id, s.name AS [schema], o.name, RTRIM(o.type) AS type, o.type_desc,
       o.parent_object_id AS parent_id,
       CONVERT(varchar(19), o.create_date, 126) AS created, CONVERT(varchar(19), o.modify_date, 126) AS modified
FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id
WHERE o.is_ms_shipped = 0 AND RTRIM(o.type) IN ('U','V','P','TR','FN','IF','TF','SN')
"""

MODULES_SQL = """
SELECT m.object_id AS id, m.definition FROM sys.sql_modules m
JOIN sys.objects o ON o.object_id = m.object_id WHERE o.is_ms_shipped = 0
"""

ROWCOUNT_SQL = """
SELECT p.object_id AS id, SUM(p.rows) AS rows FROM sys.partitions p
JOIN sys.tables t ON t.object_id = p.object_id
WHERE p.index_id IN (0, 1) GROUP BY p.object_id
"""

COLUMNS_SQL = """
SELECT c.object_id AS id, c.column_id, c.name, ty.name AS type, c.max_length, c.precision,
       c.scale, c.is_nullable AS nullable, c.is_identity AS [identity], c.is_computed AS computed
FROM sys.columns c
JOIN sys.objects o ON o.object_id = c.object_id
JOIN sys.types ty ON ty.user_type_id = c.user_type_id
WHERE o.is_ms_shipped = 0 AND RTRIM(o.type) IN ('U','V')
ORDER BY c.object_id, c.column_id
"""

INDEXES_SQL = """
SELECT i.object_id AS id, i.name, i.is_primary_key AS is_pk,
       CAST(CASE WHEN i.is_unique = 1 OR i.is_unique_constraint = 1 THEN 1 ELSE 0 END AS bit) AS is_unique,
       c.name AS column_name, ic.key_ordinal
FROM sys.indexes i
JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
JOIN sys.tables t ON t.object_id = i.object_id
WHERE ic.is_included_column = 0 AND i.has_filter = 0
ORDER BY i.object_id, i.index_id, ic.key_ordinal
"""

FK_SQL = """
SELECT fk.name, fk.parent_object_id AS child_id, fk.referenced_object_id AS parent_id,
       pc.name AS child_column, rc.name AS parent_column, fkc.constraint_column_id AS ord,
       fk.is_disabled
FROM sys.foreign_keys fk
JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
JOIN sys.columns pc ON pc.object_id = fkc.parent_object_id AND pc.column_id = fkc.parent_column_id
JOIN sys.columns rc ON rc.object_id = fkc.referenced_object_id AND rc.column_id = fkc.referenced_column_id
ORDER BY fk.object_id, fkc.constraint_column_id
"""

DEPS_SQL = """
SELECT d.referencing_id, d.referenced_server_name AS ref_server, d.referenced_database_name AS ref_db,
       d.referenced_schema_name AS ref_schema, d.referenced_entity_name AS ref_name,
       d.referenced_id, d.is_ambiguous
FROM sys.sql_expression_dependencies d
WHERE d.referenced_class = 1
"""

TRIGGERS_SQL = """
SELECT t.object_id AS id, t.is_instead_of_trigger AS instead_of, t.is_disabled AS disabled,
       te.type_desc AS event
FROM sys.triggers t JOIN sys.trigger_events te ON te.object_id = t.object_id
WHERE t.parent_class = 1
"""

SYNONYMS_SQL = """
SELECT sn.object_id AS id, sn.base_object_name AS base FROM sys.synonyms sn
"""

# Business rules stored in the schema: CHECK constraints, defaults, computed columns.
CONSTRAINTS_SQL = """
SELECT cc.parent_object_id AS id, 'CHECK' AS kind, cc.name, c.name AS column_name, cc.definition,
       cc.is_disabled AS disabled
FROM sys.check_constraints cc
LEFT JOIN sys.columns c ON c.object_id = cc.parent_object_id AND c.column_id = cc.parent_column_id
UNION ALL
SELECT dc.parent_object_id, 'DEFAULT', dc.name, c.name, dc.definition, CAST(0 AS bit)
FROM sys.default_constraints dc
JOIN sys.columns c ON c.object_id = dc.parent_object_id AND c.column_id = dc.parent_column_id
UNION ALL
SELECT cc.object_id, 'COMPUTED', NULL, cc.name, cc.definition, CAST(0 AS bit)
FROM sys.computed_columns cc
JOIN sys.objects o ON o.object_id = cc.object_id WHERE o.is_ms_shipped = 0
"""

PARAMETERS_SQL = """
SELECT p.object_id AS id, p.parameter_id AS ord, p.name, ty.name AS type, p.max_length, p.precision,
       p.scale, p.is_output AS is_output, p.is_readonly AS is_readonly
FROM sys.parameters p
JOIN sys.objects o ON o.object_id = p.object_id
JOIN sys.types ty ON ty.user_type_id = p.user_type_id
WHERE o.is_ms_shipped = 0 AND p.parameter_id > 0
ORDER BY p.object_id, p.parameter_id
"""


MESSAGES_SQL = """
SELECT message_id, severity, text FROM sys.messages WHERE message_id >= 50000 AND language_id = 1033
"""

# SQL Agent (msdb). Reading these needs SQLAgentReaderRole (or higher) in msdb.
JOBS_SQL = """
SELECT CONVERT(varchar(36), j.job_id) AS job_id, j.name, j.enabled, j.description, c.name AS category,
       SUSER_SNAME(j.owner_sid) AS owner,
       CONVERT(varchar(19), j.date_created, 126) AS created, CONVERT(varchar(19), j.date_modified, 126) AS modified
FROM msdb.dbo.sysjobs j LEFT JOIN msdb.dbo.syscategories c ON c.category_id = j.category_id
"""
JOB_STEPS_SQL = """
SELECT CONVERT(varchar(36), s.job_id) AS job_id, s.step_id, s.step_name, s.subsystem, s.command, s.database_name,
       s.on_success_action, s.on_success_step_id, s.on_fail_action, s.on_fail_step_id,
       s.retry_attempts, s.retry_interval
FROM msdb.dbo.sysjobsteps s ORDER BY s.job_id, s.step_id
"""
JOB_SCHEDULES_SQL = """
SELECT CONVERT(varchar(36), js.job_id) AS job_id, s.name, s.enabled, s.freq_type, s.freq_interval,
       s.freq_subday_type, s.freq_subday_interval, s.freq_relative_interval, s.freq_recurrence_factor,
       s.active_start_time, s.active_end_time, s.active_start_date, s.active_end_date,
       js.next_run_date, js.next_run_time
FROM msdb.dbo.sysjobschedules js JOIN msdb.dbo.sysschedules s ON s.schedule_id = js.schedule_id
"""
JOB_HISTORY_SQL = """
WITH h AS (
    SELECT job_id, run_status, run_date, run_time,
           (run_duration / 10000) * 3600 + (run_duration / 100 % 100) * 60 + run_duration % 100 AS secs,
           ROW_NUMBER() OVER (PARTITION BY job_id ORDER BY run_date DESC, run_time DESC) AS rn
    FROM msdb.dbo.sysjobhistory WHERE step_id = 0
)
SELECT CONVERT(varchar(36), job_id) AS job_id,
       SUM(CASE WHEN run_date >= CONVERT(int, CONVERT(char(8), DATEADD(day, -30, GETDATE()), 112)) THEN 1 ELSE 0 END) AS runs_30d,
       SUM(CASE WHEN run_status = 0 AND run_date >= CONVERT(int, CONVERT(char(8), DATEADD(day, -30, GETDATE()), 112))
                THEN 1 ELSE 0 END) AS failures_30d,
       AVG(secs) AS avg_secs, MAX(secs) AS max_secs,
       MAX(CASE WHEN rn = 1 THEN run_status END) AS last_status,
       MAX(CASE WHEN rn = 1 THEN run_date END) AS last_run_date,
       MAX(CASE WHEN rn = 1 THEN run_time END) AS last_run_time,
       MAX(CASE WHEN rn = 1 THEN secs END) AS last_secs
FROM h GROUP BY job_id
"""


def extract_jobs(cur):
    """All SQL Agent jobs with steps, schedules and run history. Raises if msdb is not readable."""
    jobs = {j["job_id"]: {**j, "enabled": bool(j["enabled"]), "steps": [], "schedules": [], "history": {}}
            for j in rows(cur, JOBS_SQL)}
    for s in rows(cur, JOB_STEPS_SQL):
        if s["job_id"] in jobs:
            jobs[s["job_id"]]["steps"].append(s)
    for s in rows(cur, JOB_SCHEDULES_SQL):
        if s["job_id"] in jobs:
            jobs[s["job_id"]]["schedules"].append({**s, "enabled": bool(s["enabled"])})
    try:
        for h in rows(cur, JOB_HISTORY_SQL):
            if h["job_id"] in jobs:
                jobs[h["job_id"]]["history"] = {k: v for k, v in h.items() if k != "job_id"}
    except Exception:
        pass   # history can be purged or restricted; jobs are still useful without it
    return list(jobs.values())


def extract(cfg, out_path):
    cn, driver = connect(cfg.connection, timeout=120)
    cur = cn.cursor()
    meta = rows(cur, "SELECT @@SERVERNAME AS server, DB_NAME() AS [database], @@VERSION AS version")[0]

    objects = {o["id"]: o for o in rows(cur, OBJECTS_SQL)}
    for m in rows(cur, MODULES_SQL):
        if m["id"] in objects:
            objects[m["id"]]["definition"] = m["definition"]
    for r in rows(cur, ROWCOUNT_SQL):
        if r["id"] in objects:
            objects[r["id"]]["rows"] = int(r["rows"])
    for t in rows(cur, TRIGGERS_SQL):
        o = objects.get(t["id"])
        if o:
            o["instead_of"] = bool(t["instead_of"])
            o["disabled"] = bool(t["disabled"])
            o.setdefault("events", []).append(t["event"])
    for s in rows(cur, SYNONYMS_SQL):
        if s["id"] in objects:
            objects[s["id"]]["base"] = s["base"]
    for o in objects.values():
        if o["type"] in ("P", "V", "TR", "FN", "IF", "TF") and o.get("definition") is None:
            o["encrypted"] = True

    columns = rows(cur, COLUMNS_SQL)

    indexes = {}
    for r in rows(cur, INDEXES_SQL):
        key = (r["id"], r["name"])
        ix = indexes.setdefault(key, {"id": r["id"], "name": r["name"], "is_pk": bool(r["is_pk"]),
                                      "is_unique": bool(r["is_unique"]), "columns": []})
        ix["columns"].append(r["column_name"])

    fks = {}
    for r in rows(cur, FK_SQL):
        fk = fks.setdefault(r["name"], {"name": r["name"], "child_id": r["child_id"],
                                        "parent_id": r["parent_id"], "disabled": bool(r["is_disabled"]),
                                        "columns": []})
        fk["columns"].append([r["child_column"], r["parent_column"]])

    deps = rows(cur, DEPS_SQL)
    constraints = rows(cur, CONSTRAINTS_SQL)
    for p in rows(cur, PARAMETERS_SQL):
        if p["id"] in objects:
            objects[p["id"]].setdefault("parameters", []).append(p)
    messages = rows(cur, MESSAGES_SQL)
    jobs, jobs_error = [], None
    try:
        jobs = extract_jobs(cur)
    except Exception as ex:
        jobs_error = str(ex).splitlines()[0][:300]
    cn.close()

    catalog = {
        "meta": {**meta, "driver": driver, "extracted_at": datetime.now().isoformat(timespec="seconds")},
        "objects": list(objects.values()),
        "columns": columns,
        "indexes": list(indexes.values()),
        "foreign_keys": list(fks.values()),
        "dependencies": deps,
        "constraints": constraints,
        "messages": messages,
        "jobs": jobs,
        "jobs_error": jobs_error,
    }
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(catalog, indent=1, default=str), encoding="utf-8")
    counts = {}
    for o in objects.values():
        counts[o["type_desc"]] = counts.get(o["type_desc"], 0) + 1
    counts["SQL_AGENT_JOBS"] = len(jobs) if not jobs_error else f"unavailable ({jobs_error})"
    return catalog, counts
