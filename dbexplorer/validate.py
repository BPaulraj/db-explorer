"""Check inferred relationships against real data with small, read-only sampled queries."""
import json
from pathlib import Path

from .db import connect

CHAR = {"char", "varchar", "nchar", "nvarchar"}


def _q(name):
    return "[" + name.replace("]", "]]") + "]"


def _obj(display):
    schema, name = display.split(".", 1)
    return f"{_q(schema)}.{_q(name)}"


def overlap_sql(child_tbl, child_col, parent_tbl, parent_col, sample, collate):
    c = f"x.k{' COLLATE DATABASE_DEFAULT' if collate else ''}"
    p = f"p.{_q(parent_col)}{' COLLATE DATABASE_DEFAULT' if collate else ''}"
    return f"""
SELECT COUNT(*) AS sampled,
       SUM(CASE WHEN EXISTS (SELECT 1 FROM {parent_tbl} p WITH (NOLOCK) WHERE {p} = {c}) THEN 1 ELSE 0 END) AS matched
FROM (SELECT DISTINCT s.k FROM (SELECT TOP ({int(sample)}) {_q(child_col)} AS k
                                FROM {child_tbl} WITH (NOLOCK) WHERE {_q(child_col)} IS NOT NULL) s) x
"""


def run_validation(doc, cfg, out_path, log=print):
    tables = {t["key"]: t for t in doc["tables"]}
    coltype = {(t["key"], c["name"].lower()): c["type"].split("(")[0] for t in doc["tables"] for c in t["columns"]}
    path = Path(out_path)
    results = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    todo = [e for e in doc["edges"] if "fk" not in e["sources"]
            and cfg.validate_min <= e["confidence"] <= cfg.validate_max and e["id"] not in results]
    log(f"Validating {len(todo)} relationships (sample {cfg.sample_rows} rows, timeout {cfg.query_timeout}s)...")
    cn, _ = connect(cfg.connection, timeout=cfg.query_timeout)
    cur = cn.cursor()
    for i, e in enumerate(todo, 1):
        # child -> parent if direction is known, otherwise a -> b
        child, parent = (e["b"], e["a"]) if e["parent"] == "a" else (e["a"], e["b"])
        ta, tb = coltype.get((child["table"], child["column"])), coltype.get((parent["table"], parent["column"]))
        collate = ta in CHAR and tb in CHAR
        sql = overlap_sql(_obj(tables[child["table"]]["display"]), child["column"],
                          _obj(tables[parent["table"]]["display"]), parent["column"], cfg.sample_rows, collate)
        try:
            cur.execute(sql)
            sampled, matched = cur.fetchone()
            matched = matched or 0
            results[e["id"]] = {"sampled": sampled, "matched": matched,
                                "orphan_pct": round(100 * (sampled - matched) / sampled, 2) if sampled else 0,
                                "checked": f"{child['table']}.{child['column']} in {parent['table']}.{parent['column']}"}
        except Exception as ex:
            results[e["id"]] = {"error": str(ex).splitlines()[0], "sampled": 0, "matched": 0, "orphan_pct": 0}
        if i % 20 == 0 or i == len(todo):
            log(f"  {i}/{len(todo)}")
            path.write_text(json.dumps(results, indent=1), encoding="utf-8")
    cn.close()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(results, indent=1), encoding="utf-8")
    return results
