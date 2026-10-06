"""Column profiling with small, read-only sampled queries (NOLOCK, TABLESAMPLE, timeout)."""
import fnmatch
import json
from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from .db import connect
from .prefix import detect_prefixes, logical_name
from .sensitive import by_name, classify, shape

NO_STATS = {"text", "ntext", "image", "xml", "geography", "geometry", "hierarchyid", "sql_variant",
            "varbinary", "binary", "timestamp", "rowversion"}
CHAR = {"char", "varchar", "nchar", "nvarchar"}
NUMERIC = {"tinyint", "smallint", "int", "bigint", "decimal", "numeric", "money", "smallmoney", "float", "real"}
DATES = {"date", "datetime", "datetime2", "smalldatetime", "datetimeoffset", "time"}
CHUNK = 40


def _q(name):
    return "[" + name.replace("]", "]]") + "]"


def _val(v):
    if isinstance(v, Decimal):
        return float(v) if v == v.to_integral_value() or abs(v) < 1e15 else str(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, bytes):
        return "0x" + v.hex()[:20]
    return v


def _sample_source(obj, rows, sample):
    if rows is not None and rows <= sample:
        return f"SELECT * FROM {obj} WITH (NOLOCK)", True
    pct = min(100.0, max(0.001, sample * 150.0 / max(rows or sample, 1)))
    return f"SELECT TOP ({int(sample)}) * FROM {obj} TABLESAMPLE SYSTEM ({pct:.4f} PERCENT) WITH (NOLOCK)", False


def _stat_exprs(i, c):
    """SELECT-list expressions for one column; aliases are suffixed with the column's index."""
    n, t = _q(c["name"]), c["type"].lower()
    is_max = c["max_length"] == -1
    ex = [f"SUM(CASE WHEN {n} IS NULL THEN 1 ELSE 0 END) AS nul_{i}"]
    if t in NO_STATS:
        return ex
    if not is_max:
        ex.append(f"COUNT(DISTINCT {n}) AS dis_{i}")
    if t in CHAR:
        ex += [f"MIN(LEN({n})) AS mnl_{i}", f"MAX(LEN({n})) AS mxl_{i}",
               f"SUM(CASE WHEN LTRIM(RTRIM({n})) = '' THEN 1 ELSE 0 END) AS blk_{i}"]
    elif t in NUMERIC:
        ex += [f"MIN({n}) AS mn_{i}", f"MAX({n}) AS mx_{i}",
               f"SUM(CASE WHEN {n} = 0 THEN 1 ELSE 0 END) AS zer_{i}",
               f"SUM(CASE WHEN {n} < 0 THEN 1 ELSE 0 END) AS neg_{i}"]
    elif t in DATES:
        ex += [f"MIN({n}) AS mn_{i}", f"MAX({n}) AS mx_{i}"]
    return ex


def profile_table(cur, display, rows, columns, logical, cfg):
    table = display.split(".", 1)[-1]
    schema, name = display.split(".", 1)
    obj = f"{_q(schema)}.{_q(name)}"
    src, full = _sample_source(obj, rows, cfg.profile_sample_rows)
    cols = list(columns)

    def run(select_list, source):
        cur.execute(f"WITH s AS ({source}) SELECT {select_list} FROM s")
        r = cur.fetchone()
        return dict(zip([d[0] for d in cur.description], r))

    # 1. Aggregate stats, chunked to keep each statement manageable.
    stats = {}
    n_rows = None
    for start in range(0, len(cols), CHUNK):
        chunk = list(enumerate(cols))[start:start + CHUNK]
        select = ", ".join(["COUNT(*) AS n_rows"] + [e for i, c in chunk for e in _stat_exprs(i, c)])
        res = run(select, src)
        if not res["n_rows"] and not full:
            # The page sample came back empty (tiny percent on a small table) - read the first rows instead.
            src, full = f"SELECT TOP ({int(cfg.profile_sample_rows)}) * FROM {obj} WITH (NOLOCK)", False
            res = run(select, src)
        n_rows = res["n_rows"]
        stats.update(res)

    out = {"sampled_rows": n_rows, "full_scan": full, "columns": {}}
    if not n_rows:
        for c in cols:
            out["columns"][c["name"].lower()] = {"null_pct": None, "distinct": 0}
        return out

    # 2. Value lists for low-cardinality columns, sample values for the rest (pattern / sensitivity checks).
    lists, samples = [], []
    for i, c in enumerate(cols):
        t = c["type"].lower()
        d = stats.get(f"dis_{i}")
        if t in NO_STATS or c["max_length"] == -1 or d is None or d == 0:
            continue
        if by_name(logical.get(c["name"].lower()), c["name"]) or classify(c["name"], logical.get(c["name"].lower(), ""),
                                                                         None, cfg, table):
            if t in CHAR:
                samples.append((i, c))   # needed for the format; values are reduced to shapes below
            continue
        if d <= cfg.value_list_max and t not in DATES and t not in ("float", "real", "money", "smallmoney") \
                and not (t in ("decimal", "numeric") and c.get("scale")):
            lists.append((i, c))
        elif t in CHAR:
            samples.append((i, c))

    values = {}
    for start in range(0, len(lists), CHUNK):
        parts = [f"SELECT {i} AS col, CAST({_q(c['name'])} AS nvarchar(200)) AS v, COUNT(*) AS n FROM s "
                 f"GROUP BY {_q(c['name'])}" for i, c in lists[start:start + CHUNK]]
        cur.execute(f"WITH s AS ({src}) " + " UNION ALL ".join(parts))
        for col, v, cnt in cur.fetchall():
            values.setdefault(col, []).append([v, cnt])
    sampled = {}
    for start in range(0, len(samples), CHUNK):
        parts = [f"SELECT * FROM (SELECT TOP 50 {i} AS col, CAST({_q(c['name'])} AS nvarchar(200)) AS v FROM s "
                 f"WHERE {_q(c['name'])} IS NOT NULL GROUP BY {_q(c['name'])}) x{i}" for i, c in samples[start:start + CHUNK]]
        cur.execute(f"WITH s AS ({src}) " + " UNION ALL ".join(parts))
        for col, v in cur.fetchall():
            sampled.setdefault(col, []).append(v)

    # 3. Assemble, removing raw content from sensitive columns.
    for i, c in enumerate(cols):
        key = c["name"].lower()
        t = c["type"].lower()
        nul = stats.get(f"nul_{i}") or 0
        p = {"null_pct": round(100.0 * nul / n_rows, 2), "distinct": stats.get(f"dis_{i}")}
        for src_key, dst in (("mnl", "min_len"), ("mxl", "max_len"), ("blk", "blank"), ("mn", "min"),
                             ("mx", "max"), ("zer", "zeros"), ("neg", "negatives")):
            v = stats.get(f"{src_key}_{i}")
            if v is not None:
                p[dst] = _val(v)
        vals = [v for v, _ in values.get(i, [])] + sampled.get(i, [])
        cat = classify(key, logical.get(key, key), vals, cfg, table)
        if t in CHAR and vals:
            shapes = Counter(shape(v) for v in vals)
            total = sum(shapes.values())
            p["formats"] = [[s, round(100.0 * k / total)] for s, k in shapes.most_common(3)]
        if cat:
            p["sensitive"] = cat
            p["sensitive_basis"] = "name" if classify(key, logical.get(key, key), None, cfg, table) else "values"
            p.pop("min", None)
            p.pop("max", None)
        else:
            if i in values:
                p["values"] = sorted(values[i], key=lambda x: -x[1])
            if cfg.example_values and sampled.get(i):
                p["examples"] = sampled[i][: cfg.example_values]
        out["columns"][key] = p
    return out


def run_profile(catalog, cfg, out_path, only=None, fresh=False, log=print):
    path = Path(out_path)
    results = {} if fresh or not path.exists() else json.loads(path.read_text(encoding="utf-8"))
    prefixes = detect_prefixes(catalog, cfg)
    tables = []
    for o in sorted(catalog.tables(), key=lambda o: o["key"]):
        if any(fnmatch.fnmatch(o["name"].lower(), pat) for pat in cfg.skip_tables):
            continue
        if only and o["key"] not in only and o["name"].lower() not in only:
            continue
        if o["key"] in results and not results[o["key"]].get("error") and not only:
            continue
        tables.append(o)
    log(f"Profiling {len(tables)} tables (sample {cfg.profile_sample_rows} rows, timeout {cfg.profile_timeout}s)...")
    cn, _ = connect(cfg.connection, timeout=cfg.profile_timeout)
    cur = cn.cursor()
    for n, o in enumerate(tables, 1):
        pref = prefixes.get(o["key"], {}).get("prefix", "")
        logical = {c["name"].lower(): logical_name(c["name"], pref, cfg) for c in catalog.columns.get(o["key"], [])}
        try:
            r = profile_table(cur, o["display"], o.get("rows"), catalog.columns.get(o["key"], []), logical, cfg)
        except Exception as ex:
            r = {"error": str(ex).splitlines()[0][:300]}
        r["profiled_at"] = datetime.now().isoformat(timespec="seconds")
        results[o["key"]] = r
        log(f"  [{n}/{len(tables)}] {o['display']}: " + (r["error"] if "error" in r else f"{r['sampled_rows']} rows"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(results, indent=1, default=_val), encoding="utf-8")
    cn.close()
    return results
